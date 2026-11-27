"""Helpers shared by the E2E tests (run the CLI, read state files, wait for things)."""
from __future__ import annotations

import asyncio
import functools
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path


def home() -> Path:
    return Path(os.environ["HOME"])


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str

    @property
    def json(self):
        return json.loads(self.stdout)

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def run_cli(*args: str, input: str | None = None, env: dict | None = None, timeout: float = 60) -> Result:
    """Run `claude-mux ARGS...` in a fresh process, like a user in a shell."""
    proc = subprocess.run(
        [sys.executable, "-m", "claude_mux", *args],
        input=input, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    return Result(proc.returncode, proc.stdout, proc.stderr)


def settings() -> dict:
    """Claude Code's ~/.claude/settings.json."""
    return json.loads((home() / ".claude" / "settings.json").read_text())


def write_settings(data: dict) -> None:
    p = home() / ".claude" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))


def config() -> dict:
    return json.loads((home() / ".claude-mux" / "subscriptions.json").read_text())


def sub_named(name: str) -> dict:
    return next(s for s in config()["subscriptions"] if s["name"] == name)


def instance_port(name: str) -> int | None:
    return config()["instances"].get(sub_named(name)["id"], {}).get("port")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except Exception:
            if time.time() > deadline:
                raise TimeoutError(f"{url} did not come up")
            time.sleep(0.05)


def wait_for(predicate, timeout: float = 10, interval: float = 0.05, what: str = "condition"):
    deadline = time.time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.time() > deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        time.sleep(interval)


async def until(pilot, predicate, timeout: float = 10, what: str = "condition"):
    """Let the TUI run (timers, workers, threads) until predicate() holds."""
    deadline = time.time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.time() > deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        await pilot.pause(0.05)


def tui_test(fn):
    """Run an `async def` test body (Textual Pilot) without extra pytest plugins."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapper
