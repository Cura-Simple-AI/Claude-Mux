"""Safe listing, exit and resume of Claude Code sessions running in tmux.

Everything that touches the outside world (tmux, /proc, the session registry,
signals, sleeping) goes through a ``Host`` object so the logic can be tested
against a fake tmux without touching real sessions.

Pane modes:
  "shell"  - the pane runs a shell and Claude Code is its child. After exit we
             wait for the shell and type the relaunch command.
  "direct" - Claude Code (or ``claude attach``) IS the pane process. The pane
             would close on exit, so remain-on-exit is set first and the
             session is relaunched with ``tmux respawn-pane``.

Attached panes run ``claude attach <job>`` for a background session. Those are
restarted with ``claude respawn <job>`` and the pane is re-attached if the
attach client exited.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Short model aliases may resolve to an older model under another profile,
#: so they are expanded to full ids. Override or extend via
#: ``session_model_aliases`` in the claude-mux config file.
DEFAULT_MODEL_ALIASES = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-4-5-20251001",
}

#: Session-scoped cron jobs do not survive a resume.
DEFAULT_NUDGE = (
    "Your session was restarted and resumed. Scheduled loops and cron jobs do "
    "not survive a resume: please recreate any scheduled loops you had, then "
    "continue where you left off."
)

SHELLS = {"bash", "zsh", "sh", "fish", "dash", "ksh", "-bash", "-zsh", "-sh"}

#: Flags that tie a launch to a specific conversation; they are replaced by
#: ``--resume <sessionId>`` on relaunch. Value: True if the flag takes a value.
_SESSION_FLAGS = {
    "--resume": True, "-r": True, "--session-id": True,
    "--fork-session": False, "--continue": False, "-c": False,
}

#: Footer of a dialog. Exit and trust dialogs say "Enter to confirm", older
#: selection lists "Enter to select"; permission prompts have no Enter hint
#: at all ("Esc to cancel · Tab to amend").
DIALOG_RE = re.compile(r"Enter to (?:select|confirm)|Esc to cancel|Do you want to proceed\?")
_RULE_RE = re.compile(r"^\s*[─━═▔▁-]{10,}\s*$")
_WORKING_RE = re.compile(r"esc to interrupt", re.IGNORECASE)
_STATUS_LINE_RE = re.compile(
    r"ctx \d+%|⏵⏵|⏸|\? for shortcuts|bypass permissions|(?:manual|plan) mode on|for agents")
_CTX_RE = re.compile(r"ctx (\d+)%")
_MODEL_RE = re.compile(r"\b((?:Opus|Sonnet|Haiku|Fable) \d+(?:\.\d+)?)\b")
_OPTION_RE = re.compile(r"^\s*[❯>]?\s*(\d+)[.)]\s+(.*\S)\s*$")

_PANE_FMT = "\t".join([
    "#{session_name}:#{window_index}.#{pane_index}", "#{pane_id}", "#{pane_pid}",
    "#{pane_current_command}", "#{pane_current_path}", "#{pane_dead}",
])


class SessionError(Exception):
    """Raised when a restart step cannot be completed safely."""


class _DialogVisible(Exception):
    pass


# ---------------------------------------------------------------------------
# Host abstraction
# ---------------------------------------------------------------------------

class LocalHost:
    """Runs tmux and reads /proc on this machine."""

    def run(self, argv: list[str]) -> tuple[int, str]:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, str(exc)
        return p.returncode, p.stdout

    def read_text(self, path: str) -> str | None:
        try:
            return Path(path).read_text(errors="replace")
        except OSError:
            return None

    def listdir(self, path: str) -> list[str]:
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []

    def kill(self, pid: int, sig: int = signal.SIGTERM) -> bool:
        try:
            os.kill(pid, sig)
            return True
        except OSError:
            return False

    def home(self) -> str:
        return str(Path.home())

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> float:
        return time.monotonic()


class DockerHost(LocalHost):
    """Same operations, run inside a running container via ``docker exec``."""

    def __init__(self, container: str):
        self.container = container

    def _in_container(self, argv: list[str]) -> tuple[int, str]:
        return LocalHost.run(self, ["docker", "exec", self.container, *argv])

    def run(self, argv: list[str]) -> tuple[int, str]:
        return self._in_container(argv)

    def read_text(self, path: str) -> str | None:
        rc, out = self._in_container(["cat", path])
        return out if rc == 0 else None

    def listdir(self, path: str) -> list[str]:
        rc, out = self._in_container(["ls", "-1", path])
        return sorted(out.split()) if rc == 0 else []

    def kill(self, pid: int, sig: int = signal.SIGTERM) -> bool:
        return self._in_container(["kill", f"-{int(sig)}", str(pid)])[0] == 0

    def home(self) -> str:
        rc, out = self._in_container(["sh", "-c", "echo $HOME"])
        return out.strip() if rc == 0 and out.strip() else "/root"


def make_host(container: str | None = None) -> LocalHost:
    return DockerHost(container) if container else LocalHost()


# ---------------------------------------------------------------------------
# Tmux wrapper
# ---------------------------------------------------------------------------

class Tmux:
    def __init__(self, host: LocalHost):
        self.host = host

    def _run(self, *args: str) -> str:
        rc, out = self.host.run(["tmux", *args])
        if rc != 0:
            raise SessionError(f"tmux {args[0]} failed (exit {rc})")
        return out

    @staticmethod
    def _parse(line: str) -> dict:
        keys = ["target", "pane_id", "pane_pid", "command", "path", "dead"]
        parts = line.split("\t")
        d = dict(zip(keys, parts + [""] * (len(keys) - len(parts))))
        d["pane_pid"] = int(d["pane_pid"]) if d["pane_pid"].isdigit() else 0
        d["dead"] = d["dead"] == "1"
        return d

    def list_panes(self) -> list[dict]:
        try:
            out = self._run("list-panes", "-a", "-F", _PANE_FMT)
        except SessionError:
            return []
        return [self._parse(line) for line in out.splitlines() if line.strip()]

    def pane(self, target: str) -> dict:
        return self._parse(self._run("display-message", "-p", "-t", target, _PANE_FMT).strip())

    def capture(self, target: str) -> str:
        return self._run("capture-pane", "-p", "-t", target)

    def send_literal(self, target: str, text: str) -> None:
        self._run("send-keys", "-t", target, "-l", text)

    def send_keys(self, target: str, *keys: str) -> None:
        """Send named keys in ONE send-keys call (e.g. ``C-c C-c``)."""
        self._run("send-keys", "-t", target, *keys)

    def set_remain_on_exit(self, target: str, on: bool) -> None:
        if on:
            self._run("set-option", "-p", "-t", target, "remain-on-exit", "on")
        else:
            self._run("set-option", "-p", "-u", "-t", target, "remain-on-exit")

    def respawn(self, target: str, cwd: str, command: str) -> None:
        self._run("respawn-pane", "-t", target, "-c", cwd, command)

    def run_shell_background(self, command: str) -> None:
        self._run("run-shell", "-b", command)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def proc_argv(host: LocalHost, pid: int) -> list[str] | None:
    raw = host.read_text(f"/proc/{pid}/cmdline")
    if not raw:
        return None
    return [a for a in raw.split("\0") if a]


def proc_ppid(host: LocalHost, pid: int) -> int | None:
    for line in (host.read_text(f"/proc/{pid}/status") or "").splitlines():
        if line.startswith("PPid:"):
            value = line.split(":", 1)[1].strip()
            return int(value) if value.isdigit() else None
    return None


def proc_children(host: LocalHost, pid: int) -> list[int]:
    raw = host.read_text(f"/proc/{pid}/task/{pid}/children") or ""
    return [int(x) for x in raw.split() if x.isdigit()]


def is_claude(argv: list[str] | None) -> bool:
    if not argv:
        return False
    base = os.path.basename(argv[0])
    # Background workers rewrite their title: argv[0] is "claude bg-spare".
    if base in ("claude", "claude.exe") or base.split(" ", 1)[0] in ("claude", "claude.exe"):
        return True
    return base == "node" and len(argv) > 1 and "claude-code" in argv[1]


def _claude_args(argv: list[str]) -> list[str]:
    """Arguments after the executable (and after the script for node launches)."""
    return argv[2:] if os.path.basename(argv[0]) == "node" else argv[1:]


def is_attach(argv: list[str] | None) -> bool:
    return is_claude(argv) and _claude_args(argv)[:1] == ["attach"]


def load_registry(host: LocalHost, sessions_dir: str | None = None) -> dict[int, dict]:
    """Read ``~/.claude/sessions/<pid>.json`` (metadata only; other files are ignored)."""
    d = sessions_dir or f"{host.home()}/.claude/sessions"
    reg: dict[int, dict] = {}
    for name in host.listdir(d):
        if not name.endswith(".json") or not name[:-5].isdigit():
            continue
        try:
            data = json.loads(host.read_text(f"{d}/{name}") or "")
        except ValueError:
            continue
        if isinstance(data, dict):
            reg[int(name[:-5])] = data
    return reg


def _flag_value(args: list[str], flag: str) -> str | None:
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


#: A dialog replaces the prompt at the bottom of the pane. The marker text can
#: also appear in the transcript above (e.g. quoted by the agent); that is not a dialog.
DIALOG_BOTTOM_LINES = 12


def _bottom_lines(screen: str) -> list[str]:
    lines = [line for line in screen.splitlines() if line.strip()]
    return lines[-DIALOG_BOTTOM_LINES:]


def has_dialog(screen: str) -> bool:
    return any(DIALOG_RE.search(line) for line in _bottom_lines(screen))


def dialog_region(screen: str) -> str:
    """The dialog itself: the bottom lines, cut at the last horizontal rule.

    The transcript above a dialog can contain numbered lists and words like
    "worktree"; those must never influence which answer is chosen.
    """
    lines = _bottom_lines(screen)
    for i in range(len(lines) - 1, -1, -1):
        if _RULE_RE.match(lines[i]):
            lines = lines[i + 1:]
            break
    return "\n".join(lines)


def pane_state(screen: str, registry_status: str | None) -> str:
    """Return ``dialog``, ``working`` or ``idle``."""
    # The registry reports "waiting" while a permission prompt is open.
    if has_dialog(screen) or registry_status == "waiting":
        return "dialog"
    if registry_status == "busy" or _WORKING_RE.search(screen):
        return "working"
    return "idle"


@dataclass
class SessionInfo:
    target: str
    pane_id: str
    pane_pid: int
    mode: str                       # "shell" | "direct"
    claude_pid: int                 # Claude process shown in the pane
    argv: list[str] = field(default_factory=list)
    attached: bool = False          # pane runs `claude attach <job>`
    session_pid: int | None = None  # registry pid (the background process if attached)
    session_id: str | None = None
    name: str | None = None
    cwd: str | None = None
    kind: str | None = None
    model: str | None = None
    context_pct: int | None = None
    state: str = "idle"

    def to_dict(self) -> dict:
        return asdict(self)


def find_claude_in_pane(host: LocalHost, pane: dict) -> tuple[str, int, list[str]] | None:
    """Return (mode, pid, argv) of the Claude process in a pane, if any."""
    pid = pane["pane_pid"]
    argv = proc_argv(host, pid)
    if is_claude(argv):
        return "direct", pid, argv
    for child in proc_children(host, pid):
        cargv = proc_argv(host, child)
        if is_claude(cargv):
            return "shell", child, cargv
    return None


def inspect_pane(host: LocalHost, tmux: Tmux, pane: dict,
                 registry: dict[int, dict]) -> SessionInfo | None:
    found = find_claude_in_pane(host, pane)
    if not found:
        return None
    mode, pid, argv = found
    info = SessionInfo(target=pane["target"], pane_id=pane["pane_id"],
                       pane_pid=pane["pane_pid"], mode=mode, claude_pid=pid, argv=argv)
    entry = None
    if is_attach(argv):
        info.attached = True
        job = (_claude_args(argv)[1:2] or [""])[0]
        for rpid, data in registry.items():
            if job and (data.get("jobId") == job or str(data.get("sessionId", "")).startswith(job)):
                info.session_pid, entry = rpid, data
                break
    elif pid in registry:
        info.session_pid, entry = pid, registry[pid]
    if entry:
        info.session_id = entry.get("sessionId")
        info.name = entry.get("name")
        info.cwd = entry.get("cwd")
        info.kind = entry.get("kind")
    try:
        screen = tmux.capture(pane["target"])
    except SessionError:
        screen = ""
    info.state = pane_state(screen, (entry or {}).get("status"))
    m = _CTX_RE.search(screen)
    info.context_pct = int(m.group(1)) if m else None
    m = _MODEL_RE.search(screen)
    info.model = _flag_value(_claude_args(argv), "--model") or (m.group(1) if m else None)
    return info


def list_sessions(host: LocalHost, tmux: Tmux | None = None,
                  sessions_dir: str | None = None) -> list[SessionInfo]:
    tmux = tmux or Tmux(host)
    registry = load_registry(host, sessions_dir)
    out = []
    for pane in tmux.list_panes():
        if pane["dead"]:
            continue
        info = inspect_pane(host, tmux, pane, registry)
        if info:
            out.append(info)
    return out


def own_pane(host: LocalHost, tmux: Tmux | None = None, pid: int | None = None,
             sessions_dir: str | None = None) -> str | None:
    """Pane id of the Claude session this process runs under, if any.

    Walks up the process tree to the Claude process. This also works for
    background sessions: they run under a daemon, have no ``$TMUX_PANE`` and
    are shown in a pane by ``claude attach``.
    """
    ancestors: set[int] = set()
    current = pid or os.getpid()
    for _ in range(64):
        parent = proc_ppid(host, current)
        if not parent or parent <= 1 or parent in ancestors:
            break
        ancestors.add(parent)
        current = parent
    if not ancestors:
        return None
    for s in list_sessions(host, tmux, sessions_dir):
        if s.claude_pid in ancestors or (s.session_pid or -1) in ancestors:
            return s.pane_id
    return None


# ---------------------------------------------------------------------------
# Model aliases and relaunch argv
# ---------------------------------------------------------------------------

def resolve_model(model: str | None,
                  aliases: dict[str, str] | None = None) -> tuple[str | None, str | None]:
    """Expand a short alias to a full model id. Returns (model, warning)."""
    if not model:
        return model, None
    table = {**DEFAULT_MODEL_ALIASES, **(aliases or {})}
    if model.lower() in table:
        return table[model.lower()], None
    if not model.startswith("claude-"):
        return model, (f"model '{model}' is not a full model id and may resolve "
                       "to a different model under another profile")
    return model, None


def build_relaunch_argv(argv: list[str], session_id: str, model: str | None = None,
                        drop_worktree: bool = False) -> list[str]:
    """Original argv minus conversation flags, plus ``--resume <sessionId>``.

    ``--model`` is replaced when ``model`` is given. ``--worktree`` is dropped
    when relaunching inside the worktree directory itself (direct panes).
    """
    head = argv[:2] if os.path.basename(argv[0]) == "node" else argv[:1]
    args = _claude_args(argv)
    drop = dict(_SESSION_FLAGS)
    if model:
        drop["--model"] = True
    if drop_worktree:
        drop["--worktree"] = True
        drop["-w"] = True
    kept: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        flag = a.split("=", 1)[0]
        if flag in drop:
            takes_value = drop[flag] and "=" not in a
            if flag in ("--worktree", "-w") and (i + 1 >= len(args) or args[i + 1].startswith("-")):
                takes_value = False  # --worktree may be given without a name
            i += 2 if takes_value else 1
            continue
        kept.append(a)
        i += 1
    if model:
        kept += ["--model", model]
    return head + kept + ["--resume", session_id]


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

def parse_options(screen: str) -> dict[int, str]:
    """Numbered options of a dialog, e.g. ``{1: 'Keep worktree', 2: 'Remove worktree'}``."""
    opts: dict[int, str] = {}
    for line in screen.splitlines():
        m = _OPTION_RE.match(line)
        if m:
            opts[int(m.group(1))] = m.group(2)
    return opts


def choose_dialog_answer(screen: str) -> int:
    """Pick the safe answer for an exit dialog, or raise SessionError.

    - worktree dialog: the option labelled Keep - never one labelled Remove.
    - "Exit and stop tasks": option 1.
    Unknown dialogs are never answered.
    """
    region = dialog_region(screen)
    opts = parse_options(region)
    lower = region.lower()
    if "worktree" in lower:
        keep = [n for n, label in sorted(opts.items())
                if "keep" in label.lower() and "remove" not in label.lower()]
        if not keep:
            raise SessionError("worktree dialog without a Keep option; refusing to answer")
        return keep[0]
    if "exit and stop tasks" in lower:
        label = opts.get(1, "").lower()
        if "remove" in label or "delete" in label:
            raise SessionError("unexpected option 1 in exit dialog; refusing to answer")
        return 1
    raise SessionError("unknown dialog on screen; refusing to answer")


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

@dataclass
class RestartResult:
    target: str
    status: str = "FAILED"   # OK | SKIPPED | FAILED
    message: str = ""
    session_id: str | None = None
    old_pid: int | None = None
    new_pid: int | None = None
    warnings: list[str] = field(default_factory=list)

    def line(self) -> str:
        return f"{self.status:<8} {self.target}  {self.message}".rstrip()

    def to_dict(self) -> dict:
        return asdict(self)


class Restarter:
    """Restart one session safely. Every wait is polled with a timeout."""

    def __init__(self, host: LocalHost, tmux: Tmux | None = None, *,
                 sessions_dir: str | None = None, timeout: float = 300.0,
                 poll: float = 1.0, log=None):
        self.host = host
        self.tmux = tmux or Tmux(host)
        self.sessions_dir = sessions_dir
        self.timeout = timeout
        self.poll = poll
        self.log = log or (lambda msg: None)

    def _wait(self, predicate, what: str, timeout: float | None = None):
        deadline = self.host.now() + (self.timeout if timeout is None else timeout)
        while True:
            value = predicate()
            if value:
                return value
            if self.host.now() >= deadline:
                raise SessionError(f"timed out waiting for {what}")
            self.host.sleep(self.poll)

    def _alive(self, pid: int) -> bool:
        return is_claude(proc_argv(self.host, pid))

    def _registry(self) -> dict[int, dict]:
        return load_registry(self.host, self.sessions_dir)

    def inspect(self, target: str) -> SessionInfo:
        info = inspect_pane(self.host, self.tmux, self.tmux.pane(target), self._registry())
        if not info:
            raise SessionError("no Claude Code process in this pane")
        if not info.session_id:
            raise SessionError("session not found in the session registry")
        return info

    def _wait_idle(self, info: SessionInfo) -> None:
        def idle():
            status = self._registry().get(info.session_pid or -1, {}).get("status")
            state = pane_state(self.tmux.capture(info.target), status)
            if state == "dialog":
                raise _DialogVisible()
            return state == "idle"
        self._wait(idle, "the session to become idle")

    def _exited(self, info: SessionInfo) -> bool:
        if self._alive(info.claude_pid):
            return False
        pane = self.tmux.pane(info.target)
        return pane["dead"] if info.mode == "direct" else pane["command"] in SHELLS

    def _exit_interactive(self, info: SessionInfo) -> None:
        # Text and Enter are sent in separate send-keys calls.
        self.tmux.send_literal(info.target, "/exit")
        self.tmux.send_keys(info.target, "Enter")
        seen: dict[str, int] = {}

        def exited():
            if self._exited(info):
                return True
            screen = self.tmux.capture(info.target)
            if has_dialog(screen):
                choice = choose_dialog_answer(screen)
                key = screen.strip()
                seen[key] = seen.get(key, 0) + 1
                if seen[key] == 1:
                    self.log(f"{info.target}: answering exit dialog with option {choice}")
                    self.tmux.send_literal(info.target, str(choice))
                elif seen[key] > 5:
                    raise SessionError("exit dialog did not accept the answer")
            return False
        self._wait(exited, "Claude Code to exit")

    def _respawn_attached(self, info: SessionInfo) -> int:
        """Restart a background session with ``claude respawn <job>``.

        Killing the background process does not work: the daemon starts it
        again at once and the attach view stays open. ``claude respawn``
        resumes the same conversation; the ``claude attach`` client in the pane
        may exit, so remain-on-exit is set and the pane is re-attached.
        """
        job = (_claude_args(info.argv)[1:2] or [""])[0]
        if not job:
            raise SessionError("cannot tell the background job id from `claude attach`")
        self.tmux.set_remain_on_exit(info.target, True)
        rc, _ = self.host.run([info.argv[0], "respawn", job])
        if rc != 0:
            raise SessionError(f"claude respawn {job} failed (exit {rc})")

        def respawned():
            for rpid, data in self._registry().items():
                if (data.get("sessionId") == info.session_id and rpid != info.session_pid
                        and self._alive(rpid)):
                    return rpid
            return None
        new_pid = self._wait(respawned, "the background session to come back")
        if self.tmux.pane(info.target)["dead"]:
            self.tmux.respawn(info.target, info.cwd or self.host.home(), shlex.join(info.argv))
        self.tmux.set_remain_on_exit(info.target, False)
        self._wait(lambda: _STATUS_LINE_RE.search(self.tmux.capture(info.target)),
                   "the re-attached session (status line)")
        return new_pid

    def _relaunch(self, info: SessionInfo, argv: list[str]) -> None:
        command = shlex.join(argv)
        if info.mode == "direct":
            self.tmux.respawn(info.target, info.cwd or self.host.home(), command)
            self.tmux.set_remain_on_exit(info.target, False)
        else:
            self.tmux.send_literal(info.target, command)
            self.tmux.send_keys(info.target, "Enter")

    def _verify(self, info: SessionInfo) -> int:
        def resumed():
            found = find_claude_in_pane(self.host, self.tmux.pane(info.target))
            if not found or found[1] == info.claude_pid:
                return None
            if self._registry().get(found[1], {}).get("sessionId") != info.session_id:
                return None
            if not _STATUS_LINE_RE.search(self.tmux.capture(info.target)):
                return None
            return found[1]
        return self._wait(resumed, "the resumed session (new pid, same sessionId, status line)")

    def _nudge(self, info: SessionInfo, text: str) -> None:
        def ready():
            screen = self.tmux.capture(info.target)
            return not has_dialog(screen) and _STATUS_LINE_RE.search(screen)
        self._wait(ready, "the resumed session to accept the nudge")
        # Text and Enter in separate calls.
        self.tmux.send_literal(info.target, text)
        self.tmux.send_keys(info.target, "Enter")

    def restart(self, target: str, *, model: str | None = None,
                aliases: dict[str, str] | None = None,
                nudge: str | None = DEFAULT_NUDGE, force: bool = False) -> RestartResult:
        result = RestartResult(target=target)
        info = None
        try:
            info = self.inspect(target)
            result.session_id = info.session_id
            result.old_pid = info.session_pid if info.attached else info.claude_pid
            if info.attached and model:
                raise SessionError("--model is not supported for background sessions "
                                   "(claude respawn keeps the session's settings)")
            original = None if info.attached else _flag_value(_claude_args(info.argv), "--model")
            model, warning = resolve_model(model or original, aliases)
            if warning:
                result.warnings.append(warning)
            if info.state == "dialog":
                # Pasting into a dialog would select an answer. Never do that.
                result.status, result.message = "SKIPPED", "dialog open (Enter to select/confirm)"
                return result
            if force:
                self.tmux.send_keys(info.target, "Escape")
            else:
                self._wait_idle(info)
            if info.attached:
                result.new_pid = self._respawn_attached(info)
            else:
                argv = build_relaunch_argv(info.argv, info.session_id, model,
                                           drop_worktree=info.mode == "direct")
                if info.mode == "direct":
                    self.tmux.set_remain_on_exit(info.target, True)
                self._exit_interactive(info)
                self._relaunch(info, argv)
                result.new_pid = self._verify(info)
            if nudge:
                self._nudge(info, nudge)
            result.status = "OK"
            result.message = f"pid {result.old_pid} -> {result.new_pid}, session {info.session_id}"
        except _DialogVisible:
            result.status, result.message = "SKIPPED", "dialog open (Enter to select/confirm)"
        except SessionError as exc:
            result.status, result.message = "FAILED", str(exc)
            self._restore_pane(info)
        return result

    def _restore_pane(self, info: SessionInfo | None) -> None:
        """After a failure, undo remain-on-exit on a pane that is still alive."""
        if not info or info.mode != "direct":
            return
        try:
            if not self.tmux.pane(info.target)["dead"]:
                self.tmux.set_remain_on_exit(info.target, False)
        except SessionError:
            pass


def self_restart_command(python: str, target: str, *, delay: float, extra: list[str]) -> str:
    """Shell command run by the detached helper for ``restart --self``.

    The helper runs from the tmux server's directory, so the package location
    is put on PYTHONPATH: the helper runs the same claude_mux as the caller.
    """
    package_parent = str(Path(__file__).resolve().parent.parent)
    inner = shlex.join(["env", f"PYTHONPATH={package_parent}", python, "-m", "claude_mux",
                        "session", "restart", target, *extra])
    return f"sleep {float(delay):g}; {inner}"
