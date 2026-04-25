"""
claude-mux failover module — FailoverManager.

Automatic failover to next subscription on 429/connection errors.
"""

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from claude_mux.config import CLAUDE_MUX_DIR, ConfigManager

log = logging.getLogger("claude-mux")


class FailoverManager:
    """Automatic failover to next subscription on 429/connection errors.

    Run `test_health()` to test if active subscription is working.
    Call `do_failover()` to switch to next working subscription.
    """

    # HTTP status codes that indicate rate-limit / auth error → failover
    FAILOVER_CODES = {429, 401, 403, 503}
    # Subprocess exit codes that indicate error
    FAILOVER_PATTERNS = ("rate limit", "429", "too many requests", "overloaded", "limit exceeded")

    FAILOVER_LOG = CLAUDE_MUX_DIR / "failover.log"
    # Attempt to reactivate original subscription after N minutes
    RETRY_ORIGINAL_AFTER_SECS = 600  # 10 min

    def __init__(self, config: ConfigManager, sync: "SyncManager"):  # noqa: F821
        self.cm = config
        self.sync = sync
        self._failed_subs: set[str] = set()  # temporarily marked subs
        self._original_sub_id: str | None = None  # sub_id that failed first
        self._failover_ts: float | None = None  # timestamp of latest failover

    def test_health(self, sub_id: str | None = None) -> tuple[bool, str]:
        """Test if a subscription works via `claude -p "write OK"`.

        Returns (ok: bool, reason: str).
        Uses sub_id's settings (does NOT call sync_default temporarily — tests against current settings).
        """
        target = sub_id or self.cm.default_instance
        if not target:
            return False, "No active subscription"
        sub = self.cm.get_subscription(target)
        if not sub:
            return False, "Subscription not found"

        # For OAuth: test via subprocess claude -p
        # For proxy: test via HTTP directly against proxy
        auth_type = sub.get("auth_type", "bearer")
        if auth_type == "oauth":
            return self._test_via_claude_cli()
        else:
            port = self.cm.get_instance_port(target)
            if not port:
                return False, "Proxy not running"
            return self._test_proxy_http(port, auth_type, sub)

    def _test_via_claude_cli(self) -> tuple[bool, str]:
        """Test active OAuth settings via `claude -p OK`."""
        try:
            result = subprocess.run(
                ["claude", "-p", "write OK"],
                capture_output=True, text=True, timeout=30,
            )
            out = (result.stdout + result.stderr).lower()
            if result.returncode == 0:
                return True, "OK"
            for pat in self.FAILOVER_PATTERNS:
                if pat in out:
                    return False, f"Rate-limit/auth error: {out[:120]}"
            return False, f"Exit {result.returncode}: {out[:120]}"
        except subprocess.TimeoutExpired:
            return False, "Timeout (30s) — claude CLI not responding"
        except FileNotFoundError:
            return False, "claude CLI not found"
        except Exception as e:
            return False, str(e)

    def _test_proxy_http(self, port: int, auth_type: str, sub: dict) -> tuple[bool, str]:
        """Test proxy endpoint direkte via HTTP."""
        from urllib.request import Request, urlopen
        from urllib.error import URLError, HTTPError
        api_key = sub.get("api_key") or os.environ.get(sub.get("api_key_env", ""), "")
        url = f"http://localhost:{port}/v1/messages"
        body = json.dumps({
            "model": "claude-haiku-4-5", "max_tokens": 5,
            "messages": [{"role": "user", "content": "OK"}],
        }).encode()
        req = Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("anthropic-version", "2023-06-01")
        if api_key:
            req.add_header("x-api-key", api_key)
        try:
            with urlopen(req, timeout=10) as resp:
                code = resp.getcode()
                if code in self.FAILOVER_CODES:
                    return False, f"HTTP {code}"
                return True, f"HTTP {code}"
        except HTTPError as e:
            if e.code in self.FAILOVER_CODES:
                return False, f"HTTP {e.code} — {e.reason}"
            return True, f"HTTP {e.code} (non-fatal)"
        except (URLError, OSError) as e:
            return False, f"Connection error: {e}"

    def _log_failover_event(self, from_sub: dict | None, to_sub: dict | None, reason: str):
        """Write failover event to FAILOVER_LOG."""
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        from_name = from_sub["name"] if from_sub else "?"
        to_name = to_sub["name"] if to_sub else "none"
        line = f"{ts}  FROM={from_name}  TO={to_name}  REASON={reason[:120]}\n"
        try:
            self.FAILOVER_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(self.FAILOVER_LOG, "a") as f:
                f.write(line)
        except OSError as e:
            log.warning("Failover: could not write to log: %s", e)

    def do_failover(self, failed_sub_id: str, reason: str = "unknown") -> str | None:
        """Try next subscription not in _failed_subs.

        Returns sub_id of new active subscription, or None if none work.
        """
        failed_sub = self.cm.get_subscription(failed_sub_id)
        self._failed_subs.add(failed_sub_id)
        # Save original sub_id the first time failover occurs
        if self._original_sub_id is None:
            self._original_sub_id = failed_sub_id
        self._failover_ts = time.time()

        subs = [s for s in self.cm.subscriptions if s["id"] not in self._failed_subs]
        if not subs:
            log.warning("Failover: no working subscriptions remaining")
            self._log_failover_event(failed_sub, None, reason)
            return None
        for sub in subs:
            sub_id = sub["id"]
            log.info("Failover: testing %s (%s)", sub["name"], sub_id)
            try:
                self.sync.sync_default(sub_id)
                ok, health_reason = self.test_health(sub_id)
                if ok:
                    self.cm.set_default(sub_id)
                    log.info("Failover: %s works — switched to %s", sub["name"], sub_id)
                    self._log_failover_event(failed_sub, sub, reason)
                    return sub_id
                else:
                    log.warning("Failover: %s failed: %s", sub["name"], health_reason)
                    self._failed_subs.add(sub_id)
            except Exception as e:
                log.warning("Failover: could not switch to %s: %s", sub["name"], e)
                self._failed_subs.add(sub_id)
        self._log_failover_event(failed_sub, None, reason)
        return None

    def reset_failures(self):
        """Reset marked failed subscriptions (e.g. after manual test)."""
        self._failed_subs.clear()
        self._original_sub_id = None
        self._failover_ts = None

    def should_retry_original(self) -> bool:
        """True if RETRY_ORIGINAL_AFTER_SECS has passed since last failover."""
        if self._failover_ts is None or self._original_sub_id is None:
            return False
        return (time.time() - self._failover_ts) >= self.RETRY_ORIGINAL_AFTER_SECS


# Avoid circular import at module level — SyncManager type hint only
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from claude_mux.sync import SyncManager
