"""The proxy must serve this machine only: loopback bind, no CORS, no Origin/Host from elsewhere."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).parent.parent
PROC_NET_TCP = Path("/proc/net/tcp")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _non_loopback_ip():
    """An address of this machine that is not loopback, or None (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


def _listen_address_hex(port: int):
    """Local address (hex, as in /proc/net/tcp) of the LISTEN socket on `port`."""
    for line in PROC_NET_TCP.read_text().splitlines()[1:]:
        fields = line.split()
        addr, _, p = fields[1].partition(":")
        if fields[3] == "0A" and int(p, 16) == port:
            return addr
    return None


@pytest.fixture()
def proxy(tmp_path):
    """Start the real proxy in a subprocess with an isolated HOME; yields a start(listen_addr) fn."""
    procs = []

    def start(listen_addr: str) -> int:
        port = int(listen_addr.rpartition(":")[2])
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "LISTEN_ADDR": listen_addr,
            # Nothing listens here; the tests never reach upstream.
            "PROXY_TARGET_URL": "http://127.0.0.1:9/v1",
            "PROXY_AUTH_TOKEN": "test-token-not-real",
            "LOG_FILE": str(tmp_path / "proxy.log"),
            "PYTHONPATH": str(PKG_ROOT),
        }
        p = subprocess.Popen([sys.executable, "-m", "claude_mux.proxy"], env=env, cwd=tmp_path,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert p.poll() is None, "proxy exited during start-up"
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                return port
            except OSError:
                continue
        raise AssertionError("proxy did not start listening")

    yield start
    for p in procs:
        p.terminate()
        p.wait(timeout=10)


def _raw_request(port: int, method: str = "GET", path: str = "/v1/messages",
                 headers: dict | None = None) -> tuple[int, dict, str]:
    hdrs = {"Host": f"127.0.0.1:{port}", "Connection": "close", "Content-Length": "0"}
    hdrs.update(headers or {})
    req = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(req.encode())
        data = b""
        while chunk := s.recv(4096):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    resp_headers = {k.lower(): v for k, v in (ln.split(": ", 1) for ln in lines[1:])}
    return int(lines[0].split()[1]), resp_headers, body.decode()


@pytest.mark.skipif(not PROC_NET_TCP.exists(), reason="needs /proc/net/tcp (Linux)")
class TestListensOnLoopbackOnly:
    @pytest.mark.parametrize("make_addr", [
        pytest.param(lambda port: f"127.0.0.1:{port}", id="explicit-loopback"),
        pytest.param(lambda port: f":{port}", id="empty-host"),
    ])
    def test_bound_to_127_0_0_1(self, proxy, make_addr):
        port = _free_port()
        proxy(make_addr(port))
        assert _listen_address_hex(port) == "0100007F"  # 127.0.0.1, not 00000000 (0.0.0.0)

    def test_not_reachable_on_other_interface(self, proxy):
        ip = _non_loopback_ip()
        if ip is None:
            pytest.skip("no non-loopback address on this machine")
        port = _free_port()
        proxy(f":{port}")
        with pytest.raises(OSError):
            socket.create_connection((ip, port), timeout=2).close()

    def test_default_listen_addr_is_loopback(self):
        env = {k: v for k, v in os.environ.items() if k != "LISTEN_ADDR"}
        out = subprocess.run(
            [sys.executable, "-I", "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); from claude_mux import proxy; print(proxy.LISTEN_ADDR)",
             str(PKG_ROOT)],
            env=env, capture_output=True, text=True, check=True)
        assert out.stdout.strip().startswith("127.0.0.1:")


class TestRefusesWebPageRequests:
    def test_health_works_for_local_client(self, proxy):
        port = proxy(f"127.0.0.1:{_free_port()}")
        status, _, body = _raw_request(port, path="/health")
        assert status == 200 and json.loads(body) == {"status": "ok"}

    @pytest.mark.parametrize("host", ["localhost", "localhost:1234", "127.0.0.1", "[::1]:18080"])
    def test_local_host_headers_pass(self, proxy, host):
        port = proxy(f"127.0.0.1:{_free_port()}")
        status, _, _ = _raw_request(port, path="/health", headers={"Host": host})
        assert status == 200

    @pytest.mark.parametrize("path", ["/health", "/v1/messages"])
    def test_foreign_origin_refused(self, proxy, path):
        port = proxy(f"127.0.0.1:{_free_port()}")
        status, _, body = _raw_request(port, path=path, headers={"Origin": "https://evil.example"})
        assert status == 403
        assert "Origin" in json.loads(body)["error"]["message"]

    @pytest.mark.parametrize("host", ["evil.example", "evil.example:18080", "192.168.1.5:18080",
                                      "localhost.evil.example", "[2001:db8::1]:18080"])
    def test_foreign_host_refused(self, proxy, host):
        port = proxy(f"127.0.0.1:{_free_port()}")
        status, _, body = _raw_request(port, path="/v1/messages", headers={"Host": host})
        assert status == 403
        assert "Host" in json.loads(body)["error"]["message"]

    def test_options_preflight_refused(self, proxy):
        port = proxy(f"127.0.0.1:{_free_port()}")
        status, _, _ = _raw_request(port, method="OPTIONS", headers={
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-api-key"})
        assert status == 405

    def test_no_cors_header_on_any_response(self, proxy):
        port = proxy(f"127.0.0.1:{_free_port()}")
        for kwargs in ({"path": "/health"}, {"path": "/nope"}, {"method": "OPTIONS"},
                       {"headers": {"Origin": "https://evil.example"}}):
            _, headers, _ = _raw_request(port, **kwargs)
            assert not any(k.startswith("access-control-") for k in headers), kwargs
