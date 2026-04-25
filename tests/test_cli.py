"""Tests for claude-mux CLI subcommands."""
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

import claude_mux as hs
from claude_mux.cli import cli


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def tmp_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _cm(d):
    return hs.ConfigManager(data_file=d / "subs.json")


def _patch_cm(d, monkeypatch=None):
    """Return a patch context that makes CLI commands use tmp ConfigManager."""
    cm = _cm(d)
    return patch("claude_mux.cli._cm", return_value=cm)


class TestVersion:
    def test_version_flag(self, runner):
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "claude-mux" in result.output
        assert hs.__version__ in result.output


class TestHelp:
    def test_root_help(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "activate" in result.output
        assert "failover" in result.output

    def test_list_help(self, runner):
        result = runner.invoke(cli, ["list", "--help"])
        assert result.exit_code == 0
        assert "--json" in result.output

    def test_activate_help(self, runner):
        result = runner.invoke(cli, ["activate", "--help"])
        assert result.exit_code == 0


class TestList:
    def test_empty_list(self, runner, tmp_dir):
        with _patch_cm(tmp_dir):
            result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "No subscriptions" in result.output

    def test_empty_list_json(self, runner, tmp_dir):
        with _patch_cm(tmp_dir):
            result = runner.invoke(cli, ["list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == []

    def test_list_shows_subscriptions(self, runner, tmp_dir):
        cm = _cm(tmp_dir)
        cm.add_subscription("deepseek", "https://api.deepseek.com/v1", "DS_KEY")
        cm.add_subscription("copilot", "https://api.githubcopilot.com", "GH_TOKEN", auth_type="gh_token")
        with _patch_cm(tmp_dir):
            result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "deepseek" in result.output
        assert "copilot" in result.output

    def test_list_json_has_required_fields(self, runner, tmp_dir):
        cm = _cm(tmp_dir)
        cm.add_subscription("deepseek", "https://api.deepseek.com/v1", "DS_KEY")
        with _patch_cm(tmp_dir):
            result = runner.invoke(cli, ["list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert "id" in data[0]
        assert "name" in data[0]
        assert "active" in data[0]
        assert data[0]["name"] == "deepseek"

    def test_list_marks_active(self, runner, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("deepseek", "https://api.deepseek.com/v1", "DS_KEY")
        cm._data["default_instance"] = sub["id"]
        cm._save()
        with _patch_cm(tmp_dir):
            result = runner.invoke(cli, ["list", "--json"])
        data = json.loads(result.output)
        assert data[0]["active"] is True


class TestActivate:
    def test_activate_by_name(self, runner, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("deepseek", "https://api.deepseek.com/v1", "DS_KEY")
        sync_mock = MagicMock()
        with _patch_cm(tmp_dir):
            with patch("claude_mux.cli.SyncManager", return_value=sync_mock):
                with patch("claude_mux.cli.InstanceManager"):
                    with patch("claude_mux.cli.FailoverManager"):
                        result = runner.invoke(cli, ["activate", "deepseek"])
        assert result.exit_code == 0
        assert "deepseek" in result.output
        sync_mock.sync_default.assert_called_once_with(sub["id"])

    def test_activate_not_found(self, runner, tmp_dir):
        with _patch_cm(tmp_dir):
            result = runner.invoke(cli, ["activate", "nonexistent"])
        assert result.exit_code == 3
        assert "not found" in result.output

    def test_activate_json_output(self, runner, tmp_dir):
        cm = _cm(tmp_dir)
        cm.add_subscription("deepseek", "https://api.deepseek.com/v1", "DS_KEY")
        sync_mock = MagicMock()
        with _patch_cm(tmp_dir):
            with patch("claude_mux.cli.SyncManager", return_value=sync_mock):
                with patch("claude_mux.cli.InstanceManager"):
                    with patch("claude_mux.cli.FailoverManager"):
                        result = runner.invoke(cli, ["activate", "deepseek", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["name"] == "deepseek"


class TestStatus:
    def test_status_empty(self, runner, tmp_dir):
        with _patch_cm(tmp_dir):
            with patch("claude_mux.cli.InstanceManager"):
                with patch("claude_mux.cli.SyncManager"):
                    with patch("claude_mux.cli.FailoverManager"):
                        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0

    def test_status_not_found(self, runner, tmp_dir):
        with _patch_cm(tmp_dir):
            with patch("claude_mux.cli.InstanceManager"):
                with patch("claude_mux.cli.SyncManager"):
                    with patch("claude_mux.cli.FailoverManager"):
                        result = runner.invoke(cli, ["status", "nonexistent"])
        assert result.exit_code == 3

    def test_status_json(self, runner, tmp_dir):
        cm = _cm(tmp_dir)
        cm.add_subscription("deepseek", "https://api.deepseek.com/v1", "DS_KEY")
        im_mock = MagicMock()
        im_mock.status.return_value = {"status": "stopped"}
        with _patch_cm(tmp_dir):
            with patch("claude_mux.cli.InstanceManager", return_value=im_mock):
                with patch("claude_mux.cli.SyncManager"):
                    with patch("claude_mux.cli.FailoverManager"):
                        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["name"] == "deepseek"


class TestTest:
    def test_test_no_active(self, runner, tmp_dir):
        fm_mock = MagicMock()
        with _patch_cm(tmp_dir):
            with patch("claude_mux.cli.FailoverManager", return_value=fm_mock):
                with patch("claude_mux.cli.InstanceManager"):
                    with patch("claude_mux.cli.SyncManager"):
                        result = runner.invoke(cli, ["test"])
        assert result.exit_code == 3

    def test_test_ok_exit_0(self, runner, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("deepseek", "http://x", "K")
        cm._data["default_instance"] = sub["id"]
        cm._save()
        fm_mock = MagicMock()
        fm_mock.test_health.return_value = (True, "HTTP 200")
        with _patch_cm(tmp_dir):
            with patch("claude_mux.cli.FailoverManager", return_value=fm_mock):
                with patch("claude_mux.cli.InstanceManager"):
                    with patch("claude_mux.cli.SyncManager"):
                        result = runner.invoke(cli, ["test"])
        assert result.exit_code == 0
        assert "OK" in result.output

    def test_test_fail_exit_4(self, runner, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("deepseek", "http://x", "K")
        cm._data["default_instance"] = sub["id"]
        cm._save()
        fm_mock = MagicMock()
        fm_mock.test_health.return_value = (False, "HTTP 429")
        with _patch_cm(tmp_dir):
            with patch("claude_mux.cli.FailoverManager", return_value=fm_mock):
                with patch("claude_mux.cli.InstanceManager"):
                    with patch("claude_mux.cli.SyncManager"):
                        result = runner.invoke(cli, ["test"])
        assert result.exit_code == 4
        assert "FAIL" in result.output

    def test_test_json_output(self, runner, tmp_dir):
        cm = _cm(tmp_dir)
        sub = cm.add_subscription("deepseek", "http://x", "K")
        cm._data["default_instance"] = sub["id"]
        cm._save()
        fm_mock = MagicMock()
        fm_mock.test_health.return_value = (True, "HTTP 200")
        with _patch_cm(tmp_dir):
            with patch("claude_mux.cli.FailoverManager", return_value=fm_mock):
                with patch("claude_mux.cli.InstanceManager"):
                    with patch("claude_mux.cli.SyncManager"):
                        result = runner.invoke(cli, ["test", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["name"] == "deepseek"


class TestFailoverLog:
    def test_failover_log_empty(self, runner, tmp_dir):
        with patch("claude_mux.cli.CLAUDE_MUX_DIR", tmp_dir):
            result = runner.invoke(cli, ["failover-log"])
        assert result.exit_code == 0
        assert "No failover" in result.output

    def test_failover_log_shows_entries(self, runner, tmp_dir):
        log = tmp_dir / "failover.log"
        log.write_text("2026-04-25 10:00:00  FROM=deepseek  TO=openai  REASON=HTTP 429\n")
        with patch("claude_mux.cli.CLAUDE_MUX_DIR", tmp_dir):
            result = runner.invoke(cli, ["failover-log"])
        assert result.exit_code == 0
        assert "deepseek" in result.output
        assert "HTTP 429" in result.output

    def test_failover_log_json(self, runner, tmp_dir):
        log = tmp_dir / "failover.log"
        log.write_text("2026-04-25 10:00:00  FROM=deepseek  TO=openai  REASON=HTTP 429\n")
        with patch("claude_mux.cli.CLAUDE_MUX_DIR", tmp_dir):
            result = runner.invoke(cli, ["failover-log", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_failover_log_tail(self, runner, tmp_dir):
        log = tmp_dir / "failover.log"
        lines = "\n".join([f"2026-04-25 10:0{i}:00  FROM=a  TO=b  REASON=test" for i in range(5)])
        log.write_text(lines + "\n")
        with patch("claude_mux.cli.CLAUDE_MUX_DIR", tmp_dir):
            result = runner.invoke(cli, ["failover-log", "--tail", "2"])
        assert result.exit_code == 0
        assert result.output.count("FROM=a") == 2


class TestConfig:
    def test_config_output(self, runner, tmp_dir):
        cm = _cm(tmp_dir)
        with _patch_cm(tmp_dir):
            with patch("claude_mux.cli.SyncManager") as sync_cls:
                sync_cls.return_value.SETTINGS_PATH = tmp_dir / "settings.json"
                with patch("claude_mux.cli.InstanceManager"):
                    with patch("claude_mux.cli.FailoverManager"):
                        result = runner.invoke(cli, ["config"])
        assert result.exit_code == 0
        assert "Config dir" in result.output
        assert "Active sub" in result.output

    def test_config_json(self, runner, tmp_dir):
        with _patch_cm(tmp_dir):
            with patch("claude_mux.cli.SyncManager") as sync_cls:
                sync_cls.return_value.SETTINGS_PATH = tmp_dir / "settings.json"
                with patch("claude_mux.cli.InstanceManager"):
                    with patch("claude_mux.cli.FailoverManager"):
                        result = runner.invoke(cli, ["config", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "config_dir" in data
        assert "active" in data
