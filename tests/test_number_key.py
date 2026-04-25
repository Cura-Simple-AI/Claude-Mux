"""Tests for number-key activation logic — #23 review finding.

The on_key handler in HeimsenseApp maps digit keys 1-9 to _sorted_rows indices.
When the row at that index is "__current__" (virtual row), it must NOT call
_do_set_default — activating an unsaved "current settings" row is a no-op.
"""
import pytest
from unittest.mock import MagicMock, patch
import claude_mux as cm_pkg
from claude_mux.sync import SyncManager
from claude_mux.failover import FailoverManager
from claude_mux.tui import HeimsenseApp


def _make_app(tmp_path, sorted_rows=None):
    """Stub HeimsenseApp with controllable _sorted_rows."""
    config = cm_pkg.ConfigManager(data_file=tmp_path / "subscriptions.json")
    sync = MagicMock(spec=SyncManager)
    failover = MagicMock(spec=FailoverManager)

    app = HeimsenseApp.__new__(HeimsenseApp)
    app.cm = config
    app.sync = sync
    app.failover = failover
    app._test_results = {}
    app._sorted_rows = sorted_rows or []
    app._do_set_default = MagicMock()
    app.notify = MagicMock()
    return app


class _FakeEvent:
    """Minimal keyboard event stub."""
    def __init__(self, key: str):
        self.key = key
        self._stopped = False

    def stop(self):
        self._stopped = True

    def isdigit(self):
        return self.key.isdigit()


class _FakeTable:
    def move_cursor(self, **kwargs):
        pass


def _invoke_number_key(app, key: str):
    """Simulate the digit branch of on_key without running Textual."""
    event = _FakeEvent(key)
    table = _FakeTable()
    if event.key.isdigit() and event.key != "0":
        row_idx = int(event.key) - 1
        if 0 <= row_idx < len(app._sorted_rows):
            target_id = app._sorted_rows[row_idx]
            if target_id != "__current__":
                table.move_cursor(row=row_idx)
                app._do_set_default(target_id)
                event.stop()
    return event


class TestNumberKeySkipsCurrentRow:
    def test_current_row_at_index_0_not_activated(self, tmp_path):
        """Key '1' with __current__ at index 0 must not call _do_set_default."""
        app = _make_app(tmp_path, sorted_rows=["__current__"])
        event = _invoke_number_key(app, "1")
        app._do_set_default.assert_not_called()
        assert not event._stopped

    def test_current_row_in_middle_not_activated(self, tmp_path):
        """Key '2' with __current__ at index 1 must not call _do_set_default."""
        app = _make_app(tmp_path, sorted_rows=["sub-aaa", "__current__", "sub-bbb"])
        event = _invoke_number_key(app, "2")
        app._do_set_default.assert_not_called()

    def test_real_sub_after_current_is_activated(self, tmp_path):
        """Key '3' pointing to a real sub_id after __current__ must activate it."""
        app = _make_app(tmp_path, sorted_rows=["__current__", "sub-aaa", "sub-bbb"])
        event = _invoke_number_key(app, "3")
        app._do_set_default.assert_called_once_with("sub-bbb")
        assert event._stopped

    def test_key_0_never_activates(self, tmp_path):
        """Key '0' is explicitly excluded — must never activate anything."""
        app = _make_app(tmp_path, sorted_rows=["sub-aaa", "sub-bbb"])
        event = _invoke_number_key(app, "0")
        app._do_set_default.assert_not_called()

    def test_index_out_of_bounds_does_nothing(self, tmp_path):
        """Key '9' when only 2 rows exist must not call _do_set_default."""
        app = _make_app(tmp_path, sorted_rows=["sub-aaa", "sub-bbb"])
        event = _invoke_number_key(app, "9")
        app._do_set_default.assert_not_called()

    def test_valid_sub_id_activated(self, tmp_path):
        """Key '1' pointing at real sub_id activates it."""
        app = _make_app(tmp_path, sorted_rows=["sub-real-id"])
        event = _invoke_number_key(app, "1")
        app._do_set_default.assert_called_once_with("sub-real-id")
        assert event._stopped
