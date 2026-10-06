"""Tests for `cm session` (list / restart) against a fake tmux.

No real tmux server, process or session registry is touched: every external
operation goes through FakeHost.
"""
import json
import re
import shlex
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from claude_mux import session as sess
from claude_mux.cli import cli

HOME = "/home/user"
REG = f"{HOME}/.claude/sessions"
CLAUDE = "/usr/bin/claude"
STATUS = "─" * 40 + "\n❯ \n" + "─" * 40 + "\n  main · Opus 5.5 ctx 17% · 5h 10%\n  ⏵⏵ bypass permissions on\n"
WORKING = "* Brewing… (esc to interrupt)\n" + STATUS

KEEP_DIALOG = (
    "This session ran in a worktree.\n"
    "❯ 1. Keep worktree\n"
    "  2. Remove worktree\n"
    "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
)
KEEP_SECOND_DIALOG = (
    "This session ran in a worktree.\n"
    "❯ 1. Remove worktree and branch\n"
    "  2. Keep worktree\n"
    "Enter to select · Esc to cancel\n"
)
REMOVE_ONLY_DIALOG = (
    "Worktree has changes.\n"
    "❯ 1. Remove worktree\n"
    "  2. Cancel\n"
    "Enter to select\n"
)
STOP_TASKS_DIALOG = (
    "Background tasks are still running.\n"
    "❯ 1. Exit and stop tasks\n"
    "  2. Cancel\n"
    "Enter to select\n"
)
# Exit dialogs as rendered by Claude Code 2.1.x (captured from a live pane).
RULE = "▔" * 60 + "\n"
REAL_KEEP_DIALOG = (
    "● PONG\n" + RULE
    + "   Exiting worktree session\n"
    "   You have 1 uncommitted file. These will be lost if you remove the worktree.\n"
    "   ❯ 1. Keep worktree    Stays at /work/.claude/worktrees/wt\n"
    "     2. Remove worktree  All changes and commits will be lost.\n"
    "   Enter to confirm · Esc to cancel\n"
)
REAL_STOP_TASKS_DIALOG = (
    RULE + "   Background work is running\n"
    "   The following will stop when you exit:\n"
    "   shell · sleep 600\n"
    "   ❯ 1. Exit and stop tasks\n"
    "     2. Move to background and exit\n"
    "     3. Stay\n"
    "   Enter to confirm · Esc to cancel\n"
)
REAL_TRUST_DIALOG = (
    "─" * 60 + "\n Accessing workspace:\n /work\n"
    " Quick safety check: Is this a project you created or one you trust?\n"
    " ❯ No, exit\n   Yes, I trust this folder\n"
    " Enter to confirm · Esc to cancel\n"
)
REAL_PERMISSION_PROMPT = (
    "─" * 60 + "\n Bash command\n   │ python3 -m claude_mux session restart --self\n"
    " This command requires approval\n Do you want to proceed?\n"
    " ❯ 1. Yes\n   2. Yes, and allow access to /work\n   3. No\n"
    " Esc to cancel · Tab to amend\n"
)
UNKNOWN_DIALOG = "Do you trust this folder?\n❯ 1. Yes\n  2. No\nEnter to select\n"


def fish_split(text):
    """Minimal fish tokenizer: bare words, single quotes (\\ and \' escapes), ';'."""
    words, cur, quoted, i = [], "", False, 0
    while i < len(text):
        c = text[i]
        if c == "'":
            quoted, i = True, i + 1
            while text[i] != "'":
                if text[i] == "\\" and text[i + 1] in "\\'":
                    cur, i = cur + text[i + 1], i + 2
                else:
                    cur, i = cur + text[i], i + 1
        elif c in " ;":
            if cur or quoted:
                words.append(cur)
            cur, quoted = "", False
            if c == ";":
                words.append(";")
        elif c in "\"$*?~{}()|&<>\\":
            raise AssertionError(f"unquoted fish metacharacter {c!r} in {text!r}")
        else:
            cur += c
        i += 1
    if cur or quoted:
        words.append(cur)
    return words


class FakeHost:
    """In-memory tmux + /proc + session registry."""

    def __init__(self):
        self.files: dict[str, str] = {}
        self.panes: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.killed: list[int] = []
        self.t = 0.0
        self.next_pid = 5000
        # behaviour knobs per pane
        self.dialogs: dict[str, list[str]] = {}
        self.answers: list[tuple[str, str]] = []
        self.resume_session_override: str | None = None
        self.busy_polls: dict[str, int] = {}
        self.attach_survives_respawn = False
        self.respawn_argvs: list[list[str]] = []
        self.respawn_rc = 0
        self.after_exit: dict[str, object] = {}
        #: captures that still show the old screen after text is typed or
        #: erased (Claude Code renders typed text only after ~75 ms)
        self.render_delay = 0

    # -- setup helpers -----------------------------------------------------
    def add_proc(self, pid, argv, children=()):
        self.files[f"/proc/{pid}/cmdline"] = "\0".join(argv) + "\0"
        self.files[f"/proc/{pid}/task/{pid}/children"] = " ".join(map(str, children))

    def remove_proc(self, pid):
        self.files.pop(f"/proc/{pid}/cmdline", None)
        self.files.pop(f"/proc/{pid}/task/{pid}/children", None)
        self.files.pop(f"{REG}/{pid}.json", None)

    def register(self, pid, session_id, **extra):
        data = {"pid": pid, "sessionId": session_id, "cwd": "/work", "name": f"s-{pid}",
                "kind": "interactive", "status": "idle", **extra}
        self.files[f"{REG}/{pid}.json"] = json.dumps(data)
        self.files[f"{REG}/{pid}.0123abcd.key"] = "secret"

    def add_shell_pane(self, target, claude_pid, argv, session_id, screen=STATUS, shell_name="bash",
                       **reg):
        shell = claude_pid - 1
        self.add_proc(shell, [f"-{shell_name}"], children=[claude_pid])
        self.add_proc(claude_pid, argv)
        self.register(claude_pid, session_id, **reg)
        self.panes[target] = {"target": target, "pane_id": f"%{len(self.panes)}",
                              "pane_pid": shell, "command": "claude", "path": "/work",
                              "dead": False, "screen": screen, "mode": "shell",
                              "claude": claude_pid, "remain": False, "shell": shell_name}

    def add_direct_pane(self, target, claude_pid, argv, session_id, screen=STATUS, **reg):
        self.add_proc(claude_pid, argv)
        self.register(claude_pid, session_id, **reg)
        self.panes[target] = {"target": target, "pane_id": f"%{len(self.panes)}",
                              "pane_pid": claude_pid, "command": "claude", "path": "/work",
                              "dead": False, "screen": screen, "mode": "direct",
                              "claude": claude_pid, "remain": False}

    def add_attach_pane(self, target, attach_pid, bg_pid, session_id, head=(CLAUDE,),
                        shell=False):
        self.add_proc(attach_pid, [*head, "attach", session_id[:8]])
        self.add_proc(bg_pid, ["claude", "bg-spare", "--bg-spare", "/tmp/x.sock"])
        self.register(bg_pid, session_id, kind="bg", jobId=session_id[:8])
        pane_pid = attach_pid
        if shell:
            pane_pid = attach_pid - 1
            self.add_proc(pane_pid, ["-bash"], children=[attach_pid])
        self.panes[target] = {"target": target, "pane_id": f"%{len(self.panes)}",
                              "pane_pid": pane_pid, "command": "claude", "path": "/work",
                              "dead": False, "screen": STATUS,
                              "mode": "shell" if shell else "direct",
                              "claude": attach_pid, "remain": False, "bg": bg_pid}

    # -- Host API ----------------------------------------------------------
    def read_text(self, path):
        return self.files.get(path)

    def listdir(self, path):
        prefix = path.rstrip("/") + "/"
        return sorted({p[len(prefix):].split("/")[0] for p in self.files if p.startswith(prefix)})

    def kill(self, pid, sig=15):
        self.killed.append(pid)
        self.remove_proc(pid)
        for pane in self.panes.values():
            if pane.get("bg") == pid:
                pane["screen"] = "Conversation moved to background.\n  Sessions overview\n"
        return True

    def home(self):
        return HOME

    def sleep(self, seconds):
        self.t += seconds
        for target, n in list(self.busy_polls.items()):
            if n > 0:
                self.busy_polls[target] = n - 1
                if n == 1:
                    self.panes[target]["screen"] = STATUS

    def now(self):
        return self.t

    def find(self, target):
        """Pane by target (sess:win.pane) or pane id (%N), as tmux resolves -t."""
        for pane in self.panes.values():
            if target in (pane["target"], pane["pane_id"]):
                return pane
        return None

    def run(self, argv):
        if "respawn" in argv[:3]:
            self.respawn_argvs.append(argv)
            return self._claude_respawn(argv[argv.index("respawn") + 1])
        assert argv[0] == "tmux", argv
        self.calls.append(argv[1:])
        cmd, args = argv[1], argv[2:]
        if cmd == "list-panes":
            return 0, "\n".join(self._fmt(p) for p in self.panes.values()) + "\n"
        target = args[args.index("-t") + 1] if "-t" in args else None
        pane = self.find(target) if target else None
        if cmd == "run-shell":
            return 0, ""
        if pane is None:
            return 1, ""
        if cmd == "display-message":
            return 0, self._fmt(pane) + "\n"
        if cmd == "capture-pane":
            return 0, self.render(pane)
        if cmd == "send-keys":
            self._send(pane, args[args.index("-t") + 2:])
            return 0, ""
        if cmd == "set-option":
            pane["remain_opt"] = None if "-u" in args else args[-1]
            pane["remain"] = pane["remain_opt"] == "on"
            return 0, ""
        if cmd == "show-options":
            return 0, (pane.get("remain_opt") or "") + "\n"
        if cmd == "respawn-pane":
            assert pane["dead"], "respawn on a live pane"
            self._launch(pane, shlex.split(args[-1]), args[args.index("-c") + 1])
            pane["pane_pid"] = pane["claude"]
            pane["dead"] = False
            return 0, ""
        return 1, ""

    # -- simulation --------------------------------------------------------
    @staticmethod
    def _fmt(p):
        return sess._FMT_SEP.join([p["target"], p["pane_id"], str(p["pane_pid"]), p["command"],
                          p["path"], "1" if p["dead"] else "0",
                          "1" if p.get("in_mode") else "0"])

    def _exit_claude(self, pane):
        hook = self.after_exit.pop(pane["target"], None)
        if hook:
            hook()
        self.remove_proc(pane["claude"])
        if pane["mode"] == "shell":
            self.files[f"/proc/{pane['pane_pid']}/task/{pane['pane_pid']}/children"] = ""
            pane["command"], pane["screen"] = pane.get("shell", "bash"), "user@host:/work$ "
        else:
            assert pane["remain"], "pane would have closed: remain-on-exit not set"
            pane["dead"], pane["screen"] = True, "Pane is dead\n"

    def _launch(self, pane, argv, cwd):
        self.next_pid += 1
        pid = self.next_pid
        self.add_proc(pid, argv)
        if "attach" in argv:
            self.add_proc(pid, argv)
            pane.update(claude=pid, command="claude", screen=STATUS, launched=argv)
            return
        sid = self.resume_session_override or argv[argv.index("--resume") + 1]
        self.register(pid, sid, cwd=cwd)
        pane.update(claude=pid, command="claude", screen=STATUS, launched=argv, launched_cwd=cwd)
        if pane.get("draft_after_launch"):
            pane["draft"] = pane.pop("draft_after_launch")
        if pane["mode"] == "shell":
            self.files[f"/proc/{pane['pane_pid']}/task/{pane['pane_pid']}/children"] = str(pid)

    def _send(self, pane, keys):
        t = pane["target"]
        if keys[:2] == ["-X", "cancel"]:
            pane["in_mode"] = False
            return
        if pane.get("in_mode"):
            # copy/view mode: the keys are mode commands, Claude Code never sees them
            pane.setdefault("mode_keys", []).append(keys)
            return
        edits = keys[0] == "-l" or keys == ["C-u"] or keys[2:] == ["BSpace"]
        if edits and self.render_delay and not (keys[0] == "-l" and keys[1].isdigit()
                                                and sess.has_dialog(pane["screen"])):
            if not pane.get("stale"):
                pane["stale"] = [0, self._render_now(pane)]
            pane["stale"][0] = self.render_delay
        if keys[0] == "-l":
            text = keys[1]
            if sess.has_dialog(pane["screen"]) and text.isdigit():
                self.answers.append((t, text))
                queue = self.dialogs.get(t, [])
                if queue:
                    queue.pop(0)
                if queue:
                    pane["screen"] = queue[0]
                else:
                    self._exit_claude(pane)
                return
            pane["typed"] = pane.get("typed", "") + text
            if pane.get("on_type"):
                pane.pop("on_type")(pane)
            return
        if keys == ["Enter"]:
            # Claude Code submits the whole prompt line: draft + typed text.
            text = pane.pop("draft", "") + pane.pop("typed", "")
            if text == "/exit":
                queue = self.dialogs.get(t, [])
                if queue:
                    pane["screen"] = queue[0]
                else:
                    self._exit_claude(pane)
            elif pane["mode"] == "shell" and pane["command"] == "bash" and text:
                words, cwd = shlex.split(text), pane["path"]
                if words[:1] == ["cd"] and words[2:3] == ["&&"]:
                    cwd, words = words[1], words[3:]
                self._launch(pane, words, cwd)
            elif pane["mode"] == "shell" and pane["command"] == "fish" and text:
                words, cwd = fish_split(text), pane["path"]
                if words[:1] == ["cd"] and words[2:4] == [";", "and"]:
                    cwd, words = words[1], words[4:]
                self._launch(pane, words, cwd)
            elif text:
                pane.setdefault("received", []).append(text)
            return
        if keys == ["Escape"] and pane.get("on_escape"):
            pane.pop("on_escape")(pane)
            return
        if keys == ["C-u"]:
            line = pane.get("draft", "") + pane.get("typed", "")
            # Kills to the start of the current line only.
            pane["draft"], pane["typed"] = line.rsplit("\n", 1)[0] + "\n" if "\n" in line \
                else "", ""
            return
        if keys[:1] == ["-N"] and keys[2:] == ["BSpace"]:
            line = pane.get("draft", "") + pane.get("typed", "")
            pane["draft"], pane["typed"] = line[:max(0, len(line) - int(keys[1]))], ""
            return
        if keys == ["C-c", "C-c"] and pane.get("bg") and not self.files.get(
                f"/proc/{pane['bg']}/cmdline"):
            self._exit_claude(pane)

    def render(self, pane):
        """The pane as captured; for ``render_delay`` captures after an edit, the old screen."""
        stale = pane.get("stale")
        if stale:
            stale[0] -= 1
            if stale[0] <= 0:
                pane.pop("stale")
            return stale[1]
        return self._render_now(pane)

    def _render_now(self, pane):
        """Draft and typed text shown in the prompt line."""
        line = pane.get("draft", "") + pane.get("typed", "")
        if line and pane["command"] == "claude":
            return pane["screen"].replace("\n❯ \n", "\n❯ " + line.replace("\n", "\n  ") + "\n", 1)
        return pane["screen"]

    def _claude_respawn(self, job):
        """`claude respawn <job>`: new background pid, same session; the attach
        client exits unless ``attach_survives_respawn`` is set."""
        self.calls.append(["claude-respawn", job])
        if self.respawn_rc:
            return self.respawn_rc, ""
        for rpid, raw in list(self.files.items()):
            if not rpid.startswith(REG + "/") or not rpid.endswith(".json"):
                continue
            data = json.loads(raw)
            if data.get("jobId") != job:
                continue
            self.remove_proc(data["pid"])
            self.next_pid += 1
            # As seen live: the worker rewrites argv[0] to "claude bg-spare".
            self.add_proc(self.next_pid, ["claude bg-spare", "--bg-spare", "/tmp/y.sock"])
            self.register(self.next_pid, data["sessionId"], kind="bg", jobId=job)
            for pane in self.panes.values():
                if pane.get("bg") == data["pid"]:
                    pane["bg"] = self.next_pid
                    if not self.attach_survives_respawn:
                        assert pane["remain"], "pane would have closed: remain-on-exit not set"
                        self.remove_proc(pane["claude"])
                        pane["dead"], pane["screen"] = True, "Pane is dead\n"
            return 0, f"respawned {job}\n"
        return 1, ""

    def sends(self, target):
        pane = self.find(target) or {}
        ids = {target, pane.get("target"), pane.get("pane_id")}
        return [c for c in self.calls if c[0] == "send-keys" and c[c.index("-t") + 1] in ids]


@pytest.fixture()
def host():
    return FakeHost()


def restarter(host, **kw):
    return sess.Restarter(host, timeout=kw.pop("timeout", 60), poll=1, **kw)


AGENT_ARGV = [CLAUDE, "--agent", "dev", "--worktree", "wt-dev", "--permission-mode",
              "bypassPermissions", "--model", "claude-opus-5-5", "--resume", "old-id",
              "--fork-session"]


# ---------------------------------------------------------------------------
# Discovery / list
# ---------------------------------------------------------------------------

class TestList:
    def test_maps_pane_to_pid_and_registry(self, host):
        host.add_shell_pane("a:0.0", 101, AGENT_ARGV, "sid-a")
        (s,) = sess.list_sessions(host)
        assert (s.target, s.claude_pid, s.session_id, s.mode) == ("a:0.0", 101, "sid-a", "shell")
        assert s.cwd == "/work" and s.name == "s-101"
        assert s.model == "claude-opus-5-5" and s.context_pct == 17 and s.state == "idle"

    def test_ignores_key_files_in_registry(self, host):
        host.add_shell_pane("a:0.0", 101, AGENT_ARGV, "sid-a")
        reg = sess.load_registry(host)
        assert list(reg) == [101]

    def test_states(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "s1", screen=WORKING)
        host.add_shell_pane("b:0.0", 201, [CLAUDE], "s2", screen=KEEP_DIALOG)
        host.add_shell_pane("c:0.0", 301, [CLAUDE], "s3", status="busy")
        states = {s.target: s.state for s in sess.list_sessions(host)}
        assert states == {"a:0.0": "working", "b:0.0": "dialog", "c:0.0": "working"}

    def test_dialog_text_in_transcript_is_not_a_dialog(self, host):
        screen = "agent said: «Enter to select» was in my grep\n" + "line\n" * 20 + STATUS
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "s1", screen=screen)
        assert sess.list_sessions(host)[0].state == "idle"

    def test_attached_background_session(self, host):
        host.add_attach_pane("bg:0.0", 400, 401, "cbf58c04-aaaa")
        (s,) = sess.list_sessions(host)
        assert s.attached and s.session_pid == 401 and s.session_id == "cbf58c04-aaaa"

    def test_cli_list_json(self, host):
        host.add_shell_pane("a:0.0", 101, AGENT_ARGV, "sid-a")
        with patch("claude_mux.cli._session_host", return_value=host):
            r = CliRunner().invoke(cli, ["session", "list", "--json"])
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)[0]["session_id"] == "sid-a"

    def test_docker_host_runs_as_container_user(self):
        h = sess.make_host("box", "vscode")
        assert h.exec_argv(["tmux", "ls"]) == ["docker", "exec", "-u", "vscode", "box",
                                               "tmux", "ls"]
        assert sess.make_host("box").exec_argv(["tmux"]) == ["docker", "exec", "box", "tmux"]

    def test_cli_passes_container_user(self, host):
        with patch("claude_mux.cli._session_host", return_value=host) as mk:
            r = CliRunner().invoke(cli, ["session", "list", "--container", "box",
                                         "--container-user", "vscode"])
        assert r.exit_code == 0, r.output
        mk.assert_called_once_with("box", "vscode")

    def test_cli_container_user_requires_container(self, host):
        with patch("claude_mux.cli._session_host", return_value=host):
            r = CliRunner().invoke(cli, ["session", "list", "--container-user", "vscode"])
        assert r.exit_code == 2

    def test_pane_format_has_no_control_characters(self):
        # tmux may print control characters (tab) as "_", which broke parsing
        # of every pane when run through `docker exec`.
        assert all(c.isprintable() for c in sess._PANE_FMT)
        assert sess.Tmux._parse("a:0.0|~|%1|~|12|~|claude|~|/p q|~|0")["path"] == "/p q"

    def test_cli_list_is_read_only(self, host):
        host.add_shell_pane("a:0.0", 101, AGENT_ARGV, "sid-a")
        with patch("claude_mux.cli._session_host", return_value=host):
            r = CliRunner().invoke(cli, ["session", "list"])
        assert r.exit_code == 0 and "sid-a" in r.output
        assert not [c for c in host.calls if c[0] not in ("list-panes", "capture-pane")]


# ---------------------------------------------------------------------------
# Model aliases and argv
# ---------------------------------------------------------------------------

class TestArgvAndModel:
    def test_relaunch_argv_keeps_flags_and_resumes(self):
        argv = sess.build_relaunch_argv(AGENT_ARGV, "sid")
        assert argv == [CLAUDE, "--agent", "dev", "--worktree", "wt-dev", "--permission-mode",
                        "bypassPermissions", "--model", "claude-opus-5-5", "--resume", "sid"]

    def test_relaunch_argv_drops_session_id_flag_and_replaces_model(self):
        argv = sess.build_relaunch_argv([CLAUDE, "--session-id", "x", "--model=opus", "-c"],
                                        "sid", "claude-sonnet-5-5")
        assert argv == [CLAUDE, "--model", "claude-sonnet-5-5", "--resume", "sid"]

    @pytest.mark.parametrize("args, kept", [
        (["do the task", "--model", "m"], ["--model", "m"]),
        (["--model", "m", "do the task"], ["--model", "m"]),
        (["--verbose", "do the task", "--model", "m"], ["--verbose", "--model", "m"]),
        (["--model", "m", "--", "do the task"], ["--model", "m"]),
    ])
    def test_initial_prompt_is_not_replayed_on_resume(self, args, kept):
        # Seen live: `claude "prompt" ... --resume <id>` sends the prompt again.
        assert sess.build_relaunch_argv([CLAUDE, *args], "sid") == [CLAUDE, *kept,
                                                                     "--resume", "sid"]

    def test_variadic_and_optional_values_are_kept(self):
        argv = [CLAUDE, "--add-dir", "/a", "/b", "--worktree", "--debug", "api",
                "--permission-mode", "plan", "prompt"]
        assert sess.build_relaunch_argv(argv, "sid") == [
            CLAUDE, "--add-dir", "/a", "/b", "--worktree", "--debug", "api",
            "--permission-mode", "plan", "--resume", "sid"]

    @pytest.mark.parametrize("flag", ["--plan-mode-required", "--init", "--hard-fail",
                                      "-d2e", "--reply-on-resume", "--enable-auto-mode",
                                      "--session-mirror", "-v"])
    def test_hidden_boolean_flags_do_not_swallow_the_prompt(self, flag):
        # `claude --plan-mode-required "prompt"` must not keep the prompt as
        # the flag's value: it would be sent again on resume.
        assert sess.build_relaunch_argv([CLAUDE, flag, "do the task", "--model", "m"],
                                        "sid") == [CLAUDE, flag, "--model", "m",
                                                   "--resume", "sid"]

    @pytest.mark.parametrize("flag", ["--channels", "--dangerously-load-development-channels"])
    def test_variadic_channels_keep_every_value(self, flag):
        argv = [CLAUDE, flag, "a", "b", "--model", "m"]
        assert sess.build_relaunch_argv(argv, "sid") == [CLAUDE, flag, "a", "b", "--model",
                                                         "m", "--resume", "sid"]

    @pytest.mark.parametrize("flag", ["--rc", "--remote", "--project"])
    def test_optional_value_flag_followed_by_option(self, flag):
        assert sess.build_relaunch_argv([CLAUDE, flag, "--model", "opus"], "sid") == [
            CLAUDE, flag, "--model", "opus", "--resume", "sid"]

    def test_unknown_flag_never_swallows_a_following_option(self):
        argv = [CLAUDE, "--brand-new", "--model", "opus", "--other-new", "v", "prompt"]
        groups, prompt = sess.split_claude_args(argv[1:])
        assert groups == [["--brand-new"], ["--model", "opus"], ["--other-new", "v"]]
        assert prompt == ["prompt"]
        assert sess.unknown_flags(argv) == ["--brand-new", "--other-new"]

    def test_value_flag_takes_a_value_starting_with_dash(self):
        groups, _ = sess.split_claude_args(["--append-system-prompt", "-x", "--verbose"])
        assert groups == [["--append-system-prompt", "-x"], ["--verbose"]]

    def test_agent_team_flags_are_kept_with_their_values(self):
        argv = [CLAUDE, "--agent-id", "a@t", "--team-name", "t", "--plan-mode-required",
                "--agent-color", "blue", "start"]
        assert sess.unknown_flags(argv) == []
        assert sess.build_relaunch_argv(argv, "sid") == [*argv[:-1], "--resume", "sid"]

    def test_restart_warns_about_unknown_flags(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE, "--brand-new", "x"], "sid")
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "OK", r.message
        assert any("--brand-new" in w for w in r.warnings)

    def test_restart_does_not_resend_initial_prompt(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE, "--agent", "dev", "fix the bug"], "sid")
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "OK", r.message
        assert host.panes["a:0.0"]["launched"] == [CLAUDE, "--agent", "dev", "--resume", "sid"]

    def test_short_alias_expanded_to_full_id(self):
        assert sess.resolve_model("opus") == ("claude-opus-5-5", None)
        assert sess.resolve_model("Sonnet")[0] == "claude-sonnet-5-5"
        assert sess.resolve_model("haiku")[0] == "claude-haiku-4-5-20251001"

    def test_alias_table_is_configurable(self):
        assert sess.resolve_model("opus", {"opus": "claude-opus-9"})[0] == "claude-opus-9"

    def test_unknown_short_alias_warns(self):
        model, warning = sess.resolve_model("opusplan")
        assert model == "opusplan" and "not a full model id" in warning

    def test_restart_expands_alias_from_original_argv(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE, "--model", "opus"], "sid")
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "OK", r.message
        assert host.panes["a:0.0"]["launched"] == [CLAUDE, "--model", "claude-opus-5-5",
                                                    "--resume", "sid"]

    def test_cli_alias_from_config(self, host, tmp_path):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        with patch("claude_mux.cli._session_host", return_value=host), \
                patch("claude_mux.cli._session_aliases", return_value={"opus": "claude-opus-x"}):
            r = CliRunner().invoke(cli, ["session", "restart", "a:0.0", "--model", "opus",
                                         "--no-nudge", "--timeout", "30"])
        assert r.exit_code == 0, r.output
        assert host.panes["a:0.0"]["launched"][-3:] == ["claude-opus-x", "--resume", "sid"]


# ---------------------------------------------------------------------------
# Restart traps
# ---------------------------------------------------------------------------

class TestRestartTraps:
    def test_refuses_when_dialog_visible(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid", screen=KEEP_DIALOG)
        r = restarter(host).restart("a:0.0")
        assert r.status == "SKIPPED" and "dialog" in r.message
        assert host.sends("a:0.0") == []

    def test_refuses_dialog_even_with_force(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid", screen=UNKNOWN_DIALOG)
        r = restarter(host).restart("a:0.0", force=True)
        assert r.status == "SKIPPED" and host.sends("a:0.0") == []

    def test_waits_until_idle(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid", screen=WORKING)
        host.busy_polls["a:0.0"] = 3
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "OK", r.message
        assert host.t >= 3

    def test_working_session_times_out_without_force(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid", screen=WORKING)
        r = restarter(host, timeout=5).restart("a:0.0")
        assert r.status == "FAILED" and "idle" in r.message
        assert host.sends("a:0.0") == []

    def test_force_sends_escape_first(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid", screen=WORKING)
        r = restarter(host).restart("a:0.0", force=True, nudge=None)
        assert r.status == "OK", r.message
        assert host.sends("a:0.0")[0] == ["send-keys", "-t", "%0", "Escape"]

    def test_keep_worktree_dialog_answers_keep(self, host):
        host.add_shell_pane("a:0.0", 101, AGENT_ARGV, "sid")
        host.dialogs["a:0.0"] = [KEEP_DIALOG]
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "OK", r.message
        assert host.answers == [("a:0.0", "1")]

    def test_keep_worktree_picks_keep_when_it_is_not_option_1(self, host):
        host.add_shell_pane("a:0.0", 101, AGENT_ARGV, "sid")
        host.dialogs["a:0.0"] = [KEEP_SECOND_DIALOG]
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "OK", r.message
        assert host.answers == [("a:0.0", "2")]

    def test_never_answers_remove_worktree(self, host):
        host.add_shell_pane("a:0.0", 101, AGENT_ARGV, "sid")
        host.dialogs["a:0.0"] = [REMOVE_ONLY_DIALOG]
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "FAILED" and "Keep" in r.message
        assert host.answers == []

    def test_exit_and_stop_tasks_dialog_answers_1(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.dialogs["a:0.0"] = [STOP_TASKS_DIALOG, KEEP_DIALOG]
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "OK", r.message
        assert host.answers == [("a:0.0", "1"), ("a:0.0", "1")]

    def test_unknown_dialog_is_never_answered(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.dialogs["a:0.0"] = [UNKNOWN_DIALOG]
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "FAILED" and "unknown dialog" in r.message
        assert host.answers == []

    @pytest.mark.parametrize("screen", [REAL_KEEP_DIALOG, REAL_STOP_TASKS_DIALOG,
                                        REAL_TRUST_DIALOG, REAL_PERMISSION_PROMPT])
    def test_refuses_real_enter_to_confirm_dialogs(self, host, screen):
        # Enter in an open dialog confirms the highlighted option (possibly
        # "Remove worktree"), so nothing may be typed into such a pane.
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid", screen=screen)
        assert sess.list_sessions(host)[0].state == "dialog"
        r = restarter(host).restart("a:0.0", nudge=None, force=True)
        assert r.status == "SKIPPED"
        assert host.sends("a:0.0") == []

    def test_real_keep_worktree_dialog_answers_keep(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.dialogs["a:0.0"] = [REAL_KEEP_DIALOG]
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "OK", r.message
        assert host.answers == [("a:0.0", "1")]

    def test_real_stop_tasks_dialog_answers_1(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.dialogs["a:0.0"] = [REAL_STOP_TASKS_DIALOG]
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "OK", r.message
        assert host.answers == [("a:0.0", "1")]

    def test_transcript_above_dialog_does_not_pick_the_answer(self):
        # A numbered list mentioning "keep" and "worktree" in the transcript
        # must not be mistaken for the options of the dialog below it.
        screen = ("● Options:\n  2. Keep using the worktree\n" + REAL_STOP_TASKS_DIALOG)
        assert sess.choose_dialog_answer(screen) == 1

    def test_registry_waiting_status_is_a_dialog(self, host):
        # A permission prompt sets the registry status to "waiting".
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid", status="waiting")
        assert sess.list_sessions(host)[0].state == "dialog"
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "SKIPPED" and host.sends("a:0.0") == []

    def test_permission_prompt_is_never_answered(self):
        with pytest.raises(sess.SessionError, match="unknown dialog"):
            sess.choose_dialog_answer(REAL_PERMISSION_PROMPT)

    def test_trust_dialog_is_never_answered(self):
        with pytest.raises(sess.SessionError, match="unknown dialog"):
            sess.choose_dialog_answer(REAL_TRUST_DIALOG)

    def test_text_and_enter_sent_in_separate_calls(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        r = restarter(host).restart("a:0.0", nudge="hello agent")
        assert r.status == "OK", r.message
        for call in host.sends("a:0.0"):
            assert not ("-l" in call and "Enter" in call), call
        literal = [c[-1] for c in host.sends("a:0.0") if "-l" in c]
        assert literal[0] == "/exit" and literal[-1] == "hello agent"
        assert host.panes["a:0.0"]["received"] == ["hello agent"]

    def test_default_nudge_asks_to_recreate_loops(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        r = restarter(host).restart("a:0.0")
        assert r.status == "OK", r.message
        (msg,) = host.panes["a:0.0"]["received"]
        assert msg == sess.DEFAULT_NUDGE and "scheduled loops" in msg

    def test_background_session_uses_claude_respawn_and_reattaches(self, host):
        # Killing the background process does not restart it cleanly (the
        # daemon brings it back and the attach view stays open), so the
        # supported `claude respawn <job>` is used and the pane re-attached.
        host.add_attach_pane("bg:0.0", 400, 401, "cbf58c04-aaaa")
        r = restarter(host).restart("bg:0.0", nudge="hi")
        assert r.status == "OK", r.message
        assert ["claude-respawn", "cbf58c04"] in host.calls
        assert host.killed == []
        assert (r.old_pid, r.new_pid) == (401, host.panes["bg:0.0"]["bg"]) and r.new_pid != 401
        assert host.panes["bg:0.0"]["launched"] == [CLAUDE, "attach", "cbf58c04"]
        assert host.panes["bg:0.0"]["remain"] is False
        assert host.panes["bg:0.0"]["received"] == ["hi"]

    def test_background_session_attach_client_survives_respawn(self, host):
        host.attach_survives_respawn = True
        host.add_attach_pane("bg:0.0", 400, 401, "cbf58c04-aaaa")
        r = restarter(host).restart("bg:0.0", nudge=None)
        assert r.status == "OK", r.message
        assert not [c for c in host.calls if c[0] == "respawn-pane"]
        assert host.panes["bg:0.0"]["remain"] is False

    def test_background_session_waits_until_idle(self, host):
        host.add_attach_pane("bg:0.0", 400, 401, "cbf58c04-aaaa")
        host.panes["bg:0.0"]["screen"] = WORKING
        r = restarter(host, timeout=5).restart("bg:0.0", nudge=None)
        assert r.status == "FAILED" and "idle" in r.message
        assert not [c for c in host.calls if c[0] == "claude-respawn"]

    def test_background_session_refuses_model_change(self, host):
        host.add_attach_pane("bg:0.0", 400, 401, "cbf58c04-aaaa")
        r = restarter(host).restart("bg:0.0", model="opus", nudge=None)
        assert r.status == "FAILED" and "--model" in r.message
        assert not [c for c in host.calls if c[0] == "claude-respawn"]

    def test_direct_pane_uses_remain_on_exit_and_respawn(self, host):
        host.add_direct_pane("d:0.0", 700, AGENT_ARGV, "sid-d")
        r = restarter(host).restart("d:0.0", nudge=None)
        assert r.status == "OK", r.message
        pane = host.panes["d:0.0"]
        assert pane["remain"] is False  # restored after respawn
        assert any(c[0] == "respawn-pane" for c in host.calls)
        # relaunched inside the worktree dir, so --worktree is dropped
        assert "--worktree" not in pane["launched"]

    def test_relaunch_uses_original_argv_and_resume(self, host):
        host.add_shell_pane("a:0.0", 101, AGENT_ARGV, "sid-a")
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "OK", r.message
        assert host.panes["a:0.0"]["launched"] == sess.build_relaunch_argv(AGENT_ARGV, "sid-a")
        assert r.old_pid == 101 and r.new_pid not in (None, 101)

    def test_shell_pane_drops_bare_worktree_and_resumes_in_its_cwd(self, host):
        # A bare --worktree creates a new, randomly named worktree on every
        # launch, so replaying it would resume in a fresh worktree. The shell
        # stayed in the launch dir; cd into the session's worktree instead.
        argv = [CLAUDE, "--worktree", "--agent", "dev"]
        host.add_shell_pane("a:0.0", 101, argv, "sid-a", cwd="/work/.claude/worktrees/x")
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "OK", r.message
        pane = host.panes["a:0.0"]
        assert pane["launched"] == [CLAUDE, "--agent", "dev", "--resume", "sid-a"]
        assert pane["launched_cwd"] == "/work/.claude/worktrees/x"

    @pytest.mark.parametrize("flag", ["-w", "--worktree"])
    def test_shell_pane_keeps_named_worktree(self, host, flag):
        argv = [CLAUDE, flag, "wt-dev", "--agent", "dev"]
        host.add_shell_pane("a:0.0", 101, argv, "sid-a", cwd="/work/.claude/worktrees/wt-dev")
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "OK", r.message
        pane = host.panes["a:0.0"]
        assert pane["launched"] == [CLAUDE, flag, "wt-dev", "--agent", "dev",
                                    "--resume", "sid-a"]
        assert pane["launched_cwd"] == "/work"

    @pytest.mark.parametrize("shell_name", ["bash", "fish"])
    def test_cwd_and_arguments_with_spaces_and_quotes(self, host, shell_name):
        # The relaunch command is typed into the pane's own shell; fish treats
        # backslashes inside single quotes differently from POSIX shells.
        cwd = "/work/it's a \\dir/.claude/worktrees/x"
        argv = [CLAUDE, "--worktree", "--append-system-prompt", "say 'hi' \\o/ $HOME *"]
        host.add_shell_pane("a:0.0", 101, argv, "sid", shell_name=shell_name, cwd=cwd)
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "OK", r.message
        pane = host.panes["a:0.0"]
        assert pane["launched"] == [CLAUDE, "--append-system-prompt", argv[3], "--resume", "sid"]
        assert pane["launched_cwd"] == cwd

    def test_fish_quote(self):
        assert sess.fish_quote("plain/path-1.0") == "plain/path-1.0"
        assert sess.fish_quote("a b") == "'a b'"
        assert sess.fish_quote("it's \\x") == "'it\\'s \\\\x'"

    def test_verify_fails_when_session_id_differs(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.resume_session_override = "another-session"
        r = restarter(host, timeout=5).restart("a:0.0", nudge=None)
        assert r.status == "FAILED" and "resumed session" in r.message

    def test_unregistered_session_is_refused(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.files.pop(f"{REG}/101.json")
        r = restarter(host).restart("a:0.0")
        assert r.status == "FAILED" and "registry" in r.message
        assert host.sends("a:0.0") == []

    def test_targets_the_pane_id_when_windows_are_renumbered(self, host):
        # With renumber-windows on, closing window 0 during the restart moves
        # our window to index 0. sess:win.pane would then miss (or hit
        # another pane); the pane id %N stays the same.
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "other")
        host.add_shell_pane("a:1.0", 201, [CLAUDE], "sid")

        def renumber():
            del host.panes["a:0.0"]
            host.panes["a:1.0"]["target"] = "a:0.0"
        host.after_exit["a:1.0"] = renumber
        r = restarter(host).restart("a:1.0", nudge="hi")
        assert r.status == "OK", r.message
        assert host.panes["a:1.0"]["received"] == ["hi"]
        targets = {c[c.index("-t") + 1] for c in host.calls if "-t" in c}
        assert targets <= {"a:1.0", "%1"}

    def test_background_session_respawn_uses_the_node_head(self, host):
        head = ("/usr/bin/node", "/opt/claude-code/cli.js")
        host.add_attach_pane("bg:0.0", 400, 401, "cbf58c04-aaaa", head=head)
        r = restarter(host).restart("bg:0.0", nudge=None)
        assert r.status == "OK", r.message
        assert host.respawn_argvs == [[*head, "respawn", "cbf58c04"]]

    @pytest.mark.parametrize("prior", ["on", "off", "failed"])
    def test_direct_pane_restores_users_remain_on_exit(self, host, prior):
        host.add_direct_pane("d:0.0", 700, [CLAUDE], "sid-d")
        host.panes["d:0.0"]["remain_opt"] = prior
        r = restarter(host).restart("d:0.0", nudge=None)
        assert r.status == "OK", r.message
        assert host.panes["d:0.0"]["remain_opt"] == prior

    @pytest.mark.parametrize("shell", [False, True])
    def test_failed_background_restart_restores_remain_on_exit(self, host, shell):
        host.add_attach_pane("bg:0.0", 400, 401, "cbf58c04-aaaa", shell=shell)
        host.respawn_rc = 1
        r = restarter(host).restart("bg:0.0", nudge=None)
        assert r.status == "FAILED" and "respawn" in r.message
        assert host.panes["bg:0.0"].get("remain_opt") is None

    def test_skipped_restart_leaves_remain_on_exit_untouched(self, host):
        host.add_direct_pane("d:0.0", 700, [CLAUDE], "sid-d", screen=KEEP_DIALOG)
        r = restarter(host).restart("d:0.0", nudge=None)
        assert r.status == "SKIPPED"
        assert not [c for c in host.calls if c[0] == "set-option"]


# ---------------------------------------------------------------------------
# Prompt line: drafts, re-checks before typing and Enter, recovery log
# ---------------------------------------------------------------------------

LIVE_RULE = "\x1b[38;5;244m" + "─" * 60 + "\n"
LIVE_STATUS_LINE = "\x1b[39m  \x1b[90mOpus 5.5\x1b[38;5;246m ctx 30%\x1b[39m\n"


def styled_prompt(line):
    """A prompt box as captured with ``capture-pane -e`` from Claude Code 2.1.x."""
    return "● done\n" + LIVE_RULE + "\x1b[39m❯\xa0" + line + "\n" + LIVE_RULE + LIVE_STATUS_LINE


class TestPromptLine:
    def test_empty_prompt_with_cursor(self):
        assert sess.prompt_text(styled_prompt("\x1b[7m \x1b[0m")) == ""

    def test_typed_draft(self):
        assert sess.prompt_text(styled_prompt("half a \x1b[7mt\x1b[0mhought")) == "half a thought"

    def test_dim_placeholder_is_not_a_draft(self):
        # Placeholder and prompt suggestions: cursor on the first character,
        # the rest dim (SGR 2).
        line = '\x1b[7mT\x1b[0m\x1b[2mry "fix lint errors"\x1b[22m'
        assert sess.prompt_text(styled_prompt(line)) == ""

    def test_multiline_draft_and_titled_rule(self):
        screen = ("── my-agent " + "─" * 40 + "\n❯ first line\n  second line\n"
                  + "─" * 52 + "\n  ctx 3%\n")
        assert sess.prompt_text(screen) == "first line second line"

    def test_no_prompt_box(self):
        assert sess.prompt_text("user@host:~$ \n") is None

    def test_truncated_escape_sequences(self):
        assert sess.strip_ansi("a\x1b") == "a"
        assert sess.strip_ansi("\x1b]8;;http://x\x1b\\link\x1b[38;5;1mb") == "linkb"

    def test_same_text_accepts_wrapping_and_paste_markers(self):
        assert sess._same_text("/exit", "/exit")
        assert sess._same_text("Your session was\n restarted", "Your session was restarted")
        assert sess._same_text("[Pasted text #1 +3 lines]", "a\nb\nc\nd")
        assert not sess._same_text("draft /exit", "/exit")


class TestDraftAndRechecks:
    def test_draft_is_never_submitted_with_exit(self, host):
        # Seen live: "/exit" was appended to an unsent draft and the agent
        # received "draft text /exit" as a normal message.
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.panes["a:0.0"]["draft"] = "half a thought"
        r = restarter(host).restart("a:0.0")
        assert r.status == "SKIPPED" and "unsent text" in r.message
        assert host.sends("a:0.0") == []
        assert host.panes["a:0.0"]["draft"] == "half a thought"
        assert "received" not in host.panes["a:0.0"]
        assert host.panes["a:0.0"]["claude"] == 101

    def test_draft_in_direct_pane_does_not_touch_remain_on_exit(self, host):
        host.add_direct_pane("d:0.0", 700, [CLAUDE], "sid-d")
        host.panes["d:0.0"]["draft"] = "wip"
        r = restarter(host).restart("d:0.0")
        assert r.status == "SKIPPED"
        assert not [c for c in host.calls if c[0] == "set-option"]

    @pytest.mark.parametrize("draft", ["half a thought", "line one\nline two"])
    def test_force_clears_the_draft_first(self, host, draft):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.panes["a:0.0"]["draft"] = draft
        r = restarter(host).restart("a:0.0", force=True, nudge="hi")
        assert r.status == "OK", r.message
        assert ["send-keys", "-t", "%0", "C-u"] in host.calls
        assert host.panes["a:0.0"]["received"] == ["hi"]

    def test_draft_before_nudge_is_never_appended_to(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.panes["a:0.0"]["draft_after_launch"] = "typed during the restart"
        r = restarter(host).restart("a:0.0", nudge="hi")
        assert r.status == "SKIPPED" and "nudge not sent" in r.message
        assert "pid 101 ->" in r.message
        assert "received" not in host.panes["a:0.0"]
        assert host.panes["a:0.0"]["draft"] == "typed during the restart"

    def test_force_clears_draft_before_nudge(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.panes["a:0.0"]["draft_after_launch"] = "typed during the restart"
        r = restarter(host).restart("a:0.0", nudge="hi", force=True)
        assert r.status == "OK", r.message
        assert host.panes["a:0.0"]["received"] == ["hi"]

    def test_dialog_appearing_while_typing_exit_gets_no_enter(self, host):
        # Enter in a permission prompt approves option 1 ("Yes").
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.panes["a:0.0"]["on_type"] = lambda p: p.update(screen=REAL_PERMISSION_PROMPT)
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "FAILED" and "Enter not sent" in r.message
        assert not [c for c in host.sends("a:0.0") if c[-1] == "Enter"]

    def test_text_typed_by_someone_else_while_typing_gets_no_enter(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.panes["a:0.0"]["on_type"] = lambda p: p.update(draft="user ")
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "FAILED" and "prompt line changed" in r.message
        assert not [c for c in host.sends("a:0.0") if c[-1] == "Enter"]
        assert "received" not in host.panes["a:0.0"]
        # Only the text the tool typed is removed; the other text stays.
        assert host.panes["a:0.0"]["draft"] == "user "
        assert host.panes["a:0.0"].get("typed", "") == ""

    def test_dialog_after_force_escape_is_rechecked(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid", screen=WORKING)
        host.panes["a:0.0"]["on_escape"] = lambda p: p.update(screen=REAL_PERMISSION_PROMPT)
        r = restarter(host).restart("a:0.0", force=True, nudge=None)
        assert r.status == "SKIPPED" and "dialog" in r.message
        assert [c[-1] for c in host.sends("a:0.0")] == ["Escape"]

    def test_dialog_appearing_while_typing_nudge_gets_no_enter(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        r = sess.Restarter(host, timeout=60, poll=1)
        real_launch = host._launch

        def launch(pane, argv, cwd):
            real_launch(pane, argv, cwd)
            pane["on_type"] = lambda p: p.update(screen=REAL_PERMISSION_PROMPT)
        host._launch = launch
        result = r.restart("a:0.0", nudge="hi")
        # The restart itself succeeded: only the nudge was not sent.
        assert result.status == "SKIPPED" and "Enter not sent" in result.message
        assert "pid 101 ->" in result.message
        assert "received" not in host.panes["a:0.0"]



def _never_render_after_launch(host):
    real_launch = host._launch

    def launch(pane, argv, cwd):
        real_launch(pane, argv, cwd)
        host.render_delay = 10 ** 6
    host._launch = launch


class TestLateRendering:
    """Claude Code renders typed text ~75 ms after send-keys (measured live:
    '' at 0/25/50 ms, '/exit' at 75 ms). A capture right after typing sees
    the old prompt line."""

    @pytest.mark.parametrize("delay", [1, 3])
    def test_exit_and_nudge_rendered_late_are_submitted(self, host, delay):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.render_delay = delay
        r = restarter(host).restart("a:0.0", nudge="hi")
        assert r.status == "OK", r.message
        assert host.panes["a:0.0"]["claude"] != 101
        assert host.panes["a:0.0"]["received"] == ["hi"]

    def test_direct_pane_exit_rendered_late(self, host):
        host.add_direct_pane("d:0.0", 700, [CLAUDE], "sid-d")
        host.render_delay = 2
        r = restarter(host).restart("d:0.0", nudge=None)
        assert r.status == "OK", r.message

    def test_exit_never_rendered_is_removed_and_fails(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.render_delay = 10 ** 6
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "FAILED" and "Enter not sent" in r.message
        assert "/exit" in r.message
        sends = host.sends("a:0.0")
        assert not [c for c in sends if c[-1] == "Enter"]
        assert sends[-1][-3:] == ["-N", "5", "BSpace"]
        assert host.panes["a:0.0"].get("typed", "") == ""
        assert host.panes["a:0.0"]["claude"] == 101
        # Polled for a bounded time, not the restart timeout.
        assert host.t <= sess.Restarter.RENDER_TIMEOUT + 1

    def test_no_backspace_when_the_prompt_box_is_gone(self, host):
        # Without a prompt line the keys could reach a shell: never erase blind.
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.panes["a:0.0"]["on_type"] = lambda p: p.update(screen="user@host:/work$ ")
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "FAILED" and "Enter not sent" in r.message
        assert [c[-1] for c in host.sends("a:0.0")] == ["/exit"]

    def test_nudge_never_rendered_after_successful_restart_is_skipped(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        _never_render_after_launch(host)
        r = restarter(host).restart("a:0.0", nudge="hi")
        assert r.status == "SKIPPED", r.message
        assert "pid 101 ->" in r.message and "nudge" in r.message
        assert host.panes["a:0.0"]["claude"] != 101
        assert "received" not in host.panes["a:0.0"]
        assert host.panes["a:0.0"].get("typed", "") == ""

    def test_nudge_timeout_exit_code_is_skipped_not_failed(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        _never_render_after_launch(host)
        r = _invoke(host, ["a:0.0", "--nudge", "hi"])
        assert r.exit_code == 4, r.output
        assert r.output.splitlines()[-1].startswith("SKIPPED")

    @pytest.mark.parametrize("draft", ["half a thought", "line one\nline two"])
    def test_force_clear_rendered_late(self, host, draft):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.panes["a:0.0"]["draft"] = draft
        host.render_delay = 2
        r = restarter(host).restart("a:0.0", force=True, nudge="hi")
        assert r.status == "OK", r.message
        assert host.panes["a:0.0"]["received"] == ["hi"]

    def test_force_clear_single_line_needs_no_backspace(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.panes["a:0.0"]["draft"] = "half a thought"
        host.render_delay = 2
        r = restarter(host).restart("a:0.0", force=True, nudge=None)
        assert r.status == "OK", r.message
        assert not [c for c in host.sends("a:0.0") if c[-1] == "BSpace"]


class TestCopyMode:
    def test_pane_in_copy_mode_is_skipped_before_typing(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        host.panes["a:0.0"]["in_mode"] = True
        r = restarter(host).restart("a:0.0", nudge=None)
        assert r.status == "SKIPPED" and "copy/view mode" in r.message
        assert host.sends("a:0.0") == []
        assert host.panes["a:0.0"]["claude"] == 101

    def test_force_leaves_copy_mode_first(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid", screen=WORKING)
        host.busy_polls["a:0.0"] = 0
        host.panes["a:0.0"]["in_mode"] = True
        r = restarter(host).restart("a:0.0", nudge=None, force=True)
        assert r.status == "OK", r.message
        assert host.sends("a:0.0")[0][-2:] == ["-X", "cancel"]
        assert "mode_keys" not in host.panes["a:0.0"]

    def test_copy_mode_before_nudge_skips_the_nudge(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "sid")
        real_launch = host._launch

        def launch(pane, argv, cwd):
            real_launch(pane, argv, cwd)
            pane["in_mode"] = True
        host._launch = launch
        r = restarter(host).restart("a:0.0", nudge="hi")
        assert r.status == "SKIPPED" and "copy/view mode" in r.message
        assert "pid 101 ->" in r.message
        assert "mode_keys" not in host.panes["a:0.0"]

class TestRecoveryLog:
    def _events(self, host):
        events = []
        return events, (lambda m: events.append((len(host.calls), m)))

    def _exit_index(self, host):
        return next(i for i, c in enumerate(host.calls) if c[-1] == "/exit")

    def test_shell_pane_logs_relaunch_command_before_exit(self, host):
        argv = [CLAUDE, "--agent", "dev", "--worktree", "--model", "claude-opus-5-5"]
        host.add_shell_pane("a:0.0", 101, argv, "sid", cwd="/work/wt x")
        events, log = self._events(host)
        r = restarter(host, log=log).restart("a:0.0", nudge=None)
        assert r.status == "OK", r.message
        (at, msg), = [e for e in events if "relaunch with" in e[1]]
        assert at <= self._exit_index(host)
        expected = shlex.join([CLAUDE, "--agent", "dev", "--model", "claude-opus-5-5",
                               "--resume", "sid"])
        assert msg.endswith(f"cd {shlex.quote('/work/wt x')} && {expected}")

    def test_direct_pane_logs_respawn_pane_command_before_exit(self, host):
        host.add_direct_pane("d:0.0", 700, [CLAUDE, "--agent", "dev"], "sid-d")
        events, log = self._events(host)
        r = restarter(host, log=log).restart("d:0.0", nudge=None)
        assert r.status == "OK", r.message
        (at, msg), = [e for e in events if "relaunch with" in e[1]]
        assert at <= self._exit_index(host)
        cmd = msg.split("relaunch with: ", 1)[1]
        assert shlex.split(cmd) == ["tmux", "respawn-pane", "-k", "-t", "%0", "-c", "/work",
                                    shlex.join([CLAUDE, "--agent", "dev", "--resume", "sid-d"])]

    def test_background_session_logs_reattach_command_before_respawn(self, host):
        host.add_attach_pane("bg:0.0", 400, 401, "cbf58c04-aaaa")
        events, log = self._events(host)
        r = restarter(host, log=log).restart("bg:0.0", nudge=None)
        assert r.status == "OK", r.message
        assert any("re-attach with: tmux respawn-pane" in m for _, m in events)


# ---------------------------------------------------------------------------
# CLI: --all, --self, --profile, usage
# ---------------------------------------------------------------------------

def _invoke(host, args, env=None):
    with patch("claude_mux.cli._session_host", return_value=host):
        return CliRunner().invoke(cli, ["session", "restart", *args], env=env or {})


class TestRestartCli:
    def test_requires_exactly_one_mode(self, host):
        assert _invoke(host, []).exit_code == 2
        assert _invoke(host, ["a:0.0", "--all"]).exit_code == 2

    def test_all_reports_one_line_per_session(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "s1")
        host.add_shell_pane("b:0.0", 201, [CLAUDE], "s2", screen=KEEP_DIALOG)
        host.add_shell_pane("c:0.0", 301, [CLAUDE], "s3")
        host.dialogs["c:0.0"] = [UNKNOWN_DIALOG]
        r = _invoke(host, ["--all", "--no-nudge", "--timeout", "20"])
        lines = [line.split()[:2] for line in r.output.splitlines()
                 if line.split()[:1] in (["OK"], ["SKIPPED"], ["FAILED"])]
        assert lines == [["OK", "a:0.0"], ["SKIPPED", "b:0.0"], ["FAILED", "c:0.0"]]
        assert r.exit_code == 1

    def test_all_with_match(self, host):
        host.add_shell_pane("agent-1:0.0", 101, [CLAUDE], "s1")
        host.add_shell_pane("other:0.0", 201, [CLAUDE], "s2")
        r = _invoke(host, ["--all", "--match", "^agent-", "--no-nudge"])
        assert r.exit_code == 0, r.output
        assert "agent-1:0.0" in r.output and "other:0.0" not in r.output
        assert host.sends("other:0.0") == []

    def test_all_skips_own_pane(self, host):
        host.add_shell_pane("me:0.0", 101, [CLAUDE], "s1")
        own = host.panes["me:0.0"]["pane_id"]
        r = _invoke(host, ["--all", "--no-nudge"], env={"TMUX_PANE": own})
        assert "SKIPPED" in r.output and "--self" in r.output
        assert host.sends("me:0.0") == []

    def test_self_spawns_detached_helper(self, host):
        host.add_shell_pane("me:0.0", 101, [CLAUDE], "s1")
        r = _invoke(host, ["--self", "--model", "opus", "--delay", "3"], env={"TMUX_PANE": "%0"})
        assert r.exit_code == 0, r.output
        (call,) = [c for c in host.calls if c[0] == "run-shell"]
        assert call[1] == "-b"
        assert call[2].startswith("sleep 3; ")
        assert "session restart %0 --model opus" in call[2]
        assert host.sends("%0") == [] and host.sends("me:0.0") == []

    def test_self_finds_pane_of_background_session_without_tmux_pane(self, host):
        host.add_attach_pane("bg:0.0", 400, 401, "cbf58c04-aaaa")
        host.files["/proc/900/status"] = "Name:\tpython3\nPPid:\t899\n"
        host.files["/proc/899/status"] = "Name:\tbash\nPPid:\t401\n"
        host.files["/proc/401/status"] = "Name:\tclaude\nPPid:\t1\n"
        assert sess.own_pane(host, pid=900) == host.panes["bg:0.0"]["pane_id"]

    def test_own_pane_of_shell_pane(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "s1")
        host.add_shell_pane("b:0.0", 201, [CLAUDE], "s2")
        host.files["/proc/900/status"] = "PPid:\t201\n"
        host.files["/proc/201/status"] = "PPid:\t200\n"
        assert sess.own_pane(host, pid=900) == host.panes["b:0.0"]["pane_id"]
        assert sess.own_pane(host, pid=12345) is None

    @pytest.mark.parametrize("existing, expected_suffix", [
        (None, ""), ("/deps a:/more", ":/deps a:/more")])
    def test_self_helper_prepends_package_to_pythonpath(self, tmp_path, existing,
                                                        expected_suffix):
        # The helper runs the caller's claude_mux, but keeps an existing
        # PYTHONPATH (dependencies may only be found through it).
        fake_python = tmp_path / "python"
        fake_python.write_text('#!/bin/sh\nprintf %s "$PYTHONPATH"\n')
        fake_python.chmod(0o755)
        cmd = sess.self_restart_command(str(fake_python), "%3", delay=0, extra=[])
        env = {"PATH": "/usr/bin:/bin"}
        if existing is not None:
            env["PYTHONPATH"] = existing
        out = subprocess.run(["/bin/sh", "-c", cmd], env=env, capture_output=True,
                             text=True, check=True).stdout
        parent = str(sess.Path(sess.__file__).resolve().parent.parent)
        assert out == parent + expected_suffix

    def test_self_escapes_tmux_formats_in_the_command(self, host):
        # run-shell expands formats: "#S" would become the session name and
        # "#(cmd)" would run cmd. Every "#" must reach the shell unchanged.
        host.add_shell_pane("me:0.0", 101, [CLAUDE], "s1")
        nudge = "issue #S #{session_name} #(touch /tmp/pwned) ##x"
        r = _invoke(host, ["--self", "--nudge", nudge], env={"TMUX_PANE": "%0"})
        assert r.exit_code == 0, r.output
        (call,) = [c for c in host.calls if c[0] == "run-shell"]
        assert re.sub("##", "", call[2]).count("#") == 0
        assert shlex.quote(nudge) in call[2].replace("##", "#")

    def test_self_run_shell_command_always_exits_zero(self, host):
        # run-shell -b puts the pane in view-mode when its command exits
        # non-zero; the helper's status is in the log instead.
        host.add_shell_pane("me:0.0", 101, [CLAUDE], "s1")
        r = _invoke(host, ["--self"], env={"TMUX_PANE": "%0"})
        assert r.exit_code == 0, r.output
        (call,) = [c for c in host.calls if c[0] == "run-shell"]
        assert call[2].endswith("; exit 0")

    @pytest.mark.parametrize("rc", [1, 4])
    def test_self_helper_exits_zero_and_logs_failure(self, tmp_path, rc):
        fake_python = tmp_path / "python"
        fake_python.write_text(f'#!/bin/sh\necho "FAILED   %3  boom"\nexit {rc}\n')
        fake_python.chmod(0o755)
        log = tmp_path / "restart.log"
        cmd = sess.self_restart_command(str(fake_python), "%3", delay=0, extra=[], log=str(log))
        done = subprocess.run(["/bin/sh", "-c", cmd], env={"PATH": "/usr/bin:/bin"})
        assert done.returncode == 0
        assert "FAILED   %3  boom" in log.read_text()

    def test_self_outside_tmux_is_usage_error(self, host):
        assert _invoke(host, ["--self"], env={"TMUX_PANE": ""}).exit_code == 2

    def test_profile_activates_before_restart(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "s1")
        order = []
        sync = MagicMock()
        with patch("claude_mux.cli._managers", return_value=(MagicMock(), sync, None, None)), \
                patch("claude_mux.cli._find_sub", return_value={"id": "x", "name": "work"}):
            sync.sync_default.side_effect = lambda _id: order.append(("activate", len(host.calls)))
            r = _invoke(host, ["a:0.0", "--profile", "work", "--no-nudge"])
        assert r.exit_code == 0, r.output
        assert order == [("activate", 0)]

    def test_profile_with_container_is_refused(self, host):
        # Activating a profile writes the HOST's config; the container's
        # Claude Code would never see it.
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "s1")
        with patch("claude_mux.cli.cmd_activate") as activate:
            r = _invoke(host, ["a:0.0", "--profile", "work", "--container", "box",
                               "--no-nudge"])
        assert r.exit_code == 2
        assert "--profile" in r.output and "--container" in r.output
        activate.assert_not_called()
        assert host.sends("a:0.0") == []

    def test_dialog_exit_code(self, host):
        host.add_shell_pane("a:0.0", 101, [CLAUDE], "s1", screen=KEEP_DIALOG)
        r = _invoke(host, ["a:0.0"])
        assert r.exit_code == 4 and r.output.startswith("SKIPPED")

    @pytest.mark.parametrize("name", ["list", "restart"])
    def test_cli_docs_cover_every_session_flag(self, name):
        root = sess.Path(__file__).resolve().parent.parent
        docs = (root / "docs" / "CLI.md").read_text()
        section = docs.split(f"### `claude-mux session {name}`", 1)[1].split("\n### ", 1)[0]
        command = cli.commands["session"].commands[name]
        flags = [o for p in command.params for o in p.opts
                 if o.startswith("--") and o != "--help"]
        assert flags
        assert [f for f in flags if f"`{f}" not in section] == []

    def test_changelog_mentions_container_user(self):
        root = sess.Path(__file__).resolve().parent.parent
        unreleased = (root / "CHANGELOG.md").read_text().split("## [0.", 1)[0]
        assert "--container-user" in unreleased

    def test_help_lists_commands(self):
        r = CliRunner().invoke(cli, ["session", "--help"])
        assert r.exit_code == 0 and "list" in r.output and "restart" in r.output
        r = CliRunner().invoke(cli, ["session", "restart", "--help"])
        assert "--self" in r.output and "Keep worktree" in r.output
