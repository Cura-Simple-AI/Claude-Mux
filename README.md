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
The proxy listens on `127.0.0.1` only and refuses requests from web pages, because it attaches
your API key or OAuth token to every request it forwards.

It multiplexes requests across providers and automatically switches when needed.

You stay in Claude Code.  
claude-mux handles the rest.

---

## Install

Requires Python 3.11 or newer.

> **Install from this repository.** The name `claude-mux` on PyPI belongs to a different,
> unrelated project, so `pip install claude-mux` does **not** install this tool.

With pipx (isolated environment, recommended):

```bash
pipx install git+https://github.com/Cura-Simple-AI/Claude-Mux.git
```

Or with pip:

```bash
pip install git+https://github.com/Cura-Simple-AI/Claude-Mux.git
```

Or run the install script (`--pipx` for an isolated environment, `--dev` for an editable install):

```bash
curl -sSL https://raw.githubusercontent.com/Cura-Simple-AI/Claude-Mux/main/install.sh | bash
```

Both `claude-mux` and the short alias `cm` are installed.

Optional tools:

| Tool | Needed for |
|---|---|
| `pm2` (`npm install -g pm2`) | Proxy providers (API key, Copilot, Gemini, OAuth via proxy) |
| `claude` CLI | Claude Max OAuth setup |
| `tmux` | `claude-mux session list` / `session restart` |
| `docker` | `--container` on the `session` commands |

---

## Quickstart

```bash
claude-mux init          # once: show the active subscription in Claude Code's status line
claude-mux               # open the TUI, press + to add a provider
```

The same from the command line, without the TUI:

```bash
export DEEPSEEK_API_KEY=...
claude-mux add -n deepseek -u https://api.deepseek.com/v1 -k DEEPSEEK_API_KEY
claude-mux start deepseek      # proxy providers must be running before they can be activated
claude-mux activate deepseek   # point Claude Code at it
claude-mux test                # real inference test of the active subscription
```

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

Every TUI action has a CLI equivalent, so everything can be scripted. Run `claude-mux` with no
subcommand to open the TUI; `cm` is a short alias.

```bash
claude-mux list                       # list subscriptions
claude-mux status [NAME]              # status of one or all subscriptions
claude-mux add -n NAME -u URL ...     # add a subscription
claude-mux edit NAME ...              # edit a subscription
claude-mux delete NAME                # delete a subscription
claude-mux activate NAME              # make it the default for Claude Code
claude-mux active                     # print the active subscription
claude-mux start NAME                 # start a proxy (pm2)
claude-mux stop [NAME | --all]        # stop proxies
claude-mux test [NAME] [TIER]         # real inference test (haiku | sonnet | opus)
claude-mux probe [NAME] [--quota]     # one inference request, or OAuth rate-limit usage
claude-mux models [NAME] [--refresh]  # cached model list
claude-mux blacklist NAME [MODEL]     # models excluded from tests
claude-mux force-model NAME [MODEL]   # force a model for a tier
claude-mux failover                   # manual failover check
claude-mux failover-log               # failover events
claude-mux logs NAME                  # pm2 proxy logs
claude-mux config                     # configuration paths and active settings
claude-mux session list|restart       # Claude Code sessions in tmux
claude-mux init                       # install the status line integration
claude-mux --version
claude-mux --help
```

Full reference with options, JSON output and exit codes: [docs/CLI.md](docs/CLI.md)

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

## Security

The proxy attaches your API key or OAuth token to every request it forwards, so it only serves
local, non-browser clients:

- It listens on `127.0.0.1` only. An empty host in `LISTEN_ADDR` also means loopback, never all
  interfaces.
- Requests with an `Origin` header are refused with `403`, so web pages cannot use the proxy.
- CORS preflights (`OPTIONS`) are refused with `405`, and no `Access-Control-Allow-Origin`
  header is sent.
- Requests whose `Host` header is not `localhost`, `127.0.0.1` or `::1` are refused with `403`,
  which blocks DNS-rebinding attacks.

**Behaviour change:** the proxy is no longer reachable from other machines. Restart running
proxies (`claude-mux stop --all`, then `claude-mux start NAME`) to apply this after upgrading.

---

## Keyboard shortcuts (TUI) and CLI equivalents

| Key | TUI action | CLI equivalent |
|---|---|---|
| `+` | Add provider | `claude-mux add` |
| `Enter` or `1`–`9` | Activate as default | `claude-mux activate NAME` |
| `s` | Start / Stop proxy | `claude-mux start NAME` / `claude-mux stop NAME` |
| `t` | Test (all tiers) | `claude-mux test [NAME]` |
| `e` | Edit provider (model maps, force-model) | `claude-mux edit NAME` (model maps), `claude-mux force-model NAME MODEL --tier TIER` (one tier) |
| `e` on `*current settings` | Save current settings as a new subscription | `claude-mux add` |
| `d` | Delete provider | `claude-mux delete NAME` |
| `l` | Proxy logs (pm2) | `claude-mux logs NAME` |
| `L` | Failover log | `claude-mux failover-log` |
| `x` | Manual failover check | `claude-mux failover` |
| `/` | Filter providers by name | `claude-mux list` |
| `h` or `?` | Help | `claude-mux --help` |
| `r` | Reload the TUI | — |
| `q` | Quit | — |

The **Reauth** button (OAuth providers) has no CLI equivalent. `claude-mux probe --quota`,
`models`, `blacklist`, `session`, `active` and `init` are CLI-only.

Works on iPhone via SSH (Blink Shell, a-Shell) — no Ctrl/F-keys/arrows needed.  
Full reference: [docs/KEYBOARD.md](docs/KEYBOARD.md)

---

## Docs

- [QUICKSTART.md](docs/QUICKSTART.md) — Installation and first steps
- [PROVIDERS.md](docs/PROVIDERS.md) — Setup guide for all providers
- [KEYBOARD.md](docs/KEYBOARD.md) — All keyboard shortcuts
- [FAILOVER.md](docs/FAILOVER.md) — Automatic failover and auto-resume
- [CLI.md](docs/CLI.md) — Full CLI reference
- [KNOWN_ISSUES.md](KNOWN_ISSUES.md) — Open bugs and limitations

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

Built by [Cura AI](https://github.com/Cura-Simple-AI)
