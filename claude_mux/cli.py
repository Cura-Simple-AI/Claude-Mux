#!/usr/bin/env python3
"""claude-mux CLI — all TUI actions available as CLI subcommands.

Every command follows CLI Guidelines (https://clig.dev/):
  - --json for machine-readable output
  - --quiet to suppress non-essential output
  - Exit codes: 0=ok, 1=error, 2=usage, 3=not-found, 4=health-fail
  - --help on every subcommand
"""
import json
import sys

import click

from claude_mux.tui import (
    __version__,
    ConfigManager,
    InstanceManager,
    SyncManager,
    FailoverManager,
    CLAUDE_MUX_DIR,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cm() -> ConfigManager:
    return ConfigManager()


def _managers():
    cm = _cm()
    sync = SyncManager(cm)
    im = InstanceManager(cm)
    fm = FailoverManager(cm, sync)
    return cm, sync, im, fm


def _find_sub(cm: ConfigManager, name_or_id: str) -> dict | None:
    """Resolve subscription by name or id (case-insensitive prefix match)."""
    name_lower = name_or_id.lower()
    for s in cm.subscriptions:
        if s["id"] == name_or_id or s["name"].lower() == name_lower:
            return s
    # prefix match
    matches = [s for s in cm.subscriptions if s["name"].lower().startswith(name_lower)]
    return matches[0] if len(matches) == 1 else None


def _sub_status(cm: ConfigManager, im: InstanceManager, sub: dict) -> str:
    """Return human-readable status string for a subscription."""
    if sub.get("auth_type") == "oauth":
        return "oauth"
    port = cm.get_instance_port(sub["id"])
    if not port:
        return "stopped"
    try:
        info = im.status(sub["id"])
        return info.get("status", "stopped")
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version", prog_name="claude-mux")
@click.pass_context
def cli(ctx):
    """claude-mux — use any LLM inside Claude Code. Automatically.

    Run without a subcommand to open the interactive TUI.
    Use subcommands for scripting and automation.
    """
    if ctx.invoked_subcommand is None:
        from claude_mux.tui import run_tui
        run_tui()


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@cli.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--quiet", "-q", is_flag=True, help="Suppress header")
def cmd_list(as_json, quiet):
    """List all subscriptions."""
    cm = _cm()
    subs = cm.subscriptions
    default_id = cm.default_instance

    if as_json:
        out = []
        for s in subs:
            out.append({
                "id": s["id"],
                "name": s["name"],
                "provider": s.get("provider", ""),
                "auth_type": s.get("auth_type", "bearer"),
                "port": cm.get_instance_port(s["id"]),
                "active": s["id"] == default_id,
            })
        click.echo(json.dumps(out, indent=2))
        return

    if not subs:
        if not quiet:
            click.echo("No subscriptions. Run 'claude-mux' to add one.")
        sys.exit(0)

    if not quiet:
        active_name = next((s["name"] for s in subs if s["id"] == default_id), "none")
        click.echo(f"Active: {active_name}\n")
        click.echo(f"{'NAME':<20} {'PROVIDER':<20} {'AUTH':<10} {'PORT':<8} {'DEFAULT'}")
        click.echo("-" * 66)

    for s in subs:
        is_default = "✓" if s["id"] == default_id else ""
        port = cm.get_instance_port(s["id"]) or "-"
        provider = s.get("provider_url", "")[:18]
        auth = s.get("auth_type", "bearer")
        click.echo(f"{s['name']:<20} {provider:<20} {auth:<10} {str(port):<8} {is_default}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command("status")
@click.argument("name", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def cmd_status(name, as_json):
    """Show status of subscriptions.

    NAME: optional subscription name to show a single entry.
    """
    cm, _, im, _ = _managers()
    subs = [_find_sub(cm, name)] if name else cm.subscriptions

    if name and not subs[0]:
        click.echo(f"Error: subscription '{name}' not found", err=True)
        sys.exit(3)

    result = []
    for s in subs:
        st = _sub_status(cm, im, s)
        result.append({
            "name": s["name"],
            "status": st,
            "active": s["id"] == cm.default_instance,
            "port": cm.get_instance_port(s["id"]),
        })

    if as_json:
        click.echo(json.dumps(result, indent=2))
        return

    for r in result:
        active_marker = " [active]" if r["active"] else ""
        port_str = f" port={r['port']}" if r["port"] else ""
        click.echo(f"{r['name']}: {r['status']}{port_str}{active_marker}")


# ---------------------------------------------------------------------------
# activate
# ---------------------------------------------------------------------------

@cli.command("activate")
@click.argument("name")
@click.option("--quiet", "-q", is_flag=True, help="Suppress output")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def cmd_activate(name, quiet, as_json):
    """Activate a subscription as the default for Claude Code.

    Updates ~/.claude/settings.json with the correct env vars.
    """
    cm, sync, _, _ = _managers()
    sub = _find_sub(cm, name)
    if not sub:
        click.echo(f"Error: subscription '{name}' not found", err=True)
        sys.exit(3)

    try:
        sync.sync_default(sub["id"])
        if as_json:
            click.echo(json.dumps({"ok": True, "name": sub["name"], "id": sub["id"]}))
        elif not quiet:
            click.echo(f"Activated: {sub['name']}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

@cli.command("start")
@click.argument("name")
@click.option("--quiet", "-q", is_flag=True)
def cmd_start(name, quiet):
    """Start the proxy for a subscription (pm2)."""
    cm, _, im, _ = _managers()
    sub = _find_sub(cm, name)
    if not sub:
        click.echo(f"Error: subscription '{name}' not found", err=True)
        sys.exit(3)
    if sub.get("auth_type") == "oauth":
        click.echo(f"{sub['name']}: OAuth provider — no proxy needed", err=True)
        sys.exit(1)
    try:
        result = im.start(sub["id"])
        if not quiet:
            click.echo(f"Started {sub['name']} on port {result['port']}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

@cli.command("stop")
@click.argument("name", required=False)
@click.option("--all", "stop_all", is_flag=True, help="Stop all running proxies")
@click.option("--quiet", "-q", is_flag=True)
def cmd_stop(name, stop_all, quiet):
    """Stop the proxy for a subscription."""
    cm, _, im, _ = _managers()

    if stop_all:
        for s in cm.subscriptions:
            try:
                im.stop(s["id"])
                if not quiet:
                    click.echo(f"Stopped {s['name']}")
            except Exception:
                pass
        return

    if not name:
        click.echo("Error: provide NAME or --all", err=True)
        sys.exit(2)

    sub = _find_sub(cm, name)
    if not sub:
        click.echo(f"Error: subscription '{name}' not found", err=True)
        sys.exit(3)
    try:
        im.stop(sub["id"])
        if not quiet:
            click.echo(f"Stopped {sub['name']}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------

@cli.command("test")
@click.argument("name", required=False)
@click.option("--json", "as_json", is_flag=True)
def cmd_test(name, as_json):
    """Run a health check on a subscription.

    NAME: defaults to active subscription if omitted.

    Exit code 4 if health check fails.
    """
    cm, sync, im, fm = _managers()

    if name:
        sub = _find_sub(cm, name)
        if not sub:
            click.echo(f"Error: subscription '{name}' not found", err=True)
            sys.exit(3)
        sub_id = sub["id"]
    else:
        sub_id = cm.default_instance
        if not sub_id:
            click.echo("Error: no active subscription", err=True)
            sys.exit(3)
        sub = cm.get_subscription(sub_id)

    ok, reason = fm.test_health(sub_id)

    if as_json:
        port = cm.get_instance_port(sub_id)
        click.echo(json.dumps({
            "name": sub["name"],
            "ok": ok,
            "reason": reason,
            "port": port,
        }))
    else:
        status = "OK" if ok else "FAIL"
        click.echo(f"{sub['name']}: {status} — {reason}")

    if not ok:
        sys.exit(4)


# ---------------------------------------------------------------------------
# failover
# ---------------------------------------------------------------------------

@cli.command("failover")
@click.option("--json", "as_json", is_flag=True)
def cmd_failover(as_json):
    """Trigger a manual failover check on the active subscription.

    Tests active subscription; if failing, switches to next available.
    """
    cm, sync, im, fm = _managers()
    sub_id = cm.default_instance
    if not sub_id:
        click.echo("Error: no active subscription", err=True)
        sys.exit(3)

    ok, reason = fm.test_health(sub_id)
    if ok:
        sub = cm.get_subscription(sub_id)
        if as_json:
            click.echo(json.dumps({"ok": True, "name": sub["name"], "action": "none"}))
        else:
            click.echo(f"{sub['name']}: healthy — no failover needed")
        return

    new_id = fm.do_failover(sub_id)
    if new_id:
        new_sub = cm.get_subscription(new_id)
        sync.sync_default(new_id)
        if as_json:
            click.echo(json.dumps({"ok": True, "switched_to": new_sub["name"], "reason": reason}))
        else:
            click.echo(f"Failover: switched to {new_sub['name']} (was: {reason})")
    else:
        if as_json:
            click.echo(json.dumps({"ok": False, "reason": "no working subscription found"}))
        else:
            click.echo(f"Failover failed: no working subscription found ({reason})", err=True)
        sys.exit(4)


# ---------------------------------------------------------------------------
# failover-log
# ---------------------------------------------------------------------------

@cli.command("failover-log")
@click.option("--tail", "-n", default=0, type=int, help="Show last N lines (default: all)")
@click.option("--json", "as_json", is_flag=True)
def cmd_failover_log(tail, as_json):
    """Show the failover event log."""
    log_path = CLAUDE_MUX_DIR / "failover.log"
    if not log_path.exists():
        if as_json:
            click.echo("[]")
        else:
            click.echo("No failover events yet.")
        return

    lines = log_path.read_text().splitlines()
    if tail:
        lines = lines[-tail:]

    if as_json:
        events = []
        for line in lines:
            parts = {}
            for token in line.split("  "):
                token = token.strip()
                if "=" in token:
                    k, _, v = token.partition("=")
                    parts[k.lower()] = v
                elif token:
                    parts["ts"] = token
            if parts:
                events.append(parts)
        click.echo(json.dumps(events, indent=2))
    else:
        click.echo("\n".join(lines) if lines else "No failover events yet.")


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------

@cli.command("logs")
@click.argument("name")
@click.option("--tail", "-n", default=50, type=int, help="Show last N lines (default: 50)")
def cmd_logs(name, tail):
    """Show PM2 proxy logs for a subscription."""
    import subprocess
    cm = _cm()
    sub = _find_sub(cm, name)
    if not sub:
        click.echo(f"Error: subscription '{name}' not found", err=True)
        sys.exit(3)
    pm2_name = cm.get_pm2_name(sub["id"]) or f"claude-mux-{sub['name']}"
    try:
        result = subprocess.run(
            ["pm2", "logs", pm2_name, "--lines", str(tail), "--nostream"],
            capture_output=True, text=True, timeout=10,
        )
        click.echo(result.stdout or result.stderr or "(no output)")
    except FileNotFoundError:
        click.echo("Error: pm2 not found — install with: npm install -g pm2", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

@cli.command("config")
@click.option("--json", "as_json", is_flag=True)
def cmd_config(as_json):
    """Show configuration paths and active settings."""
    from claude_mux.tui import SyncManager
    cm = _cm()
    sync = SyncManager(cm)

    default_id = cm.default_instance
    active_sub = cm.get_subscription(default_id) if default_id else None
    active_name = active_sub["name"] if active_sub else "none"
    active_auth = active_sub.get("auth_type", "?") if active_sub else "-"

    settings_path = sync.SETTINGS_PATH

    if as_json:
        click.echo(json.dumps({
            "config_dir": str(CLAUDE_MUX_DIR),
            "subscriptions": str(CLAUDE_MUX_DIR / "subscriptions.json"),
            "failover_log": str(CLAUDE_MUX_DIR / "failover.log"),
            "claude_settings": str(settings_path),
            "active": active_name,
            "active_auth": active_auth,
        }, indent=2))
        return

    click.echo(f"Config dir:    {CLAUDE_MUX_DIR}")
    click.echo(f"Subscriptions: {CLAUDE_MUX_DIR / 'subscriptions.json'}")
    click.echo(f"Failover log:  {CLAUDE_MUX_DIR / 'failover.log'}")
    click.echo(f"Claude config: {settings_path}")
    click.echo(f"Active sub:    {active_name} ({active_auth})")


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

@cli.command("add")
@click.option("--name", "-n", required=True, help="Subscription name (e.g. deepseek)")
@click.option("--url", "-u", required=True, help="Provider base URL")
@click.option("--key-env", "-k", default="", help="Env var name that holds the API key")
@click.option("--api-key", "-K", default="", help="API key value (stored directly in subscriptions.json)")
@click.option("--auth", default="bearer", show_default=True,
              help="Auth type: bearer | gh_token | x-goog-api-key | oauth")
@click.option("--haiku", default="", help="Model alias for haiku tier")
@click.option("--sonnet", default="", help="Model alias for sonnet tier")
@click.option("--opus", default="", help="Model alias for opus tier")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def cmd_add(name, url, key_env, api_key, auth, haiku, sonnet, opus, as_json):
    """Add a new subscription.

    Example:

      claude-mux add -n deepseek -u https://api.deepseek.com/v1 -k DEEPSEEK_API_KEY

    For Claude Max (OAuth):

      claude-mux add -n claude-max -u https://api.anthropic.com -k CLAUDE_CODE_OAUTH_TOKEN --auth oauth
    """
    cm = _cm()
    if any(s["name"].lower() == name.lower() for s in cm.subscriptions):
        click.echo(f"Error: subscription '{name}' already exists", err=True)
        sys.exit(1)

    model_maps = {}
    if haiku:
        model_maps["haiku"] = haiku
    if sonnet:
        model_maps["sonnet"] = sonnet
    if opus:
        model_maps["opus"] = opus

    sub = cm.add_subscription(name, url, key_env, auth_type=auth, model_maps=model_maps, api_key=api_key)

    if as_json:
        click.echo(json.dumps({"ok": True, "id": sub["id"], "name": sub["name"]}))
    else:
        click.echo(f"Added: {sub['name']} ({auth})")


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------

@cli.command("edit")
@click.argument("name")
@click.option("--url", "-u", default=None, help="New provider base URL")
@click.option("--key-env", "-k", default=None, help="New env var name for API key")
@click.option("--auth", default=None, help="New auth type")
@click.option("--haiku", default=None, help="Model alias for haiku tier")
@click.option("--sonnet", default=None, help="Model alias for sonnet tier")
@click.option("--opus", default=None, help="Model alias for opus tier")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def cmd_edit(name, url, key_env, auth, haiku, sonnet, opus, as_json):
    """Edit an existing subscription.

    Only the fields you pass are updated.
    """
    cm = _cm()
    sub = _find_sub(cm, name)
    if not sub:
        click.echo(f"Error: subscription '{name}' not found", err=True)
        sys.exit(3)

    updates = {}
    if url is not None:
        updates["provider_url"] = url
    if key_env is not None:
        updates["api_key_env"] = key_env
    if auth is not None:
        updates["auth_type"] = auth

    model_maps = {}
    if haiku is not None:
        model_maps["haiku"] = haiku
    if sonnet is not None:
        model_maps["sonnet"] = sonnet
    if opus is not None:
        model_maps["opus"] = opus
    if model_maps:
        updates["model_maps"] = model_maps

    if not updates:
        click.echo("Nothing to update — pass at least one option", err=True)
        sys.exit(2)

    updated = cm.update_subscription(sub["id"], **updates)
    if as_json:
        click.echo(json.dumps({"ok": True, "name": updated["name"]}))
    else:
        click.echo(f"Updated: {updated['name']}")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

@cli.command("delete")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def cmd_delete(name, yes, as_json):
    """Delete a subscription."""
    cm = _cm()
    sub = _find_sub(cm, name)
    if not sub:
        click.echo(f"Error: subscription '{name}' not found", err=True)
        sys.exit(3)

    if not yes and not as_json:
        click.confirm(f"Delete '{sub['name']}'?", abort=True)

    ok = cm.delete_subscription(sub["id"])
    if as_json:
        click.echo(json.dumps({"ok": ok, "name": sub["name"]}))
    elif ok:
        click.echo(f"Deleted: {sub['name']}")
    else:
        click.echo(f"Error: failed to delete '{sub['name']}'", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# force-model
# ---------------------------------------------------------------------------

@cli.command("force-model")
@click.argument("name")
@click.argument("model", required=False)
@click.option("--tier", default="sonnet", show_default=True,
              help="Which tier to override: haiku | sonnet | opus")
@click.option("--reset", is_flag=True, help="Remove the forced model override")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def cmd_force_model(name, model, tier, reset, as_json):
    """Force a specific model for a subscription tier.

    Example: override the 'sonnet' tier to use a specific model name:

      claude-mux force-model deepseek deepseek-reasoner --tier sonnet

    Reset the override:

      claude-mux force-model deepseek --reset --tier sonnet
    """
    cm = _cm()
    sub = _find_sub(cm, name)
    if not sub:
        click.echo(f"Error: subscription '{name}' not found", err=True)
        sys.exit(3)

    if reset:
        # update_subscription merges model_maps — to delete a key we must replace directly
        maps = dict(sub.get("model_maps", {}))
        maps.pop(tier, None)
        for s in cm._data["subscriptions"]:
            if s["id"] == sub["id"]:
                s["model_maps"] = maps
                cm._save()
                break
        if as_json:
            click.echo(json.dumps({"ok": True, "name": sub["name"], "action": "reset", "tier": tier}))
        else:
            click.echo(f"Reset {tier} model for {sub['name']}")
        return

    if not model:
        click.echo("Error: provide MODEL or --reset", err=True)
        sys.exit(2)

    cm.update_subscription(sub["id"], model_maps={tier: model})
    if as_json:
        click.echo(json.dumps({"ok": True, "name": sub["name"], "tier": tier, "model": model}))
    else:
        click.echo(f"Set {sub['name']} {tier} → {model}")


# ---------------------------------------------------------------------------
# active
# ---------------------------------------------------------------------------

@cli.command("active")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def cmd_active(as_json):
    """Print the subscription currently active in Claude Code.

    Reads ~/.claude/settings.json and matches against saved subscriptions.
    Exits with code 1 if no match is found.
    """
    from claude_mux.sync import SyncManager
    cm = _cm()
    sync = SyncManager(cm)
    sub_id = sync.detect_active()
    if sub_id:
        sub = cm.get_subscription(sub_id)
        name = sub["name"] if sub else sub_id
        if as_json:
            click.echo(json.dumps({"active": name, "id": sub_id}))
        else:
            click.echo(name)
    else:
        if as_json:
            click.echo(json.dumps({"active": None}))
        sys.exit(1)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

_STATUSLINE_SCRIPT = """\
#!/bin/sh
# claude-mux statusline — prints the subscription currently active in Claude Code.
# Installed by: claude-mux init
claude-mux active 2>/dev/null
"""

_STATUSLINE_SETTINGS_KEY = "statusLine"


@cli.command("init")
@click.option("--force", is_flag=True, help="Overwrite existing statusLine setting")
def cmd_init(force):
    """First-time setup: install the Claude Code status line integration.

    Writes a statusLine entry to ~/.claude/settings.json so Claude Code
    displays the active claude-mux subscription in its status bar.

    Also installs ~/.claude-mux/bin/statusline.sh which is called by Claude.

    Run once after installing claude-mux:

      claude-mux init
    """
    import json
    from pathlib import Path
    from claude_mux.config import CLAUDE_MUX_DIR, _atomic_write

    settings_path = Path.home() / ".claude" / "settings.json"
    script_path = CLAUDE_MUX_DIR / "bin" / "statusline.sh"

    # Install statusline.sh
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(_STATUSLINE_SCRIPT)
    script_path.chmod(0o755)
    click.echo(f"✓ Installed {script_path}")

    # Patch settings.json
    settings = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                settings = json.load(f)
        except Exception:
            pass

    if _STATUSLINE_SETTINGS_KEY in settings and not force:
        click.echo(
            f"✓ statusLine already set in {settings_path} (use --force to overwrite)"
        )
    else:
        settings[_STATUSLINE_SETTINGS_KEY] = {
            "type": "command",
            "command": f"sh {script_path}",
        }
        try:
            _atomic_write(settings_path, settings)
            click.echo(f"✓ statusLine written to {settings_path}")
        except OSError as e:
            click.echo(f"✗ Could not write {settings_path}: {e}", err=True)
            sys.exit(1)

    click.echo("\nclaude-mux is ready. Restart Claude Code to see the status line.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    cli()


if __name__ == "__main__":
    main()
