"""E2E: the local proxy must only serve Claude Code on this machine.

The proxy attaches the user's API key / OAuth token to every request it
forwards and ignores client credentials, so whoever can talk to it can spend
the user's subscription. It must therefore be unreachable from the network
and unusable from web pages open in the user's browser.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import psutil
import pytest

from e2e_support.helpers import home, instance_port, run_cli, wait_http
from e2e_support.proxy_process import Proxy

# The E2E sandbox routes non-local traffic to a dead proxy; these requests must go direct.
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def network_ip() -> str:
    """This host's address on its network (what another machine would connect to)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        ip = ""
    if not ip or ip.startswith("127."):
        pytest.skip("host has no non-loopback IPv4 address")
    return ip


def reachable(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex((host, port)) == 0


def listen_addresses(port: int) -> set[str]:
    return {c.laddr.ip for c in psutil.net_connections("tcp") if c.laddr.port == port and c.status == "LISTEN"}


def call(method, url, body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with _direct.open(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


MESSAGE = json.dumps({"model": "claude-opus-4-6", "max_tokens": 5,
                      "messages": [{"role": "user", "content": "hi"}]}).encode()


# --- network exposure (P0) ----------------------------------------------------

@pytest.mark.parametrize("auth, key_flag, key", [
    ("oauth_proxy", "-K", "sk-ant-oat01-SECRET"),   # claude-mux's own proxy (Claude Max via proxy)
    ("bearer", "-K", "sk-deepseek-SECRET"),          # heimsense (stand-in) for API-key providers
])
def test_started_proxy_is_not_reachable_from_the_network(upstream, auth, key_flag, key):
    ip = network_ip()
    url = upstream.url if auth == "oauth_proxy" else f"{upstream.url}/v1"
    run_cli("add", "-n", "prov", "-u", url, key_flag, key, "--auth", auth)
    assert run_cli("start", "prov").code == 0
    port = instance_port("prov")
    wait_http(f"http://127.0.0.1:{port}/health")

    env_file = (home() / ".claude-mux" / "instances" / "prov" / ".env").read_text().splitlines()
    assert f"LISTEN_ADDR=127.0.0.1:{port}" in env_file
    assert listen_addresses(port) == {"127.0.0.1"}
    assert not reachable(ip, port), f"proxy with the user's token is reachable at {ip}:{port}"
    assert upstream.received("POST", "/v1/messages") == []


def test_listen_address_without_host_means_loopback(upstream):
    """LISTEN_ADDR=":PORT" (the format older claude-mux versions wrote) must not expose the proxy."""
    ip = network_ip()
    p = Proxy(upstream.url, listen_host="")
    try:
        assert listen_addresses(p.port) == {"127.0.0.1"}
        assert not reachable(ip, p.port)
    finally:
        p.stop()


# --- browser access (P1) --------------------------------------------------------

@pytest.fixture
def proxy(upstream):
    p = Proxy(upstream.url)
    yield p
    p.stop()


def test_no_cross_origin_access_is_granted(proxy, upstream):
    for method, path, body in (("GET", "/v1/models", None), ("POST", "/v1/messages", MESSAGE),
                               ("GET", "/health", None), ("GET", "/nope", None)):
        status, headers, _ = call(method, proxy.url + path, body, {"Content-Type": "application/json"})
        assert "Access-Control-Allow-Origin" not in headers, (method, path, status)


def test_cors_preflight_is_refused_locally(proxy, upstream):
    status, headers, _ = call("OPTIONS", proxy.url + "/v1/messages", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert status in (403, 405)
    assert "Access-Control-Allow-Origin" not in headers
    assert upstream.requests == [], "preflight must not reach the provider"


@pytest.mark.parametrize("method, path, body, ctype", [
    ("POST", "/v1/messages", MESSAGE, "text/plain"),   # CORS "simple request": no preflight
    ("GET", "/v1/models", None, None),
])
def test_requests_from_web_pages_are_refused(proxy, upstream, method, path, body, ctype):
    headers = {"Origin": "https://evil.example"}
    if ctype:
        headers["Content-Type"] = ctype
    status, _, _ = call(method, proxy.url + path, body, headers)
    assert status == 403
    assert upstream.requests == [], "the user's token must not be used for a web page"


def test_dns_rebinding_host_is_refused(proxy, upstream):
    """A page on evil.example re-pointed to 127.0.0.1 sends Host: evil.example (same-origin, no Origin)."""
    status, _, _ = call("GET", proxy.url + "/v1/models", headers={"Host": f"evil.example:{proxy.port}"})
    assert status == 403
    assert upstream.requests == []


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_claude_code_style_requests_still_work(proxy, upstream, host):
    status, _, body = call("POST", proxy.url + "/v1/messages", MESSAGE, {
        "Host": f"{host}:{proxy.port}", "Content-Type": "application/json",
        "anthropic-version": "2023-06-01", "Authorization": "Bearer ignored"})
    assert status == 200, body
    assert upstream.received("POST", "/v1/messages")[0].headers["authorization"] == "Bearer sk-proxy-secret-token-123"
