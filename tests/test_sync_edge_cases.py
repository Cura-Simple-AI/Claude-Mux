"""SyncManager edge cases: merge med eksisterende settings, model maps, force-override."""
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
import claude_mux as hs


def make_cm_sync(tmp_path, settings_path):
    cm = hs.ConfigManager(data_file=tmp_path / "subs.json")

    class _Sync(hs.SyncManager):
        SETTINGS_PATH = settings_path
        CLAUDE_MUX_DOT_ENV = tmp_path / ".env"

    return cm, _Sync(cm)


class TestSyncMergeExistingSettings:
    def test_preserves_existing_unrelated_keys(self):
        d = Path(tempfile.mkdtemp())
        try:
            sp = d / "settings.json"
            sp.write_text(json.dumps({"env": {"MY_CUSTOM_VAR": "keep-me"}, "other": True}))
            cm, sync = make_cm_sync(d, sp)
            sub = cm.add_subscription("s", "", "CLAUDE_CODE_OAUTH_TOKEN", auth_type="oauth")
            cm.update_subscription(sub["id"], api_key="sk-ant-x")
            cm.set_instance_port(sub["id"], 0)
            with patch.object(hs.InstanceManager, "generate_env", return_value=d / "fake.env"):
                with patch("shutil.copy2"):
                    sync.sync_default(sub["id"])
            s = json.loads(sp.read_text())
            # Bevar ikke-relaterede env vars
            assert s["env"].get("MY_CUSTOM_VAR") == "keep-me"
            assert s["other"] is True
            # OAuth token sat
            assert "CLAUDE_CODE_OAUTH_TOKEN" in s["env"]
        finally:
            shutil.rmtree(d)

    def test_oauth_removes_proxy_keys(self):
        d = Path(tempfile.mkdtemp())
        try:
            sp = d / "settings.json"
            sp.write_text(json.dumps({
                "env": {
                    "ANTHROPIC_BASE_URL": "http://localhost:18080",
                    "ANTHROPIC_AUTH_TOKEN": "old-bearer-key",
                }
            }))
            cm, sync = make_cm_sync(d, sp)
            sub = cm.add_subscription("oauth", "", "CLAUDE_CODE_OAUTH_TOKEN", auth_type="oauth")
            cm.update_subscription(sub["id"], api_key="sk-ant-new")
            cm.set_instance_port(sub["id"], 0)
            with patch.object(hs.InstanceManager, "generate_env", return_value=d / "fake.env"):
                with patch("shutil.copy2"):
                    sync.sync_default(sub["id"])
            s = json.loads(sp.read_text())
            assert "ANTHROPIC_BASE_URL" not in s["env"]
            assert "ANTHROPIC_AUTH_TOKEN" not in s["env"]
            assert s["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-new"
        finally:
            shutil.rmtree(d)

    def test_bearer_removes_old_oauth_token(self):
        """OAuth → bearer: CLAUDE_CODE_OAUTH_TOKEN skal fjernes så Claude ikke bruger det."""
        d = Path(tempfile.mkdtemp())
        try:
            sp = d / "settings.json"
            sp.write_text(json.dumps({
                "env": {"CLAUDE_CODE_OAUTH_TOKEN": "old-oauth-token"}
            }))
            cm, sync = make_cm_sync(d, sp)
            sub = cm.add_subscription("proxy", "http://localhost:18082", "DS_KEY", auth_type="bearer")
            cm.set_instance_port(sub["id"], 18082)
            with patch.object(hs.InstanceManager, "generate_env", return_value=d / "fake.env"):
                with patch("shutil.copy2"):
                    with patch.dict("os.environ", {"DS_KEY": "ds-token"}):
                        sync.sync_default(sub["id"])
            s = json.loads(sp.read_text())
            assert "localhost:18082" in s["env"]["ANTHROPIC_BASE_URL"]
            # OAuth token SKAL fjernes ved skift til bearer
            assert "CLAUDE_CODE_OAUTH_TOKEN" not in s["env"]
        finally:
            shutil.rmtree(d)

    def test_model_maps_empty_values_not_written(self):
        """Tomme model maps skal IKKE overskriv eksisterende settings."""
        d = Path(tempfile.mkdtemp())
        try:
            sp = d / "settings.json"
            sp.write_text(json.dumps({"env": {
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "previous-sonnet",
            }}))
            cm, sync = make_cm_sync(d, sp)
            sub = cm.add_subscription("oauth", "", "CLAUDE_CODE_OAUTH_TOKEN", auth_type="oauth",
                                       model_maps={})  # tomme maps
            cm.update_subscription(sub["id"], api_key="sk-ant-x")
            cm.set_instance_port(sub["id"], 0)
            with patch.object(hs.InstanceManager, "generate_env", return_value=d / "fake.env"):
                with patch("shutil.copy2"):
                    sync.sync_default(sub["id"])
            s = json.loads(sp.read_text())
            # Tomme maps → IKKE overskriv eksisterende model settings
            # (SyncManager skriver kun hvis model_maps.get("x") er truthy)
            assert s["env"].get("ANTHROPIC_DEFAULT_SONNET_MODEL") == "previous-sonnet"
        finally:
            shutil.rmtree(d)
