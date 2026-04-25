"""
claude-mux config module — constants, helpers, ConfigManager.

Data model and persistence for ~/.claude-mux/subscriptions.json.
API keys are stored ONLY as env var references, never in subscriptions.json.
"""

# Dependency-check BEFORE other imports — gives clear error message and offers install
import sys as _sys  # noqa: E401 — must be BEFORE dep-check


def _check_and_install_deps():
    missing = []
    for pkg, import_name in [("textual", "textual"), ("rich", "rich"), ("psutil", "psutil")]:
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"claude-mux is missing Python packages: {', '.join(missing)}")
        answer = input("Install now with pip? [Y/n] ").strip().lower()
        if answer in ("", "j", "y", "ja", "yes"):
            import subprocess as _sp
            _sp.check_call([_sys.executable, "-m", "pip", "install", "--quiet"] + missing)
            print("Installation complete — restart claude-mux.")
        else:
            print("Aborted. Install manually: pip install " + " ".join(missing))
        raise SystemExit(1)


_check_and_install_deps()

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Safe subscription name: alphanumeric, dots, hyphens, underscores; max 100 chars.
# Prevents path traversal (no slashes) and shell injection.
_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]{1,100}$")

CLAUDE_MUX_DIR = Path.home() / ".claude-mux"
SUBSCRIPTIONS_FILE = CLAUDE_MUX_DIR / "subscriptions.json"
DEFAULT_PORT_RANGE_START = 18080
MAX_PORT = 65535

# Logging to ~/.claude-mux/claude-mux.log
_LOG_FILE = CLAUDE_MUX_DIR / "claude-mux.log"
CLAUDE_MUX_DIR.mkdir(parents=True, exist_ok=True)  # ensure dir exists before logging
logging.basicConfig(
    filename=str(_LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("claude-mux")

EMPTY_SUBSCRIPTIONS = {
    "version": 1,
    "subscriptions": [],
    "default_instance": None,
    "instances": {},
}

ENV_TO_SETTINGS_MAP = {
    "ANTHROPIC_API_KEY": "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL": "ANTHROPIC_BASE_URL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "ANTHROPIC_DEFAULT_OPUS_MODEL",
}
MERGE_KEYS = set(ENV_TO_SETTINGS_MAP.keys())
# Keys to remove from settings.json during sync (deprecated approach)
SETTINGS_KEYS_TO_REMOVE = {
    "ANTHROPIC_CUSTOM_MODEL_OPTION",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
}

# --- .env template keys ---

ENV_TEMPLATE_KEYS = {
    "ANTHROPIC_BASE_URL": "",
    "ANTHROPIC_API_KEY": "",
    "LISTEN_ADDR": ":18080",
    "MODEL_MAP_HAIKU": "",
    "MODEL_MAP_SONNET": "",
    "MODEL_MAP_OPUS": "",
    "REQUEST_TIMEOUT_MS": "120000",
    "MAX_TOKENS": "4096",
    "SERVER_METRICS_PORT": "",
    "MAX_RETRIES": "3",
}

PROVIDER_PRESETS = {
    "deepseek": {
        "label": "DeepSeek",
        "provider_url": "https://api.deepseek.com/v1",
        "auth_type": "bearer",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model_maps": {"haiku": "deepseek-chat", "sonnet": "deepseek-chat", "opus": "deepseek-reasoner"},
    },
    "anthropic": {
        "label": "Anthropic",
        "provider_url": "https://api.anthropic.com/v1",
        "auth_type": "bearer",
        "api_key_env": "ANTHROPIC_API_KEY",
        "model_maps": {},
    },
    "openai": {
        "label": "OpenAI / ChatGPT",
        "provider_url": "https://api.openai.com/v1",
        "auth_type": "bearer",
        "api_key_env": "OPENAI_API_KEY",
        "model_maps": {"haiku": "gpt-4o-mini", "sonnet": "gpt-4o", "opus": "o1-mini"},
    },
    "copilot": {
        "label": "GitHub Copilot",
        "provider_url": "https://api.githubcopilot.com",
        "auth_type": "gh_token",
        "api_key_env": "GH_TOKEN",
        "model_maps": {"haiku": "claude-haiku-4.5", "sonnet": "claude-sonnet-4.6", "opus": "claude-opus-4.7"},
    },
    "claude-max": {
        "label": "Claude (OAuth)",
        "provider_url": "",
        "auth_type": "oauth",
        "api_key_env": "",
        "model_maps": {},
    },
    "gemini": {
        "label": "Gemini (Google)",
        "provider_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "auth_type": "x-goog-api-key",
        "api_key_env": "GEMINI_API_KEY",
        "model_maps": {"haiku": "gemini-2.0-flash", "sonnet": "gemini-2.0-pro", "opus": "gemini-2.5-pro"},
    },
    "z-ai": {
        "label": "z.ai (direct)",
        "provider_url": "https://api.z.ai/api/anthropic",
        "auth_type": "direct",
        "api_key_env": "Z_AI_API_KEY",
        "model_maps": {"haiku": "glm-4.5-air", "sonnet": "glm-4.7", "opus": "glm-ww5"},
    },
    "custom": {
        "label": "Custom (OpenAI-compatible)",
        "provider_url": "",
        "auth_type": "bearer",
        "api_key_env": "CUSTOM_API_KEY",
        "model_maps": {},
    },
}

# Reverse-lookup: provider_url → display label (for detail panel)
PROVIDER_URL_LABELS: dict[str, str] = {
    p["provider_url"]: p["label"]
    for p in PROVIDER_PRESETS.values()
    if p.get("provider_url")
}

# Copilot models that support /chat/completions or /v1/messages
COPILOT_CHAT_ENDPOINTS = {"/chat/completions", "/v1/messages"}


def fetch_copilot_models(token: str) -> list[dict]:
    """Fetch available chat models from api.githubcopilot.com/models.
    Returns list of {"id": str, "name": str, "category": str}.
    """
    import json as _json
    import subprocess as _sp
    try:
        result = _sp.run(
            ["curl", "-s", "-H", f"Authorization: Bearer {token}",
             "-H", "Copilot-Integration-Id: vscode-chat",
             "https://api.githubcopilot.com/models"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        data = _json.loads(result.stdout)
        models = []
        seen = set()
        for m in data.get("data", []):
            mid = m.get("id", "")
            if not mid or mid in seen:
                continue
            # Only chat models with relevant endpoint
            endpoints = set(m.get("supported_endpoints", []))
            if not (endpoints & COPILOT_CHAT_ENDPOINTS):
                continue
            if m.get("capabilities", {}).get("type") != "chat":
                continue
            seen.add(mid)
            models.append({
                "id": mid,
                "name": m.get("name", mid),
                "category": m.get("model_picker_category", ""),
                "vendor": m.get("vendor", ""),
            })
        return models
    except Exception:
        return []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_id() -> str:
    return str(uuid.uuid4())


def _port_is_available(port: int, timeout: float = 1.0) -> bool:
    """Check if a port is available via connect_ex."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex(("127.0.0.1", port)) != 0
    except OSError:
        return False


def _atomic_write(path: Path, data: dict):
    """Atomic write: .tmp + os.rename() + fsync. Backup before write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Backup
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    # Write atomically
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.rename(path)
    path.chmod(0o600)


class ConfigManager:
    """Central config manager for ~/.claude-mux/subscriptions.json.

    No caching — every CRUD operation reads/writes directly to disk.
    """

    def __init__(self, data_file: Path | None = None):
        self._data_file: Path = data_file or SUBSCRIPTIONS_FILE
        self._ensure_dir()
        self._data = self._load()

    # --- Directory setup ---

    def _ensure_dir(self):
        self._data_file.parent.mkdir(parents=True, exist_ok=True)

    # --- Load / Save ---

    def _load(self) -> dict:
        if not self._data_file.exists():
            import copy
            return copy.deepcopy(EMPTY_SUBSCRIPTIONS)
        try:
            with open(self._data_file) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            import copy
            return copy.deepcopy(EMPTY_SUBSCRIPTIONS)
        if not isinstance(data, dict) or data.get("version") != 1:
            import copy
            return copy.deepcopy(EMPTY_SUBSCRIPTIONS)
        data.setdefault("subscriptions", [])
        data.setdefault("default_instance", None)
        data.setdefault("instances", {})
        # Migration: remove auto-created claude-backup/auto-custom subs
        before_count = len(data["subscriptions"])
        data = self._migrate(data)
        if len(data["subscriptions"]) != before_count:
            # Persist migration so it doesn't re-run on next start
            _atomic_write(self._data_file, data)
        return data

    @staticmethod
    def _migrate(data: dict) -> dict:
        """Run data migrations. Return updated data."""
        _auto_names = {"claude-backup", "auto-custom"}
        _auto_note = "Backup af oprindelig Claude-konfiguration"
        before = len(data["subscriptions"])
        data["subscriptions"] = [
            s for s in data["subscriptions"]
            if s.get("name") not in _auto_names
            and _auto_note not in s.get("notes", "")
        ]
        removed = before - len(data["subscriptions"])
        if removed:
            import logging as _lg
            _lg.getLogger(__name__).info(
                "Migration: removed %d auto-created subscription(s)", removed
            )
            # Reset default if it pointed to a removed sub
            remaining_ids = {s["id"] for s in data["subscriptions"]}
            if data.get("default_instance") not in remaining_ids:
                data["default_instance"] = None
        return data

    def _save(self):
        _atomic_write(self._data_file, self._data)

    @property
    def subscriptions(self) -> list:
        return list(self._data.get("subscriptions", []))

    @property
    def default_instance(self):
        return self._data.get("default_instance")

    # --- Subscription CRUD ---

    def add_subscription(
        self,
        name: str,
        provider_url: str,
        api_key_env: str,
        label: str | None = None,
        auth_type: str = "bearer",
        model_maps: dict | None = None,
        notes: str = "",
        api_key: str = "",
    ) -> dict:
        """Add new subscription.

        Raises ValueError if name contains invalid characters (path traversal prevention).
        """
        if not _NAME_RE.match(name):
            raise ValueError(
                f"Invalid subscription name {name!r}. "
                "Use only letters, digits, dots, hyphens, underscores (max 100 chars)."
            )
        sub_id = _generate_id()
        now = _now()
        pm2_name = f"claude-mux-{name}"
        sub = {
            "id": sub_id,
            "name": name,
            "label": name,
            "auth_type": auth_type,
            "provider_url": provider_url,
            "api_key_env": api_key_env,
            "model_maps": model_maps or {},
            "created_at": now,
            "updated_at": now,
            "notes": notes,
        }
        if api_key:
            sub["api_key"] = api_key
        self._data["subscriptions"].append(sub)
        self._data["instances"][sub_id] = {
            "pm2_name": pm2_name,
        }
        self._save()
        return sub

    def get_subscription(self, sub_id: str) -> dict | None:
        for sub in self._data["subscriptions"]:
            if sub["id"] == sub_id:
                return sub
        return None

    def update_subscription(self, sub_id: str, **kwargs) -> dict | None:
        sub = self.get_subscription(sub_id)
        if sub is None:
            return None
        updatable = {
            "name", "label", "auth_type", "provider_url",
            "api_key_env", "model_maps", "notes", "api_key",
        }
        for key, val in kwargs.items():
            if key in updatable:
                if key == "model_maps" and isinstance(val, dict) and isinstance(sub.get("model_maps"), dict):
                    # Partial merge — preserve existing keys not being updated
                    sub["model_maps"] = {**sub["model_maps"], **val}
                else:
                    sub[key] = val
        if "name" in kwargs:
            sub["label"] = kwargs["name"]
        sub["updated_at"] = _now()
        self._save()
        return sub

    def delete_subscription(self, sub_id: str) -> bool:
        before = len(self._data["subscriptions"])
        self._data["subscriptions"] = [
            s for s in self._data["subscriptions"] if s["id"] != sub_id
        ]
        self._data["instances"].pop(sub_id, None)
        if self._data.get("default_instance") == sub_id:
            self._data["default_instance"] = None
        if len(self._data["subscriptions"]) < before:
            self._save()
            return True
        return False

    # --- Default instance ---

    def set_default(self, sub_id: str) -> bool:
        if self.get_subscription(sub_id) is None:
            return False
        self._data["default_instance"] = sub_id
        self._save()
        return True

    # --- Port allocation ---

    def _allocate_port(self) -> int:
        """Find next available port (18080+). Check port bind before allocation."""
        used_ports = {
            inst["port"]
            for inst in self._data.get("instances", {}).values()
            if isinstance(inst, dict) and "port" in inst
        }
        port = DEFAULT_PORT_RANGE_START
        while port <= MAX_PORT:
            if port not in used_ports and _port_is_available(port):
                return port
            port += 1
        raise RuntimeError(
            f"No free port between {DEFAULT_PORT_RANGE_START} and {MAX_PORT}"
        )

    # --- Instance helpers ---

    def get_instance_port(self, sub_id: str) -> int | None:
        inst = self._data.get("instances", {}).get(sub_id)
        if inst and isinstance(inst, dict):
            return inst.get("port")
        return None

    def set_instance_port(self, sub_id: str, port: int):
        self._data.setdefault("instances", {})
        if sub_id not in self._data["instances"]:
            self._data["instances"][sub_id] = {}
        self._data["instances"][sub_id]["port"] = port
        self._save()

    def clear_instance_port(self, sub_id: str):
        """Release port on stop — so it can be reused by others."""
        inst = self._data.get("instances", {}).get(sub_id)
        if inst and "port" in inst:
            del inst["port"]
            self._save()

    def get_pm2_name(self, sub_id: str) -> str | None:
        inst = self._data.get("instances", {}).get(sub_id)
        if inst and isinstance(inst, dict):
            return inst.get("pm2_name")
        return None

    def get_instance_dir(self, sub_id: str) -> Path | None:
        """Return the instance directory (where .env, logs are stored)."""
        sub = self.get_subscription(sub_id)
        if sub is None:
            return None
        d = CLAUDE_MUX_DIR / "instances" / sub["name"]
        return d if d.exists() else None

    # --- API key resolution ---

    def resolve_api_key(self, sub: dict, *, allow_subprocess: bool = True) -> str:
        """Return the effective API key for a subscription.

        Resolution order:
        1. sub["api_key"] (stored literal — OAuth tokens, direct bearer)
        2. gh auth token subprocess (gh_token auth only, if allow_subprocess)
        3. os.environ[api_key_env]

        This method lives on ConfigManager because key resolution is a config
        concern: it reads stored subscription data and environment variables.
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
