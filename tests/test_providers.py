"""Tests for alle 8 providers — generate_env + sync_default per auth_type."""
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import claude_mux as hs

PROVIDERS = [
    ("deepseek",   "https://api.deepseek.com/v1",                               "bearer",       "DEEPSEEK_API_KEY",          {"haiku": "deepseek-chat", "sonnet": "deepseek-chat", "opus": "deepseek-reasoner"}),
    ("anthropic",  "https://api.anthropic.com/v1",                              "bearer",       "ANTHROPIC_API_KEY",         {}),
    ("openai",     "https://api.openai.com/v1",                                 "bearer",       "OPENAI_API_KEY",            {"haiku": "gpt-4o-mini", "sonnet": "gpt-4o", "opus": "o1-mini"}),
    ("copilot",    "https://api.githubcopilot.com",                             "gh_token",     "GH_TOKEN",                  {"haiku": "claude-haiku-4.5", "sonnet": "claude-sonnet-4.6", "opus": "claude-opus-4.7"}),
    ("gemini",     "https://generativelanguage.googleapis.com/v1beta/openai",   "x-goog-api-key", "GEMINI_API_KEY",          {"haiku": "gemini-2.0-flash", "sonnet": "gemini-2.0-pro", "opus": "gemini-2.5-pro"}),
    ("z-ai",       "https://api.z.ai/v1",                                       "bearer",       "Z_AI_API_KEY",              {}),
    ("custom",     "https://my-custom.example.com/v1",                          "bearer",       "CUSTOM_API_KEY",            {}),
]


def _make_env(d, auth_type, name, provider_url, api_key_env, model_maps, port=18082, api_key=""):
    cm = hs.ConfigManager(data_file=d / "subs.json")
    sub = cm.add_subscription(name, provider_url, api_key_env,
                               auth_type=auth_type, model_maps=model_maps)
    cm.set_instance_port(sub["id"], port)
    if api_key:
        cm.update_subscription(sub["id"], api_key=api_key)
    return cm, sub


class TestGenerateEnv:
    @pytest.mark.parametrize("name,url,auth,env_var,maps", PROVIDERS)
    def test_env_contains_base_url(self, name, url, auth, env_var, maps):
        """generate_env skriver ANTHROPIC_BASE_URL for alle providers."""
        d = Path(tempfile.mkdtemp())
        try:
            cm, sub = _make_env(d, auth, name, url, env_var, maps)
            with patch("claude_mux.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="gh-token-123")
                im = hs.InstanceManager(cm)
                env_path = im.generate_env(sub["id"])
            content = env_path.read_text()
            assert url in content, f"ANTHROPIC_BASE_URL mangler for {name}"
        finally:
            shutil.rmtree(d)

    @pytest.mark.parametrize("name,url,auth,env_var,maps", PROVIDERS)
    def test_env_has_correct_permissions(self, name, url, auth, env_var, maps):
        """generate_env sætter chmod 600 på .env filen."""
        d = Path(tempfile.mkdtemp())
        try:
            cm, sub = _make_env(d, auth, name, url, env_var, maps)
            with patch("claude_mux.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="gh-token")
                im = hs.InstanceManager(cm)
                env_path = im.generate_env(sub["id"])
            mode = env_path.stat().st_mode & 0o777
            assert mode == 0o600, f".env permissions forkert for {name}: {oct(mode)}"
        finally:
            shutil.rmtree(d)

    def test_oauth_writes_oauth_token_to_env(self):
        """Claude Max: api_key (OAuth token) skrives til .env som CLAUDE_CODE_OAUTH_TOKEN."""
        d = Path(tempfile.mkdtemp())
        try:
            cm, sub = _make_env(d, "oauth", "claude-max", "", "CLAUDE_CODE_OAUTH_TOKEN", {},
                                 api_key="sk-ant-oauth-token-xyz")
            with patch.object(hs, "CLAUDE_MUX_DIR", d / "claude-mux"):
                im = hs.InstanceManager(cm)
                env_path = im.generate_env(sub["id"])
            content = env_path.read_text()
            assert "sk-ant-oauth-token-xyz" in content
        finally:
            shutil.rmtree(d)

    def test_bearer_writes_api_key_from_env(self):
        """Bearer: API key læses fra os.environ[api_key_env]."""
        d = Path(tempfile.mkdtemp())
        try:
            cm, sub = _make_env(d, "bearer", "deepseek", "https://api.deepseek.com/v1",
                                 "DEEPSEEK_API_KEY", {})
            with patch.object(hs, "CLAUDE_MUX_DIR", d / "claude-mux"):
                with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-deep-123"}):
                    im = hs.InstanceManager(cm)
                    env_path = im.generate_env(sub["id"])
            content = env_path.read_text()
            assert "sk-deep-123" in content
        finally:
            shutil.rmtree(d)

    def test_gh_token_writes_token_from_gh_cli(self):
        """Copilot: token hentes via 'gh auth token'."""
        d = Path(tempfile.mkdtemp())
        try:
            cm, sub = _make_env(d, "gh_token", "copilot", "https://api.githubcopilot.com",
                                 "GH_TOKEN", {})
            with patch.object(hs, "CLAUDE_MUX_DIR", d / "claude-mux"):
                with patch("claude_mux.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stdout="gho_copilot_token\n")
                    im = hs.InstanceManager(cm)
                    env_path = im.generate_env(sub["id"])
            content = env_path.read_text()
            assert "gho_copilot_token" in content
        finally:
            shutil.rmtree(d)

    def test_gh_token_fallback_to_env_on_cli_failure(self):
        """Copilot: fallback til GH_TOKEN env var hvis gh CLI fejler."""
        d = Path(tempfile.mkdtemp())
        try:
            cm, sub = _make_env(d, "gh_token", "copilot", "https://api.githubcopilot.com",
                                 "GH_TOKEN", {})
            with patch.object(hs, "CLAUDE_MUX_DIR", d / "claude-mux"):
                with patch("claude_mux.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1, stdout="")
                    with patch.dict("os.environ", {"GH_TOKEN": "env-fallback-token"}):
                        im = hs.InstanceManager(cm)
                        env_path = im.generate_env(sub["id"])
            content = env_path.read_text()
            assert "env-fallback-token" in content
        finally:
            shutil.rmtree(d)

    def test_model_maps_written_to_env(self):
        """Model maps skrives korrekt til .env."""
        d = Path(tempfile.mkdtemp())
        try:
            maps = {"haiku": "deepseek-chat", "sonnet": "deepseek-chat", "opus": "deepseek-reasoner"}
            cm, sub = _make_env(d, "bearer", "deepseek", "https://api.deepseek.com/v1",
                                 "DS_KEY", maps)
            with patch.object(hs, "CLAUDE_MUX_DIR", d / "claude-mux"):
                with patch.dict("os.environ", {"DS_KEY": "sk-test"}):
                    im = hs.InstanceManager(cm)
                    env_path = im.generate_env(sub["id"])
            content = env_path.read_text()
            assert "deepseek-chat" in content
            assert "deepseek-reasoner" in content
        finally:
            shutil.rmtree(d)


class TestSyncDefaultAllProviders:
    def _run_sync(self, d, sub, settings_path, env_overrides=None, api_key=None):
        class _Sync(hs.SyncManager):
            SETTINGS_PATH = settings_path
            HEIMSENSE_DOT_ENV = d / ".env"
        cm = sub[0]
        sub_obj = sub[1]
        sync = _Sync(cm)
        with patch.object(hs.InstanceManager, "generate_env", return_value=d / "fake.env"):
            with patch("shutil.copy2"):
                with patch.dict("os.environ", env_overrides or {}):
                    sync.sync_default(sub_obj["id"])
        return json.loads(settings_path.read_text())

    def test_bearer_sets_proxy_url(self):
        d = Path(tempfile.mkdtemp())
        try:
            cm, sub = _make_env(d, "bearer", "deepseek",
                                 "https://api.deepseek.com/v1", "DS_KEY", {}, port=18082)
            settings_path = d / "settings.json"
            s = self._run_sync(d, (cm, sub), settings_path, {"DS_KEY": "sk-test"})
            assert "localhost:18082" in s["env"].get("ANTHROPIC_BASE_URL", "")
            assert s["env"].get("ANTHROPIC_AUTH_TOKEN") == "sk-test"
            assert "CLAUDE_CODE_OAUTH_TOKEN" not in s["env"]
        finally:
            shutil.rmtree(d)

    def test_oauth_sets_token_removes_proxy(self):
        d = Path(tempfile.mkdtemp())
        try:
            cm, sub = _make_env(d, "oauth", "claude-max", "", "CLAUDE_CODE_OAUTH_TOKEN", {},
                                 api_key="sk-ant-tok")
            settings_path = d / "settings.json"
            s = self._run_sync(d, (cm, sub), settings_path)
            assert s["env"].get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-tok"
            assert "ANTHROPIC_BASE_URL" not in s["env"]
            assert "ANTHROPIC_AUTH_TOKEN" not in s["env"]
        finally:
            shutil.rmtree(d)

    def test_telemetry_disabled_for_all(self):
        """ANTHROPIC_DISABLE_TELEMETRY=true og CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 altid sat."""
        d = Path(tempfile.mkdtemp())
        try:
            cm, sub = _make_env(d, "bearer", "test", "http://x", "K", {}, port=18082)
            settings_path = d / "settings.json"
            s = self._run_sync(d, (cm, sub), settings_path, {"K": "tok"})
            assert s["env"].get("ANTHROPIC_DISABLE_TELEMETRY") == "true"
            assert s["env"].get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC") == "1"
        finally:
            shutil.rmtree(d)
