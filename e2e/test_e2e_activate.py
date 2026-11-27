"""E2E: activating a provider rewrites Claude Code's settings.json (CLI as a real process)."""
from __future__ import annotations

import json
import stat

import pytest

from e2e_support.helpers import home, instance_port, settings, sub_named, write_settings

PRE_EXISTING = {
    "permissions": {"allow": ["Bash(ls:*)"]},
    "statusLine": {"type": "command", "command": "my-statusline"},
    "env": {
        "MY_OWN_VAR": "keep-me",
        "ANTHROPIC_CUSTOM_MODEL_OPTION": "deprecated",
        "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "deprecated",
        "CLAUDE_CODE_OAUTH_TOKEN": "stale-oauth-token",
    },
}


@pytest.fixture
def ds_key(monkeypatch):
    """The user has exported their DeepSeek key in the shell claude-mux runs in."""
    monkeypatch.setenv("DS_KEY", "sk-deepseek-123")
    return "sk-deepseek-123"


def add_deepseek(cli, upstream, *extra):
    r = cli("add", "-n", "deepseek", "-u", f"{upstream.url}/v1", "-k", "DS_KEY",
            "--haiku", "deepseek-chat", "--sonnet", "deepseek-chat", "--opus", "deepseek-reasoner", *extra)
    assert r.code == 0, r.output
    return r


def test_activate_proxy_provider_points_claude_code_at_the_local_proxy(cli, upstream, start_proxy, ds_key):
    write_settings(PRE_EXISTING)
    add_deepseek(cli, upstream)
    port = start_proxy("deepseek")

    r = cli("activate", "deepseek")
    assert (r.code, r.stdout.strip()) == (0, "Activated: deepseek")

    s = settings()
    env = s["env"]
    assert env["ANTHROPIC_BASE_URL"] == f"http://localhost:{port}"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-deepseek-123", "key is resolved from the env var named at add time"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "deepseek-chat"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "deepseek-chat"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "deepseek-reasoner"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["ANTHROPIC_DISABLE_TELEMETRY"] == "true"
    # the user's own settings survive, stale/deprecated keys are removed
    assert env["MY_OWN_VAR"] == "keep-me"
    assert s["permissions"] == PRE_EXISTING["permissions"]
    assert s["statusLine"] == PRE_EXISTING["statusLine"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_CUSTOM_MODEL_OPTION" not in env and "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME" not in env
    # settings.json holds a secret now: owner-only, and the previous version is kept as backup
    settings_path = home() / ".claude" / "settings.json"
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600
    assert json.loads(settings_path.with_suffix(".json.bak").read_text()) == PRE_EXISTING

    dot_env = home() / ".claude-mux" / ".env"
    assert stat.S_IMODE(dot_env.stat().st_mode) == 0o600
    lines = dot_env.read_text().splitlines()
    assert f"LISTEN_ADDR=127.0.0.1:{port}" in lines and "ANTHROPIC_API_KEY=sk-deepseek-123" in lines
    assert (home() / ".claude-mux" / "active-name").read_text() == "deepseek"

    # every read-side command now agrees on what is active
    assert cli("active").stdout.strip() == "deepseek"
    assert cli("active", "--json").json == {"active": "deepseek", "id": sub_named("deepseek")["id"]}
    listed = cli("list", "--json").json
    assert [(x["name"], x["active"], x["port"]) for x in listed] == [("deepseek", True, port)]
    assert "Active: deepseek" in cli("list").stdout
    assert cli("status").stdout.strip() == f"deepseek: online port={port} [active]"
    assert cli("config", "--json").json["active"] == "deepseek"


def test_activate_oauth_subscription_removes_proxy_settings(cli, upstream, start_proxy, ds_key):
    add_deepseek(cli, upstream)
    start_proxy("deepseek")
    cli("activate", "deepseek")
    r = cli("add", "-n", "claude-max", "-u", "https://api.anthropic.com", "-K", "sk-ant-oat01-secret", "--auth", "oauth")
    assert r.code == 0, r.output

    r = cli("activate", "claude-max", "--json")
    assert r.json == {"ok": True, "name": "claude-max", "id": sub_named("claude-max")["id"]}
    env = settings()["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-secret"
    assert "ANTHROPIC_BASE_URL" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert cli("active").stdout.strip() == "claude-max"
    assert cli("status", "claude-max").stdout.strip() == "claude-max: oauth [active]"

    # and back again: the OAuth token must not linger and override the proxy
    cli("activate", "deepseek")
    env = settings()["env"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["ANTHROPIC_BASE_URL"] == f"http://localhost:{instance_port('deepseek')}"
    assert cli("active").stdout.strip() == "deepseek"


def test_activate_direct_provider_uses_provider_url_without_proxy(cli, upstream):
    r = cli("add", "-n", "zai", "-u", upstream.url, "-K", "zai-key", "--auth", "direct", "--sonnet", "glm-4.7")
    assert r.stdout.strip() == "Added: zai (direct) (3 models fetched)"
    [models_req] = upstream.received("GET", "/v1/models")
    assert models_req.headers["authorization"] == "Bearer zai-key"

    assert cli("activate", "zai", "-q").stdout == ""
    env = settings()["env"]
    assert env["ANTHROPIC_BASE_URL"] == upstream.url
    assert env["ANTHROPIC_AUTH_TOKEN"] == "zai-key"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "glm-4.7"
    assert cli("active").stdout.strip() == "zai"
    assert cli("start", "zai").code == 1, "direct providers have no proxy to start"


def test_force_model_is_applied_on_next_activation(cli, upstream, start_proxy, ds_key):
    add_deepseek(cli, upstream)
    start_proxy("deepseek")
    r = cli("force-model", "deepseek", "deepseek-v4", "--tier", "opus")
    assert r.stdout.strip() == "Set deepseek opus → deepseek-v4"
    cli("activate", "deepseek")
    assert settings()["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "deepseek-v4"
    assert cli("force-model", "deepseek", "--reset", "--tier", "opus", "--json").json["action"] == "reset"
    assert "opus" not in sub_named("deepseek")["model_maps"]


def test_switching_provider_does_not_leak_previous_model_names(cli, upstream, start_proxy, ds_key):
    add_deepseek(cli, upstream)
    start_proxy("deepseek")
    cli("activate", "deepseek")
    cli("add", "-n", "anthropic", "-u", upstream.url, "-K", "k2", "--auth", "direct")
    cli("activate", "anthropic")
    env = settings()["env"]
    assert not any(k.startswith("ANTHROPIC_DEFAULT_") for k in env), "no mapping → Claude Code defaults"


def test_switch_to_claude_max_leaves_nothing_of_the_previous_provider(cli, upstream, start_proxy, ds_key):
    """Everything provider-specific is replaced; only the user's own settings survive
    (including their own ANTHROPIC_API_KEY, which claude-mux never writes)."""
    write_settings({
        "model": "deepseek-chat",                      # pinned with /model while on DeepSeek
        "permissions": {"allow": ["Bash(ls:*)"]},
        "env": {
            "MY_OWN_VAR": "keep-me",
            "ANTHROPIC_API_KEY": "sk-user-own-key",    # the user's own: never touched
            "ANTHROPIC_MODEL": "deepseek-chat",        # the user's own pins: never touched
            "ANTHROPIC_SMALL_FAST_MODEL": "deepseek-chat",
        },
    })
    add_deepseek(cli, upstream)
    start_proxy("deepseek")
    cli("activate", "deepseek")
    cli("add", "-n", "claude-max", "-u", "https://api.anthropic.com", "-K", "sk-ant-oat01-max", "--auth", "oauth")

    assert cli("activate", "claude-max").code == 0
    s = settings()
    assert s["env"] == {
        "MY_OWN_VAR": "keep-me",
        "ANTHROPIC_API_KEY": "sk-user-own-key",
        "ANTHROPIC_MODEL": "deepseek-chat",
        "ANTHROPIC_SMALL_FAST_MODEL": "deepseek-chat",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-max",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ANTHROPIC_DISABLE_TELEMETRY": "true",
    }
    assert "model" not in s, "a DeepSeek model id must not stay pinned on Claude Max"
    assert s["permissions"] == {"allow": ["Bash(ls:*)"]}
    dot_env = (home() / ".claude-mux" / ".env").read_text()
    assert "sk-deepseek" not in dot_env and "ANTHROPIC_API_KEY=sk-ant-oat01-max" in dot_env
    assert cli("active").stdout.strip() == "claude-max"


@pytest.mark.parametrize("pinned, kept", [
    ("opus", True),                      # alias: resolved per provider, always valid
    ("deepseek-reasoner", True),         # offered by the new provider's model map
    ("claude-opus-4-6", True),           # the user's own pin: the previous provider did not serve it
])
def test_pinned_model_is_kept_unless_only_the_previous_provider_served_it(cli, upstream, start_proxy, ds_key, pinned, kept):
    write_settings({"model": pinned})
    add_deepseek(cli, upstream)
    start_proxy("deepseek")
    cli("activate", "deepseek")
    assert ("model" in settings()) is kept


def test_activating_a_stopped_proxy_provider_is_refused(cli, upstream, ds_key):
    """Claude Code must never be pointed at http://localhost:0."""
    cli("add", "-n", "zai", "-u", upstream.url, "-K", "k", "--auth", "direct")
    cli("activate", "zai")
    before = settings()
    add_deepseek(cli, upstream)

    r = cli("activate", "deepseek")
    assert r.code == 1
    assert "'deepseek' is served through a local proxy that is not running" in r.stderr
    assert "claude-mux start deepseek" in r.stderr
    assert settings() == before, "settings.json is left untouched"
    assert cli("active").stdout.strip() == "zai"
    assert "localhost:0" not in (home() / ".claude").joinpath("settings.json").read_text()

    # after `stop`, the same applies again
    cli("start", "deepseek")
    assert cli("activate", "deepseek").code == 0
    cli("stop", "deepseek")
    assert cli("activate", "deepseek").code == 1


def test_failover_skips_providers_whose_proxy_is_stopped(cli, upstream, ds_key):
    from e2e_support.fake_upstream import FakeUpstream, Reply
    backup = FakeUpstream()
    try:
        cli("add", "-n", "a-primary", "-u", f"{upstream.url}/p", "-K", "k", "--auth", "direct")
        add_deepseek(cli, upstream)                       # proxy provider, never started
        cli("add", "-n", "z-backup", "-u", backup.url, "-K", "backup-key", "--auth", "direct")
        cli("activate", "a-primary")
        upstream.route("GET", "/p/v1/models", Reply(429, json={"error": {"message": "limit"}}))

        r = cli("failover")
        assert r.stdout.strip().startswith("Failover: switched to z-backup"), r.output
        env = settings()["env"]
        assert env["ANTHROPIC_BASE_URL"] == backup.url
    finally:
        backup.close()


def test_concurrent_cli_writers_do_not_lose_subscriptions(upstream):
    """Several claude-mux processes changing the config at once (e.g. scripts, TUI + CLI)."""
    import os
    import subprocess
    import sys
    procs = [subprocess.Popen([sys.executable, "-m", "claude_mux", "add", "-n", f"p{i}", "-u",
                               f"{upstream.url}/{i}", "-K", "k", "--auth", "direct"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ.copy())
             for i in range(8)]
    assert [p.wait(timeout=60) for p in procs] == [0] * 8
    names = sorted(s["name"] for s in json.loads((home() / ".claude-mux" / "subscriptions.json").read_text())["subscriptions"])
    assert names == [f"p{i}" for i in range(8)]


def test_name_resolution_and_not_found(cli, upstream):
    add_deepseek(cli, upstream)
    cli("add", "-n", "deepinfra", "-u", f"{upstream.url}/deepinfra", "-K", "k", "--auth", "direct")
    cli("add", "-n", "openai", "-u", f"{upstream.url}/openai", "-K", "k", "--auth", "direct")

    assert cli("activate", "OPENAI").stdout.strip() == "Activated: openai", "case-insensitive"
    assert cli("activate", "open").stdout.strip() == "Activated: openai", "unique prefix"
    r = cli("activate", "deep")
    assert r.code == 3 and "subscription 'deep' not found" in r.stderr, "ambiguous prefix is refused"
    r = cli("activate", "nope")
    assert r.code == 3 and "not found" in r.stderr
    assert cli("active").stdout.strip() == "openai"


@pytest.mark.xfail(strict=True, reason=(
    "BUG: active-provider detection for direct providers compares only ANTHROPIC_BASE_URL, so "
    "with two accounts on the same provider (e.g. two z.ai keys) the first one is always reported "
    "as active, whichever was activated. The token in settings.json is ignored."))
def test_two_accounts_on_the_same_direct_provider(cli, upstream):
    cli("add", "-n", "zai-work", "-u", upstream.url, "-K", "work-key", "--auth", "direct")
    cli("add", "-n", "zai-home", "-u", upstream.url, "-K", "home-key", "--auth", "direct")
    cli("activate", "zai-home")
    assert settings()["env"]["ANTHROPIC_AUTH_TOKEN"] == "home-key"
    assert cli("active").stdout.strip() == "zai-home"


def test_nothing_active(cli, upstream):
    assert cli("active").code == 1
    r = cli("active", "--json")
    assert (r.code, r.json) == (1, {"active": None})
    cfg = cli("config", "--json").json
    assert cfg["active"] == "none"
    assert cfg["config_dir"] == str(home() / ".claude-mux")
    assert cfg["claude_settings"] == str(home() / ".claude" / "settings.json")
    assert "Active sub:    none (-)" in cli("config").stdout


def test_corrupt_settings_file_is_replaced_not_fatal(cli, upstream):
    path = home() / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json")
    cli("add", "-n", "zai", "-u", upstream.url, "-K", "k", "--auth", "direct")
    assert cli("activate", "zai").code == 0
    assert settings()["env"]["ANTHROPIC_BASE_URL"] == upstream.url
    assert path.with_suffix(".json.bak").read_text() == "{ this is not json", "original kept as backup"


def test_oauth_activation_keeps_a_model_the_user_pinned(cli, upstream, start_proxy, ds_key):
    """A pinned Anthropic model survives switching profiles; the old provider's models do not."""
    write_settings({"model": "claude-opus-5-5", "env": {"ANTHROPIC_API_KEY": "sk-user-own"}})
    add_deepseek(cli, upstream)
    start_proxy("deepseek")
    cli("add", "-n", "claude-max", "-u", "https://api.anthropic.com", "-K", "sk-ant-oat01-secret", "--auth", "oauth")

    assert cli("activate", "deepseek").code == 0
    assert cli("activate", "claude-max").code == 0
    s = settings()
    assert s["model"] == "claude-opus-5-5"
    assert s["env"]["ANTHROPIC_API_KEY"] == "sk-user-own"
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in s["env"]  # the DeepSeek tier mapping is gone
