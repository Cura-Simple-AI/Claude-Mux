"""Unit tests for ConfigManager — subscription CRUD og persistence."""
import tempfile
import shutil
from pathlib import Path

import pytest
import claude_mux as hs


@pytest.fixture()
def tmp_cm():
    """Frisk isoleret ConfigManager for hvert test."""
    d = Path(tempfile.mkdtemp())
    cm = hs.ConfigManager(data_file=d / "subscriptions.json")
    yield cm
    shutil.rmtree(d, ignore_errors=True)


class TestConfigManagerCRUD:
    def test_add_subscription(self, tmp_cm):
        sub = tmp_cm.add_subscription(
            name="test-sub",
            provider_url="http://localhost:18080",
            api_key_env="TEST_API_KEY",
            auth_type="bearer",
            model_maps={"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-4-6", "opus": "claude-opus-4-7"},
        )
        assert sub["name"] == "test-sub"
        assert sub["auth_type"] == "bearer"
        assert sub["model_maps"]["haiku"] == "claude-haiku-4-5"
        assert sub["label"] == "test-sub"  # label = name altid

    def test_get_subscription(self, tmp_cm):
        sub = tmp_cm.add_subscription("foo", "http://x", "FOO_KEY")
        found = tmp_cm.get_subscription(sub["id"])
        assert found is not None
        assert found["id"] == sub["id"]

    def test_get_subscription_unknown(self, tmp_cm):
        assert tmp_cm.get_subscription("non-existent-id") is None

    def test_update_model_maps(self, tmp_cm):
        sub = tmp_cm.add_subscription("foo", "http://x", "FOO_KEY",
                                       model_maps={"sonnet": "old-model"})
        tmp_cm.update_subscription(sub["id"], model_maps={"sonnet": "new-model"})
        found = tmp_cm.get_subscription(sub["id"])
        assert found["model_maps"]["sonnet"] == "new-model"

    def test_update_name_syncs_label(self, tmp_cm):
        sub = tmp_cm.add_subscription("original", "http://x", "KEY")
        tmp_cm.update_subscription(sub["id"], name="renamed")
        found = tmp_cm.get_subscription(sub["id"])
        assert found["name"] == "renamed"
        assert found["label"] == "renamed"

    def test_update_api_key_oauth(self, tmp_cm):
        sub = tmp_cm.add_subscription("oauth-sub", "", "CLAUDE_CODE_OAUTH_TOKEN",
                                       auth_type="oauth")
        tmp_cm.update_subscription(sub["id"], api_key="sk-ant-real-token-123")
        found = tmp_cm.get_subscription(sub["id"])
        assert found["api_key"] == "sk-ant-real-token-123"

    def test_delete_subscription(self, tmp_cm):
        sub = tmp_cm.add_subscription("to-delete", "http://x", "KEY")
        sub_id = sub["id"]
        assert tmp_cm.delete_subscription(sub_id) is True
        assert tmp_cm.get_subscription(sub_id) is None

    def test_delete_nonexistent(self, tmp_cm):
        assert tmp_cm.delete_subscription("does-not-exist") is False

    def test_persistence_on_disk(self):
        d = Path(tempfile.mkdtemp())
        try:
            data_file = d / "subscriptions.json"
            cm1 = hs.ConfigManager(data_file=data_file)
            sub = cm1.add_subscription("persist-me", "http://x", "KEY")
            cm2 = hs.ConfigManager(data_file=data_file)
            found = cm2.get_subscription(sub["id"])
            assert found is not None
            assert found["name"] == "persist-me"
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_set_and_clear_default(self, tmp_cm):
        sub = tmp_cm.add_subscription("default-test", "http://x", "KEY")
        tmp_cm.set_default(sub["id"])
        assert tmp_cm.default_instance == sub["id"]

    def test_subscriptions_list(self, tmp_cm):
        assert len(tmp_cm.subscriptions) == 0
        tmp_cm.add_subscription("a", "http://a", "A_KEY")
        tmp_cm.add_subscription("b", "http://b", "B_KEY")
        assert len(tmp_cm.subscriptions) == 2

    def test_update_ignores_non_updatable_fields(self, tmp_cm):
        sub = tmp_cm.add_subscription("foo", "http://x", "KEY")
        original_id = sub["id"]
        tmp_cm.update_subscription(sub["id"], id="hacked-id", created_at="1970")
        found = tmp_cm.get_subscription(original_id)
        assert found is not None
        assert found["id"] == original_id


class TestMigration:
    def test_removes_claude_backup_on_load(self):
        import json, tempfile, shutil
        d = Path(tempfile.mkdtemp())
        try:
            data_file = d / "subs.json"
            # Skriv data direkte med auto-oprettet sub
            raw = {
                "version": 1,
                "subscriptions": [
                    {"id": "abc", "name": "claude-backup", "notes": "", "label": "Claude Backup",
                     "auth_type": "bearer", "provider_url": "http://x", "api_key_env": "K",
                     "model_maps": {}, "created_at": "now", "updated_at": "now"},
                    {"id": "xyz", "name": "min-sub", "notes": "", "label": "Min Sub",
                     "auth_type": "bearer", "provider_url": "http://y", "api_key_env": "K2",
                     "model_maps": {}, "created_at": "now", "updated_at": "now"},
                ],
                "default_instance": "abc",
                "instances": {},
            }
            data_file.write_text(json.dumps(raw))
            cm = hs.ConfigManager(data_file=data_file)
            assert len(cm.subscriptions) == 1
            assert cm.subscriptions[0]["name"] == "min-sub"
            assert cm.default_instance != "abc"
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_removes_auto_custom_on_load(self):
        import json, tempfile, shutil
        d = Path(tempfile.mkdtemp())
        try:
            data_file = d / "subs.json"
            raw = {
                "version": 1,
                "subscriptions": [
                    {"id": "bad", "name": "auto-custom", "notes": "Backup af oprindelig Claude-konfiguration — genaktiver", "label": "x",
                     "auth_type": "bearer", "provider_url": "", "api_key_env": "", "model_maps": {},
                     "created_at": "now", "updated_at": "now"},
                ],
                "default_instance": None,
                "instances": {},
            }
            data_file.write_text(json.dumps(raw))
            cm = hs.ConfigManager(data_file=data_file)
            assert len(cm.subscriptions) == 0
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_normal_subs_not_removed(self):
        import json, tempfile, shutil
        d = Path(tempfile.mkdtemp())
        try:
            data_file = d / "subs.json"
            raw = {
                "version": 1,
                "subscriptions": [
                    {"id": "ok1", "name": "deepseek", "notes": "", "label": "DeepSeek",
                     "auth_type": "bearer", "provider_url": "https://api.deepseek.com/v1", "api_key_env": "DS_KEY",
                     "model_maps": {}, "created_at": "now", "updated_at": "now"},
                ],
                "default_instance": "ok1",
                "instances": {},
            }
            data_file.write_text(json.dumps(raw))
            cm = hs.ConfigManager(data_file=data_file)
            assert len(cm.subscriptions) == 1
            assert cm.default_instance == "ok1"
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestMigrationPersistence:
    def test_migration_saves_to_disk(self):
        """Migration must persist to disk — so it doesn't re-run on next start."""
        import json, tempfile, shutil
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        try:
            data_file = d / "subs.json"
            # Write a file with an auto-created sub that migration should remove
            raw = {
                "version": 1,
                "subscriptions": [
                    {"id": "real-1", "name": "claude-max", "notes": ""},
                    {"id": "auto-1", "name": "claude-backup", "notes": ""},
                ],
                "default_instance": None,
                "instances": {},
            }
            data_file.write_text(json.dumps(raw))

            # First load — should migrate and save
            cm1 = hs.ConfigManager(data_file=data_file)
            assert len(cm1.subscriptions) == 1  # auto-sub removed in memory

            # Second load — file should already be clean, migration should NOT re-run
            on_disk = json.loads(data_file.read_text())
            assert len(on_disk["subscriptions"]) == 1, (
                "Migration did not persist to disk — re-runs every start"
            )

            cm2 = hs.ConfigManager(data_file=data_file)
            assert len(cm2.subscriptions) == 1
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_migration_idempotent_no_auto_subs(self):
        """If no auto-subs exist, migration makes no changes and no extra save."""
        import json, tempfile, shutil
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        try:
            data_file = d / "subs.json"
            raw = {
                "version": 1,
                "subscriptions": [
                    {"id": "r1", "name": "deepseek", "notes": ""},
                ],
                "default_instance": None,
                "instances": {},
            }
            data_file.write_text(json.dumps(raw))
            mtime_before = data_file.stat().st_mtime

            hs.ConfigManager(data_file=data_file)

            mtime_after = data_file.stat().st_mtime
            # File should not have been rewritten (no migration needed)
            assert mtime_before == mtime_after, "File was rewritten unnecessarily"
        finally:
            shutil.rmtree(d, ignore_errors=True)
