"""Tests for FailoverManager HTTP proxy testing + retry-original flow."""
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError

import pytest
import claude_mux as hs


@pytest.fixture()
def tmp_setup():
    d = Path(tempfile.mkdtemp())
    cm = hs.ConfigManager(data_file=d / "subs.json")
    sync = MagicMock()
    fm = hs.FailoverManager(cm, sync)
    yield cm, sync, fm, d
    shutil.rmtree(d, ignore_errors=True)


def _bearer_sub(cm, name="proxy", port=18082):
    sub = cm.add_subscription(name, "http://localhost:18082", "MY_KEY", auth_type="bearer")
    cm.set_instance_port(sub["id"], port)
    return sub


class TestProxyHTTPHealth:
    def test_200_returns_ok(self, tmp_setup):
        cm, sync, fm, _ = tmp_setup
        sub = _bearer_sub(cm)
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.getcode.return_value = 200

        with patch("urllib.request.urlopen", return_value=mock_resp):
            ok, reason = fm.test_health(sub["id"])
        assert ok is True
        assert "200" in reason

    def test_429_returns_not_ok(self, tmp_setup):
        cm, sync, fm, _ = tmp_setup
        sub = _bearer_sub(cm)
        err = HTTPError(url="http://x", code=429, msg="Too Many Requests", hdrs={}, fp=None)

        with patch("urllib.request.urlopen", side_effect=err):
            ok, reason = fm.test_health(sub["id"])
        assert ok is False
        assert "429" in reason

    def test_401_returns_not_ok(self, tmp_setup):
        cm, sync, fm, _ = tmp_setup
        sub = _bearer_sub(cm)
        err = HTTPError(url="http://x", code=401, msg="Unauthorized", hdrs={}, fp=None)

        with patch("urllib.request.urlopen", side_effect=err):
            ok, reason = fm.test_health(sub["id"])
        assert ok is False

    def test_403_returns_not_ok(self, tmp_setup):
        cm, sync, fm, _ = tmp_setup
        sub = _bearer_sub(cm)
        err = HTTPError(url="http://x", code=403, msg="Forbidden", hdrs={}, fp=None)

        with patch("urllib.request.urlopen", side_effect=err):
            ok, reason = fm.test_health(sub["id"])
        assert ok is False

    def test_404_non_fatal(self, tmp_setup):
        """HTTP 404 er IKKE i FAILOVER_CODES — betragtes som ikke-fatal (proxy kører)."""
        cm, sync, fm, _ = tmp_setup
        sub = _bearer_sub(cm)
        err = HTTPError(url="http://x", code=404, msg="Not Found", hdrs={}, fp=None)

        with patch("urllib.request.urlopen", side_effect=err):
            ok, _ = fm.test_health(sub["id"])
        assert ok is True  # 404 = proxy kører, men endpoint mangler

    def test_connection_error_returns_not_ok(self, tmp_setup):
        cm, sync, fm, _ = tmp_setup
        sub = _bearer_sub(cm)
        from urllib.error import URLError
        err = URLError("Connection refused")

        with patch("urllib.request.urlopen", side_effect=err):
            ok, reason = fm.test_health(sub["id"])
        assert ok is False
        assert "forbindel" in reason.lower() or "connection" in reason.lower()

    def test_no_port_returns_not_ok(self, tmp_setup):
        cm, sync, fm, _ = tmp_setup
        sub = cm.add_subscription("noportsub", "http://localhost:18082", "K", auth_type="bearer")
        # Sæt IKKE port
        ok, reason = fm.test_health(sub["id"])
        assert ok is False
        assert "proxy" in reason.lower() or "kørende" in reason.lower()


class TestFailoverRetryOriginalFlow:
    def test_auto_resume_resets_state(self, tmp_setup):
        """should_retry_original → True → reset_failures nulstiller alt."""
        import time
        cm, sync, fm, d = tmp_setup
        sub1 = cm.add_subscription("s1", "http://x", "K1", auth_type="bearer")
        sub2 = cm.add_subscription("s2", "http://y", "K2", auth_type="bearer")
        cm.set_instance_port(sub1["id"], 18080)
        cm.set_instance_port(sub2["id"], 18081)

        # Simulér failover fra sub1 → sub2
        fm._original_sub_id = sub1["id"]
        fm._failover_ts = time.time() - fm.RETRY_ORIGINAL_AFTER_SECS - 1
        fm._failed_subs.add(sub1["id"])

        assert fm.should_retry_original() is True
        fm.reset_failures()
        assert fm._original_sub_id is None
        assert fm._failover_ts is None
        assert len(fm._failed_subs) == 0

    def test_failover_skips_failed_subs(self, tmp_setup):
        cm, sync, fm, _ = tmp_setup
        sub1 = cm.add_subscription("s1", "http://x", "K1", auth_type="bearer")
        sub2 = cm.add_subscription("s2", "http://y", "K2", auth_type="bearer")
        sub3 = cm.add_subscription("s3", "http://z", "K3", auth_type="bearer")

        # sub2 er allerede fejlet
        fm._failed_subs.add(sub2["id"])

        calls = []
        def mock_health(sub_id=None):
            calls.append(sub_id)
            return (True, "OK") if sub_id == sub3["id"] else (False, "fejl")

        fm.test_health = mock_health
        result = fm.do_failover(sub1["id"])
        # sub2 skal IKKE forsøges
        assert sub2["id"] not in calls
        assert result == sub3["id"]

    def test_log_written_on_successful_failover(self, tmp_setup):
        cm, sync, fm, d = tmp_setup
        fm.FAILOVER_LOG = d / "failover.log"
        sub1 = cm.add_subscription("s1", "http://x", "K1", auth_type="bearer")
        sub2 = cm.add_subscription("s2", "http://y", "K2", auth_type="bearer")
        fm.test_health = lambda sub_id=None: (True, "OK") if sub_id == sub2["id"] else (False, "429")
        fm.do_failover(sub1["id"], reason="HTTP 429")
        assert fm.FAILOVER_LOG.exists()
        content = fm.FAILOVER_LOG.read_text()
        assert "s1" in content
        assert "s2" in content
        assert "HTTP 429" in content

    def test_log_written_on_all_failed(self, tmp_setup):
        cm, sync, fm, d = tmp_setup
        fm.FAILOVER_LOG = d / "failover.log"
        sub1 = cm.add_subscription("s1", "http://x", "K1")
        fm.test_health = lambda sub_id=None: (False, "fejl")
        fm.do_failover(sub1["id"], reason="rate-limit")
        assert fm.FAILOVER_LOG.exists()
        content = fm.FAILOVER_LOG.read_text()
        assert "TO=none" in content


class TestSyncManagerSaveFailure:
    def test_save_settings_returns_false_on_oserror(self):
        """_save_settings returnerer False hvis disk write fejler."""
        d = Path(tempfile.mkdtemp())
        try:
            sp = d / "settings.json"
            cm = hs.ConfigManager(data_file=d / "subs.json")

            class _Sync(hs.SyncManager):
                SETTINGS_PATH = sp
                CLAUDE_MUX_DOT_ENV = d / ".env"

            sync = _Sync(cm)
            with patch("claude_mux.tui._atomic_write", side_effect=OSError("disk full")):
                result = sync._save_settings({})
            assert result is False
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestConfigManagerCorruption:
    def test_corrupt_json_returns_empty(self):
        d = Path(tempfile.mkdtemp())
        try:
            data_file = d / "subs.json"
            data_file.write_text("{ corrupt json !!!")
            cm = hs.ConfigManager(data_file=data_file)
            assert len(cm.subscriptions) == 0
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_wrong_version_returns_empty(self):
        d = Path(tempfile.mkdtemp())
        try:
            data_file = d / "subs.json"
            data_file.write_text(json.dumps({"version": 99, "subscriptions": [{"id": "x"}]}))
            cm = hs.ConfigManager(data_file=data_file)
            assert len(cm.subscriptions) == 0
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_two_corrupt_instances_dont_share_state(self):
        """Korrupt JSON → deepcopy af EMPTY — ingen shared state."""
        d1 = Path(tempfile.mkdtemp())
        d2 = Path(tempfile.mkdtemp())
        try:
            for d in (d1, d2):
                (d / "subs.json").write_text("not json")
            cm1 = hs.ConfigManager(data_file=d1 / "subs.json")
            cm2 = hs.ConfigManager(data_file=d2 / "subs.json")
            cm1.add_subscription("a", "http://a", "K")
            assert len(cm2.subscriptions) == 0
        finally:
            shutil.rmtree(d1, ignore_errors=True)
            shutil.rmtree(d2, ignore_errors=True)

    def test_backup_created_on_save(self):
        d = Path(tempfile.mkdtemp())
        try:
            data_file = d / "subs.json"
            cm = hs.ConfigManager(data_file=data_file)
            cm.add_subscription("first", "http://x", "K")
            # Nu gemmes — den originale fil skal backuppes ved næste gem
            cm.add_subscription("second", "http://y", "K2")
            backup = data_file.with_suffix(".json.bak")
            assert backup.exists(), "Backup .json.bak skal oprettes ved save"
        finally:
            shutil.rmtree(d, ignore_errors=True)
