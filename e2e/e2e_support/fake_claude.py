"""Stand-in for Claude Code, for E2E tests of provider switching and `session` commands.

It is started through a node entry point at ``.../@anthropic-ai/claude-code/cli.js``
(see stubs.py), so the process looks exactly like an npm-installed Claude Code to
claude-mux: ``node .../claude-code/cli.js ARGS``. The entry point hands the terminal
to this script and passes its own pid in FAKE_CLAUDE_PID.

Behaviour model — what the tests assume about the real Claude Code. It comes from
the claude-mux author's observations and is NOT verified against Claude Code here:

* ``env.ANTHROPIC_BASE_URL`` in ~/.claude/settings.json is re-read on every request
  (a running session follows a URL switch on its next request).
* Credentials are read once at startup: ``CLAUDE_CODE_OAUTH_TOKEN`` (observed) and
  ``ANTHROPIC_AUTH_TOKEN`` / ``ANTHROPIC_API_KEY`` (assumed — unknown).
* Screens (prompt box, status line, exit dialogs, permission prompt) copy the
  Claude Code 2.1 captures used in tests/test_session.py.

Modes:
    claude [options] [prompt]     interactive session in the terminal
    claude --bg [options]         start a background session; prints its job id
    claude attach JOB             attach view of a background session (exits when the
                                  background process is replaced, like the real client)
    claude respawn JOB            restart a background session, same conversation
    claude auth status --text     behaves like a logged-out install (exit 1)

Typed commands (followed by Enter): ``/exit``, ``/busy SECONDS``, ``/permission``,
``/task`` (starts a background task, so /exit asks "Exit and stop tasks"),
``!COMMAND`` (runs a shell command as a child process, like the Bash tool).
Anything else is sent to the model.

Everything observable is appended to ~/.fake-claude/events.jsonl.
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import termios
import time
import tty
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HOME = Path.home()
SESSIONS = HOME / ".claude" / "sessions"
STATE = HOME / ".fake-claude"
CLI_JS = os.environ.get("FAKE_CLAUDE_CLI_JS", "")
PID = int(os.environ.get("FAKE_CLAUDE_PID") or os.getpid())
WIDTH = 100
BOX = "─" * WIDTH
RULE = "▔" * 60
STATUS_HINT = "  ? for shortcuts"

KEEP_DIALOG = [
    "   Exiting worktree session",
    "   You have 1 uncommitted file. These will be lost if you remove the worktree.",
    "   ❯ 1. Keep worktree    Stays at {cwd}",
    "     2. Remove worktree  All changes and commits will be lost.",
    "   Enter to confirm · Esc to cancel",
]
STOP_TASKS_DIALOG = [
    "   Background work is running",
    "   The following will stop when you exit:",
    "   shell · sleep 600",
    "   ❯ 1. Exit and stop tasks",
    "     2. Move to background and exit",
    "     3. Stay",
    "   Enter to confirm · Esc to cancel",
]
PERMISSION_DIALOG = [
    " Bash command",
    "   │ rm -rf build/",
    " This command requires approval",
    " Do you want to proceed?",
    " ❯ 1. Yes",
    "   2. Yes, and allow access to /work",
    "   3. No",
    " Esc to cancel · Tab to amend",
]


# --- helpers ---------------------------------------------------------------------

def event(kind: str, **data) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "events.jsonl", "a") as f:
        f.write(json.dumps({"ts": time.time(), "pid": PID, "kind": kind, **data}) + "\n")


def settings_env() -> dict:
    try:
        return json.loads((HOME / ".claude" / "settings.json").read_text()).get("env", {}) or {}
    except (OSError, ValueError):
        return {}


def flag_value(args: list[str], flag: str) -> str | None:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def model_label(model: str) -> str:
    for family in ("opus", "sonnet", "haiku"):
        if f"-{family}-" in f"-{model}-":
            nums = [p for p in model.split("-") if p.isdigit() and len(p) <= 2][:2]
            return f"{family.capitalize()} {'.'.join(nums) or '4'}"
    return model


def registry_path(pid: int) -> Path:
    return SESSIONS / f"{pid}.json"


def write_registry(**data) -> None:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    tmp = registry_path(PID).with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": PID, **data}))
    tmp.replace(registry_path(PID))


def read_registry() -> dict[int, dict]:
    out = {}
    for p in SESSIONS.glob("*.json"):
        try:
            out[int(p.stem)] = json.loads(p.read_text())
        except (ValueError, OSError):
            pass
    return out


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0] != "Z"
    except OSError:
        return False


# --- the conversation (model requests) ----------------------------------------------

class Conversation:
    def __init__(self, session_id: str, model: str):
        self.session_id = session_id
        self.model = model
        self.file = STATE / "transcripts" / f"{session_id}.jsonl"
        self.messages: list[dict] = []
        if self.file.exists():
            self.messages = [json.loads(l) for l in self.file.read_text().splitlines() if l.strip()]
        # Credentials: read once, at startup (see module docstring).
        env = {**os.environ, **settings_env()}
        self.credentials: dict[str, str] = {}
        if env.get("ANTHROPIC_AUTH_TOKEN"):
            self.credentials["Authorization"] = f"Bearer {env['ANTHROPIC_AUTH_TOKEN']}"
        elif env.get("ANTHROPIC_API_KEY"):
            self.credentials["x-api-key"] = env["ANTHROPIC_API_KEY"]
        elif env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            self.credentials["Authorization"] = f"Bearer {env['CLAUDE_CODE_OAUTH_TOKEN']}"
            self.credentials["anthropic-beta"] = "oauth-2025-04-20"

    def _remember(self, message: dict) -> None:
        self.messages.append(message)
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file, "a") as f:
            f.write(json.dumps(message) + "\n")

    def send(self, text: str) -> str:
        # The base URL is looked up per request (see module docstring).
        base = (settings_env().get("ANTHROPIC_BASE_URL")
                or os.environ.get("FAKE_CLAUDE_ANTHROPIC_URL", "https://api.anthropic.com"))
        url = base.rstrip("/") + "/v1/messages"
        body = {"model": self.model, "max_tokens": 64,
                "messages": self.messages + [{"role": "user", "content": text}]}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Content-Type": "application/json", "anthropic-version": "2023-06-01", **self.credentials})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            event("request", session=self.session_id, text=text, url=url, status=0, error=str(e))
            return f"API Error: {e}"
        reply = ""
        if status == 200:
            try:
                reply = json.loads(raw)["content"][0]["text"]
            except (ValueError, KeyError, IndexError):
                reply = raw.decode(errors="replace")[:80]
            self._remember({"role": "user", "content": text})
            self._remember({"role": "assistant", "content": reply})
        event("request", session=self.session_id, text=text, url=url, status=status, reply=reply)
        return reply if status == 200 else f"API Error: {status} {raw.decode(errors='replace')[:80]}"


# --- interactive terminal session ------------------------------------------------------

class Terminal:
    """Raw-mode keyboard + full redraws (enough for tmux capture-pane)."""

    def __enter__(self):
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        return self

    def __exit__(self, *exc):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        sys.stdout.write("\r\n")
        sys.stdout.flush()

    def draw(self, lines: list[str]) -> None:
        sys.stdout.write("\x1b[2J\x1b[H" + "\r\n".join(lines))
        sys.stdout.flush()

    def keys(self, timeout: float = 0.1) -> list[str]:
        """Pending keys: printable characters, 'enter', 'backspace', 'ctrl-u', 'escape'."""
        if not select.select([self.fd], [], [], timeout)[0]:
            return []
        data = os.read(self.fd, 4096).decode(errors="ignore")
        out, i = [], 0
        while i < len(data):
            ch = data[i]
            if ch == "\x1b":
                if data[i + 1:i + 2] in ("[", "O"):      # arrow keys etc.: ignored
                    i += 3
                    continue
                out.append("escape")
            elif ch in "\r\n":
                out.append("enter")
            elif ch in "\x7f\x08":
                out.append("backspace")
            elif ch == "\x15":
                out.append("ctrl-u")
            elif ch.isprintable():
                out.append(ch)
            i += 1
        return out


def prompt_box(buf: str, status: str) -> list[str]:
    text = "❯ " + buf
    body = [text[i:i + WIDTH - 2] for i in range(0, max(len(text), 1), WIDTH - 2)] or ["❯ "]
    return [BOX, *body, BOX, status, STATUS_HINT]


class Interactive:
    def __init__(self, args: list[str]):
        self.args = args
        self.session_id = flag_value(args, "--resume") or str(uuid.uuid4())
        self.resumed = "--resume" in args
        self.model = (flag_value(args, "--model") or settings_env().get("ANTHROPIC_MODEL")
                      or "claude-sonnet-4-6")
        self.name = flag_value(args, "--name") or f"session-{self.session_id[:4]}"
        self._enter_worktree()
        self.conv = Conversation(self.session_id, self.model)
        self.lines = [f"● resumed session {self.session_id} ({len(self.conv.messages)} messages)"
                      if self.resumed else "● Claude Code (stand-in) — ready"]
        for m in self.conv.messages[-6:]:
            self.lines.append(("> " if m["role"] == "user" else "● ") + m["content"][:WIDTH])
        self.buf = ""
        self.dialog: tuple[str, list[str]] | None = None
        self.busy_until = 0.0
        self.tasks = False
        self.running = True

    def _enter_worktree(self) -> None:
        if "--worktree" not in self.args and "-w" not in self.args:
            return
        flag = "--worktree" if "--worktree" in self.args else "-w"
        name = flag_value(self.args, flag)
        if not name or name.startswith("-"):
            name = f"wt-{uuid.uuid4().hex[:6]}"
        path = Path.cwd() / ".claude" / "worktrees" / name
        path.mkdir(parents=True, exist_ok=True)
        os.chdir(path)
        event("worktree-entered", path=str(path))

    def status(self) -> str:
        if self.dialog and self.dialog[0] == "permission":
            return "waiting"
        return "busy" if time.time() < self.busy_until else "idle"

    def register(self) -> None:
        write_registry(sessionId=self.session_id, cwd=os.getcwd(), name=self.name,
                       kind="interactive", status=self.status())

    def screen(self) -> list[str]:
        out = list(self.lines[-18:])
        if self.dialog:
            kind, dialog = self.dialog
            return out + [RULE, *[l.format(cwd=os.getcwd()) for l in dialog]]
        if time.time() < self.busy_until:
            out.append("* Brewing… (esc to interrupt)")
        return out + prompt_box(self.buf, f"  main · {model_label(self.model)} ctx 17% · 5h 10%")

    def send(self, text: str) -> None:
        self.lines.append(f"> {text}")
        self.busy_until = time.time() + 60
        self.register()
        reply = self.conv.send(text)
        self.busy_until = 0
        self.lines.append(f"● {reply}"[:WIDTH])

    def submit(self, text: str) -> None:
        event("submitted", session=self.session_id, text=text)
        if text == "/exit":
            if self.tasks:
                self.dialog = ("stop-tasks", STOP_TASKS_DIALOG)
            elif Path.cwd().parent.name == "worktrees":
                self.dialog = ("worktree", KEEP_DIALOG)
            else:
                self.running = False
        elif text.startswith("/busy"):
            self.busy_until = time.time() + float(text.split()[1])
        elif text == "/permission":
            self.dialog = ("permission", PERMISSION_DIALOG)
        elif text == "/task":
            self.tasks = True
            self.lines.append("● started background task: sleep 600")
        elif text.startswith("!"):
            self.lines.append(f"! {text[1:]}")
            r = subprocess.run(text[1:], shell=True, capture_output=True, text=True, timeout=120)
            for line in (r.stdout + r.stderr).strip().splitlines()[-4:]:
                self.lines.append(f"  ⎿ {line}"[:WIDTH])
            event("shell", session=self.session_id, command=text[1:], code=r.returncode,
                  output=r.stdout + r.stderr)
        elif text:
            self.send(text)

    def answer_dialog(self, key: str) -> None:
        kind, _ = self.dialog
        if key == "escape":
            self.dialog = None
            return
        event("dialog-answer", dialog=kind, answer=key)
        if kind == "worktree":
            event("worktree-kept" if key == "1" else "worktree-removed", path=os.getcwd())
            self.running = False
        elif kind == "stop-tasks":
            if key == "1":
                self.running = False
            self.dialog = None
        else:
            self.dialog = None

    def run(self) -> int:
        event("started", session=self.session_id, argv=self.args, cwd=os.getcwd(),
              credentials=sorted(self.conv.credentials), resumed=self.resumed)
        self.register()
        prompt = self._initial_prompt()
        with Terminal() as term:
            if prompt:
                term.draw(self.screen())
                self.send(prompt)
            while self.running:
                term.draw(self.screen())
                self.register()
                for key in term.keys():
                    if self.dialog:
                        if key in ("escape",) or key.isdigit():
                            self.answer_dialog(key)
                    elif key == "escape":
                        self.busy_until = 0
                    elif key == "enter":
                        text, self.buf = self.buf, ""
                        term.draw(self.screen())
                        self.submit(text)
                    elif key == "backspace":
                        self.buf = self.buf[:-1]
                    elif key == "ctrl-u":
                        self.buf = ""
                    elif len(key) == 1:
                        self.buf += key
                    if not self.running:
                        break
        registry_path(PID).unlink(missing_ok=True)
        event("exited", session=self.session_id)
        return 0

    def _initial_prompt(self) -> str | None:
        """A positional prompt on the command line is sent as the first message."""
        if self.resumed:
            return None  # claude-mux must drop it on resume; if it didn't, it would show here
        value_flags = {"--model", "--resume", "--name", "--permission-mode", "--worktree", "-w"}
        skip = False
        for a in self.args:
            if skip:
                skip = False
            elif a in value_flags:
                skip = True
            elif not a.startswith("-"):
                return a
        return None


# --- background sessions ---------------------------------------------------------------

def job_dir(job: str) -> Path:
    return STATE / "jobs" / job


def find_worker(job: str) -> tuple[int, dict] | tuple[None, None]:
    for pid, data in read_registry().items():
        if data.get("jobId") == job and alive(pid):
            return pid, data
    return None, None


def spawn_worker(job: str, args: list[str]) -> None:
    subprocess.Popen(["node", CLI_JS, "__worker", job, *args], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 15
    while find_worker(job)[0] is None:
        if time.time() > deadline:
            raise SystemExit(f"background session {job} did not start")
        time.sleep(0.05)


def worker(job: str, args: list[str]) -> int:
    session_id = flag_value(args, "--resume") or str(uuid.uuid4())
    model = flag_value(args, "--model") or "claude-sonnet-4-6"
    conv = Conversation(session_id, model)
    d = job_dir(job)
    d.mkdir(parents=True, exist_ok=True)
    (d / "args.json").write_text(json.dumps([a for i, a in enumerate(args)
                                             if a != "--resume" and args[i - 1:i] != ["--resume"]]))
    inbox = d / "inbox"
    offset = inbox.stat().st_size if inbox.exists() else 0
    lines = [f"● background session {session_id} ({len(conv.messages)} messages)"]
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.update(now=True))

    def publish(status: str):
        (d / "screen.json").write_text(json.dumps({"pid": PID, "lines": lines[-18:], "status": status,
                                                   "model": model}))
        write_registry(sessionId=session_id, jobId=job, cwd=os.getcwd(), name=f"bg-{job}",
                       kind="bg", status=status)

    event("started", session=session_id, job=job, argv=args, credentials=sorted(conv.credentials),
          resumed="--resume" in args, background=True)
    publish("idle")
    while not stop["now"]:
        if inbox.exists() and inbox.stat().st_size > offset:
            with open(inbox) as f:
                f.seek(offset)
                new = f.read()
                offset = f.tell()
            for text in [l for l in new.split("\n") if l.strip()]:
                event("submitted", session=session_id, text=text, background=True)
                lines.append(f"> {text}")
                publish("busy")
                lines.append(f"● {conv.send(text)}"[:WIDTH])
                publish("idle")
        time.sleep(0.05)
    registry_path(PID).unlink(missing_ok=True)
    event("exited", session=session_id, job=job, background=True)
    return 0


def start_background(args: list[str]) -> int:
    job = uuid.uuid4().hex[:8]
    spawn_worker(job, args)
    print(job)
    return 0


def respawn(job: str) -> int:
    pid, data = find_worker(job)
    if pid is None:
        print(f"no background session {job}", file=sys.stderr)
        return 1
    args = json.loads((job_dir(job) / "args.json").read_text())
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 10
    while alive(pid) and time.time() < deadline:
        time.sleep(0.05)
    spawn_worker(job, [*args, "--resume", data["sessionId"]])
    event("respawned", job=job, old_pid=pid)
    return 0


def attach(job: str) -> int:
    pid, _ = find_worker(job)
    if pid is None:
        print(f"no background session {job}", file=sys.stderr)
        return 1
    buf = ""
    event("attached", job=job, worker=pid)
    with Terminal() as term:
        while True:
            current, _ = find_worker(job)
            if current != pid:
                event("attach-client-exit", job=job, old_worker=pid)
                return 0  # the real client exits when the background process is replaced
            try:
                state = json.loads((job_dir(job) / "screen.json").read_text())
            except (OSError, ValueError):
                state = {"lines": [], "status": "idle", "model": ""}
            lines = list(state["lines"])
            if state["status"] == "busy":
                lines.append("* Brewing… (esc to interrupt)")
            term.draw(lines + prompt_box(buf, f"  bg {job} · {model_label(state['model'])} ctx 9%"))
            for key in term.keys():
                if key == "enter":
                    with open(job_dir(job) / "inbox", "a") as f:
                        f.write(buf + "\n")
                    buf = ""
                elif key == "backspace":
                    buf = buf[:-1]
                elif key == "ctrl-u":
                    buf = ""
                elif len(key) == 1:
                    buf += key


def main(argv: list[str]) -> int:
    if argv[:2] == ["auth", "status"]:
        print("Not logged in. Run claude to log in.", file=sys.stderr)
        return 1
    if argv[:1] == ["__worker"]:
        return worker(argv[1], argv[2:])
    if argv[:1] == ["attach"]:
        return attach(argv[1])
    if argv[:1] == ["respawn"]:
        return respawn(argv[1])
    if "--bg" in argv:
        return start_background([a for a in argv if a != "--bg"])
    return Interactive(argv).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
