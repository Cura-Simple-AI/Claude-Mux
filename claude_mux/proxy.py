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
import time
import urllib.error
import urllib.request

LOG_FILE = os.environ.get("LOG_FILE")
LISTEN_ADDR = os.environ.get("LISTEN_ADDR", ":18080")
TARGET_URL = os.environ.get("PROXY_TARGET_URL", "").rstrip("/")
AUTH_TOKEN = os.environ.get("PROXY_AUTH_TOKEN", "")
# When set, log full request/response headers and bodies to stdout
DEBUG = os.environ.get("PROXY_DEBUG", "") == "1"

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


def _handle(conn, addr):
    """Handle one connection — read request, forward, stream response."""
    try:
        req = _parse_request(conn)
        if req is None:
            return
        method, path, headers, body = req

        # Health check
        if path == "/health":
            _send_response(conn, 200, b'{"status":"ok"}', ctype="application/json")
            return

        # Only forward /v1/* paths
        if not path.startswith("/v1/"):
            _send_response(conn, 404, b"Not Found")
            return

        upstream = f"{TARGET_URL}{path}"
        t0 = time.time()

        req_headers = {
            "Authorization": f"Bearer {AUTH_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # anthropic-version: forward from client or use default
        req_headers["anthropic-version"] = headers.get("anthropic-version", "2023-06-01")
        # oauth-2025-04-20 is required for OAuth tokens to be accepted by Anthropic.
        # Merge it with any beta flags the client sends (e.g. interleaved-thinking, context-management).
        client_betas = [b.strip() for b in headers.get("anthropic-beta", "").split(",") if b.strip()]
        all_betas = list(dict.fromkeys(["oauth-2025-04-20"] + client_betas))  # deduplicated, oauth first
        req_headers["anthropic-beta"] = ",".join(all_betas)
        # Forward other Anthropic-specific headers the client may send
        for hdr in ("anthropic-dangerous-direct-browser-access", "x-app"):
            if hdr in headers:
                req_headers[hdr] = headers[hdr]

        if DEBUG:
            # Write debug directly to stdout so PM2 captures it in out.log
            def _dbg(msg):
                sys.stdout.write(f"[DEBUG] {msg}\n")
                sys.stdout.flush()
            client_hdrs_safe = {k: v for k, v in headers.items() if k != "authorization"}
            _dbg(f"CLIENT→PROXY headers={json.dumps(client_hdrs_safe)}")
            _dbg(f"CLIENT→PROXY body={((body or '')[:2000])}")
            upstream_hdrs_safe = {k: ("Bearer [REDACTED]" if k == "Authorization" else v)
                                   for k, v in req_headers.items()}
            _dbg(f"PROXY→UPSTREAM url={upstream} headers={json.dumps(upstream_hdrs_safe)}")
            _dbg(f"PROXY→UPSTREAM body={((body or '')[:2000])}")

        data = body.encode() if body else None
        upstream_req = urllib.request.Request(upstream, data=data, headers=req_headers, method=method)

        try:
            upstream_resp = urllib.request.urlopen(upstream_req, timeout=120)
            resp_body = upstream_resp.read()
            status = upstream_resp.status
            elapsed = int((time.time() - t0) * 1000)

            # Extract model from response if present
            model = ""
            try:
                resp_json = json.loads(resp_body)
                model = resp_json.get("model", "")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

            if DEBUG:
                _dbg(f"UPSTREAM→PROXY status={status} headers={json.dumps(dict(upstream_resp.headers))}")
                _dbg(f"UPSTREAM→PROXY body={resp_body[:2000].decode('utf-8', errors='replace')}")

            _log_request(method, path, status, elapsed, model)
            _send_response(conn, status, resp_body, ctype=upstream_resp.headers.get("Content-Type", "application/json"))

        except urllib.error.HTTPError as e:
            elapsed = int((time.time() - t0) * 1000)
            err_body = e.read()
            if DEBUG:
                _dbg(f"UPSTREAM→PROXY error={e.code} headers={json.dumps(dict(e.headers))}")
                _dbg(f"UPSTREAM→PROXY body={err_body[:2000].decode('utf-8', errors='replace')}")
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
    """Read and parse HTTP/1.1 request from socket. Returns (method, path, headers, body)."""
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

        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        # Read body
        body = ""
        content_len = int(headers.get("content-length", 0))
        if content_len > 0:
            body = rest.decode("utf-8", errors="replace")
            while len(body.encode()) < content_len:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                body += chunk.decode("utf-8", errors="replace")

        return method, path, headers, body
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

    log.info("proxy listening on %s → %s", LISTEN_ADDR, TARGET_URL)

    try:
        while True:
            try:
                conn, addr = s.accept()
            except socket.timeout:
                continue
            _handle(conn, addr)
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        s.close()


if __name__ == "__main__":
    main()
