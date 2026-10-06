# CLI Reference

claude-mux provides a full command-line interface that mirrors every TUI action. Scripts, CI pipelines, and automation use the CLI; the TUI is a convenience layer on top of the same logic.

**Design principles:** [CLI Guidelines](https://clig.dev/) — subcommands, `--json` for scripting, stderr for errors, exit codes, `--help` on every command.

---

## Global flags

| Flag | Short | Description |
|---|---|---|
| `--json` | `-j` | Output as JSON (machine-readable) |
| `--quiet` | `-q` | Suppress non-essential output |
| `--no-color` | | Disable color output |
| `--config-dir PATH` | | Override config directory (default: `~/.claude-mux/`) |
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

### `claude-mux list`

List all subscriptions.

```bash
claude-mux list
claude-mux list --json
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

### `claude-mux add`

Add a new subscription interactively or non-interactively.

```bash
# Interactive wizard (same as TUI `+`)
claude-mux add

# Non-interactive
claude-mux add \
  --name deepseek \
  --provider deepseek \
  --api-key-env DEEPSEEK_API_KEY \
  --port 18082

# Claude Max (OAuth) — opens browser for OAuth flow
claude-mux add --name claude-max --provider claude-max
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

### `claude-mux activate <name>`

Activate a subscription as the default for Claude Code.

```bash
claude-mux activate claude-max
claude-mux activate deepseek --json
```

Updates `~/.claude/settings.json` with the correct env vars.

**TUI equivalent:** `Enter` (Set Default)

---

### `claude-mux start <name>`

Start the proxy for a subscription (pm2).

```bash
claude-mux start deepseek
```

**TUI equivalent:** `s` (Start)

---

### `claude-mux stop <name>`

Stop the proxy for a subscription.

```bash
claude-mux stop deepseek
claude-mux stop --all
```

**Flags:**
| Flag | Description |
|---|---|
| `--all` | Stop all running proxies |

**TUI equivalent:** `s` (Stop)

---

### `claude-mux test [name]`

Run a health check on a subscription.

```bash
# Test active subscription
claude-mux test

# Test specific
claude-mux test deepseek
claude-mux test deepseek --json
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

### `claude-mux edit <name>`

Edit a subscription's settings interactively or via flags.

Provider URL and auth type are **locked after creation** — they define what the subscription is. To use a different provider, add a new subscription.

```bash
# Interactive
claude-mux edit deepseek

# Non-interactive
claude-mux edit deepseek --api-key-env NEW_KEY_VAR
claude-mux edit deepseek --port 18090
```

**Editable flags:**
| Flag | Description |
|---|---|
| `--name NAME` | Rename the subscription |
| `--api-key-env VAR` | Change API key env variable |
| `--port PORT` | Change proxy port |
| `--model-haiku MODEL` | Override haiku model alias |
| `--model-sonnet MODEL` | Override sonnet model alias |
| `--model-opus MODEL` | Override opus model alias |

**TUI equivalent:** `e` (Edit)

---

### `claude-mux delete <name>`

Delete a subscription (prompts for confirmation unless `--yes`).

```bash
claude-mux delete deepseek
claude-mux delete deepseek --yes   # skip confirmation
```

**TUI equivalent:** `d` (Delete)

---

### `claude-mux status [name]`

Show status of one or all subscriptions.

```bash
claude-mux status
claude-mux status claude-max
claude-mux status --json
```

**TUI equivalent:** Main table (auto-refreshes every 5s)

---

### `claude-mux failover`

Trigger a manual failover check on the active subscription.

```bash
claude-mux failover
claude-mux failover --json
```

Tests active subscription; if failing, switches to the next available.

**TUI equivalent:** `x` (Failover check)

---

### `claude-mux failover-log`

Show the failover event log.

```bash
claude-mux failover-log
claude-mux failover-log --tail 20
claude-mux failover-log --json
```

**Flags:**
| Flag | Description |
|---|---|
| `--tail N` | Show last N entries (default: all) |
| `--follow` / `-f` | Follow log in real time |

**TUI equivalent:** `L` (Failover Log modal)

---

### `claude-mux logs <name>`

Show PM2 proxy logs for a subscription.

```bash
claude-mux logs deepseek
claude-mux logs deepseek --tail 50
claude-mux logs deepseek --follow
```

**TUI equivalent:** `l` (PM2 Logs)

---

### `claude-mux force-model <name> <model>`

Override all model aliases to a single model.

```bash
claude-mux force-model deepseek deepseek-chat
claude-mux force-model claude-max claude-opus-4-6
claude-mux force-model deepseek --reset   # remove override
```

**TUI equivalent:** `f` (Force model)

---

### `claude-mux config`

Show configuration paths and active settings.

```bash
claude-mux config
claude-mux config --json
```

**Output:**
```
Config dir:    ~/.claude-mux/
Subscriptions: ~/.claude-mux/subscriptions.json
Failover log:  ~/.claude-mux/failover.log
Claude config: ~/.claude/settings.json
Active sub:    claude-max (oauth, direct)
```

---

### `claude-mux active`

Print the name of the subscription currently active in Claude Code.

Reads `~/.claude/settings.json` and matches against saved subscriptions using
auth-type-specific logic (OAuth token, direct provider URL, or proxy port).

```bash
claude-mux active          # print name, exit 0; or exit 1 if no match
claude-mux active --json   # {"active": "deepseek", "id": "de3a7e01-..."}
```

**Exit codes:** `0` = match found · `1` = no match (settings not from any saved subscription)

---

### `claude-mux statusline`

Read a JSON status payload from Claude Code on stdin and print a formatted status line.

This command is called by the statusline script installed by `claude-mux init`. It is not normally invoked directly.

```bash
# Claude Code pipes JSON to this command automatically via statusLine setting
echo '{"model":"claude-sonnet-4-6","rate_limits":[...]}' | claude-mux statusline
```

**Output format:**
```
🔌 claude-max · claude-sonnet-4-6 · 5h 23% · 7d 41% · ctx 12%
```

- **Subscription name** — active subscription
- **Model** — model currently in use
- **5h %** — input token usage in the 5-hour window
- **7d %** — input token usage in the 7-day window
- **ctx %** — context window used this turn

Usage is color-coded: green < 50%, yellow 50–79%, red ≥ 80%.

**TUI equivalent:** Status bar (bottom of TUI)

---

### `claude-mux init`

First-time setup: installs the Claude Code status line integration.

```bash
claude-mux init           # install (safe to re-run — skips if already set)
claude-mux init --force   # overwrite existing statusLine setting
```

**What it does:**
1. Writes `~/.claude-mux/bin/statusline.sh` — a script that prints the active subscription name
2. Adds a `statusLine` entry to `~/.claude/settings.json` so Claude Code shows the name in its status bar

**After running:** restart Claude Code. The status bar shows the active subscription name,
updated every time you activate a subscription with `claude-mux activate`.

### `claude-mux session list`

Lists Claude Code sessions running in tmux panes. Read-only.

```bash
claude-mux session list                    # table
claude-mux session list --json             # machine-readable
claude-mux session list --container dev    # tmux inside a running container (docker exec)
claude-mux session list --container dev --container-user vscode   # tmux server of another user
```

| Option | Description |
|---|---|
| `--container NAME` | Inspect tmux inside a running container (`docker exec`) |
| `--container-user USER` | With `--container`: the user owning the tmux server (`docker exec -u`); tmux servers are per user |
| `--json` | Output as JSON |

Each session is mapped **tmux pane → claude pid → `~/.claude/sessions/<pid>.json`** and shows
the session id, name, cwd, original command line, model, context % and state:

| State | Meaning |
|---|---|
| `idle` | waiting for input |
| `working` | a turn is running (registry status `busy` or "esc to interrupt" on screen) |
| `dialog` | a dialog or permission prompt is open ("Enter to select", "Enter to confirm" or "Esc to cancel" at the bottom of the pane, or registry status `waiting`) |

Panes running `claude attach <job>` (background sessions) are marked with `*`; the pid shown is
the background process.

---

### `claude-mux session restart`

Exits a Claude Code session and resumes the same conversation in the same pane.

```bash
claude-mux session restart main:0.1                    # one pane
claude-mux session restart main:0.1 --model opus       # expanded to a full model id
claude-mux session restart main:0.1 --profile work     # `activate work` first
claude-mux session restart --self                      # the session calling the command
claude-mux session restart --all --match '^agent-'     # one at a time
```

| Option | Description |
|---|---|
| `--model ID` | Model for the resumed session. Short aliases are expanded (see below) |
| `--profile NAME` | Run `activate NAME` before restarting. Host only: refused with `--container`, because `activate` writes the host's config, which the container never sees |
| `--nudge TEXT` | Message sent after resume. Default asks the agent to recreate its scheduled loops |
| `--no-nudge` | Send no message after resume |
| `--force` | Do not wait for idle; send Escape first. Open dialogs are still refused |
| `--timeout SEC` | Seconds to wait for each step (default 300) |
| `--self` | Restart the calling session's pane via a detached helper (`tmux run-shell -b`). Not with `--container` |
| `--delay SEC` | With `--self`: seconds before the helper starts (default 5) |
| `--all` | Restart every session, one at a time. The caller's own pane is skipped |
| `--match REGEX` | With `--all`: only sessions whose target, name or cwd matches |
| `--container NAME` | Operate on tmux inside a running container |
| `--container-user USER` | With `--container`: the user owning the tmux server (`docker exec -u`); tmux servers are per user |
| `--json` | Output as JSON |

**Sequence and safety rules:**

1. **Refuse if a dialog is open.** Text typed into a selection dialog picks an answer, so a pane
   in the `dialog` state (see above) is reported as `SKIPPED` — even with `--force`.
2. **Wait until idle** (unless `--force`, which sends Escape first).
3. **Exit.** `/exit` is typed, then Enter is sent in a *separate* `send-keys` call. Exit dialogs:
   - worktree dialog → the option labelled **Keep**. An option labelled Remove is never chosen;
     if there is no Keep option the restart fails and the dialog is left untouched.
   - "Exit and stop tasks" → option 1.
   - any other dialog → the restart fails without answering.
4. **Background sessions** (`claude attach`): restarted with `claude respawn <job>`, which
   resumes the same conversation (killing the background process does not work — the daemon
   starts it again and the attach view stays open). `remain-on-exit` is set first; if the
   `claude attach` client exits, the pane is re-attached with `tmux respawn-pane`. Steps 5-6
   do not apply, and `--model` is refused because `claude respawn` keeps the session's settings.
5. **Relaunch** with the original argv (`--agent`, `--worktree`, `--permission-mode`, `--model`, …)
   minus `--resume`, `--session-id`, `--fork-session` and `--continue`, plus `--resume <sessionId>`.
   A positional prompt from the original command line is dropped — with `--resume` it would be
   sent to the agent again as a new message. To tell option values from the prompt, the arity of
   every Claude Code option is known, including hidden ones (`--plan-mode-required`,
   `--agent-id`, `--channels a b`, `--rc [name]`, …). An unknown option is assumed to take one
   value unless the next argument starts with `-`, and the restart reports a warning for it.
   - Pane runs a shell: wait for the shell, then type the command. A bare `--worktree` (no name)
     is dropped and the command is prefixed with `cd <session cwd> &&`: a bare `--worktree`
     creates a new worktree on every launch, and the shell is still in the launch directory.
     `--worktree <name>` is kept as is.
   - Claude Code is the pane process itself: `remain-on-exit` is set before exit and the pane is
     relaunched with `tmux respawn-pane` in the session's cwd (`--worktree` is dropped because
     the cwd already is the worktree).
6. **Verify:** a new pid, the same `sessionId` in the registry and a visible status line.
7. **Nudge:** the text, then Enter in a separate call. Session-scoped cron jobs do not survive a
   resume, so the default nudge asks the agent to recreate its scheduled loops.

Worktrees are never removed.

**Model aliases.** A short alias such as `opus` can resolve to an older model under a different
profile, so aliases are expanded to full ids before relaunch (this also applies to an alias in the
original command line). Defaults: `opus` → `claude-opus-5-5`, `sonnet` → `claude-sonnet-5-5`,
`haiku` → `claude-haiku-4-5-20251001`. Override or extend them in `~/.claude-mux/subscriptions.json`:

```json
{ "session_model_aliases": { "opus": "claude-opus-5-5" } }
```

Any other model name that does not start with `claude-` produces a warning.

**`--self`.** An agent cannot exit its own session synchronously. `--self` schedules a detached
helper that sleeps `--delay` seconds and then runs `session restart <pane>` with the same
options. The pane is found by walking up the process tree to the Claude process, which also works
for background sessions shown through `claude attach` (they have no `$TMUX_PANE`); `$TMUX_PANE`
is the fallback. The helper runs the same claude-mux package as the caller
(log: `~/.claude-mux/session-restart.log`). End the turn right after calling it so the session
becomes idle.

**`--all` output:** one line per session — `OK`, `SKIPPED` (dialog / own pane) or `FAILED`.

Exit codes: `0` OK, `1` failed (any session with `--all`), `2` usage, `4` skipped (dialog open).

---

## Scripting examples

### Switch provider from a shell script

```bash
#!/bin/bash
# Switch to deepseek if DEEPSEEK_API_KEY is set
if [ -n "$DEEPSEEK_API_KEY" ]; then
  claude-mux activate deepseek --quiet
fi
```

### Health check in CI

```bash
# Fail CI if active provider is down
claude-mux test --json | python3 -c "
import sys, json
r = json.load(sys.stdin)
sys.exit(0 if r['ok'] else 4)
"
```

### List active subscription name

```bash
# Simple (exits 1 if nothing matches)
claude-mux active

# JSON
claude-mux active --json
# → {"active": "deepseek", "id": "de3a7e01-..."}
```

### Automate failover on 429

```bash
# In a loop — switch providers until one works
claude-mux test || claude-mux failover
```

---

## TUI ↔ CLI parity table

| TUI key | CLI command | Description |
|---|---|---|
| `r` | — | Reload TUI (hotload restart) |
| `+` | `claude-mux add` | Add subscription |
| `Enter` | `claude-mux activate <name>` | Set as default |
| `s` (start) | `claude-mux start <name>` | Start proxy |
| `s` (stop) | `claude-mux stop <name>` | Stop proxy |
| `t` | `claude-mux test [name]` | Health check |
| `e` | `claude-mux edit <name>` | Edit subscription |
| `e` on `*current` | — | Save current settings as new subscription |
| `d` | `claude-mux delete <name>` | Delete subscription |
| `f` | `claude-mux force-model <name> <model>` | Force model |
| `l` | `claude-mux logs <name>` | PM2 logs |
| `L` | `claude-mux failover-log` | Failover log |
| `x` | `claude-mux failover` | Manual failover |
| `h` | `claude-mux --help` | Help |
| `q` | (exit TUI) | — |
| — | `claude-mux active` | Print active subscription name |
| — | `claude-mux init` | Install Claude Code status line |
| — | `claude-mux session list` | List Claude Code sessions in tmux (TUI view may follow) |
| — | `claude-mux session restart` | Exit and resume sessions safely (TUI action may follow) |
| — | `claude-mux statusline` | Format status line from Claude Code JSON (called by statusline.sh) |

---

## Development rule

> **TUI and CLI must stay in sync.** Every new TUI feature gets a CLI equivalent in the same PR. Every CLI command gets documented in this file and in `--help`.
