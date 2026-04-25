"""Test config: make claude_mux importable as a package and silence file logging."""
import logging
import sys
from pathlib import Path

# Add the package root to sys.path so `import claude_mux` works as a real package.
# This supports both: running tests from tools/claude-mux/ AND from the repo root.
_PKG_ROOT = Path(__file__).parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

# Fallback: if package import fails, load tui.py directly as claude_mux
# (used in dev when the package is not installed)
if "claude_mux" not in sys.modules:
    try:
        import claude_mux  # noqa: F401 — triggers package __init__
    except ImportError:
        import importlib.util
        _SCRIPT = Path(__file__).parent.parent.parent.parent / "scripts" / "heimsense-tui.py"
        if _SCRIPT.exists():
            spec = importlib.util.spec_from_file_location("claude_mux", _SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["claude_mux"] = mod
            spec.loader.exec_module(mod)


def _silence_file_logging():
    """Redirect all FileHandlers to NullHandler — tests must not write to ~/.claude-mux/."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            root.removeHandler(handler)
            handler.close()
    root.addHandler(logging.NullHandler())

    for logger_name in ("claude-mux", "heimsense-tui"):
        lg = logging.getLogger(logger_name)
        for handler in list(lg.handlers):
            if isinstance(handler, logging.FileHandler):
                lg.removeHandler(handler)
                handler.close()


_silence_file_logging()
