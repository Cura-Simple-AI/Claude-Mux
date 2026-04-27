#!/usr/bin/env python3
"""
Heimsense TUI Manager — ConfigManager + InstanceManager

Data model and persistence for AI subscriptions.
API keys are stored ONLY as env var references, never in subscriptions.json.
"""
__version__ = "0.1.3"

# ---------------------------------------------------------------------------
# Imports from dedicated modules — classes no longer defined here
# ---------------------------------------------------------------------------

from claude_mux.config import (  # noqa: E402
    CLAUDE_MUX_DIR,
    SUBSCRIPTIONS_FILE,
    ENV_TEMPLATE_KEYS,
    PROVIDER_PRESETS,
    PROVIDER_URL_LABELS,
    COPILOT_CHAT_ENDPOINTS,
    ENV_TO_SETTINGS_MAP,
    MERGE_KEYS,
    SETTINGS_KEYS_TO_REMOVE,
    EMPTY_SUBSCRIPTIONS,
    fetch_copilot_models,
    _now,
    _generate_id,
    _port_is_available,
    _atomic_write,
    log,
    ConfigManager,
)
from claude_mux.instance import InstanceManager, _format_duration  # noqa: E402
from claude_mux.sync import SyncManager, TIER_FALLBACK_MODELS, extract_response_body  # noqa: E402
from claude_mux.failover import FailoverManager  # noqa: E402

import json
import logging
import os
import re
import sys
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path


# ═══════════════════════════════════════════════════════════
# TUI — Textual App
# ═══════════════════════════════════════════════════════════

try:
    from textual.app import App, ComposeResult
    from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen, Screen
    from textual.widgets import Button, DataTable, Footer, Input, Label, ProgressBar, RichLog, Select, Static, TextArea
    from textual.worker import Worker
    from rich.text import Text
    from rich.markup import escape
    from rich.table import Table as RichTable

    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False


def _detail_table(rows: list) -> "RichTable":
    """Build a borderless two-column grid for detail panel key/value rows.

    Each row is either:
    - (label, value)  — label rendered bold-dim, colon appended automatically;
                        value may be a str (treated as Rich markup), Text, or any renderable
    - None            — blank separator row
    """
    t = RichTable.grid(padding=(0, 2))
    t.add_column(style="bold dim", no_wrap=True)
    t.add_column(overflow="fold")
    for row in rows:
        if row is None:
            t.add_row("", "")
        else:
            label, value = row
            cell = value if isinstance(value, Text) else Text.from_markup(str(value))
            t.add_row(f"{label}:", cell)
    return t


def _status_char(status: str) -> str:
    return {"online": "*", "stopped": "o", "error": "x", "unknown": "?"}.get(status, "?")


def _status_color(status: str) -> str:
    return {"online": "green", "stopped": "gray", "error": "red", "unknown": "yellow"}.get(status, "gray")


def _trunc(s: str, n: int = 40) -> str:
    return s[:n] + "..." if len(s) > n else s


def _time_ago(iso_ts) -> str:
    """ISO timestamp (str) or Unix epoch (float/int) → '3m ago' or '-'."""
    if not iso_ts:
        return "-"
    try:
        if isinstance(iso_ts, (int, float)):
            t = float(iso_ts)
        else:
            t = datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp()
        secs = int(time.time() - t)
        if secs < 0:
            return "0s"
        return _format_duration(secs) + " ago"
    except (ValueError, TypeError):
        return "-"


# --- Model Test popup ---

class HealthPopup(ModalScreen):
    """Show model response with selectable body text."""

    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, label: str, status_code: int, elapsed_ms: int, body: str):
        super().__init__()
        self._label = label
        self._status_code = status_code
        self._elapsed = elapsed_ms
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="health-popup-box"):
            yield Static(
                f"[bold]{self._label}[/bold]  ([dim]{self._elapsed}ms[/dim])",
                id="health-popup-header",
            )
            yield TextArea(self._body[:2000], read_only=True, id="health-popup-body")
            with Horizontal(id="health-popup-buttons"):
                yield Button("Copy", id="copy", variant="default")
                yield Button("OK", id="ok", variant="primary")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "copy":
            self._do_copy()
        else:
            self.dismiss()

    def _do_copy(self):
        import os, subprocess, tempfile
        # Strategy 1: OSC 52 (works in SSH / native terminals / Blink)
        self.app.copy_to_clipboard(self._body)
        # Strategy 2: open in VS Code editor if running inside VS Code / code-server
        if os.environ.get("TERM_PROGRAM") == "vscode" or os.environ.get("VSCODE_IPC_HOOK_CLI"):
            tmp = CLAUDE_MUX_DIR / "last-response.txt"
            try:
                tmp.write_text(self._body)
                subprocess.Popen(["code", str(tmp)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.app.notify(f"Opened in VS Code editor — use CTRL+A, CTRL+C there", timeout=4)
                return
            except OSError:
                pass
        # Fallback: save to file and show path
        tmp = CLAUDE_MUX_DIR / "last-response.txt"
        try:
            tmp.write_text(self._body)
            self.app.notify(f"Saved to {tmp}\n(OSC 52 also sent — works in SSH/Blink)", timeout=5)
        except OSError:
            self.app.notify("Copied via OSC 52 (works in SSH/native terminals)", timeout=3)

    CSS = """
    HealthPopup {
        align: center middle;
    }
    #health-popup-box {
        width: 70;
        height: auto;
        max-height: 24;
        border: solid $primary;
        padding: 1 2;
        background: $surface;
    }
    #health-popup-header {
        height: auto;
        margin-bottom: 1;
    }
    #health-popup-body {
        height: 12;
        width: 100%;
    }
    #health-popup-buttons {
        height: auto;
        align: right middle;
        margin-top: 1;
    }
    #health-popup-buttons > Button {
        margin-left: 1;
    }
    """


# --- Name input modal (used for saving *current settings) ---

class NameInputModal(ModalScreen):
    """Prompt the user for a subscription name, then dismiss with the value."""

    BINDINGS = [("escape", "dismiss_empty", "Cancel")]

    def __init__(self, title: str = "Save as new subscription"):
        super().__init__()
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="name-modal-box"):
            yield Static(f"[bold]{self._title}[/bold]\n", markup=True)
            yield Input(placeholder="Subscription name…", id="name-input")
            with Horizontal(id="name-modal-buttons"):
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("Save", id="save", variant="primary")

    CSS = """
    NameInputModal {
        align: center middle;
    }
    #name-modal-box {
        width: 50;
        height: auto;
        border: solid $primary;
        padding: 1 2;
        background: $surface;
    }
    #name-modal-buttons {
        height: auto;
        align: right middle;
        margin-top: 1;
    }
    #name-modal-buttons > Button {
        margin-left: 1;
    }
    """

    def action_dismiss_empty(self):
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "save":
            name = self.query_one("#name-input", Input).value.strip()
            self.dismiss(name if name else None)

    def on_input_submitted(self, event: Input.Submitted):
        name = event.value.strip()
        self.dismiss(name if name else None)


# --- Log viewer ---

class LogViewer(Screen):
    """PM2 log viewer — shows out.log and error.log."""

    def __init__(self, pm2_name: str):
        super().__init__()
        self._pm2_name = pm2_name
        self.sub_title = f"Logs: {pm2_name}"

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
        ("b", "dismiss", "Close"),
    ]

    CSS = """
    LogViewer {
        align: center middle;
    }
    #log-container {
        width: 100%;
        height: 85%;
        border: solid $primary;
        padding: 1;
    }
    #log-content {
        width: 100%;
    }
    """

    def action_dismiss(self):
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "close":
            self.dismiss()

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="log-container"):
            yield Static(id="log-content")
        yield Button("Close", id="close", variant="primary")

    def on_mount(self):
        self._load_logs()

    def _load_logs(self):
        content = []
        try:
            sub_name = self._pm2_name.replace("claude-mux-", "", 1)
            inst_dir = CLAUDE_MUX_DIR / "instances" / sub_name
            out_log = inst_dir / "out.log"
            err_log = inst_dir / "error.log"
            wrote = False
            if out_log.exists() and out_log.stat().st_size > 0:
                content.append("── out.log ──")
                for line in out_log.read_text().splitlines()[-100:]:
                    content.append(line)
                wrote = True
            if err_log.exists() and err_log.stat().st_size > 0:
                content.append("")
                content.append("── error.log ──")
                for line in err_log.read_text().splitlines()[-100:]:
                    content.append(line)
                wrote = True
            if not wrote:
                out_log2 = Path.home() / ".pm2" / "logs" / f"{self._pm2_name}-out.log"
                if out_log2.exists() and out_log2.stat().st_size > 0:
                    content.append(f"── {out_log2.name} ──")
                    for line in out_log2.read_text().splitlines()[-100:]:
                        content.append(line)
                    wrote = True
                else:
                    content.append("No log lines found")
                    content.append(f"Searched: {out_log}")
                    content.append(f"and: {out_log2}")
        except Exception as e:
            content.append(f"Error loading: {e}")
        text = "\n".join(content) if content else "(empty log)"
        self.query_one("#log-content", Static).update(text)


# --- Failover log modal ---

class FailoverLogModal(ModalScreen):
    """Show ~/.claude-mux/failover.log in a modal."""

    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]

    def __init__(self, log_path):
        super().__init__()
        self._log_path = log_path

    def compose(self) -> ComposeResult:
        yield Static("[bold]Failover Log[/bold]", id="fl-title")
        with VerticalScroll(id="fl-scroll"):
            yield Static(id="fl-content")
        yield Button("Close (q/ESC)", id="fl-close", variant="primary")

    def on_mount(self):
        try:
            if self._log_path.exists():
                lines = self._log_path.read_text().splitlines()
                # Show newest first
                text = "\n".join(reversed(lines[-200:]))
                self.query_one("#fl-content", Static).update(text or "(empty log)")
            else:
                self.query_one("#fl-content", Static).update(
                    f"[dim]No failover events yet.\nLog file: {self._log_path}[/dim]"
                )
        except OSError as e:
            self.query_one("#fl-content", Static).update(f"[red]Error reading: {e}[/red]")

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss()


# --- Confirm dialogs ---

class ConfirmModal(ModalScreen):
    """Confirmation dialog. dismiss(bool) — True = yes/ok."""

    def __init__(self, title: str, message: str):
        super().__init__()
        self._title = title
        self._message = message

    def compose(self) -> ComposeResult:
        yield Static(f"[bold]{self._title}[/bold]\n\n{self._message}")
        with Horizontal():
            yield Button("Yes", id="yes", variant="error")
            yield Button("No", id="no", variant="primary")

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(event.button.id == "yes")

    def on_key(self, event):
        if event.key in ("j", "y", "enter"):
            self.dismiss(True)
            event.stop()
        elif event.key in ("n", "q", "escape", "b"):
            self.dismiss(False)
            event.stop()


# --- Test modal ---

class TestModal(ModalScreen):
    """Live test results for all three model tiers.

    Opens immediately so the user sees progress. Each tier row updates
    as the result arrives. Close with Enter/Escape/q or the Close button.
    """

    CSS = """
    TestModal {
        align: center middle;
    }
    TestModal > Vertical {
        background: $surface;
        border: solid $accent;
        padding: 1 2;
        width: 70;
        height: auto;
        max-height: 24;
    }
    .test-row {
        height: 3;
        margin: 0;
        padding: 0 1;
        border: solid $panel;
    }
    """

    def __init__(self, sub_name: str, tiers: list[tuple[str, str]]):
        super().__init__()
        self._sub_name = sub_name
        self._tiers = tiers  # [(tier, model), ...]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"[bold]Testing {self._sub_name}[/bold]", id="test-modal-title")
            for tier, model in self._tiers:
                with Horizontal(classes="test-row", id=f"test-row-{tier}"):
                    yield Static(
                        f"[dim]{tier:6s}[/dim]  [dim]{model}[/dim]",
                        id=f"test-status-{tier}",
                    )
            with Horizontal():
                yield Button("Close", id="close-test", variant="primary")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "close-test":
            self.dismiss()

    def on_key(self, event):
        if event.key in ("enter", "escape", "q"):
            self.dismiss()
            event.stop()

    def set_running(self, tier: str):
        """Mark tier as running (spinner-like indicator)."""
        try:
            self.query_one(f"#test-status-{tier}", Static).update(
                f"[yellow]⏳[/yellow] [dim]{tier:6s}[/dim]  [yellow]testing…[/yellow]"
            )
        except Exception:
            pass

    def set_result(self, tier: str, model: str, result: dict):
        """Update tier row with OK/FAIL result."""
        try:
            code = result.get("code", 0)
            elapsed = result.get("elapsed", 0)
            body = result.get("body", "")[:80]
            if code == 200:
                self.query_one(f"#test-status-{tier}", Static).update(
                    f"[green]✓ OK[/green]  [dim]{tier:6s}[/dim]  {model}  [dim]{elapsed}ms[/dim]\n"
                    f"[dim]{body}[/dim]"
                )
            else:
                self.query_one(f"#test-status-{tier}", Static).update(
                    f"[red]✖ {code}[/red]  [dim]{tier:6s}[/dim]  {model}  [dim]{elapsed}ms[/dim]\n"
                    f"[red]{body}[/red]"
                )
        except Exception:
            pass


# --- Help modal ---

class HelpModal(ModalScreen):
    """Show all keybindings — press h or ? to open."""

    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close"), ("h", "dismiss", "Close")]

    HELP_TEXT = """\
[bold]Claude Mux — Keyboard Shortcuts[/bold]
[dim]Optimized for iPhone SSH (no Ctrl/F/arrow/Tab/ESC required)[/dim]

[bold yellow]Navigation[/bold yellow]
  j / n / ↓   Move down  (iPhone: j or n)
  k / p / ↑   Move up    (iPhone: k or p)
  1-9          Activate provider N directly (skips current-settings row)
  /            Filter providers by name (Enter/Esc to close)
  h / ?        Show this help
  q            Quit / Close modal

[bold yellow]Provider management[/bold yellow]
  +         Add new subscription (wizard)
  e         Edit subscription (model maps, force-model, fields)
  d         Delete subscription (confirm: j=yes, n=no)
  R         Reauth (renew OAuth token)
  Activate  Switch Claude Code to this provider (button / 1-9)

[bold yellow]Proxy providers[/bold yellow]
  s         Start / Stop (toggle)
  t         Test HTTP endpoint
  l         Show PM2 logs (close: q/b)

[bold yellow]System[/bold yellow]
  r         Refresh table and details
  z         Reload TUI (hotload)
  x         Failover check (manual)
  L         Show failover log

[bold yellow]Provider Select (wizard)[/bold yellow]
  1-8       Select directly with number
  j/k/n/p   Navigate up/down
  Enter     Confirm selection
  q / b     Cancel / Back

[bold yellow]Confirmation dialogs[/bold yellow]
  j / Enter   Confirm (Yes)
  n / q / b   Cancel (No)
"""

    def compose(self) -> ComposeResult:
        yield Static(self.HELP_TEXT, id="help-text")
        yield Button("Close (q/ESC)", id="close-help", variant="primary")

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss()

    def on_key(self, event):
        if event.key in ("escape", "q", "h", "?"):
            self.dismiss()
            event.stop()


# --- Hotload countdown modal ---

class HotloadModal(ModalScreen):
    """Shows countdown progress bar — ESC or Cancel aborts hotload."""

    COUNTDOWN = 2  # sekunder

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[bold]Hotload[/bold]\nUpdate detected — reloading shortly...", id="hl-msg")
            yield ProgressBar(total=self.COUNTDOWN * 10, show_eta=False, id="hl-bar")
            yield Button("Cancel (ESC)", id="hl-cancel", variant="warning")

    def on_mount(self):
        self._ticks = 0
        self.set_interval(0.1, self._tick)

    def _tick(self):
        self._ticks += 1
        try:
            self.query_one("#hl-bar", ProgressBar).advance(1)
        except Exception:
            pass
        if self._ticks >= self.COUNTDOWN * 10:
            self.dismiss(True)  # True = run hotload

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(False)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(False)


# --- Force model modal ---

class ForceModelModal(ModalScreen):
    """Select force model for all model aliases (haiku/sonnet/opus → same model)."""

    def __init__(self, current_model: str, available_models: list[str]):
        super().__init__()
        self._current = current_model
        self._available = available_models

    def compose(self) -> ComposeResult:
        options = [("No force (remove)", "__none__")] + [(m, m) for m in self._available]
        yield Static("[bold]Force Model[/bold]\n\nAll model aliases point to selected model.\nSelect 'No force' to remove.", id="fm-title")
        yield Select(options, id="fm-select", prompt="Select model...", allow_blank=False)
        with Horizontal():
            yield Button("OK", id="ok", variant="primary")
            yield Button("Cancel", id="cancel")

    def on_mount(self):
        sel = self.query_one("#fm-select", Select)
        if self._current in [m for _, m in [("No force", "__none__")] + [(m, m) for m in self._available]]:
            try:
                sel.value = self._current
            except Exception:
                pass

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "ok":
            sel = self.query_one("#fm-select", Select)
            self.dismiss(sel.value if sel.value != Select.BLANK else None)
        else:
            self.dismiss(None)

    def on_key(self, event):
        if event.key in ("q", "escape", "b"):
            self.dismiss(None)
            event.stop()
        elif event.key == "enter":
            sel = self.query_one("#fm-select", Select)
            self.dismiss(sel.value if sel.value != Select.BLANK else None)
            event.stop()


# --- Provider selection screen ---

class ProviderSelectScreen(ModalScreen):
    """Full-screen provider selection with digit, arrow keys, and highlight."""
    def __init__(self, presets: dict, index_map: dict[int, str]):
        super().__init__()
        self._presets = presets
        self._index_map = index_map
        self._keys = list(presets)
        self._highlighted = -1  # -1 = none
        self._dismissed = False

    def compose(self):
        yield Static("[bold]Select Provider[/bold]", id="ps-title")
        lines = []
        for i, key in enumerate(self._presets, start=1):
            p = self._presets[key]
            lines.append(f"   {i}. {p['label']}")
        yield Static("\n".join(lines), id="ps-list")
        yield Static("[dim]Number 1-8 to select  ·  j/k or ↑/↓ to navigate  ·  Enter to confirm  ·  Esc/q to cancel[/dim]", id="ps-hint")

    def on_key(self, event):
        if self._dismissed:
            event.stop()
            return
        if event.key in ("escape", "q"):
            self._dismissed = True
            self.dismiss(None)
            event.stop()
        elif event.key in ("up", "k", "p"):
            self._highlighted = (len(self._keys) - 1) if self._highlighted <= 0 else (self._highlighted - 1)
            self._render_highlight()
            event.stop()
        elif event.key in ("down", "j", "n"):
            self._highlighted = 0 if (self._highlighted < 0 or self._highlighted >= len(self._keys) - 1) else (self._highlighted + 1)
            self._render_highlight()
            event.stop()
        elif event.key == "enter":
            if 0 <= self._highlighted < len(self._keys):
                self._dismissed = True
                self.dismiss(self._keys[self._highlighted])
            event.stop()
        elif event.key.isdigit():
            num = int(event.key)
            if num in self._index_map:
                idx = self._keys.index(self._index_map[num])
                self._highlighted = idx
                self._render_highlight()
                self._dismissed = True
                # Wrap in def so return value is None — otherwise Textual awaits AwaitComplete and crashes
                key_val = self._keys[idx]
                def _dismiss_after_highlight(k=key_val):
                    self.dismiss(k)
                self.set_timer(0.3, _dismiss_after_highlight)
            event.stop()

    def _safe_dismiss(self, key):
        self.dismiss(key)

    def _render_highlight(self):
        lines = []
        for i, key in enumerate(self._presets, start=1):
            p = self._presets[key]
            if i - 1 == self._highlighted:
                lines.append(f"  [reverse]  {i}. {p['label']}  [/reverse]")
            else:
                lines.append(f"   {i}. {p['label']}")
        self.query_one("#ps-list", Static).update("\n".join(lines))


class AddWizard(ModalScreen):
    """Multi-step wizard for adding/editing subscriptions."""

    CSS = """
    .oauth-url {
        border: solid $accent;
        padding: 1;
        margin: 1 0;
        max-height: 5;
        overflow-x: auto;
        overflow-y: hidden;
        width: 100%;
    }
    """

    def __init__(self, config: ConfigManager, existing_sub: dict | None = None, reauth: bool = False):
        super().__init__()
        self.cm = config
        self._step = 1
        self._provider_presets = PROVIDER_PRESETS
        self._data: dict = {}
        self._existing = existing_sub
        self._edit_mode = existing_sub is not None
        self._reauth = reauth
        self._copilot_models: list[dict] = []
        self._copilot_fetch_done = False
        self._oauth_state: dict = {}
        self._selected_provider: str | None = None
        self._provider_index_map: dict[int, str] = {}

    def compose(self) -> ComposeResult:
        title = "[bold]Edit Subscription[/bold]" if self._edit_mode else "[bold]Add Subscription[/bold]"
        yield Static(title, id="wiz-title")
        # Step 1: Name only
        with Vertical(id="step1"):
            yield Label("Name:")
            yield Input(placeholder="e.g. my-deepseek", id="wiz-name")
            yield Static("", id="wiz-name-hint")
            with Horizontal():
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("Next →", id="next-name", variant="primary", disabled=True)
        # Step 2: Provider list (numbered)
        with Vertical(id="step2", classes="hidden"):
            yield Static("Select provider (type 1-8):", id="wiz-provider-prompt")
            yield Static("", id="wiz-provider-list")
            with Horizontal():
                yield Button("← Back", id="back-provider", variant="default")
        # Step 3: API Key + URL (hidden for OAuth)
        with Vertical(id="step3", classes="hidden"):
            yield Label("API Key (or env var name):", id="wiz-key-label")
            yield Input(placeholder="e.g. MY_API_KEY or sk-...", id="wiz-key", password=True)
            yield Label("Base URL (auto-filled for presets):", id="wiz-url-label")
            yield Input(placeholder="https://api.example.com/v1", id="wiz-url")
            with Horizontal():
                yield Button("← Back", id="back-key", variant="default")
                yield Button("Next →", id="next-key", variant="primary")
        # Step 4: Model maps (skipped for OAuth)
        with Vertical(id="step4", classes="hidden"):
            yield Static("", id="wiz-models-status")
            yield Label("Model Maps:")
            yield Label("Haiku:")
            yield Input(placeholder="model name", id="wiz-haiku")
            yield Select([], id="wiz-haiku-sel", prompt="Select model...", classes="hidden")
            yield Label("Sonnet:")
            yield Input(placeholder="model name", id="wiz-sonnet")
            yield Select([], id="wiz-sonnet-sel", prompt="Select model...", classes="hidden")
            yield Label("Opus:")
            yield Input(placeholder="model name", id="wiz-opus")
            yield Select([], id="wiz-opus-sel", prompt="Select model...", classes="hidden")
            yield Label("Force all aliases to one model (optional):")
            yield Select([("No force", "__none__")], id="wiz-force", prompt="No force", allow_blank=False)
            yield Label("Notes (optional):")
            yield Input(placeholder="notes", id="wiz-notes")
            with Horizontal():
                yield Button("← Back", id="back-models", variant="default")
                btn_label = "Save" if self._edit_mode else "Create"
                yield Button(Text.from_markup(f"[bold yellow]{btn_label[0]}[/bold yellow]{btn_label[1:]}"), id="create", variant="primary")
        # Step 5: OAuth (only for Claude Max)
        with Vertical(id="step5", classes="hidden"):
            yield Static("", id="oauth-info")
            yield TextArea("", id="oauth-url-display", classes="hidden", disabled=True)
            with Horizontal(id="oauth-url-row"):
                yield Button("Authenticate in browser", id="oauth-open-url", variant="primary")
            yield Static("", id="oauth-status")
            yield Input(placeholder="paste code here", id="wiz-oauth-code", classes="hidden")
            with Horizontal(id="oauth-nav-row"):
                yield Button("Back", id="back-oauth", variant="default")
                yield Button("Next", id="oauth-next", variant="primary", disabled=True)
            yield Static("", id="oauth-result", classes="hidden")
            yield Button("Close", id="oauth-close", classes="hidden")

    def on_mount(self):
        # Hide all steps except step1
        for s in ("step2", "step3", "step4", "step5"):
            self.query_one(f"#{s}", Vertical).display = False
        # Build provider index map
        self._provider_index_map = {}
        for i, key in enumerate(PROVIDER_PRESETS, start=1):
            self._provider_index_map[i] = key
        # If edit-mode: pre-fill fields
        if self._edit_mode and self._existing:
            sub = self._existing
            self.query_one("#wiz-name", Input).value = sub.get("name", "")
            self.query_one("#wiz-url", Input).value = sub.get("provider_url", "")
            self.query_one("#wiz-key", Input).value = sub.get("api_key_env", "")
            models = sub.get("model_maps", {})
            self.query_one("#wiz-haiku", Input).value = models.get("haiku", "")
            self.query_one("#wiz-sonnet", Input).value = models.get("sonnet", "")
            self.query_one("#wiz-opus", Input).value = models.get("opus", "")
            self.query_one("#wiz-notes", Input).value = sub.get("notes", "")
            self._validate_step1()
            # Reauth: jump directly to OAuth flow
            if self._reauth and sub.get("auth_type") == "oauth":
                self._data["name"] = sub.get("name", "")
                self._data["auth_type"] = "oauth"
                self._data["provider_key"] = sub.get("provider_key", "")
                self._data["provider_url"] = sub.get("provider_url", "")
                self.query_one("#wiz-title", Static).update("[bold]Reauthenticate Claude Max[/bold]")
                self._start_oauth_flow(self._data["name"])

    def _show_side(self, n: int):
        for s in ("step1", "step2", "step3", "step4", "step5"):
            self.query_one(f"#{s}", Vertical).display = False
        self.query_one(f"#step{n}", Vertical).display = True
        self._step = n

    def on_input_submitted(self, event: Input.Submitted):
        """Enter in Input → go to next step or confirm."""
        eid = event.input.id
        if eid == "wiz-name":
            if self.query_one("#wiz-name", Input).value.strip():
                # Edit-mode: provider is locked — always jump directly to models
                if self._edit_mode and self._existing:
                    self._data["name"] = self.query_one("#wiz-name", Input).value.strip()
                    self._data["provider_key"] = self._existing.get("provider_key", "")
                    self._skip_to_models_edit()
                else:
                    self._go_to_providers()
        elif eid == "wiz-key":
            # Step 3 → if OAuth skip, otherwise default
            pass
        elif eid == "wiz-oauth-code":
            self._submit_oauth_code()

    def on_input_changed(self, event: Input.Changed):
        eid = event.input.id
        if eid == "wiz-name":
            self._validate_step1()
        elif eid == "wiz-key":
            self._validate_step3()
        elif eid == "wiz-url":
            self._validate_step3()
        elif eid == "wiz-oauth-code":
            has_code = bool(event.input.value.strip())
            self.query_one("#oauth-next", Button).disabled = not has_code

    def _validate_step1(self):
        """Step 1: name + provider selected (provider selected on step 2)."""
        name_ok = bool(self.query_one("#wiz-name", Input).value.strip())
        self.query_one("#next-name", Button).disabled = not name_ok
        if name_ok:
            self.query_one("#wiz-name-hint", Static).update("[dim]Press Enter to continue[/dim]")
        else:
            self.query_one("#wiz-name-hint", Static).update("")

    def _validate_step3(self):
        """Step 3: key+url optional for OAuth, required for others."""
        is_oauth = bool(self._selected_provider and PROVIDER_PRESETS.get(self._selected_provider, {}).get("auth_type") == "oauth")
        if is_oauth:
            self.query_one("#next-key", Button).disabled = False
        else:
            key_ok = bool(self.query_one("#wiz-key", Input).value.strip())
            url_ok = bool(self.query_one("#wiz-url", Input).value.strip())
            self.query_one("#next-key", Button).disabled = not (key_ok and url_ok)

    def on_button_pressed(self, event: Button.Pressed):
        eid = event.button.id
        if eid == "cancel":
            self.dismiss()
        elif eid == "next-name":
            # Edit-mode: provider is locked — always jump directly to models
            if self._edit_mode and self._existing:
                self._data["name"] = self.query_one("#wiz-name", Input).value.strip()
                self._data["provider_key"] = self._existing.get("provider_key", "")
                self._skip_to_models_edit()
            else:
                self._go_to_providers()
        elif eid == "back-key":
            self._show_side(1)
            self.query_one("#wiz-title", Static).update("[bold]Add Subscription[/bold]")
            self.query_one("#wiz-name", Input).disabled = False
            self.query_one("#wiz-name", Input).disabled = False
            self.query_one("#wiz-name", Input).focus()
        elif eid == "next-key":
            self._go_to_models()
        elif eid == "back-key":
            self._show_side(2)
            self.query_one("#wiz-title", Static).update("[bold]Select Provider[/bold]")
            self._render_provider_list()
        elif eid == "back-models":
            self._show_side(3)
            self.query_one("#wiz-title", Static).update("[bold]API Key[/bold]")
        elif eid == "back-oauth":
            self._show_side(1)
            self.query_one("#wiz-title", Static).update("[bold]Add Subscription[/bold]")
            self.query_one("#wiz-name", Input).disabled = False
            self.query_one("#wiz-name", Input).focus()
            self._oauth_cleanup()
        elif eid == "oauth-open-url":
            self._open_oauth_url()
        elif eid == "oauth-next":
            self._submit_oauth_code()
        elif eid == "oauth-close":
            sub_id = self._oauth_state.get("_sub_id")
            name = self._data.get("name", "")
            self.dismiss({"id": sub_id, "name": name, "updated": True})
        elif eid == "create":
            self._do_create()

    def on_key(self, event):
        if event.key == "escape":
            self._oauth_cleanup()
            self.dismiss()
            event.stop()

    def _skip_to_models_edit(self):
        """Edit-mode: jump directly to models (step 4), locking provider fields."""
        self._data["api_key"] = self._existing.get("api_key", "") if self._existing else ""
        self._data["provider_url"] = self._existing.get("provider_url", "") if self._existing else ""
        self._data["auth_type"] = self._existing.get("auth_type", "bearer") if self._existing else "bearer"
        self._show_side(4)
        self.query_one("#wiz-title", Static).update("[bold]Edit Model Maps[/bold]")
        self.query_one("#wiz-key-label", Label).display = False
        self.query_one("#wiz-key", Input).display = False
        self.query_one("#wiz-url-label", Label).display = False
        self.query_one("#wiz-url", Input).display = False

    def _go_to_providers(self):
        """From step 1 → push ProviderSelectScreen."""
        import re as _re
        _NAME_RE = _re.compile(r"^[a-zA-Z0-9._-]{1,100}$")
        raw_name = self.query_one("#wiz-name", Input).value.strip()
        if not raw_name:
            self.notify("Name is required.", severity="error")
            return
        if not _NAME_RE.match(raw_name):
            self.notify(
                "Name may only contain letters, digits, dots, hyphens, underscores (max 100 chars).",
                severity="error",
            )
            return
        self._data["name"] = raw_name
        self._data["auth_type"] = "bearer"  # default
        self.query_one("#wiz-name", Input).disabled = True

        def _on_done(provider_key):
            self.query_one("#wiz-name", Input).disabled = False
            if not provider_key:
                return  # canceled
            self._selected_provider = provider_key
            preset = PROVIDER_PRESETS[provider_key]
            self.query_one("#wiz-key", Input).value = preset["api_key_env"]
            self.query_one("#wiz-url", Input).value = preset["provider_url"]
            models = preset.get("model_maps", {})
            self.query_one("#wiz-haiku", Input).value = models.get("haiku", "")
            self.query_one("#wiz-sonnet", Input).value = models.get("sonnet", "")
            self.query_one("#wiz-opus", Input).value = models.get("opus", "")
            self._data["auth_type"] = preset.get("auth_type", "bearer")
            self._data["provider_url"] = preset.get("provider_url", "")
            self._data["provider_key"] = provider_key
            if provider_key == "copilot":
                self._start_copilot_fetch()
            is_oauth = preset.get("auth_type") == "oauth"
            if is_oauth:
                self._start_oauth_flow(self._data["name"])
            else:
                self._show_side(3)
                self.query_one("#wiz-title", Static).update("[bold]API Key[/bold]")
                self.query_one("#wiz-key", Input).focus()
                self._validate_step3()
                # Warn if heimsense is missing — bearer providers need it to start
                from claude_mux.instance import InstanceManager
                if not Path(InstanceManager.HEIMSENSE_BIN).exists():
                    self.app.notify(
                        "heimsense not installed — you can add this subscription but cannot start it until heimsense is installed.\n"
                        "Install: curl -fsSL https://raw.githubusercontent.com/cura-ai/claude-mux/main/install.sh | bash",
                        title="heimsense missing",
                        severity="warning",
                        timeout=12,
                    )

        self.app.push_screen(ProviderSelectScreen(self._provider_presets, self._provider_index_map), _on_done)

    def _go_to_models(self):
        """From step 3 → step 4 (models)."""
        self._data["api_key"] = self.query_one("#wiz-key", Input).value.strip()
        self._data["provider_url"] = self.query_one("#wiz-url", Input).value.strip()
        self._show_side(4)
        self.query_one("#wiz-title", Static).update("[bold]Model Maps[/bold]")
        # Copilot: show Select if models are ready
        if self._selected_provider == "copilot":
            self._apply_copilot_model_selects()
            if not self._copilot_fetch_done:
                self.query_one("#wiz-models-status", Static).update("[yellow]Fetching models from Copilot...[/yellow]")
                self.set_interval(0.5, self._poll_copilot_models)
        self._populate_force_select()
        self.query_one("#create", Button).focus()

    def _populate_force_select(self):
        """Rebuild force-select options from current haiku/sonnet/opus inputs."""
        haiku = self.query_one("#wiz-haiku", Input).value.strip()
        sonnet = self.query_one("#wiz-sonnet", Input).value.strip()
        opus = self.query_one("#wiz-opus", Input).value.strip()
        models = list(dict.fromkeys(m for m in (haiku, sonnet, opus) if m))
        options = [("No force", "__none__")] + [(m, m) for m in models]
        sel = self.query_one("#wiz-force", Select)
        sel.set_options(options)
        # Pre-fill from existing subscription's saved force setting
        if self._edit_mode and self._existing:
            saved_force = self._existing.get("force_model", "__none__")
            try:
                sel.value = saved_force
            except Exception:
                sel.value = "__none__"

    def _do_create(self):
        """Create or update subscription."""
        name = self._data.get("name", "")
        provider_url = self._data.get("provider_url", "")
        api_key = self._data.get("api_key", "")
        auth_type = self._data.get("auth_type", "bearer")
        model_maps = {
            "haiku": self._get_model_value("haiku"),
            "sonnet": self._get_model_value("sonnet"),
            "opus": self._get_model_value("opus"),
        }
        notes = self.query_one("#wiz-notes", Input).value.strip().replace("[", "\\[").replace("]", "\\]")
        # Force model: read from select (may not exist for oauth skip)
        try:
            force_sel = self.query_one("#wiz-force", Select)
            force_model = str(force_sel.value) if force_sel.value not in (Select.BLANK, None) else "__none__"
        except Exception:
            force_model = "__none__"
        if self._edit_mode and self._existing:
            sub_id = self._existing["id"]
            is_env_ref = api_key.isupper() and "_" in api_key
            api_key_env = api_key if is_env_ref else self._existing.get("api_key_env", api_key)
            # Save api_key for non-env refs (OAuth, direct, or literal bearer keys)
            if api_key and not is_env_ref:
                kwargs = dict(
                    name=name, provider_url=provider_url,
                    api_key_env=api_key_env, auth_type=auth_type,
                    model_maps=model_maps, notes=notes,
                    api_key=api_key,
                )
            else:
                kwargs = dict(
                    name=name, provider_url=provider_url,
                    api_key_env=api_key_env, auth_type=auth_type,
                    model_maps=model_maps, notes=notes,
                )
            if force_model != "__none__":
                kwargs["force_model"] = force_model
            else:
                kwargs["force_model"] = ""
            self.cm.update_subscription(sub_id, **kwargs)
            # .env will be regenerated by InstanceManager.generate_env() on start/sync
            self.dismiss({"id": sub_id, "name": name, "updated": True, "force_model": force_model})
        else:
            is_env_ref = api_key.isupper() and "_" in api_key
            api_key_env = api_key if is_env_ref else f"{name.upper()}_API_KEY".replace("-", "_")
            sub = self.cm.add_subscription(
                name=name, provider_url=provider_url,
                api_key_env=api_key_env, auth_type=auth_type,
                model_maps=model_maps, notes=notes,
                api_key=api_key if not is_env_ref else "",
            )
            if force_model != "__none__":
                self.cm.update_subscription(sub["id"], force_model=force_model)
            self.dismiss({"id": sub["id"], "name": sub["name"], "updated": False, "force_model": force_model})
            if not is_env_ref and api_key:
                os.environ[api_key_env] = api_key
                inst_dir = CLAUDE_MUX_DIR / "instances" / name
                inst_dir.mkdir(parents=True, exist_ok=True)
                env_path = inst_dir / ".env"
                env_text = env_path.read_text() if env_path.exists() else ""
                env_lines = [l for l in env_text.splitlines() if not l.startswith(f"{api_key_env}=")]
                env_lines.append(f"{api_key_env}={api_key}")
                env_path.write_text("\n".join(env_lines) + "\n")
                env_path.chmod(0o600)

    # --- Copilot model fetch ---

    def _start_copilot_fetch(self):
        """Start background thread to fetch Copilot models."""
        self._copilot_fetch_done = False
        def _fetch():
            try:
                result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
                token = result.stdout.strip() if result.returncode == 0 else ""
                if token:
                    self._copilot_models = fetch_copilot_models(token)
                else:
                    self._copilot_models = []
            except Exception:
                self._copilot_models = []
            self._copilot_fetch_done = True
        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_copilot_model_selects(self):
        """Switch haiku/sonnet/opus to Select dropdowns if models are ready."""
        if not self._copilot_models:
            return
        options = [(f"{m['name']} ({m['vendor']})", m["id"]) for m in self._copilot_models]
        preset_maps = PROVIDER_PRESETS["copilot"]["model_maps"]
        for alias, sel_id, inp_id in [
            ("haiku", "wiz-haiku-sel", "wiz-haiku"),
            ("sonnet", "wiz-sonnet-sel", "wiz-sonnet"),
            ("opus", "wiz-opus-sel", "wiz-opus"),
        ]:
            sel = self.query_one(f"#{sel_id}", Select)
            inp = self.query_one(f"#{inp_id}", Input)
            sel.set_options(options)
            current = inp.value.strip() or preset_maps.get(alias, "")
            try:
                sel.value = current
            except Exception:
                pass
            sel.display = True
            inp.display = False
        self.query_one("#wiz-models-status", Static).update(
            f"[green]{len(self._copilot_models)} models available[/green]"
        )

    def _poll_copilot_models(self):
        """Poll until fetch is done, then update UI and stop."""
        if self._copilot_fetch_done:
            self._apply_copilot_model_selects()
            try:
                for timer in self._timers:
                    timer.stop()
            except Exception:
                pass

    # --- OAuth flow (Claude Max) ---

    def _start_oauth_flow(self, name: str):
        safe_name = name.lower().replace(" ", "-")
        session_name = f"claude-oauth-{safe_name}"
        self._oauth_state = {"session": session_name, "token": None, "step": "starting"}
        log.info("OAuth: kill-session %s", session_name)
        subprocess.run(["tmux", "kill-session", "-t", session_name],
                       capture_output=True, text=True)
        # Delete old log file so we don't reuse stale URL/token
        old_log = f"/tmp/oauth-{safe_name}.log"
        try:
            os.remove(old_log)
            log.info("OAuth: removed stale log %s", old_log)
        except OSError:
            pass
        log.info("OAuth: new-session %s", session_name)
        proc = subprocess.Popen(
            ["tmux", "new-session", "-d", "-s", session_name, "-x", "220",
             f"env -u ANTHROPIC_BASE_URL -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u CLAUDE_CODE_OAUTH_TOKEN BROWSER=/bin/false claude setup-token 2>&1 | tee /tmp/oauth-{safe_name}.log; tmux wait-for -S oauth-done"],
        )
        log.info("OAuth: new-session started (async) PID=%s", proc.pid)
        # Verify session exists
        r = subprocess.run(["tmux", "has-session", "-t", session_name], capture_output=True, text=True)
        log.info("OAuth: has-session check: ret=%s err=%s", r.returncode, r.stderr.strip())
        self._show_side(5)
        self.query_one("#wiz-title", Static).update("[bold]Claude Max OAuth Setup[/bold]")
        self.query_one("#oauth-info", Static).update("[yellow]Starting OAuth flow...[/yellow]")
        self.query_one("#oauth-status", Static).update("")
        self.query_one("#oauth-url-row", Horizontal).display = False
        self.query_one("#oauth-open-url", Button).display = False
        self.query_one("#wiz-oauth-code", Input).display = False
        self.query_one("#oauth-nav-row", Horizontal).display = False
        self.query_one("#oauth-result", Static).display = False
        self.query_one("#oauth-close", Button).display = False
        self._oauth_poll_url()

    def _oauth_poll_url(self):
        try:
            # Read URL from log file (avoids tmux wrapping truncation)
            safe_name = self._oauth_state.get("session", "").replace("claude-oauth-", "")
            log_path = f"/tmp/oauth-{safe_name}.log"
            log.info("OAuth poll: checking %s", log_path)
            full_url = None
            try:
                with open(log_path) as f:
                    for line in f:
                        if "https://claude.com/cai/oauth/authorize" in line:
                            # Strip ANSI escapes, extract visible URL after OSC 8 hyperlink
                            # Line format: ESC]8;id=X;URL\x07URL\x1b]8;;\x07
                            # The visible URL is the second BEL-delimited chunk
                            parts = line.split("\x07")
                            visible = parts[1] if len(parts) > 1 else parts[0]
                            # Strip remaining CSI/OSC ANSI escapes from visible URL
                            cleaned = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*', '', visible)
                            if cleaned.startswith("https://claude.com/cai/oauth/authorize"):
                                full_url = cleaned.strip()
                            log.info("OAuth poll: found URL in log (len=%s)", len(full_url))
            except FileNotFoundError:
                log.info("OAuth poll: log file not found yet")
            except OSError as e:
                log.info("OAuth poll: log error %s", e)
            # Fallback: tmux capture-pane

            if not full_url:
                self.query_one("#oauth-info", Static).update("[yellow]⏳ Waiting for authorization URL...[/yellow]")
                output = subprocess.run(
                    ["tmux", "capture-pane", "-S", "-500", "-J", "-t", "--", self._oauth_state["session"], "-p"],
                    capture_output=True, text=True, timeout=5,
                ).stdout
                lines = output.splitlines()
                for i, line in enumerate(lines):
                    if "https://claude.com/cai/oauth/authorize" in line:
                        parts = [line.strip()]
                        for j in range(i + 1, min(i + 10, len(lines))):
                            nl = lines[j].strip()
                            if not nl or "http" in nl:
                                break
                            parts.append(nl)
                        raw = "".join(parts)
                        # BEL-delimited format: ESC]8;id=X;URL\x07URL\x07ESC]8;;\x07
                        # Take second BEL-delimited part (the visible URL)
                        bel_parts = raw.split("\x07")
                        visible_url = bel_parts[1] if len(bel_parts) > 1 else bel_parts[0]
                        full_url = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*', '', visible_url).strip()
                        if full_url.startswith("https://claude.com/cai/oauth/authorize"):
                            break
            if full_url:
                self._oauth_state["step"] = "awaiting_code"
                # Stop URL poll timer if running
                turl = self._oauth_state.get("_timer_url")
                if turl:
                    try:
                        turl.stop()
                    except Exception:
                        pass
                    self._oauth_state["_timer_url"] = None
                self.query_one("#oauth-info", Static).update(
                    "[bold]Authenticate with Claude Max[/bold]\n\n"
                    "Click the button below to open the authorization page in your browser."
                )
                self._oauth_state["url"] = full_url
                in_tmux = "TMUX" in os.environ
                if in_tmux:
                    self.query_one("#oauth-open-url", Button).display = False
                    self.query_one("#oauth-url-row", Horizontal).display = False
                    self.query_one("#oauth-info", Static).update(
                        "[bold]Authenticate with Claude Max[/bold]\n\n"
                        "Open this URL in your browser, then paste the authorization code below."
                    )
                    self.query_one("#oauth-url-display", TextArea).disabled = False
                    self.query_one("#oauth-url-display", TextArea).text = full_url
                    self.query_one("#oauth-url-display", TextArea).display = True
                else:
                    self.query_one("#oauth-url-row", Horizontal).display = True
                    self.query_one("#oauth-open-url", Button).display = True
                    self.query_one("#oauth-open-url", Button).focus()
                self.query_one("#oauth-nav-row", Horizontal).display = True
                self.query_one("#wiz-oauth-code", Input).display = True
                self.query_one("#back-oauth", Button).display = True
                return
        except Exception as e:
            log.info("OAuth poll url: exception: %s", e, exc_info=True)
        if self._oauth_state.get("step") == "starting" and not self._oauth_state.get("_timer_url"):
            t = self.set_interval(0.5, self._oauth_poll_url)
            self._oauth_state["_timer_url"] = t

    def _submit_oauth_code(self):
        code = self.query_one("#wiz-oauth-code", Input).value.strip()
        log.info("OAuth: submit code len=%s step=%s", len(code), self._oauth_state.get("step"))
        if not code:
            return
        # Take only first line (remove any line breaks)
        code = code.splitlines()[0].strip()
        self._oauth_state["step"] = "submitting"
        self.query_one("#oauth-info", Static).update("[yellow]⏳ Exchanging authorization code for token...[/yellow]")
        self.query_one("#oauth-url-row", Horizontal).display = False
        self.query_one("#wiz-oauth-code", Input).display = False
        self.query_one("#oauth-nav-row", Horizontal).display = False
        self.query_one("#oauth-status", Static).update("")
        session = self._oauth_state["session"]
        log.info("OAuth: send-keys to %s", session)
        subprocess.run(["tmux", "send-keys", "-t", session, code, "Enter"], timeout=5)
        time.sleep(1)
        subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], timeout=5)
        log.info("OAuth: keys sent, polling token")
        self._oauth_poll_token()

    def _oauth_poll_token(self):
        try:
            # Save safe_name BEFORE cleanup can clear session
            safe_name = self._oauth_state.get("session", "").replace("claude-oauth-", "")
            log_path = f"/tmp/oauth-{safe_name}.log" if safe_name else None
            token = None
            try:
                with open(log_path) as f:
                    for line in f:
                        # "export CLAUDE_CODE_OAUTH_TOKEN=<token>" er placeholder — ignorer
                        if "export CLAUDE_CODE_OAUTH_TOKEN=" in line and "sk-" not in line:
                            continue
                        if "export CLAUDE_CODE_OAUTH_TOKEN=" in line:
                            token = line.split("export CLAUDE_CODE_OAUTH_TOKEN=", 1)[1].strip()
                            break
                        # "Your OAuth token (valid for 1 year):" second line after has token
                        if "Your OAuth token" in line:
                            try:
                                next(f, "")  # skip blank line
                                tok = next(f, "").strip()
                                if tok.startswith("sk-"):
                                    token = tok
                            except StopIteration:
                                pass
            except OSError:
                pass
            if token:
                log.info("OAuth poll token: FOUND token len=%d", len(token))
                self._oauth_state["token"] = token
                # Stop token poll timer
                ttok = self._oauth_state.get("_timer_token")
                if ttok:
                    try:
                        ttok.stop()
                    except Exception:
                        pass
                    self._oauth_state["_timer_token"] = None
                self.query_one("#oauth-info", Static).update(
                    "[yellow]⏳ Saving subscription...[/yellow]"
                )
                self._oauth_cleanup()
                self._oauth_finish()
                return
        except Exception as e:
            log.info("OAuth poll token: exception: %s", e)
        if not log_path:
            log.info("OAuth poll token: no log path (cleaned up), stopping")
            return
        count = self._oauth_state.get("poll_count", 0) + 1
        self._oauth_state["poll_count"] = count
        log.info("OAuth poll token: attempt %d from log %s", count, log_path)
        # Show countdown every 5th attempt (2.5s interval)
        if count % 5 == 0:
            secs_left = max(0, 30 - count // 2)
            self.query_one("#oauth-status", Static).update(
                f"[yellow]⏳ Waiting for token... ({secs_left}s remaining)[/yellow]"
            )
        # Check if tmux session is still running (early exit on crash)
        session = self._oauth_state.get("session", "")
        if session and count > 4:
            chk = subprocess.run(
                ["tmux", "has-session", "-t", session],
                capture_output=True,
            )
            if chk.returncode != 0:
                # Session is dead — no token will come
                log.warning("OAuth poll: tmux session %s is gone (exit %d)", session, chk.returncode)
                ttok = self._oauth_state.get("_timer_token")
                if ttok:
                    try:
                        ttok.stop()
                    except Exception:
                        pass
                    self._oauth_state["_timer_token"] = None
                self._oauth_state["step"] = "failed"
                self.query_one("#oauth-info", Static).update(
                    "[red]⚠ OAuth session ended without token[/red]\n\n"
                    "The process stopped before a token was generated.\n"
                    "Try again — click 'Open browser' below."
                )
                self.query_one("#oauth-url-row", Horizontal).display = True
                self.query_one("#wiz-oauth-code", Input).value = ""
                self.query_one("#wiz-oauth-code", Input).display = True
                self.query_one("#oauth-nav-row", Horizontal).display = True
                self.query_one("#wiz-oauth-code", Input).focus()
                return

        if count > 60:
            # Stop token poll timer
            ttok = self._oauth_state.get("_timer_token")
            if ttok:
                try:
                    ttok.stop()
                except Exception:
                    pass
                self._oauth_state["_timer_token"] = None
            self._oauth_cleanup()
            self._oauth_state["step"] = "failed"
            self.query_one("#oauth-info", Static).update(
                "[red]⚠ Authorization failed[/red]\n\n"
                "No token was received. This may be because the code was invalid or expired.\n\n"
                "Click the button below to start over."
            )
            self.query_one("#oauth-url-row", Horizontal).display = True
            self.query_one("#wiz-oauth-code", Input).value = ""
            self.query_one("#wiz-oauth-code", Input).display = True
            self.query_one("#oauth-nav-row", Horizontal).display = True
            self.query_one("#wiz-oauth-code", Input).focus()
            return
        if not self._oauth_state.get("_timer_token"):
            t = self.set_interval(0.5, self._oauth_poll_token)
            self._oauth_state["_timer_token"] = t

    def _oauth_cleanup(self):
        for tkey in ("_timer_url", "_timer_token"):
            t = self._oauth_state.get(tkey)
            if t:
                try:
                    t.stop()
                except Exception:
                    pass
        session = self._oauth_state.get("session", "")
        if session:
            subprocess.run(["tmux", "kill-session", "-t", session],
                           capture_output=True, text=True)
            self._oauth_state["session"] = ""

    def _open_oauth_url(self):
        url = self._get_oauth_url()
        if not url:
            return
        import webbrowser
        in_tmux = "TMUX" in os.environ
        if not in_tmux:
            try:
                webbrowser.open(url)
            except Exception:
                in_tmux = True
        if in_tmux:
            self.query_one("#oauth-open-url", Button).display = False
            self.query_one("#oauth-url-row", Horizontal).display = False
            self.query_one("#oauth-info", Static).update(
                "[bold]Authenticate with Claude Max[/bold]\n\n"
                "Open this URL in your browser, then paste the authorization code below."
            )
            self.query_one("#oauth-url-display", TextArea).disabled = False
            self.query_one("#oauth-url-display", TextArea).text = url
            self.query_one("#oauth-url-display", TextArea).display = True
        else:
            self.query_one("#oauth-info", Static).update(
                "[bold]Authenticate with Claude Max[/bold]\n\n"
                "Click the button below to open the authorization page in your browser."
            )
        self._oauth_focus_paste_delayed()

    def _oauth_focus_paste_delayed(self):
        def _do_focus():
            self.query_one("#wiz-oauth-code", Input).focus()
        self.set_timer(5, _do_focus)

    def _get_oauth_url(self) -> str:
        return self._oauth_state.get("url", "")

    def _oauth_finish(self):
        name = self._data["name"]
        api_key = self._oauth_state["token"]
        log.info("_oauth_finish: name=%s api_key_len=%d edit=%s", name, len(api_key) if api_key else 0, self._edit_mode)
        provider_url = self._data.get("provider_url", "")
        auth_type = self._data.get("auth_type", "bearer")
        model_maps = {
            "haiku": self._get_model_value("haiku") or "claude-sonnet-4-5",
            "sonnet": self._get_model_value("sonnet") or "claude-sonnet-4-6",
            "opus": self._get_model_value("opus") or "claude-opus-4-7",
        }
        notes = self.query_one("#wiz-notes", Input).value.strip().replace("[", "\\[").replace("]", "\\]")
        api_key_env = "CLAUDE_CODE_OAUTH_TOKEN"
        if self._edit_mode and self._existing:
            sub_id = self._existing["id"]
            self.cm.update_subscription(
                sub_id, name=name, provider_url=provider_url,
                api_key_env=api_key_env, auth_type=auth_type,
                model_maps=model_maps, notes=notes,
                api_key=api_key,
            )
            log.info("_oauth_finish: updated sub %s api_key_len=%d", sub_id, len(api_key) if api_key else 0)
        else:
            sub = self.cm.add_subscription(
                name=name, provider_url=provider_url,
                api_key_env=api_key_env, auth_type=auth_type,
                model_maps=model_maps, notes=notes,
            )
            self.cm.update_subscription(sub["id"], api_key=api_key)
        os.environ[api_key_env] = api_key
        inst_dir = CLAUDE_MUX_DIR / "instances" / name
        inst_dir.mkdir(parents=True, exist_ok=True)
        env_path = inst_dir / ".env"
        env_text = env_path.read_text() if env_path.exists() else ""
        env_lines = [l for l in env_text.splitlines() if not l.startswith(f"{api_key_env}=")]
        env_lines.append(f"{api_key_env}={api_key}")
        env_path.write_text("\n".join(env_lines) + "\n")
        env_path.chmod(0o600)
        sub_id = self._existing["id"] if self._edit_mode else sub["id"]
        models_str = ", ".join(f"{k}={v}" for k, v in model_maps.items() if v)
        self.query_one("#oauth-info", Static).update(
            "[bold green]✓ Subscription created successfully![/bold green]\n\n"
            f"[bold]Name:[/bold] {name}\n"
            f"[bold]Auth:[/bold] Claude Max (OAuth)\n"
            f"[bold]Models:[/bold] {models_str}\n"
        )
        self.query_one("#oauth-url-row", Horizontal).display = False
        self.query_one("#wiz-oauth-code", Input).display = False
        self.query_one("#oauth-nav-row", Horizontal).display = False
        self.query_one("#oauth-status", Static).update("[bold green]✓ OAuth token obtained![/bold green]")
        self.query_one("#oauth-result", Static).display = True
        self.query_one("#oauth-close", Button).display = True
        self._oauth_state["_sub_id"] = sub_id

    def _get_model_value(self, alias: str) -> str:
        sel_id = f"wiz-{alias}-sel"
        inp_id = f"wiz-{alias}"
        sel = self.query_one(f"#{sel_id}", Select)
        inp = self.query_one(f"#{inp_id}", Input)
        if sel.display and sel.value and sel.value != Select.BLANK:
            return str(sel.value)
        return inp.value.strip()# --- Main App ---

class HeimsenseApp(App):
    """Heimsense TUI Manager — main screen."""

    ENABLE_COMMAND_PALETTE = False
    TITLE = "Heimsense TUI"

    def _handle_exception(self, error: Exception) -> bool:
        """Textual exception handler — log and restore terminal on crash."""
        log.exception("Unexpected error in TUI: %s", error)
        _restore_terminal()
        return super()._handle_exception(error)

    CSS = """
    DataTable.instance-list {
        width: 100%;
        height: 1fr;
        border: solid $primary;
    }
    DataTable.instance-list > .datatable--header {
        display: none;
    }
    DataTable:focus {
        background-tint: $foreground 0%;
    }
    DataTable > .datatable--cursor {
        background: $panel;
    }
    Vertical.list-panel {
        width: 45%;
        height: 100%;
    }
    Button.add-btn {
        width: 100%;
        margin: 0;
    }
    Vertical.detail-panel {
        width: 55%;
        height: 100%;
        padding: 1 1 0 1;
    }
    #detail {
        height: 1fr;
        overflow-y: auto;
        padding: 0 1;
    }
    #app-header {
        height: 1;
        width: 100%;
        padding: 0 1;
        background: $panel;
        color: $text;
        text-align: left;
    }
    #filter-input {
        height: 1;
        width: 100%;
        border: none;
        padding: 0 1;
        display: none;
    }
    #filter-input.active {
        display: block;
    }
    #providers-label {
        height: 1;
        width: 100%;
        padding: 0 1;
        background: $primary;
        color: $text;
        text-style: bold;
    }
    #script-age {
        height: 1;
        width: 100%;
        text-align: right;
        padding: 0 1;
        color: $text-muted;
    }
    Grid.buttons {
        grid-size: 3;
        height: auto;
        margin: 2 0 0 0;
    }
    Button {
        margin: 0 1;
        min-width: 12;
    }
    #oauth-url-display {
        margin: 0;
        padding: 0;
        border: none;
        max-height: 12;
        min-height: 3;
    }
    ModalScreen {
        align: center middle;
    }
    ModalScreen > Vertical, ModalScreen > Static, ModalScreen > Horizontal {
        width: 50;
        padding: 1 2;
        border: solid $primary;
        background: $surface;
    }
    ModalScreen Input, ModalScreen Select {
        width: 100%;
    }
    .hidden {
        display: none;
    }
    #oauth-url-row {
        width: 100%;
        height: auto;
        min-height: 1;
        align: center middle;
        border: none;
        padding: 0 1;
    }
    #oauth-url-row > Button {
        width: auto;
        min-width: 14;
        margin: 0 1;
    }
    """

    def __init__(self, config: ConfigManager, initial_selected: str | None = None):
        super().__init__()
        self.cm = config
        self.im = InstanceManager(config)
        self.sync = SyncManager(config)
        self.failover = FailoverManager(config, self.sync)
        self._selected_id: str | None = None
        self._initial_selected: str | None = initial_selected
        # Cached result of detect_active() — updated on every _refresh_table()
        self._active_id: str | None = None
        # Cache for direct test results (sub_id → {code, body, ts})
        self._test_results: dict[str, dict] = {}
        # Filter state — live search via /
        self._filter_text: str = ""
        # Ordered list of row keys in current table (sub_id or "__current__")
        self._sorted_rows: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static(id="app-header", markup=True)
        with Horizontal():
            with Vertical(classes="list-panel"):
                yield Static("[bold]Providers[/bold]", id="providers-label")
                yield Input(placeholder="Filter providers…", id="filter-input")
                yield DataTable(classes="instance-list", id="inst-table")
                yield Button(Text.from_markup("[bold yellow]+[/bold yellow] Add Provider"), id="add", variant="primary", classes="add-btn")
            with Vertical(classes="detail-panel"):
                yield Static(id="detail", markup=True)
                with Grid(classes="buttons"):
                    yield Button(Text.from_markup("[bold yellow]S[/bold yellow]tart"), id="toggle", variant="success")
                    yield Button(Text.from_markup("[bold yellow]T[/bold yellow]est"), id="test")
                    yield Button(Text.from_markup("[bold yellow]S[/bold yellow]ync"), id="launch", variant="primary")
                    yield Button(Text.from_markup("[bold yellow]R[/bold yellow]eauth"), id="reauth", variant="primary")
                    yield Button(Text.from_markup("[bold yellow]F[/bold yellow]orce Model"), id="force_model", classes="hidden")
                    yield Button(Text.from_markup("[bold yellow]E[/bold yellow]dit"), id="edit")
                    yield Button(Text.from_markup("[bold yellow]L[/bold yellow]ogs"), id="logs")
                    yield Button("Cancel reload", id="cancel_hotload", variant="warning", classes="hidden")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#inst-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = False
        table.show_header = False
        table.cursor_foreground_priority = "renderable"
        table.add_column("", width=1)   # status dot — minimal width, no header
        table.add_column("Name")
        self._refresh_table()
        # Hotload restart: reuse previously selected subscription
        if self._initial_selected:
            self._selected_id = self._initial_selected
            # Find row index for selected sub and set cursor
            for row_idx, sub in enumerate(sorted(self.cm.subscriptions, key=lambda s: (
                0 if s["id"] == self._active_id else 1,
                s["name"].lower(),
            ))):
                if sub["id"] == self._initial_selected:
                    if row_idx < table.row_count:
                        table.move_cursor(row=row_idx)
                    break
        self._show_detail()
        # Rebuild footer after layout is complete (size.width is 0 during on_mount)
        self.call_after_refresh(self._refresh_footer)
        # Context-sensitive is now controlled by _show_detail (called via _refresh_table cursor)
        # Auto-reload: check every 3 seconds if the script has changed on disk
        self._script_mtime = os.stat(os.path.abspath(__file__)).st_mtime
        self._hotload_debounce_ts: float | None = None  # first mtime change detection
        self.set_interval(3, self._check_hotload)
        # Update table every 5 seconds (PM2 log scanning)
        self.set_interval(5, self._refresh_table)
        # Script age display — update every second
        self._script_birth = self._script_mtime
        self.set_interval(1, self._update_script_age)
        self._update_script_age()
        # Failover: periodic health-check every 5 minutes (background thread)
        self.set_interval(300, self._background_health_check)
        # Hint: suggest running claude-mux init if statusLine is not configured
        self.call_after_refresh(self._check_init_hint)
        # Ensure the instance list has initial focus so j/k/n/p works immediately
        self.query_one("#inst-table", DataTable).focus()

    def _check_init_hint(self):
        """Show a one-time hint if claude-mux init has not been run yet."""
        settings = self.sync._load_settings()
        if "statusLine" not in settings:
            self.notify(
                "Run [bold]claude-mux init[/bold] to enable the Claude Code status line.",
                title="Setup tip",
                timeout=8,
            )

    def _check_hotload(self):
        try:
            new_mtime = os.stat(os.path.abspath(__file__)).st_mtime
            if new_mtime != self._script_mtime:
                self._script_mtime = new_mtime
                self._hotload_debounce_ts = time.time()
            elif self._hotload_debounce_ts is not None:
                # Stable for 42s → show modal
                if time.time() - self._hotload_debounce_ts >= 42:
                    self._hotload_debounce_ts = None
                    self.push_screen(HotloadModal(), self._on_hotload_result)
        except Exception:
            pass

    def _on_hotload_result(self, do_reload: bool):
        if do_reload:
            log.info("Hotload: running test-tui before reload...")
            try:
                result = subprocess.run(
                    ["claude-mux", "test-tui"],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    err = (result.stderr.strip() or result.stdout.strip()
                           or "unknown error")
                    self.notify(f"❌ test-tui failed: {err}", timeout=10)
                    return
            except FileNotFoundError:
                self.notify("❌ claude-mux not found in PATH — cannot validate",
                            timeout=5)
                return
            except Exception as e:
                self.notify(f"❌ test-tui error: {e}", timeout=5)
                return
            log.info("Hotload: restarting...")
            args = [sys.executable, os.path.abspath(__file__), "--tui"]
            if self._selected_id:
                args += ["--selected", self._selected_id]
            os.execv(sys.executable, args)
        else:
            self.notify("Hotload cancelled", timeout=3)

    def _update_script_age(self):
        try:
            age = time.time() - self._script_birth
            if age < 120:
                age_label = f"{age:.0f}s"
            else:
                m = int(age // 60)
                s = int(age % 60)
                age_label = f"{m}m {s}s"
            # Full-width header: title left, timer right (padded with spaces)
            try:
                width = self.size.width
            except Exception:
                width = 80
            title = "Claude Mux — Use any LLM inside Claude Code"
            right = age_label
            # Subtract 2 for `padding: 0 1` on each side of #app-header.
            # title and right are plain text; no markup characters affect len().
            content_width = max(0, width - 2)
            gap = max(1, content_width - len(title) - len(right))
            # Show filter query in header when filter is active
            filter_input = self.query_one("#filter-input", None)
            filter_active = (
                filter_input is not None
                and "active" in (filter_input.classes or set())
                and filter_input.value
            )
            if filter_active:
                filter_label = f"  [bold yellow]/ {filter_input.value}[/bold yellow]"
                gap = max(1, content_width - len(title) - len(f"  / {filter_input.value}") - len(right))
                self.query_one("#app-header", Static).update(
                    f"[bold]{title}[/bold]{filter_label}{' ' * gap}[dim]{right}[/dim]"
                )
            else:
                self.query_one("#app-header", Static).update(
                    f"[bold]{title}[/bold]{' ' * gap}[dim]{right}[/dim]"
                )
        except Exception:
            pass

    def action_cancel_hotload(self):
        pass

    # --- Table ---

    # Auth-type colour palette
    _AUTH_COLORS = {"oauth": "cyan", "oauth_proxy": "cyan", "direct": "yellow", "gh_token": "magenta"}

    def _refresh_table(self):
        """Refresh DataTable with subscriptions + test status."""
        table = self.query_one("#inst-table", DataTable)
        # Save cursor position before clear
        prev_cursor = table.cursor_row if table.row_count > 0 else None
        table.clear()
        self._sorted_rows = []
        self._active_id = self.sync.detect_active()
        sorted_subs = sorted(self.cm.subscriptions, key=lambda s: (
            0 if s["id"] == self._active_id else 1,
            s["name"].lower(),
        ))
        # Apply live filter
        flt = self._filter_text.lower()
        if flt:
            sorted_subs = [s for s in sorted_subs if flt in s["name"].lower()]

        for sub in sorted_subs:
            sub_id = sub["id"]
            auth_type = sub.get("auth_type", "bearer")
            is_oauth = auth_type in ("oauth", "direct")
            status_info = self.im.get_status(sub_id)
            status = status_info.get("status", "unknown")
            is_failed = sub_id in self.failover._failed_subs
            name_txt = sub["name"] + (" ⚠" if is_failed else "")
            # Colour by auth type; bold for active; red for failed
            if is_failed:
                name_style = "bold red" if sub_id == self._active_id else "red"
            else:
                auth_color = self._AUTH_COLORS.get(auth_type, "")
                name_style = f"bold {auth_color}".strip() if sub_id == self._active_id else auth_color
            name = Text(name_txt, style=name_style)
            # Mark the subscription that is currently active in Claude with prefix
            if sub_id == self._active_id:
                name = Text("▶ ", style="bold green") + name

            if is_oauth:
                oauth_token = sub.get("api_key", "")
                tr = self._test_results.get(sub_id)
                if tr is not None:
                    dot_char = "●" if tr["code"] == 200 else "✖"
                    dot_color = "green" if tr["code"] == 200 else "red"
                else:
                    dot_char = "●" if oauth_token else "○"
                    dot_color = "green" if oauth_token else "red"
                status_dot = Text(dot_char, style=dot_color)
            else:
                http_stat = status_info.get("last_http_status")
                # Status dot: shape from PM2 status, color from last HTTP result
                dot_char = {"online": "●", "stopped": "○", "error": "✖", "unknown": "?"}.get(status, "?")
                if http_stat is None:
                    dot_color = _status_color(status)  # fallback to PM2 color before first test
                elif http_stat == 200:
                    dot_color = "green"
                elif 400 <= http_stat < 500:
                    dot_color = "yellow"
                else:
                    dot_color = "red"
                status_dot = Text(dot_char, style=dot_color)

            table.add_row(status_dot, name, key=sub_id)
            self._sorted_rows.append(sub_id)

        # Virtual *current settings row — shown when Claude's active settings
        # don't match any saved subscription (hidden when filter is active)
        if self._active_id is None and self.cm.subscriptions and not flt:
            virtual_name = Text("▶ ", style="bold green") + Text("*current settings", style="dim italic")
            cur_result = self._test_results.get("__current__")
            if cur_result is None:
                cur_status = Text("?", style="dim")
            elif cur_result["code"] == 200:
                cur_status = Text("●", style="green")
            else:
                cur_status = Text("✖", style="red")
            table.add_row(cur_status, virtual_name, key="__current__")
            self._sorted_rows.append("__current__")

        # Restore cursor: prefer _selected_id over prev_cursor (avoid stale detail)
        target_row = None
        if self._selected_id:
            if self._selected_id == "__current__":
                # Virtual row is always last
                target_row = len(sorted_subs)
            else:
                for i, s in enumerate(sorted_subs):
                    if s["id"] == self._selected_id:
                        target_row = i
                        break
        if target_row is not None and target_row < table.row_count:
            table.move_cursor(row=target_row)
        elif prev_cursor is not None and prev_cursor < table.row_count:
            table.move_cursor(row=prev_cursor)
        elif table.row_count > 0:
            table.move_cursor(row=0)

    def _show_current_settings_detail(self):
        """Detail panel for the virtual *current settings row."""
        detail = self.query_one("#detail", Static)
        settings_env = self.sync._load_settings().get("env", {})
        oauth_token = (
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            or settings_env.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        )
        base_url = settings_env.get("ANTHROPIC_BASE_URL", "")
        auth_token = settings_env.get("ANTHROPIC_AUTH_TOKEN", "")

        if oauth_token and not base_url:
            auth_display = "oauth (Claude Max)"
            key_display = oauth_token[:8] + "…" if oauth_token else "-"
        elif base_url and not base_url.startswith("http://localhost"):
            auth_display = "direct"
            key_display = auth_token[:12] + "…" if auth_token else "-"
        elif base_url.startswith("http://localhost"):
            auth_display = "bearer (proxy)"
            key_display = auth_token[:12] + "…" if auth_token else "-"
        else:
            auth_display = "unknown"
            key_display = "-"

        haiku = settings_env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "-")
        sonnet = settings_env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "-")
        opus = settings_env.get("ANTHROPIC_DEFAULT_OPUS_MODEL", "-")

        cur_result = self._test_results.get("__current__")
        if cur_result is None:
            test_line = "[dim]Not tested — press [bold]t[/bold] to check.[/dim]"
        elif cur_result["code"] == 200:
            test_line = f"[green]✓ OK[/green] — {cur_result['body']}"
        else:
            test_line = f"[red]✖ {cur_result['code']}[/red] — {cur_result['body']}"

        from rich.console import Group as RichGroup
        tbl = _detail_table([
            ("Auth", auth_display),
            ("Base URL", base_url or "(none)"),
            ("Key", key_display),
            None,
            ("Haiku", haiku),
            ("Sonnet", sonnet),
            ("Opus", opus),
            None,
            ("Status", test_line),
        ])
        detail.update(RichGroup(
            Text.from_markup(
                "[bold yellow]*current settings[/bold yellow]\n\n"
                "[dim]Claude is using settings not saved in claude-mux.[/dim]\n\n"
            ),
            tbl,
            Text.from_markup(
                "\n[dim]Press [bold]e[/bold] to save as subscription · [bold]t[/bold] to test.[/dim]"
            ),
        ))
        # Show Save as... and Test buttons; hide the rest
        for btn_id in ("toggle", "launch", "reauth", "force_model", "logs"):
            self.query_one(f"#{btn_id}", Button).display = False
        self.query_one("#test", Button).display = True
        edit_btn = self.query_one("#edit", Button)
        edit_btn.display = True
        edit_btn.label = "Save as…"
        self._refresh_footer()

    def _test_current_settings(self):
        """Run a health check against the current (unsaved) Claude settings."""
        settings_env = self.sync._load_settings().get("env", {})
        oauth_token = (
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            or settings_env.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        )
        base_url = settings_env.get("ANTHROPIC_BASE_URL", "")
        auth_token = settings_env.get("ANTHROPIC_AUTH_TOKEN", "")

        # Infer auth_type + build a temporary sub dict for _test_direct_http
        if oauth_token and not base_url:
            sub = {"auth_type": "oauth", "api_key": oauth_token, "provider_url": ""}
        elif base_url and not base_url.startswith("http://localhost"):
            sub = {"auth_type": "direct", "api_key": auth_token, "provider_url": base_url}
        elif base_url.startswith("http://localhost"):
            # Proxy — extract port and run proxy test
            try:
                port = int(base_url.split(":")[-1].split("/")[0])
            except (ValueError, IndexError):
                self.notify("Cannot parse proxy port from ANTHROPIC_BASE_URL", severity="error", timeout=4)
                return
            self.notify("Testing current proxy settings...", title="Test", timeout=2)
            t0 = time.time()
            result = self._run_proxy_test(port, auth_token, TIER_FALLBACK_MODELS["haiku"])
            elapsed = int((time.time() - t0) * 1000)
            status_code, reply = result["code"], result["body"]
            if status_code == 200:
                self.notify(f"OK ({elapsed}ms) — {reply[:120]}", title="*current settings — Test", timeout=8)
            else:
                self.push_screen(HealthPopup("*current settings — error", status_code, elapsed, reply))
            self._test_results["__current__"] = {"code": status_code, "body": reply, "ts": time.time()}
            self._refresh_table()
            self._show_detail()
            return
        else:
            self.notify("No Claude settings detected — nothing to test", severity="warning", timeout=4)
            return

        # Security note: the test sends the active token to the configured endpoint.
        # For OAuth, this is always api.anthropic.com (trusted).
        # For direct providers, this is the sub's provider_url.
        endpoint = "api.anthropic.com" if sub.get("auth_type") == "oauth" else sub.get("provider_url", "?")
        self.notify(f"Testing against {endpoint}…", title="Test", timeout=3)
        t0 = time.time()
        ok, reason = self.failover._test_direct_http(sub)
        elapsed = int((time.time() - t0) * 1000)
        status_code = 200 if ok else 401
        self.push_screen(HealthPopup("*current settings — response", status_code, elapsed, reason))
        self._test_results["__current__"] = {"code": status_code, "body": reason, "ts": time.time()}
        self._refresh_table()
        self._show_detail()

    def _build_event_log(self, sub_name: str, sub_id: str) -> str:
        """Return a mini event-log string for the detail panel (last 3 events)."""
        cutoff = time.time() - 3600  # 1-hour cap
        events: list[tuple[float, str]] = []  # (timestamp, rich_label)

        # 1. Last test result from in-memory cache
        tr = self._test_results.get(sub_id)
        if tr and tr["ts"] >= cutoff:
            icon = "✓" if tr["code"] == 200 else "✖"
            color = "green" if tr["code"] == 200 else "red"
            events.append((tr["ts"], f"[{color}]{icon} Tested {tr['code']}[/{color}]"))

        # 2. Events from failover.log — parsing delegated to FailoverManager
        events.extend(self.failover.recent_events(sub_name, since=cutoff))

        events = [(ts, label) for ts, label in events if ts >= cutoff]
        if not events:
            return ""

        events.sort(key=lambda e: e[0], reverse=True)
        lines = []
        for ts, label in events[:3]:
            if ts == float("inf"):
                lines.append(f"  {label}")
            else:
                ago = _time_ago(ts)
                lines.append(f"  {label}  [dim]{ago}[/dim]")

        # Separator width adapts to detail panel (~50 chars typical; 40 is safe minimum)
        try:
            panel_w = max(40, self.query_one("#detail").size.width - 4)
        except Exception:
            panel_w = 40
        sep = "─" * panel_w
        return f"\n[dim]{sep}[/dim]\n" + "\n".join(lines)

    def _show_detail(self):
        """Show details for selected subscription."""
        self._update_subtitle()
        detail = self.query_one("#detail", Static)
        sub_id = self._selected_id
        # Virtual row: current Claude settings don't match any saved subscription
        if sub_id == "__current__":
            self._show_current_settings_detail()
            return
        if not sub_id:
            if len(self.cm.subscriptions) == 0:
                detail.update(
                    "[bold yellow]Welcome to Heimsense![/bold yellow]\n\n"
                    "No providers configured yet.\n\n"
                    "  [bold yellow]+[/bold yellow]   Add new subscription\n"
                    "  [bold yellow]h[/bold yellow]   Show all keyboard shortcuts\n\n"
                    "[dim]Supports: Claude Max (OAuth), DeepSeek,\n"
                    "GitHub Copilot, Gemini, OpenAI and Custom proxies[/dim]"
                )
            else:
                detail.update(
                    "[dim]No subscription selected\n\n"
                    "Press [bold]j[/bold]/[bold]k[/bold] or ↑/↓ to navigate\n"
                    "Press [bold]h[/bold] for help[/dim]"
                )
            self._set_context_sensitive(False)
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            detail.update("[red]Subscription not found[/red]")
            return
        self._set_context_sensitive(True)
        # Auto-fetch available models if never fetched for this sub
        if not sub.get("available_models") and "models_fetched_at" not in sub:
            self._fetch_models_bg(sub_id)
        status_info = self.im.get_status(sub_id)
        status = status_info.get("status", "unknown")
        sc = _status_color(status)
        is_active_now = sub_id == self._active_id
        if is_active_now:
            default_badge = " [bold green]▶ Active[/bold green]"
        else:
            default_badge = ""
        is_oauth = sub.get("auth_type") in ("oauth", "direct")
        pid = status_info.get("pid") or "-"
        uptime = status_info.get("uptime") or "-"
        port = self.cm.get_instance_port(sub_id) or "?"
        pm2_name = self.cm.get_pm2_name(sub_id)

        # Update toggle button: Start (stopped) / Stop (online)
        toggle_btn = self.query_one("#toggle", Button)
        is_online = status in ("online", "starting")
        toggle_btn.label = "Stop" if is_online else "Start"
        toggle_btn.variant = "error" if is_online else "success"

        # Last HTTP status — prefer direct test result over PM2 log
        test_res = self._test_results.get(sub_id)
        http_stat = status_info.get("last_http_status")
        http_time = status_info.get("last_http_time")

        if test_res:
            # Direct test result is most recent source
            code = test_res["code"]
            stat_color = "green" if code == 200 else ("yellow" if 400 <= code < 500 else "red")
            ago = _time_ago(test_res["ts"])
            # Show error description for non-200
            if code != 200:
                # Attempt to parse JSON error message
                desc = ""
                try:
                    body_data = json.loads(test_res["body"])
                    desc = body_data.get("error", {}).get("message", "") or body_data.get("message", "")
                except Exception:
                    desc = test_res["body"][:120] if test_res["body"] else ""
                if desc:
                    http_line = f"[{stat_color}]{code}[/{stat_color}] — {ago}\n[gray]{desc}[/gray]"
                else:
                    http_line = f"[{stat_color}]{code}[/{stat_color}] — {ago}"
            else:
                http_line = f"[{stat_color}]{code}[/{stat_color}] — {ago}"
        elif http_stat is None:
            http_line = "[gray]No HTTP yet[/gray]"
        else:
            stat_color = "green" if http_stat == 200 else ("yellow" if 400 <= http_stat < 500 else "red")
            ago = _time_ago(http_time)
            http_line = f"[{stat_color}]{http_stat}[/{stat_color}] — {ago}"

        # Show active force model (from settings.json) — only if default
        settings_env = self.sync._load_settings().get("env", {})
        sonnet_override = settings_env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "")
        haiku_override = settings_env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "")
        opus_override = settings_env.get("ANTHROPIC_DEFAULT_OPUS_MODEL", "")
        model_maps = sub.get("model_maps", {})

        # Always show subscription's model_maps — overrides only affect Force line (if default)
        haiku_display = model_maps.get('haiku', '').strip()
        sonnet_display = model_maps.get('sonnet', '').strip()
        opus_display = model_maps.get('opus', '').strip()
        is_active = sub_id == self._active_id
        is_forced = (is_active and sonnet_override and sonnet_override == haiku_override == opus_override
                     and sonnet_override not in model_maps.values())
        from rich.console import Group as RichGroup

        # Available models summary value
        available_models = sub.get("available_models", [])
        blacklisted_models = sub.get("blacklisted_models", [])
        if available_models:
            bl_count = len(blacklisted_models)
            bl_note = f" ({bl_count} blacklisted)" if bl_count else ""
            models_val = f"{len(available_models)} available{bl_note}"
        elif "models_fetched_at" in sub:
            models_val = "[dim]fetch failed[/dim]"
        else:
            models_val = "[dim]fetching…[/dim]"

        header_txt = Text.from_markup(f"[bold]{escape(sub['name'])}[/bold]{default_badge}\n\n")
        event_log = self._build_event_log(sub["name"], sub_id)
        event_txt = Text.from_markup(event_log) if event_log else Text("")

        if is_oauth:
            auth_type = sub.get("auth_type", "oauth")
            provider_url = sub.get("provider_url", "")
            if auth_type == "direct":
                provider_label = PROVIDER_URL_LABELS.get(provider_url, provider_url) or provider_url
            else:
                provider_label = "Claude Max (OAuth)"
            oauth_token = sub.get("api_key", "")
            token_prefix = oauth_token[:8] + "..." if oauth_token else "[red]not set[/red]"
            rows = [
                ("Provider", provider_label),
                ("Token", token_prefix),
            ]
            model_rows = [(t, v) for t, v in [("Haiku", haiku_display), ("Sonnet", sonnet_display), ("Opus", opus_display)] if v]
            if model_rows:
                rows.append(None)
                rows.extend(model_rows)
            if is_forced:
                rows.append(("Force", f"[yellow]{escape(sonnet_override)}[/yellow]"))
            rows.append(("Models", models_val))
            if sub.get("notes", "").strip():
                rows.append(("Notes", escape(sub["notes"])))
            detail.update(RichGroup(header_txt, _detail_table(rows), event_txt))
        else:
            provider_url = sub.get('provider_url', '-')
            provider_label = PROVIDER_URL_LABELS.get(provider_url, provider_url) or provider_url
            api_key_val = sub.get('api_key', '')
            pm2_id = status_info.get("pm2_id")
            pm2_label = f" (PM2 id {pm2_id})" if pm2_id is not None else ""
            pm2_name = status_info.get("pm2_name")
            rows = [
                ("Provider", provider_label),
                ("Status", f"[{sc}]{status}[/{sc}]{pm2_label}"),
            ]
            if port and is_online:
                rows.append(("Port", str(port)))
            if pm2_name:
                rows.append(("PM2", pm2_name))
            if api_key_val:
                rows.append(("Token", api_key_val[:12] + "..."))
            else:
                rows.append(("API Key env", sub.get('api_key_env', '-')))
            model_rows = [(t, v) for t, v in [("Haiku", haiku_display), ("Sonnet", sonnet_display), ("Opus", opus_display)] if v]
            if model_rows:
                rows.append(None)
                rows.extend(model_rows)
            if is_forced:
                rows.append(("Force", f"[yellow]{escape(sonnet_override)}[/yellow]"))
            rows += [
                ("Models", models_val),
                None,
                ("PM2 uptime", uptime),
                ("Last HTTP", http_line),
            ]
            if sub.get("notes", "").strip():
                rows.append(("Notes", escape(sub["notes"])))
            detail.update(RichGroup(header_txt, _detail_table(rows), event_txt))

    def _on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        """Arrow keys → update details."""
        self._selected_id = event.row_key.value
        self._show_detail()

    def on_key(self, event):
        """j/k/n/p as alternative to ↑/↓; 1-9 instant-activate; / open filter."""
        # Don't intercept keys when filter input is focused
        filter_input = self.query_one("#filter-input", Input)
        if self.focused is filter_input:
            if event.key == "escape":
                self._close_filter()
                event.stop()
            return

        table = self.query_one("#inst-table", DataTable)
        if event.key in ("j", "n"):
            table.action_cursor_down()
            event.stop()
        elif event.key in ("k", "p"):
            table.action_cursor_up()
            event.stop()
        elif event.key == "slash":
            # Open filter
            filter_input.add_class("active")
            filter_input.focus()
            event.stop()
        elif event.key.isdigit() and event.key != "0":
            # 1-9: instantly activate the N-th provider (1-based)
            row_idx = int(event.key) - 1
            if 0 <= row_idx < len(self._sorted_rows):
                target_id = self._sorted_rows[row_idx]
                if target_id != "__current__":
                    table.move_cursor(row=row_idx)
                    self._do_activate(target_id)
                    event.stop()

    def _close_filter(self):
        """Clear and hide the filter input."""
        filter_input = self.query_one("#filter-input", Input)
        filter_input.value = ""
        filter_input.remove_class("active")
        self._filter_text = ""
        self._refresh_table()
        self.query_one("#inst-table", DataTable).focus()

    def on_input_changed(self, event: Input.Changed):
        """Live-filter the provider list as the user types."""
        if event.input.id == "filter-input":
            self._filter_text = event.value
            self._refresh_table()

    def on_input_submitted(self, event: Input.Submitted):
        """Close filter on Enter — jump to first result."""
        if event.input.id == "filter-input":
            self._close_filter()

    # --- Actions ---

    def _run_proxy_inference(self, sub: dict, model: str, auth_type: str) -> dict:
        """Delegate to SyncManager.inference_test — single implementation shared w/ CLI."""
        return self.sync.inference_test(sub, model)

    @staticmethod
    def _run_proxy_test(port: int, api_key: str, model: str) -> dict:
        """Call proxy port exactly as Claude Code would.

        Returns dict with keys: code (int), body (str), elapsed (int ms).
        code=0 means connection error.
        """
        test_url = f"http://localhost:{port}/v1/messages"
        payload = json.dumps({
            "model": model,
            "max_tokens": 100,
            "stream": False,
            "messages": [{"role": "user", "content": "Tell me a fun fact about the universe in 2 sentences."}],
        })
        start_ts = time.time()
        try:
            req = urllib.request.Request(
                test_url,
                data=payload.encode(),
                headers={
                    "content-type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    http_code = resp.getcode()
                    raw = resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as http_err:
                http_code = http_err.code
                raw = http_err.read().decode("utf-8", errors="replace")
            elapsed = int((time.time() - start_ts) * 1000)
            body = extract_response_body(raw, http_code)
            log.info("Test %s: HTTP %d (%dms)", model, http_code, elapsed)
            return {"code": http_code, "body": body, "elapsed": elapsed}
        except Exception as e:
            elapsed = int((time.time() - start_ts) * 1000)
            log.warning("Test failed for port %s: %s (%dms)", port, e, elapsed)
            return {"code": 0, "body": f"Error: {e}", "elapsed": elapsed}

    async def action_test(self):
        """Test the selected subscription via HTTP health check.

        oauth/direct: GET /v1/models against the provider API directly.
        bearer/gh_token: POST /v1/messages against the local proxy port.
        *current settings: build a temporary sub-dict from active settings.json and test.
        """
        sub_id = self._selected_id
        if not sub_id:
            return

        # Virtual *current settings row — synthesise sub dict from active settings
        if sub_id == "__current__":
            self._test_current_settings()
            return

        sub = self.cm.get_subscription(sub_id)
        if not sub:
            return

        auth_type = sub.get("auth_type", "bearer")

        # Determine which (tier, model) pairs to test — uses shared TIER_FALLBACK_MODELS
        tiers_to_test = []
        for tier in ("haiku", "sonnet", "opus"):
            model = self.sync.resolve_model_for_tier(sub, tier) or TIER_FALLBACK_MODELS[tier]
            tiers_to_test.append((tier, model))

        modal = TestModal(sub["name"], tiers_to_test)

        def _save_results(results: list):
            """Called from thread when all tiers are done — update table on UI thread."""
            best = next((r for _, _, r in results if r["code"] == 200), None) or (results[-1][2] if results else {})
            self._test_results[sub_id] = {
                "code": best.get("code", 0),
                "body": best.get("body", ""),
                "ts": time.time(),
            }
            self._refresh_table()
            self._show_detail()

        def _do_tests():
            results = []
            for tier, model in tiers_to_test:
                self.call_from_thread(modal.set_running, tier)
                result = self._run_proxy_inference(sub, model, auth_type)
                self.call_from_thread(modal.set_result, tier, model, result)
                results.append((tier, model, result))
            self.call_from_thread(_save_results, results)
            return results

        self.run_worker(_do_tests, thread=True, description=f"test-{sub['name']}")
        await self.push_screen(modal)

    def action_start(self):
        """Start claude-mux for selected subscription."""
        sub_id = self._selected_id
        if not sub_id:
            self.notify("Select a subscription first", title="Start", timeout=3)
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            log.warning("action_start: subscription %s not found", sub_id)
            return
        try:
            result = self.im.start(sub_id)
            log.info("Starting %s on port %s", sub['name'], result['port'])
            # Clear previous test result on new start
            self._test_results.pop(sub_id, None)
            self.notify(f"{sub['name']} started on port {result['port']}",
                        title="Start", timeout=5)
            self._refresh_table()
            self._show_detail()
        except FileNotFoundError:
            log.warning("heimsense binary not found for %s", sub_id)
            self.notify(
                "heimsense not installed — bearer providers require it.\n"
                "Install: curl -fsSL https://raw.githubusercontent.com/cura-ai/claude-mux/main/install.sh | bash",
                title="heimsense missing",
                severity="error",
                timeout=10,
            )
        except Exception as e:
            log.exception("Error starting %s", sub_id)
            self.notify(f"Error: {e}", title="Start", timeout=5)

    def action_launch(self):
        """Sync: generate .env + sync settings for selected subscription."""
        sub_id = self._selected_id or self._active_id
        if not sub_id:
            self.notify("Select a subscription", title="Sync", timeout=3)
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            self.notify("Subscription not found", title="Sync", timeout=3)
            return
        try:
            is_oauth = sub.get("auth_type") in ("oauth", "direct")
            if not is_oauth:
                self.im.generate_env(sub_id)
            result = self.sync.sync_default(sub_id)
            label = "Activated" if is_oauth else "Settings synced"
            self.notify(
                f"{sub['name']} — {label}. Claude Code will use this provider.",
                title="Activate" if is_oauth else "Sync",
                timeout=4,
            )
        except Exception as e:
            self.notify(f"Sync failed: {e}", severity="error", timeout=5)
        self._refresh_table()
        self._show_detail()

    def action_stop(self):
        """Stop selected subscription."""
        sub_id = self._selected_id
        if not sub_id:
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            return
        msg = f"Stop {sub['name']}?"
        confirm = ConfirmModal("Stop Instance", msg)
        self.push_screen(confirm, self._on_stop_confirmed)

    def _on_stop_confirmed(self, confirmed: bool):
        if not confirmed:
            return
        sub_id = self._selected_id
        if not sub_id:
            return
        try:
            self.im.stop(sub_id)
            log.info("Stopped %s", sub_id)
            self.notify("Stopped", timeout=3)
        except Exception as e:
            log.exception("Stop failed for %s", sub_id)
            self.notify(f"Error: {e}", severity="error", timeout=5)
        self._refresh_table()
        self._show_detail()

    def action_logs(self):
        """Show PM2 logs for selected subscription."""
        sub_id = self._selected_id
        if not sub_id:
            return
        pm2_name = self.cm.get_pm2_name(sub_id)
        if not pm2_name:
            self.notify("No PM2 name for this subscription", severity="error", timeout=3)
            return
        self.push_screen(LogViewer(pm2_name))

    async def action_activate(self):
        """Activate selected subscription — health-check first, confirm if failing."""
        import asyncio
        sub_id = self._selected_id
        if not sub_id or sub_id == "__current__":
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            return
        current_active = self._active_id
        # Already active — just refresh
        if current_active == sub_id:
            self.notify(f"{sub['name']} is already active", timeout=3)
            return

        # Health-check before switching
        self.notify(f"Checking {sub['name']}…", title="Activating", timeout=3)
        loop = asyncio.get_running_loop()
        ok, reason = await loop.run_in_executor(None, self.failover.test_health, sub_id)

        if not ok:
            msg = (
                f"[bold red]Health check failed[/bold red]\n\n"
                f"{sub['name']}: {reason}\n\n"
                f"Activate anyway?"
            )
            confirm = ConfirmModal("Activate anyway?", msg)
            self.push_screen(confirm, lambda result, sid=sub_id: self._do_activate(sid) if result else None)
            return

        # Check passed — confirm if displacing another active provider
        if current_active and current_active != sub_id:
            cur_sub = self.cm.get_subscription(current_active)
            cur_name = cur_sub["name"] if cur_sub else current_active
            msg = f"Switch active provider?\n\nFrom: {cur_name}\nTo: {sub['name']}"
            confirm = ConfirmModal("Switch Provider", msg)
            self.push_screen(confirm, lambda ok, sid=sub_id: self._do_activate(sid) if ok else None)
        else:
            self._do_activate(sub_id)

    def _do_activate(self, sub_id: str):
        """Execute provider switch after optional confirmation."""
        try:
            self.sync.sync_default(sub_id)
            name = (self.cm.get_subscription(sub_id) or {}).get("name", sub_id)
            self.notify(f"Active: {name}", timeout=3)
        except Exception as e:
            self.notify(f"Error: {e}", severity="error", timeout=5)
        self._refresh_table()
        self._show_detail()
        # Fetch models in background if not yet cached
        sub = self.cm.get_subscription(sub_id)
        if sub and not sub.get("available_models"):
            self._fetch_models_bg(sub_id)

    def action_delete(self):
        """Delete selected subscription (confirm first)."""
        sub_id = self._selected_id
        log.info("action_delete called, _selected_id=%s", sub_id)
        if not sub_id:
            self.notify("No subscription selected", title="Delete", timeout=3)
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            log.warning("action_delete: sub %s not found", sub_id)
            self.notify("Subscription not found", title="Delete", timeout=3)
            return
        msg = f"Delete {sub['name']}?\nThis will also stop the PM2 process."
        confirm = ConfirmModal("Delete Subscription", msg)
        self.push_screen(confirm, lambda ok: self._do_delete(sub_id, sub["name"]) if ok else None)

    def _do_delete(self, sub_id: str, label: str):
        """Execute deletion (called after confirmation)."""
        log.info("Deleting subscription %s (%s)", sub_id, label)
        try:
            self.im.stop(sub_id)
        except Exception as e:
            log.warning("Stop failed during delete: %s", e)
        self.cm.delete_subscription(sub_id)
        self.notify(f"Deleted: {label}", timeout=3)
        self._selected_id = None
        self._refresh_table()
        self._show_detail()

    def action_edit(self):
        """Edit selected subscription via AddWizard in edit-mode."""
        sub_id = self._selected_id
        if not sub_id:
            self.notify("Select a subscription first", title="Edit", timeout=3)
            return
        # Virtual *current settings row — save as new subscription
        if sub_id == "__current__":
            self._save_current_settings()
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            return
        wizard = AddWizard(self.cm, existing_sub=sub)
        self.push_screen(wizard, self._on_wizard_done)

    def _save_current_settings(self):
        """Prompt for name and save *current settings as a new subscription."""
        self.push_screen(NameInputModal(), self._on_save_current_done)

    def _on_save_current_done(self, name: str | None):
        if not name:
            return
        settings_env = self.sync._load_settings().get("env", {})
        oauth_token = (
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            or settings_env.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        )
        base_url = settings_env.get("ANTHROPIC_BASE_URL", "")
        auth_token = settings_env.get("ANTHROPIC_AUTH_TOKEN", "")
        model_maps = {
            k: settings_env.get(f"ANTHROPIC_DEFAULT_{k.upper()}_MODEL", "")
            for k in ("haiku", "sonnet", "opus")
        }

        if oauth_token and not base_url:
            auth_type = "oauth"
            provider_url = ""
            api_key = oauth_token
        elif base_url and not base_url.startswith("http://localhost"):
            auth_type = "direct"
            provider_url = base_url
            api_key = auth_token
        else:
            auth_type = "bearer"
            provider_url = base_url
            api_key = auth_token

        sub = self.cm.add_subscription(
            name=name,
            provider_url=provider_url,
            api_key_env="",
            auth_type=auth_type,
            model_maps={k: v for k, v in model_maps.items() if v},
            api_key=api_key,
        )
        self.notify(f"Saved: {name}", timeout=3)
        self._selected_id = sub["id"]
        self._refresh_table()
        self._show_detail()

    def action_add(self):
        """Open wizard to add subscription."""
        wizard = AddWizard(self.cm)
        self.push_screen(wizard, self._on_wizard_done)

    def _on_wizard_done(self, result):
        if result:
            action = "Updated" if result.get("updated") else "Added"
            self.notify(f"{action}: {result['name']}", timeout=3)
            self._selected_id = result["id"]
            # Apply force_model to settings.json if this sub is the active one
            force_model = result.get("force_model", "__none__")
            sub_id = result["id"]
            if sub_id == self._active_id:
                self._apply_force_model(sub_id, force_model)
            self._refresh_table()
            self._show_detail()
            # Fetch available models in background
            self._fetch_models_bg(sub_id)

    def _apply_force_model(self, sub_id: str, force_model: str):
        """Re-sync settings.json after force_model change.

        Delegates to sync_default which is the single source of truth for
        writing model env vars (including force_model override logic).
        """
        try:
            self.sync.sync_default(sub_id)
        except Exception as e:
            log.warning("_apply_force_model: sync_default failed: %s", e)

    def _fetch_models_bg(self, sub_id: str):
        """Fetch available models for sub_id in a background thread.

        Uses Textual's run_worker so the UI stays responsive during the HTTP call.
        Refreshes the table once the fetch completes.
        """
        def _do_fetch():
            models = self.sync.fetch_available_models(sub_id)
            sub = self.cm.get_subscription(sub_id)
            name = sub["name"] if sub else sub_id
            if models:
                self.call_from_thread(
                    self.notify, f"{len(models)} models cached", title=f"{name} — Models", timeout=4
                )
            else:
                self.call_from_thread(
                    self.notify, "Model fetch failed (proxy not running?)",
                    title=f"{name} — Models", severity="warning", timeout=4
                )
            self.call_from_thread(self._refresh_table)
            self.call_from_thread(self._show_detail)

        self.run_worker(_do_fetch, thread=True)

    def on_key(self, event) -> None:
        """Intercept r/R at app level so it works even when an Input is focused."""
        from textual.widgets import Input
        focused = self.focused
        if isinstance(focused, Input) and event.key == "r":
            event.stop()
            self.action_restart()

    def _background_health_check(self):
        """Periodic health-check in background thread — auto-switches on failure.

        Also checks if RETRY_ORIGINAL_AFTER_SECS has passed since last failover
        and attempts to reactivate the original subscription if it works again.
        """
        def _run():
            # Attempt reactivation of original subscription after timeout
            if self.failover.should_retry_original():
                orig_id = self.failover._original_sub_id
                orig_sub = self.cm.get_subscription(orig_id)
                if orig_sub:
                    log.info("Health-check: attempting to reactivate original sub %s", orig_id)
                    try:
                        # Save current active sub BEFORE switching
                        current_sub = self.cm.get_subscription(self._active_id or self.sync.detect_active())
                        self.sync.sync_default(orig_id)
                        ok, reason = self.failover.test_health(orig_id)
                        if ok:
                            self.failover.reset_failures()
                            self.failover._log_failover_event(
                                current_sub,
                                orig_sub,
                                "auto-resume after timeout",
                            )
                            log.info("Health-check: reactivated original sub %s", orig_sub["name"])
                            self.call_from_thread(self._notify_resume, orig_sub["name"])
                            return
                        else:
                            # Still failing — reset timer so we try again after RETRY_ORIGINAL_AFTER_SECS
                            self.failover._failover_ts = time.time()
                    except Exception as e:
                        log.warning("Health-check: could not reactivate original: %s", e)
                        self.failover._failover_ts = time.time()

            active_id = self._active_id or self.sync.detect_active()
            if not active_id:
                return
            ok, reason = self.failover.test_health(active_id)
            if not ok:
                log.warning("Health-check failed for %s: %s — attempting failover", active_id, reason)
                self.call_from_thread(self._do_auto_failover, active_id, reason)
        threading.Thread(target=_run, daemon=True).start()

    def _notify_resume(self, name: str):
        """UI thread: notification about reactivation of original subscription."""
        self.notify(f"✓ Reactivated: {name} (auto-resume)", title="Failover", timeout=6)
        self._refresh_table()
        self._show_detail()

    def _do_auto_failover(self, failed_id: str, reason: str):
        """Run in UI thread: switch to next subscription."""
        sub = self.cm.get_subscription(failed_id)
        name = sub["name"] if sub else failed_id
        self.notify(f"⚠ {name} failed ({reason[:60]}) — attempting failover...",
                    title="Failover", severity="warning", timeout=8)
        new_id = self.failover.do_failover(failed_id, reason=reason)
        if new_id:
            new_sub = self.cm.get_subscription(new_id)
            new_name = new_sub["name"] if new_sub else new_id
            self.notify(f"✓ Switched to {new_name}", title="Failover", timeout=6)
        else:
            self.notify("No working subscription found!", title="Failover",
                        severity="error", timeout=10)
        self._refresh_table()
        self._show_detail()

    def action_help(self):
        """H / ?: show keyboard shortcuts."""
        self.push_screen(HelpModal())

    def action_failover_log(self):
        """L: show failover log in modal."""
        self.push_screen(FailoverLogModal(self.failover.FAILOVER_LOG))

    def _update_subtitle(self):
        """Update app subtitle with active subscription and failover status."""
        active_id = self._active_id or self.sync.detect_active()
        if active_id:
            sub = self.cm.get_subscription(active_id)
            name = sub["name"] if sub else active_id
            failed_count = len(self.failover._failed_subs)
            if failed_count:
                self.sub_title = f"Active: {name}  ⚠ {failed_count} failed"
            else:
                self.sub_title = f"Active: {name}"
        else:
            self.sub_title = "No active subscription"

    def action_failover_check(self):
        """Manual: test active subscription and failover if necessary."""
        active_id = self._active_id or self.sync.detect_active()
        if not active_id:
            self.notify("No active subscription", timeout=3)
            return
        sub = self.cm.get_subscription(active_id)
        self.notify(f"Testing {sub['name'] if sub else active_id}...", timeout=3)
        self.failover.reset_failures()
        self._background_health_check()

    def action_force_model(self):
        """F: force all model aliases to one model for active subscription."""
        sub_id = self._selected_id or self._active_id
        if not sub_id:
            self.notify("Select a subscription first", timeout=3)
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            return
        model_maps = sub.get("model_maps", {})
        available = list(dict.fromkeys(v for v in model_maps.values() if v))
        if not available:
            self.notify("No model maps configured", timeout=3)
            return
        # Current force: read from settings.json
        settings = self.sync._load_settings()
        current = settings.get("env", {}).get("ANTHROPIC_DEFAULT_SONNET_MODEL", "__none__")
        modal = ForceModelModal(current, available)
        self.push_screen(modal, self._on_force_model_done)

    def _on_force_model_done(self, model: str | None):
        if model is None:
            return  # canceled
        settings = self.sync._load_settings()
        env = settings.setdefault("env", {})
        if model == "__none__":
            # Remove force — restore model maps
            sub_id = self._selected_id or self._active_id
            sub = self.cm.get_subscription(sub_id) if sub_id else None
            model_maps = sub.get("model_maps", {}) if sub else {}
            for key, alias in [("haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                                ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
                                ("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL")]:
                if model_maps.get(key):
                    env[alias] = model_maps[key]
                else:
                    env.pop(alias, None)
            self.sync._save_settings(settings)
            self.notify("Force removed — model maps restored", timeout=4)
        else:
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = model
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model
            self.sync._save_settings(settings)
            self.notify(f"Force model: {model}", timeout=4)
        self._show_detail()

    def action_restart(self):
        """Z: restart the app (hotload) — os.execv replaces the process in-place."""
        args = [sys.executable, os.path.abspath(__file__), "--tui"]
        if self._selected_id:
            args += ["--selected", self._selected_id]
        os.execv(sys.executable, args)

    def action_quit_app(self):
        """Q: quit with confirmation if instances are running."""
        running = []
        for sub in self.cm.subscriptions:
            status = self.im.get_status(sub["id"])
            if status.get("status") in ("online", "starting"):
                running.append(sub["name"])
        if running:
            msg = "Running instances:\n" + "\n".join(f"• {n}" for n in running)
            confirm = ConfirmModal("Quit?", msg)
            self.push_screen(confirm, self._on_quit_confirmed)
        else:
            self.exit()

    def action_reauth(self):
        """Reauth: restart OAuth flow for selected Claude Max sub."""
        sub_id = self._selected_id
        if not sub_id:
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub or sub.get("auth_type") != "oauth":
            self.notify("Only OAuth subscriptions support reauth", timeout=3)
            return
        wizard = AddWizard(self.cm, existing_sub=sub, reauth=True)
        self.push_screen(wizard, self._on_wizard_done)

    def _on_quit_confirmed(self, result: bool):
        if result:
            self.exit()

    # --- Keyboard bindings ---

    # Minimal class-level bindings — footer is rebuilt dynamically in _build_bindings()
    BINDINGS = [
        ("r", "restart", "Reload"),
        ("d", "delete", "Delete"),
        ("q", "quit_app", "Quit"),
        ("h", "help", "Help"),
        # Keep all action bindings registered so keystrokes still work
        ("s", "toggle", ""),
        ("t", "test", ""),
        ("+", "add", ""),
        ("e", "edit", ""),
        ("l", "logs", ""),
        ("L", "failover_log", ""),
        ("x", "failover_check", ""),
        ("question_mark", "help", ""),
    ]

    def _set_context_sensitive(self, enabled: bool):
        """Show/hide buttons and bindings that require a selected subscription.
        Add button is always shown."""
        sub = self.cm.get_subscription(self._selected_id) if self._selected_id else None
        is_oauth = bool(sub and sub.get("auth_type") == "oauth")
        # Hide Activate button (and Activate footer) when already active in Claude
        is_active_now = bool(self._selected_id and self._selected_id == self._active_id)

        # Force model btn removed — force is setting in Edit wizard only
        self.query_one("#force_model", Button).display = False
        edit_btn = self.query_one("#edit", Button)
        edit_btn.display = enabled
        edit_btn.label = Text.from_markup("[bold yellow]E[/bold yellow]dit")

        # OAuth-specific buttons
        for btn_id in ("toggle", "test", "logs"):
            self.query_one(f"#{btn_id}", Button).display = enabled and not is_oauth

        # Reauth only for OAuth
        self.query_one("#reauth", Button).display = enabled and is_oauth

        # Launch (Activate/Sync) — hidden when provider is already the active one.
        # OAuth/direct: "Activate" — writes token to settings.json, no proxy needed.
        # Bearer/proxy: "Sync" — updates settings.json to point to local proxy port.
        launch_btn = self.query_one("#launch", Button)
        launch_btn.display = enabled and not is_active_now
        launch_btn.label = "Activate" if is_oauth else "Sync settings"

        # Update BINDINGS so only relevant ones show in footer
        self._refresh_footer()

    def _build_bindings(self, enabled: bool, is_oauth: bool, width: int) -> list:
        """Return context- and width-sensitive footer bindings.

        Buttons cover: Start/Stop, Test, Sync, Reauth, Force Model, Edit, Logs, + Add.
        Footer only shows what has NO button: Reload, Delete, Activate, Quit, Help, Failover.
        Activate is hidden when selected provider is already active in Claude.

        <80:  Reload · Del · Activate · Quit
        <120: Reload · Del · Activate · Quit · Help
        >=120: above + Failover · Failover Log
        """
        if width < 80:
            base = [("r", "restart", "↺Reload"), ("q", "quit_app", "Quit")]
            if enabled:
                base += [("d", "delete", "Del")]
        elif width < 120:
            base = [("r", "restart", "Reload"), ("q", "quit_app", "Quit"), ("h", "help", "Help")]
            if enabled:
                base += [("d", "delete", "Delete")]
        else:
            base = [("r", "restart", "Reload"), ("q", "quit_app", "Quit"), ("h", "help", "Help")]
            if enabled:
                base += [
                    ("d", "delete", "Delete"),
                    ("x", "failover_check", "Failover"),
                    ("L", "failover_log", "Failover Log"),
                ]
        return base

    def _refresh_footer(self) -> None:
        """Rebuild footer bindings using actual terminal width."""
        sub = self.cm.get_subscription(self._selected_id) if self._selected_id else None
        is_oauth = bool(sub and sub.get("auth_type") == "oauth")
        enabled = bool(self._selected_id)
        width = self.size.width or 120
        self.BINDINGS = self._build_bindings(enabled, is_oauth, width)
        self.refresh_bindings()

    def on_resize(self, event) -> None:
        """Re-render footer bindings when terminal is resized."""
        sub = self.cm.get_subscription(self._selected_id) if self._selected_id else None
        is_oauth = bool(sub and sub.get("auth_type") == "oauth")
        enabled = bool(self._selected_id)
        self.BINDINGS = self._build_bindings(enabled, is_oauth, event.size.width or 120)
        self.refresh_bindings()

    # --- Button handlers ---

    async def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "toggle":
            self.action_toggle()
        elif event.button.id == "launch":
            self.action_launch()
        elif event.button.id == "test":
            await self.action_test()
        elif event.button.id == "force_model":
            self.action_force_model()
        elif event.button.id == "logs":
            self.action_logs()
        elif event.button.id == "edit":
            self.action_edit()
        elif event.button.id == "reauth":
            self.action_reauth()
        elif event.button.id == "cancel_hotload":
            self.action_cancel_hotload()
        elif event.button.id == "add":
            self.action_add()

    def action_toggle(self):
        """Start/Stop toggle for selected subscription."""
        sub_id = self._selected_id
        if not sub_id:
            self.notify("Select a subscription first", title="Toggle", timeout=3)
            return
        status = self.im.get_status(sub_id).get("status", "unknown")
        if status in ("online", "starting"):
            self.action_stop()
        else:
            self.action_start()


# --- Entry points ---

def _restore_terminal() -> None:
    """Restore terminal to normal state after TUI exit or crash.

    Textual enables kitty keyboard protocol, mouse tracking, and alternate
    screen buffer. stty sane only resets TTY line discipline — it does NOT
    disable these application-layer protocols. We must send the escape
    sequences explicitly.
    """
    try:
        import sys as _sys
        _sys.stdout.write(
            "\x1b[?1049l"   # leave alternate screen (restore main screen buffer)
            "\x1b[?1000l"   # disable mouse click tracking
            "\x1b[?1002l"   # disable mouse button+motion tracking
            "\x1b[?1003l"   # disable all mouse events
            "\x1b[?1006l"   # disable SGR extended mouse mode
            "\x1b[<u"       # pop kitty keyboard protocol (restore previous state)
            "\x1b[?25h"     # show cursor
        )
        _sys.stdout.flush()
    except Exception:
        pass
    try:
        import subprocess as _sp
        _sp.run(["stty", "sane"], check=False)
    except Exception:
        pass


def run_tui():
    """Start Heimsense TUI."""
    log.info("=== Heimsense TUI starting ===")

    # Global exception hook — catch anything not caught by Textual
    def _global_excepthook(exc_type, exc_value, exc_tb):
        log.exception("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        print(f"FATAL: {exc_type.__name__}: {exc_value}", file=sys.stderr)
    sys.excepthook = _global_excepthook

    # Parse --selected <sub_id> from hotload
    initial_selected: str | None = None
    if "--selected" in sys.argv:
        idx = sys.argv.index("--selected")
        if idx + 1 < len(sys.argv):
            initial_selected = sys.argv[idx + 1]
    try:
        cm = ConfigManager()
        app = HeimsenseApp(cm, initial_selected=initial_selected)
        app.run()
    except Exception as e:
        log.exception("Fatal error in TUI")
        print(f"FATAL: {e}", file=sys.stderr)
        raise
    finally:
        _restore_terminal()


# --- Standalone test ---
if __name__ == "__main__":

    if "--tui" in sys.argv:
        run_tui()
        sys.exit(0)

    cm = ConfigManager()

    if "--setup-test" in sys.argv:
        # Create a test-subscription and set as default
        os.environ["TEST_API_KEY"] = "sk-test-456"
        sub = cm.add_subscription(
            name="test",
            provider_url="https://api.test.dev/v1",
            api_key_env="TEST_API_KEY",
            label="Test Provider",
            auth_type="bearer",
            model_maps={"haiku": "test-mini", "sonnet": "test-medium", "opus": "test-max"},
            notes="TUI test",
        )
        print(f"Created: {sub['name']} (id={sub['id'][:8]}..., port={sub['default_port']})")
        im = InstanceManager(cm)
        im.generate_env(sub["id"])
        print(f"Active provider: {sub['name']}")
        print(f".env generated: {CLAUDE_MUX_DIR / 'instances' / sub['name'] / '.env'}")
        print("OK: Setup complete")

    elif "--test-all" in sys.argv:
        # Test 1: ConfigManager
        initial_count = len(cm.subscriptions)
        sub = cm.add_subscription("test-all", "https://test.all", "TEST_KEY")
        assert len(cm.subscriptions) == initial_count + 1
        assert cm.get_subscription(sub["id"]) is not None
        cm.delete_subscription(sub["id"])
        assert len(cm.subscriptions) == initial_count
        print("OK: ConfigManager")

        # Test 2: InstanceManager (env file generation)
        sub = cm.add_subscription("im-test", "https://im.test", "IM_KEY")
        im = InstanceManager(cm)
        im._regenerate_ecosystem()

        # Verify API key and default values are written to .env (Fund 1+2 fix)
        os.environ["IM_KEY"] = "sk-test-123"
        env_path = im.generate_env(sub["id"])
        assert env_path.exists()
        assert env_path.stat().st_mode & 0o777 == 0o600
        env_text = env_path.read_text()
        assert "ANTHROPIC_API_KEY=sk-test-123" in env_text, \
            f"API key missing in .env:\n{env_text}"
        assert "REQUEST_TIMEOUT_MS=120000" in env_text, \
            f"Default value Request timeout missing:\n{env_text}"
        assert "MAX_TOKENS=4096" in env_text, \
            f"Default value Max tokens missing:\n{env_text}"
        print("OK: InstanceManager (+ API key + defaults verified)")
        cm.delete_subscription(sub["id"])

        # Test 3: SyncManager
        sub = cm.add_subscription("sync-test", "https://sync.test", "SYNC_KEY")
        sync = SyncManager(cm)
        result = sync.sync_default(sub["id"])
        assert result["default"] == "sync-test"
        assert result["base_url"].startswith("http://localhost:")
        assert "ANTHROPIC_BASE_URL" in result["keys_updated"]
        cm.delete_subscription(sub["id"])
        print("OK: SyncManager")

        print("\n=== ALL TESTS PASSED ===")

    else:
        # Quick smoke test
        initial_count = len(cm.subscriptions)
        sub = cm.add_subscription("smoke", "https://smoke.test", "SMOKE_KEY")
        assert len(cm.subscriptions) == initial_count + 1
        cm.delete_subscription(sub["id"])
        assert len(cm.subscriptions) == initial_count
        print("OK: Smoke test passed")

