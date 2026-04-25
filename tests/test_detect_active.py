"""Unit tests for SyncManager.detect_active()."""
import pytest
from unittest.mock import patch

import claude_mux as hs
from claude_mux.sync import SyncManager


@pytest.fixture()
def cm(tmp_path):
    return hs.ConfigManager(data_file=tmp_path / "subscriptions.json")


@pytest.fixture()
def sync(cm, tmp_path, monkeypatch):
    s = SyncManager(cm)
    monkeypatch.setattr(SyncManager, "SETTINGS_PATH", tmp_path / "settings.json")
    return s


def _write_settings(sync, env: dict):
    import json
    sync.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    sync.SETTINGS_PATH.write_text(json.dumps({"env": env}))


class TestDetectActiveOAuth:
    def test_matches_by_api_key(self, cm, sync, monkeypatch):
        sub = cm.add_subscription("max", "", "", auth_type="oauth")
        cm.update_subscription(sub["id"], api_key="sk-ant-tok123")
        _write_settings(sync, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-tok123"})
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        assert sync.detect_active() == sub["id"]

    def test_matches_token_from_os_environ(self, cm, sync, monkeypatch):
        sub = cm.add_subscription("max", "", "CLAUDE_TOKEN_ENV", auth_type="oauth")
        cm.update_subscription(sub["id"], api_key="sk-ant-envtok")
        _write_settings(sync, {})
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-envtok")
        assert sync.detect_active() == sub["id"]

    def test_matches_via_api_key_env(self, cm, sync, monkeypatch):
        sub = cm.add_subscription("max", "", "MY_OAUTH_KEY", auth_type="oauth")
        # No api_key stored — resolve from env
        _write_settings(sync, {"CLAUDE_CODE_OAUTH_TOKEN": "from-env-var"})
        monkeypatch.setenv("MY_OAUTH_KEY", "from-env-var")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        assert sync.detect_active() == sub["id"]

    def test_no_match_when_token_differs(self, cm, sync, monkeypatch):
        sub = cm.add_subscription("max", "", "", auth_type="oauth")
        cm.update_subscription(sub["id"], api_key="sk-ant-AAAA")
        _write_settings(sync, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-BBBB"})
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        assert sync.detect_active() is None

    def test_no_match_when_token_absent(self, cm, sync, monkeypatch):
        cm.add_subscription("max", "", "", auth_type="oauth")
        _write_settings(sync, {})
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        assert sync.detect_active() is None


class TestDetectActiveDirect:
    def test_matches_by_provider_url(self, cm, sync, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        sub = cm.add_subscription("zai", "https://api.z.ai/api/anthropic", "Z_AI_API_KEY",
                                   auth_type="direct")
        _write_settings(sync, {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"})
        assert sync.detect_active() == sub["id"]

    def test_no_match_different_url(self, cm, sync, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        cm.add_subscription("zai", "https://api.z.ai/api/anthropic", "Z_AI_API_KEY",
                             auth_type="direct")
        _write_settings(sync, {"ANTHROPIC_BASE_URL": "https://api.other.com/v1"})
        assert sync.detect_active() is None


class TestDetectActiveBearer:
    def test_matches_by_port(self, cm, sync, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        sub = cm.add_subscription("deepseek", "https://api.deepseek.com/v1", "DEEPSEEK_KEY",
                                   auth_type="bearer")
        cm.set_instance_port(sub["id"], 18082)
        _write_settings(sync, {"ANTHROPIC_BASE_URL": "http://localhost:18082"})
        assert sync.detect_active() == sub["id"]

    def test_no_match_wrong_port(self, cm, sync, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        sub = cm.add_subscription("deepseek", "https://api.deepseek.com/v1", "DEEPSEEK_KEY",
                                   auth_type="bearer")
        cm.set_instance_port(sub["id"], 18082)
        _write_settings(sync, {"ANTHROPIC_BASE_URL": "http://localhost:19999"})
        assert sync.detect_active() is None

    def test_no_match_no_port_assigned(self, cm, sync, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        cm.add_subscription("deepseek", "https://api.deepseek.com/v1", "DEEPSEEK_KEY",
                             auth_type="bearer")
        _write_settings(sync, {"ANTHROPIC_BASE_URL": "http://localhost:18082"})
        assert sync.detect_active() is None


class TestDetectActiveMultipleSubs:
    def test_returns_first_match_among_subs(self, cm, sync, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        sub1 = cm.add_subscription("ds1", "https://api.deepseek.com/v1", "K1", auth_type="bearer")
        sub2 = cm.add_subscription("ds2", "https://api.deepseek.com/v1", "K2", auth_type="bearer")
        cm.set_instance_port(sub1["id"], 18081)
        cm.set_instance_port(sub2["id"], 18082)
        _write_settings(sync, {"ANTHROPIC_BASE_URL": "http://localhost:18082"})
        assert sync.detect_active() == sub2["id"]

    def test_empty_subscriptions_returns_none(self, cm, sync, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        _write_settings(sync, {"ANTHROPIC_BASE_URL": "http://localhost:18082"})
        assert sync.detect_active() is None


class TestDetectActiveMissingSettings:
    def test_no_settings_file(self, cm, sync, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        cm.add_subscription("ds", "https://api.deepseek.com/v1", "K", auth_type="bearer")
        # settings file does not exist
        assert sync.detect_active() is None
