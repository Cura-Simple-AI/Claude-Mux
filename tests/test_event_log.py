"""Tests for _build_event_log — #19 review finding."""
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock


def _make_app(tmp_path, subscriptions=None, default_instance=None, test_results=None):
    """Build a minimal Claude Mux app stub with _build_event_log available.

    Uses a real FailoverManager so recent_events() exercises the actual log parser.
    The FAILOVER_LOG path is redirected to tmp_path for test isolation.
    """
    import claude_mux as cm_pkg
    from claude_mux.failover import FailoverManager
    from claude_mux.sync import SyncManager
    from claude_mux.tui import HeimsenseApp as ClaudeMux

    config = cm_pkg.ConfigManager(data_file=tmp_path / "subscriptions.json")
    if subscriptions:
        for s in subscriptions:
            sub = config.add_subscription(
                s["name"], s.get("url", ""), s.get("env", "KEY"),
                auth_type=s.get("auth_type", "bearer"),
            )
            if s.get("default"):
                config.set_default(sub["id"])

    sync = MagicMock(spec=SyncManager)
    # Use a real FailoverManager so recent_events() works; redirect log to tmp_path
    failover = FailoverManager.__new__(FailoverManager)
    failover.cm = config
    failover.sync = sync
    failover._failed_subs = set()
    failover._original_sub_id = None
    failover._failover_ts = None
    failover.FAILOVER_LOG = tmp_path / "failover.log"

    app = ClaudeMux.__new__(ClaudeMux)
    app.cm = config
    app.sync = sync
    app.failover = failover
    app._test_results = test_results or {}
    return app


class TestBuildEventLogEmpty:
    def test_no_log_file_no_test_results_returns_empty(self, tmp_path):
        """Empty event log when no log file and no test results."""
        app = _make_app(tmp_path)
        result = app._build_event_log("myname", "sub-id-123")
        assert result == ""

    def test_log_file_missing_returns_empty(self, tmp_path):
        """Missing log file does not raise — returns empty string."""
        app = _make_app(tmp_path)
        # FAILOVER_LOG path does not exist
        result = app._build_event_log("myname", "does-not-exist")
        assert result == ""

    def test_default_instance_not_this_sub_returns_empty(self, tmp_path):
        """Active-now event only appended if sub_id == default_instance."""
        app = _make_app(tmp_path)
        app.cm._data["default_instance"] = "other-sub-id"
        result = app._build_event_log("myname", "sub-id-123")
        assert result == ""


class TestBuildEventLogFiltering:
    def _old_ts(self):
        """Timestamp >1 hour ago."""
        return time.time() - 4000

    def _new_ts(self):
        """Timestamp <1 hour ago."""
        return time.time() - 60

    def test_old_test_result_filtered_out(self, tmp_path):
        """Test results older than 1 hour are excluded."""
        old_ts = self._old_ts()
        app = _make_app(tmp_path, test_results={"sub-id": {"code": 200, "ts": old_ts}})
        result = app._build_event_log("myname", "sub-id")
        assert result == ""

    def test_new_test_result_included(self, tmp_path):
        """Recent test result appears in event log."""
        app = _make_app(
            tmp_path,
            test_results={"sub-id": {"code": 200, "ts": self._new_ts()}},
        )
        result = app._build_event_log("myname", "sub-id")
        assert result != ""
        assert "✓" in result or "Tested" in result

    def test_old_log_entries_filtered_out(self, tmp_path):
        """Failover log entries older than 1 hour are excluded."""
        old_dt = datetime.fromtimestamp(self._old_ts(), tz=timezone.utc)
        ts_str = old_dt.strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"{ts_str}  FROM=myname  TO=other  REASON=rate-limit"
        log_path = tmp_path / "failover.log"
        log_path.write_text(log_line + "\n")

        app = _make_app(tmp_path)
        app.failover.FAILOVER_LOG = log_path
        result = app._build_event_log("myname", "sub-id")
        assert result == ""

    def test_new_log_entry_included(self, tmp_path):
        """Recent failover log entry appears in event log."""
        new_dt = datetime.fromtimestamp(self._new_ts(), tz=timezone.utc)
        ts_str = new_dt.strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"{ts_str}  FROM=myname  TO=other  REASON=429"
        log_path = tmp_path / "failover.log"
        log_path.write_text(log_line + "\n")

        app = _make_app(tmp_path)
        app.failover.FAILOVER_LOG = log_path
        result = app._build_event_log("myname", "sub-id")
        assert result != ""
        assert "Failover" in result

    def test_mix_old_and_new_returns_only_new(self, tmp_path):
        """Old entries filtered, new entries retained."""
        old_dt = datetime.fromtimestamp(self._old_ts(), tz=timezone.utc)
        new_dt = datetime.fromtimestamp(self._new_ts(), tz=timezone.utc)
        lines = [
            f"{old_dt.strftime('%Y-%m-%d %H:%M:%S')}  FROM=myname  TO=other  REASON=timeout",
            f"{new_dt.strftime('%Y-%m-%d %H:%M:%S')}  TO=myname  FROM=other  REASON=recovery",
        ]
        log_path = tmp_path / "failover.log"
        log_path.write_text("\n".join(lines) + "\n")

        app = _make_app(tmp_path)
        app.failover.FAILOVER_LOG = log_path
        result = app._build_event_log("myname", "sub-id")
        assert result != ""
        assert "Failover to" in result
        # Old "Failover away" entry must be absent
        assert "Failover away" not in result


class TestBuildEventLogCurrentSettings:
    def test_current_key_included_when_matching_sub(self, tmp_path):
        """__current__ test result appears when queried for the virtual row."""
        ts = time.time() - 30  # 30 seconds ago — well within 1 hour
        app = _make_app(
            tmp_path,
            test_results={"__current__": {"code": 401, "ts": ts}},
        )
        result = app._build_event_log("*current settings", "__current__")
        assert result != ""
        assert "✖" in result or "401" in result
