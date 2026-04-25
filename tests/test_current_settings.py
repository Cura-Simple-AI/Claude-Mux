"""Tests for _test_current_settings logic — #20 review finding.

_test_current_settings() is a method on HeimsenseApp. It reads the current
Claude settings.json, infers auth_type, builds a temporary sub-dict, and
delegates to failover._test_direct_http().

We unit-test the inference logic by mocking _test_direct_http and verifying
the sub-dict that was passed to it.
"""
import json
import pytest
from unittest.mock import MagicMock, patch, call
import claude_mux as cm_pkg
from claude_mux.failover import FailoverManager
from claude_mux.sync import SyncManager
from claude_mux.tui import HeimsenseApp


def _make_app(tmp_path):
    """Minimal HeimsenseApp stub with _test_current_settings available."""
    config = cm_pkg.ConfigManager(data_file=tmp_path / "subscriptions.json")
    sync = MagicMock(spec=SyncManager)
    failover = MagicMock(spec=FailoverManager)
    failover._test_direct_http.return_value = (True, "HTTP 200")

    app = HeimsenseApp.__new__(HeimsenseApp)
    app.cm = config
    app.sync = sync
    app.failover = failover
    app._test_results = {}

    # Mock Textual notify (not running in TUI)
    app.notify = MagicMock()
    app.push_screen = MagicMock()
    app._refresh_table = MagicMock()
    app._show_detail = MagicMock()
    return app, sync, failover


class TestCurrentSettingsOAuth:
    def test_oauth_token_builds_correct_sub(self, tmp_path):
        """OAuth token in settings → sub with auth_type=oauth and api.anthropic.com."""
        app, sync, failover = _make_app(tmp_path)
        sync._load_settings.return_value = {
            "env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-tok-XXX"}
        }

        with patch.dict("os.environ", {}, clear=True):
            app._test_current_settings()

        failover._test_direct_http.assert_called_once()
        sub = failover._test_direct_http.call_args[0][0]
        assert sub["auth_type"] == "oauth"
        assert sub["api_key"] == "sk-ant-tok-XXX"
        assert sub.get("provider_url", "") == ""

    def test_oauth_token_from_os_environ(self, tmp_path):
        """OAuth token from os.environ (not settings) is also detected."""
        app, sync, failover = _make_app(tmp_path)
        sync._load_settings.return_value = {"env": {}}

        with patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-env-tok"}):
            app._test_current_settings()

        failover._test_direct_http.assert_called_once()
        sub = failover._test_direct_http.call_args[0][0]
        assert sub["auth_type"] == "oauth"
        assert sub["api_key"] == "sk-ant-env-tok"


class TestCurrentSettingsDirect:
    def test_direct_base_url_builds_correct_sub(self, tmp_path):
        """Non-localhost ANTHROPIC_BASE_URL → direct sub with that URL."""
        app, sync, failover = _make_app(tmp_path)
        sync._load_settings.return_value = {
            "env": {
                "ANTHROPIC_BASE_URL": "https://api.z.ai/v1",
                "ANTHROPIC_AUTH_TOKEN": "z-key-123",
            }
        }

        with patch.dict("os.environ", {}, clear=True):
            app._test_current_settings()

        failover._test_direct_http.assert_called_once()
        sub = failover._test_direct_http.call_args[0][0]
        assert sub["auth_type"] == "direct"
        assert sub["provider_url"] == "https://api.z.ai/v1"
        assert sub["api_key"] == "z-key-123"


class TestCurrentSettingsNoConfig:
    def test_no_token_no_base_url_notifies_warning(self, tmp_path):
        """No OAuth token and no ANTHROPIC_BASE_URL → warning notification, no HTTP call."""
        app, sync, failover = _make_app(tmp_path)
        sync._load_settings.return_value = {"env": {}}

        with patch.dict("os.environ", {}, clear=True):
            app._test_current_settings()

        failover._test_direct_http.assert_not_called()
        app.notify.assert_called_once()
        call_kwargs = app.notify.call_args
        # Should be a warning severity
        assert "warning" in str(call_kwargs)

    def test_http_error_from_direct_http_does_not_raise(self, tmp_path):
        """If _test_direct_http raises, _test_current_settings handles it gracefully."""
        app, sync, failover = _make_app(tmp_path)
        failover._test_direct_http.return_value = (False, "Connection error")
        sync._load_settings.return_value = {
            "env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-tok-YYY"}
        }

        with patch.dict("os.environ", {}, clear=True):
            # Must not raise
            app._test_current_settings()
