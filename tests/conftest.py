"""Test-konfig: importer claude_mux.tui som 'claude_mux' modul."""
import importlib.util
import logging
import sys
from pathlib import Path

# Forsøg 1: brug pakke-versionen (claude_mux.tui) når pakken er installeret/tilgængelig
# Forsøg 2: fallback til scripts/claude-mux.py (dev-miljø)
_PKG_TUI = Path(__file__).parent.parent / "claude_mux" / "tui.py"
_SCRIPT = Path(__file__).parent.parent.parent.parent / "scripts" / "claude-mux.py"


def _load_hs():
    # Brug pakke-versionen hvis den findes
    src = _PKG_TUI if _PKG_TUI.exists() else _SCRIPT
    spec = importlib.util.spec_from_file_location("claude_mux", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claude_mux"] = mod
    spec.loader.exec_module(mod)
    return mod


def _silence_file_logging():
    """Omdirigér alle FileHandlers til NullHandler — tests må ikke skrive til ~/.heimsense/."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            root.removeHandler(handler)
            handler.close()
    root.addHandler(logging.NullHandler())

    hs_log = logging.getLogger("claude-mux")
    for handler in list(hs_log.handlers):
        if isinstance(handler, logging.FileHandler):
            hs_log.removeHandler(handler)
            handler.close()


if "claude_mux" not in sys.modules:
    _load_hs()

_silence_file_logging()
