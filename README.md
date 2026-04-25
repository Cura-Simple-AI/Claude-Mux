# claude-mux

**Use any LLM inside Claude Code. Automatically.**

claude-mux lets you keep working in Claude Code while routing requests to any provider — OpenAI, Anthropic, DeepSeek, Copilot, Gemini, and more.

No rewrites. No context switching. No limits blocking you.

---

## Why

Claude Code is great. But:

- you hit rate limits
- you run out of tokens
- you want to use other models
- you're locked into one provider

claude-mux removes all of that.

---

## What it does

- Route Claude Code to any LLM provider
- Automatic failover when limits are hit
- Switch between Anthropic subscriptions (Claude Max / Pro)
- Use your existing tools (Copilot, OpenAI, DeepSeek, etc.)
- Zero workflow changes

---

## How it works

claude-mux sits between Claude Code and your providers via a local proxy.

It multiplexes requests across providers and automatically switches when needed.

You stay in Claude Code.  
claude-mux handles the rest.

---

## Install

```bash
pip install claude-mux
```

Or with pipx (isolated environment):

```bash
pipx install claude-mux
```

---

## Usage

```bash
claude-mux
```

That's it. A TUI opens where you configure providers.

Claude Code will now:
- use your configured providers
- switch automatically on failure
- keep working without interruption

---

## Example

You start coding in Claude Code.

```
Claude hits token limit
  → claude-mux switches to DeepSeek
DeepSeek fails
  → claude-mux switches to OpenAI
You keep working. No errors. No manual switching.
```

---

## Supported providers

| Provider | Auth |
|---|---|
| Claude Max (OAuth) | OAuth — direct, no proxy |
| Anthropic | API key |
| OpenAI / ChatGPT | API key |
| GitHub Copilot | `gh auth token` |
| DeepSeek | API key |
| Gemini | API key |
| z.ai | API key |
| Custom (OpenAI-compatible) | API key |

---

## CLI

```bash
claude-mux            # Start TUI
claude-mux --version  # Show version
claude-mux --help     # Help
```

Full CLI reference (non-interactive mode): [docs/CLI.md](docs/CLI.md)

---

## Configuration

All configuration lives in `~/.claude-mux/`:

| File | Purpose |
|---|---|
| `subscriptions.json` | Provider configurations |
| `.env` | Active provider env vars |
| `failover.log` | Failover event log |

Claude Code configuration is written to `~/.claude/settings.json` automatically.

---

## Keyboard shortcuts (TUI)

| Key | Action |
|---|---|
| `+` | Add provider |
| `Enter` | Activate as default |
| `s` | Start / Stop proxy |
| `t` | Test connection |
| `x` | Manual failover |
| `h` | Help |
| `q` | Quit |

Works on iPhone via SSH (Blink Shell, a-Shell) — no Ctrl/F-keys/arrows needed.  
Full reference: [docs/KEYBOARD.md](docs/KEYBOARD.md)

---

## Docs

- [QUICKSTART.md](docs/QUICKSTART.md) — Installation and first steps
- [PROVIDERS.md](docs/PROVIDERS.md) — Setup guide for all 8 providers
- [KEYBOARD.md](docs/KEYBOARD.md) — All keyboard shortcuts
- [FAILOVER.md](docs/FAILOVER.md) — Automatic failover and auto-resume
- [CLI.md](docs/CLI.md) — Full CLI reference

---

## Roadmap

- Smarter routing (latency, cost, quality)
- Usage-based auto-switch rules (token budget, time-of-day)
- Team configs
- Usage analytics
- Plugin system

---

## Philosophy

You shouldn't care which model you're using.

You should just be able to work.

---

## License

MIT — [LICENSE](LICENSE)

Built by [Cura AI](https://github.com/cura-ai)
