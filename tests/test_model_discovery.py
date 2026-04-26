"""Tests for model discovery, caching, and tier resolution.

Covers:
- ConfigManager.update_subscription_models
- ConfigManager.add/remove/get blacklisted_models
- SyncManager.fetch_available_models (mocked HTTP)
- SyncManager.resolve_model_for_tier
"""
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError

import pytest

import claude_mux as hs
from claude_mux.sync import SyncManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _cm(d):
    return hs.ConfigManager(data_file=d / "subs.json")


# ---------------------------------------------------------------------------
# ConfigManager — model cache methods
# ---------------------------------------------------------------------------

class TestUpdateSubscriptionModels:
    def test_persists_available_models(self, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("test", "http://x", "K")
        models = ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"]
        cm.update_subscription_models(sub["id"], models, 1745000000.0)
        reloaded = _cm(tmp_dir).get_subscription(sub["id"])
        assert reloaded["available_models"] == models
        assert reloaded["models_fetched_at"] == 1745000000.0

    def test_persists_fetch_failure(self, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("test", "http://x", "K")
        cm.update_subscription_models(sub["id"], [], None)
        reloaded = _cm(tmp_dir).get_subscription(sub["id"])
        assert reloaded["available_models"] == []
        assert reloaded["models_fetched_at"] is None

    def test_caps_at_200(self, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("test", "http://x", "K")
        models = [f"model-{i}" for i in range(300)]
        cm.update_subscription_models(sub["id"], models, 1.0)
        reloaded = _cm(tmp_dir).get_subscription(sub["id"])
        assert len(reloaded["available_models"]) == 200

    def test_returns_false_for_unknown_sub(self, tmp_dir):
        cm = _cm(tmp_dir)
        result = cm.update_subscription_models("nonexistent-id", [], None)
        assert result is False


# ---------------------------------------------------------------------------
# ConfigManager — blacklist methods
# ---------------------------------------------------------------------------

class TestBlacklist:
    def test_add_blacklisted_model(self, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("test", "http://x", "K")
        result = cm.add_blacklisted_model(sub["id"], "claude-opus-4-6")
        assert result is True
        assert "claude-opus-4-6" in cm.get_blacklisted_models(sub["id"])

    def test_add_duplicate_is_idempotent(self, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("test", "http://x", "K")
        cm.add_blacklisted_model(sub["id"], "model-x")
        cm.add_blacklisted_model(sub["id"], "model-x")
        assert cm.get_blacklisted_models(sub["id"]).count("model-x") == 1

    def test_remove_blacklisted_model(self, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("test", "http://x", "K")
        cm.add_blacklisted_model(sub["id"], "claude-opus-4-6")
        cm.remove_blacklisted_model(sub["id"], "claude-opus-4-6")
        assert "claude-opus-4-6" not in cm.get_blacklisted_models(sub["id"])

    def test_get_empty_blacklist(self, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("test", "http://x", "K")
        assert cm.get_blacklisted_models(sub["id"]) == []

    def test_returns_false_for_unknown_sub(self, tmp_dir):
        cm = _cm(tmp_dir)
        assert cm.add_blacklisted_model("bad-id", "model") is False
        assert cm.remove_blacklisted_model("bad-id", "model") is False
        assert cm.get_blacklisted_models("bad-id") == []

    def test_persists_across_reload(self, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("test", "http://x", "K")
        cm.add_blacklisted_model(sub["id"], "model-abc")
        reloaded = _cm(tmp_dir).get_subscription(sub["id"])
        assert "model-abc" in reloaded.get("blacklisted_models", [])


# ---------------------------------------------------------------------------
# SyncManager.fetch_available_models
# ---------------------------------------------------------------------------

def _make_models_response(model_ids: list[str]) -> bytes:
    return json.dumps({"data": [{"id": mid} for mid in model_ids]}).encode()


class TestFetchAvailableModels:
    def _make_urlopen(self, body: bytes, status: int = 200):
        """Return a mock urlopen context manager."""
        cm_mock = MagicMock()
        cm_mock.__enter__ = MagicMock(return_value=cm_mock)
        cm_mock.__exit__ = MagicMock(return_value=False)
        cm_mock.getcode.return_value = status
        cm_mock.read.return_value = body
        return cm_mock

    def test_parses_model_ids(self, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("test", "http://x", "K",
                                  auth_type="bearer", api_key="tok")
        cm.set_instance_port(sub["id"], 18080)
        sync = SyncManager(cm)
        body = _make_models_response(["claude-haiku-4-5-20251001", "claude-sonnet-4-6"])
        mock_resp = self._make_urlopen(body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            models = sync.fetch_available_models(sub["id"])
        assert models == ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"]
        reloaded = _cm(tmp_dir).get_subscription(sub["id"])
        assert reloaded["available_models"] == models
        assert reloaded["models_fetched_at"] is not None

    def test_returns_empty_on_http_error(self, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("test", "http://x", "K",
                                  auth_type="bearer", api_key="tok")
        cm.set_instance_port(sub["id"], 18080)
        sync = SyncManager(cm)
        http_err = HTTPError("http://localhost:18080/v1/models", 401, "Unauthorized", {}, None)
        with patch("urllib.request.urlopen", side_effect=http_err):
            models = sync.fetch_available_models(sub["id"])
        assert models == []
        reloaded = _cm(tmp_dir).get_subscription(sub["id"])
        assert reloaded["models_fetched_at"] is None

    def test_returns_empty_on_connection_error(self, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("test", "http://x", "K",
                                  auth_type="bearer", api_key="tok")
        cm.set_instance_port(sub["id"], 18080)
        sync = SyncManager(cm)
        with patch("urllib.request.urlopen", side_effect=URLError("Connection refused")):
            models = sync.fetch_available_models(sub["id"])
        assert models == []

    def test_returns_empty_when_no_port(self, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("test", "http://x", "K", auth_type="bearer")
        sync = SyncManager(cm)
        # No port set → should return [] without making HTTP call
        with patch("urllib.request.urlopen") as mock_open:
            models = sync.fetch_available_models(sub["id"])
        assert models == []
        mock_open.assert_not_called()

    def test_oauth_uses_anthropic_url(self, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("claude-max", "", "",
                                  auth_type="oauth", api_key="oauth-token-xyz")
        sync = SyncManager(cm)
        body = _make_models_response(["claude-haiku-4-5-20251001"])
        mock_resp = self._make_urlopen(body)
        captured_reqs = []
        def _fake_urlopen(req, timeout=10):
            captured_reqs.append(req)
            return mock_resp
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            models = sync.fetch_available_models(sub["id"])
        assert models == ["claude-haiku-4-5-20251001"]
        assert len(captured_reqs) == 1
        assert "api.anthropic.com" in captured_reqs[0].full_url

    def test_unknown_sub_returns_empty(self, tmp_dir):
        cm = _cm(tmp_dir)
        sync = SyncManager(cm)
        models = sync.fetch_available_models("nonexistent-id")
        assert models == []


# ---------------------------------------------------------------------------
# SyncManager.resolve_model_for_tier
# ---------------------------------------------------------------------------

class TestResolveModelForTier:
    def _make_sub(self, model_maps=None, available_models=None, blacklisted_models=None):
        return {
            "id": "test-id",
            "name": "test",
            "model_maps": model_maps or {},
            "available_models": available_models or [],
            "blacklisted_models": blacklisted_models or [],
        }

    def test_returns_model_map_first(self, tmp_dir):
        cm = _cm(tmp_dir)
        sync = SyncManager(cm)
        sub = self._make_sub(
            model_maps={"haiku": "deepseek-chat"},
            available_models=["claude-haiku-4-5-20251001"],
        )
        assert sync.resolve_model_for_tier(sub, "haiku") == "deepseek-chat"

    def test_falls_back_to_available_models(self, tmp_dir):
        cm = _cm(tmp_dir)
        sync = SyncManager(cm)
        sub = self._make_sub(
            model_maps={},
            available_models=["claude-haiku-4-5-20251001", "claude-sonnet-4-6"],
        )
        assert sync.resolve_model_for_tier(sub, "haiku") == "claude-haiku-4-5-20251001"
        assert sync.resolve_model_for_tier(sub, "sonnet") == "claude-sonnet-4-6"

    def test_returns_none_when_no_match(self, tmp_dir):
        cm = _cm(tmp_dir)
        sync = SyncManager(cm)
        sub = self._make_sub(model_maps={}, available_models=[])
        assert sync.resolve_model_for_tier(sub, "haiku") is None

    def test_skips_blacklisted_model_map(self, tmp_dir):
        cm = _cm(tmp_dir)
        sync = SyncManager(cm)
        sub = self._make_sub(
            model_maps={"haiku": "blacklisted-model"},
            available_models=["claude-haiku-4-5-20251001"],
            blacklisted_models=["blacklisted-model"],
        )
        # model_maps entry is blacklisted → fall through to available_models
        assert sync.resolve_model_for_tier(sub, "haiku") == "claude-haiku-4-5-20251001"

    def test_skips_blacklisted_available_models(self, tmp_dir):
        cm = _cm(tmp_dir)
        sync = SyncManager(cm)
        sub = self._make_sub(
            model_maps={},
            available_models=["claude-haiku-4-5-20251001", "claude-haiku-alt"],
            blacklisted_models=["claude-haiku-4-5-20251001"],
        )
        # First haiku match is blacklisted → returns second
        assert sync.resolve_model_for_tier(sub, "haiku") == "claude-haiku-alt"

    def test_returns_none_when_all_blacklisted(self, tmp_dir):
        cm = _cm(tmp_dir)
        sync = SyncManager(cm)
        sub = self._make_sub(
            model_maps={"haiku": "claude-haiku-4-5-20251001"},
            available_models=["claude-haiku-4-5-20251001"],
            blacklisted_models=["claude-haiku-4-5-20251001"],
        )
        assert sync.resolve_model_for_tier(sub, "haiku") is None

    def test_case_insensitive_tier_match(self, tmp_dir):
        cm = _cm(tmp_dir)
        sync = SyncManager(cm)
        sub = self._make_sub(
            model_maps={},
            available_models=["Claude-Haiku-4-5"],
        )
        # "haiku" should match "Claude-Haiku-4-5" case-insensitively
        assert sync.resolve_model_for_tier(sub, "haiku") == "Claude-Haiku-4-5"

    def test_empty_model_map_value_falls_through(self, tmp_dir):
        cm = _cm(tmp_dir)
        sync = SyncManager(cm)
        sub = self._make_sub(
            model_maps={"haiku": ""},  # empty string
            available_models=["claude-haiku-4-5-20251001"],
        )
        # Empty string in model_maps → fall through to available_models
        assert sync.resolve_model_for_tier(sub, "haiku") == "claude-haiku-4-5-20251001"
