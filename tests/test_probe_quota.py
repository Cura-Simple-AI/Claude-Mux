"""Tests for `claude-mux probe --quota` (OAuth rate-limit measurement).

No real network: urllib is mocked. The fake token is a sentinel string that
must never appear in stdout, stderr or JSON output.
"""
import json
import shutil
import tempfile
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

import claude_mux as hs
from claude_mux.cli import cli
from claude_mux import quota

SENTINEL = "SENTINEL-TOKEN-do-not-print-0123456789"
UTIL_HEADERS = {
    "anthropic-ratelimit-unified-7d-utilization": "0.42",
    "anthropic-ratelimit-unified-7d-reset": "1793200000",
    "anthropic-ratelimit-unified-5h-utilization": "0.07",
    "anthropic-ratelimit-unified-5h-reset": "1793100000",
    "anthropic-ratelimit-unified-status": "allowed",
}


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def tmp_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _headers(d):
    m = Message()
    for k, v in d.items():
        m[k] = v
    return m


def _ok_response(headers=None):
    resp = MagicMock()
    resp.status = 200
    resp.getcode.return_value = 200
    resp.headers = _headers(UTIL_HEADERS if headers is None else headers)
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


def _http_error(code, headers=None):
    return urllib.error.HTTPError(
        "https://api.anthropic.com/v1/messages", code, "err",
        _headers(UTIL_HEADERS if headers is None else headers), None)


def _setup(tmp_dir):
    cm = hs.ConfigManager(data_file=tmp_dir / "subs.json")
    cm.add_subscription("acct-a", "", "", auth_type="oauth", api_key=SENTINEL)
    cm.add_subscription("deepseek", "https://api.deepseek.com/v1", "DS_KEY")
    return cm


def _run(runner, cm, args):
    with patch("claude_mux.cli._cm", return_value=cm):
        return runner.invoke(cli, ["probe", *args])


def _all_output(result):
    out = result.output
    try:
        out += result.stderr
    except (ValueError, AttributeError):
        pass
    return out


class TestParseHeaders:
    def test_parses_all_fields(self):
        q = quota.parse_quota_headers(_headers(UTIL_HEADERS))
        assert q["week_pct"] == 42.0
        assert q["five_hour_pct"] == 7.0
        assert q["week_reset_epoch"] == 1793200000
        assert q["five_hour_reset_epoch"] == 1793100000
        assert q["status"] == "allowed"

    def test_missing_headers_give_none(self):
        q = quota.parse_quota_headers(_headers({}))
        assert q["week_pct"] is None
        assert q["status"] is None

    def test_garbage_values_give_none(self):
        q = quota.parse_quota_headers(_headers({
            "anthropic-ratelimit-unified-7d-utilization": "abc",
            "anthropic-ratelimit-unified-7d-reset": "soon",
        }))
        assert q["week_pct"] is None
        assert q["week_reset_epoch"] is None


class TestProbeQuota:
    def test_200_prints_table_row_without_token(self, runner, tmp_dir):
        cm = _setup(tmp_dir)
        with patch("claude_mux.quota.urllib.request.urlopen", return_value=_ok_response()):
            r = _run(runner, cm, ["acct-a", "--quota"])
        assert r.exit_code == 0, r.output
        assert "acct-a" in r.output and "200" in r.output
        assert "42 %" in r.output and "7 %" in r.output and "allowed" in r.output
        assert SENTINEL not in _all_output(r)

    def test_429_still_reads_headers(self, runner, tmp_dir):
        cm = _setup(tmp_dir)
        with patch("claude_mux.quota.urllib.request.urlopen", side_effect=_http_error(429)):
            r = _run(runner, cm, ["acct-a", "--quota", "--json"])
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert data[0]["http_code"] == 429
        assert data[0]["week_pct"] == 42.0
        assert SENTINEL not in _all_output(r)

    def test_json_has_no_token_for_200(self, runner, tmp_dir):
        cm = _setup(tmp_dir)
        with patch("claude_mux.quota.urllib.request.urlopen", return_value=_ok_response()):
            r = _run(runner, cm, ["--quota", "--all", "--json"])
        assert r.exit_code == 0, r.output
        assert SENTINEL not in _all_output(r)
        names = [x["name"] for x in json.loads(r.output)]
        assert names == ["acct-a"]  # non-OAuth profile skipped by --all

    def test_token_only_sent_in_authorization_header(self, runner, tmp_dir):
        cm = _setup(tmp_dir)
        with patch("claude_mux.quota.urllib.request.urlopen", return_value=_ok_response()) as m:
            _run(runner, cm, ["acct-a", "--quota"])
        req = m.call_args[0][0]
        assert req.full_url == "https://api.anthropic.com/v1/messages"
        assert req.get_header("Authorization") == "Bearer " + SENTINEL
        assert req.get_header("Anthropic-beta") == "oauth-2025-04-20"
        assert SENTINEL not in req.full_url

    def test_non_oauth_profile_cannot_be_measured(self, runner, tmp_dir):
        cm = _setup(tmp_dir)
        with patch("claude_mux.quota.urllib.request.urlopen") as m:
            r = _run(runner, cm, ["deepseek", "--quota"])
        assert "cannot be measured: not an OAuth profile" in r.output
        assert r.exit_code == 2
        m.assert_not_called()

    def test_non_oauth_does_not_fail_other_profiles(self, runner, tmp_dir):
        cm = _setup(tmp_dir)
        with patch("claude_mux.quota.urllib.request.urlopen", return_value=_ok_response()):
            r = _run(runner, cm, ["--quota", "--all"])
        assert r.exit_code == 0
        assert "acct-a" in r.output

    def test_network_error_prints_type_only_exit_4(self, runner, tmp_dir):
        cm = _setup(tmp_dir)
        err = OSError("boom Bearer " + SENTINEL)
        with patch("claude_mux.quota.urllib.request.urlopen", side_effect=err):
            r = _run(runner, cm, ["acct-a", "--quota"])
        assert r.exit_code == 4
        assert "cannot be measured: OSError" in r.output
        assert "boom" not in _all_output(r)
        assert SENTINEL not in _all_output(r)

    def test_http_error_without_quota_headers_is_failure(self, runner, tmp_dir):
        cm = _setup(tmp_dir)
        with patch("claude_mux.quota.urllib.request.urlopen",
                   side_effect=_http_error(401, headers={})):
            r = _run(runner, cm, ["acct-a", "--quota"])
        assert r.exit_code == 4
        assert "401" in r.output
        assert SENTINEL not in _all_output(r)

    def test_no_oauth_profiles_is_unmeasurable_exit_2(self, runner, tmp_dir):
        cm = hs.ConfigManager(data_file=tmp_dir / "subs.json")
        cm.add_subscription("deepseek", "https://api.deepseek.com/v1", "DS_KEY")
        r = _run(runner, cm, ["--quota", "--all"])
        assert r.exit_code == 2

    def test_unknown_name_exit_3(self, runner, tmp_dir):
        cm = _setup(tmp_dir)
        r = _run(runner, cm, ["nope", "--quota"])
        assert r.exit_code == 3

    def test_oauth_without_stored_token_not_measurable(self, runner, tmp_dir):
        cm = hs.ConfigManager(data_file=tmp_dir / "subs.json")
        cm.add_subscription("empty", "", "", auth_type="oauth")
        with patch("claude_mux.quota.urllib.request.urlopen") as m:
            r = _run(runner, cm, ["empty", "--quota"])
        assert "cannot be measured: no token stored" in r.output
        m.assert_not_called()

    def test_help_documents_quota_and_exit_codes(self, runner):
        r = runner.invoke(cli, ["probe", "--help"])
        assert "--quota" in r.output and "--all" in r.output
        assert "Exit codes" in r.output

    def test_probe_without_quota_unchanged(self, runner, tmp_dir):
        cm = _setup(tmp_dir)
        r = _run(runner, cm, ["acct-a"])
        # old path: oauth sub has no proxy port -> "not running", exit 3
        assert r.exit_code == 3
        assert "not running" in _all_output(r)
