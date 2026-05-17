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
    # Prevent real claude CLI from interfering — unit tests use settings.json fallback only
    import claude_mux.sync as _sync_mod
    monkeypatch.setattr(_sync_mod.subprocess, "run",
        lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": ""})())
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


class TestDetectActiveMixedAuth:
    """#22 — detect_active with oauth + direct + bearer in same subscriptions.json."""

    def test_oauth_wins_when_token_matches(self, cm, sync, monkeypatch):
        """OAuth sub matched by token even when bearer sub has matching port."""
        oauth_sub = cm.add_subscription("max", "", "OAUTH_ENV", auth_type="oauth")
        cm.update_subscription(oauth_sub["id"], api_key="sk-ant-tok-AAA")
        bearer_sub = cm.add_subscription("ds", "https://api.deepseek.com/v1", "DS_KEY", auth_type="bearer")
        cm.set_instance_port(bearer_sub["id"], 18080)
        # Settings has oauth token — no localhost URL
        _write_settings(sync, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-tok-AAA"})
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        assert sync.detect_active() == oauth_sub["id"]

    def test_direct_sub_matched_by_base_url(self, cm, sync, monkeypatch):
        """Direct sub matched by provider_url in ANTHROPIC_BASE_URL."""
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        cm.add_subscription("max", "", "OAUTH_ENV", auth_type="oauth")  # no token stored
        direct_sub = cm.add_subscription("zai", "https://api.z.ai/v1", "Z_KEY", auth_type="direct")
        cm.add_subscription("ds", "https://api.deepseek.com/v1", "DS_KEY", auth_type="bearer")
        _write_settings(sync, {"ANTHROPIC_BASE_URL": "https://api.z.ai/v1"})
        assert sync.detect_active() == direct_sub["id"]

    def test_bearer_sub_matched_by_port_when_others_dont_match(self, cm, sync, monkeypatch):
        """Bearer sub matched by port when OAuth token and direct URL don't match."""
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        cm.add_subscription("max", "", "OAUTH_ENV", auth_type="oauth")
        cm.add_subscription("zai", "https://api.z.ai/v1", "Z_KEY", auth_type="direct")
        bearer_sub = cm.add_subscription("ds", "https://api.deepseek.com/v1", "DS_KEY", auth_type="bearer")
        cm.set_instance_port(bearer_sub["id"], 18099)
        _write_settings(sync, {"ANTHROPIC_BASE_URL": "http://localhost:18099"})
        assert sync.detect_active() == bearer_sub["id"]

    def test_no_match_when_all_auth_types_present_but_none_match(self, cm, sync, monkeypatch):
        """Returns None when none of the mixed-auth subs match current settings."""
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        cm.add_subscription("max", "", "OAUTH_ENV", auth_type="oauth")
        cm.add_subscription("zai", "https://api.z.ai/v1", "Z_KEY", auth_type="direct")
        bearer_sub = cm.add_subscription("ds", "https://api.deepseek.com/v1", "DS_KEY", auth_type="bearer")
        cm.set_instance_port(bearer_sub["id"], 18099)
        # Settings has a different localhost port
        _write_settings(sync, {"ANTHROPIC_BASE_URL": "http://localhost:19999"})
        assert sync.detect_active() is None


def _make_claude_auth_output(base_url: str = "") -> str:
    """Build a minimal `claude auth status --text` output."""
    lines = ["Logged in as: test@example.com"]
    if base_url:
        lines.append(f"Anthropic base URL: {base_url}")
    return "\n".join(lines)


def _mock_claude_run(output: str):
    """Return a mock subprocess.run that succeeds with the given stdout."""
    return lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": output})()


class TestDetectActiveViaClaudeAuthStatus:
    """Strategy 1 path — claude auth status succeeds."""

    def test_multi_oauth_matches_by_token_not_first(self, cm, tmp_path, monkeypatch):
        """When two OAuth subs exist, detect_active picks the one whose token
        matches settings.json — NOT simply the first in the list."""
        import claude_mux.sync as _sync_mod

        sub_first = cm.add_subscription("troels", "", "", auth_type="oauth")
        cm.update_subscription(sub_first["id"], api_key="sk-ant-TROELS")
        sub_second = cm.add_subscription("ada", "", "", auth_type="oauth")
        cm.update_subscription(sub_second["id"], api_key="sk-ant-ADA")

        sync = SyncManager(cm)
        monkeypatch.setattr(SyncManager, "SETTINGS_PATH", tmp_path / "settings.json")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        # Activate ada: write ada's token to settings.json
        _write_settings(sync, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-ADA"})

        # claude auth status returns no base URL → OAuth path
        monkeypatch.setattr(_sync_mod.subprocess, "run",
                            _mock_claude_run(_make_claude_auth_output()))

        assert sync.detect_active() == sub_second["id"]

    def test_multi_oauth_first_sub_also_works(self, cm, tmp_path, monkeypatch):
        """Token-match also works when the active sub happens to be first."""
        import claude_mux.sync as _sync_mod

        sub_first = cm.add_subscription("troels", "", "", auth_type="oauth")
        cm.update_subscription(sub_first["id"], api_key="sk-ant-TROELS")
        cm.add_subscription("ada", "", "", auth_type="oauth")  # second, not activated

        sync = SyncManager(cm)
        monkeypatch.setattr(SyncManager, "SETTINGS_PATH", tmp_path / "settings.json")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        _write_settings(sync, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-TROELS"})
        monkeypatch.setattr(_sync_mod.subprocess, "run",
                            _mock_claude_run(_make_claude_auth_output()))

        assert sync.detect_active() == sub_first["id"]

    def test_no_token_in_settings_falls_back_to_first_oauth(self, cm, tmp_path, monkeypatch):
        """No token in settings → fallback: return first oauth sub (pre-fix behaviour)."""
        import claude_mux.sync as _sync_mod

        sub_first = cm.add_subscription("troels", "", "", auth_type="oauth")
        cm.add_subscription("ada", "", "", auth_type="oauth")

        sync = SyncManager(cm)
        monkeypatch.setattr(SyncManager, "SETTINGS_PATH", tmp_path / "settings.json")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        _write_settings(sync, {})  # no token
        monkeypatch.setattr(_sync_mod.subprocess, "run",
                            _mock_claude_run(_make_claude_auth_output()))

        assert sync.detect_active() == sub_first["id"]

    def test_bearer_sub_matched_via_claude_auth_output(self, cm, tmp_path, monkeypatch):
        """Bearer sub matched by localhost port from claude auth status output."""
        import claude_mux.sync as _sync_mod

        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        sub = cm.add_subscription("ds", "https://api.deepseek.com/v1", "DS_KEY", auth_type="bearer")
        cm.set_instance_port(sub["id"], 18082)

        sync = SyncManager(cm)
        monkeypatch.setattr(SyncManager, "SETTINGS_PATH", tmp_path / "settings.json")
        _write_settings(sync, {})

        monkeypatch.setattr(_sync_mod.subprocess, "run",
                            _mock_claude_run(_make_claude_auth_output("http://localhost:18082")))

        assert sync.detect_active() == sub["id"]
