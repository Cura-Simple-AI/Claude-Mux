#!/usr/bin/env python3
"""
Heimsense TUI Manager — ConfigManager + InstanceManager

Data model and persistence for AI subscriptions.
API keys are stored ONLY as env var references, never in subscriptions.json.
"""
__version__ = "0.1.3"

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
        print(f"Heimsense is missing Python packages: {', '.join(missing)}")
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
import sys
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
    ) -> dict:
        """Add new subscription."""
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
        "label": "z.ai",
        "provider_url": "https://api.z.ai/v1",
        "auth_type": "bearer",
        "api_key_env": "Z_AI_API_KEY",
        "model_maps": {"haiku": "z-ai-mini", "sonnet": "z-ai-medium", "opus": "z-ai-max"},
    },
    "custom": {
        "label": "Custom (OpenAI-compatible)",
        "provider_url": "",
        "auth_type": "bearer",
        "api_key_env": "CUSTOM_API_KEY",
        "model_maps": {},
    },
}

# Copilot models that support /chat/completions or /v1/messages
COPILOT_CHAT_ENDPOINTS = {"/chat/completions", "/v1/messages"}

def fetch_copilot_models(token: str) -> list[dict]:
    """Fetch available chat models from api.githubcopilot.com/models.
    Returns list of {"id": str, "name": str, "category": str}.
    """
    try:
        result = subprocess.run(
            ["curl", "-s", "-H", f"Authorization: Bearer {token}",
             "-H", "Copilot-Integration-Id: vscode-chat",
             "https://api.githubcopilot.com/models"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
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


class InstanceManager:
    """PM2 lifecycle for Heimsense proxy instances.

    Handles start/stop/restart via PM2, .env generation, and
    ecosystem.config.js merge (preserves non-claude-mux apps).
    """

    CLAUDE_MUX_BIN = os.environ.get(
        "CLAUDE_MUX_BIN",
        str(Path.home() / ".local" / "bin" / "claude-mux"),
    )
    ECOSYSTEM_PATH = Path.home() / ".ecosystem.config.js"

    def __init__(self, config: ConfigManager):
        self.cm = config
        self._http_cleared: set[str] = set()  # sub_ids where http-status was cleared on Start

    # --- .env management ---

    def ensure_instance_dir(self, sub_id: str) -> Path:
        """Return instance .env directory. Create if missing."""
        sub = self.cm.get_subscription(sub_id)
        if sub is None:
            raise ValueError(f"Subscription {sub_id} not found")
        inst_dir = CLAUDE_MUX_DIR / "instances" / sub["name"]
        inst_dir.mkdir(parents=True, exist_ok=True)
        return inst_dir

    def generate_env(self, sub_id: str) -> Path:
        """Generate .env for instance. Return path."""
        sub = self.cm.get_subscription(sub_id)
        if sub is None:
            raise ValueError(f"Subscription {sub_id} not found")
        port = self.cm.get_instance_port(sub_id)
        inst_dir = self.ensure_instance_dir(sub_id)
        env_path = inst_dir / ".env"

        lines = []
        for key in ENV_TEMPLATE_KEYS:
            lines.append(f"{key}={ENV_TEMPLATE_KEYS[key]}")

        # Fill in known values
        self._set_line(lines, "ANTHROPIC_BASE_URL", sub["provider_url"])
        self._set_line(lines, "LISTEN_ADDR", f":{port}")
        self._set_line(lines, "MODEL_MAP_HAIKU", sub.get("model_maps", {}).get("haiku", ""))
        self._set_line(lines, "MODEL_MAP_SONNET", sub.get("model_maps", {}).get("sonnet", ""))
        self._set_line(lines, "MODEL_MAP_OPUS", sub.get("model_maps", {}).get("opus", ""))

        # Write API key to .env (Fund 1 fix)
        api_key_env = sub.get("api_key_env", "")
        auth_type = sub.get("auth_type", "bearer")

        # OAuth: token stored directly in subscription
        api_key_val = sub.get("api_key", "")
        if not api_key_val:
            if auth_type == "gh_token":
                try:
                    gh_result = subprocess.run(
                        ["gh", "auth", "token"], capture_output=True, text=True, timeout=10,
                    )
                    if gh_result.returncode == 0:
                        api_key_val = gh_result.stdout.strip()
                    else:
                        api_key_val = os.environ.get(api_key_env, "")
                        log.warning("gh auth token failed, falling back to env %s", api_key_env)
                except Exception as e:
                    api_key_val = os.environ.get(api_key_env, "")
                    log.warning("gh auth token exception: %s", e)
            else:
                api_key_val = os.environ.get(api_key_env, "") if api_key_env else ""
        if api_key_val:
            self._set_line(lines, "ANTHROPIC_API_KEY", api_key_val)

        env_path.write_text("\n".join(lines) + "\n")
        env_path.chmod(0o600)
        return env_path

    @staticmethod
    def _set_line(lines: list[str], key: str, value: str):
        """Update or append KEY=value in lines list (in-place)."""
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                return
        # Key not in template — append (ensures new keys are always written)
        lines.append(f"{key}={value}")

    # --- PM2 lifecycle ---

    def start(self, sub_id: str) -> dict:
        """Start claude-mux for a subscription via PM2. Return PM2 status."""
        sub = self.cm.get_subscription(sub_id)
        if sub is None:
            raise ValueError(f"Subscription {sub_id} not found")
        pm2_name = self.cm.get_pm2_name(sub_id) or f"claude-mux-{sub['name']}"

        # Allocate port on start — always check that saved port is available
        port = self.cm.get_instance_port(sub_id)
        if not port or not _port_is_available(port):
            port = self.cm._allocate_port()
            self.cm.set_instance_port(sub_id, port)

        # Generate .env and copy to ~/.claude-mux/.env
        env_path = self.generate_env(sub_id)
        CLAUDE_MUX_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(env_path, CLAUDE_MUX_DIR / ".env")

        if not Path(self.CLAUDE_MUX_BIN).exists():
            raise FileNotFoundError(
                f"claude-mux binary not found at {self.CLAUDE_MUX_BIN}. "
                f"Install: curl -fsSL https://raw.githubusercontent.com/cura-ai/claude-mux/main/scripts/install.sh | bash"
            )

        inst_dir = CLAUDE_MUX_DIR / "instances" / sub["name"]
        inst_dir.mkdir(parents=True, exist_ok=True)

        # Delete existing PM2 instance (new port requires new process)
        subprocess.run(["pm2", "delete", pm2_name], capture_output=True, text=True)

        # Start via PM2 — bash sources .env explicitly so shell-env doesn't contaminate
        cmd = f"set -a; source {env_path}; set +a; exec {self.CLAUDE_MUX_BIN} run"
        result = subprocess.run(
            [
                "pm2", "start", "bash",
                "--name", pm2_name,
                "--output", str(inst_dir / "out.log"),
                "--error", str(inst_dir / "error.log"),
                "--", "-c", cmd,
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            log.error("PM2 start failed for %s: %s", pm2_name, err)
            raise RuntimeError(f"PM2 start failed: {err}")
        log.info("PM2 startet %s (port %s, pid se pm2 status)", pm2_name, port)
        self._regenerate_ecosystem()
        self._http_cleared.add(sub_id)
        self._pm2_save()
        return {"name": pm2_name, "port": port, "status": "started"}

    def stop(self, sub_id: str) -> dict:
        """Stop claude-mux via PM2."""
        pm2_name = self.cm.get_pm2_name(sub_id)
        if pm2_name is None:
            raise ValueError(f"No PM2 name for subscription {sub_id}")
        r1 = subprocess.run(["pm2", "stop", pm2_name], capture_output=True, text=True)
        r2 = subprocess.run(["pm2", "delete", pm2_name], capture_output=True, text=True)
        if r1.returncode != 0:
            log.warning("PM2 stop %s: %s", pm2_name, r1.stderr.strip())
        if r2.returncode != 0:
            log.warning("PM2 delete %s: %s", pm2_name, r2.stderr.strip())
        log.info("PM2 stopped %s", pm2_name)
        self.cm.clear_instance_port(sub_id)
        self._regenerate_ecosystem()
        self._pm2_save()
        return {"name": pm2_name, "status": "stopped"}

    def restart(self, sub_id: str) -> dict:
        """Restart claude-mux via PM2 (generate fresh .env first)."""
        self.generate_env(sub_id)
        inst_dir = self.ensure_instance_dir(sub_id)
        inst_env = inst_dir / ".env"
        if inst_env.exists():
            shutil.copy2(inst_env, CLAUDE_MUX_DIR / ".env")
        pm2_name = self.cm.get_pm2_name(sub_id)
        if pm2_name is None:
            return self.start(sub_id)
        result = subprocess.run(["pm2", "restart", pm2_name], capture_output=True, text=True)
        if result.returncode != 0:
            log.error("PM2 restart failed for %s: %s", pm2_name, result.stderr.strip())
            raise RuntimeError(f"PM2 restart failed: {result.stderr.strip()}")
        log.info("PM2 restarted %s", pm2_name)
        self._regenerate_ecosystem()
        return {"name": pm2_name, "status": "restarted"}

    # --- Ecosystem config merge ---

    def _read_existing_ecosystem(self) -> dict:
        """Read existing ecosystem.config.js. Return {'apps': []} if missing."""
        if not self.ECOSYSTEM_PATH.exists():
            return {"apps": []}
        try:
            # Use Node.js to read the JS file (it's CommonJS)
            result = subprocess.run(
                ["node", "-e", """
                    const p = require(String.raw`""" + str(self.ECOSYSTEM_PATH) + """`);
                    console.log(JSON.stringify(p));
                """],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return json.loads(result.stdout.strip())
        except Exception:
            pass
        return {"apps": []}

    def _build_claude_mux_apps(self) -> list[dict]:
        """Build PM2 app entries for all active subscriptions."""
        apps = []
        for sub in self.cm.subscriptions:
            sub_id = sub["id"]
            pm2_name = self.cm.get_pm2_name(sub_id) or f"claude-mux-{sub['name']}"
            inst_dir = CLAUDE_MUX_DIR / "instances" / sub["name"]
            env_path = inst_dir / ".env"
            apps.append({
                "name": pm2_name,
                "script": self.CLAUDE_MUX_BIN,
                "args": "run",
                "env_file": str(env_path) if env_path.exists() else "",
                "cwd": str(CLAUDE_MUX_DIR),
                "error_file": str(inst_dir / "error.log"),
                "out_file": str(inst_dir / "out.log"),
                "pid_file": str(inst_dir / "claude-mux.pid"),
                "max_restarts": 5,
                "restart_delay": 3000,
            })
        return apps

    def _regenerate_ecosystem(self):
        """Regenerate ~/.ecosystem.config.js: keep non-claude-mux apps, add new claude-mux entries."""
        existing = self._read_existing_ecosystem()
        non_claude_mux = [
            a for a in existing.get("apps", [])
            if isinstance(a, dict) and not a.get("name", "").startswith("claude-mux-")
        ]
        claude_mux_apps = self._build_claude_mux_apps()
        merged = {"apps": non_claude_mux + claude_mux_apps}
        js = "module.exports = " + json.dumps(merged, indent=2) + ";\n"
        self.ECOSYSTEM_PATH.write_text(js)

    @staticmethod
    def _pm2_save():
        """Run pm2 save for persistence across reboot."""
        subprocess.run(["pm2", "save"], capture_output=True, text=True)

    # --- Status ---

    def get_status(self, sub_id: str) -> dict:
        """Check PM2 status for an instance + parse latest log for HTTP status."""
        pm2_name = self.cm.get_pm2_name(sub_id)
        if pm2_name is None:
            return {"status": "unknown", "pm2_name": None}
        result = subprocess.run(
            ["pm2", "jlist"], capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.warning("pm2 jlist failed: %s", result.stderr.strip())
            return {"status": "unknown", "pm2_name": pm2_name, "error": result.stderr.strip()}
        try:
            processes = json.loads(result.stdout)
            for proc in processes:
                if proc.get("name") == pm2_name:
                    pm2_status = proc.get("pm2_env", {}).get("status", "stopped")
                    pid = proc.get("pid")
                    # PM2 uptime is start time in epoch ms → format
                    started_at = proc.get("pm2_env", {}).get("pm_uptime") or proc.get("pm2_env", {}).get("created_at")
                    uptime_str = "-"
                    if started_at:
                        secs = int((time.time() * 1000 - started_at) / 1000)
                        uptime_str = _format_duration(secs)
                    # Scan PM2 log for last HTTP status — skip if cleared on Start
                    inst_dir = self.cm.get_instance_dir(sub_id)
                    if sub_id in self._http_cleared:
                        last_status, last_ts = None, None
                    else:
                        last_status, last_ts = self._last_http_status(pm2_name, inst_dir)
                    # Remove clear-flag when first real request is logged
                    if sub_id in self._http_cleared and last_status is not None:
                        self._http_cleared.discard(sub_id)
                    return {
                        "status": pm2_status,
                        "pm2_name": pm2_name,
                        "pid": pid,
                        "uptime": uptime_str,
                        "monit": proc.get("monit", {}),
                        "last_http_status": last_status,
                        "last_http_time": last_ts,
                    }
        except (json.JSONDecodeError, KeyError):
            pass
        return {"status": "stopped", "pm2_name": pm2_name}

    @staticmethod
    def _last_http_status(pm2_name: str, instance_dir: Path | None = None) -> tuple[int | None, float | None]:
        """Scan PM2 out-log for last HTTP status and its timestamp."""
        # Try custom instance dir first, then ~/.pm2/logs/
        candidates = []
        if instance_dir:
            candidates.append(instance_dir / "out.log")
        candidates.append(Path.home() / ".pm2" / "logs" / f"{pm2_name}-out.log")
        log_file = None
        for c in candidates:
            if c.exists():
                log_file = c
                break
        if log_file is None:
            return None, None
        try:
            # Read last 50KB — enough for ~100+ requests
            size = log_file.stat().st_size
            offset = max(0, size - 50_000)
            with open(log_file, "r") as f:
                f.seek(offset)
                # Skip to next full line
                if offset > 0:
                    f.readline()
                lines = f.readlines()
            last_status = None
            last_ts = None
            for line in reversed(lines):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("msg") == "http request" and "status" in entry:
                        last_status = entry["status"]
                        last_ts = entry.get("time")
                        break
                except (json.JSONDecodeError, KeyError):
                    continue
            return last_status, last_ts
        except Exception:
            return None, None


class SyncManager:
    """Synchronize default instance's .env to ~/.claude/settings.json.

    Uses explicit ENV_TO_SETTINGS_MAP — ANTHROPIC_API_KEY → ANTHROPIC_AUTH_TOKEN (rename!).
    """

    SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
    HEIMSENSE_DOT_ENV = CLAUDE_MUX_DIR / ".env"

    def __init__(self, config: ConfigManager):
        self.cm = config

    def sync_default(self, sub_id: str) -> dict:
        """Set sub_id as default and sync .env → settings.json.

        Steps:
        1. Set default_instance in subscriptions.json
        2. Copy instance .env to ~/.claude-mux/.env
        3. Merge relevant keys (only MERGE_KEYS) to ~/.claude/settings.json
        4. Set CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
        5. Set ANTHROPIC_DISABLE_TELEMETRY=true
        """
        sub = self.cm.get_subscription(sub_id)
        if sub is None:
            raise ValueError(f"Subscription {sub_id} not found")

        # 1. Set default in subscriptions.json
        self.cm.set_default(sub_id)

        # 2. Copy instance .env to ~/.claude-mux/.env
        im = InstanceManager(self.cm)
        inst_env = im.generate_env(sub_id)  # ensures .env is fresh
        CLAUDE_MUX_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(inst_env, self.HEIMSENSE_DOT_ENV)

        # 3. Merge to settings.json
        settings = self._load_settings()
        env_block = settings.setdefault("env", {})

        # Read API key — OAuth: from sub["api_key"], gh_token → `gh auth token`, else os.environ
        api_key_env = sub.get("api_key_env", "")
        auth_type = sub.get("auth_type", "bearer")
        api_key = sub.get("api_key", "")
        if not api_key:
            if auth_type == "gh_token":
                try:
                    gh_result = subprocess.run(
                        ["gh", "auth", "token"], capture_output=True, text=True, timeout=10,
                    )
                    api_key = gh_result.stdout.strip() if gh_result.returncode == 0 else os.environ.get(api_key_env, "")
                except Exception:
                    api_key = os.environ.get(api_key_env, "")
            else:
                api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        provider_url = sub.get("provider_url", "")
        port = self.cm.get_instance_port(sub_id) or 0
        listen_addr = f"http://localhost:{port}"
        auth_type = sub.get("auth_type", "bearer")

        # Build settings env block: only keys in MERGE_KEYS
        merged = {}

        if auth_type == "oauth":
            # Claude Max: remove proxy, set OAuth token, let Claude use its own OAuth
            merged["ANTHROPIC_BASE_URL"] = None  # mark for removal
            merged["ANTHROPIC_AUTH_TOKEN"] = None
            if api_key:
                merged["CLAUDE_CODE_OAUTH_TOKEN"] = api_key
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
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_settings(self, settings: dict):
        """Save settings.json atomically. Returns True on success, False on error."""
        try:
            _atomic_write(self.SETTINGS_PATH, settings)
            return True
        except OSError as e:
            log.error("SyncManager: could not write settings.json: %s", e)
            return False


# ═══════════════════════════════════════════════════════════
# Failover Manager
# ═══════════════════════════════════════════════════════════

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

    def __init__(self, config: ConfigManager, sync: "SyncManager"):
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


# ═══════════════════════════════════════════════════════════
# TUI — Textual App
# ═══════════════════════════════════════════════════════════

try:
    from textual.app import App, ComposeResult
    from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen, Screen
    from textual.widgets import Button, DataTable, Footer, Input, Label, ProgressBar, RichLog, Select, Static
    from textual.worker import Worker, WorkerState
    from rich.text import Text
    from rich.markup import escape

    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False


def _status_char(status: str) -> str:
    return {"online": "*", "stopped": "o", "error": "x", "unknown": "?"}.get(status, "?")


def _status_color(status: str) -> str:
    return {"online": "green", "stopped": "gray", "error": "red", "unknown": "yellow"}.get(status, "gray")


def _trunc(s: str, n: int = 40) -> str:
    return s[:n] + "..." if len(s) > n else s


def _format_duration(secs: int) -> str:
    """Format seconds into readable format like '12d 3h' or '45m 22s'."""
    if secs < 0:
        return "0s"
    days, secs = divmod(secs, 86400)
    hours, secs = divmod(secs, 3600)
    mins, secs = divmod(secs, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts[:2])


def _time_ago(iso_ts) -> str:
    """ISO timestamp (str) or Unix epoch (float/int) → '3m ago' or '-'."""
    if not iso_ts:
        return "-"
    try:
        if isinstance(iso_ts, (int, float)):
            t = float(iso_ts)
        else:
            t = datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp()
        secs = int(time.time() - t)
        if secs < 0:
            return "0s"
        return _format_duration(secs) + " ago"
    except (ValueError, TypeError):
        return "-"


# --- Model Test popup ---

class HealthPopup(ModalScreen):
    """Show model response."""

    def __init__(self, label: str, status_code: int, elapsed_ms: int, body: str):
        super().__init__()
        self._label = label
        self._status_code = status_code
        self._elapsed = elapsed_ms
        self._body = body

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold]{self._label}[/bold]  ([dim]{self._elapsed}ms[/dim])\n\n"
            f"{self._body[:500]}"
        )
        yield Button("OK", id="ok", variant="primary")

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss()


# --- Log viewer ---

class LogViewer(Screen):
    """PM2 log viewer — shows out.log and error.log."""

    def __init__(self, pm2_name: str):
        super().__init__()
        self._pm2_name = pm2_name
        self.sub_title = f"Logs: {pm2_name}"

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
        ("b", "dismiss", "Close"),
    ]

    CSS = """
    LogViewer {
        align: center middle;
    }
    #log-container {
        width: 100%;
        height: 85%;
        border: solid $primary;
        padding: 1;
    }
    #log-content {
        width: 100%;
    }
    """

    def action_dismiss(self):
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "close":
            self.dismiss()

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="log-container"):
            yield Static(id="log-content")
        yield Button("Close", id="close", variant="primary")

    def on_mount(self):
        self._load_logs()

    def _load_logs(self):
        content = []
        try:
            sub_name = self._pm2_name.replace("claude-mux-", "", 1)
            inst_dir = CLAUDE_MUX_DIR / "instances" / sub_name
            out_log = inst_dir / "out.log"
            err_log = inst_dir / "error.log"
            wrote = False
            if out_log.exists() and out_log.stat().st_size > 0:
                content.append("── out.log ──")
                for line in out_log.read_text().splitlines()[-100:]:
                    content.append(line)
                wrote = True
            if err_log.exists() and err_log.stat().st_size > 0:
                content.append("")
                content.append("── error.log ──")
                for line in err_log.read_text().splitlines()[-100:]:
                    content.append(line)
                wrote = True
            if not wrote:
                out_log2 = Path.home() / ".pm2" / "logs" / f"{self._pm2_name}-out.log"
                if out_log2.exists() and out_log2.stat().st_size > 0:
                    content.append(f"── {out_log2.name} ──")
                    for line in out_log2.read_text().splitlines()[-100:]:
                        content.append(line)
                    wrote = True
                else:
                    content.append("No log lines found")
                    content.append(f"Searched: {out_log}")
                    content.append(f"and: {out_log2}")
        except Exception as e:
            content.append(f"Error loading: {e}")
        text = "\n".join(content) if content else "(empty log)"
        self.query_one("#log-content", Static).update(text)


# --- Failover log modal ---

class FailoverLogModal(ModalScreen):
    """Show ~/.claude-mux/failover.log in a modal."""

    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]

    def __init__(self, log_path):
        super().__init__()
        self._log_path = log_path

    def compose(self) -> ComposeResult:
        yield Static("[bold]Failover Log[/bold]", id="fl-title")
        with VerticalScroll(id="fl-scroll"):
            yield Static(id="fl-content")
        yield Button("Close (q/ESC)", id="fl-close", variant="primary")

    def on_mount(self):
        try:
            if self._log_path.exists():
                lines = self._log_path.read_text().splitlines()
                # Show newest first
                text = "\n".join(reversed(lines[-200:]))
                self.query_one("#fl-content", Static).update(text or "(empty log)")
            else:
                self.query_one("#fl-content", Static).update(
                    f"[dim]No failover events yet.\nLog file: {self._log_path}[/dim]"
                )
        except OSError as e:
            self.query_one("#fl-content", Static).update(f"[red]Error reading: {e}[/red]")

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss()


# --- Confirm dialogs ---

class ConfirmModal(ModalScreen):
    """Confirmation dialog. dismiss(bool) — True = yes/ok."""

    def __init__(self, title: str, message: str):
        super().__init__()
        self._title = title
        self._message = message

    def compose(self) -> ComposeResult:
        yield Static(f"[bold]{self._title}[/bold]\n\n{self._message}")
        with Horizontal():
            yield Button("Yes", id="yes", variant="error")
            yield Button("No", id="no", variant="primary")

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(event.button.id == "yes")

    def on_key(self, event):
        if event.key in ("j", "y", "enter"):
            self.dismiss(True)
            event.stop()
        elif event.key in ("n", "q", "escape", "b"):
            self.dismiss(False)
            event.stop()


# --- Help modal ---

class HelpModal(ModalScreen):
    """Show all keybindings — press h or ? to open."""

    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close"), ("h", "dismiss", "Close")]

    HELP_TEXT = """\
[bold]Heimsense TUI — Keyboard Shortcuts[/bold]
[dim]Optimized for iPhone SSH (no Ctrl/F/arrow/Tab/ESC required)[/dim]

[bold yellow]Navigation[/bold yellow]
  j / n / ↓   Move down  (iPhone: j or n)
  k / p / ↑   Move up    (iPhone: k or p)
  1-9          Jump directly to row N
  h / ?        Show this help
  q            Quit / Close modal

[bold yellow]Provider management[/bold yellow]
  +         Add new subscription (wizard)
  Enter     Activate selected subscription
  e         Edit (model maps / fields)
  d         Delete subscription (confirm: j=yes, n=no)
  R         Reauth (renew OAuth token)

[bold yellow]Proxy providers[/bold yellow]
  s         Start / Stop (toggle)
  t         Test HTTP endpoint
  l         Show PM2 logs (close: q/b)
  f         Force model (route all → one)

[bold yellow]System[/bold yellow]
  r         Refresh table and details
  z         Reload TUI (hotload)
  x         Failover check (manual)
  L         Show failover log

[bold yellow]Provider Select (wizard)[/bold yellow]
  1-8       Select directly with number
  j/k/n/p   Navigate up/down
  Enter     Confirm selection
  q / b     Cancel / Back

[bold yellow]Confirmation dialogs[/bold yellow]
  j / Enter   Confirm (Yes)
  n / q / b   Cancel (No)
"""

    def compose(self) -> ComposeResult:
        yield Static(self.HELP_TEXT, id="help-text")
        yield Button("Close (q/ESC)", id="close-help", variant="primary")

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss()

    def on_key(self, event):
        if event.key in ("escape", "q", "h", "?"):
            self.dismiss()
            event.stop()


# --- Hotload countdown modal ---

class HotloadModal(ModalScreen):
    """Shows countdown progress bar — ESC or Cancel aborts hotload."""

    COUNTDOWN = 2  # sekunder

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[bold]Hotload[/bold]\nUpdate detected — reloading shortly...", id="hl-msg")
            yield ProgressBar(total=self.COUNTDOWN * 10, show_eta=False, id="hl-bar")
            yield Button("Cancel (ESC)", id="hl-cancel", variant="warning")

    def on_mount(self):
        self._ticks = 0
        self.set_interval(0.1, self._tick)

    def _tick(self):
        self._ticks += 1
        try:
            self.query_one("#hl-bar", ProgressBar).advance(1)
        except Exception:
            pass
        if self._ticks >= self.COUNTDOWN * 10:
            self.dismiss(True)  # True = run hotload

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(False)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(False)


# --- Force model modal ---

class ForceModelModal(ModalScreen):
    """Select force model for all model aliases (haiku/sonnet/opus → same model)."""

    def __init__(self, current_model: str, available_models: list[str]):
        super().__init__()
        self._current = current_model
        self._available = available_models

    def compose(self) -> ComposeResult:
        options = [("No force (remove)", "__none__")] + [(m, m) for m in self._available]
        yield Static("[bold]Force Model[/bold]\n\nAll model aliases point to selected model.\nSelect 'No force' to remove.", id="fm-title")
        yield Select(options, id="fm-select", prompt="Select model...", allow_blank=False)
        with Horizontal():
            yield Button("OK", id="ok", variant="primary")
            yield Button("Cancel", id="cancel")

    def on_mount(self):
        sel = self.query_one("#fm-select", Select)
        if self._current in [m for _, m in [("No force", "__none__")] + [(m, m) for m in self._available]]:
            try:
                sel.value = self._current
            except Exception:
                pass

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "ok":
            sel = self.query_one("#fm-select", Select)
            self.dismiss(sel.value if sel.value != Select.BLANK else None)
        else:
            self.dismiss(None)

    def on_key(self, event):
        if event.key in ("q", "escape", "b"):
            self.dismiss(None)
            event.stop()
        elif event.key == "enter":
            sel = self.query_one("#fm-select", Select)
            self.dismiss(sel.value if sel.value != Select.BLANK else None)
            event.stop()


# --- Provider selection screen ---

class ProviderSelectScreen(ModalScreen):
    """Full-screen provider selection with digit, arrow keys, and highlight."""
    def __init__(self, presets: dict, index_map: dict[int, str]):
        super().__init__()
        self._presets = presets
        self._index_map = index_map
        self._keys = list(presets)
        self._highlighted = -1  # -1 = none
        self._dismissed = False

    def compose(self):
        yield Static("[bold]Select Provider[/bold]", id="ps-title")
        lines = []
        for i, key in enumerate(self._presets, start=1):
            p = self._presets[key]
            lines.append(f"   {i}. {p['label']}")
        yield Static("\n".join(lines), id="ps-list")
        yield Static("[dim]Number 1-8 to select  ·  j/k or ↑/↓ to navigate  ·  Enter to confirm  ·  Esc/q to cancel[/dim]", id="ps-hint")

    def on_key(self, event):
        if self._dismissed:
            event.stop()
            return
        if event.key in ("escape", "q"):
            self._dismissed = True
            self.dismiss(None)
            event.stop()
        elif event.key in ("up", "k", "p"):
            self._highlighted = (len(self._keys) - 1) if self._highlighted <= 0 else (self._highlighted - 1)
            self._render_highlight()
            event.stop()
        elif event.key in ("down", "j", "n"):
            self._highlighted = 0 if (self._highlighted < 0 or self._highlighted >= len(self._keys) - 1) else (self._highlighted + 1)
            self._render_highlight()
            event.stop()
        elif event.key == "enter":
            if 0 <= self._highlighted < len(self._keys):
                self._dismissed = True
                self.dismiss(self._keys[self._highlighted])
            event.stop()
        elif event.key.isdigit():
            num = int(event.key)
            if num in self._index_map:
                idx = self._keys.index(self._index_map[num])
                self._highlighted = idx
                self._render_highlight()
                self._dismissed = True
                # Wrap in def so return value is None — otherwise Textual awaits AwaitComplete and crashes
                key_val = self._keys[idx]
                def _dismiss_after_highlight(k=key_val):
                    self.dismiss(k)
                self.set_timer(0.3, _dismiss_after_highlight)
            event.stop()

    def _safe_dismiss(self, key):
        self.dismiss(key)

    def _render_highlight(self):
        lines = []
        for i, key in enumerate(self._presets, start=1):
            p = self._presets[key]
            if i - 1 == self._highlighted:
                lines.append(f"  [reverse]  {i}. {p['label']}  [/reverse]")
            else:
                lines.append(f"   {i}. {p['label']}")
        self.query_one("#ps-list", Static).update("\n".join(lines))


class AddWizard(ModalScreen):
    """Multi-step wizard for adding/editing subscriptions."""

    CSS = """
    .oauth-url {
        border: solid $accent;
        padding: 1;
        margin: 1 0;
        max-height: 5;
        overflow-x: auto;
        overflow-y: hidden;
        width: 100%;
    }
    """

    def __init__(self, config: ConfigManager, existing_sub: dict | None = None, reauth: bool = False):
        super().__init__()
        self.cm = config
        self._step = 1
        self._provider_presets = PROVIDER_PRESETS
        self._data: dict = {}
        self._existing = existing_sub
        self._edit_mode = existing_sub is not None
        self._reauth = reauth
        self._copilot_models: list[dict] = []
        self._copilot_fetch_done = False
        self._oauth_state: dict = {}
        self._selected_provider: str | None = None
        self._provider_index_map: dict[int, str] = {}

    def compose(self) -> ComposeResult:
        title = "[bold]Edit Subscription[/bold]" if self._edit_mode else "[bold]Add Subscription[/bold]"
        yield Static(title, id="wiz-title")
        # Step 1: Name only
        with Vertical(id="step1"):
            yield Label("Name:")
            yield Input(placeholder="e.g. my-deepseek", id="wiz-name")
            yield Static("", id="wiz-name-hint")
            with Horizontal():
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("Next →", id="next-name", variant="primary", disabled=True)
        # Step 2: Provider list (numbered)
        with Vertical(id="step2", classes="hidden"):
            yield Static("Select provider (type 1-8):", id="wiz-provider-prompt")
            yield Static("", id="wiz-provider-list")
            with Horizontal():
                yield Button("← Back", id="back-provider", variant="default")
        # Step 3: API Key + URL (hidden for OAuth)
        with Vertical(id="step3", classes="hidden"):
            yield Label("API Key (or env var name):", id="wiz-key-label")
            yield Input(placeholder="e.g. MY_API_KEY or sk-...", id="wiz-key", password=True)
            yield Label("Base URL (auto-filled for presets):", id="wiz-url-label")
            yield Input(placeholder="https://api.example.com/v1", id="wiz-url")
            with Horizontal():
                yield Button("← Back", id="back-key", variant="default")
                yield Button("Next →", id="next-key", variant="primary")
        # Step 4: Model maps (skipped for OAuth)
        with Vertical(id="step4", classes="hidden"):
            yield Static("", id="wiz-models-status")
            yield Label("Model Maps:")
            yield Label("Haiku:")
            yield Input(placeholder="model name", id="wiz-haiku")
            yield Select([], id="wiz-haiku-sel", prompt="Select model...", classes="hidden")
            yield Label("Sonnet:")
            yield Input(placeholder="model name", id="wiz-sonnet")
            yield Select([], id="wiz-sonnet-sel", prompt="Select model...", classes="hidden")
            yield Label("Opus:")
            yield Input(placeholder="model name", id="wiz-opus")
            yield Select([], id="wiz-opus-sel", prompt="Select model...", classes="hidden")
            yield Label("Notes (optional):")
            yield Input(placeholder="notes", id="wiz-notes")
            with Horizontal():
                yield Button("← Back", id="back-models", variant="default")
                btn_label = "Save" if self._edit_mode else "Create"
                yield Button(Text.from_markup(f"[bold yellow]{btn_label[0]}[/bold yellow]{btn_label[1:]}"), id="create", variant="primary")
        # Step 5: OAuth (only for Claude Max)
        with Vertical(id="step5", classes="hidden"):
            yield Static("", id="oauth-info")
            with Horizontal(id="oauth-url-row"):
                yield Button("Authenticate in browser", id="oauth-open-url", variant="primary")
            yield Static("", id="oauth-status")
            yield Input(placeholder="paste code here", id="wiz-oauth-code", classes="hidden")
            with Horizontal(id="oauth-nav-row"):
                yield Button("Back", id="back-oauth", variant="default")
                yield Button("Next", id="oauth-next", variant="primary", disabled=True)
            yield Static("", id="oauth-result", classes="hidden")
            yield Button("Close", id="oauth-close", classes="hidden")

    def on_mount(self):
        # Hide all steps except step1
        for s in ("step2", "step3", "step4", "step5"):
            self.query_one(f"#{s}", Vertical).display = False
        # Build provider index map
        self._provider_index_map = {}
        for i, key in enumerate(PROVIDER_PRESETS, start=1):
            self._provider_index_map[i] = key
        # If edit-mode: pre-fill fields
        if self._edit_mode and self._existing:
            sub = self._existing
            self.query_one("#wiz-name", Input).value = sub.get("name", "")
            self.query_one("#wiz-url", Input).value = sub.get("provider_url", "")
            self.query_one("#wiz-key", Input).value = sub.get("api_key_env", "")
            models = sub.get("model_maps", {})
            self.query_one("#wiz-haiku", Input).value = models.get("haiku", "")
            self.query_one("#wiz-sonnet", Input).value = models.get("sonnet", "")
            self.query_one("#wiz-opus", Input).value = models.get("opus", "")
            self.query_one("#wiz-notes", Input).value = sub.get("notes", "")
            self._validate_step1()
            # Reauth: jump directly to OAuth flow
            if self._reauth and sub.get("auth_type") == "oauth":
                self._data["name"] = sub.get("name", "")
                self._data["auth_type"] = "oauth"
                self._data["provider_key"] = sub.get("provider_key", "")
                self._data["provider_url"] = sub.get("provider_url", "")
                self.query_one("#wiz-title", Static).update("[bold]Reauthenticate Claude Max[/bold]")
                self._start_oauth_flow(self._data["name"])

    def _show_side(self, n: int):
        for s in ("step1", "step2", "step3", "step4", "step5"):
            self.query_one(f"#{s}", Vertical).display = False
        self.query_one(f"#step{n}", Vertical).display = True
        self._step = n

    def on_input_submitted(self, event: Input.Submitted):
        """Enter in Input → go to next step or confirm."""
        eid = event.input.id
        if eid == "wiz-name":
            if self.query_one("#wiz-name", Input).value.strip():
                # Edit-mode OAuth: jump directly to models
                if self._edit_mode and self._existing and self._existing.get("auth_type") == "oauth":
                    self._data["name"] = self.query_one("#wiz-name", Input).value.strip()
                    self._data["auth_type"] = "oauth"
                    self._data["provider_key"] = self._existing.get("provider_key", "")
                    self._skip_to_models_edit()
                else:
                    self._go_to_providers()
        elif eid == "wiz-key":
            # Step 3 → if OAuth skip, otherwise default
            pass
        elif eid == "wiz-oauth-code":
            self._submit_oauth_code()

    def on_input_changed(self, event: Input.Changed):
        eid = event.input.id
        if eid == "wiz-name":
            self._validate_step1()
        elif eid == "wiz-key":
            self._validate_step3()
        elif eid == "wiz-url":
            self._validate_step3()
        elif eid == "wiz-oauth-code":
            has_code = bool(event.input.value.strip())
            self.query_one("#oauth-next", Button).disabled = not has_code

    def _validate_step1(self):
        """Step 1: name + provider selected (provider selected on step 2)."""
        name_ok = bool(self.query_one("#wiz-name", Input).value.strip())
        self.query_one("#next-name", Button).disabled = not name_ok
        if name_ok:
            self.query_one("#wiz-name-hint", Static).update("[dim]Press Enter to continue[/dim]")
        else:
            self.query_one("#wiz-name-hint", Static).update("")

    def _validate_step3(self):
        """Step 3: key+url optional for OAuth, required for others."""
        is_oauth = bool(self._selected_provider and PROVIDER_PRESETS.get(self._selected_provider, {}).get("auth_type") == "oauth")
        if is_oauth:
            self.query_one("#next-key", Button).disabled = False
        else:
            key_ok = bool(self.query_one("#wiz-key", Input).value.strip())
            url_ok = bool(self.query_one("#wiz-url", Input).value.strip())
            self.query_one("#next-key", Button).disabled = not (key_ok and url_ok)

    def on_button_pressed(self, event: Button.Pressed):
        eid = event.button.id
        if eid == "cancel":
            self.dismiss()
        elif eid == "next-name":
            if self._edit_mode and self._existing and self._existing.get("auth_type") == "oauth":
                self._data["name"] = self.query_one("#wiz-name", Input).value.strip()
                self._data["auth_type"] = "oauth"
                self._data["provider_key"] = self._existing.get("provider_key", "")
                self._skip_to_models_edit()
            else:
                self._go_to_providers()
        elif eid == "back-key":
            self._show_side(1)
            self.query_one("#wiz-title", Static).update("[bold]Add Subscription[/bold]")
            self.query_one("#wiz-name", Input).disabled = False
            self.query_one("#wiz-name", Input).disabled = False
            self.query_one("#wiz-name", Input).focus()
        elif eid == "next-key":
            self._go_to_models()
        elif eid == "back-key":
            self._show_side(2)
            self.query_one("#wiz-title", Static).update("[bold]Select Provider[/bold]")
            self._render_provider_list()
        elif eid == "back-models":
            self._show_side(3)
            self.query_one("#wiz-title", Static).update("[bold]API Key[/bold]")
        elif eid == "back-oauth":
            self._show_side(1)
            self.query_one("#wiz-title", Static).update("[bold]Add Subscription[/bold]")
            self.query_one("#wiz-name", Input).disabled = False
            self.query_one("#wiz-name", Input).focus()
            self._oauth_cleanup()
        elif eid == "oauth-open-url":
            self._open_oauth_url()
        elif eid == "oauth-next":
            self._submit_oauth_code()
        elif eid == "oauth-close":
            sub_id = self._oauth_state.get("_sub_id")
            name = self._data.get("name", "")
            self.dismiss({"id": sub_id, "name": name, "updated": True})
        elif eid == "create":
            self._do_create()

    def on_key(self, event):
        if event.key == "escape":
            self._oauth_cleanup()
            self.dismiss()
            event.stop()

    def _skip_to_models_edit(self):
        """Edit-mode OAuth: jump to models (step 4)."""
        self._data["api_key"] = self._existing.get("api_key", "") if self._existing else ""
        self._show_side(4)
        self.query_one("#wiz-title", Static).update("[bold]Edit Model Maps[/bold]")
        self.query_one("#wiz-key-label", Label).display = False
        self.query_one("#wiz-key", Input).display = False
        self.query_one("#wiz-url-label", Label).display = False
        self.query_one("#wiz-url", Input).display = False

    def _go_to_providers(self):
        """From step 1 → push ProviderSelectScreen."""
        self._data["name"] = self.query_one("#wiz-name", Input).value.strip()
        self._data["auth_type"] = "bearer"  # default
        self.query_one("#wiz-name", Input).disabled = True

        def _on_done(provider_key):
            self.query_one("#wiz-name", Input).disabled = False
            if not provider_key:
                return  # canceled
            self._selected_provider = provider_key
            preset = PROVIDER_PRESETS[provider_key]
            self.query_one("#wiz-key", Input).value = preset["api_key_env"]
            self.query_one("#wiz-url", Input).value = preset["provider_url"]
            models = preset.get("model_maps", {})
            self.query_one("#wiz-haiku", Input).value = models.get("haiku", "")
            self.query_one("#wiz-sonnet", Input).value = models.get("sonnet", "")
            self.query_one("#wiz-opus", Input).value = models.get("opus", "")
            self._data["auth_type"] = preset.get("auth_type", "bearer")
            self._data["provider_url"] = preset.get("provider_url", "")
            self._data["provider_key"] = provider_key
            if provider_key == "copilot":
                self._start_copilot_fetch()
            is_oauth = preset.get("auth_type") == "oauth"
            if is_oauth:
                self._start_oauth_flow(self._data["name"])
            else:
                self._show_side(3)
                self.query_one("#wiz-title", Static).update("[bold]API Key[/bold]")
                self.query_one("#wiz-key", Input).focus()
                self._validate_step3()

        self.app.push_screen(ProviderSelectScreen(self._provider_presets, self._provider_index_map), _on_done)

    def _go_to_models(self):
        """From step 3 → step 4 (models)."""
        self._data["api_key"] = self.query_one("#wiz-key", Input).value.strip()
        self._data["provider_url"] = self.query_one("#wiz-url", Input).value.strip()
        self._show_side(4)
        self.query_one("#wiz-title", Static).update("[bold]Model Maps[/bold]")
        # Copilot: show Select if models are ready
        if self._selected_provider == "copilot":
            self._apply_copilot_model_selects()
            if not self._copilot_fetch_done:
                self.query_one("#wiz-models-status", Static).update("[yellow]Fetching models from Copilot...[/yellow]")
                self.set_interval(0.5, self._poll_copilot_models)
        self.query_one("#create", Button).focus()

    def _do_create(self):
        """Create or update subscription."""
        name = self._data.get("name", "")
        provider_url = self._data.get("provider_url", "")
        api_key = self._data.get("api_key", "")
        auth_type = self._data.get("auth_type", "bearer")
        model_maps = {
            "haiku": self._get_model_value("haiku"),
            "sonnet": self._get_model_value("sonnet"),
            "opus": self._get_model_value("opus"),
        }
        notes = self.query_one("#wiz-notes", Input).value.strip().replace("[", "\\[").replace("]", "\\]")
        if self._edit_mode and self._existing:
            sub_id = self._existing["id"]
            api_key_env = api_key if (api_key.isupper() and "_" in api_key) else self._existing.get("api_key_env", api_key)
            # Preserve api_key on edit (OAuth token)
            kwargs = dict(
                name=name, provider_url=provider_url,
                api_key_env=api_key_env, auth_type=auth_type,
                model_maps=model_maps, notes=notes,
            )
            if auth_type == "oauth" and api_key:
                kwargs["api_key"] = api_key
            self.cm.update_subscription(sub_id, **kwargs)
            is_env_ref = api_key.isupper() and "_" in api_key
            if not is_env_ref and api_key:
                os.environ[api_key_env] = api_key
                inst_dir = CLAUDE_MUX_DIR / "instances" / name
                inst_dir.mkdir(parents=True, exist_ok=True)
                env_path = inst_dir / ".env"
                env_text = env_path.read_text() if env_path.exists() else ""
                env_lines = [l for l in env_text.splitlines() if not l.startswith(f"{api_key_env}=")]
                env_lines.append(f"{api_key_env}={api_key}")
                env_path.write_text("\n".join(env_lines) + "\n")
                env_path.chmod(0o600)
            self.dismiss({"id": sub_id, "name": name, "updated": True})
        else:
            is_env_ref = api_key.isupper() and "_" in api_key
            api_key_env = api_key if is_env_ref else f"{name.upper()}_API_KEY".replace("-", "_")
            sub = self.cm.add_subscription(
                name=name, provider_url=provider_url,
                api_key_env=api_key_env, auth_type=auth_type,
                model_maps=model_maps, notes=notes,
            )
            self.dismiss({"id": sub["id"], "name": sub["name"], "updated": False})
            if not is_env_ref and api_key:
                os.environ[api_key_env] = api_key
                inst_dir = CLAUDE_MUX_DIR / "instances" / name
                inst_dir.mkdir(parents=True, exist_ok=True)
                env_path = inst_dir / ".env"
                env_text = env_path.read_text() if env_path.exists() else ""
                env_lines = [l for l in env_text.splitlines() if not l.startswith(f"{api_key_env}=")]
                env_lines.append(f"{api_key_env}={api_key}")
                env_path.write_text("\n".join(env_lines) + "\n")
                env_path.chmod(0o600)

    # --- Copilot model fetch ---

    def _start_copilot_fetch(self):
        """Start background thread to fetch Copilot models."""
        self._copilot_fetch_done = False
        def _fetch():
            try:
                result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
                token = result.stdout.strip() if result.returncode == 0 else ""
                if token:
                    self._copilot_models = fetch_copilot_models(token)
                else:
                    self._copilot_models = []
            except Exception:
                self._copilot_models = []
            self._copilot_fetch_done = True
        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_copilot_model_selects(self):
        """Switch haiku/sonnet/opus to Select dropdowns if models are ready."""
        if not self._copilot_models:
            return
        options = [(f"{m['name']} ({m['vendor']})", m["id"]) for m in self._copilot_models]
        preset_maps = PROVIDER_PRESETS["copilot"]["model_maps"]
        for alias, sel_id, inp_id in [
            ("haiku", "wiz-haiku-sel", "wiz-haiku"),
            ("sonnet", "wiz-sonnet-sel", "wiz-sonnet"),
            ("opus", "wiz-opus-sel", "wiz-opus"),
        ]:
            sel = self.query_one(f"#{sel_id}", Select)
            inp = self.query_one(f"#{inp_id}", Input)
            sel.set_options(options)
            current = inp.value.strip() or preset_maps.get(alias, "")
            try:
                sel.value = current
            except Exception:
                pass
            sel.display = True
            inp.display = False
        self.query_one("#wiz-models-status", Static).update(
            f"[green]{len(self._copilot_models)} models available[/green]"
        )

    def _poll_copilot_models(self):
        """Poll until fetch is done, then update UI and stop."""
        if self._copilot_fetch_done:
            self._apply_copilot_model_selects()
            try:
                for timer in self._timers:
                    timer.stop()
            except Exception:
                pass

    # --- OAuth flow (Claude Max) ---

    def _start_oauth_flow(self, name: str):
        safe_name = name.lower().replace(" ", "-")
        session_name = f"claude-oauth-{safe_name}"
        self._oauth_state = {"session": session_name, "token": None, "step": "starting"}
        log.info("OAuth: kill-session %s", session_name)
        subprocess.run(["tmux", "kill-session", "-t", session_name],
                       capture_output=True, text=True)
        # Delete old log file so we don't reuse stale URL/token
        old_log = f"/tmp/oauth-{safe_name}.log"
        try:
            os.remove(old_log)
            log.info("OAuth: removed stale log %s", old_log)
        except OSError:
            pass
        log.info("OAuth: new-session %s", session_name)
        proc = subprocess.Popen(
            ["tmux", "new-session", "-d", "-s", session_name, "-x", "220",
             f"env -u ANTHROPIC_BASE_URL -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u CLAUDE_CODE_OAUTH_TOKEN BROWSER=/bin/false claude setup-token 2>&1 | tee /tmp/oauth-{safe_name}.log; tmux wait-for -S oauth-done"],
        )
        log.info("OAuth: new-session started (async) PID=%s", proc.pid)
        # Verify session exists
        r = subprocess.run(["tmux", "has-session", "-t", session_name], capture_output=True, text=True)
        log.info("OAuth: has-session check: ret=%s err=%s", r.returncode, r.stderr.strip())
        self._show_side(5)
        self.query_one("#wiz-title", Static).update("[bold]Claude Max OAuth Setup[/bold]")
        self.query_one("#oauth-info", Static).update("[yellow]Starting OAuth flow...[/yellow]")
        self.query_one("#oauth-status", Static).update("")
        self.query_one("#oauth-url-row", Horizontal).display = False
        self.query_one("#oauth-open-url", Button).display = False
        self.query_one("#wiz-oauth-code", Input).display = False
        self.query_one("#oauth-nav-row", Horizontal).display = False
        self.query_one("#oauth-result", Static).display = False
        self.query_one("#oauth-close", Button).display = False
        self._oauth_poll_url()

    def _oauth_poll_url(self):
        try:
            # Read URL from log file (avoids tmux wrapping truncation)
            safe_name = self._oauth_state.get("session", "").replace("claude-oauth-", "")
            log_path = f"/tmp/oauth-{safe_name}.log"
            log.info("OAuth poll: checking %s", log_path)
            full_url = None
            try:
                with open(log_path) as f:
                    for line in f:
                        if "https://claude.com/cai/oauth/authorize" in line:
                            # Strip ANSI escapes: OSC-8 hyperlinks, CSI codes, BEL characters
                            full_url = re.sub(r'\x1b\[[?\s>][0-9;]*[a-zA-Z]|\x1b\[[0-9;]*[a-zA-Z]|\x1b\][0-9;]*[^\x1b]*(?:\x1b\\|[\x07])', '', line).strip()
                            log.info("OAuth poll: found URL in log (len=%s)", len(full_url))
            except FileNotFoundError:
                log.info("OAuth poll: log file not found yet")
            except OSError as e:
                log.info("OAuth poll: log error %s", e)
            # Fallback: tmux capture-pane

            if not full_url:
                self.query_one("#oauth-info", Static).update("[yellow]⏳ Waiting for authorization URL...[/yellow]")
                output = subprocess.run(
                    ["tmux", "capture-pane", "-S", "-500", "-J", "-t", self._oauth_state["session"], "-p"],
                    capture_output=True, text=True, timeout=5,
                ).stdout
                lines = output.splitlines()
                for i, line in enumerate(lines):
                    if "https://claude.com/cai/oauth/authorize" in line:
                        parts = [line.strip()]
                        for j in range(i + 1, min(i + 10, len(lines))):
                            nl = lines[j].strip()
                            if not nl or "http" in nl:
                                break
                            parts.append(nl)
                        raw = "".join(parts)
                        # Strip ANSI escapes
                        full_url = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][0-9;]*[^\x1b]*(?:\x1b\\|[\x07])', '', raw)
            if full_url:
                self._oauth_state["step"] = "awaiting_code"
                # Stop URL poll timer if running
                turl = self._oauth_state.get("_timer_url")
                if turl:
                    try:
                        turl.stop()
                    except Exception:
                        pass
                    self._oauth_state["_timer_url"] = None
                self.query_one("#oauth-info", Static).update(
                    "[bold]Authenticate with Claude Max[/bold]\n\n"
                    "Click the button below to open the authorization page in your browser.\n"
                )
                self._oauth_state["url"] = full_url
                self.query_one("#oauth-url-row", Horizontal).display = True
                self.query_one("#oauth-open-url", Button).display = True
                self.query_one("#oauth-open-url", Button).focus()
                self.query_one("#oauth-nav-row", Horizontal).display = True
                self.query_one("#wiz-oauth-code", Input).display = True
                self.query_one("#back-oauth", Button).display = True
                return
        except Exception as e:
            log.info("OAuth poll url: exception: %s", e, exc_info=True)
        if self._oauth_state.get("step") == "starting" and not self._oauth_state.get("_timer_url"):
            t = self.set_interval(0.5, self._oauth_poll_url)
            self._oauth_state["_timer_url"] = t

    def _submit_oauth_code(self):
        code = self.query_one("#wiz-oauth-code", Input).value.strip()
        log.info("OAuth: submit code len=%s step=%s", len(code), self._oauth_state.get("step"))
        if not code:
            return
        # Take only first line (remove any line breaks)
        code = code.splitlines()[0].strip()
        self._oauth_state["step"] = "submitting"
        self.query_one("#oauth-info", Static).update("[yellow]⏳ Exchanging authorization code for token...[/yellow]")
        self.query_one("#oauth-url-row", Horizontal).display = False
        self.query_one("#wiz-oauth-code", Input).display = False
        self.query_one("#oauth-nav-row", Horizontal).display = False
        self.query_one("#oauth-status", Static).update("")
        session = self._oauth_state["session"]
        log.info("OAuth: send-keys to %s", session)
        subprocess.run(["tmux", "send-keys", "-t", session, code, "Enter"], timeout=5)
        time.sleep(1)
        subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], timeout=5)
        log.info("OAuth: keys sent, polling token")
        self._oauth_poll_token()

    def _oauth_poll_token(self):
        try:
            # Save safe_name BEFORE cleanup can clear session
            safe_name = self._oauth_state.get("session", "").replace("claude-oauth-", "")
            log_path = f"/tmp/oauth-{safe_name}.log" if safe_name else None
            token = None
            try:
                with open(log_path) as f:
                    for line in f:
                        # "export CLAUDE_CODE_OAUTH_TOKEN=<token>" er placeholder — ignorer
                        if "export CLAUDE_CODE_OAUTH_TOKEN=" in line and "sk-" not in line:
                            continue
                        if "export CLAUDE_CODE_OAUTH_TOKEN=" in line:
                            token = line.split("export CLAUDE_CODE_OAUTH_TOKEN=", 1)[1].strip()
                            break
                        # "Your OAuth token (valid for 1 year):" second line after has token
                        if "Your OAuth token" in line:
                            try:
                                next(f, "")  # skip blank line
                                tok = next(f, "").strip()
                                if tok.startswith("sk-"):
                                    token = tok
                            except StopIteration:
                                pass
            except OSError:
                pass
            if token:
                log.info("OAuth poll token: FOUND token len=%d", len(token))
                self._oauth_state["token"] = token
                # Stop token poll timer
                ttok = self._oauth_state.get("_timer_token")
                if ttok:
                    try:
                        ttok.stop()
                    except Exception:
                        pass
                    self._oauth_state["_timer_token"] = None
                self.query_one("#oauth-info", Static).update(
                    "[yellow]⏳ Saving subscription...[/yellow]"
                )
                self._oauth_cleanup()
                self._oauth_finish()
                return
        except Exception as e:
            log.info("OAuth poll token: exception: %s", e)
        if not log_path:
            log.info("OAuth poll token: no log path (cleaned up), stopping")
            return
        count = self._oauth_state.get("poll_count", 0) + 1
        self._oauth_state["poll_count"] = count
        log.info("OAuth poll token: attempt %d from log %s", count, log_path)
        # Show countdown every 5th attempt (2.5s interval)
        if count % 5 == 0:
            secs_left = max(0, 30 - count // 2)
            self.query_one("#oauth-status", Static).update(
                f"[yellow]⏳ Waiting for token... ({secs_left}s remaining)[/yellow]"
            )
        # Check if tmux session is still running (early exit on crash)
        session = self._oauth_state.get("session", "")
        if session and count > 4:
            chk = subprocess.run(
                ["tmux", "has-session", "-t", session],
                capture_output=True,
            )
            if chk.returncode != 0:
                # Session is dead — no token will come
                log.warning("OAuth poll: tmux session %s is gone (exit %d)", session, chk.returncode)
                ttok = self._oauth_state.get("_timer_token")
                if ttok:
                    try:
                        ttok.stop()
                    except Exception:
                        pass
                    self._oauth_state["_timer_token"] = None
                self._oauth_state["step"] = "failed"
                self.query_one("#oauth-info", Static).update(
                    "[red]⚠ OAuth session ended without token[/red]\n\n"
                    "The process stopped before a token was generated.\n"
                    "Try again — click 'Open browser' below."
                )
                self.query_one("#oauth-url-row", Horizontal).display = True
                self.query_one("#wiz-oauth-code", Input).value = ""
                self.query_one("#wiz-oauth-code", Input).display = True
                self.query_one("#oauth-nav-row", Horizontal).display = True
                self.query_one("#wiz-oauth-code", Input).focus()
                return

        if count > 60:
            # Stop token poll timer
            ttok = self._oauth_state.get("_timer_token")
            if ttok:
                try:
                    ttok.stop()
                except Exception:
                    pass
                self._oauth_state["_timer_token"] = None
            self._oauth_cleanup()
            self._oauth_state["step"] = "failed"
            self.query_one("#oauth-info", Static).update(
                "[red]⚠ Authorization failed[/red]\n\n"
                "No token was received. This may be because the code was invalid or expired.\n\n"
                "Click the button below to start over."
            )
            self.query_one("#oauth-url-row", Horizontal).display = True
            self.query_one("#wiz-oauth-code", Input).value = ""
            self.query_one("#wiz-oauth-code", Input).display = True
            self.query_one("#oauth-nav-row", Horizontal).display = True
            self.query_one("#wiz-oauth-code", Input).focus()
            return
        if not self._oauth_state.get("_timer_token"):
            t = self.set_interval(0.5, self._oauth_poll_token)
            self._oauth_state["_timer_token"] = t

    def _oauth_cleanup(self):
        for tkey in ("_timer_url", "_timer_token"):
            t = self._oauth_state.get(tkey)
            if t:
                try:
                    t.stop()
                except Exception:
                    pass
        session = self._oauth_state.get("session", "")
        if session:
            subprocess.run(["tmux", "kill-session", "-t", session],
                           capture_output=True, text=True)
            self._oauth_state["session"] = ""

    def _open_oauth_url(self):
        url = self._get_oauth_url()
        if url:
            import webbrowser
            webbrowser.open(url)
        self._oauth_focus_paste_delayed()

    def _oauth_focus_paste_delayed(self):
        def _do_focus():
            self.query_one("#wiz-oauth-code", Input).focus()
        self.set_timer(5, _do_focus)

    def _get_oauth_url(self) -> str:
        return self._oauth_state.get("url", "")

    def _oauth_finish(self):
        name = self._data["name"]
        api_key = self._oauth_state["token"]
        log.info("_oauth_finish: name=%s api_key_len=%d edit=%s", name, len(api_key) if api_key else 0, self._edit_mode)
        provider_url = self._data.get("provider_url", "")
        auth_type = self._data.get("auth_type", "bearer")
        model_maps = {
            "haiku": self._get_model_value("haiku") or "claude-sonnet-4-5",
            "sonnet": self._get_model_value("sonnet") or "claude-sonnet-4-6",
            "opus": self._get_model_value("opus") or "claude-opus-4-7",
        }
        notes = self.query_one("#wiz-notes", Input).value.strip().replace("[", "\\[").replace("]", "\\]")
        api_key_env = "CLAUDE_CODE_OAUTH_TOKEN"
        if self._edit_mode and self._existing:
            sub_id = self._existing["id"]
            self.cm.update_subscription(
                sub_id, name=name, provider_url=provider_url,
                api_key_env=api_key_env, auth_type=auth_type,
                model_maps=model_maps, notes=notes,
                api_key=api_key,
            )
            log.info("_oauth_finish: updated sub %s api_key=%s", sub_id, api_key[:10] if api_key else "NONE")
        else:
            sub = self.cm.add_subscription(
                name=name, provider_url=provider_url,
                api_key_env=api_key_env, auth_type=auth_type,
                model_maps=model_maps, notes=notes,
            )
            self.cm.update_subscription(sub["id"], api_key=api_key)
        os.environ[api_key_env] = api_key
        inst_dir = CLAUDE_MUX_DIR / "instances" / name
        inst_dir.mkdir(parents=True, exist_ok=True)
        env_path = inst_dir / ".env"
        env_text = env_path.read_text() if env_path.exists() else ""
        env_lines = [l for l in env_text.splitlines() if not l.startswith(f"{api_key_env}=")]
        env_lines.append(f"{api_key_env}={api_key}")
        env_path.write_text("\n".join(env_lines) + "\n")
        env_path.chmod(0o600)
        sub_id = self._existing["id"] if self._edit_mode else sub["id"]
        models_str = ", ".join(f"{k}={v}" for k, v in model_maps.items() if v)
        self.query_one("#oauth-info", Static).update(
            "[bold green]✓ Subscription created successfully![/bold green]\n\n"
            f"[bold]Name:[/bold] {name}\n"
            f"[bold]Auth:[/bold] Claude Max (OAuth)\n"
            f"[bold]Models:[/bold] {models_str}\n"
        )
        self.query_one("#oauth-url-row", Horizontal).display = False
        self.query_one("#wiz-oauth-code", Input).display = False
        self.query_one("#oauth-nav-row", Horizontal).display = False
        self.query_one("#oauth-status", Static).update("[bold green]✓ OAuth token obtained![/bold green]")
        self.query_one("#oauth-result", Static).display = True
        self.query_one("#oauth-close", Button).display = True
        self._oauth_state["_sub_id"] = sub_id

    def _get_model_value(self, alias: str) -> str:
        sel_id = f"wiz-{alias}-sel"
        inp_id = f"wiz-{alias}"
        sel = self.query_one(f"#{sel_id}", Select)
        inp = self.query_one(f"#{inp_id}", Input)
        if sel.display and sel.value and sel.value != Select.BLANK:
            return str(sel.value)
        return inp.value.strip()# --- Main App ---

class HeimsenseApp(App):
    """Heimsense TUI Manager — main screen."""

    ENABLE_COMMAND_PALETTE = False
    TITLE = "Heimsense TUI"

    def _handle_exception(self, error: Exception) -> bool:
        """Textual exception handler — log all unexpected errors."""
        log.exception("Unexpected error in TUI: %s", error)
        return super()._handle_exception(error)

    CSS = """
    DataTable.instance-list {
        width: 100%;
        height: 1fr;
        border: solid $primary;
    }
    Vertical.list-panel {
        width: 45%;
        height: 100%;
    }
    Button.add-btn {
        width: 100%;
        margin: 0;
    }
    Vertical.detail-panel {
        width: 55%;
        height: 100%;
        padding: 1 1;
    }
    #detail {
        height: 70%;
        overflow-y: auto;
        padding: 0 1;
    }
    #script-age {
        height: 1;
        width: 100%;
        text-align: right;
        padding: 0 1;
        color: $text-muted;
    }
    Grid.buttons {
        grid-size: 3;
        height: auto;
        margin: 1 0;
    }
    Button {
        margin: 0 1;
        min-width: 12;
    }
    ModalScreen {
        align: center middle;
    }
    ModalScreen > Vertical, ModalScreen > Static, ModalScreen > Horizontal {
        width: 50;
        padding: 1 2;
        border: solid $primary;
        background: $surface;
    }
    ModalScreen Input, ModalScreen Select {
        width: 100%;
    }
    .hidden {
        display: none;
    }
    #oauth-url-row {
        width: 100%;
        height: auto;
        min-height: 1;
        align: center middle;
        border: none;
        padding: 0 1;
    }
    #oauth-url-row > Button {
        width: auto;
        min-width: 14;
        margin: 0 1;
    }
    """

    def __init__(self, config: ConfigManager, initial_selected: str | None = None):
        super().__init__()
        self.cm = config
        self.im = InstanceManager(config)
        self.sync = SyncManager(config)
        self.failover = FailoverManager(config, self.sync)
        self._selected_id: str | None = None
        self._initial_selected: str | None = initial_selected
        # Cache for direct test results (sub_id → {code, body, ts})
        self._test_results: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(classes="list-panel"):
                yield DataTable(classes="instance-list", id="inst-table")
                yield Button(Text.from_markup("[bold yellow]+[/bold yellow] Add Provider"), id="add", variant="primary", classes="add-btn")
            with Vertical(classes="detail-panel"):
                yield Static(id="script-age", markup=True)
                yield Static(id="detail", markup=True)
                with Grid(classes="buttons"):
                    yield Button(Text.from_markup("[bold yellow]S[/bold yellow]tart"), id="toggle", variant="success")
                    yield Button(Text.from_markup("[bold yellow]T[/bold yellow]est"), id="test")
                    yield Button(Text.from_markup("[bold yellow]S[/bold yellow]ync"), id="launch", variant="primary")
                    yield Button(Text.from_markup("[bold yellow]R[/bold yellow]eauth"), id="reauth", variant="primary")
                    yield Button(Text.from_markup("[bold yellow]F[/bold yellow]orce Model"), id="force_model")
                    yield Button(Text.from_markup("[bold yellow]E[/bold yellow]dit"), id="edit")
                    yield Button(Text.from_markup("[bold yellow]L[/bold yellow]ogs"), id="logs")
                    yield Button("Cancel reload", id="cancel_hotload", variant="warning", classes="hidden")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#inst-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Name", "Port", "Status", "Test")
        self._refresh_table()
        # Hotload restart: reuse previously selected subscription
        if self._initial_selected:
            self._selected_id = self._initial_selected
            # Find row index for selected sub and set cursor
            for row_idx, sub in enumerate(sorted(self.cm.subscriptions, key=lambda s: (
                0 if s["id"] == self.cm.default_instance else 1,
                s["name"].lower(),
            ))):
                if sub["id"] == self._initial_selected:
                    if row_idx < table.row_count:
                        table.move_cursor(row=row_idx)
                    break
        self._show_detail()
        # Context-sensitive is now controlled by _show_detail (called via _refresh_table cursor)
        # Auto-reload: check every 3 seconds if the script has changed on disk
        self._script_mtime = os.stat(os.path.abspath(__file__)).st_mtime
        self.set_interval(3, self._check_hotload)
        # Update table every 5 seconds (PM2 log scanning)
        self.set_interval(5, self._refresh_table)
        # Script age display — update every second
        self._script_birth = self._script_mtime
        self.set_interval(1, self._update_script_age)
        self._update_script_age()
        # Failover: periodic health-check every 5 minutes (background thread)
        self.set_interval(300, self._background_health_check)

    def _check_hotload(self):
        try:
            new_mtime = os.stat(os.path.abspath(__file__)).st_mtime
            if new_mtime != self._script_mtime:
                self._script_mtime = new_mtime
                self.push_screen(HotloadModal(), self._on_hotload_result)
        except Exception:
            pass

    def _on_hotload_result(self, do_reload: bool):
        if do_reload:
            log.info("Hotload: restarting...")
            args = [sys.executable, os.path.abspath(__file__), "--tui"]
            if self._selected_id:
                args += ["--selected", self._selected_id]
            os.execv(sys.executable, args)
        else:
            self.notify("Hotload cancelled", timeout=3)

    def _update_script_age(self):
        try:
            age = time.time() - self._script_birth
            if age < 120:
                label = f"[dim]{age:.0f}s[/dim]"
            else:
                m = int(age // 60)
                s = int(age % 60)
                label = f"[dim]{m}m {s}s[/dim]"
            self.query_one("#script-age", Static).update(label)
        except Exception:
            pass

    def action_cancel_hotload(self):
        pass

    # --- Table ---

    def _refresh_table(self):
        """Refresh DataTable with subscriptions + test status."""
        table = self.query_one("#inst-table", DataTable)
        # Save cursor position before clear
        prev_cursor = table.cursor_row if table.row_count > 0 else None
        table.clear()
        default_id = self.cm.default_instance
        sorted_subs = sorted(self.cm.subscriptions, key=lambda s: (
            0 if s["id"] == default_id else 1,
            s["name"].lower(),
        ))
        for sub in sorted_subs:
            sub_id = sub["id"]
            is_oauth = sub.get("auth_type") == "oauth"
            status_info = self.im.get_status(sub_id)
            status = status_info.get("status", "unknown")
            port = self.cm.get_instance_port(sub_id) if status == "online" else None
            port_str = str(port) if port else ""
            is_failed = sub_id in self.failover._failed_subs
            name_txt = sub["name"] + (" ⚠" if is_failed else "")
            name = Text(name_txt, style="bold" if sub_id == default_id else "red" if is_failed else "")

            if is_oauth:
                oauth_token = sub.get("api_key", "")
                token_pref = oauth_token[:8] if oauth_token else "?"
                status_dot = Text(token_pref, style="green" if oauth_token else "red")
                test_text = Text("—", style="gray")
            else:
                status_dot = Text({
                    "online": "●",
                    "stopped": "○",
                    "error": "✖",
                    "unknown": "?"
                }.get(status, "?"), style=_status_color(status))

                http_stat = status_info.get("last_http_status")
                if http_stat is None:
                    test_icon = "—"
                    test_color = "gray"
                elif http_stat == 200:
                    test_icon = "✓"
                    test_color = "green"
                elif 400 <= http_stat < 500:
                    test_icon = "⚠"
                    test_color = "yellow"
                else:
                    test_icon = "✖"
                    test_color = "red"
                test_text = Text(test_icon, style=test_color)

            table.add_row(
                name, port_str, status_dot, test_text,
                key=sub_id,
            )
        # Restore cursor: prefer _selected_id over prev_cursor (avoid stale detail)
        target_row = None
        if self._selected_id:
            for i, s in enumerate(sorted_subs):
                if s["id"] == self._selected_id:
                    target_row = i
                    break
        if target_row is not None:
            table.move_cursor(row=target_row)
        elif prev_cursor is not None and prev_cursor < table.row_count:
            table.move_cursor(row=prev_cursor)
        elif table.row_count > 0:
            table.move_cursor(row=0)

    def _show_detail(self):
        """Show details for selected subscription."""
        self._update_subtitle()
        detail = self.query_one("#detail", Static)
        sub_id = self._selected_id
        if not sub_id:
            if len(self.cm.subscriptions) == 0:
                detail.update(
                    "[bold yellow]Welcome to Heimsense![/bold yellow]\n\n"
                    "No providers configured yet.\n\n"
                    "  [bold yellow]+[/bold yellow]   Add new subscription\n"
                    "  [bold yellow]h[/bold yellow]   Show all keyboard shortcuts\n\n"
                    "[dim]Supports: Claude Max (OAuth), DeepSeek,\n"
                    "GitHub Copilot, Gemini, OpenAI and Custom proxies[/dim]"
                )
            else:
                detail.update(
                    "[dim]No subscription selected\n\n"
                    "Press [bold]j[/bold]/[bold]k[/bold] or ↑/↓ to navigate\n"
                    "Press [bold]h[/bold] for help[/dim]"
                )
            self._set_context_sensitive(False)
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            detail.update("[red]Subscription not found[/red]")
            return
        self._set_context_sensitive(True)
        status_info = self.im.get_status(sub_id)
        status = status_info.get("status", "unknown")
        sc = _status_color(status)
        is_default = sub_id == self.cm.default_instance
        default_badge = " [bold green][ACTIVE][/bold green]" if is_default else ""
        pid = status_info.get("pid") or "-"
        uptime = status_info.get("uptime") or "-"
        port = self.cm.get_instance_port(sub_id) or "?"

        # Update toggle button: Start (stopped) / Stop (online)
        toggle_btn = self.query_one("#toggle", Button)
        is_online = status in ("online", "starting")
        toggle_btn.label = "Stop" if is_online else "Start"
        toggle_btn.variant = "error" if is_online else "success"

        # Last HTTP status — prefer direct test result over PM2 log
        test_res = self._test_results.get(sub_id)
        http_stat = status_info.get("last_http_status")
        http_time = status_info.get("last_http_time")

        if test_res:
            # Direct test result is most recent source
            code = test_res["code"]
            stat_color = "green" if code == 200 else ("yellow" if 400 <= code < 500 else "red")
            ago = _time_ago(test_res["ts"])
            # Show error description for non-200
            if code != 200:
                # Attempt to parse JSON error message
                desc = ""
                try:
                    body_data = json.loads(test_res["body"])
                    desc = body_data.get("error", {}).get("message", "") or body_data.get("message", "")
                except Exception:
                    desc = test_res["body"][:120] if test_res["body"] else ""
                if desc:
                    http_line = f"[{stat_color}]{code}[/{stat_color}] — {ago}\n[gray]{desc}[/gray]"
                else:
                    http_line = f"[{stat_color}]{code}[/{stat_color}] — {ago}"
            else:
                http_line = f"[{stat_color}]{code}[/{stat_color}] — {ago}"
        elif http_stat is None:
            http_line = "[gray]No HTTP yet[/gray]"
        else:
            stat_color = "green" if http_stat == 200 else ("yellow" if 400 <= http_stat < 500 else "red")
            ago = _time_ago(http_time)
            http_line = f"[{stat_color}]{http_stat}[/{stat_color}] — {ago}"

        # Show active force model (from settings.json) — only if default
        settings_env = self.sync._load_settings().get("env", {})
        sonnet_override = settings_env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "")
        haiku_override = settings_env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "")
        opus_override = settings_env.get("ANTHROPIC_DEFAULT_OPUS_MODEL", "")
        model_maps = sub.get("model_maps", {})
        is_default = sub_id == self.cm.default_instance
        is_oauth = sub.get("auth_type") == "oauth"
        if is_default:
            haiku_display = haiku_override or model_maps.get('haiku', '-')
            sonnet_display = sonnet_override or model_maps.get('sonnet', '-')
            opus_display = opus_override or model_maps.get('opus', '-')
        else:
            haiku_display = model_maps.get('haiku', '-')
            sonnet_display = model_maps.get('sonnet', '-')
            opus_display = model_maps.get('opus', '-')
            # Reset force-override for display (only relevant for default)
            haiku_override = sonnet_override = opus_override = ""
        is_forced = (sonnet_override and sonnet_override == haiku_override == opus_override
                     and sonnet_override not in model_maps.values())
        force_line = f"[yellow]Force: {sonnet_override}[/yellow]" if is_forced else "[dim]Force: none[/dim]"

        if is_oauth:
            # Get token from subscription api_key (saved during OAuth flow)
            oauth_token = sub.get("api_key", "")
            token_prefix = oauth_token[:8] + "..." if oauth_token else "[red]not set[/red]"
            detail.update(
                f"[bold]{sub['name']}[/bold]{default_badge}\n\n"
                f"Provider:    Claude Max (OAuth)\n"
                f"Token:       {token_prefix}\n"
                f"{'Auth:        OAuth (direct)' + chr(10)}"
                f"Model Haiku: {haiku_display}\n"
                f"Model Sonnet: {sonnet_display}\n"
                f"Model Opus:  {opus_display}\n"
                f"{force_line}\n"
                f"{'Notes:       ' + sub.get('notes', '-') if sub.get('notes', '').strip() else ''}"
            )
        else:
            detail.update(
                f"[bold]{sub['name']}[/bold]{default_badge}\n\n"
                f"Provider:    {sub.get('provider_url', '-')}\n"
                f"Status:      [{sc}]{status}[/{sc}] (PID: {pid})\n"
                f"{'Port:        ' + str(port) + chr(10) if port and is_online else ''}"
                f"{'Auth:        ' + sub.get('auth_type', 'bearer') + chr(10)}"
                f"{'API Key env: ' + sub.get('api_key_env', '-') + chr(10)}"
                f"Model Haiku: {haiku_display}\n"
                f"Model Sonnet: {sonnet_display}\n"
                f"Model Opus:  {opus_display}\n"
                f"{force_line}\n\n"
                f"Uptime:      {uptime}\n"
                f"Last HTTP: {http_line}\n"
                f"{'Notes:       ' + sub.get('notes', '-') if sub.get('notes', '').strip() else ''}"
            )

    def _on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        """Arrow keys → update details."""
        self._selected_id = event.row_key.value
        self._show_detail()

    def on_key(self, event):
        """j/k/n/p as alternative to ↑/↓, 1-9 as direct row-jump (iPhone/mobile keyboard)."""
        table = self.query_one("#inst-table", DataTable)
        if event.key in ("j", "n"):
            table.action_cursor_down()
            event.stop()
        elif event.key in ("k", "p"):
            table.action_cursor_up()
            event.stop()
        elif event.key.isdigit() and event.key != "0":
            # 1-9: jump directly to row N (1-based)
            row_idx = int(event.key) - 1
            if 0 <= row_idx < table.row_count:
                table.move_cursor(row=row_idx)
                event.stop()

    # --- Actions ---

    @staticmethod
    def _run_proxy_test(port: int, api_key: str, model: str) -> dict:
        """Call proxy port exactly as Claude Code would.

        Returns dict with keys: code (int), body (str), elapsed (int ms).
        code=0 means connection error.
        """
        test_url = f"http://localhost:{port}/v1/messages"
        payload = json.dumps({
            "model": model,
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Tell me a fun fact about the universe in 2 sentences."}],
        })
        start_ts = time.time()
        try:
            req = urllib.request.Request(
                test_url,
                data=payload.encode(),
                headers={
                    "content-type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    http_code = resp.getcode()
                    raw = resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as http_err:
                http_code = http_err.code
                raw = http_err.read().decode("utf-8", errors="replace")
            elapsed = int((time.time() - start_ts) * 1000)
            if http_code == 200:
                try:
                    data = json.loads(raw)
                    body = data.get("content", [{}])[0].get("text", raw)[:500]
                except Exception:
                    body = raw[:500]
            else:
                body = raw[:500]
            log.info("Test %s: HTTP %d (%dms)", model, http_code, elapsed)
            return {"code": http_code, "body": body, "elapsed": elapsed}
        except Exception as e:
            elapsed = int((time.time() - start_ts) * 1000)
            log.warning("Test failed for port %s: %s (%dms)", port, e, elapsed)
            return {"code": 0, "body": f"Error: {e}", "elapsed": elapsed}

    def action_test(self):
        """Call claude-mux port exactly as Claude Code would — Anthropic /v1/messages format."""
        sub_id = self._selected_id
        if not sub_id:
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            return
        port = self.cm.get_instance_port(sub_id) or 0
        if not port:
            self.notify("Start the instance first (port not assigned)", title="Test", timeout=3)
            return

        model = (sub.get("model_maps", {}).get("sonnet")
                 or sub.get("model_maps", {}).get("haiku") or "claude-sonnet-4-5")

        # Get API key for claude-mux proxy (it uses ANTHROPIC_AUTH_TOKEN)
        settings_env = self.sync._load_settings().get("env", {})
        api_key = settings_env.get("ANTHROPIC_AUTH_TOKEN", "")
        if not api_key and sub.get("auth_type") == "gh_token":
            try:
                gh_r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
                api_key = gh_r.stdout.strip() if gh_r.returncode == 0 else "dummy"
            except Exception:
                api_key = "dummy"

        self.notify(f"Testing {sub['name']} on port {port}...", title="Test", timeout=2)
        result = self._run_proxy_test(port, api_key, model)
        status_code, reply, elapsed = result["code"], result["body"], result["elapsed"]

        self.push_screen(HealthPopup(f"{sub['name']} — response", status_code, elapsed, reply))
        # Save test result to detail panel
        self._test_results[sub_id] = {
            "code": status_code,
            "body": reply,
            "ts": time.time(),
        }
        self._refresh_table()
        self._show_detail()

    def action_start(self):
        """Start claude-mux for selected subscription."""
        sub_id = self._selected_id
        if not sub_id:
            self.notify("Select a subscription first", title="Start", timeout=3)
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            log.warning("action_start: subscription %s not found", sub_id)
            return
        try:
            result = self.im.start(sub_id)
            log.info("Starting %s on port %s", sub['name'], result['port'])
            # Clear previous test result on new start
            self._test_results.pop(sub_id, None)
            self.notify(f"{sub['name']} started on port {result['port']}",
                        title="Start", timeout=5)
            self._refresh_table()
            self._show_detail()
        except Exception as e:
            log.exception("Error starting %s", sub_id)
            self.notify(f"Error: {e}", title="Start", timeout=5)

    def action_launch(self):
        """Sync: generate .env + sync settings for selected subscription."""
        sub_id = self._selected_id or self.cm.default_instance
        if not sub_id:
            self.notify("Select a subscription", title="Sync", timeout=3)
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            self.notify("Subscription not found", title="Sync", timeout=3)
            return
        try:
            is_oauth = sub.get("auth_type") == "oauth"
            if not is_oauth:
                self.im.generate_env(sub_id)
            result = self.sync.sync_default(sub_id)
            label = "Activate" if is_oauth else "Sync"
            self.notify(
                f"{sub['name']} — {label} OK",
                title=label,
                timeout=4,
            )
        except Exception as e:
            self.notify(f"Sync failed: {e}", severity="error", timeout=5)
        self._refresh_table()
        self._show_detail()

    def action_stop(self):
        """Stop selected subscription."""
        sub_id = self._selected_id
        if not sub_id:
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            return
        msg = f"Stop {sub['name']}?"
        confirm = ConfirmModal("Stop Instance", msg)
        self.push_screen(confirm, self._on_stop_confirmed)

    def _on_stop_confirmed(self, confirmed: bool):
        if not confirmed:
            return
        sub_id = self._selected_id
        if not sub_id:
            return
        try:
            self.im.stop(sub_id)
            log.info("Stopped %s", sub_id)
            self.notify("Stopped", timeout=3)
        except Exception as e:
            log.exception("Stop failed for %s", sub_id)
            self.notify(f"Error: {e}", severity="error", timeout=5)
        self._refresh_table()
        self._show_detail()

    def action_logs(self):
        """Show PM2 logs for selected subscription."""
        sub_id = self._selected_id
        if not sub_id:
            return
        pm2_name = self.cm.get_pm2_name(sub_id)
        if not pm2_name:
            self.notify("No PM2 name for this subscription", severity="error", timeout=3)
            return
        self.push_screen(LogViewer(pm2_name))

    def action_set_default(self):
        """Set selected subscription as default — confirm if another is already active."""
        sub_id = self._selected_id
        if not sub_id:
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            return
        current_default = self.cm.default_instance
        # Already active — just refresh
        if current_default == sub_id:
            self.notify(f"{sub['name']} is already active", timeout=3)
            return
        # Switch to new — show confirm if another is active
        if current_default and current_default != sub_id:
            cur_sub = self.cm.get_subscription(current_default)
            cur_name = cur_sub["name"] if cur_sub else current_default
            msg = f"Switch active provider?\n\nFrom: {cur_name}\nTo: {sub['name']}"
            confirm = ConfirmModal("Switch Provider", msg)
            self.push_screen(confirm, lambda ok, sid=sub_id: self._do_set_default(sid) if ok else None)
        else:
            self._do_set_default(sub_id)

    def _do_set_default(self, sub_id: str):
        """Execute provider switch after optional confirmation."""
        try:
            self.sync.sync_default(sub_id)
            name = (self.cm.get_subscription(sub_id) or {}).get("name", sub_id)
            self.notify(f"Active: {name}", timeout=3)
        except Exception as e:
            self.notify(f"Error: {e}", severity="error", timeout=5)
        self._refresh_table()
        self._show_detail()

    def action_delete(self):
        """Delete selected subscription (confirm first)."""
        sub_id = self._selected_id
        log.info("action_delete called, _selected_id=%s", sub_id)
        if not sub_id:
            self.notify("No subscription selected", title="Delete", timeout=3)
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            log.warning("action_delete: sub %s not found", sub_id)
            self.notify("Subscription not found", title="Delete", timeout=3)
            return
        msg = f"Delete {sub['name']}?\nThis will also stop the PM2 process."
        confirm = ConfirmModal("Delete Subscription", msg)
        self.push_screen(confirm, lambda ok: self._do_delete(sub_id, sub["name"]) if ok else None)

    def _do_delete(self, sub_id: str, label: str):
        """Execute deletion (called after confirmation)."""
        log.info("Deleting subscription %s (%s)", sub_id, label)
        try:
            self.im.stop(sub_id)
        except Exception as e:
            log.warning("Stop failed during delete: %s", e)
        self.cm.delete_subscription(sub_id)
        self.notify(f"Deleted: {label}", timeout=3)
        self._selected_id = None
        self._refresh_table()
        self._show_detail()

    def action_edit(self):
        """Edit selected subscription via AddWizard in edit-mode."""
        sub_id = self._selected_id
        if not sub_id:
            self.notify("Select a subscription first", title="Edit", timeout=3)
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            return
        wizard = AddWizard(self.cm, existing_sub=sub)
        self.push_screen(wizard, self._on_wizard_done)

    def action_add(self):
        """Open wizard to add subscription."""
        wizard = AddWizard(self.cm)
        self.push_screen(wizard, self._on_wizard_done)

    def _on_wizard_done(self, result):
        if result:
            action = "Updated" if result.get("updated") else "Added"
            self.notify(f"{action}: {result['name']}", timeout=3)
            self._selected_id = result["id"]
            self._refresh_table()
            self._show_detail()

    def action_refresh(self):
        """R: refresh all status."""
        self._refresh_table()
        self._show_detail()

    def on_key(self, event) -> None:
        """Intercept r/R at app level so it works even when an Input is focused."""
        from textual.widgets import Input
        focused = self.focused
        if isinstance(focused, Input) and event.key == "r":
            event.stop()
            self.action_refresh()

    def _background_health_check(self):
        """Periodic health-check in background thread — auto-switches on failure.

        Also checks if RETRY_ORIGINAL_AFTER_SECS has passed since last failover
        and attempts to reactivate the original subscription if it works again.
        """
        def _run():
            # Attempt reactivation of original subscription after timeout
            if self.failover.should_retry_original():
                orig_id = self.failover._original_sub_id
                orig_sub = self.cm.get_subscription(orig_id)
                if orig_sub:
                    log.info("Health-check: attempting to reactivate original sub %s", orig_id)
                    try:
                        # Save current active sub BEFORE switching
                        current_sub = self.cm.get_subscription(self.cm.default_instance)
                        self.sync.sync_default(orig_id)
                        ok, reason = self.failover.test_health(orig_id)
                        if ok:
                            self.cm.set_default(orig_id)
                            self.failover.reset_failures()
                            self.failover._log_failover_event(
                                current_sub,
                                orig_sub,
                                "auto-resume after timeout",
                            )
                            log.info("Health-check: reactivated original sub %s", orig_sub["name"])
                            self.call_from_thread(self._notify_resume, orig_sub["name"])
                            return
                        else:
                            # Still failing — reset timer so we try again after RETRY_ORIGINAL_AFTER_SECS
                            self.failover._failover_ts = time.time()
                    except Exception as e:
                        log.warning("Health-check: could not reactivate original: %s", e)
                        self.failover._failover_ts = time.time()

            default_id = self.cm.default_instance
            if not default_id:
                return
            ok, reason = self.failover.test_health(default_id)
            if not ok:
                log.warning("Health-check failed for %s: %s — attempting failover", default_id, reason)
                self.call_from_thread(self._do_auto_failover, default_id, reason)
        threading.Thread(target=_run, daemon=True).start()

    def _notify_resume(self, name: str):
        """UI thread: notification about reactivation of original subscription."""
        self.notify(f"✓ Reactivated: {name} (auto-resume)", title="Failover", timeout=6)
        self._refresh_table()
        self._show_detail()

    def _do_auto_failover(self, failed_id: str, reason: str):
        """Run in UI thread: switch to next subscription."""
        sub = self.cm.get_subscription(failed_id)
        name = sub["name"] if sub else failed_id
        self.notify(f"⚠ {name} failed ({reason[:60]}) — attempting failover...",
                    title="Failover", severity="warning", timeout=8)
        new_id = self.failover.do_failover(failed_id, reason=reason)
        if new_id:
            new_sub = self.cm.get_subscription(new_id)
            new_name = new_sub["name"] if new_sub else new_id
            self.notify(f"✓ Switched to {new_name}", title="Failover", timeout=6)
        else:
            self.notify("No working subscription found!", title="Failover",
                        severity="error", timeout=10)
        self._refresh_table()
        self._show_detail()

    def action_help(self):
        """H / ?: show keyboard shortcuts."""
        self.push_screen(HelpModal())

    def action_failover_log(self):
        """L: show failover log in modal."""
        self.push_screen(FailoverLogModal(self.failover.FAILOVER_LOG))

    def _update_subtitle(self):
        """Update app subtitle with active subscription and failover status."""
        default_id = self.cm.default_instance
        if default_id:
            sub = self.cm.get_subscription(default_id)
            name = sub["name"] if sub else default_id
            failed_count = len(self.failover._failed_subs)
            if failed_count:
                self.sub_title = f"Active: {name}  ⚠ {failed_count} failed"
            else:
                self.sub_title = f"Active: {name}"
        else:
            self.sub_title = "No active subscription"

    def action_failover_check(self):
        """Manual: test active subscription and failover if necessary."""
        default_id = self.cm.default_instance
        if not default_id:
            self.notify("No active subscription", timeout=3)
            return
        sub = self.cm.get_subscription(default_id)
        self.notify(f"Testing {sub['name'] if sub else default_id}...", timeout=3)
        self.failover.reset_failures()
        self._background_health_check()

    def action_force_model(self):
        """F: force all model aliases to one model for active subscription."""
        sub_id = self._selected_id or self.cm.default_instance
        if not sub_id:
            self.notify("Select a subscription first", timeout=3)
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub:
            return
        model_maps = sub.get("model_maps", {})
        available = list(dict.fromkeys(v for v in model_maps.values() if v))
        if not available:
            self.notify("No model maps configured", timeout=3)
            return
        # Current force: read from settings.json
        settings = self.sync._load_settings()
        current = settings.get("env", {}).get("ANTHROPIC_DEFAULT_SONNET_MODEL", "__none__")
        modal = ForceModelModal(current, available)
        self.push_screen(modal, self._on_force_model_done)

    def _on_force_model_done(self, model: str | None):
        if model is None:
            return  # canceled
        settings = self.sync._load_settings()
        env = settings.setdefault("env", {})
        if model == "__none__":
            # Remove force — restore model maps
            sub_id = self._selected_id or self.cm.default_instance
            sub = self.cm.get_subscription(sub_id) if sub_id else None
            model_maps = sub.get("model_maps", {}) if sub else {}
            for key, alias in [("haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                                ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
                                ("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL")]:
                if model_maps.get(key):
                    env[alias] = model_maps[key]
                else:
                    env.pop(alias, None)
            self.sync._save_settings(settings)
            self.notify("Force removed — model maps restored", timeout=4)
        else:
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = model
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model
            self.sync._save_settings(settings)
            self.notify(f"Force model: {model}", timeout=4)
        self._show_detail()

    def action_restart(self):
        """Z: restart the app (hotload) — os.execv replaces the process in-place."""
        args = [sys.executable, os.path.abspath(__file__), "--tui"]
        if self._selected_id:
            args += ["--selected", self._selected_id]
        os.execv(sys.executable, args)

    def action_quit_app(self):
        """Q: quit with confirmation if instances are running."""
        running = []
        for sub in self.cm.subscriptions:
            status = self.im.get_status(sub["id"])
            if status.get("status") in ("online", "starting"):
                running.append(sub["name"])
        if running:
            msg = "Running instances:\n" + "\n".join(f"• {n}" for n in running)
            confirm = ConfirmModal("Quit?", msg)
            self.push_screen(confirm, self._on_quit_confirmed)
        else:
            self.exit()

    def action_reauth(self):
        """Reauth: restart OAuth flow for selected Claude Max sub."""
        sub_id = self._selected_id
        if not sub_id:
            return
        sub = self.cm.get_subscription(sub_id)
        if not sub or sub.get("auth_type") != "oauth":
            self.notify("Only OAuth subscriptions support reauth", timeout=3)
            return
        wizard = AddWizard(self.cm, existing_sub=sub, reauth=True)
        self.push_screen(wizard, self._on_wizard_done)

    def _on_quit_confirmed(self, result: bool):
        if result:
            self.exit()

    # --- Keyboard bindings ---

    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("s", "toggle", "Start"),
        ("t", "test", "Test"),
        ("f", "force_model", "Force Model"),
        ("z", "restart", "Reload"),
        ("q", "quit_app", "Quit"),
        ("+", "add", "Add"),
        ("d", "delete", "Delete"),
        ("e", "edit", "Edit"),
        ("enter", "set_default", "Activate"),
        ("l", "logs", "Logs"),
        ("L", "failover_log", "Failover Log"),
        ("x", "failover_check", "Failover"),
        ("h", "help", "Help"),
        ("question_mark", "help", "Help"),
    ]

    def _set_context_sensitive(self, enabled: bool):
        """Show/hide buttons and bindings that require a selected subscription.
        Add button is always shown."""
        # OAuth subscriptions have Start/Stop/Test/Logs — but have Force Model, Activate, Edit, Delete
        sub = self.cm.get_subscription(self._selected_id) if self._selected_id else None
        is_oauth = bool(sub and sub.get("auth_type") == "oauth")

        # Common buttons: force_model, edit — always shown when enabled
        for btn_id in ("force_model", "edit"):
            self.query_one(f"#{btn_id}", Button).display = enabled

        # OAuth-specific buttons
        for btn_id in ("toggle", "test", "logs"):
            self.query_one(f"#{btn_id}", Button).display = enabled and not is_oauth

        # Reauth only for OAuth
        self.query_one("#reauth", Button).display = enabled and is_oauth

        # Launch (Sync/Activate) — different text
        launch_btn = self.query_one("#launch", Button)
        launch_btn.display = enabled
        launch_btn.label = "Activate" if is_oauth else "Sync"
        launch_btn.variant = "primary" if is_oauth else "primary"

        # Update BINDINGS so only relevant ones show in footer
        all_bindings = [
            ("r", "refresh", "Refresh"),
            ("z", "restart", "Reload"),
            ("q", "quit_app", "Quit"),
            ("+", "add", "Add"),
        ]
        if enabled and not is_oauth:
            all_bindings += [
                ("s", "toggle", "Start"),
                ("t", "test", "Test"),
                ("d", "delete", "Delete"),
                ("e", "edit", "Edit"),
                ("enter", "set_default", "Set Default"),
                ("l", "logs", "Logs"),
                ("f", "force_model", "Force Model"),
            ]
        elif enabled:
            # OAuth: force_model, activate, reauth, edit, set_default, delete (no proxy controls)
            all_bindings += [
                ("f", "force_model", "Force Model"),
                ("a", "launch", "Activate"),
                ("R", "reauth", "Reauth"),
                ("d", "delete", "Delete"),
                ("e", "edit", "Edit"),
                ("enter", "set_default", "Set Default"),
            ]
        self.BINDINGS = all_bindings
        self.refresh_bindings()

    # --- Button handlers ---

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "toggle":
            self.action_toggle()
        elif event.button.id == "launch":
            self.action_launch()
        elif event.button.id == "test":
            self.action_test()
        elif event.button.id == "force_model":
            self.action_force_model()
        elif event.button.id == "logs":
            self.action_logs()
        elif event.button.id == "edit":
            self.action_edit()
        elif event.button.id == "reauth":
            self.action_reauth()
        elif event.button.id == "cancel_hotload":
            self.action_cancel_hotload()
        elif event.button.id == "add":
            self.action_add()

    def action_toggle(self):
        """Start/Stop toggle for selected subscription."""
        sub_id = self._selected_id
        if not sub_id:
            self.notify("Select a subscription first", title="Toggle", timeout=3)
            return
        status = self.im.get_status(sub_id).get("status", "unknown")
        if status in ("online", "starting"):
            self.action_stop()
        else:
            self.action_start()


# --- Entry points ---

def run_tui():
    """Start Heimsense TUI."""
    log.info("=== Heimsense TUI starting ===")

    # Global exception hook — catch anything not caught by Textual
    def _global_excepthook(exc_type, exc_value, exc_tb):
        log.exception("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        print(f"FATAL: {exc_type.__name__}: {exc_value}", file=sys.stderr)
    sys.excepthook = _global_excepthook

    # Parse --selected <sub_id> from hotload
    initial_selected: str | None = None
    if "--selected" in sys.argv:
        idx = sys.argv.index("--selected")
        if idx + 1 < len(sys.argv):
            initial_selected = sys.argv[idx + 1]
    try:
        cm = ConfigManager()
        app = HeimsenseApp(cm, initial_selected=initial_selected)
        app.run()
    except Exception as e:
        log.exception("Fatal error in TUI")
        print(f"FATAL: {e}", file=sys.stderr)
        raise


# --- Standalone test ---
if __name__ == "__main__":

    if "--tui" in sys.argv:
        run_tui()
        sys.exit(0)

    cm = ConfigManager()

    if "--setup-test" in sys.argv:
        # Create a test-subscription and set as default
        os.environ["TEST_API_KEY"] = "sk-test-456"
        sub = cm.add_subscription(
            name="test",
            provider_url="https://api.test.dev/v1",
            api_key_env="TEST_API_KEY",
            label="Test Provider",
            auth_type="bearer",
            model_maps={"haiku": "test-mini", "sonnet": "test-medium", "opus": "test-max"},
            notes="TUI test",
        )
        print(f"Created: {sub['name']} (id={sub['id'][:8]}..., port={sub['default_port']})")
        cm.set_default(sub["id"])
        assert cm.default_instance == sub["id"]
        im = InstanceManager(cm)
        im.generate_env(sub["id"])
        print(f"Default set to: {sub['name']}")
        print(f".env generated: {CLAUDE_MUX_DIR / 'instances' / sub['name'] / '.env'}")
        print("OK: Setup complete")

    elif "--test-all" in sys.argv:
        # Test 1: ConfigManager
        initial_count = len(cm.subscriptions)
        sub = cm.add_subscription("test-all", "https://test.all", "TEST_KEY")
        assert len(cm.subscriptions) == initial_count + 1
        assert cm.get_subscription(sub["id"]) is not None
        cm.set_default(sub["id"])
        assert cm.default_instance == sub["id"]
        cm.delete_subscription(sub["id"])
        assert len(cm.subscriptions) == initial_count
        print("OK: ConfigManager")

        # Test 2: InstanceManager (env file generation)
        sub = cm.add_subscription("im-test", "https://im.test", "IM_KEY")
        im = InstanceManager(cm)
        im._regenerate_ecosystem()

        # Verify API key and default values are written to .env (Fund 1+2 fix)
        os.environ["IM_KEY"] = "sk-test-123"
        env_path = im.generate_env(sub["id"])
        assert env_path.exists()
        assert env_path.stat().st_mode & 0o777 == 0o600
        env_text = env_path.read_text()
        assert "ANTHROPIC_API_KEY=sk-test-123" in env_text, \
            f"API key missing in .env:\n{env_text}"
        assert "REQUEST_TIMEOUT_MS=120000" in env_text, \
            f"Default value Request timeout missing:\n{env_text}"
        assert "MAX_TOKENS=4096" in env_text, \
            f"Default value Max tokens missing:\n{env_text}"
        print("OK: InstanceManager (+ API key + defaults verified)")
        cm.delete_subscription(sub["id"])

        # Test 3: SyncManager
        sub = cm.add_subscription("sync-test", "https://sync.test", "SYNC_KEY")
        sync = SyncManager(cm)
        result = sync.sync_default(sub["id"])
        assert result["default"] == "sync-test"
        assert result["base_url"].startswith("http://localhost:")
        assert "ANTHROPIC_BASE_URL" in result["keys_updated"]
        cm.delete_subscription(sub["id"])
        print("OK: SyncManager")

        print("\n=== ALL TESTS PASSED ===")

    else:
        # Quick smoke test
        initial_count = len(cm.subscriptions)
        sub = cm.add_subscription("smoke", "https://smoke.test", "SMOKE_KEY")
        assert len(cm.subscriptions) == initial_count + 1
        cm.delete_subscription(sub["id"])
        assert len(cm.subscriptions) == initial_count
        print("OK: Smoke test passed")

