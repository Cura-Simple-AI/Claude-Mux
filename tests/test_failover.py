"""Unit tests for FailoverManager — subscription failover logik."""
import tempfile
import shutil
from pathlib import Path

import pytest
from unittest.mock import MagicMock

import claude_mux as hs


@pytest.fixture()
def tmp_setup():
    d = Path(tempfile.mkdtemp())
    cm = hs.ConfigManager(data_file=d / "subscriptions.json")
    sync = MagicMock()
    sync.detect_active.return_value = None
    fm = hs.FailoverManager(cm, sync)
    fm.FAILOVER_LOG = d / "failover.log"  # isolate from ~/.claude-mux/failover.log
    yield cm, sync, fm
    shutil.rmtree(d, ignore_errors=True)


class TestFailoverManager:
    def test_test_health_no_default(self, tmp_setup):
        _, _, fm = tmp_setup
        ok, reason = fm.test_health()
        assert not ok
        assert "No active" in reason

    def test_test_health_unknown_sub(self, tmp_setup):
        _, _, fm = tmp_setup
        ok, _ = fm.test_health("non-existent-id")
        assert not ok

    def test_do_failover_no_alternatives(self, tmp_setup):
        cm, _, fm = tmp_setup
        sub = cm.add_subscription("only-one", "http://x", "KEY")
        result = fm.do_failover(sub["id"])
        assert result is None

    def test_do_failover_switches_to_next(self, tmp_setup):
        cm, sync, fm = tmp_setup
        sub1 = cm.add_subscription("sub1", "http://x", "KEY1", auth_type="bearer")
        sub2 = cm.add_subscription("sub2", "http://y", "KEY2", auth_type="bearer")

        def mock_health(sub_id=None):
            return (True, "OK") if sub_id == sub2["id"] else (False, "fejl")

        fm.test_health = mock_health
        result = fm.do_failover(sub1["id"])
        assert result == sub2["id"]
        assert sub1["id"] in fm._failed_subs

    def test_reset_failures(self, tmp_setup):
        _, _, fm = tmp_setup
        fm._failed_subs.add("some-id")
        fm.reset_failures()
        assert len(fm._failed_subs) == 0

    def test_failover_marks_all_failed_if_none_work(self, tmp_setup):
        cm, _, fm = tmp_setup
        sub1 = cm.add_subscription("s1", "http://x", "K1")
        sub2 = cm.add_subscription("s2", "http://y", "K2")
        fm.test_health = lambda sub_id=None: (False, "fejl")
        result = fm.do_failover(sub1["id"])
        assert result is None
        assert sub2["id"] in fm._failed_subs

    def test_failover_codes_include_429(self):
        assert 429 in hs.FailoverManager.FAILOVER_CODES

    def test_failover_codes_include_auth_errors(self):
        assert 401 in hs.FailoverManager.FAILOVER_CODES
        assert 403 in hs.FailoverManager.FAILOVER_CODES

    def test_failover_patterns_include_rate_limit(self):
        assert any("rate limit" in p for p in hs.FailoverManager.FAILOVER_PATTERNS)

    def test_do_failover_sets_original_sub_id(self, tmp_setup):
        cm, sync, fm = tmp_setup
        sub1 = cm.add_subscription("s1", "http://x", "K1", auth_type="bearer")
        sub2 = cm.add_subscription("s2", "http://y", "K2", auth_type="bearer")
        fm.test_health = lambda sub_id=None: (True, "OK") if sub_id == sub2["id"] else (False, "fejl")
        fm.do_failover(sub1["id"], reason="429")
        assert fm._original_sub_id == sub1["id"]
        assert fm._failover_ts is not None

    def test_do_failover_only_sets_original_once(self, tmp_setup):
        """_original_sub_id bevares selv ved kaskade-failover."""
        cm, sync, fm = tmp_setup
        sub1 = cm.add_subscription("s1", "http://x", "K1", auth_type="bearer")
        sub2 = cm.add_subscription("s2", "http://y", "K2", auth_type="bearer")
        sub3 = cm.add_subscription("s3", "http://z", "K3", auth_type="bearer")
        call_count = [0]

        def mock_health(sub_id=None):
            call_count[0] += 1
            return (True, "OK") if sub_id == sub3["id"] else (False, "fejl")

        fm.test_health = mock_health
        fm.do_failover(sub1["id"], reason="429")
        fm.do_failover(sub2["id"], reason="503")
        # Original er stadig sub1
        assert fm._original_sub_id == sub1["id"]

    def test_reset_clears_failover_state(self, tmp_setup):
        _, _, fm = tmp_setup
        fm._failed_subs.add("x")
        fm._original_sub_id = "x"
        fm._failover_ts = 12345.0
        fm.reset_failures()
        assert len(fm._failed_subs) == 0
        assert fm._original_sub_id is None
        assert fm._failover_ts is None

    def test_should_retry_original_false_if_no_failover(self, tmp_setup):
        _, _, fm = tmp_setup
        assert fm.should_retry_original() is False

    def test_should_retry_original_false_before_timeout(self, tmp_setup):
        import time
        _, _, fm = tmp_setup
        fm._original_sub_id = "x"
        fm._failover_ts = time.time()  # lige nu
        assert fm.should_retry_original() is False

    def test_should_retry_original_true_after_timeout(self, tmp_setup):
        import time
        _, _, fm = tmp_setup
        fm._original_sub_id = "x"
        fm._failover_ts = time.time() - fm.RETRY_ORIGINAL_AFTER_SECS - 1
        assert fm.should_retry_original() is True

    def test_log_failover_event_writes_file(self, tmp_setup):
        import tempfile, shutil
        d = Path(tempfile.mkdtemp())
        try:
            cm, sync, fm = tmp_setup
            fm.FAILOVER_LOG = d / "failover.log"
            sub1 = {"name": "A"}
            sub2 = {"name": "B"}
            fm._log_failover_event(sub1, sub2, "HTTP 429")
            content = fm.FAILOVER_LOG.read_text()
            assert "FROM=A" in content
            assert "TO=B" in content
            assert "HTTP 429" in content
        finally:
            shutil.rmtree(d)
