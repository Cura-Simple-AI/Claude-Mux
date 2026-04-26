#!/usr/bin/env python3
"""Validate all Textual CSS in tui.py — catch runtime crashes BEFORE launch."""

import re
import sys
from textual.css.stylesheet import Stylesheet, StylesheetParseError

# Theme variable values for validation (Textual's dark theme defaults).
# Keys must NOT include the leading '$' — that's how set_variables() expects them.
_VARS: dict[str, str] = {
    "primary": "#004578",
    "secondary": "#ffa500",
    "accent": "#ffa500",
    "background": "#1e1e1e",
    "surface": "#2d2d2d",
    "panel": "#333333",
    "text": "#ffffff",
    "text-muted": "#888888",
    "text-disabled": "#555555",
    "boost": "#ffffff",
    "error": "#ff4444",
    "success": "#44ff44",
    "warning": "#ff8800",
    "warning-text": "#ffffff",
    "block-cursor-background": "#ffa500",
    "block-cursor-foreground": "#000000",
    "block-cursor-text-style": "bold",
    "block-cursor-blurred-background": "#444444",
    "block-cursor-blurred-foreground": "#ffffff",
    "block-cursor-blurred-text-style": "none",
    "block-hover-background": "#3a3a3a",
    "surface-darken-1": "#1a1a1a",
    "surface-lighten-1": "#3d3d3d",
    "secondary-muted": "#553300",
    "foreground": "#ffffff",
}


def validate_css(css_text: str, block_label: str = "css") -> list[str]:
    """Parse CSS with theme vars, return errors (empty = valid).

    Important: add_source() must be called before set_variables() — otherwise
    variable substitution is skipped and $primary etc. appear undefined.
    """
    ss = Stylesheet()
    wrapped = f"Screen {{ }}  /* dummy */\n{css_text}"
    # add_source first, then inject variables (order matters)
    ss.add_source(wrapped, None)
    ss.set_variables(_VARS)
    try:
        ss.parse()
        return []
    except StylesheetParseError as e:
        err_obj = e.args[0] if e.args else e
        return [str(e) for e in getattr(err_obj, "errors", [err_obj])]
    except Exception as e:
        return [str(e)]


def main() -> None:
    with open("claude_mux/tui.py") as f:
        source = f.read()

    blocks = re.findall(r'CSS\s*=\s*"""(.+?)"""', source, re.DOTALL)
    if not blocks:
        print("❌ No CSS blocks found")
        sys.exit(1)

    total_errors = 0
    for i, block in enumerate(blocks):
        errs = validate_css(block.strip(), f"block{i}")
        if errs:
            total_errors += len(errs)
            for e in errs:
                print(f"  ❌ Block {i}: {e}")
        else:
            print(f"  ✅ Block {i} OK")

    if total_errors:
        print(f"\n❌ {total_errors} CSS error(s) found — fix before running!")
        sys.exit(1)
    else:
        print(f"\n✅ All {len(blocks)} CSS blocks valid")


if __name__ == "__main__":
    main()
