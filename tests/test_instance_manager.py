"""Unit tests for InstanceManager.generate_env — .env generering uden PM2."""
import tempfile
import shutil
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import claude_mux as hs


@pytest.fixture()
def tmp_setup():
    d = Path(tempfile.mkdtemp())
    cm = hs.ConfigManager(data_file=d / "subs.json")
    # Patch CLAUDE_MUX_DIR så instans-mapper skrives til tmp
    with patch.object(hs, "CLAUDE_MUX_DIR", d / "claude-mux"):
        im = hs.InstanceManager(cm)
        yield cm, im, d
    shutil.rmtree(d, ignore_errors=True)


class TestGenerateEnvBearer:
    def test_sets_base_url(self, tmp_setup):
        cm, im, _ = tmp_setup
        sub = cm.add_subscription("ds", "https://api.deepseek.com/v1", "DS_KEY", auth_type="bearer")
        cm.set_instance_port(sub["id"], 18082)
        with patch.dict("os.environ", {"DS_KEY": "sk-deep-token"}):
            env_path = im.generate_env(sub["id"])
        content = env_path.read_text()
        assert "ANTHROPIC_BASE_URL=https://api.deepseek.com/v1" in content

    def test_sets_api_key_from_env(self, tmp_setup):
        cm, im, _ = tmp_setup
        sub = cm.add_subscription("ds", "https://api.deepseek.com/v1", "MY_KEY", auth_type="bearer")
        cm.set_instance_port(sub["id"], 18082)
        with patch.dict("os.environ", {"MY_KEY": "my-secret-key"}):
            env_path = im.generate_env(sub["id"])
        content = env_path.read_text()
        assert "ANTHROPIC_API_KEY=my-secret-key" in content

    def test_sets_listen_addr(self, tmp_setup):
        cm, im, _ = tmp_setup
        sub = cm.add_subscription("ds", "https://api.deepseek.com/v1", "DS_KEY")
        cm.set_instance_port(sub["id"], 18085)
        with patch.dict("os.environ", {"DS_KEY": "x"}):
            env_path = im.generate_env(sub["id"])
        content = env_path.read_text()
        assert "LISTEN_ADDR=:18085" in content

    def test_sets_model_maps(self, tmp_setup):
        cm, im, _ = tmp_setup
        sub = cm.add_subscription("ds", "https://api.deepseek.com/v1", "DS_KEY",
                                   model_maps={"haiku": "deepseek-chat", "sonnet": "deepseek-reasoner"})
        cm.set_instance_port(sub["id"], 18082)
        with patch.dict("os.environ", {"DS_KEY": "x"}):
            env_path = im.generate_env(sub["id"])
        content = env_path.read_text()
        assert "MODEL_MAP_HAIKU=deepseek-chat" in content
        assert "MODEL_MAP_SONNET=deepseek-reasoner" in content

    def test_env_file_permissions_600(self, tmp_setup):
        cm, im, _ = tmp_setup
        sub = cm.add_subscription("ds", "https://api.deepseek.com/v1", "DS_KEY")
        cm.set_instance_port(sub["id"], 18082)
        with patch.dict("os.environ", {"DS_KEY": "x"}):
            env_path = im.generate_env(sub["id"])
        mode = oct(env_path.stat().st_mode)
        assert mode.endswith("600"), f"Forventede 600, fik {mode}"

    def test_missing_subscription_raises(self, tmp_setup):
        _, im, _ = tmp_setup
        with pytest.raises(ValueError, match="not found"):
            im.generate_env("non-existent-id")

    def test_api_key_stored_in_sub_takes_priority(self, tmp_setup):
        """api_key direkte på subscription bruges frem for env var."""
        cm, im, _ = tmp_setup
        sub = cm.add_subscription("oauth", "", "CLAUDE_CODE_OAUTH_TOKEN", auth_type="oauth")
        cm.update_subscription(sub["id"], api_key="sk-ant-direct-token")
        cm.set_instance_port(sub["id"], 0)
        with patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "env-var-should-not-win"}):
            env_path = im.generate_env(sub["id"])
        content = env_path.read_text()
        assert "sk-ant-direct-token" in content
        assert "env-var-should-not-win" not in content


class TestGenerateEnvGhToken:
    def test_falls_back_to_env_if_gh_fails(self, tmp_setup):
        cm, im, _ = tmp_setup
        sub = cm.add_subscription("copilot", "https://api.githubcopilot.com", "GH_TOKEN",
                                   auth_type="gh_token")
        cm.set_instance_port(sub["id"], 18083)
        mock_result = type("R", (), {"returncode": 1, "stdout": ""})()
        with patch("subprocess.run", return_value=mock_result):
            with patch.dict("os.environ", {"GH_TOKEN": "fallback-token"}):
                env_path = im.generate_env(sub["id"])
        content = env_path.read_text()
        assert "ANTHROPIC_API_KEY=fallback-token" in content

    def test_uses_gh_auth_token_if_available(self, tmp_setup):
        cm, im, _ = tmp_setup
        sub = cm.add_subscription("copilot", "https://api.githubcopilot.com", "GH_TOKEN",
                                   auth_type="gh_token")
        cm.set_instance_port(sub["id"], 18083)
        mock_result = type("R", (), {"returncode": 0, "stdout": "gh-live-token\n"})()
        with patch("subprocess.run", return_value=mock_result):
            env_path = im.generate_env(sub["id"])
        content = env_path.read_text()
        assert "ANTHROPIC_API_KEY=gh-live-token" in content


class TestConfigManagerEdgeCases:
    def test_delete_default_clears_default(self):
        d = Path(tempfile.mkdtemp())
        try:
            cm = hs.ConfigManager(data_file=d / "subs.json")
            sub = cm.add_subscription("only", "http://x", "K")
            cm.set_default(sub["id"])
            assert cm.default_instance == sub["id"]
            cm.delete_subscription(sub["id"])
            assert cm.default_instance is None or cm.default_instance != sub["id"]
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_multiple_subs_isolation(self):
        """To uafhængige ConfigManagers deler IKKE state."""
        d1 = Path(tempfile.mkdtemp())
        d2 = Path(tempfile.mkdtemp())
        try:
            cm1 = hs.ConfigManager(data_file=d1 / "s.json")
            cm2 = hs.ConfigManager(data_file=d2 / "s.json")
            cm1.add_subscription("a", "http://a", "K")
            assert len(cm2.subscriptions) == 0
        finally:
            shutil.rmtree(d1, ignore_errors=True)
            shutil.rmtree(d2, ignore_errors=True)

    def test_update_model_maps_partial_merge(self):
        d = Path(tempfile.mkdtemp())
        try:
            cm = hs.ConfigManager(data_file=d / "s.json")
            sub = cm.add_subscription("x", "http://x", "K",
                                       model_maps={"haiku": "h1", "sonnet": "s1", "opus": "o1"})
            # Opdater kun sonnet
            cm.update_subscription(sub["id"], model_maps={"sonnet": "s2"})
            found = cm.get_subscription(sub["id"])
            assert found["model_maps"]["sonnet"] == "s2"
            # haiku og opus bevares
            assert found["model_maps"]["haiku"] == "h1"
            assert found["model_maps"]["opus"] == "o1"
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_get_pm2_name_format(self):
        d = Path(tempfile.mkdtemp())
        try:
            cm = hs.ConfigManager(data_file=d / "s.json")
            sub = cm.add_subscription("my-provider", "http://x", "K")
            pm2_name = cm.get_pm2_name(sub["id"])
            assert pm2_name is not None
            assert "my-provider" in pm2_name.lower() or "claude-mux" in pm2_name.lower()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_set_default_returns_false_for_missing(self):
        d = Path(tempfile.mkdtemp())
        try:
            cm = hs.ConfigManager(data_file=d / "s.json")
            result = cm.set_default("does-not-exist")
            assert result is False
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_subscriptions_property_returns_copy(self):
        """Mutation af returneret liste påvirker IKKE intern state."""
        d = Path(tempfile.mkdtemp())
        try:
            cm = hs.ConfigManager(data_file=d / "s.json")
            cm.add_subscription("a", "http://a", "K")
            subs = cm.subscriptions
            subs.clear()
            assert len(cm.subscriptions) == 1
        finally:
            shutil.rmtree(d, ignore_errors=True)
