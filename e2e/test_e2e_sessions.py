"""E2E: switching providers under a running Claude Code session, and `session` commands.

A real tmux server (private socket) runs the Claude Code stand-in from
e2e_support/fake_claude.py. Its behaviour model — base URL re-read on every
request, credentials read once at startup — is documented there; these tests
verify claude-mux against that model, not Claude Code itself.

A. Live switching: proxy providers switch while Claude keeps running.
B. OAuth accounts switch by `session restart --profile` (exit + resume).
C. `session restart` safety rules against real tmux.
D. Background sessions (`claude attach` / `claude respawn`) and `--self`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import time

import pytest

from claude_mux.session import DEFAULT_NUDGE
from e2e_support.fake_upstream import FakeUpstream, Reply
from e2e_support.helpers import home, instance_port, run_cli, settings, wait_for, wait_http
from e2e_support.tmux import TmuxServer

pytestmark = pytest.mark.skipif(not (shutil.which("tmux") and shutil.which("node")),
                                reason="session tests need tmux and node on PATH")


# --- fixtures and helpers ------------------------------------------------------------

@pytest.fixture
def upstream2():
    srv = FakeUpstream()
    yield srv
    srv.close()


@pytest.fixture
def anthropic(upstream, monkeypatch):
    """Where the stand-in sends requests when no ANTHROPIC_BASE_URL is set (api.anthropic.com).
    Must be requested before `tmux`, so the tmux server (and every pane) inherits it."""
    monkeypatch.setenv("FAKE_CLAUDE_ANTHROPIC_URL", upstream.url)
    return upstream


@pytest.fixture
def tmux():
    server = TmuxServer()
    yield server
    server.kill()
    for pid, data in registry().items():       # background sessions outlive their panes
        if data.get("kind") == "bg":
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass


def registry() -> dict[int, dict]:
    d = home() / ".claude" / "sessions"
    out = {}
    for p in d.glob("*.json") if d.exists() else []:
        try:
            out[int(p.stem)] = json.loads(p.read_text())
        except (ValueError, OSError):
            pass
    return out


def events(kind: str | None = None) -> list[dict]:
    p = home() / ".fake-claude" / "events.jsonl"
    rows = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    return [e for e in rows if kind is None or e["kind"] == kind]


def sessions() -> list[dict]:
    r = run_cli("session", "list", "--json")
    assert r.code == 0, r.output
    return r.json


def session_of(pane: str) -> dict:
    return next(s for s in sessions() if s["pane_id"] == pane)


def start_claude(tmux: TmuxServer, args: str = "", cwd: str | None = None) -> str:
    """Open a shell pane and start Claude Code in it, as a user would."""
    pane = tmux.shell_pane(cwd)
    tmux.type(pane, f"claude {args}".strip())
    tmux.wait(pane, "? for shortcuts", what="Claude Code status line")
    wait_for(lambda: any(s["pane_id"] == pane and s["session_id"] for s in sessions()),
             what="session registered")
    return pane


def say(tmux: TmuxServer, pane: str, text: str) -> dict:
    """Send a message from the Claude Code prompt; return the stand-in's request event."""
    before = len(events("request"))
    tmux.type(pane, text)
    return wait_for(lambda: next((e for e in events("request")[before:] if e["text"] == text), None),
                    timeout=20, what=f"request for {text!r}")


def restart(*args: str):
    return run_cli("session", "restart", *args, "--timeout", "30", timeout=120)


def last_message(req) -> str:
    return req.json()["messages"][-1]["content"]


def add_and_start(name: str, url: str, key: str, auth: str) -> int:
    assert run_cli("add", "-n", name, "-u", url, "-K", key, "--auth", auth).code == 0
    r = run_cli("start", name)
    assert r.code == 0, r.output
    port = instance_port(name)
    wait_http(f"http://127.0.0.1:{port}/health")
    return port


# =====================================================================================
# A. Live switching through the proxy — no restart
# =====================================================================================

@pytest.mark.parametrize("auth, suffix", [
    ("oauth_proxy", ""),       # Claude Max via claude-mux's own proxy
    ("bearer", "/v1"),         # API-key providers via heimsense (stand-in)
])
def test_running_claude_follows_a_provider_switch(tmux, upstream, upstream2, auth, suffix):
    add_and_start("acct-a", upstream.url + suffix, "key-A", auth)
    add_and_start("acct-b", upstream2.url + suffix, "key-B", auth)
    run_cli("activate", "acct-a")
    pane = start_claude(tmux)
    pid = session_of(pane)["claude_pid"]

    say(tmux, pane, "first question")
    [req] = upstream.received("POST", "/v1/messages")
    assert req.headers["authorization"] == "Bearer key-A"

    assert run_cli("activate", "acct-b").code == 0
    say(tmux, pane, "second question")
    [req] = upstream2.received("POST", "/v1/messages")
    assert req.headers["authorization"] == "Bearer key-B", \
        "the running session still holds key-A; the proxy must send acct-b's key"
    assert [m["content"] for m in req.json()["messages"] if m["role"] == "user"] == \
        ["first question", "second question"], "same conversation, new provider"
    assert session_of(pane)["claude_pid"] == pid, "no restart was needed"
    assert len(upstream.received("POST", "/v1/messages")) == 1


def test_running_claude_follows_a_failover(tmux, upstream, upstream2):
    add_and_start("primary", upstream.url + "/v1", "key-A", "bearer")
    add_and_start("backup", upstream2.url + "/v1", "key-B", "bearer")
    run_cli("activate", "primary")
    pane = start_claude(tmux)
    say(tmux, pane, "before the limit")

    upstream.route("POST", "/v1/messages",
                   Reply(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "limit"}}))
    assert say(tmux, pane, "hits the limit")["status"] == 429
    tmux.wait(pane, "API Error: 429")

    r = run_cli("failover")
    assert r.stdout.startswith("Failover: switched to backup"), r.output
    assert say(tmux, pane, "after failover")["status"] == 200
    # (the failover health check also posted a small message to backup)
    [req] = [q for q in upstream2.received("POST", "/v1/messages") if last_message(q) == "after failover"]
    assert req.headers["authorization"] == "Bearer key-B"


# =====================================================================================
# B. OAuth accounts: switching needs exit + resume, done by `session restart --profile`
# =====================================================================================

def test_oauth_account_switch_with_session_restart_profile(anthropic, tmux):
    run_cli("add", "-n", "max-a", "-u", "https://api.anthropic.com", "-K", "oat-A", "--auth", "oauth")
    run_cli("add", "-n", "max-b", "-u", "https://api.anthropic.com", "-K", "oat-B", "--auth", "oauth")
    run_cli("activate", "max-a")
    pane = start_claude(tmux, "--model claude-opus-4-6")
    before = session_of(pane)
    say(tmux, pane, "one")
    assert anthropic.received("POST", "/v1/messages")[-1].headers["authorization"] == "Bearer oat-A"

    # Behaviour model: a running session keeps the OAuth token it started with.
    run_cli("activate", "max-b")
    say(tmux, pane, "two")
    assert anthropic.received("POST", "/v1/messages")[-1].headers["authorization"] == "Bearer oat-A"

    r = restart(tmux.target(pane), "--profile", "max-b")
    assert r.code == 0, r.output
    assert re.match(rf"OK\s+{re.escape(tmux.target(pane))}\s+pid {before['claude_pid']} -> \d+, "
                    rf"session {before['session_id']}$", r.stdout.strip())
    assert "if this restart is interrupted, relaunch with" in r.stderr

    after = session_of(pane)
    assert after["session_id"] == before["session_id"] and after["claude_pid"] != before["claude_pid"]
    assert after["argv"][2:] == ["--model", "claude-opus-4-6", "--resume", before["session_id"]]
    nudge = wait_for(lambda: next((q for q in anthropic.received("POST", "/v1/messages")
                                   if last_message(q) == DEFAULT_NUDGE), None), what="nudge")
    assert nudge.headers["authorization"] == "Bearer oat-B", "resumed with the new account"
    assert [m["content"] for m in nudge.json()["messages"] if m["role"] == "user"][:2] == ["one", "two"]
    assert run_cli("active").stdout.strip() == "max-b"


def test_direct_provider_switch_takes_effect_after_restart(tmux, upstream, upstream2):
    """Direct providers (e.g. z.ai): the URL follows live; the key is read at startup
    (assumed — unknown for ANTHROPIC_AUTH_TOKEN), so a restart guarantees the new key."""
    run_cli("add", "-n", "zai-a", "-u", upstream.url, "-K", "zai-A", "--auth", "direct")
    run_cli("add", "-n", "zai-b", "-u", upstream2.url, "-K", "zai-B", "--auth", "direct")
    run_cli("activate", "zai-a")
    pane = start_claude(tmux)
    say(tmux, pane, "hello")
    r = restart(tmux.target(pane), "--profile", "zai-b", "--no-nudge")
    assert r.code == 0, r.output
    say(tmux, pane, "after restart")
    [req] = upstream2.received("POST", "/v1/messages")
    assert req.headers["authorization"] == "Bearer zai-B"


# =====================================================================================
# C. `session restart` safety rules against real tmux
# =====================================================================================

def test_session_list_shows_pane_states(anthropic, tmux):
    idle = start_claude(tmux, "--model claude-opus-4-6 --name idle-one")
    busy = start_claude(tmux)
    tmux.type(busy, "/busy 60")
    dialog = start_claude(tmux)
    tmux.type(dialog, "/permission")
    tmux.wait(dialog, "Do you want to proceed?")
    direct = tmux.direct_pane("claude --model claude-haiku-4-5-20251001")
    tmux.wait(direct, "? for shortcuts")

    states = wait_for(lambda: (lambda d: d if d.get(busy) == "working" else None)(
        {s["pane_id"]: s["state"] for s in sessions()}), what="busy state")
    assert states == {idle: "idle", busy: "working", dialog: "dialog", direct: "idle"}
    by_pane = {s["pane_id"]: s for s in sessions()}
    assert by_pane[idle]["mode"] == "shell" and by_pane[direct]["mode"] == "direct"
    assert by_pane[idle]["model"] == "claude-opus-4-6" and by_pane[idle]["context_pct"] == 17
    assert by_pane[idle]["name"] == "idle-one"
    assert by_pane[direct]["model"] == "claude-haiku-4-5-20251001"

    table = run_cli("session", "list").stdout
    assert re.search(rf"{re.escape(tmux.target(idle))}\s+\d+\s+idle\s+claude-opus-4-6\s+17%\s+idle-one", table)
    assert "argv: node " in table and "claude-code/cli.js --model claude-opus-4-6 --name idle-one" in table


def test_unsent_draft_is_never_sent(anthropic, tmux):
    pane = start_claude(tmux)
    tmux.type(pane, "half-written thought", enter=False)
    tmux.wait(pane, "❯ half-written thought")
    r = restart(tmux.target(pane))
    assert r.code == 4 and "SKIPPED" in r.stdout and "unsent text in the prompt line" in r.stdout
    assert "❯ half-written thought" in tmux.capture(pane), "the draft is left alone"

    old = session_of(pane)
    r = restart(tmux.target(pane), "--force", "--no-nudge")
    assert r.code == 0, r.output
    assert "clearing unsent text" in r.stderr
    assert session_of(pane)["session_id"] == old["session_id"]
    assert [e["text"] for e in events("submitted")] == ["/exit"], "the draft was never submitted"
    assert anthropic.requests == []


def test_open_dialog_is_never_answered(anthropic, tmux):
    pane = start_claude(tmux)
    tmux.type(pane, "/permission")
    tmux.wait(pane, "Do you want to proceed?")
    pid = session_of(pane)["claude_pid"]
    for extra in ([], ["--force"]):
        r = restart(tmux.target(pane), *extra)
        assert r.code == 4 and "dialog" in r.stdout, r.output
    assert "Do you want to proceed?" in tmux.capture(pane)
    assert events("dialog-answer") == [], "no key reached the dialog"
    assert session_of(pane)["claude_pid"] == pid


def test_busy_session_is_restarted_only_once_idle(anthropic, tmux):
    pane = start_claude(tmux)
    tmux.type(pane, "/busy 4")
    wait_for(lambda: session_of(pane)["state"] == "working", what="working")
    r = restart(tmux.target(pane), "--no-nudge")
    assert r.code == 0, r.output
    busy = next(e for e in events("submitted") if e["text"] == "/busy 4")
    exit_ = next(e for e in events("submitted") if e["text"] == "/exit")
    assert exit_["ts"] - busy["ts"] >= 4, "/exit must not be typed while the turn is running"


def test_force_interrupts_a_busy_session(anthropic, tmux):
    pane = start_claude(tmux)
    tmux.type(pane, "/busy 600")
    wait_for(lambda: session_of(pane)["state"] == "working", what="working")
    t0 = time.time()
    r = restart(tmux.target(pane), "--force", "--no-nudge")
    assert r.code == 0, r.output
    assert time.time() - t0 < 60


def test_copy_mode_blocks_keys_until_forced(anthropic, tmux):
    pane = start_claude(tmux)
    tmux("copy-mode", "-t", pane)
    assert tmux.pane_info(pane)["in_mode"]
    r = restart(tmux.target(pane), "--no-nudge")
    assert r.code == 4 and "copy/view mode" in r.stdout
    r = restart(tmux.target(pane), "--force", "--no-nudge")
    assert r.code == 0, r.output
    assert not tmux.pane_info(pane)["in_mode"]


def test_worktree_is_kept_and_resumed_in_place(anthropic, tmux):
    project = home() / "project"
    project.mkdir()
    pane = start_claude(tmux, "--worktree", cwd=str(project))
    before = session_of(pane)
    worktree = before["cwd"]
    assert worktree.startswith(str(project / ".claude" / "worktrees"))

    r = restart(tmux.target(pane), "--no-nudge")
    assert r.code == 0, r.output
    assert "answering exit dialog with option 1" in r.stderr
    assert [e["kind"] for e in events() if e["kind"].startswith("worktree-")] == \
        ["worktree-entered", "worktree-kept"], "Keep chosen; no second worktree created"
    after = session_of(pane)
    assert after["cwd"] == worktree and after["session_id"] == before["session_id"]
    assert "--worktree" not in after["argv"], "a bare --worktree would create a new worktree"
    assert len(list((project / ".claude" / "worktrees").iterdir())) == 1


def test_exit_and_stop_tasks_dialog_is_answered(anthropic, tmux):
    pane = start_claude(tmux)
    tmux.type(pane, "/task")
    tmux.wait(pane, "started background task")
    r = restart(tmux.target(pane), "--no-nudge")
    assert r.code == 0, r.output
    assert [(e["dialog"], e["answer"]) for e in events("dialog-answer")] == [("stop-tasks", "1")]


def test_direct_pane_is_respawned_and_remain_on_exit_restored(anthropic, tmux):
    pane = tmux.direct_pane("claude --model claude-opus-4-6")
    tmux.wait(pane, "? for shortcuts")
    wait_for(lambda: any(s["pane_id"] == pane for s in sessions()), what="session")
    before = session_of(pane)
    assert before["mode"] == "direct" and tmux.option(pane, "remain-on-exit") == ""
    r = restart(tmux.target(pane), "--no-nudge")
    assert r.code == 0, r.output
    after = session_of(pane)
    assert after["claude_pid"] != before["claude_pid"]
    assert after["session_id"] == before["session_id"]
    assert tmux.option(pane, "remain-on-exit") == "", "the pane's own setting is restored"


@pytest.mark.parametrize("default_shell", [
    "/bin/bash",
    pytest.param("/bin/sh", marks=pytest.mark.xfail(strict=True, reason=(
        "BUG: direct panes are relaunched with `tmux respawn-pane CMD`, which tmux runs as "
        "`$SHELL -c CMD`. bash exec()s a single command, but sh (dash; common in containers) "
        "keeps itself as the pane process with Claude as its child. The pane then looks like a "
        "shell pane: the next restart exits Claude, the shell exits with it and the pane closes "
        "(here the whole tmux server). Relaunching with `exec CMD` would avoid it."))),
])
def test_direct_pane_survives_repeated_restarts(anthropic, monkeypatch, default_shell):
    monkeypatch.setenv("SHELL", default_shell)    # tmux's default-shell comes from $SHELL
    server = TmuxServer()
    try:
        pane = server.direct_pane("claude --model claude-opus-4-6")
        server.wait(pane, "? for shortcuts")
        wait_for(lambda: any(s["pane_id"] == pane for s in sessions()), what="session")
        sid = session_of(pane)["session_id"]
        for _ in range(2):
            r = restart(pane, "--no-nudge")
            assert r.code == 0, r.output
            assert session_of(pane)["session_id"] == sid and session_of(pane)["mode"] == "direct"
    finally:
        server.kill()


def test_relaunch_keeps_options_but_not_the_initial_prompt(anthropic, tmux):
    pane = start_claude(tmux, "--model claude-opus-4-6 --permission-mode plan 'write a haiku'")
    wait_for(lambda: anthropic.received("POST", "/v1/messages"), what="initial prompt sent")
    sid = session_of(pane)["session_id"]
    r = restart(tmux.target(pane), "--model", "opus")
    assert r.code == 0, r.output
    after = session_of(pane)
    # the original --model is replaced by the expanded alias; other options are kept
    assert after["argv"][2:] == ["--permission-mode", "plan", "--model", "claude-opus-5-5", "--resume", sid]
    nudge = wait_for(lambda: next((q for q in anthropic.received("POST", "/v1/messages")
                                   if last_message(q) == DEFAULT_NUDGE), None), what="nudge")
    assert nudge.json()["model"] == "claude-opus-5-5", "the alias was expanded to a full model id"
    sent = [last_message(q) for q in anthropic.received("POST", "/v1/messages")]
    assert sent.count("write a haiku") == 1, "the initial prompt is not sent again on resume"


def test_restart_all_with_match(anthropic, tmux):
    a1 = start_claude(tmux, "--name agent-1")
    a2 = start_claude(tmux, "--name agent-2")
    other = start_claude(tmux, "--name reviewer")
    pids = {p: session_of(p)["claude_pid"] for p in (a1, a2, other)}
    r = run_cli("session", "restart", "--all", "--match", "^agent-|agent-", "--no-nudge",
                "--timeout", "30", "--json", timeout=180)
    assert r.code == 0, r.output
    assert sorted((x["target"], x["status"]) for x in r.json) == \
        sorted([(tmux.target(a1), "OK"), (tmux.target(a2), "OK")])
    assert session_of(a1)["claude_pid"] != pids[a1] and session_of(a2)["claude_pid"] != pids[a2]
    assert session_of(other)["claude_pid"] == pids[other]


# =====================================================================================
# D. Background sessions (`claude attach` / `claude respawn`) and `--self`
# =====================================================================================

def start_background(args: str = "") -> str:
    import subprocess
    r = subprocess.run(["claude", "--bg", *args.split()], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.parametrize("pane_kind", [
    "direct",
    pytest.param("shell", marks=pytest.mark.xfail(strict=True, reason=(
        "BUG: when `claude attach` was started from a shell, the attach client exits after "
        "`claude respawn` but the pane stays alive (the shell), so claude-mux never re-attaches. "
        "The stale attach screen above the shell prompt is taken for the re-attached view, the "
        "nudge is typed into the shell (then erased; Enter is not sent) and the result is "
        "'OK (nudge not submitted)', exit 0 — while the pane shows a bare shell."))),
])
def test_background_session_is_respawned_and_reattached(anthropic, tmux, pane_kind):
    job = start_background("--model claude-sonnet-4-6")
    if pane_kind == "direct":
        pane = tmux.direct_pane(f"claude attach {job}")
    else:
        pane = tmux.shell_pane()
        tmux.type(pane, f"claude attach {job}")
    tmux.wait(pane, "? for shortcuts")
    say(tmux, pane, "hello background")

    info = wait_for(lambda: next((s for s in sessions() if s["pane_id"] == pane), None), what="session")
    assert info["attached"] and info["session_pid"] != info["claude_pid"]
    assert "* background session shown through `claude attach`" in run_cli("session", "list").stdout

    r = restart(tmux.target(pane))
    assert r.code == 0, r.output
    assert f"running node " in r.stderr and f"respawn {job}" in r.stderr
    after = session_of(pane)
    assert after["session_id"] == info["session_id"] and after["session_pid"] != info["session_pid"]
    nudge = wait_for(lambda: next((q for q in anthropic.received("POST", "/v1/messages")
                                   if last_message(q) == DEFAULT_NUDGE), None), timeout=30, what="nudge")
    assert [m["content"] for m in nudge.json()["messages"] if m["role"] == "user"][0] == "hello background"
    assert tmux.option(pane, "remain-on-exit") == ""


def test_background_session_refuses_model_change(anthropic, tmux):
    job = start_background()
    pane = tmux.direct_pane(f"claude attach {job}")
    tmux.wait(pane, "? for shortcuts")
    wait_for(lambda: any(s["pane_id"] == pane for s in sessions()), what="session")
    r = restart(tmux.target(pane), "--model", "opus")
    assert r.code == 1 and "--model is not supported for background sessions" in r.stdout


def test_self_restart_from_inside_the_session(anthropic, tmux):
    pane = start_claude(tmux)
    before = session_of(pane)
    tmux.type(pane, "!claude-mux session restart --self --delay 1 --no-nudge --timeout 30 --json")
    shell = wait_for(lambda: next(iter(events("shell")), None), timeout=30, what="command ran")
    assert shell["code"] == 0, shell["output"]
    scheduled = json.loads(shell["output"])
    assert scheduled["ok"] and scheduled["target"] == pane

    log_file = home() / ".claude-mux" / "session-restart.log"
    log = wait_for(lambda: log_file.exists() and re.search(r"^(OK|SKIPPED|FAILED) ", log_file.read_text(), re.M)
                   and log_file.read_text(), timeout=60, what="helper result in the log")
    assert re.search(rf"^OK\s+{re.escape(pane)}\s+pid {before['claude_pid']} -> \d+", log, re.M), log
    assert session_of(pane)["session_id"] == before["session_id"]
    assert session_of(pane)["claude_pid"] != before["claude_pid"]
