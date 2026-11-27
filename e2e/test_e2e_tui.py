"""E2E: the Textual TUI, driven by key presses and clicks (Textual Pilot).

The app runs against the isolated HOME with the real ConfigManager, real
subprocess calls to the pm2 stand-in (which really starts the proxy), real
HTTP to the fake provider, and real writes to Claude Code's settings.json.
Test setup uses the real CLI, just like a user with a second terminal.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from rich.console import Console

from claude_mux.config import ConfigManager
import claude_mux.tui as ui
from claude_mux.tui import (AddWizard, ConfirmModal, FailoverLogModal, HealthPopup, HeimsenseApp,
                            HelpModal, LogViewer, NameInputModal)
from e2e_support import stubs
from e2e_support.fake_upstream import FakeUpstream, Reply
from e2e_support.helpers import (config, home, instance_port, run_cli, settings, sub_named, tui_test,
                                 until, write_settings)


class RecordingApp(HeimsenseApp):
    """The real app; notifications are additionally recorded so tests can read them after
    the toast has timed out."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.notices: list[str] = []

    def notify(self, message, *a, **kw):
        self.notices.append(str(message))
        return super().notify(message, *a, **kw)


@asynccontextmanager
async def tui(size=(150, 45)):
    app = RecordingApp(ConfigManager())
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.2)
        yield app, pilot
        # Let queued UI messages (e.g. a RowHighlighted from the last table refresh) run
        # before shutdown; a late one during teardown races with widget removal.
        if app.is_running:
            await pilot.pause(0.2)


def plain(renderable) -> str:
    console = Console(width=160, file=io.StringIO(), record=True, color_system=None)
    console.print(renderable)
    return console.export_text()


def detail(app) -> str:
    """The detail panel as plain text, runs of spaces collapsed ("Status: online")."""
    text = plain(app.query_one("#detail").content)
    return "\n".join(re.sub(r"\s+", " ", line).strip() for line in text.splitlines())


def rows(app) -> list[str]:
    table = app.query_one("#inst-table")
    return [table.get_row_at(i)[1].plain for i in range(table.row_count)]


def row_dots(app) -> list[tuple[str, str]]:
    table = app.query_one("#inst-table")
    return [(table.get_row_at(i)[0].plain, str(table.get_row_at(i)[0].style)) for i in range(table.row_count)]


def visible_buttons(app) -> set[str]:
    return {b.id for b in app.query("Button") if b.display and b.screen is app.screen}


async def press_button(app, pilot, button_id: str):
    """Press a visible button. (Pilot clicks can land on a notification toast that
    overlaps the button grid, so the button's own press() is used.)"""
    button = app.screen.query_one(f"#{button_id}")
    assert button.display, f"#{button_id} is not shown"
    button.press()
    await pilot.pause(0.1)


def static_text(screen, selector) -> str:
    return plain(screen.query_one(selector).content)


def add_direct(name, url, key="direct-key", *extra):
    r = run_cli("add", "-n", name, "-u", url, "-K", key, "--auth", "direct", *extra)
    assert r.code == 0, r.output


@pytest.fixture
def deepseek(upstream, monkeypatch):
    monkeypatch.setenv("DS_KEY", "sk-deepseek-123")
    r = run_cli("add", "-n", "deepseek", "-u", f"{upstream.url}/v1", "-k", "DS_KEY",
                "--haiku", "deepseek-chat", "--sonnet", "deepseek-chat", "--opus", "deepseek-reasoner")
    assert r.code == 0


@pytest.fixture
def upstream2():
    srv = FakeUpstream()
    yield srv
    srv.close()


# =============================================================================
# A. Startup, provider table and detail panel
# =============================================================================

@tui_test
async def test_first_start_shows_welcome_and_setup_tip():
    async with tui() as (app, pilot):
        text = detail(app)
        assert "Welcome to Heimsense!" in text and "No providers configured yet." in text
        assert rows(app) == []
        assert app.sub_title == "No active subscription"
        await until(pilot, lambda: any("claude-mux init" in n for n in app.notices), what="setup tip")
        # without a selection only the Add button is offered
        assert visible_buttons(app) == {"add"}
        assert "Claude Mux — Use any LLM inside Claude Code" in plain(app.query_one("#app-header").content)


@tui_test
async def test_no_setup_tip_once_status_line_is_installed():
    assert run_cli("init").code == 0
    async with tui() as (app, pilot):
        await pilot.pause(0.3)
        assert not any("claude-mux init" in n for n in app.notices)


@tui_test
async def test_table_lists_active_provider_first_and_shows_its_details(upstream, upstream2):
    add_direct("zeta", upstream.url, "zeta-key-1234567890", "--sonnet", "glm-4.7")
    add_direct("alpha", upstream2.url)
    add_direct("mid", f"{upstream2.url}/mid")
    run_cli("activate", "zeta")
    async with tui() as (app, pilot):
        assert rows(app) == ["▶ zeta", "alpha", "mid"]
        assert app.sub_title == "Active: zeta"
        text = detail(app)
        assert "zeta ▶ Active" in text
        assert f"Provider: {upstream.url}" in text
        assert "Token: zeta-key..." in text and "zeta-key-1234567890" not in text, "token is masked"
        assert "Sonnet: glm-4.7" in text
        assert "Models: 3 available" in text
        # already active, so no Sync button
        assert visible_buttons(app) == {"add", "edit", "toggle", "test", "logs"}


@tui_test
async def test_arrow_keys_select_provider_and_update_details(upstream, upstream2, deepseek):
    add_direct("zai", upstream2.url)
    async with tui() as (app, pilot):
        assert rows(app) == ["deepseek", "zai", "▶ *current settings"], "nothing active yet"
        text = detail(app)
        assert "deepseek" in text and "API Key env: DS_KEY" in text
        assert "Status: stopped" in text and "Last HTTP: No HTTP yet" in text
        assert "Opus: deepseek-reasoner" in text
        assert "Models: fetch failed" in text
        assert visible_buttons(app) == {"add", "toggle", "test", "launch", "edit", "logs"}
        assert str(app.query_one("#toggle").label) == "Start"

        await pilot.press("down")
        assert app.query_one("#inst-table").cursor_row == 1
        assert detail(app).startswith("zai")
        await pilot.press("up")
        assert detail(app).startswith("deepseek")


@tui_test
async def test_known_provider_urls_get_a_friendly_label(monkeypatch):
    monkeypatch.setenv("DS_KEY", "k")
    run_cli("add", "-n", "ds", "-u", "https://api.deepseek.com/v1", "-k", "DS_KEY")
    async with tui() as (app, pilot):
        assert "Provider: DeepSeek" in detail(app)


@tui_test
async def test_unsaved_claude_settings_show_as_current_settings_row(upstream):
    add_direct("zai", upstream.url)
    write_settings({"env": {"ANTHROPIC_BASE_URL": "https://llm.example.com/anthropic",
                            "ANTHROPIC_AUTH_TOKEN": "sk-unsaved-token-abcdef",
                            "ANTHROPIC_DEFAULT_SONNET_MODEL": "custom-sonnet"}})
    async with tui() as (app, pilot):
        assert rows(app) == ["zai", "▶ *current settings"]
        await pilot.press("down")
        text = detail(app)
        assert "*current settings" in text and "Claude is using settings not saved in claude-mux." in text
        assert "Auth: direct" in text
        assert "Base URL: https://llm.example.com/anthropic" in text
        assert "Key: sk-unsaved-t…" in text and "sk-unsaved-token-abcdef" not in text
        assert "Sonnet: custom-sonnet" in text
        assert "Not tested" in text
        assert visible_buttons(app) == {"add", "test", "edit"}
        assert str(app.query_one("#edit").label) == "Save as…"


@tui_test
async def test_activation_from_another_terminal_is_picked_up(upstream, upstream2):
    add_direct("zai", upstream.url)
    add_direct("late", upstream2.url)
    async with tui() as (app, pilot):
        assert rows(app) == ["late", "zai", "▶ *current settings"]
        run_cli("activate", "late")
        await until(pilot, lambda: rows(app) == ["▶ late", "zai"], timeout=12, what="periodic refresh")


@tui_test
async def test_provider_added_from_another_terminal_appears(upstream, upstream2):
    add_direct("zai", upstream.url)
    async with tui() as (app, pilot):
        add_direct("late", upstream2.url)
        await until(pilot, lambda: "late" in rows(app), timeout=12, what="periodic refresh")


@tui_test
async def test_tui_does_not_discard_providers_added_from_another_terminal(upstream, upstream2):
    add_direct("zai", upstream.url)
    add_direct("other", f"{upstream.url}/other")
    async with tui() as (app, pilot):
        add_direct("late", upstream2.url)          # user adds a provider in a second terminal
        await pilot.press("d", "y")                # ... then deletes 'other' in the TUI
        assert sorted(s["name"] for s in config()["subscriptions"]) == ["late", "zai"]


@tui_test
async def test_starts_without_pm2_installed(monkeypatch, tmp_path, deepseek):
    """Regression: pm2 is optional (install.sh, QUICKSTART). Without it the TUI crashed on start
    with FileNotFoundError as soon as one proxy provider existed."""
    nopm2 = tmp_path / "bin"
    nopm2.mkdir()
    for name in ("claude", "gh", "heimsense"):
        (nopm2 / name).symlink_to(Path(os.environ["HEIMSENSE_BIN"]).parent / name)
    monkeypatch.setenv("PATH", os.pathsep.join([str(nopm2), str(Path(sys.executable).parent)]))
    assert shutil.which("pm2") is None
    async with tui() as (app, pilot):
        assert rows(app) == ["deepseek", "▶ *current settings"]
        assert row_dots(app)[0][0] == "?"
        assert "Status: unknown" in detail(app)
        await pilot.press("q")
    assert app.return_code == 0


# =============================================================================
# B. Actions
# =============================================================================

@tui_test
async def test_help_opens_and_closes():
    async with tui() as (app, pilot):
        for open_key, close_key in (("h", "q"), ("question_mark", "escape"), ("h", "h")):
            await pilot.press(open_key)
            assert isinstance(app.screen, HelpModal)
            assert "Keyboard Shortcuts" in static_text(app.screen, "#help-text")
            await pilot.press(close_key)
            assert not isinstance(app.screen, HelpModal)
        await pilot.press("h")
        await pilot.click("#close-help")
        assert not isinstance(app.screen, HelpModal)


@tui_test
async def test_delete_asks_for_confirmation(upstream, upstream2):
    add_direct("alpha", upstream.url)
    add_direct("beta", upstream2.url)
    async with tui() as (app, pilot):
        await pilot.press("d")
        assert isinstance(app.screen, ConfirmModal)
        assert "Delete alpha?" in plain(app.screen.query_one("Static").content)
        await pilot.press("n")
        assert [s["name"] for s in config()["subscriptions"]] == ["alpha", "beta"]

        await pilot.press("d", "y")
        assert [s["name"] for s in config()["subscriptions"]] == ["beta"]
        assert rows(app) == ["beta", "▶ *current settings"]
        assert "Deleted: alpha" in app.notices
        assert ["delete", "claude-mux-alpha"] in stubs.pm2_calls(home()), "PM2 process is removed too"


@tui_test
async def test_sync_button_activates_provider_for_claude_code(upstream, upstream2):
    add_direct("alpha", upstream.url, "alpha-key")
    add_direct("beta", upstream2.url, "beta-key", "--opus", "beta-opus")
    run_cli("activate", "alpha")
    async with tui() as (app, pilot):
        await pilot.press("down")
        assert detail(app).startswith("beta")
        assert "launch" in visible_buttons(app)
        await press_button(app, pilot, "launch")
        env = settings()["env"]
        assert (env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_AUTH_TOKEN"]) == (upstream2.url, "beta-key")
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "beta-opus"
        assert rows(app) == ["▶ beta", "alpha"]
        assert "beta ▶ Active" in detail(app)
        assert "launch" not in visible_buttons(app), "already active"
        assert "beta — Activated. Claude Code will use this provider." in app.notices
    assert run_cli("active").stdout.strip() == "beta"


@tui_test
async def test_proxy_lifecycle_start_test_logs_stop(upstream, deepseek):
    async with tui() as (app, pilot):
        # --- start: pm2 really launches the proxy --------------------------------
        await pilot.press("s")
        port = instance_port("deepseek")
        assert f"deepseek started on port {port}" in app.notices
        assert stubs.pm2_state(home())["claude-mux-deepseek"]["argv"][0] == "bash"
        await until(pilot, lambda: "Status: online" in detail(app), what="online status")
        assert f"Port: {port}" in detail(app)
        assert str(app.query_one("#toggle").label) == "Stop"

        # --- test: three tiers through the running proxy -------------------------
        await pilot.press("t")
        assert isinstance(app.screen, ui.TestModal)
        modal = app.screen
        await until(pilot, lambda: all("✓ OK" in static_text(modal, f"#test-status-{t}")
                                       for t in ("haiku", "sonnet", "opus")), timeout=20, what="tier results")
        assert "deepseek-reasoner" in static_text(modal, "#test-status-opus")
        assert "Hello from fake upstream (deepseek-chat)" in static_text(modal, "#test-status-haiku")
        sent = upstream.received("POST", "/v1/messages")
        assert [r.json()["model"] for r in sent][-3:] == ["deepseek-chat", "deepseek-chat", "deepseek-reasoner"]
        assert all(r.headers["authorization"] == "Bearer sk-deepseek-123" for r in sent)
        await pilot.press("enter")
        assert not isinstance(app.screen, ui.TestModal)
        assert "Last HTTP: 200" in detail(app)
        assert row_dots(app)[0] == ("●", "green")

        # --- logs: the proxy's request log ---------------------------------------
        await pilot.press("l")
        assert isinstance(app.screen, LogViewer)
        log_text = static_text(app.screen, "#log-content")
        assert "── out.log ──" in log_text and '"msg": "http request"' in log_text
        await pilot.press("q")
        assert not isinstance(app.screen, LogViewer)

        # --- stop (with confirmation) --------------------------------------------
        await pilot.press("s")
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("y")
        assert "Stopped" in app.notices
        assert "claude-mux-deepseek" not in stubs.pm2_state(home())
        assert instance_port("deepseek") is None
        assert "Status: stopped" in detail(app)


@tui_test
async def test_test_modal_shows_failing_tier(upstream):
    add_direct("zai", upstream.url)
    upstream.fail_models("claude-opus-4-6", status=429, message="Opus quota exhausted")
    async with tui() as (app, pilot):
        await press_button(app, pilot, "test")
        modal = app.screen
        assert isinstance(modal, ui.TestModal)
        await until(pilot, lambda: modal.query("#test-status-opus"), what="test modal composed")
        await until(pilot, lambda: "429" in static_text(modal, "#test-status-opus"), timeout=20, what="opus result")
        assert "✖ 429" in static_text(modal, "#test-status-opus")
        assert "rate_limit_error" in static_text(modal, "#test-status-opus")
        await until(pilot, lambda: "✓ OK" in static_text(modal, "#test-status-sonnet"), what="sonnet result")
        await pilot.click("#close-test")
        assert not isinstance(app.screen, ui.TestModal)


@tui_test
async def test_start_is_refused_for_direct_provider(upstream):
    add_direct("zai", upstream.url)
    async with tui() as (app, pilot):
        await pilot.press("s")
        assert any("uses direct API access — no proxy needed" in n for n in app.notices)
        assert stubs.pm2_state(home()) == {}


@tui_test
async def test_manual_failover_check_switches_to_working_provider(upstream, upstream2):
    add_direct("primary", upstream.url, "primary-key")
    add_direct("backup", upstream2.url, "backup-key")
    run_cli("activate", "primary")
    upstream.route("GET", "/v1/models", Reply(429, json={"error": {"message": "Rate limit reached"}}))
    async with tui() as (app, pilot):
        await pilot.press("x")
        await until(pilot, lambda: any(n.startswith("✓ Switched to backup") for n in app.notices),
                    timeout=15, what="failover")
        assert any("primary failed (HTTP 429 — Rate limit reached)" in n for n in app.notices)
        assert settings()["env"]["ANTHROPIC_AUTH_TOKEN"] == "backup-key"
        assert rows(app) == ["▶ backup", "primary ⚠"]
        assert app.sub_title == "Active: backup  ⚠ 1 failed"

        await pilot.press("L")
        assert isinstance(app.screen, FailoverLogModal)
        assert "FROM=primary  TO=backup" in static_text(app.screen, "#fl-content")
        await pilot.press("q")
        assert not isinstance(app.screen, FailoverLogModal)


@tui_test
async def test_failover_check_keeps_healthy_provider(upstream, upstream2):
    add_direct("primary", upstream.url)
    add_direct("backup", upstream2.url)
    run_cli("activate", "primary")
    async with tui() as (app, pilot):
        await pilot.press("x")
        # one GET /v1/models came from `add`, the next one is the health check
        await until(pilot, lambda: len(upstream.received("GET", "/v1/models")) >= 2, what="health check")
        await pilot.pause(0.3)
        assert settings()["env"]["ANTHROPIC_BASE_URL"] == upstream.url
        assert not any("Switched" in n for n in app.notices)
        await pilot.press("L")
        assert "No failover events yet." in static_text(app.screen, "#fl-content")


@tui_test
async def test_save_current_settings_as_new_subscription(upstream):
    add_direct("zai", f"{upstream.url}/zai")
    write_settings({"env": {"ANTHROPIC_BASE_URL": upstream.url,
                            "ANTHROPIC_AUTH_TOKEN": "sk-imported",
                            "ANTHROPIC_DEFAULT_OPUS_MODEL": "big-model"}})
    async with tui() as (app, pilot):
        await pilot.press("down", "e")
        assert isinstance(app.screen, NameInputModal)
        await pilot.press(*"imported", "enter")
        sub = sub_named("imported")
        assert (sub["auth_type"], sub["provider_url"], sub["api_key"]) == ("direct", upstream.url, "sk-imported")
        assert sub["model_maps"] == {"opus": "big-model"}
        assert "Saved: imported" in app.notices
        assert rows(app) == ["▶ imported", "zai"], "the saved subscription is recognised as active"


@tui_test
async def test_cancel_save_current_settings(upstream):
    add_direct("zai", f"{upstream.url}/zai")
    write_settings({"env": {"ANTHROPIC_BASE_URL": upstream.url, "ANTHROPIC_AUTH_TOKEN": "t"}})
    async with tui() as (app, pilot):
        await pilot.press("down", "e")
        await pilot.press("escape")
        assert [s["name"] for s in config()["subscriptions"]] == ["zai"]


@pytest.mark.parametrize("status, shown", [(200, "HTTP 200"), (401, "HTTP 401")])
@tui_test
async def test_test_current_settings(upstream, status, shown):
    add_direct("zai", f"{upstream.url}/zai")
    upstream.route("GET", "/v1/models", Reply(status, json={"data": [], "error": {"message": "bad key"}}))
    write_settings({"env": {"ANTHROPIC_BASE_URL": upstream.url, "ANTHROPIC_AUTH_TOKEN": "sk-current"}})
    async with tui() as (app, pilot):
        await pilot.press("down", "t")
        await until(pilot, lambda: isinstance(app.screen, HealthPopup), what="result popup")
        assert shown in app.screen.query_one("#health-popup-body").text
        assert upstream.received("GET", "/v1/models")[-1].headers["authorization"] == "Bearer sk-current"
        await pilot.click("#ok")
        expected_dot = ("●", "green") if status == 200 else ("✖", "red")
        assert row_dots(app)[-1] == expected_dot
        assert ("✓ OK" if status == 200 else "✖ 401") in detail(app)


@tui_test
async def test_edit_opens_wizard_for_selected_provider(upstream):
    add_direct("zai", upstream.url)
    async with tui() as (app, pilot):
        await pilot.press("e")
        assert isinstance(app.screen, AddWizard)
        await pilot.press("escape")
        assert not isinstance(app.screen, AddWizard)
        await pilot.press("plus")
        assert isinstance(app.screen, AddWizard)
        await pilot.press("escape")
        assert sub_named("zai")["provider_url"] == upstream.url


@tui_test
async def test_quit_without_running_proxies():
    async with tui() as (app, pilot):
        await pilot.press("q")
    assert app.return_code == 0


@tui_test
async def test_quit_asks_while_a_proxy_is_running(upstream, deepseek):
    async with tui() as (app, pilot):
        await pilot.press("s")
        await until(pilot, lambda: "Status: online" in detail(app), what="online status")
        await pilot.press("q")
        assert isinstance(app.screen, ConfirmModal)
        assert "• deepseek" in plain(app.screen.query_one("Static").content)
        await pilot.press("n")
        assert app.is_running
        await pilot.press("q", "y")
    assert app.return_code == 0


# =============================================================================
# Keyboard shortcuts from the README / help screen (regression: a second on_key
# definition used to replace the handler, so none of these worked)
# =============================================================================

@tui_test
async def test_j_and_k_move_the_selection(upstream, upstream2):
    add_direct("alpha", upstream.url)
    add_direct("beta", upstream2.url)
    async with tui() as (app, pilot):
        await pilot.press("j")
        assert detail(app).startswith("beta")
        await pilot.press("k")
        assert detail(app).startswith("alpha")


@tui_test
async def test_number_key_activates_provider(upstream, upstream2):
    add_direct("alpha", upstream.url)
    add_direct("beta", upstream2.url, "beta-key")
    async with tui() as (app, pilot):
        await pilot.press("2")
        await pilot.pause(0.3)
    assert run_cli("active").stdout.strip() == "beta"


@tui_test
async def test_slash_filters_provider_list(upstream, upstream2):
    add_direct("alpha", upstream.url)
    add_direct("beta", upstream2.url)
    async with tui() as (app, pilot):
        await pilot.press("slash", *"bet")
        assert rows(app) == ["beta"]


@tui_test
async def test_enter_activates_selected_provider(upstream, upstream2):
    add_direct("alpha", upstream.url)
    add_direct("beta", upstream2.url, "beta-key")
    run_cli("activate", "alpha")
    async with tui() as (app, pilot):
        await pilot.press("down", "enter")              # switches immediately, no dialog
        assert not isinstance(app.screen, ConfirmModal)
        assert settings()["env"]["ANTHROPIC_AUTH_TOKEN"] == "beta-key"
        assert rows(app) == ["▶ beta", "alpha"]
        assert "Active: beta" in app.notices


@tui_test
async def test_enter_on_current_settings_row_does_nothing(upstream):
    add_direct("alpha", upstream.url)
    write_settings({"env": {"ANTHROPIC_BASE_URL": "https://elsewhere.example", "ANTHROPIC_AUTH_TOKEN": "t"}})
    async with tui() as (app, pilot):
        await pilot.press("down", "enter", "2")
        assert settings()["env"]["ANTHROPIC_BASE_URL"] == "https://elsewhere.example"


@tui_test
async def test_enter_on_stopped_proxy_provider_explains_instead_of_breaking_claude(upstream, deepseek):
    add_direct("zai", f"{upstream.url}/zai")
    run_cli("activate", "zai")
    async with tui() as (app, pilot):
        await pilot.press("down")
        assert detail(app).startswith("deepseek")
        await pilot.press("enter")
        assert any("local proxy that is not running — start it first" in n for n in app.notices)
        assert run_cli("active").stdout.strip() == "zai"
        await pilot.pause(0.5)  # background model fetch + table rebuilds
        assert detail(app).startswith("deepseek"), "selection must not jump to another provider"
        await press_button(app, pilot, "launch")         # the Sync button is refused the same way
        assert any(n.startswith("Sync failed:") and "not running" in n for n in app.notices)
        # start it, then Enter works
        await pilot.press("s")
        await until(pilot, lambda: "Status: online" in detail(app), what="online status")
        await pilot.press("enter")
        assert run_cli("active").stdout.strip() == "deepseek"


@tui_test
async def test_selection_survives_periodic_refresh(upstream, upstream2):
    """The selection (and what Enter activates) stays put across the 5 s table refresh."""
    add_direct("alpha", upstream.url)
    add_direct("beta", upstream2.url)
    add_direct("gamma", f"{upstream2.url}/g")
    async with tui() as (app, pilot):
        await pilot.press("down", "down")
        assert detail(app).startswith("gamma")
        await pilot.pause(6)
        assert detail(app).startswith("gamma")
        assert app.query_one("#inst-table").cursor_row == 2
        await pilot.press("enter")
        assert run_cli("active").stdout.strip() == "gamma"


@tui_test
async def test_filter_narrows_list_and_escape_or_enter_closes_it(upstream, upstream2):
    add_direct("alpha", upstream.url)
    add_direct("beta", upstream2.url)
    add_direct("bravo", f"{upstream2.url}/bravo")
    async with tui() as (app, pilot):
        await pilot.press("slash", *"br")
        assert rows(app) == ["bravo"]
        await until(pilot, lambda: "/ br" in plain(app.query_one("#app-header").content), what="header")
        await pilot.press("escape")
        assert rows(app)[:3] == ["alpha", "beta", "bravo"]
        await pilot.press("slash", *"bet", "enter")
        assert rows(app)[:3] == ["alpha", "beta", "bravo"]
        assert detail(app).startswith("beta"), "Enter keeps the match selected"


@tui_test
async def test_main_screen_keys_are_ignored_while_a_dialog_is_open(upstream, upstream2):
    add_direct("alpha", upstream.url)
    add_direct("beta", upstream2.url, "beta-key")
    async with tui() as (app, pilot):
        await pilot.press("h")
        await pilot.press("j", "2", "slash")
        assert isinstance(app.screen, HelpModal)
        await pilot.press("escape")
        assert detail(app).startswith("alpha")
        assert not (home() / ".claude" / "settings.json").exists(), "nothing was activated"
        await pilot.press("l")                            # log viewer is a full screen
        assert isinstance(app.screen, LogViewer)
        await pilot.press("j", "1")
        await pilot.press("b")
        assert not (home() / ".claude" / "settings.json").exists()


@pytest.mark.xfail(strict=True, reason=(
    "BUG: failover.log is written in local time (time.strftime) but parsed as UTC in "
    "FailoverManager.recent_events, so west of UTC recent events are dropped from the detail panel."))
@tui_test
async def test_recent_failover_shows_in_detail_panel_west_of_utc(upstream, upstream2, monkeypatch):
    monkeypatch.setenv("TZ", "EST5")
    time.tzset()
    try:
        add_direct("primary", upstream.url)
        add_direct("backup", upstream2.url)
        run_cli("activate", "primary")
        upstream.route("GET", "/v1/models", Reply(429, json={"error": {"message": "limit"}}))
        assert run_cli("failover").code == 0
        async with tui() as (app, pilot):
            await pilot.press("down")
            assert "Failover away" in detail(app)
    finally:
        monkeypatch.undo()
        time.tzset()


@tui_test
async def test_recent_failover_shows_in_detail_panel_in_utc(upstream, upstream2, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        add_direct("primary", upstream.url)
        add_direct("backup", upstream2.url)
        run_cli("activate", "primary")
        upstream.route("GET", "/v1/models", Reply(429, json={"error": {"message": "limit"}}))
        assert run_cli("failover").code == 0
        async with tui() as (app, pilot):
            assert rows(app) == ["▶ backup", "primary"]
            assert "↩ Failover to" in detail(app)
            await pilot.press("down")
            assert "✖ Failover away" in detail(app)
    finally:
        monkeypatch.undo()
        time.tzset()
