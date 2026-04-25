"""
claude-mux instance module — InstanceManager.

PM2 lifecycle for claude-mux proxy instances.
Handles start/stop/restart via PM2, .env generation, and
ecosystem.config.js merge (preserves non-claude-mux apps).
"""

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from claude_mux.config import (
    CLAUDE_MUX_DIR,
    ENV_TEMPLATE_KEYS,
    ConfigManager,
    _port_is_available,
)

log = logging.getLogger("claude-mux")


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


class InstanceManager:
    """PM2 lifecycle for claude-mux proxy instances.

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
