"""`python -m claude_mux.tui --test-all` — the built-in self-test must pass."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_builtin_selftest_passes(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-m", "claude_mux.tui", "--test-all"],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "=== ALL TESTS PASSED ===" in r.stdout
