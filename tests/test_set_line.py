"""Tests for InstanceManager._set_line og generate_env key-append."""
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
import claude_mux as hs


class TestSetLine:
    def test_updates_existing_key(self):
        lines = ["FOO=old", "BAR=keep"]
        hs.InstanceManager._set_line(lines, "FOO", "new")
        assert lines == ["FOO=new", "BAR=keep"]

    def test_appends_missing_key(self):
        """Nøgle der ikke er i template skal tilføjes i stedet for at droppes."""
        lines = ["FOO=x"]
        hs.InstanceManager._set_line(lines, "NEW_KEY", "val")
        assert "NEW_KEY=val" in lines

    def test_updates_only_first_match(self):
        lines = ["KEY=a", "KEY=b"]
        hs.InstanceManager._set_line(lines, "KEY", "c")
        assert lines[0] == "KEY=c"
        assert lines[1] == "KEY=b"

    def test_empty_value_written(self):
        lines = ["FOO=old"]
        hs.InstanceManager._set_line(lines, "FOO", "")
        assert lines[0] == "FOO="

    def test_generate_env_appends_new_key(self):
        """Hvis .env-template mangler en key, tilføjes den stadig."""
        d = Path(tempfile.mkdtemp())
        try:
            cm = hs.ConfigManager(data_file=d / "subs.json")
            sub = cm.add_subscription("test", "http://localhost:18082", "MY_KEY", auth_type="bearer",
                                       model_maps={"haiku": "h", "sonnet": "s", "opus": "o"})
            cm.set_instance_port(sub["id"], 18082)

            with patch.object(hs, "CLAUDE_MUX_DIR", d / "claude-mux"):
                im = hs.InstanceManager(cm)
                # Fjern ANTHROPIC_API_KEY fra template temporært
                original = dict(hs.ENV_TEMPLATE_KEYS)
                reduced = {k: v for k, v in original.items() if k != "ANTHROPIC_API_KEY"}
                with patch.dict("claude_mux.ENV_TEMPLATE_KEYS", reduced, clear=True):
                    with patch.dict("os.environ", {"MY_KEY": "secret"}):
                        env_path = im.generate_env(sub["id"])
            content = env_path.read_text()
            # ANTHROPIC_API_KEY skal stadig skrives via append
            assert "ANTHROPIC_API_KEY=secret" in content
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestMigrationEdgeCases:
    def test_default_cleared_when_pointing_to_removed_sub(self):
        """Migration sætter default til None hvis den pegede på fjernet sub."""
        data = {
            "version": 1,
            "subscriptions": [
                {"id": "bad", "name": "claude-backup", "notes": "", "label": "x",
                 "auth_type": "bearer", "provider_url": "", "api_key_env": "",
                 "model_maps": {}, "created_at": "", "updated_at": ""},
            ],
            "default_instance": "bad",
            "instances": {},
        }
        result = hs.ConfigManager._migrate(data)
        assert result["default_instance"] is None

    def test_default_preserved_when_sub_remains(self):
        """Migration bevarer default hvis sub ikke fjernes."""
        data = {
            "version": 1,
            "subscriptions": [
                {"id": "ok", "name": "deepseek", "notes": "", "label": "DS",
                 "auth_type": "bearer", "provider_url": "http://x", "api_key_env": "K",
                 "model_maps": {}, "created_at": "", "updated_at": ""},
            ],
            "default_instance": "ok",
            "instances": {},
        }
        result = hs.ConfigManager._migrate(data)
        assert result["default_instance"] == "ok"
        assert len(result["subscriptions"]) == 1

    def test_note_pattern_matches_partial(self):
        """Fjern sub der har auto-notes selv om navn er anderledes."""
        data = {
            "version": 1,
            "subscriptions": [
                {"id": "x", "name": "andet-navn", "notes": "Backup af oprindelig Claude-konfiguration — url",
                 "label": "", "auth_type": "bearer", "provider_url": "", "api_key_env": "",
                 "model_maps": {}, "created_at": "", "updated_at": ""},
            ],
            "default_instance": None,
            "instances": {},
        }
        result = hs.ConfigManager._migrate(data)
        assert len(result["subscriptions"]) == 0

    def test_empty_subscriptions_no_crash(self):
        data = {"version": 1, "subscriptions": [], "default_instance": None, "instances": {}}
        result = hs.ConfigManager._migrate(data)
        assert result["subscriptions"] == []
