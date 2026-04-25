"""Unit tests for SyncManager — settings.json sync."""
import json
import pytest
from unittest.mock import patch

import claude_mux as hs


@pytest.fixture()
def tmp_setup(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(hs.SyncManager, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(hs.SyncManager, "CLAUDE_MUX_DOT_ENV", tmp_path / ".env")
    cm = hs.ConfigManager(data_file=tmp_path / "subscriptions.json")
    return cm, tmp_path, settings_path


def _run_sync(cm, sub, tmp_path, settings_path, env_overrides=None):
    sync = hs.SyncManager(cm)
    with patch.object(hs.InstanceManager, "generate_env", return_value=tmp_path / "fake.env"):
        with patch("shutil.copy2"):
            with patch.dict("os.environ", env_overrides or {}):
                sync.sync_default(sub["id"])
    return json.loads(settings_path.read_text())


class TestSyncManagerOAuth:
    def test_sets_oauth_token(self, tmp_setup):
        cm, tmp_path, settings_path = tmp_setup
        sub = cm.add_subscription("claude-max", "", "CLAUDE_CODE_OAUTH_TOKEN", auth_type="oauth",
                                   model_maps={"haiku": "h", "sonnet": "s", "opus": "o"})
        cm.update_subscription(sub["id"], api_key="sk-ant-real-token")
        cm.set_instance_port(sub["id"], 0)
        settings = _run_sync(cm, sub, tmp_path, settings_path)
        env = settings.get("env", {})
        assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-real-token"
        assert "ANTHROPIC_BASE_URL" not in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env

    def test_sets_model_maps(self, tmp_setup):
        cm, tmp_path, settings_path = tmp_setup
        sub = cm.add_subscription("claude-max", "", "CLAUDE_CODE_OAUTH_TOKEN", auth_type="oauth",
                                   model_maps={"haiku": "my-haiku", "sonnet": "my-sonnet", "opus": "my-opus"})
        cm.update_subscription(sub["id"], api_key="sk-ant-token")
        cm.set_instance_port(sub["id"], 0)
        settings = _run_sync(cm, sub, tmp_path, settings_path)
        env = settings.get("env", {})
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "my-haiku"
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "my-sonnet"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "my-opus"


class TestSyncManagerBearer:
    def test_sets_base_url(self, tmp_setup):
        cm, tmp_path, settings_path = tmp_setup
        sub = cm.add_subscription("my-proxy", "http://localhost:18080", "MY_API_KEY",
                                   auth_type="bearer")
        cm.set_instance_port(sub["id"], 18080)
        settings = _run_sync(cm, sub, tmp_path, settings_path, {"MY_API_KEY": "test-key"})
        env = settings.get("env", {})
        assert "localhost:18080" in env.get("ANTHROPIC_BASE_URL", "")
        assert env.get("ANTHROPIC_AUTH_TOKEN") == "test-key"
