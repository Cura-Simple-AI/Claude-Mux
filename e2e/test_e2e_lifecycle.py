"""E2E: `start` only reports success for a proxy that actually serves, and a proxy that
is not listening is never activated (Claude Code would be pointed at a dead port)."""
from __future__ import annotations

import os
import signal
import socket

import pytest

from e2e_support import stubs
from e2e_support.helpers import home, instance_port, run_cli, settings, tui_test, until, wait_for


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture
def broken_provider(upstream):
    """A proxy provider whose key variable is not exported: the proxy exits at once."""
    assert "UNSET_KEY" not in os.environ
    assert run_cli("add", "-n", "deepseek", "-u", f"{upstream.url}/v1", "-k", "UNSET_KEY").code == 0


def test_start_reports_a_proxy_that_dies_immediately(broken_provider):
    r = run_cli("start", "deepseek")
    assert r.code == 1
    assert "did not start" in r.stderr
    assert "PROXY_AUTH_TOKEN not set" in r.stderr, "the proxy's own error is shown"
    assert "claude-mux-deepseek" not in stubs.pm2_state(home()), "no crashed process left in pm2"
    assert instance_port("deepseek") is None
    assert run_cli("status", "deepseek").stdout.strip() == "deepseek: stopped"
    assert run_cli("activate", "deepseek").code == 1


def test_start_returns_once_the_proxy_accepts_connections(upstream, monkeypatch):
    monkeypatch.setenv("DS_KEY", "k")
    run_cli("add", "-n", "deepseek", "-u", f"{upstream.url}/v1", "-k", "DS_KEY")
    r = run_cli("start", "deepseek")
    port = instance_port("deepseek")
    assert (r.code, r.stdout.strip()) == (0, f"Started deepseek on port {port}")
    assert listening(port), "no waiting needed after `start` returns"


def test_proxy_that_crashed_later_is_not_activated(upstream, monkeypatch):
    monkeypatch.setenv("DS_KEY", "k")
    run_cli("add", "-n", "deepseek", "-u", f"{upstream.url}/v1", "-k", "DS_KEY")
    run_cli("add", "-n", "zai", "-u", upstream.url, "-K", "k", "--auth", "direct")
    run_cli("activate", "zai")
    assert run_cli("start", "deepseek").code == 0
    port = instance_port("deepseek")
    os.kill(stubs.pm2_state(home())["claude-mux-deepseek"]["pid"], signal.SIGKILL)
    wait_for(lambda: not listening(port), what="proxy gone")

    r = run_cli("activate", "deepseek")
    assert r.code == 1
    assert f"nothing is listening on port {port}" in r.stderr
    assert settings()["env"]["ANTHROPIC_BASE_URL"] == upstream.url, "settings.json unchanged"
    assert run_cli("active").stdout.strip() == "zai"


@tui_test
async def test_tui_start_shows_why_the_proxy_did_not_start(broken_provider):
    from claude_mux.config import ConfigManager
    from test_e2e_tui import RecordingApp, detail
    app = RecordingApp(ConfigManager())
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.2)
        await pilot.press("s")
        await until(pilot, lambda: any("did not start" in n for n in app.notices), timeout=20, what="error")
        assert any("PROXY_AUTH_TOKEN not set" in n for n in app.notices)
        assert not any("started on port" in n for n in app.notices)
        assert "Status: stopped" in detail(app)
        await pilot.pause(0.2)
