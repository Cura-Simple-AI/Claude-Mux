"""End-to-end test harness for claude-mux.

The suite drives claude-mux only through its user-facing entry points:

* the ``claude-mux`` CLI, run as a real subprocess (``python -m claude_mux``),
* the local proxy, run as a real process (``python -m claude_mux.proxy``),
* the Textual TUI, driven with key presses and clicks through Textual's Pilot.

Only true external boundaries are replaced: provider APIs by a fake HTTP
server on 127.0.0.1, and ``pm2`` / ``heimsense`` / ``claude`` / ``gh`` by
stand-in executables on PATH (see ``e2e_support/stubs.py``). Nothing inside
claude-mux is mocked or patched.

Isolation: claude-mux resolves ``~/.claude-mux`` and ``~/.claude`` when it is
imported, so HOME is pointed at a throw-away directory *before* the package
is imported and wiped between tests. This is why the suite must run in its
own pytest process (``pytest e2e``), not together with ``tests/``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

if "claude_mux" in sys.modules:
    raise pytest.UsageError(
        "The E2E suite must run in its own pytest process so HOME can be isolated "
        "before claude_mux is imported. Run: pytest e2e"
    )

sys.path.insert(0, str(Path(__file__).parent))
from e2e_support import stubs  # noqa: E402
from e2e_support.fake_upstream import FakeUpstream  # noqa: E402
from e2e_support.helpers import instance_port, run_cli, wait_http  # noqa: E402

# --------------------------------------------------------------------------
# Process-wide isolation (runs at collection time, before claude_mux import)
# --------------------------------------------------------------------------

_SANDBOX = Path(tempfile.mkdtemp(prefix="claude-mux-e2e-"))
HOME = _SANDBOX / "home"
BIN = _SANDBOX / "bin"
HOME.mkdir()
stubs.install(BIN)

for var in list(os.environ):
    if var.startswith(("ANTHROPIC_", "CLAUDE_", "PROXY_", "MODEL_MAP_")) or var in (
        "LISTEN_ADDR", "LOG_FILE", "TMUX", "TMUX_PANE", "TERM_PROGRAM", "VSCODE_IPC_HOOK_CLI",
        "GH_TOKEN", "GITHUB_TOKEN", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
    ):
        del os.environ[var]

os.environ["HOME"] = str(HOME)
# Stub bin first, then the interpreter's bin dir (so `claude-mux` itself resolves
# to this checkout, as Claude Code's status line integration needs).
os.environ["PATH"] = os.pathsep.join([str(BIN), str(Path(sys.executable).parent), os.environ.get("PATH", "")])
os.environ["HEIMSENSE_BIN"] = str(BIN / "heimsense")
# tmux: a private server socket directory, so tests can never see or send keys to
# the developer's own tmux sessions.
os.environ["TMUX_TMPDIR"] = str(_SANDBOX / "tmux")
(_SANDBOX / "tmux").mkdir()
# Network sandbox: anything that is not localhost goes to a closed port and
# fails fast, so a test can never reach a real provider by accident.
for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ[var] = "http://127.0.0.1:9"
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

import claude_mux  # noqa: E402,F401  (import only after HOME is isolated)
from claude_mux.config import CLAUDE_MUX_DIR  # noqa: E402

assert str(CLAUDE_MUX_DIR).startswith(str(HOME)), "claude_mux resolved a non-isolated HOME"

# Guard: the TUI's "reload" replaces the process with os.execv. A test must
# never turn the pytest process into a TUI, so fail loudly instead.
_real_execv = os.execv


def _refuse_execv(path, args):
    raise AssertionError(f"TUI attempted os.execv({path!r}, {args!r}) during an E2E test")


os.execv = _refuse_execv


def pytest_sessionfinish(session, exitstatus):
    stubs.kill_pm2_processes(HOME)
    if shutil.which("tmux"):
        subprocess.run(["tmux", "kill-server"], capture_output=True)
    shutil.rmtree(_SANDBOX, ignore_errors=True)


# --------------------------------------------------------------------------
# Per-test fixtures
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def home():
    """A clean home directory for every test (processes from the last test are stopped)."""
    stubs.kill_pm2_processes(HOME)
    for child in HOME.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    CLAUDE_MUX_DIR.mkdir(parents=True, exist_ok=True)
    yield HOME
    stubs.kill_pm2_processes(HOME)


@pytest.fixture
def upstream():
    srv = FakeUpstream()
    yield srv
    srv.close()


@pytest.fixture
def cli():
    return run_cli


@pytest.fixture
def start_proxy():
    """`claude-mux start NAME` and wait until the proxy answers /health."""
    def _start(name: str) -> int:
        r = run_cli("start", name)
        assert r.code == 0, r.output
        port = instance_port(name)
        wait_http(f"http://127.0.0.1:{port}/health")
        return port
    return _start
