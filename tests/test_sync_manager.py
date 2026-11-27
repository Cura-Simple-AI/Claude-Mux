"""Unit tests for SyncManager — settings.json sync."""
import json
import pytest
from unittest.mock import patch

import claude_mux as hs


@pytest.fixture(autouse=True)
def _proxy_port_is_listening():
    """Unit tests of the settings merge use made-up proxy ports; pretend something listens.
    The real "is the proxy running" check is covered end to end in e2e/."""
    with patch("claude_mux.sync._port_is_available", return_value=False):
        yield


@pytest.fixture()
def tmp_setup(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(hs.SyncManager, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(hs.SyncManager, "CLAUDE_MUX_DOT_ENV", tmp_path / ".env")
    monkeypatch.setattr("claude_mux.sync.ACTIVE_NAME_FILE", tmp_path / "active-name")
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


class TestInferencePayload:
    def test_gpt5_uses_max_completion_tokens(self):
        payload = hs.build_inference_test_payload("gpt-5.5")
        assert payload["max_completion_tokens"] == 100
        assert "max_tokens" not in payload

    def test_non_gpt5_uses_max_tokens(self):
        payload = hs.build_inference_test_payload("gpt-4o-mini")
        assert payload["max_tokens"] == 100
        assert "max_completion_tokens" not in payload

    def test_openai_reasoning_models_use_max_completion_tokens(self):
        payload = hs.build_inference_test_payload("o3")
        assert payload["max_completion_tokens"] == 100
        assert "max_tokens" not in payload

    def test_oauth_payload_adds_system_prompt(self):
        payload = hs.build_inference_test_payload("claude-sonnet-4-6", "oauth")
        assert "system" in payload


class TestPinnedModel:
    """settings.json["model"]: a model id the user pinned must survive a profile switch;
    only an id that the previous provider offered (and the new one does not) may go."""

    def _oauth_profile(self, cm, name="claude-max"):
        sub = cm.add_subscription(name, "", "CLAUDE_CODE_OAUTH_TOKEN", auth_type="oauth")
        cm.update_subscription(sub["id"], api_key="sk-ant-token")
        cm.set_instance_port(sub["id"], 0)
        return sub

    def test_oauth_switch_keeps_pinned_anthropic_model(self, tmp_setup):
        cm, tmp_path, settings_path = tmp_setup
        settings_path.write_text(json.dumps({"model": "claude-opus-5-5", "env": {}}))
        sub = self._oauth_profile(cm)
        settings = _run_sync(cm, sub, tmp_path, settings_path)
        assert settings["model"] == "claude-opus-5-5"

    def test_switch_between_oauth_profiles_keeps_pin(self, tmp_setup):
        cm, tmp_path, settings_path = tmp_setup
        settings_path.write_text(json.dumps({"model": "claude-opus-5-5", "env": {}}))
        _run_sync(cm, self._oauth_profile(cm, "profile-a"), tmp_path, settings_path)
        settings = _run_sync(cm, self._oauth_profile(cm, "profile-b"), tmp_path, settings_path)
        assert settings["model"] == "claude-opus-5-5"

    def test_pin_kept_when_new_provider_offers_it(self, tmp_setup):
        cm, tmp_path, settings_path = tmp_setup
        settings_path.write_text(json.dumps({"model": "my-opus", "env": {}}))
        sub = cm.add_subscription("my-proxy", "http://localhost:18080", "MY_API_KEY", auth_type="bearer",
                                  model_maps={"opus": "my-opus"})
        cm.set_instance_port(sub["id"], 18080)
        settings = _run_sync(cm, sub, tmp_path, settings_path, {"MY_API_KEY": "k"})
        assert settings["model"] == "my-opus"

    def test_pin_from_previous_provider_is_removed(self, tmp_setup):
        """The id the old provider served would be rejected by the new one."""
        cm, tmp_path, settings_path = tmp_setup
        settings_path.write_text(json.dumps({
            "model": "deepseek-reasoner",
            "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-reasoner"},
        }))
        settings = _run_sync(cm, self._oauth_profile(cm), tmp_path, settings_path)
        assert "model" not in settings
        assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in settings["env"]

    def test_pin_in_previous_providers_model_list_is_removed(self, tmp_setup):
        cm, tmp_path, settings_path = tmp_setup
        old = cm.add_subscription("old-proxy", "http://localhost:18080", "K", auth_type="bearer")
        cm.update_subscription_models(old["id"], ["old-model-x"], 0.0)
        cm.set_instance_port(old["id"], 18080)
        _run_sync(cm, old, tmp_path, settings_path, {"K": "k"})
        data = json.loads(settings_path.read_text())
        data["model"] = "old-model-x"
        settings_path.write_text(json.dumps(data))
        settings = _run_sync(cm, self._oauth_profile(cm), tmp_path, settings_path)
        assert "model" not in settings

    def test_users_own_api_key_env_is_not_touched(self, tmp_setup):
        cm, tmp_path, settings_path = tmp_setup
        settings_path.write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-user", "MY_VAR": "1"}}))
        settings = _run_sync(cm, self._oauth_profile(cm), tmp_path, settings_path)
        assert settings["env"]["ANTHROPIC_API_KEY"] == "sk-user"
        assert settings["env"]["MY_VAR"] == "1"

    def test_users_own_env_model_pins_are_not_touched(self, tmp_setup):
        """ANTHROPIC_MODEL / ANTHROPIC_SMALL_FAST_MODEL are Claude Code's own pin
        mechanism; claude-mux never writes them, so activation must not remove them."""
        cm, tmp_path, settings_path = tmp_setup
        settings_path.write_text(json.dumps({"env": {
            "ANTHROPIC_MODEL": "claude-opus-5-5",
            "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-5-5",
        }}))
        settings = _run_sync(cm, self._oauth_profile(cm), tmp_path, settings_path)
        assert settings["env"]["ANTHROPIC_MODEL"] == "claude-opus-5-5"
        assert settings["env"]["ANTHROPIC_SMALL_FAST_MODEL"] == "claude-haiku-5-5"

    def test_oauth_switch_keeps_pin_when_new_profile_model_list_is_unknown(self, tmp_setup):
        """An empty/unfetched available_models means "don't know", not "not offered"."""
        cm, tmp_path, settings_path = tmp_setup
        settings_path.write_text(json.dumps({"model": "claude-opus-5-5", "env": {}}))
        a = self._oauth_profile(cm, "profile-a")
        cm.update_subscription_models(a["id"], ["claude-opus-5-5", "claude-sonnet-5-5"], 0.0)
        _run_sync(cm, a, tmp_path, settings_path)
        settings = _run_sync(cm, self._oauth_profile(cm, "profile-b"), tmp_path, settings_path)
        assert settings["model"] == "claude-opus-5-5"

    def test_oauth_switch_removes_pin_when_new_profile_list_is_known_and_lacks_it(self, tmp_setup):
        cm, tmp_path, settings_path = tmp_setup
        settings_path.write_text(json.dumps({"model": "claude-opus-5-5", "env": {}}))
        a = self._oauth_profile(cm, "profile-a")
        cm.update_subscription_models(a["id"], ["claude-opus-5-5"], 0.0)
        _run_sync(cm, a, tmp_path, settings_path)
        b = self._oauth_profile(cm, "profile-b")
        cm.update_subscription_models(b["id"], ["claude-sonnet-5-5"], 0.0)
        settings = _run_sync(cm, b, tmp_path, settings_path)
        assert "model" not in settings
