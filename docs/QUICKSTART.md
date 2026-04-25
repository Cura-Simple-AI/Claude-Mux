# claude-mux — Quickstart

claude-mux is a terminal UI for managing AI provider subscriptions. Switch between Claude Max, DeepSeek, Copilot, Gemini, and other OpenAI-compatible proxies — with automatic failover.

## Installation

### pip (recommended)

```bash
pip install claude-mux
claude-mux
```

### pipx (isolated environment)

```bash
pipx install claude-mux
claude-mux
```

### From source

```bash
git clone https://github.com/cura-ai/claude-mux
cd claude-mux
pip install -e .
claude-mux
```

### Standalone (no install)

```bash
pip install textual rich psutil
python3 claude-mux.py --tui
```

## Requirements

| Dependency | Version | Purpose |
|---|---|---|
| Python | 3.11+ | Runtime |
| textual | 0.80+ | TUI framework |
| rich | 13.0+ | Formatting |
| psutil | 5.9+ | Process monitoring |
| pm2 | latest | Proxy lifecycle (optional) |
| claude CLI | latest | OAuth flow (Claude Max only) |

Install pm2: `npm install -g pm2`  
Install claude: `npm install -g @anthropic-ai/claude-code`

claude-mux automatically checks for missing Python packages at startup and offers to install them.

## First start

```
┌─────────────────────────────────────────────────────────────────┐
│  claude-mux TUI                                    0m 0s         │
├──────────────────────────┬──────────────────────────────────────┤
│                          │                                      │
│  No subscriptions        │  No subscription selected.           │
│  Press + to add one      │                                      │
│                          │  Press + to add your first           │
│                          │  AI provider subscription.           │
│                          │                                      │
├──────────────────────────┴──────────────────────────────────────┤
│ r Refresh  + Add  h Help  q Quit                                │
└─────────────────────────────────────────────────────────────────┘
```

**Steps:**
1. Press `+` to open the Add wizard
2. Enter a name (e.g. "claude-max" or "deepseek")
3. Select provider with `1-8` or `j`/`k` + `Enter`
4. Follow provider-specific setup (API key or OAuth)
5. Press `Enter` to activate as default

## Typical workflow

```bash
# Start TUI
claude-mux

# Add DeepSeek (steps in TUI):
# + → name: deepseek → 1 (DeepSeek) → API key: $DEEPSEEK_API_KEY → Enter (activate)

# Use Claude with the selected provider:
claude -p "Write a haiku about Python"

# Check status
claude models  # shows active model mapping
```

## Configuration files

| File | Purpose |
|---|---|
| `~/.claude-mux/subscriptions.json` | Subscription data |
| `~/.claude-mux/.env` | Active provider env vars |
| `~/.claude-mux/failover.log` | Failover events |
| `~/.claude/settings.json` | Claude Code env config (updated by claude-mux) |

## Next steps

- [PROVIDERS.md](PROVIDERS.md) — Setup guide for all 8 providers
- [KEYBOARD.md](KEYBOARD.md) — All keyboard shortcuts including iPhone
- [FAILOVER.md](FAILOVER.md) — Automatic failover and auto-resume
