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

import contextlib
import functools
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX: thread lock only
    fcntl = None

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

# Provider-specific keys in settings.json["env"]: endpoint, credentials and model
# selection. claude-mux owns these — on every activation each one is either set
# for the new provider or removed, so nothing from the previous provider lingers.
# Keys not listed here (the user's own env vars, e.g. their ANTHROPIC_API_KEY, which
# claude-mux never writes) are never touched.
PROVIDER_ENV_KEYS = {
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
} | SETTINGS_KEYS_TO_REMOVE

# Provider-neutral model aliases for settings.json["model"] (Claude Code resolves
# them through ANTHROPIC_DEFAULT_*_MODEL), so they survive a provider switch.
CLAUDE_MODEL_ALIASES = {"default", "sonnet", "opus", "haiku", "opusplan", "sonnet[1m]", "opus[1m]"}

# --- .env template keys ---

ENV_TEMPLATE_KEYS = {
    "ANTHROPIC_BASE_URL": "",
    "ANTHROPIC_API_KEY": "",
    "LISTEN_ADDR": "127.0.0.1:18080",
    "MODEL_MAP_HAIKU": "",
    "MODEL_MAP_SONNET": "",
    "MODEL_MAP_OPUS": "",
    "REQUEST_TIMEOUT_MS": "120000",
    "MAX_TOKENS": "4096",
    "SERVER_METRICS_PORT": "",
    "MAX_RETRIES": "3",
    "PROXY_TARGET_URL": "",
    "PROXY_AUTH_TOKEN": "",
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
        "model_maps": {"haiku": "gpt-4o-mini", "sonnet": "gpt-4o", "opus": "gpt-4.1"},
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
    "claude-max-proxy": {
        "label": "Claude (OAuth via proxy)",
        "provider_url": "https://api.anthropic.com",
        "auth_type": "oauth_proxy",
        "api_key_env": "CLAUDE_CODE_OAUTH_TOKEN",
        "model_maps": {
            "haiku": "claude-haiku-4-5-20251001",
            "sonnet": "claude-sonnet-4-6",
            "opus": "claude-opus-4-6",
        },
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


def _reads(method):
    """Re-read subscriptions.json first if another process changed it."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        self._refresh()
        return method(self, *args, **kwargs)
    return wrapper


def _mutates(method):
    """Run a read-modify-write under the config lock, on fresh data."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._locked():
            return method(self, *args, **kwargs)
    return wrapper


class ConfigManager:
    """Central config manager for ~/.claude-mux/subscriptions.json.

    The file is shared by every claude-mux process (TUI, CLI, session helpers).
    Reads pick up changes made by other processes (cheap stat check), and every
    change is a locked read-modify-write on fresh data, so a long-running TUI
    never writes a stale copy over subscriptions added from the CLI.
    """

    def __init__(self, data_file: Path | None = None):
        self._data_file: Path = data_file or SUBSCRIPTIONS_FILE
        self._lock_file: Path = self._data_file.with_name(self._data_file.name + ".lock")
        self._thread_lock = threading.RLock()
        self._lock_depth = 0
        self._disk_sig: tuple | None = None
        self._ensure_dir()
        self._data = self._load()

    # --- Cross-process consistency ---

    def _signature(self) -> tuple | None:
        try:
            st = self._data_file.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _refresh(self):
        """Reload if the file on disk is not the version we last read or wrote."""
        with self._thread_lock:  # waits while another thread is mid-change
            if self._lock_depth:
                return  # this thread is inside a locked change: data is already fresh
            if self._signature() != self._disk_sig:
                self._data = self._load()

    @contextlib.contextmanager
    def _locked(self):
        """Exclusive lock (threads + processes) around a read-modify-write."""
        with self._thread_lock:
            fd = None
            if self._lock_depth == 0:
                if fcntl is not None:
                    fd = os.open(self._lock_file, os.O_RDWR | os.O_CREAT, 0o600)
                    fcntl.flock(fd, fcntl.LOCK_EX)
                if self._signature() != self._disk_sig:
                    self._data = self._load()
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
                if fd is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)

    # --- Directory setup ---

    def _ensure_dir(self):
        self._data_file.parent.mkdir(parents=True, exist_ok=True)

    # --- Load / Save ---

    def _load(self) -> dict:
        self._disk_sig = self._signature()
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
        data.setdefault("instances", {})
        # Migration: remove auto-created claude-backup/auto-custom subs
        before_count = len(data["subscriptions"])
        data = self._migrate(data)
        if len(data["subscriptions"]) != before_count:
            # Persist migration so it doesn't re-run on next start
            _atomic_write(self._data_file, data)
            self._disk_sig = self._signature()
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
        # Clear default_instance if it points to a removed subscription
        remaining_ids = {s["id"] for s in data["subscriptions"]}
        default = data.get("default_instance")
        if default and default not in remaining_ids:
            data["default_instance"] = None
        return data

    def _save(self):
        _atomic_write(self._data_file, self._data)
        self._disk_sig = self._signature()

    @property
    @_reads
    def subscriptions(self) -> list:
        return list(self._data.get("subscriptions", []))

    # --- Subscription CRUD ---

    @_mutates
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

    @_reads
    def get_subscription(self, sub_id: str) -> dict | None:
        for sub in self._data["subscriptions"]:
            if sub["id"] == sub_id:
                return sub
        return None

    @_mutates
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

    @_mutates
    def delete_subscription(self, sub_id: str) -> bool:
        before = len(self._data["subscriptions"])
        self._data["subscriptions"] = [
            s for s in self._data["subscriptions"] if s["id"] != sub_id
        ]
        self._data["instances"].pop(sub_id, None)
        if len(self._data["subscriptions"]) < before:
            if self._data.get("default_instance") == sub_id:
                self._data["default_instance"] = None
            self._save()
            return True
        return False

    # --- Default instance (fallback for detect_active) ---

    @property
    @_reads
    def default_instance(self) -> str | None:
        """Return configured default subscription id, or None."""
        return self._data.get("default_instance")

    @_mutates
    def set_default(self, sub_id: str) -> bool:
        """Set the default subscription. Returns False if sub_id not found."""
        if not self.get_subscription(sub_id):
            return False
        self._data["default_instance"] = sub_id
        self._save()
        return True

    @_mutates
    def set_model_maps(self, sub_id: str, model_maps: dict) -> bool:
        """Replace (not merge) a subscription's model maps. False if not found."""
        sub = self.get_subscription(sub_id)
        if sub is None:
            return False
        sub["model_maps"] = dict(model_maps)
        sub["updated_at"] = _now()
        self._save()
        return True

    # --- Model cache + blacklist ---

    @_mutates
    def update_subscription_models(
        self,
        sub_id: str,
        available_models: list[str],
        models_fetched_at: float | None,
    ) -> bool:
        """Persist available_models + fetch timestamp without touching other fields."""
        sub = self.get_subscription(sub_id)
        if sub is None:
            return False
        sub["available_models"] = available_models[:200]  # cap defensively
        sub["models_fetched_at"] = models_fetched_at
        sub["updated_at"] = _now()
        self._save()
        return True

    @_mutates
    def add_blacklisted_model(self, sub_id: str, model_id: str) -> bool:
        """Add model_id to sub's blacklist. Returns False if sub not found."""
        sub = self.get_subscription(sub_id)
        if sub is None:
            return False
        bl = sub.setdefault("blacklisted_models", [])
        if model_id not in bl:
            bl.append(model_id)
            sub["updated_at"] = _now()
            self._save()
        return True

    @_mutates
    def remove_blacklisted_model(self, sub_id: str, model_id: str) -> bool:
        """Remove model_id from sub's blacklist. Returns False if sub not found."""
        sub = self.get_subscription(sub_id)
        if sub is None:
            return False
        bl = sub.get("blacklisted_models", [])
        if model_id in bl:
            bl.remove(model_id)
            sub["updated_at"] = _now()
            self._save()
        return True

    @_reads
    def get_blacklisted_models(self, sub_id: str) -> list[str]:
        """Return list of blacklisted model IDs for sub, or [] if sub not found."""
        sub = self.get_subscription(sub_id)
        if sub is None:
            return []
        return list(sub.get("blacklisted_models", []))

    # --- Port allocation ---

    @_reads
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

    @_reads
    def get_instance_port(self, sub_id: str) -> int | None:
        inst = self._data.get("instances", {}).get(sub_id)
        if inst and isinstance(inst, dict):
            return inst.get("port")
        return None

    @_mutates
    def set_instance_port(self, sub_id: str, port: int):
        self._data.setdefault("instances", {})
        if sub_id not in self._data["instances"]:
            self._data["instances"][sub_id] = {}
        self._data["instances"][sub_id]["port"] = port
        self._save()

    @_mutates
    def clear_instance_port(self, sub_id: str):
        """Release port on stop — so it can be reused by others."""
        inst = self._data.get("instances", {}).get(sub_id)
        if inst and "port" in inst:
            del inst["port"]
            self._save()

    @_reads
    def get_pm2_name(self, sub_id: str) -> str | None:
        inst = self._data.get("instances", {}).get(sub_id)
        if inst and isinstance(inst, dict):
            return inst.get("pm2_name")
        return None

    @_reads
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
