"""
claude-mux sync module — SyncManager.

Synchronize default instance's .env to ~/.claude/settings.json.
Uses explicit ENV_TO_SETTINGS_MAP — ANTHROPIC_API_KEY → ANTHROPIC_AUTH_TOKEN (rename!).
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path

from claude_mux.config import (
    CLAUDE_MUX_DIR,
    SETTINGS_KEYS_TO_REMOVE,
    ConfigManager,
    _atomic_write,
)

log = logging.getLogger("claude-mux")

# Plain-text file caching the active subscription name for fast statusline reads.
ACTIVE_NAME_FILE = CLAUDE_MUX_DIR / "active-name"


class SyncManager:
    """Synchronize default instance's .env to ~/.claude/settings.json.

    Uses explicit ENV_TO_SETTINGS_MAP — ANTHROPIC_API_KEY → ANTHROPIC_AUTH_TOKEN (rename!).
    """

    SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
    CLAUDE_MUX_DOT_ENV = CLAUDE_MUX_DIR / ".env"

    def __init__(self, config: ConfigManager):
        self.cm = config

    # ------------------------------------------------------------------
    # Key resolution
    # ------------------------------------------------------------------

    def _resolve_api_key(self, sub: dict, *, allow_subprocess: bool = True) -> str:
        """Return the effective API key for a subscription.

        Resolution order:
        1. sub["api_key"] (stored literal — OAuth tokens, direct bearer)
        2. gh auth token subprocess (gh_token auth only, if allow_subprocess)
        3. os.environ[api_key_env]
        """
        api_key = sub.get("api_key", "")
        if api_key:
            return api_key
        api_key_env = sub.get("api_key_env", "")
        auth_type = sub.get("auth_type", "bearer")
        if auth_type == "gh_token" and allow_subprocess:
            try:
                result = subprocess.run(
                    ["gh", "auth", "token"], capture_output=True, text=True, timeout=10,
                )
                return result.stdout.strip() if result.returncode == 0 else os.environ.get(api_key_env, "")
            except Exception:
                return os.environ.get(api_key_env, "")
        return os.environ.get(api_key_env, "") if api_key_env else ""

    # ------------------------------------------------------------------
    # Active subscription detection
    # ------------------------------------------------------------------

    def detect_active(self) -> str | None:
        """Return the sub_id of the subscription currently active in Claude.

        Reads ~/.claude/settings.json and os.environ and matches against
        subscriptions using auth-type-specific logic:

        - oauth:   CLAUDE_CODE_OAUTH_TOKEN (env OR settings) == resolved api_key
        - direct:  ANTHROPIC_BASE_URL == sub["provider_url"]
        - bearer:  ANTHROPIC_BASE_URL == http://localhost:<instance_port>
        """
        settings_env = self._load_settings().get("env", {})

        # OAuth token may live in os.environ (shell export) OR settings["env"]
        oauth_token = (
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            or settings_env.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        )
        base_url = settings_env.get("ANTHROPIC_BASE_URL", "")

        for sub in self.cm.subscriptions:
            sub_id = sub["id"]
            auth_type = sub.get("auth_type", "bearer")

            if auth_type == "oauth":
                if not oauth_token:
                    continue
                # No subprocess during periodic TUI refresh — env only
                resolved = self._resolve_api_key(sub, allow_subprocess=False)
                if resolved and oauth_token == resolved:
                    return sub_id

            elif auth_type == "direct":
                provider_url = sub.get("provider_url", "")
                if provider_url and base_url == provider_url:
                    return sub_id

            else:  # bearer, gh_token, custom proxy
                port = self.cm.get_instance_port(sub_id)
                if port and base_url == f"http://localhost:{port}":
                    return sub_id

        return None

    def read_active_name(self) -> str:
        """Return the cached active subscription name, or empty string."""
        try:
            return ACTIVE_NAME_FILE.read_text().strip()
        except OSError:
            return ""

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync_default(self, sub_id: str) -> dict:
        """Set sub_id as default and sync .env → settings.json.

        Steps:
        1. Set default_instance in subscriptions.json
        2. Copy instance .env to ~/.claude-mux/.env
        3. Merge relevant keys (only MERGE_KEYS) to ~/.claude/settings.json
        4. Set CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
        5. Set ANTHROPIC_DISABLE_TELEMETRY=true
        6. Write ~/.claude-mux/active-name for statusline
        """
        sub = self.cm.get_subscription(sub_id)
        if sub is None:
            raise ValueError(f"Subscription {sub_id} not found")

        # 1. Set default in subscriptions.json
        self.cm.set_default(sub_id)

        # 2. Copy instance .env to ~/.claude-mux/.env
        # Import here to avoid circular import
        from claude_mux.instance import InstanceManager
        im = InstanceManager(self.cm)
        inst_env = im.generate_env(sub_id)  # ensures .env is fresh
        CLAUDE_MUX_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(inst_env, self.CLAUDE_MUX_DOT_ENV)

        # 3. Merge to settings.json
        settings = self._load_settings()
        env_block = settings.setdefault("env", {})

        auth_type = sub.get("auth_type", "bearer")
        api_key = self._resolve_api_key(sub)
        provider_url = sub.get("provider_url", "")
        port = self.cm.get_instance_port(sub_id) or 0
        listen_addr = f"http://localhost:{port}"

        # Build settings env block
        merged = {}

        if auth_type == "oauth":
            # Claude Max: remove proxy, set OAuth token, let Claude use its own OAuth
            merged["ANTHROPIC_BASE_URL"] = None  # mark for removal
            merged["ANTHROPIC_AUTH_TOKEN"] = None
            if api_key:
                merged["CLAUDE_CODE_OAUTH_TOKEN"] = api_key
        elif auth_type == "direct":
            # Direct Anthropic-compatible API (e.g. z.ai) — no proxy needed
            merged["ANTHROPIC_BASE_URL"] = provider_url
            if api_key:
                merged["ANTHROPIC_AUTH_TOKEN"] = api_key
            merged["CLAUDE_CODE_OAUTH_TOKEN"] = None
        else:
            merged["ANTHROPIC_BASE_URL"] = listen_addr
            if api_key:
                merged["ANTHROPIC_AUTH_TOKEN"] = api_key
            # Clear OAuth token so Claude doesn't use it instead of proxy
            merged["CLAUDE_CODE_OAUTH_TOKEN"] = None
        merged["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        merged["ANTHROPIC_DISABLE_TELEMETRY"] = "true"

        # Map model maps to settings via ANTHROPIC_DEFAULT_*_MODEL
        model_maps = sub.get("model_maps", {})
        if model_maps.get("haiku"):
            merged["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model_maps["haiku"]
        if model_maps.get("sonnet"):
            merged["ANTHROPIC_DEFAULT_SONNET_MODEL"] = model_maps["sonnet"]
        if model_maps.get("opus"):
            merged["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model_maps["opus"]

        # Clear deprecated custom model keys
        for key in SETTINGS_KEYS_TO_REMOVE:
            env_block.pop(key, None)

        # Merge: preserve existing env vars, update/overwrite with merged.
        # None values = remove key (used by OAuth to clear proxy)
        for key, val in merged.items():
            if val is None:
                env_block.pop(key, None)
            else:
                env_block[key] = val

        self._save_settings(settings)

        # 6. Cache active name for fast statusline reads
        try:
            ACTIVE_NAME_FILE.parent.mkdir(parents=True, exist_ok=True)
            ACTIVE_NAME_FILE.write_text(sub["name"])
        except OSError as e:
            log.warning("Could not write active-name cache: %s", e)

        return {
            "default": sub["name"],
            "port": port,
            "base_url": listen_addr,
            "keys_updated": list(merged.keys()),
        }

    def _load_settings(self) -> dict:
        """Read settings.json. Return empty dict if missing."""
        if not self.SETTINGS_PATH.exists():
            return {}
        try:
            with open(self.SETTINGS_PATH) as f:
                import json
                return json.load(f)
        except (Exception,):
            return {}

    def _save_settings(self, settings: dict):
        """Save settings.json atomically. Returns True on success, False on error."""
        try:
            _atomic_write(self.SETTINGS_PATH, settings)
            return True
        except OSError as e:
            log.error("SyncManager: could not write settings.json: %s", e)
            return False
