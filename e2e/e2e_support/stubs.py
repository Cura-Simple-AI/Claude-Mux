"""Stand-ins for the external programs claude-mux shells out to.

They are written to a bin directory that is put first on PATH, so claude-mux
finds them exactly like it finds the real tools. Nothing inside claude-mux is
patched.

* ``pm2``       - a small process manager that really starts, lists, stops and
                  restarts processes, keeping its state in ``~/.pm2/stub-state.json``.
                  Every invocation is appended to ``~/.pm2/stub-calls.jsonl``.
* ``heimsense`` - the Go proxy binary used for bearer providers. The stand-in
                  requires the ``run`` subcommand and serves the provider through
                  claude-mux's own pass-through proxy (``python -m claude_mux.proxy``).
* ``claude``    - a Claude Code stand-in (fake_claude.py) behind a node entry point at
                  ``.../@anthropic-ai/claude-code/cli.js``, so its processes look like
                  an npm-installed Claude Code. ``claude auth status`` behaves like a
                  logged-out install, so active-provider detection relies on
                  ``~/.claude/settings.json`` (a real ``claude`` on PATH is shadowed).
* ``gh``        - behaves like a GitHub CLI without a login.
"""
from __future__ import annotations

import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

PM2 = r'''#!{python}
import json, os, signal, subprocess, sys, time
from pathlib import Path

import psutil

home = Path(os.environ["HOME"]) / ".pm2"
home.mkdir(parents=True, exist_ok=True)
state_file = home / "stub-state.json"
with open(home / "stub-calls.jsonl", "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")


def load():
    try:
        return json.loads(state_file.read_text())
    except (OSError, ValueError):
        return {{}}


def save(state):
    state_file.write_text(json.dumps(state, indent=1))


def alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def kill(pid):
    if not alive(pid):
        return
    try:
        os.killpg(pid, signal.SIGINT)  # the proxy shuts down cleanly on SIGINT
    except OSError:
        return
    deadline = time.time() + 5
    while time.time() < deadline and alive(pid):
        time.sleep(0.05)
    if alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass


def spawn(app):
    out = open(app["out"], "a")
    err = open(app["err"], "a")
    proc = subprocess.Popen(app["argv"], stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                            start_new_session=True)
    app["pid"] = proc.pid
    app["started_ms"] = int(time.time() * 1000)
    app["restarts"] = app.get("restarts", -1) + 1


args = sys.argv[1:]
cmd = args[0] if args else ""
state = load()

if cmd == "start":
    # pm2 start <script> --name N --output O --error E -- <script args...>
    script = args[1]
    opts, rest = args[2:], []
    if "--" in opts:
        i = opts.index("--")
        opts, rest = opts[:i], opts[i + 1:]
    o = dict(zip(opts[0::2], opts[1::2]))
    name = o["--name"]
    if name in state and alive(state[name]["pid"]):
        print(f"[PM2][ERROR] Script already launched, add -f option to force re-execution", file=sys.stderr)
        sys.exit(1)
    app = {{"name": name, "argv": [script] + rest, "out": o["--output"], "err": o["--error"],
           "pm_id": max([a["pm_id"] for a in state.values()] + [-1]) + 1}}
    spawn(app)
    state[name] = app
    save(state)
    print(f"[PM2] Starting {{script}} in fork_mode (1 instance)\n[PM2] Done.")
elif cmd in ("stop", "delete"):
    name = args[1]
    if name not in state:
        print(f"[PM2][ERROR] Process or Namespace {{name}} not found", file=sys.stderr)
        sys.exit(1)
    kill(state[name]["pid"])
    if cmd == "delete":
        del state[name]
    save(state)
elif cmd == "restart":
    name = args[1]
    if name not in state:
        print(f"[PM2][ERROR] Process or Namespace {{name}} not found", file=sys.stderr)
        sys.exit(1)
    kill(state[name]["pid"])
    spawn(state[name])
    save(state)
elif cmd == "jlist":
    out = []
    for name, app in state.items():
        up = alive(app["pid"])
        out.append({{"name": name, "pid": app["pid"] if up else 0,
                    "pm2_env": {{"status": "online" if up else "stopped", "pm_id": app["pm_id"],
                                "pm_uptime": app["started_ms"], "restart_time": app["restarts"]}},
                    "monit": {{"memory": 1024, "cpu": 0}}}})
    print(json.dumps(out))
elif cmd == "logs":
    name = args[1]
    lines = int(args[args.index("--lines") + 1]) if "--lines" in args else 15
    app = state.get(name)
    if not app:
        print(f"[TAILING] Tailing last {{lines}} lines for [{{name}}] process (change the value with --lines option)")
        sys.exit(0)
    for label in ("out", "err"):
        p = Path(app[label])
        print(f"{{p}} last {{lines}} lines:")
        if p.exists():
            for line in p.read_text().splitlines()[-lines:]:
                print(f"0|{{name}}  | {{line}}")
elif cmd == "save":
    print("[PM2] Saving current process list...")
else:
    print(f"pm2 stub: unsupported command {{args}}", file=sys.stderr)
    sys.exit(1)
'''

HEIMSENSE = r'''#!/bin/sh
# Stand-in for the heimsense proxy binary. claude-mux must launch it with `run`.
if [ "$1" != "run" ]; then
  echo "heimsense stub: expected the 'run' subcommand, got: $*" >&2
  exit 2
fi
PROXY_TARGET_URL="${{ANTHROPIC_BASE_URL%/v1}}" PROXY_AUTH_TOKEN="$ANTHROPIC_API_KEY" \
  exec "{python}" -m claude_mux.proxy
'''

CLAUDE = r'''#!/bin/sh
# Claude Code stand-in (see fake_claude.py). `auth status` short-circuits: it is
# called by claude-mux on every active-provider lookup.
if [ "$1" = "auth" ] && [ "$2" = "status" ]; then
  echo "Not logged in. Run claude to log in." >&2
  exit 1
fi
exec node "{cli_js}" "$@"
'''

CLI_JS = r'''// Stand-in for Claude Code's npm entry point: hands the terminal to fake_claude.py.
// Signals are forwarded so that stopping this process stops the session, like the real one.
const {{ spawn }} = require("child_process");
const child = spawn({python}, [{fake}, ...process.argv.slice(2)], {{
  stdio: "inherit",
  env: {{ ...process.env, FAKE_CLAUDE_PID: String(process.pid), FAKE_CLAUDE_CLI_JS: __filename }},
}});
for (const sig of ["SIGTERM", "SIGINT", "SIGHUP"]) process.on(sig, () => child.kill(sig));
child.on("exit", (code) => process.exit(code === null ? 1 : code));
'''

GH = r'''#!/bin/sh
echo "You are not logged into any GitHub hosts. To log in, run: gh auth login" >&2
exit 1
'''


def _write(path: Path, text: str):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install(bin_dir: Path, python: str = sys.executable) -> dict[str, Path]:
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli_js = bin_dir.parent / "npm" / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    cli_js.parent.mkdir(parents=True, exist_ok=True)
    fake = Path(__file__).with_name("fake_claude.py")
    cli_js.write_text(CLI_JS.format(python=json.dumps(python), fake=json.dumps(str(fake))))
    paths = {"cli.js": cli_js}
    for name, src in (("pm2", PM2), ("heimsense", HEIMSENSE), ("claude", CLAUDE), ("gh", GH)):
        p = bin_dir / name
        _write(p, src.format(python=python, cli_js=cli_js) if name != "gh" else src)
        paths[name] = p
    return paths


# -- helpers used by the test fixtures ---------------------------------------

def pm2_state(home: Path) -> dict:
    try:
        return json.loads((home / ".pm2" / "stub-state.json").read_text())
    except (OSError, ValueError):
        return {}


def pm2_calls(home: Path) -> list[list[str]]:
    try:
        return [json.loads(l) for l in (home / ".pm2" / "stub-calls.jsonl").read_text().splitlines()]
    except OSError:
        return []


def kill_pm2_processes(home: Path):
    """Stop everything the pm2 stand-in started (test teardown)."""
    import psutil
    for app in pm2_state(home).values():
        pid = app.get("pid")
        try:
            os.killpg(pid, signal.SIGINT)
        except (OSError, TypeError):
            continue
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.Error:
                break
            time.sleep(0.05)
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
