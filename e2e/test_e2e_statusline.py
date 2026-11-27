"""E2E: `claude-mux init` installs the Claude Code status line; `statusline` renders it.

The status line is exercised the way Claude Code runs it: the command stored in
settings.json is executed with a JSON payload on stdin.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess

import pytest

from e2e_support.helpers import home, settings, write_settings


def run_statusline_command(payload) -> subprocess.CompletedProcess:
    """Execute settings.json's statusLine command exactly like Claude Code does."""
    cmd = settings()["statusLine"]["command"]
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(cmd, shell=True, input=data, capture_output=True, text=True, timeout=60,
                          env=os.environ.copy())


CLAUDE_CODE_PAYLOAD = {
    "session_id": "abc",
    "model": {"id": "claude-sonnet-4-6", "display_name": "Sonnet 4.6"},
    "context_window": {"used_percentage": 42},
    "rate_limits": None,
    "cost": {"total_cost_usd": 0.12},
}


# --- init ---------------------------------------------------------------------

def test_init_installs_status_line_and_keeps_other_settings(cli):
    write_settings({"theme": "dark", "env": {"FOO": "1"}})
    r = cli("init")
    assert r.code == 0, r.output
    script = home() / ".claude-mux" / "bin" / "statusline.sh"
    assert f"✓ Installed {script}" in r.stdout
    assert "claude-mux is ready" in r.stdout
    assert script.stat().st_mode & stat.S_IXUSR
    assert "exec claude-mux statusline" in script.read_text()

    s = settings()
    assert s["statusLine"] == {"type": "command", "command": f"sh {script}"}
    assert s["theme"] == "dark" and s["env"] == {"FOO": "1"}


def test_init_does_not_overwrite_custom_status_line_without_force(cli):
    write_settings({"statusLine": {"type": "command", "command": "my-own-statusline"}})
    r = cli("init")
    assert "statusLine already set" in r.stdout and "--force" in r.stdout
    assert settings()["statusLine"]["command"] == "my-own-statusline"

    cli("init", "--force")
    assert settings()["statusLine"]["command"].endswith("statusline.sh")


def test_init_fetches_models_for_every_subscription(cli, upstream):
    cli("add", "-n", "zai", "-u", upstream.url, "-K", "k", "--auth", "direct")
    cli("add", "-n", "deepseek", "-u", f"{upstream.url}/v1", "-k", "DS_KEY")  # proxy not running
    before = len(upstream.received("GET", "/v1/models"))
    r = cli("init")
    assert "  ✓ zai: 3 models" in r.stdout
    assert "  ✗ deepseek: fetch failed (proxy not running or no API key)" in r.stdout
    assert len(upstream.received("GET", "/v1/models")) == before + 1
    # the model list is cached for the other commands
    models = cli("models", "zai", "--json").json
    assert models["available_models"] == ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"]


# --- the installed status line, end to end ------------------------------------

def test_status_line_shows_active_provider_model_and_context(cli, upstream):
    cli("add", "-n", "zai", "-u", upstream.url, "-K", "k", "--auth", "direct")
    cli("activate", "zai")
    cli("init")
    r = run_statusline_command(CLAUDE_CODE_PAYLOAD)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "🔌 zai · sonnet-4-6 · ctx 42%"

    # switching provider is reflected immediately
    cli("add", "-n", "backup", "-u", f"{upstream.url}/b", "-K", "k", "--auth", "direct")
    cli("activate", "backup")
    assert run_statusline_command(CLAUDE_CODE_PAYLOAD).stdout.startswith("🔌 backup · ")


@pytest.mark.parametrize("payload, expected", [
    # legacy Claude Code format: model as a string, raw token counts
    ({"model": "claude-opus-4-6", "context_window": {"total_tokens": 200000, "used_tokens": 50000}},
     "🔌 zai · opus-4-6 · ctx 25%"),
    # rate-limit windows reported by Claude Code
    ({"model": {"id": "claude-haiku-4-5"}, "rate_limits": [
        {"window": "5h", "tokens_remaining": 750, "tokens_limit": 1000},
        {"window": "7d", "tokens_remaining": 0, "tokens_limit": 5000},
        {"window": "broken", "tokens_remaining": 1, "tokens_limit": 0}]},
     "🔌 zai · haiku-4-5 · 5h 25% · 7d 100%"),
    ("this is not json", "zai"),
    ("", "zai"),
])
def test_status_line_payload_variants(cli, upstream, payload, expected):
    cli("add", "-n", "zai", "-u", upstream.url, "-K", "k", "--auth", "direct")
    cli("activate", "zai")
    r = cli("statusline", input=payload if isinstance(payload, str) else json.dumps(payload))
    assert (r.code, r.stdout.strip()) == (0, expected)


def test_status_line_without_active_provider(cli):
    assert cli("statusline", input="").stdout == ""
    r = cli("statusline", input=json.dumps(CLAUDE_CODE_PAYLOAD))
    assert r.stdout.strip() == "sonnet-4-6 · ctx 42%"
