"""Validate all Textual CSS in tui.py — catch runtime crashes BEFORE launch."""

import re
from pathlib import Path

from textual.css.stylesheet import Stylesheet, StylesheetParseError

# Theme variable values for validation (Textual's dark theme defaults)
_VARS: dict[str, str] = {
    "$primary": "#004578",
    "$secondary": "#ffa500",
    "$accent": "#ffa500",
    "$background": "#1e1e1e",
    "$surface": "#2d2d2d",
    "$panel": "#333333",
    "$text": "#ffffff",
    "$text-muted": "#888888",
    "$text-disabled": "#555555",
    "$boost": "#ffffff",
    "$error": "#ff4444",
    "$success": "#44ff44",
    "$warning": "#ff8800",
    "$warning-text": "#ffffff",
    "$block-cursor-background": "#ffa500",
    "$block-cursor-foreground": "#000000",
    "$block-cursor-text-style": "bold",
    "$block-cursor-blurred-background": "#444444",
    "$block-cursor-blurred-foreground": "#ffffff",
    "$block-cursor-blurred-text-style": "none",
    "$block-hover-background": "#3a3a3a",
    "$surface-darken-1": "#1a1a1a",
    "$surface-lighten-1": "#3d3d3d",
    "$secondary-muted": "#553300",
    "$foreground": "#ffffff",
}


def _find_tui_py() -> Path:
    """Find tui.py relative to this module."""
    return Path(__file__).resolve().parent / "tui.py"


def validate_str(css_text: str, label: str = "css") -> list[str]:
    """Parse CSS with theme vars, return list of error messages (empty = valid)."""
    ss = Stylesheet()
    ss.set_variables(_VARS)
    wrapped = f"Screen {{ }}  /* dummy */\n{css_text}"
    ss.add_source(wrapped, None)
    try:
        ss.parse()
        return []
    except StylesheetParseError as e:
        err_obj = e.args[0] if e.args else e
        return [str(e) for e in getattr(err_obj, "errors", [err_obj])]
    except Exception as e:
        return [str(e)]


def validate_file(tui_path: Path | None = None) -> tuple[bool, list[str]]:
    """Validate all CSS blocks in tui.py.

    Returns (ok, all_errors).
    """
    path = Path(tui_path) if tui_path else _find_tui_py()
    source = path.read_text()
    blocks = re.findall(r'CSS\s*=\s*"""(.+?)"""', source, re.DOTALL)

    all_errors: list[str] = []
    for i, block in enumerate(blocks):
        errs = validate_str(block.strip(), f"block{i}")
        for e in errs:
            all_errors.append(f"  ❌ Block {i}: {e}")

    if not blocks:
        all_errors.append("  ❌ No CSS blocks found")

    ok = len(all_errors) == 0
    return ok, all_errors
