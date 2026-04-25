# CLI Reference

Heimsense provides a full command-line interface that mirrors every TUI action. Scripts, CI pipelines, and automation use the CLI; the TUI is a convenience layer on top of the same logic.

**Design principles:** [CLI Guidelines](https://clig.dev/) — subcommands, `--json` for scripting, stderr for errors, exit codes, `--help` on every command.

---

## Global flags

| Flag | Short | Description |
|---|---|---|
| `--json` | `-j` | Output as JSON (machine-readable) |
| `--quiet` | `-q` | Suppress non-essential output |
| `--no-color` | | Disable color output |
| `--config-dir PATH` | | Override config directory (default: `~/.heimsense/`) |
| `--version` | `-V` | Show version and exit |
| `--help` | `-h` | Show help and exit |

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | General error |
| `2` | Usage / argument error |
| `3` | Subscription not found |
| `4` | Health check failed |

---

## Commands

### `heimsense list`

List all subscriptions.

```bash
heimsense list
heimsense list --json
```

**Output columns:** ID, Name, Provider, Status, Port, Active

**JSON output:**
```json
[
  {
    "id": "abc123",
    "name": "claude-max",
    "provider": "claude-max",
    "auth_type": "oauth",
    "port": null,
    "active": true,
    "status": "oauth"
  }
]
```

**TUI equivalent:** Main table

---

### `heimsense add`

Add a new subscription interactively or non-interactively.

```bash
# Interactive wizard (same as TUI `+`)
heimsense add

# Non-interactive
heimsense add \
  --name deepseek \
  --provider deepseek \
  --api-key-env DEEPSEEK_API_KEY \
  --port 18082

# Claude Max (OAuth) — opens browser for OAuth flow
heimsense add --name claude-max --provider claude-max
```

**Flags:**
| Flag | Description |
|---|---|
| `--name NAME` | Subscription name |
| `--provider PROVIDER` | Provider preset (deepseek, anthropic, openai, copilot, claude-max, gemini, z-ai, custom) |
| `--api-key-env VAR` | Environment variable holding the API key |
| `--port PORT` | Proxy port (auto-assigned if omitted) |
| `--url URL` | Base URL (custom provider only) |
| `--model-haiku MODEL` | Override haiku model alias |
| `--model-sonnet MODEL` | Override sonnet model alias |
| `--model-opus MODEL` | Override opus model alias |

**TUI equivalent:** `+` (Add wizard)

---

### `heimsense activate <name>`

Activate a subscription as the default for Claude Code.

```bash
heimsense activate claude-max
heimsense activate deepseek --json
```

Updates `~/.claude/settings.json` with the correct env vars.

**TUI equivalent:** `Enter` (Set Default)

---

### `heimsense start <name>`

Start the proxy for a subscription (pm2).

```bash
heimsense start deepseek
```

**TUI equivalent:** `s` (Start)

---

### `heimsense stop <name>`

Stop the proxy for a subscription.

```bash
heimsense stop deepseek
heimsense stop --all
```

**Flags:**
| Flag | Description |
|---|---|
| `--all` | Stop all running proxies |

**TUI equivalent:** `s` (Stop)

---

### `heimsense test [name]`

Run a health check on a subscription.

```bash
# Test active subscription
heimsense test

# Test specific
heimsense test deepseek
heimsense test deepseek --json
```

**JSON output:**
```json
{
  "name": "deepseek",
  "port": 18082,
  "http_code": 200,
  "elapsed_ms": 342,
  "model": "deepseek-chat",
  "ok": true
}
```

Exit code `4` if health check fails.

**TUI equivalent:** `t` (Test)

---

### `heimsense edit <name>`

Edit a subscription's settings interactively or via flags.

```bash
# Interactive
heimsense edit deepseek

# Non-interactive
heimsense edit deepseek --api-key-env NEW_KEY_VAR
heimsense edit deepseek --port 18090
```

**TUI equivalent:** `e` (Edit)

---

### `heimsense delete <name>`

Delete a subscription (prompts for confirmation unless `--yes`).

```bash
heimsense delete deepseek
heimsense delete deepseek --yes   # skip confirmation
```

**TUI equivalent:** `d` (Delete)

---

### `heimsense status [name]`

Show status of one or all subscriptions.

```bash
heimsense status
heimsense status claude-max
heimsense status --json
```

**TUI equivalent:** Main table (refresh `r`)

---

### `heimsense failover`

Trigger a manual failover check on the active subscription.

```bash
heimsense failover
heimsense failover --json
```

Tests active subscription; if failing, switches to the next available.

**TUI equivalent:** `x` (Failover check)

---

### `heimsense failover-log`

Show the failover event log.

```bash
heimsense failover-log
heimsense failover-log --tail 20
heimsense failover-log --json
```

**Flags:**
| Flag | Description |
|---|---|
| `--tail N` | Show last N entries (default: all) |
| `--follow` / `-f` | Follow log in real time |

**TUI equivalent:** `L` (Failover Log modal)

---

### `heimsense logs <name>`

Show PM2 proxy logs for a subscription.

```bash
heimsense logs deepseek
heimsense logs deepseek --tail 50
heimsense logs deepseek --follow
```

**TUI equivalent:** `l` (PM2 Logs)

---

### `heimsense force-model <name> <model>`

Override all model aliases to a single model.

```bash
heimsense force-model deepseek deepseek-chat
heimsense force-model claude-max claude-opus-4-6
heimsense force-model deepseek --reset   # remove override
```

**TUI equivalent:** `f` (Force model)

---

### `heimsense config`

Show configuration paths and active settings.

```bash
heimsense config
heimsense config --json
```

**Output:**
```
Config dir:    ~/.heimsense/
Subscriptions: ~/.heimsense/subscriptions.json
Failover log:  ~/.heimsense/failover.log
Claude config: ~/.claude/settings.json
Active sub:    claude-max (oauth, direct)
```

---

## Scripting examples

### Switch provider from a shell script

```bash
#!/bin/bash
# Switch to deepseek if DEEPSEEK_API_KEY is set
if [ -n "$DEEPSEEK_API_KEY" ]; then
  heimsense activate deepseek --quiet
fi
```

### Health check in CI

```bash
# Fail CI if active provider is down
heimsense test --json | python3 -c "
import sys, json
r = json.load(sys.stdin)
sys.exit(0 if r['ok'] else 4)
"
```

### List active subscription name

```bash
ACTIVE=$(heimsense list --json | python3 -c "
import sys, json
subs = json.load(sys.stdin)
active = next((s['name'] for s in subs if s['active']), None)
print(active or '')
")
echo "Active: $ACTIVE"
```

### Automate failover on 429

```bash
# In a loop — switch providers until one works
heimsense test || heimsense failover
```

---

## TUI ↔ CLI parity table

| TUI key | CLI command | Description |
|---|---|---|
| `r` | `heimsense status` | Refresh / show status |
| `+` | `heimsense add` | Add subscription |
| `Enter` | `heimsense activate <name>` | Set as default |
| `s` (start) | `heimsense start <name>` | Start proxy |
| `s` (stop) | `heimsense stop <name>` | Stop proxy |
| `t` | `heimsense test [name]` | Health check |
| `e` | `heimsense edit <name>` | Edit subscription |
| `d` | `heimsense delete <name>` | Delete subscription |
| `f` | `heimsense force-model <name> <model>` | Force model |
| `l` | `heimsense logs <name>` | PM2 logs |
| `L` | `heimsense failover-log` | Failover log |
| `x` | `heimsense failover` | Manual failover |
| `h` | `heimsense --help` | Help |
| `q` | (exit TUI) | — |

---

## Development rule

> **TUI and CLI must stay in sync.** Every new TUI feature gets a CLI equivalent in the same PR. Every CLI command gets documented in this file and in `--help`.
