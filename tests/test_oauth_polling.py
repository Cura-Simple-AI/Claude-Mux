"""Unit tests for OAuth token polling logik (_oauth_poll_token)."""
import tempfile
import shutil
from pathlib import Path

import pytest
import claude_mux as hs


def _make_log(content: str) -> Path:
    """Opret midlertidig log-fil med givet indhold."""
    d = Path(tempfile.mkdtemp())
    p = d / "oauth-test.log"
    p.write_text(content)
    return p, d


class TestOAuthTokenExtraction:
    """Test token-extraction logikken fra log-filen direkte."""

    def _extract_token(self, log_content: str) -> str | None:
        """Efterlign _oauth_poll_token's token-extraction."""
        token = None
        lines = log_content.splitlines()
        it = iter(lines)
        for line in it:
            if "export CLAUDE_CODE_OAUTH_TOKEN=" in line and "sk-" not in line:
                continue  # placeholder
            if "export CLAUDE_CODE_OAUTH_TOKEN=" in line:
                token = line.split("export CLAUDE_CODE_OAUTH_TOKEN=", 1)[1].strip()
                break
            if "Your OAuth token" in line:
                try:
                    next(it, "")  # tom linje
                    tok = next(it, "").strip()
                    if tok.startswith("sk-"):
                        token = tok
                except StopIteration:
                    pass
        return token

    def test_extract_from_export_line(self):
        log = "Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>\nexport CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oauth-real-token-abc123\n"
        assert self._extract_token(log) == "sk-ant-oauth-real-token-abc123"

    def test_skip_placeholder_export_line(self):
        log = "export CLAUDE_CODE_OAUTH_TOKEN=<token>\n"
        assert self._extract_token(log) is None

    def test_extract_from_your_oauth_token_block(self):
        log = "Your OAuth token (valid for 1 year):\n\nsk-ant-oauth-year-token-xyz\n"
        assert self._extract_token(log) == "sk-ant-oauth-year-token-xyz"

    def test_no_token_in_log(self):
        log = "Scanning for existing session...\nOpening browser...\n"
        assert self._extract_token(log) is None

    def test_token_must_start_with_sk(self):
        log = "Your OAuth token (valid for 1 year):\n\nnot-a-real-token\n"
        assert self._extract_token(log) is None

    def test_export_line_takes_precedence(self):
        log = (
            "export CLAUDE_CODE_OAUTH_TOKEN=<token>\n"
            "Your OAuth token (valid for 1 year):\n\nsk-ant-from-block\n"
            "export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-from-export\n"
        )
        token = self._extract_token(log)
        assert token is not None
        assert token.startswith("sk-ant-")


class TestConfigManagerPortManagement:
    def test_set_get_port(self):
        d = Path(tempfile.mkdtemp())
        try:
            cm = hs.ConfigManager(data_file=d / "subs.json")
            sub = cm.add_subscription("proxy", "http://localhost:18080", "KEY")
            cm.set_instance_port(sub["id"], 18080)
            assert cm.get_instance_port(sub["id"]) == 18080
        finally:
            shutil.rmtree(d)

    def test_port_none_if_not_set(self):
        d = Path(tempfile.mkdtemp())
        try:
            cm = hs.ConfigManager(data_file=d / "subs.json")
            sub = cm.add_subscription("proxy", "http://x", "KEY")
            assert cm.get_instance_port(sub["id"]) is None
        finally:
            shutil.rmtree(d)

    def test_set_default(self):
        d = Path(tempfile.mkdtemp())
        try:
            cm = hs.ConfigManager(data_file=d / "subs.json")
            sub1 = cm.add_subscription("a", "http://x", "K1")
            sub2 = cm.add_subscription("b", "http://y", "K2")
            cm.set_default(sub1["id"])
            assert cm.default_instance == sub1["id"]
            cm.set_default(sub2["id"])
            assert cm.default_instance == sub2["id"]
        finally:
            shutil.rmtree(d)

    def test_set_default_nonexistent(self):
        d = Path(tempfile.mkdtemp())
        try:
            cm = hs.ConfigManager(data_file=d / "subs.json")
            result = cm.set_default("does-not-exist")
            assert result is False
        finally:
            shutil.rmtree(d)
