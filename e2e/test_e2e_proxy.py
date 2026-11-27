"""E2E: the local reverse proxy (`python -m claude_mux.proxy`) as a real process.

Client (urllib, like Claude Code) -> proxy process -> fake upstream provider.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

from e2e_support.fake_upstream import Reply, anthropic_message
from e2e_support.helpers import free_port, home, wait_for, wait_http
from e2e_support.proxy_process import TOKEN, Proxy

@pytest.fixture
def proxy(upstream):
    started = []

    def _start(**env):
        p = Proxy(upstream.url, **env)
        started.append(p)
        return p

    yield _start
    for p in started:
        p.stop()


def usage_entries() -> list[dict]:
    path = home() / ".claude-mux" / "usage.log"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


# --- startup ----------------------------------------------------------------

@pytest.mark.parametrize("missing", ["PROXY_TARGET_URL", "PROXY_AUTH_TOKEN"])
def test_refuses_to_start_without_required_env(missing):
    env = {**os.environ, "PROXY_TARGET_URL": "http://127.0.0.1:1", "PROXY_AUTH_TOKEN": "x",
           "LISTEN_ADDR": f"127.0.0.1:{free_port()}"}
    del env[missing]
    r = subprocess.run([sys.executable, "-m", "claude_mux.proxy"], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 1
    assert f"{missing} not set" in r.stderr


def test_health_and_unknown_paths(proxy, upstream):
    p = proxy()
    status, headers, body = p.request("GET", "/health")
    assert (status, json.loads(body)) == (200, {"status": "ok"})
    status, _, body = p.request("GET", "/not-an-api-path")
    assert (status, body) == (404, b"Not Found")
    assert upstream.requests == [], "health and non-/v1 paths must never reach the provider"


def test_shuts_down_cleanly_on_interrupt(proxy):
    p = proxy()
    p.proc.send_signal(signal.SIGINT)
    assert p.proc.wait(timeout=10) == 0


@pytest.mark.xfail(strict=True, reason=(
    "BUG: LOG_FILE is ignored. `python -m claude_mux.proxy` imports the claude_mux package "
    "first, whose config module calls logging.basicConfig(~/.claude-mux/claude-mux.log), so "
    "the proxy's own basicConfig(filename=LOG_FILE) is a no-op."))
def test_honours_log_file(proxy):
    log_file = home() / "proxy.log"
    p = proxy(LOG_FILE=str(log_file))
    p.stop()
    text = log_file.read_text() if log_file.exists() else ""
    assert "proxy listening on" in text and "shutting down" in text


# --- request forwarding -----------------------------------------------------

def test_forwards_message_and_replaces_client_credentials(proxy, upstream):
    """Claude Code sends its own credentials; only the proxy's token may reach the provider."""
    p = proxy()
    payload = {"model": "claude-sonnet-4-6", "max_tokens": 50,
               "messages": [{"role": "user", "content": "Grüße aus Århus — 你好 ✓"}]}
    status, headers, body = p.post_json("/v1/messages?beta=true", payload, headers={
        "Authorization": "Bearer client-oauth-token",
        "x-api-key": "client-api-key",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "claude-code-20250219",
        "Accept-Encoding": "gzip",
    })

    assert status == 200
    assert json.loads(body)["content"][0]["text"] == "Hello from fake upstream (claude-sonnet-4-6)"
    assert "Access-Control-Allow-Origin" not in headers, "no cross-origin (browser) access"

    [req] = upstream.received("POST", "/v1/messages")
    assert req.query == "beta=true"
    assert req.json() == payload, "non-ASCII body must arrive byte-for-byte"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert "x-api-key" not in req.headers
    assert req.headers["anthropic-version"] == "2023-06-01"
    assert req.headers["anthropic-beta"] == "claude-code-20250219"
    assert "gzip" not in req.headers.get("accept-encoding", ""), "proxy cannot decompress, must not ask for gzip"


def test_sets_json_content_type_when_client_omits_it(proxy, upstream):
    import http.client
    p = proxy()
    conn = http.client.HTTPConnection("127.0.0.1", p.port, timeout=30)
    conn.request("POST", "/v1/messages", body=json.dumps({"model": "m"}).encode())  # no Content-Type
    assert conn.getresponse().status == 200
    conn.close()
    assert upstream.received("POST", "/v1/messages")[0].headers["content-type"] == "application/json"


def test_large_request_body_is_forwarded_intact(proxy, upstream):
    """Bodies bigger than one socket read (long Claude Code conversations)."""
    p = proxy()
    big = "x" * 300_000
    status, _, _ = p.post_json("/v1/messages", {"model": "m", "messages": [{"role": "user", "content": big}]})
    assert status == 200
    assert upstream.received("POST", "/v1/messages")[0].json()["messages"][0]["content"] == big


def test_get_with_query_string(proxy, upstream):
    p = proxy()
    status, _, body = p.request("GET", "/v1/models?limit=2")
    assert status == 200
    assert [m["id"] for m in json.loads(body)["data"]][:1] == ["claude-haiku-4-5-20251001"]
    assert upstream.received("GET", "/v1/models")[0].query == "limit=2"


# --- observability: request log, usage log, rate limits ---------------------

def test_logs_each_request_as_json_and_records_usage(proxy, upstream):
    upstream.route("POST", "/v1/messages",
                   Reply(json=anthropic_message("claude-haiku-4-5-20251001", input_tokens=321, output_tokens=45)))
    p = proxy()
    p.post_json("/v1/messages", {"model": "claude-haiku-4-5-20251001"})

    [line] = wait_for(lambda: [l for l in p.log_lines() if l.get("msg") == "http request"], what="request log")
    assert line["method"] == "POST" and line["path"] == "/v1/messages"
    assert line["status"] == 200 and line["model"] == "claude-haiku-4-5-20251001"
    assert isinstance(line["elapsed_ms"], int)

    [entry] = usage_entries()
    assert (entry["in"], entry["out"], entry["model"]) == (321, 45, "claude-haiku-4-5-20251001")


def test_caches_rate_limit_headers(proxy, upstream):
    upstream.route("POST", "/v1/messages", Reply(json=anthropic_message("m"), headers={
        "anthropic-ratelimit-tokens-limit": "80000",
        "anthropic-ratelimit-tokens-remaining": "79000",
        "anthropic-ratelimit-requests-remaining": "49",
        "anthropic-ratelimit-tokens-reset": "2026-01-01T00:00:00Z",
    }))
    p = proxy()
    p.post_json("/v1/messages", {"model": "m"})
    rl = json.loads((home() / ".claude-mux" / "rate-limits.json").read_text())
    assert rl["tokens_limit"] == 80000 and rl["tokens_remaining"] == 79000
    assert rl["requests_remaining"] == 49
    assert rl["tokens_reset"] == "2026-01-01T00:00:00Z"
    assert isinstance(rl["ts"], int)


def test_no_usage_recorded_for_non_json_or_empty_usage(proxy, upstream):
    upstream.route("POST", "/v1/messages", Reply(body=b"plain text", headers={"Content-Type": "text/plain"}))
    p = proxy()
    status, headers, body = p.post_json("/v1/messages", {"model": "m"})
    assert (status, body) == (200, b"plain text")
    assert headers["Content-Type"] == "text/plain"
    assert usage_entries() == []


# --- streaming --------------------------------------------------------------

SSE = [
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "model": "claude-sonnet-4-6",
        "usage": {"input_tokens": 100, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 5}}}),
    ("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}),
    ("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}),
    ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 42}}),
    ("message_stop", {"type": "message_stop"}),
]


def test_streams_server_sent_events_and_counts_cache_tokens(proxy, upstream):
    upstream.route("POST", "/v1/messages", Reply(sse=SSE))
    p = proxy()
    status, headers, body = p.post_json("/v1/messages", {"model": "claude-sonnet-4-6", "stream": True})

    assert status == 200
    assert headers["Content-Type"] == "text/event-stream"
    assert headers["Transfer-Encoding"] == "chunked"
    events = [json.loads(l[6:]) for l in body.decode().splitlines() if l.startswith("data: ")]
    assert [e["type"] for e in events] == [name for name, _ in SSE]
    assert "".join(e["delta"]["text"] for e in events if e["type"] == "content_block_delta") == "Hello"

    [entry] = wait_for(usage_entries, what="usage entry")
    assert entry["model"] == "claude-sonnet-4-6"
    assert entry["in"] == 125, "input + cache creation + cache read tokens"
    assert entry["out"] == 42
    [line] = wait_for(lambda: p.log_lines(), what="request log")
    assert line["status"] == 200 and line["model"] == "claude-sonnet-4-6"


# --- errors -----------------------------------------------------------------

def test_passes_through_provider_errors(proxy, upstream):
    err = {"type": "error", "error": {"type": "rate_limit_error", "message": "Too many requests"}}
    upstream.route("POST", "/v1/messages", Reply(429, json=err))
    p = proxy()
    status, _, body = p.post_json("/v1/messages", {"model": "m"})
    assert status == 429
    assert json.loads(body) == err
    assert wait_for(p.log_lines, what="request log")[0]["status"] == 429


def test_unreachable_provider_returns_502(upstream):
    p = Proxy(f"http://127.0.0.1:{free_port()}")
    try:
        status, _, body = p.post_json("/v1/messages", {"model": "m"})
        assert status == 502
        assert "refused" in json.loads(body)["error"]["message"].lower()
        assert p.log_lines()[0]["status"] == 502
    finally:
        p.stop()


def test_debug_mode_logs_traffic_but_redacts_the_token(proxy, upstream):
    p = proxy(PROXY_DEBUG="1")
    p.post_json("/v1/messages", {"model": "m", "messages": []},
                headers={"Authorization": "Bearer client-secret"})
    upstream.route("POST", "/v1/messages", Reply(500, json={"error": {"message": "boom"}}))
    p.post_json("/v1/messages", {"model": "m"})
    out = wait_for(lambda: (t := p.stdout_path.read_text()).count("UPSTREAM->PROXY error=500") and t,
                   what="debug output")
    assert "CLIENT->PROXY body=" in out and "PROXY->UPSTREAM url=" in out
    assert "Bearer [REDACTED]" in out
    assert TOKEN not in out and "client-secret" not in out
