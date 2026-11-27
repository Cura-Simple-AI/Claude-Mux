"""Run claude-mux's proxy (`python -m claude_mux.proxy`) as a real process."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import urllib.error
import urllib.request

from e2e_support.helpers import free_port, home, wait_http

TOKEN = "sk-proxy-secret-token-123"


class Proxy:
    def __init__(self, target: str, token: str = TOKEN, listen_host: str = "127.0.0.1", **extra_env):
        self.port = free_port()
        self.stdout_path = home() / f"proxy-{self.port}.out"
        self.stderr_path = home() / f"proxy-{self.port}.err"
        env = {**os.environ, "PROXY_TARGET_URL": target, "PROXY_AUTH_TOKEN": token,
               "LISTEN_ADDR": f"{listen_host}:{self.port}", **extra_env}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "claude_mux.proxy"], env=env,
            stdout=open(self.stdout_path, "w"), stderr=open(self.stderr_path, "w"))
        wait_http(f"{self.url}/health")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        req = urllib.request.Request(self.url + path, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def post_json(self, path: str, payload: dict, headers: dict | None = None):
        return self.request("POST", path, json.dumps(payload).encode(),
                            {"Content-Type": "application/json", **(headers or {})})

    def log_lines(self) -> list[dict]:
        out = []
        for line in self.stdout_path.read_text().splitlines():
            if line.startswith("{"):
                out.append(json.loads(line))
        return out

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            self.proc.wait(timeout=10)
