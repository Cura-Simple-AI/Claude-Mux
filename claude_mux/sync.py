"""
claude-mux sync module — SyncManager.

Synchronize default instance's .env to ~/.claude/settings.json.
Uses explicit ENV_TO_SETTINGS_MAP — ANTHROPIC_API_KEY → ANTHROPIC_AUTH_TOKEN (rename!).
"""

import logging
import os
import re
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


def extract_response_body(raw: str, code: int, max_len: int = 500) -> str:
    """Extract human-readable text from a /v1/messages JSON response.

    On HTTP 200: tries to parse content[0].text from the Anthropic response format.
    On error codes or parse failure: returns the raw string truncated to max_len.
    """
    import json as _json
    if code == 200:
        try:
            data = _json.loads(raw)
            return data.get("content", [{}])[0].get("text", raw)[:max_len]
        except Exception:
            pass
    return raw[:max_len]


# Plain-text file caching the active subscription name for fast statusline reads.
ACTIVE_NAME_FILE = CLAUDE_MUX_DIR / "active-name"

# Single source of truth for model tier fallbacks.
# Used by CLI (cm test) and TUI (Test button) when no model_maps or available_models entry exists.
TIER_FALLBACK_MODELS: dict[str, str] = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}

# Timeout (seconds) for all inference test HTTP requests.
INFERENCE_TIMEOUT = 30


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
        """Delegate to ConfigManager.resolve_api_key (canonical implementation)."""
        return self.cm.resolve_api_key(sub, allow_subprocess=allow_subprocess)

    # ------------------------------------------------------------------
    # Active subscription detection
    # ------------------------------------------------------------------

    def detect_active(self) -> str | None:
        """Return sub_id aktiv i Claude.

        Tries `claude auth status --text` first (authoritative runtime source).
        Falls back to reading ~/.claude/settings.json["env"] when claude CLI
        is unavailable (e.g. CI, test environments).
        """
        # Strategy 1: claude auth status --text (authoritative)
        try:
            result = subprocess.run(
                ["claude", "auth", "status", "--text"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                output = result.stdout.strip()
                match = self._match_from_claude_output(output)
                if match:
                    return match
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        # Strategy 2: fallback to settings.json parsing
        match = self._match_from_settings()
        if match:
            return match

        # Strategy 3: configured default_instance
        return self.cm.default_instance

    def _match_from_claude_output(self, output: str) -> str | None:
        """Parse `claude auth status --text` output and match to subscription."""
        base_url = ""
        for line in output.splitlines():
            if line.startswith("Anthropic base URL:"):
                base_url = line.split(":", 1)[1].strip()
                break

        if not base_url or base_url == "https://api.anthropic.com/v1":
            # Ingen base URL eller Anthropic direct = OAuth / Claude Max
            for sub in self.cm.subscriptions:
                if sub.get("auth_type") == "oauth":
                    return sub["id"]
            return None

        # base_url == provider_url → direct
        for sub in self.cm.subscriptions:
            if sub.get("auth_type") == "direct":
                if base_url == sub.get("provider_url", ""):
                    return sub["id"]

        # base_url == http://localhost:<port> → bearer/gh_token/custom proxy
        m = re.match(r"http://localhost:(\d+)", base_url)
        if m:
            port = int(m.group(1))
            for sub in self.cm.subscriptions:
                if self.cm.get_instance_port(sub["id"]) == port:
                    return sub["id"]

        return None

    def _match_from_settings(self) -> str | None:
        """Fallback: parse ~/.claude/settings.json to detect active sub.

        Mirrors the logic in _match_from_claude_output but reads settings
        directly instead of shelling out to claude CLI.
        Checks settings.json["env"] first, then os.environ as last resort.
        """
        import os
        env = self._load_settings().get("env", {})
        base_url = env.get("ANTHROPIC_BASE_URL", "")
        # CLAUDE_CODE_OAUTH_TOKEN: check settings.json first, then os.environ
        oauth_token = env.get("CLAUDE_CODE_OAUTH_TOKEN", "") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")

        # OAuth match: CLAUDE_CODE_OAUTH_TOKEN matches a sub's api_key
        if oauth_token:
            for sub in self.cm.subscriptions:
                stored_key = sub.get("api_key", "")
                if stored_key and stored_key == oauth_token:
                    return sub["id"]
                # Also check resolve_api_key for env-variants
                if sub.get("auth_type") == "oauth":
                    resolved = self._resolve_api_key(sub, allow_subprocess=False)
                    if resolved and resolved == oauth_token:
                        return sub["id"]

        if not base_url:
            return None

        # Direct match: base_url == provider_url
        for sub in self.cm.subscriptions:
            if sub.get("auth_type") == "direct":
                if base_url == sub.get("provider_url", ""):
                    return sub["id"]

        # Proxy match: http://localhost:<port>
        m = re.match(r"http://localhost:(\d+)", base_url)
        if m:
            port = int(m.group(1))
            for sub in self.cm.subscriptions:
                if self.cm.get_instance_port(sub["id"]) == port:
                    return sub["id"]

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
        """Sync sub_id's .env → settings.json for Claude Code.

        Steps:
        1. Copy instance .env to ~/.claude-mux/.env
        2. Merge relevant keys (only MERGE_KEYS) to ~/.claude/settings.json
        3. Set CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
        4. Set ANTHROPIC_DISABLE_TELEMETRY=true
        5. Write ~/.claude-mux/active-name for statusline
        """
        sub = self.cm.get_subscription(sub_id)
        if sub is None:
            raise ValueError(f"Subscription {sub_id} not found")

        # 1. Copy instance .env to ~/.claude-mux/.env
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
            # Claude Max: remove proxy vars, set OAuth token directly
            merged["ANTHROPIC_BASE_URL"] = None
            merged["ANTHROPIC_AUTH_TOKEN"] = None
            if api_key:
                merged["CLAUDE_CODE_OAUTH_TOKEN"] = api_key
        elif auth_type == "oauth_proxy":
            # OAuth via local proxy — Claude peger på localhost, token sendes af proxy
            merged["ANTHROPIC_BASE_URL"] = listen_addr
            if api_key:
                merged["ANTHROPIC_AUTH_TOKEN"] = api_key
            merged["CLAUDE_CODE_OAUTH_TOKEN"] = None
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

        # Map model maps to settings via ANTHROPIC_DEFAULT_*_MODEL.
        # If force_model is set on the subscription, all three aliases map to it.
        model_maps = sub.get("model_maps", {})
        force_model = sub.get("force_model", "")
        _aliases = (
            ("haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
            ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
            ("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
        )
        if force_model and force_model != "__none__":
            # Force all aliases to the same model — override any model_maps
            for _, env_key in _aliases:
                merged[env_key] = force_model
        else:
            for map_key, env_key in _aliases:
                if model_maps.get(map_key):
                    merged[env_key] = model_maps[map_key]
                # Empty model_map → leave existing env var untouched (don't write None)

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

    # ------------------------------------------------------------------
    # Model discovery
    # ------------------------------------------------------------------

    def fetch_available_models(self, sub_id: str) -> list[str]:
        """Fetch available model IDs from the subscription's API endpoint.

        Routes by auth_type:
          oauth       → GET https://api.anthropic.com/v1/models  (+ beta header)
          direct      → GET <provider_url>/v1/models
          bearer/gh_token/oauth_proxy → GET http://localhost:<port>/v1/models

        Persists results to subscriptions.json via config.update_subscription_models.
        Returns model id list (empty on any error).
        """
        import json as _json
        import time as _time
        from urllib.request import Request, urlopen
        from urllib.error import URLError, HTTPError

        sub = self.cm.get_subscription(sub_id)
        if not sub:
            return []

        auth_type = sub.get("auth_type", "bearer")
        api_key = self._resolve_api_key(sub, allow_subprocess=(auth_type == "gh_token"))

        try:
            if auth_type == "oauth":
                url = "https://api.anthropic.com/v1/models"
                req = Request(url, method="GET")
                req.add_header("Authorization", f"Bearer {api_key}")
                req.add_header("anthropic-version", "2023-06-01")
                req.add_header("anthropic-beta", "oauth-2025-04-20")
            elif auth_type == "direct":
                base = sub.get("provider_url", "").rstrip("/")
                if not base:
                    log.warning("fetch_available_models: no provider_url for %s", sub["name"])
                    self.cm.update_subscription_models(sub_id, [], None)
                    return []
                url = f"{base}/v1/models"
                req = Request(url, method="GET")
                req.add_header("Authorization", f"Bearer {api_key}")
                req.add_header("anthropic-version", "2023-06-01")
            else:
                # bearer, gh_token, oauth_proxy — all via local proxy
                port = self.cm.get_instance_port(sub_id)
                if not port:
                    log.warning("fetch_available_models: no port for %s", sub["name"])
                    self.cm.update_subscription_models(sub_id, [], None)
                    return []
                url = f"http://localhost:{port}/v1/models"
                req = Request(url, method="GET")
                req.add_header("anthropic-version", "2023-06-01")
                if api_key:
                    req.add_header("x-api-key", api_key)

            with urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")

            data = _json.loads(raw)
            models = [
                m["id"] for m in data.get("data", [])
                if isinstance(m, dict) and m.get("id")
            ][:200]

            log.info("fetch_available_models: %s → %d models", sub["name"], len(models))
            self.cm.update_subscription_models(sub_id, models, _time.time())
            return models

        except (HTTPError, URLError, OSError) as e:
            log.warning("fetch_available_models: %s failed: %s", sub.get("name", sub_id), e)
            self.cm.update_subscription_models(sub_id, [], None)
            return []
        except Exception as e:
            log.warning("fetch_available_models: unexpected error for %s: %s", sub.get("name", sub_id), e)
            self.cm.update_subscription_models(sub_id, [], None)
            return []

    def resolve_model_for_tier(self, sub: dict, tier: str) -> str | None:
        """Return the best model ID for the given tier on this subscription.

        Resolution order:
          1. sub["model_maps"][tier] — if non-empty and not blacklisted
          2. sub["available_models"] — first entry containing tier keyword (case-insensitive)
             and not in blacklisted_models
          3. None — caller falls back to static defaults

        tier should be one of: "haiku", "sonnet", "opus".
        """
        blacklisted = set(sub.get("blacklisted_models", []))

        # 1. Explicit model_maps entry
        mapped = sub.get("model_maps", {}).get(tier, "")
        if mapped and mapped not in blacklisted:
            return mapped

        # 2. Search available_models for tier keyword
        for model_id in sub.get("available_models", []):
            if tier.lower() in model_id.lower() and model_id not in blacklisted:
                return model_id

        return None

    # ------------------------------------------------------------------
    # Shared inference test (used by both CLI and TUI)
    # ------------------------------------------------------------------

    def inference_test(self, sub: dict, model: str) -> dict:
        """POST /v1/messages to the appropriate endpoint for any auth_type.

        Routes by auth_type:
          oauth  → api.anthropic.com directly (with OAuth + beta header)
          direct → provider_url directly (with Bearer header)
          other  → http://localhost:<port> (bearer, gh_token, oauth_proxy via proxy)

        Returns dict: {code: int, body: str, elapsed: int (ms), model: str}.
        code=0 means connection error (not an HTTP error).
        """
        import json as _json
        import time as _time
        from urllib.request import Request, urlopen
        from urllib.error import URLError, HTTPError

        auth_type = sub.get("auth_type", "bearer")
        api_key = self._resolve_api_key(sub)

        payload = _json.dumps({
            "model": model,
            "max_tokens": 100,
            "stream": False,
            "messages": [{"role": "user", "content": "Tell me a fun fact about the universe in 2 sentences."}],
        }).encode()

        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}

        if auth_type == "oauth":
            url = "https://api.anthropic.com/v1/messages"
            headers["Authorization"] = f"Bearer {api_key}"
            headers["anthropic-beta"] = "oauth-2025-04-20"
        elif auth_type == "direct":
            base = sub.get("provider_url", "").rstrip("/")
            url = f"{base}/v1/messages"
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            port = self.cm.get_instance_port(sub["id"])
            if not port:
                return {"code": 0, "body": "Proxy not running (no port assigned)", "elapsed": 0, "model": model}
            url = f"http://localhost:{port}/v1/messages"
            if api_key:
                headers["x-api-key"] = api_key

        t0 = _time.time()
        try:
            req = Request(url, data=payload, method="POST", headers=headers)
            try:
                with urlopen(req, timeout=INFERENCE_TIMEOUT) as resp:
                    code = resp.getcode()
                    raw = resp.read().decode("utf-8", errors="replace")
            except HTTPError as e:
                code = e.code
                raw = e.read().decode("utf-8", errors="replace")
            elapsed = int((_time.time() - t0) * 1000)
            body = extract_response_body(raw, code)
            log.info("inference_test %s %s: HTTP %d (%dms)", sub.get("name", "?"), model, code, elapsed)
            return {"code": code, "body": body, "elapsed": elapsed, "model": model}
        except (URLError, OSError) as e:
            elapsed = int((_time.time() - t0) * 1000)
            log.warning("inference_test %s %s: connection error: %s", sub.get("name", "?"), model, e)
            return {"code": 0, "body": f"Connection error: {e}", "elapsed": elapsed, "model": model}
        except Exception as e:
            elapsed = int((_time.time() - t0) * 1000)
            return {"code": 0, "body": f"Error: {e}", "elapsed": elapsed, "model": model}

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
