"""Tests for HeimsenseApp._run_proxy_test — isolated HTTP proxy test logic."""
import json
from io import BytesIO
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError

import pytest
import claude_mux as hs

# _run_proxy_test is a staticmethod on HeimsenseApp
run_proxy_test = hs.HeimsenseApp._run_proxy_test


class TestRunProxyTest:
    def _mock_resp(self, code: int, body: dict | str) -> MagicMock:
        """Build a mock urllib response context manager."""
        raw = json.dumps(body) if isinstance(body, dict) else body
        m = MagicMock()
        m.__enter__ = lambda s: s
        m.__exit__ = MagicMock(return_value=False)
        m.getcode.return_value = code
        m.read.return_value = raw.encode()
        return m

    def test_200_returns_ok_code(self):
        """200 → code=200, body contains text from response."""
        body = {"content": [{"text": "The universe is vast.", "type": "text"}]}
        with patch("urllib.request.urlopen", return_value=self._mock_resp(200, body)):
            result = run_proxy_test(18082, "sk-test", "claude-haiku-4-5")
        assert result["code"] == 200
        assert "universe" in result["body"]
        assert result["elapsed"] >= 0

    def test_200_body_truncated_at_500(self):
        """Response body is truncated to 500 chars."""
        long_text = "x" * 1000
        body = {"content": [{"text": long_text, "type": "text"}]}
        with patch("urllib.request.urlopen", return_value=self._mock_resp(200, body)):
            result = run_proxy_test(18082, "sk-test", "model")
        assert len(result["body"]) <= 500

    def test_429_returns_error_code(self):
        """HTTP 429 → code=429, body contains raw error."""
        err_body = b'{"error": "rate limit"}'
        http_err = HTTPError(url="http://x", code=429, msg="Too Many Requests",
                             hdrs={}, fp=BytesIO(err_body))
        with patch("urllib.request.urlopen", side_effect=http_err):
            result = run_proxy_test(18082, "sk-test", "model")
        assert result["code"] == 429

    def test_401_returns_error_code(self):
        """HTTP 401 → code=401."""
        err_body = b'{"error": "unauthorized"}'
        http_err = HTTPError(url="http://x", code=401, msg="Unauthorized",
                             hdrs={}, fp=BytesIO(err_body))
        with patch("urllib.request.urlopen", side_effect=http_err):
            result = run_proxy_test(18082, "sk-test", "model")
        assert result["code"] == 401

    def test_503_returns_error_code(self):
        """HTTP 503 → code=503."""
        err_body = b'{"error": "service unavailable"}'
        http_err = HTTPError(url="http://x", code=503, msg="Service Unavailable",
                             hdrs={}, fp=BytesIO(err_body))
        with patch("urllib.request.urlopen", side_effect=http_err):
            result = run_proxy_test(18082, "sk-test", "model")
        assert result["code"] == 503

    def test_connection_refused_returns_code_zero(self):
        """Connection error → code=0, body starts with 'Error:'."""
        with patch("urllib.request.urlopen", side_effect=URLError("Connection refused")):
            result = run_proxy_test(18082, "sk-test", "model")
        assert result["code"] == 0
        assert "Error" in result["body"]

    def test_generic_exception_returns_code_zero(self):
        """Unexpected exception → code=0."""
        with patch("urllib.request.urlopen", side_effect=OSError("socket timeout")):
            result = run_proxy_test(18082, "sk-test", "model")
        assert result["code"] == 0

    def test_elapsed_is_non_negative_int(self):
        """elapsed is always an int >= 0."""
        body = {"content": [{"text": "ok", "type": "text"}]}
        with patch("urllib.request.urlopen", return_value=self._mock_resp(200, body)):
            result = run_proxy_test(18082, "sk-test", "model")
        assert isinstance(result["elapsed"], int)
        assert result["elapsed"] >= 0

    def test_invalid_json_response_returns_raw(self):
        """If response body is not valid JSON, returns raw string."""
        m = self._mock_resp(200, "not json at all")
        with patch("urllib.request.urlopen", return_value=m):
            result = run_proxy_test(18082, "sk-test", "model")
        assert result["code"] == 200
        assert result["body"]  # non-empty

    def test_result_dict_has_required_keys(self):
        """Result always has code, body, elapsed."""
        with patch("urllib.request.urlopen", side_effect=URLError("x")):
            result = run_proxy_test(18082, "sk-test", "model")
        assert "code" in result
        assert "body" in result
        assert "elapsed" in result
