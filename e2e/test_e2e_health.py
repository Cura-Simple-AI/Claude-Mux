"""E2E: inference tests (`test`), proxy probe (`probe`) and failover (`failover`).

Requests really travel CLI -> (local proxy process) -> fake provider.
"""
from __future__ import annotations

import pytest

from e2e_support.fake_upstream import FakeUpstream, Reply
from e2e_support.helpers import home, settings


@pytest.fixture
def upstream2():
    srv = FakeUpstream()
    yield srv
    srv.close()


@pytest.fixture
def deepseek(cli, upstream, start_proxy, monkeypatch):
    """A bearer provider served through a running local proxy."""
    monkeypatch.setenv("DS_KEY", "sk-deepseek-123")
    assert cli("add", "-n", "deepseek", "-u", f"{upstream.url}/v1", "-k", "DS_KEY",
               "--haiku", "deepseek-chat", "--sonnet", "deepseek-chat", "--opus", "deepseek-reasoner").code == 0
    return start_proxy("deepseek")


def add_direct(cli, name, url, key="direct-key", *extra):
    r = cli("add", "-n", name, "-u", url, "-K", key, "--auth", "direct", *extra)
    assert r.code == 0, r.output


# --- claude-mux test ----------------------------------------------------------

def test_test_all_tiers_on_direct_provider(cli, upstream):
    add_direct(cli, "zai", upstream.url, "zai-key", "--haiku", "glm-4.5-air")
    r = cli("test", "zai")
    assert r.code == 0, r.output
    lines = r.stdout.splitlines()
    # haiku comes from the model map; sonnet/opus from the model list fetched at `add`
    assert lines[0].startswith("zai [haiku] glm-4.5-air: OK (200, ")
    assert lines[1] == "  → Hello from fake upstream (glm-4.5-air)"
    assert lines[2].startswith("zai [sonnet] claude-sonnet-4-6: OK (200, ")
    assert lines[4].startswith("zai [opus] claude-opus-4-6: OK (200, ")

    sent = upstream.received("POST", "/v1/messages")
    assert [r.json()["model"] for r in sent] == ["glm-4.5-air", "claude-sonnet-4-6", "claude-opus-4-6"]
    assert all(r.headers["authorization"] == "Bearer zai-key" for r in sent)
    assert sent[0].json()["max_tokens"] == 100 and sent[0].json()["stream"] is False


def test_partial_failure_is_reported_per_tier(cli, upstream):
    add_direct(cli, "zai", upstream.url)
    upstream.fail_models("claude-opus-4-6", status=429, message="Opus quota exhausted")
    r = cli("test", "zai", "--json")
    assert r.code == 4
    by_tier = {x["tier"]: x for x in r.json["results"]}
    assert by_tier["haiku"]["ok"] and by_tier["sonnet"]["ok"]
    assert (by_tier["opus"]["ok"], by_tier["opus"]["code"]) == (False, 429)
    assert "Opus quota exhausted" in by_tier["opus"]["body"]

    r = cli("test", "zai", "opus")
    assert r.code == 4
    assert "zai [opus] claude-opus-4-6: FAIL (429, " in r.stdout and "Opus quota exhausted" in r.stdout


def test_single_tier_and_model_override(cli, upstream):
    add_direct(cli, "openai", upstream.url)
    assert cli("test", "openai", "sonnet").stdout.startswith("openai [sonnet] claude-sonnet-4-6: OK")
    r = cli("test", "openai", "--model", "gpt-5-mini")
    assert r.stdout.startswith("openai [custom] gpt-5-mini: OK")
    payload = upstream.received("POST", "/v1/messages")[-1].json()
    assert payload["max_completion_tokens"] == 100 and "max_tokens" not in payload, \
        "GPT-5 models reject max_tokens"


@pytest.mark.xfail(strict=True, reason=(
    "BUG: `claude-mux blacklist --help` promises blacklisted models are never used by `test`, "
    "but when the blacklist removes a tier's only candidate, `test` falls back to the built-in "
    "default for that tier, which here is the blacklisted model itself."))
def test_blacklisted_model_is_never_tested(cli, upstream):
    add_direct(cli, "zai", upstream.url)
    assert cli("blacklist", "zai", "claude-opus-4-6").stdout.strip() == "Blacklisted: claude-opus-4-6 on zai"
    cli("test", "zai")
    assert "claude-opus-4-6" not in [r.json()["model"] for r in upstream.received("POST", "/v1/messages")]


def test_test_through_running_proxy_uses_the_proxy_token(cli, upstream, deepseek):
    cli("activate", "deepseek")
    r = cli("test")  # no name: the active subscription
    assert r.code == 0, r.output
    assert r.stdout.splitlines()[0].startswith("deepseek [haiku] deepseek-chat: OK (200")
    sent = upstream.received("POST", "/v1/messages")
    assert [x.json()["model"] for x in sent] == ["deepseek-chat", "deepseek-chat", "deepseek-reasoner"]
    assert all(x.headers["authorization"] == "Bearer sk-deepseek-123" for x in sent)
    assert all("x-api-key" not in x.headers for x in sent)


def test_test_reports_stopped_proxy_and_missing_subscription(cli, upstream, monkeypatch):
    monkeypatch.setenv("DS_KEY", "k")
    cli("add", "-n", "deepseek", "-u", f"{upstream.url}/v1", "-k", "DS_KEY")
    r = cli("test", "deepseek", "haiku")
    assert r.code == 4
    assert "FAIL (0, " in r.stdout and "Proxy not running" in r.stdout
    assert cli("test", "ghost").code == 3
    r = cli("test")
    assert (r.code, r.stderr.strip()) == (3, "Error: no active subscription")


# --- claude-mux probe ---------------------------------------------------------

def test_probe_sends_real_request_through_proxy(cli, upstream, deepseek):
    r = cli("probe", "deepseek")
    assert r.code == 0, r.output
    head, _, body = r.stdout.partition("\n\n")
    assert head.startswith("deepseek: OK (200, ")
    assert body.strip() == "Hello from fake upstream (deepseek-chat)"
    [req] = upstream.received("POST", "/v1/messages")
    assert req.headers["authorization"] == "Bearer sk-deepseek-123"

    r = cli("probe", "deepseek", "--model", "deepseek-reasoner", "--json")
    assert r.json["ok"] is True and r.json["body"] == "Hello from fake upstream (deepseek-reasoner)"


def test_probe_failures(cli, upstream, deepseek):
    upstream.route("POST", "/v1/messages", Reply(500, json={"error": {"message": "upstream exploded"}}))
    cli("activate", "deepseek")
    r = cli("probe")
    assert r.code == 4
    assert r.stdout.startswith("deepseek: FAIL (500, ") and "upstream exploded" in r.stdout

    assert cli("stop", "deepseek").stdout.strip() == "Stopped deepseek"
    r = cli("probe", "deepseek")
    assert (r.code, r.stderr.strip()) == (3, "Error: deepseek is not running (no port assigned)")
    assert cli("probe", "ghost").code == 3


@pytest.mark.parametrize("args, message", [
    (["--all"], "--all requires --quota"),
    (["--quota", "--model", "x"], "--model cannot be combined with --quota"),
    (["x", "--quota", "--all"], "NAME cannot be combined with --all"),
])
def test_probe_usage_errors(cli, args, message):
    r = cli("probe", *args)
    assert r.code == 2 and message in r.stderr


# --- claude-mux failover ------------------------------------------------------

def failover_log() -> list[str]:
    p = home() / ".claude-mux" / "failover.log"
    return p.read_text().splitlines() if p.exists() else []


def test_healthy_provider_is_left_alone(cli, upstream, upstream2):
    add_direct(cli, "primary", upstream.url)
    add_direct(cli, "backup", upstream2.url)
    cli("activate", "primary")
    r = cli("failover")
    assert (r.code, r.stdout.strip()) == (0, "primary: healthy — no failover needed")
    assert cli("failover", "--json").json == {"ok": True, "name": "primary", "action": "none"}
    assert settings()["env"]["ANTHROPIC_BASE_URL"] == upstream.url
    assert failover_log() == []
    assert cli("failover-log").stdout.strip() == "No failover events yet."


def test_rate_limited_provider_fails_over_to_next_working_one(cli, upstream, upstream2):
    add_direct(cli, "primary", upstream.url, "primary-key")
    add_direct(cli, "backup", upstream2.url, "backup-key")
    cli("activate", "primary")
    upstream.route("GET", "/v1/models", Reply(429, json={"error": {"message": "Rate limit reached"}}))

    r = cli("failover")
    assert r.code == 0, r.output
    assert r.stdout.strip() == "Failover: switched to backup (was: HTTP 429 — Rate limit reached)"
    env = settings()["env"]
    assert (env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_AUTH_TOKEN"]) == (upstream2.url, "backup-key")
    assert cli("active").stdout.strip() == "backup"
    assert upstream2.received("GET", "/v1/models")[-1].headers["authorization"] == "Bearer backup-key"

    [line] = failover_log()
    assert "FROM=primary  TO=backup" in line
    [event] = cli("failover-log", "--json").json
    assert (event["from"], event["to"]) == ("primary", "backup")
    assert cli("failover-log", "-n", "1").stdout.strip() == line


def test_failover_from_proxy_provider_with_rejected_key(cli, upstream, upstream2, deepseek):
    add_direct(cli, "backup", upstream2.url)
    cli("activate", "deepseek")
    upstream.route("POST", "/v1/messages", Reply(401, json={"error": {"message": "invalid x-api-key"}}))
    r = cli("failover", "--json")
    assert r.json == {"ok": True, "switched_to": "backup", "reason": "HTTP 401 — Unauthorized"}
    assert cli("active").stdout.strip() == "backup"


def test_no_working_provider_left(cli, upstream, upstream2):
    add_direct(cli, "primary", upstream.url)
    add_direct(cli, "backup", upstream2.url)
    cli("activate", "primary")
    for srv in (upstream, upstream2):
        srv.route("GET", "/v1/models", Reply(403, json={"error": {"message": "forbidden"}}))
    r = cli("failover")
    assert r.code == 4
    assert "Failover failed: no working subscription found (HTTP 403 — forbidden)" in r.stderr
    assert "TO=none" in failover_log()[-1]
    assert cli("failover", "--json").json == {"ok": False, "reason": "no working subscription found"}


def test_failover_without_active_provider(cli):
    r = cli("failover")
    assert (r.code, r.stderr.strip()) == (3, "Error: no active subscription")
