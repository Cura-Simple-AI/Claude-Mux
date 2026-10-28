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


class TestExitSemantics:
    def test_all_with_unmeasured_oauth_profile_is_not_exit_0(self, runner, tmp_dir):
        cm = _setup(tmp_dir)
        cm.add_subscription("empty", "", "", auth_type="oauth")
        with patch("claude_mux.quota.urllib.request.urlopen", return_value=_ok_response()):
            r = _run(runner, cm, ["--quota", "--all"])
        assert "acct-a | HTTP 200" in r.output
        assert "empty | cannot be measured: no token stored" in r.output
        assert r.exit_code == 4

    def test_single_oauth_without_token_is_unmeasurable_exit_2(self, runner, tmp_dir):
        cm = hs.ConfigManager(data_file=tmp_dir / "subs.json")
        cm.add_subscription("empty", "", "", auth_type="oauth")
        r = _run(runner, cm, ["empty", "--quota"])
        assert r.exit_code == 2

    def test_http_error_without_headers_does_not_crash(self, runner, tmp_dir):
        cm = _setup(tmp_dir)
        err = urllib.error.HTTPError("https://api.anthropic.com/v1/messages",
                                     502, "bad gateway", None, None)
        with patch("claude_mux.quota.urllib.request.urlopen", side_effect=err):
            r = _run(runner, cm, ["acct-a", "--quota"])
        assert r.exception is None or isinstance(r.exception, SystemExit)
        assert r.exit_code == 4
        assert "502" in r.output

    def test_401_text_says_rejected_or_expired(self, runner, tmp_dir):
        cm = _setup(tmp_dir)
        with patch("claude_mux.quota.urllib.request.urlopen",
                   side_effect=_http_error(401, headers={})):
            r = _run(runner, cm, ["acct-a", "--quota"])
        assert "token rejected or expired" in r.output
        assert SENTINEL not in _all_output(r)

    def test_active_id_missing_from_config_exit_3(self, runner, tmp_dir):
        cm = _setup(tmp_dir)
        sync = MagicMock()
        sync.detect_active.return_value = "ghost-id"
        with patch("claude_mux.cli._managers", return_value=(cm, sync, None, None)):
            r = runner.invoke(cli, ["probe", "--quota"])
        assert r.exception is None or isinstance(r.exception, SystemExit)
        assert r.exit_code == 3
        assert "not found" in _all_output(r)


class TestUsageErrors:
    def test_all_without_quota_rejected(self, runner, tmp_dir):
        r = _run(runner, _setup(tmp_dir), ["--all"])
        assert r.exit_code == 2
        assert "--all" in _all_output(r) and "--quota" in _all_output(r)

    def test_model_with_quota_rejected(self, runner, tmp_dir):
        r = _run(runner, _setup(tmp_dir), ["acct-a", "--quota", "--model", "x"])
        assert r.exit_code == 2
        assert "--model" in _all_output(r)

    def test_name_with_all_rejected(self, runner, tmp_dir):
        with patch("claude_mux.quota.urllib.request.urlopen") as m:
            r = _run(runner, _setup(tmp_dir), ["acct-a", "--quota", "--all"])
        assert r.exit_code == 2
        m.assert_not_called()


class TestResetTimeZone:
    # 1793200000 = 2026-10-28 15:06:40 UTC (CET in Copenhagen: DST ended 25/10)
    # 1774800000 = 2026-03-29 16:00:00 UTC (CEST: DST began 29/03)
    @pytest.fixture()
    def tz(self, monkeypatch):
        import time

        def _set(name):
            monkeypatch.setenv("TZ", name)
            time.tzset()
        yield _set
        monkeypatch.undo()
        time.tzset()

    def test_utc_names_zone(self, tz):
        tz("UTC")
        assert quota._local(1793200000) == "Wed 28/10 15:06 UTC"

    def test_copenhagen_winter_names_cet(self, tz):
        tz("Europe/Copenhagen")
        assert quota._local(1793200000) == "Wed 28/10 16:06 CET"

    def test_copenhagen_summer_names_cest(self, tz):
        tz("Europe/Copenhagen")
        assert quota._local(1774800000) == "Sun 29/03 18:00 CEST"

    def test_dst_change_two_instants(self, tz):
        tz("Europe/Copenhagen")
        # 2026-03-29 00:30 UTC (01:30 CET) and 02:30 UTC (04:30 CEST)
        assert quota._local(1774744200) == "Sun 29/03 01:30 CET"
        assert quota._local(1774751400) == "Sun 29/03 04:30 CEST"

    def test_row_contains_zone(self, tz):
        tz("Europe/Copenhagen")
        row = quota.format_row({"name": "a", "measured": True, "http_code": 200,
                                "week_pct": 1.0, "week_reset_epoch": 1793200000,
                                "five_hour_pct": 2.0, "five_hour_reset_epoch": 1793100000,
                                "status": "allowed"})
        assert "CET" in row
