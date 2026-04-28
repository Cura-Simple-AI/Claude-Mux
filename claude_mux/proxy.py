"""
claude-mux proxy — lightweight reverse proxy for Anthropic API.

Reads from env:
  PROXY_TARGET_URL   upstream base (e.g. https://api.anthropic.com/v1)
  PROXY_AUTH_TOKEN   Bearer token sent to upstream
  LISTEN_ADDR        bind address (default :18080)
  LOG_FILE           optional log file path (default stderr)

Usage:
  python3 -m claude_mux.proxy
"""

import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

LOG_FILE = os.environ.get("LOG_FILE")
LISTEN_ADDR = os.environ.get("LISTEN_ADDR", ":18080")
TARGET_URL = os.environ.get("PROXY_TARGET_URL", "").rstrip("/")
AUTH_TOKEN = os.environ.get("PROXY_AUTH_TOKEN", "")
# When set, log full request/response headers and bodies to stdout
DEBUG = os.environ.get("PROXY_DEBUG", "") == "1"

_CLAUDE_MUX_DIR = os.path.join(os.path.expanduser("~"), ".claude-mux")
_USAGE_LOG = os.path.join(_CLAUDE_MUX_DIR, "usage.log")
_RATE_LIMITS_FILE = os.path.join(_CLAUDE_MUX_DIR, "rate-limits.json")


def _record_usage(input_tokens: int, output_tokens: int, model: str) -> None:
    """Append a token-usage entry to usage.log for 5h/7d window tracking."""
    if not input_tokens and not output_tokens:
        return
    entry = {
        "ts": int(time.time()),
        "in": input_tokens,
        "out": output_tokens,
        "model": model,
    }
    try:
        os.makedirs(_CLAUDE_MUX_DIR, exist_ok=True)
        with open(_USAGE_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _record_rate_limits(resp_headers) -> None:
    """Cache Anthropic rate-limit headers to rate-limits.json."""
    rl: dict = {}
    for header, key in [
        ("anthropic-ratelimit-tokens-limit", "tokens_limit"),
        ("anthropic-ratelimit-tokens-remaining", "tokens_remaining"),
        ("anthropic-ratelimit-tokens-reset", "tokens_reset"),
        ("anthropic-ratelimit-requests-limit", "requests_limit"),
        ("anthropic-ratelimit-requests-remaining", "requests_remaining"),
        ("anthropic-ratelimit-input-tokens-limit", "input_tokens_limit"),
        ("anthropic-ratelimit-input-tokens-remaining", "input_tokens_remaining"),
        ("anthropic-ratelimit-output-tokens-limit", "output_tokens_limit"),
        ("anthropic-ratelimit-output-tokens-remaining", "output_tokens_remaining"),
    ]:
        val = resp_headers.get(header)
        if val is not None:
            try:
                rl[key] = int(val)
            except ValueError:
                rl[key] = val
    if rl:
        rl["ts"] = int(time.time())
        try:
            os.makedirs(_CLAUDE_MUX_DIR, exist_ok=True)
            with open(_RATE_LIMITS_FILE, "w") as f:
                json.dump(rl, f)
        except OSError:
            pass

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("proxy")


def _log_request(method: str, path: str, status: int, elapsed_ms: int, model: str = ""):
    """Structured JSON log line — parsable by TUI _last_http_status.
    Written to stdout so PM2 captures in out.log."""
    entry = {
        "msg": "http request",
        "method": method,
        "path": path,
        "status": status,
        "time": int(time.time() * 1000),
        "elapsed_ms": elapsed_ms,
    }
    if model:
        entry["model"] = model
    sys.stdout.write(json.dumps(entry) + "\n")
    sys.stdout.flush()


def _parse_sse_usage(body: bytes) -> tuple:
    """Parse SSE event stream and extract model + token counts.

    Anthropic streaming responses contain:
      message_start → message.model + message.usage (input + cache tokens)
      message_delta → usage.output_tokens (final cumulative count)

    Cache tokens are included in input_tokens so OAuth/Max users (who use
    prompt caching heavily) get accurate usage tracking.

    Returns (model, input_tokens, output_tokens).
    """
    model = ""
    input_tokens = 0
    output_tokens = 0
    try:
        text = body.decode("utf-8", errors="replace")
        for line in text.split("\n"):
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            t = data.get("type", "")
            if t == "message_start":
                msg = data.get("message", {})
                model = msg.get("model", model)
                usage = msg.get("usage", {})
                input_tokens = (
                    usage.get("input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                )
            elif t == "message_delta":
                usage = data.get("usage", {})
                output_tokens = usage.get("output_tokens", output_tokens)
    except Exception:
        pass
    return model, input_tokens, output_tokens


def _handle(conn, addr):
    """Handle one connection — read request, forward, stream response."""
    try:
        req = _parse_request(conn)
        if req is None:
            return
        method, path, headers, body, qs = req

        # Health check
        if path == "/health":
            _send_response(conn, 200, b'{"status":"ok"}', ctype="application/json")
            return

        # Only forward /v1/* paths
        if not path.startswith("/v1/"):
            _send_response(conn, 404, b"Not Found")
            return

        upstream = f"{TARGET_URL}{path}"
        if qs:
            upstream += "?" + qs
        t0 = time.time()

        # Forward ALL client headers transparently — except auth.
        # Proxy always injects its own PROXY_AUTH_TOKEN; client auth is
        # irrelevant (Claude Code 2.1.118+ sends its own OAuth token which
        # would override the proxy's upstream auth).
        req_headers = {}
        # Strip hop-by-hop + accept-encoding (proxy doesn't decompress gzip)
        skip = {"host", "content-length", "connection", "transfer-encoding",
                "accept-encoding", "authorization", "x-api-key"}
        for k, v in headers.items():
            if k.lower() not in skip:
                req_headers[k] = v

        # Always inject proxy auth token (never forward client auth)
        if AUTH_TOKEN:
            req_headers["Authorization"] = f"Bearer {AUTH_TOKEN}"

        # Ensure Content-Type is set for requests with a body.
        if body and "content-type" not in req_headers:
            req_headers["content-type"] = "application/json"

        if DEBUG:
            # Write debug directly to stdout so PM2 captures it in out.log
            def _dbg(msg):
                sys.stdout.write(f"[DEBUG] {msg}\n")
                sys.stdout.flush()
            client_hdrs_safe = {k: v for k, v in headers.items() if k != "authorization"}
            _dbg(f"CLIENT->PROXY headers={json.dumps(client_hdrs_safe)}")
            _dbg(f"CLIENT->PROXY body={body[:2000]!r}")
            upstream_hdrs_safe = {k: ("Bearer [REDACTED]" if k == "Authorization" else v)
                                   for k, v in req_headers.items()}
            _dbg(f"PROXY->UPSTREAM url={upstream} headers={json.dumps(upstream_hdrs_safe)}")
            _dbg(f"PROXY->UPSTREAM body={body[:2000]!r}")

        data = body if body else None
        upstream_req = urllib.request.Request(upstream, data=data, headers=req_headers, method=method)

        try:
            upstream_resp = urllib.request.urlopen(upstream_req, timeout=120)
            status = upstream_resp.status
            ctype = upstream_resp.headers.get("Content-Type", "application/json")
            is_sse = "text/event-stream" in ctype

            # Cache rate-limit headers immediately (available before body)
            _record_rate_limits(upstream_resp.headers)

            if is_sse:
                # Streaming: forward chunks as they arrive so Claude Code
                # gets tokens immediately (no buffering delay).
                # Collect chunks in parallel for usage tracking.
                status_text = {200: "OK", 429: "Too Many Requests",
                               500: "Internal Server Error"}.get(status, "Unknown")
                header = (
                    f"HTTP/1.1 {status} {status_text}\r\n"
                    f"Content-Type: {ctype}\r\n"
                    f"Transfer-Encoding: chunked\r\n"
                    f"Connection: close\r\n"
                    f"Access-Control-Allow-Origin: *\r\n"
                    f"\r\n"
                ).encode()
                try:
                    conn.sendall(header)
                except OSError:
                    return

                chunks: list = []
                while True:
                    chunk = upstream_resp.read(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    # HTTP/1.1 chunked encoding
                    try:
                        conn.sendall(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    except OSError:
                        break
                try:
                    conn.sendall(b"0\r\n\r\n")  # chunked terminator
                except OSError:
                    pass

                elapsed = int((time.time() - t0) * 1000)
                resp_body = b"".join(chunks)
                model, input_tokens, output_tokens = _parse_sse_usage(resp_body)
                _record_usage(input_tokens, output_tokens, model)

                if DEBUG:
                    _dbg(f"UPSTREAM->PROXY SSE status={status} chunks={len(chunks)}")
                    _dbg(f"UPSTREAM->PROXY body={resp_body[:2000].decode('utf-8', errors='replace')}")
                _log_request(method, path, status, elapsed, model)
                # Connection already closed via chunked — skip _send_response
                try:
                    conn.close()
                except OSError:
                    pass
            else:
                # Non-streaming: buffer + send
                resp_body = upstream_resp.read()
                elapsed = int((time.time() - t0) * 1000)

                model = ""
                try:
                    resp_json = json.loads(resp_body)
                    model = resp_json.get("model", "")
                    usage = resp_json.get("usage", {})
                    _record_usage(
                        usage.get("input_tokens", 0),
                        usage.get("output_tokens", 0),
                        model,
                    )
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

                if DEBUG:
                    _dbg(f"UPSTREAM->PROXY status={status} headers={json.dumps(dict(upstream_resp.headers))}")
                    _dbg(f"UPSTREAM->PROXY body={resp_body[:2000].decode('utf-8', errors='replace')}")

                _log_request(method, path, status, elapsed, model)
                _send_response(conn, status, resp_body, ctype=ctype)

        except urllib.error.HTTPError as e:
            elapsed = int((time.time() - t0) * 1000)
            err_body = e.read()
            if DEBUG:
                _dbg(f"UPSTREAM->PROXY error={e.code} headers={json.dumps(dict(e.headers))}")
                _dbg(f"UPSTREAM->PROXY body={err_body[:2000].decode('utf-8', errors='replace')}")
            _log_request(method, path, e.code, elapsed)
            _send_response(conn, e.code, err_body, ctype="application/json")

        except urllib.error.URLError as e:
            elapsed = int((time.time() - t0) * 1000)
            _log_request(method, path, 502, elapsed)
            err = json.dumps({"error": {"message": str(e.reason)}}).encode()
            _send_response(conn, 502, err, ctype="application/json")

    except Exception:
        log.exception("request handler error")


def _parse_request(conn):
    """Read and parse HTTP/1.1 request from socket."""
    try:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            data += chunk
            if len(data) > 65536:
                return None

        head, _, rest = data.partition(b"\r\n\r\n")
        head_str = head.decode("utf-8", errors="replace")
        lines = head_str.split("\r\n")

        request_line = lines[0].split(" ")
        if len(request_line) < 2:
            return None
        method = request_line[0]
        path = request_line[1].split("?")[0]
        qs = request_line[1].split("?")[1] if "?" in request_line[1] else ""

        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        # Read body — stay in bytes to avoid UTF-8 length mismatch with Content-Length
        body = b""
        content_len = int(headers.get("content-length", 0))
        if content_len > 0:
            body = rest
            while len(body) < content_len:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                body += chunk

        return method, path, headers, body, qs
    except Exception:
        log.exception("parse request error")
        return None


def _send_response(conn, status: int, body: bytes, ctype: str = "application/json"):
    """Send HTTP/1.1 response."""
    status_text = {200: "OK", 400: "Bad Request", 401: "Unauthorized",
                   404: "Not Found", 429: "Too Many Requests", 500: "Internal Server Error",
                   502: "Bad Gateway", 503: "Service Unavailable"}.get(status, "Unknown")
    resp = (
        f"HTTP/1.1 {status} {status_text}\r\n"
        f"Content-Type: {ctype}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"Access-Control-Allow-Origin: *\r\n"
        f"\r\n"
    ).encode()
    try:
        conn.sendall(resp + body)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    host, _, port_str = LISTEN_ADDR.partition(":")
    port = int(port_str) if port_str else 18080
    bind_addr = (host or "0.0.0.0", port)

    if not TARGET_URL:
        print("PROXY_TARGET_URL not set", file=sys.stderr)
        sys.exit(1)
    if not AUTH_TOKEN:
        print("PROXY_AUTH_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(bind_addr)
    s.listen(128)
    s.settimeout(1.0)

    log.info("proxy listening on %s -> %s", LISTEN_ADDR, TARGET_URL)

    try:
        while True:
            try:
                conn, addr = s.accept()
            except socket.timeout:
                continue
            threading.Thread(target=_handle, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        s.close()


if __name__ == "__main__":
    main()
