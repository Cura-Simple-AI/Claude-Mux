#!/usr/bin/env python3
"""Heimsense CLI entry-point."""
import sys


def main():
    """Start Heimsense TUI."""
    import argparse
    from claude_mux.tui import __version__

    parser = argparse.ArgumentParser(
        prog="claude-mux",
        description="TUI manager for AI-provider subscriptions (Claude, Copilot, DeepSeek, Gemini...)",
    )
    parser.add_argument("--version", "-V", action="version", version=f"heimsense {__version__}")
    parser.add_argument("--tui", action="store_true", help="Start TUI (default)", default=True)

    args = parser.parse_args()

    from claude_mux.tui import run_tui
    run_tui()


if __name__ == "__main__":
    main()
