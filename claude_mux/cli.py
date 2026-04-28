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
from claude_mux.sync import TIER_FALLBACK_MODELS, extract_response_body


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
    if sub.get("auth_type") in ("oauth", "oauth_proxy"):
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
    cm, sync, _, _ = _managers()
    subs = cm.subscriptions
    active_id = sync.detect_active()

    if as_json:
        out = []
        for s in subs:
            out.append({
                "id": s["id"],
                "name": s["name"],
                "provider": s.get("provider", ""),
                "auth_type": s.get("auth_type", "bearer"),
                "port": cm.get_instance_port(s["id"]),
                "active": s["id"] == active_id,
            })
        click.echo(json.dumps(out, indent=2))
        return

    if not subs:
        if not quiet:
            click.echo("No subscriptions. Run 'claude-mux' to add one.")
        sys.exit(0)

    if not quiet:
        active_name = next((s["name"] for s in subs if s["id"] == active_id), "none")
        click.echo(f"Active: {active_name}\n")
        click.echo(f"{'NAME':<20} {'PROVIDER':<20} {'AUTH':<10} {'PORT':<8} {'ACTIVE'}")
        click.echo("-" * 66)

    for s in subs:
        is_default = "✓" if s["id"] == active_id else ""
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
    cm, sync, im, _ = _managers()
    subs = [_find_sub(cm, name)] if name else cm.subscriptions

    if name and not subs[0]:
        click.echo(f"Error: subscription '{name}' not found", err=True)
        sys.exit(3)

    active_id = sync.detect_active()

    result = []
    for s in subs:
        st = _sub_status(cm, im, s)
        result.append({
            "name": s["name"],
            "status": st,
            "active": s["id"] == active_id,
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

def _inference_test(sub: dict, model: str, cm: "ConfigManager", sync: "SyncManager") -> dict:
    """Delegate to SyncManager.inference_test — single implementation shared w/ TUI."""
    return sync.inference_test(sub, model)


@cli.command("test")
@click.argument("name", required=False)
@click.argument("tier", required=False, type=click.Choice(["haiku", "sonnet", "opus"]))
@click.option("--model", "model_override", default="",
              help="Test a specific model ID directly (bypasses tier resolution)")
@click.option("--json", "as_json", is_flag=True)
def cmd_test(name, tier, model_override, as_json):
    """Test a subscription via real inference across model tiers.

    With no tier or model, all three tiers (haiku/sonnet/opus) are tested.
    A tier test sends a real message and reports OK/FAIL per model.
    This reveals partial failures such as haiku working but sonnet rate-limited.

    Examples:

      cm test                           # test all tiers on active subscription
      cm test deepseek haiku            # test haiku tier on deepseek
      cm test Test sonnet               # test sonnet tier on Test
      cm test Test --model claude-haiku-4-5-20251001

    Exit code 4 if any tier fails.
    """
    cm, sync, _, _ = _managers()

    if name:
        sub = _find_sub(cm, name)
        if not sub:
            click.echo(f"Error: subscription '{name}' not found", err=True)
            sys.exit(3)
    else:
        sub_id = sync.detect_active()
        if not sub_id:
            click.echo("Error: no active subscription", err=True)
            sys.exit(3)
        sub = cm.get_subscription(sub_id)

    # Determine which tiers to test
    if model_override:
        tiers_to_test = [("custom", model_override)]
    elif tier:
        model = sync.resolve_model_for_tier(sub, tier) or TIER_FALLBACK_MODELS.get(tier, tier)
        tiers_to_test = [(tier, model)]
    else:
        tiers_to_test = []
        for t in ("haiku", "sonnet", "opus"):
            model = sync.resolve_model_for_tier(sub, t) or TIER_FALLBACK_MODELS.get(t, t)
            if model:
                tiers_to_test.append((t, model))

    if not tiers_to_test:
        click.echo(f"{sub['name']}: no models available — run 'cm reload {sub['name']}' to fetch", err=True)
        sys.exit(3)

    results = []
    any_fail = False
    for t, model in tiers_to_test:
        result = _inference_test(sub, model, cm, sync)
        ok = result["code"] == 200
        if not ok:
            any_fail = True
        results.append({
            "tier": t,
            "model": model,
            "ok": ok,
            "code": result["code"],
            "elapsed_ms": result["elapsed"],
            "body": result["body"],
        })

    if as_json:
        click.echo(json.dumps({"name": sub["name"], "results": results}))
    else:
        for r in results:
            status = "OK" if r["ok"] else "FAIL"
            click.echo(
                f"{sub['name']} [{r['tier']}] {r['model']}: {status} ({r['code']}, {r['elapsed_ms']}ms)"
            )
            if not r["ok"]:
                click.echo(f"  → {r['body'][:200]}")
            else:
                click.echo(f"  → {r['body'][:120]}")

    if any_fail:
        sys.exit(4)


# ---------------------------------------------------------------------------
# models — show and refresh cached model list
# ---------------------------------------------------------------------------

@cli.command("models")
@click.argument("name", required=False)
@click.option("--refresh", is_flag=True, help="Re-fetch models from API before listing")
@click.option("--json", "as_json", is_flag=True)
def cmd_models(name, refresh, as_json):
    """Show cached available models for a subscription.

    Use --refresh to re-fetch from the provider API.

    Examples:

      cm models                   # list models for active subscription
      cm models deepseek --refresh
    """
    import time as _time
    cm, sync, _, _ = _managers()

    if name:
        sub = _find_sub(cm, name)
        if not sub:
            click.echo(f"Error: subscription '{name}' not found", err=True)
            sys.exit(3)
        sub_id = sub["id"]
    else:
        sub_id = sync.detect_active()
        if not sub_id:
            click.echo("Error: no active subscription", err=True)
            sys.exit(3)
        sub = cm.get_subscription(sub_id)
        sub_id = sub["id"]

    if refresh:
        click.echo(f"Fetching models for {sub['name']}...")
        models = sync.fetch_available_models(sub_id)
        sub = cm.get_subscription(sub_id)  # reload after update
        if not models:
            click.echo(f"  ✗ Fetch failed (proxy not running or no API key)")
        else:
            click.echo(f"  ✓ {len(models)} models cached")

    available = sub.get("available_models", [])
    blacklisted = set(sub.get("blacklisted_models", []))
    fetched_at = sub.get("models_fetched_at")
    fetched_str = ""
    if fetched_at:
        import datetime
        fetched_str = f" (fetched {datetime.datetime.fromtimestamp(fetched_at).strftime('%Y-%m-%d %H:%M')})"
    elif "models_fetched_at" in sub:
        fetched_str = " (last fetch failed)"

    if as_json:
        click.echo(json.dumps({
            "name": sub["name"],
            "available_models": available,
            "blacklisted_models": list(blacklisted),
            "models_fetched_at": fetched_at,
        }))
        return

    if not available:
        click.echo(f"{sub['name']}: no cached models{fetched_str} — run 'cm models {sub['name']} --refresh'")
        return

    click.echo(f"{sub['name']}: {len(available)} models{fetched_str}")
    for m in available:
        bl_marker = "  [blacklisted]" if m in blacklisted else ""
        click.echo(f"  {m}{bl_marker}")


# ---------------------------------------------------------------------------
# blacklist — manage per-subscription model blacklist
# ---------------------------------------------------------------------------

@cli.command("blacklist")
@click.argument("name")
@click.argument("model", required=False)
@click.option("--remove", "remove_model", default="", help="Model ID to remove from blacklist")
@click.option("--list", "do_list", is_flag=True, help="List blacklisted models")
@click.option("--json", "as_json", is_flag=True)
def cmd_blacklist(name, model, remove_model, do_list, as_json):
    """Manage the model blacklist for a subscription.

    Blacklisted models are never used by 'cm test' or the TUI Test button.
    Edit blacklisted_models in subscriptions.json directly for bulk changes.

    Examples:

      cm blacklist Test claude-opus-4-6        # blacklist a model
      cm blacklist Test --remove claude-opus-4-6
      cm blacklist Test --list
    """
    cm = _cm()
    sub = _find_sub(cm, name)
    if not sub:
        click.echo(f"Error: subscription '{name}' not found", err=True)
        sys.exit(3)

    if remove_model:
        ok = cm.remove_blacklisted_model(sub["id"], remove_model)
        if as_json:
            click.echo(json.dumps({"ok": ok, "action": "removed", "model": remove_model}))
        elif ok:
            click.echo(f"Removed from blacklist: {remove_model}")
        else:
            click.echo(f"{remove_model} was not in blacklist")
        return

    if do_list or not model:
        bl = cm.get_blacklisted_models(sub["id"])
        if as_json:
            click.echo(json.dumps({"name": sub["name"], "blacklisted_models": bl}))
        elif bl:
            click.echo(f"{sub['name']} blacklisted models:")
            for m in bl:
                click.echo(f"  {m}")
        else:
            click.echo(f"{sub['name']}: no blacklisted models")
        return

    ok = cm.add_blacklisted_model(sub["id"], model)
    if as_json:
        click.echo(json.dumps({"ok": ok, "action": "added", "model": model}))
    else:
        click.echo(f"Blacklisted: {model} on {sub['name']}")


# ---------------------------------------------------------------------------
# probe — real inference test (same as TUI "Test" button)
# ---------------------------------------------------------------------------

@cli.command("probe")
@click.argument("name", required=False)
@click.option("--model", default="", help="Model override (default: haiku map, then sonnet map, then claude-haiku-4-5-20251001)")
@click.option("--json", "as_json", is_flag=True)
def cmd_probe(name, model, as_json):
    """Send a real inference request to a subscription's proxy.

    Same as pressing the Test button in the TUI — posts a message to
    the local proxy and shows the full API response.

    NAME: subscription name or id (defaults to active subscription).

    Exit code 4 if the request fails.
    """
    import urllib.request
    import time as _time

    cm, sync, im, fm = _managers()

    if name:
        sub = _find_sub(cm, name)
        if not sub:
            click.echo(f"Error: subscription '{name}' not found", err=True)
            sys.exit(3)
    else:
        sub_id = sync.detect_active()
        if not sub_id:
            click.echo("Error: no active subscription", err=True)
            sys.exit(3)
        sub = cm.get_subscription(sub_id)

    port = cm.get_instance_port(sub["id"])
    if not port:
        click.echo(f"Error: {sub['name']} is not running (no port assigned)", err=True)
        sys.exit(3)

    probe_model = (model
                   or sub.get("model_maps", {}).get("haiku")
                   or sub.get("model_maps", {}).get("sonnet")
                   or TIER_FALLBACK_MODELS["haiku"])
    url = f"http://localhost:{port}/v1/messages"
    payload = json.dumps({
        "model": probe_model,
        "max_tokens": 100,
        "stream": False,
        "messages": [{"role": "user", "content": "Tell me a fun fact about the universe in 2 sentences."}],
    }).encode()

    t0 = _time.time()
    try:
        req = urllib.request.Request(url, data=payload, method="POST", headers={
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                code = resp.getcode()
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            code = e.code
            raw = e.read().decode("utf-8", errors="replace")
    except Exception as e:
        click.echo(f"Connection error: {e}", err=True)
        sys.exit(4)

    elapsed = int((_time.time() - t0) * 1000)
    ok = code == 200
    body = extract_response_body(raw, code)

    if as_json:
        click.echo(json.dumps({"name": sub["name"], "ok": ok, "code": code, "elapsed_ms": elapsed, "body": body}))
    else:
        status = "OK" if ok else "FAIL"
        click.echo(f"{sub['name']}: {status} ({code}, {elapsed}ms)\n\n{body}")

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
    sub_id = sync.detect_active()
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

    active_id = sync.detect_active()
    active_sub = cm.get_subscription(active_id) if active_id else None
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
              help="Auth type: bearer | gh_token | x-goog-api-key | oauth | oauth_proxy")
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

    # Attempt to fetch available models immediately (silent on failure)
    sync = SyncManager(cm)
    models = sync.fetch_available_models(sub["id"])

    if as_json:
        click.echo(json.dumps({"ok": True, "id": sub["id"], "name": sub["name"],
                               "models_fetched": len(models)}))
    else:
        model_note = f" ({len(models)} models fetched)" if models else " (model fetch failed — run 'cm models --refresh')"
        click.echo(f"Added: {sub['name']} ({auth}){model_note}")


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

    # Re-fetch models if URL or auth changed
    if "provider_url" in updates or "auth_type" in updates:
        sync = SyncManager(cm)
        sync.fetch_available_models(updated["id"])

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
# test-tui
# ---------------------------------------------------------------------------

@cli.command("test-tui")
def cmd_test_tui():
    """Open TUI and exit after 1s — verify no startup crash."""
    import sys
    from claude_mux.tui import HeimsenseApp, ConfigManager

    class _TestApp(HeimsenseApp):
        def on_mount(self):
            super().on_mount()
            self.set_timer(0.5, self.exit)

    cm = ConfigManager()
    app = _TestApp(cm)
    try:
        app.run()
        click.echo("✅ TUI started OK")
    except Exception as e:
        click.echo(f"❌ TUI crash: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

_STATUSLINE_SCRIPT = """\
#!/bin/sh
# claude-mux statusline — rich status line via `cm statusline`.
# Reads JSON from Claude Code on stdin (model, rate_limits, context_window, cost).
# Installed by: claude-mux init
exec claude-mux statusline
"""


# ---------------------------------------------------------------------------
# statusline helpers
# ---------------------------------------------------------------------------

def _compute_usage_windows(claude_mux_dir) -> list[str]:
    """Compute 5h and 7d token-usage summaries from usage.log.

    Returns list of strings like ["5h 12%", "7d 34%"] using cached rate-limit
    headers as denominator. Falls back to raw token counts ("5h 45k") if no
    limit is known.
    """
    import json as _json
    import time as _time
    from pathlib import Path as _Path

    usage_log = _Path(claude_mux_dir) / "usage.log"
    rl_file = _Path(claude_mux_dir) / "rate-limits.json"

    if not usage_log.exists():
        return []

    now = int(_time.time())
    w5h = now - 5 * 3600
    w7d = now - 7 * 24 * 3600

    tokens_5h = tokens_7d = 0
    try:
        with open(usage_log) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = _json.loads(line)
                    ts = e.get("ts", 0)
                    total = e.get("in", 0) + e.get("out", 0)
                    if ts >= w5h:
                        tokens_5h += total
                    if ts >= w7d:
                        tokens_7d += total
                except (_json.JSONDecodeError, TypeError):
                    continue
    except OSError:
        return []

    # Try to get limit from cached rate-limit headers (API key users)
    # Fall back to configurable defaults (OAuth/Max users)
    # Defaults: Claude Max ≈ 2M tokens/5h, 30M tokens/7d
    DEFAULT_LIMIT_5H = 2_000_000
    DEFAULT_LIMIT_7D = 30_000_000

    limit_5h = DEFAULT_LIMIT_5H
    limit_7d = DEFAULT_LIMIT_7D
    try:
        rl = _json.loads(rl_file.read_text())
        # Anthropic rate-limit headers give per-minute limit — scale to windows
        per_min = rl.get("tokens_limit", 0)
        if per_min:
            limit_5h = per_min * 300   # 5h = 300 minutes
            limit_7d = per_min * 10080  # 7d = 10080 minutes
    except (OSError, _json.JSONDecodeError, TypeError):
        pass

    parts = []
    for label, used, limit in [("5h", tokens_5h, limit_5h), ("7d", tokens_7d, limit_7d)]:
        pct = round(used / limit * 100) if limit > 0 else 0
        parts.append(f"{label} {pct}%")
    return parts


# ---------------------------------------------------------------------------
# statusline
# ---------------------------------------------------------------------------

@cli.command("statusline")
def cmd_statusline():
    """Format Claude Code status JSON from stdin into a compact status line.

    Claude Code pipes a JSON payload to this command via the statusLine setting.
    Output: active-sub · model · 5h XX% · 7d XX% · ctx XX%

    Installed automatically by: claude-mux init
    """
    import sys
    import json as _json
    from pathlib import Path

    CLAUDE_MUX_DIR = Path.home() / ".claude-mux"

    # Read JSON from stdin (may be empty if Claude Code doesn't pipe anything)
    raw = sys.stdin.read().strip()

    # Active subscription name from cache file
    active_name = ""
    active_file = CLAUDE_MUX_DIR / "active-name"
    if active_file.exists():
        active_name = active_file.read_text().strip()

    if not raw:
        # No JSON — fall back to just the subscription name
        if active_name:
            click.echo(active_name)
        return

    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError:
        if active_name:
            click.echo(active_name)
        return

    # model: either a string (old format) or {"id": "...", "display_name": "..."} (new format)
    model_raw = data.get("model", "")
    if isinstance(model_raw, dict):
        model = model_raw.get("id", "")
    else:
        model = model_raw

    ctx = data.get("context_window", {})
    rate_limits = data.get("rate_limits", [])

    # Context window usage — support both old and new format
    ctx_pct = ""
    if "used_percentage" in ctx:
        # New format (Claude Code 2.1.110+): used_percentage is already computed
        ctx_pct = f"{ctx['used_percentage']}%"
    else:
        total = ctx.get("total_tokens", 0)
        used = ctx.get("used_tokens", 0)
        if total and total > 0:
            ctx_pct = f"{round(used / total * 100)}%"

    # Rate limit windows — prefer cached headers, else compute from usage.log
    window_parts = []
    if rate_limits:
        # Old Claude Code format: rate_limits array in stdin JSON
        for rl in rate_limits:
            window = rl.get("window", "")
            remaining = rl.get("tokens_remaining", 0)
            limit = rl.get("tokens_limit", 0)
            if window and limit and limit > 0:
                used_pct = round((1 - remaining / limit) * 100)
                window_parts.append(f"{window} {used_pct}%")
    else:
        # New format: build windows from usage.log + optional rate-limits.json
        window_parts = _compute_usage_windows(CLAUDE_MUX_DIR)

    # Build output parts
    parts = []
    if active_name:
        parts.append(f"🔌 {active_name}")
    if model:
        # Shorten model name: claude-sonnet-4-6 → sonnet-4-6
        short_model = model.replace("claude-", "")
        parts.append(short_model)
    parts.extend(window_parts)
    if ctx_pct:
        parts.append(f"ctx {ctx_pct}")

    click.echo(" · ".join(parts))

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

    # Fetch available models for all subscriptions
    cm2 = _cm()
    from claude_mux.sync import SyncManager as _SM
    sync2 = _SM(cm2)
    subs = cm2.subscriptions
    if subs:
        click.echo("\nFetching available models...")
        for s in subs:
            models = sync2.fetch_available_models(s["id"])
            if models:
                click.echo(f"  ✓ {s['name']}: {len(models)} models")
            else:
                click.echo(f"  ✗ {s['name']}: fetch failed (proxy not running or no API key)")

    click.echo("\nclaude-mux is ready. Restart Claude Code to see the status line.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    cli()


if __name__ == "__main__":
    main()
