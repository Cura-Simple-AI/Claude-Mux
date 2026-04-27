# Provider Setup Guide

Claude Mux supports 8 AI providers. All are used as a proxy for Claude Code.

## Overview

| # | Provider | Auth | Requires proxy | Direct URL |
|---|---|---|---|---|
| 1 | DeepSeek | Bearer token | Yes (pm2) | https://api.deepseek.com/v1 |
| 2 | Anthropic | Bearer token | Yes (pm2) | https://api.anthropic.com/v1 |
| 3 | OpenAI/ChatGPT | Bearer token | Yes (pm2) | https://api.openai.com/v1 |
| 4 | GitHub Copilot | gh auth token | Yes (pm2) | https://api.githubcopilot.com |
| 5 | Claude Max (OAuth) | OAuth token | No | api.anthropic.com (direct) |
| 5b | Claude Max (OAuth via proxy) | OAuth token | Yes (pm2) | localhost → api.anthropic.com |
| 6 | Gemini (Google) | API key | Yes (pm2) | https://generativelanguage.googleapis.com/... |
| 7 | z.ai | Bearer token | Yes (pm2) | https://api.z.ai/v1 |
| 8 | Custom | Bearer token | Yes (pm2) | (your URL) |

---

## 1. DeepSeek

**Get API key:** https://platform.deepseek.com/api_keys

```bash
export DEEPSEEK_API_KEY=sk-...
```

**Default model mapping:**
- haiku → `deepseek-chat`
- sonnet → `deepseek-chat`
- opus → `deepseek-reasoner`

**Setup in TUI:**
1. `+` → name: deepseek → select `1 DeepSeek`
2. API Key env: `DEEPSEEK_API_KEY`
3. Press Enter / Activate

---

## 2. Anthropic (direct)

**Get API key:** https://console.anthropic.com/settings/keys

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

**Model mapping:** Use Anthropic model IDs directly (e.g. `claude-sonnet-4-6`).

**Setup in TUI:**
1. `+` → name: anthropic → select `2 Anthropic`
2. API Key env: `ANTHROPIC_API_KEY`

---

## 3. OpenAI / ChatGPT

**Get API key:** https://platform.openai.com/api-keys

```bash
export OPENAI_API_KEY=sk-...
```

**Default model mapping:**
- haiku → `gpt-4o-mini`
- sonnet → `gpt-4o`
- opus → `o1-mini`

---

## 4. GitHub Copilot

**Requirements:** GitHub Copilot subscription + GitHub CLI installed

```bash
# Install GitHub CLI
brew install gh  # macOS
apt install gh   # Ubuntu

# Log in
gh auth login
```

Heimsense automatically fetches the token via `gh auth token` — no manual API key needed.

**Default model mapping (fetched from API):**
- haiku → `claude-haiku-4.5`
- sonnet → `claude-sonnet-4.6`
- opus → `claude-opus-4.7`

Available models are updated automatically when the subscription is added.

---

## 5. Claude Max (OAuth) — Recommended

**Requirements:** Claude Max subscription (Pro+)

**No proxy needed** — Claude Code communicates directly with api.anthropic.com.

**Setup in TUI:**
1. `+` → name: claude-max → select `5 Claude (OAuth)`
2. TUI automatically starts the OAuth flow:
   - Press `Open browser` or copy-paste the URL
   - Log in to claude.ai and authorize
   - Copy-paste the authorization code back into the TUI
3. Token is saved and valid for ~1 year

**Benefits:**
- No pm2/proxy overhead
- Direct to Anthropic — no middleman
- Uses your Claude Max rate limit

**Token expired?**
- Press `R` (Reauth) to renew the token via OAuth flow
- Happens automatically on 401 OAuth error

---

## 5b. Claude Max (OAuth via proxy)

Same OAuth token as #5, but traffic is routed through a local pm2 proxy instead of going directly to `api.anthropic.com`. This gives you request logging, failover, and the same subscription-management workflow as other proxy-based providers.

**When to use this instead of #5:**
- You want to log all API traffic (see `cm logs <name>`)
- You want automatic failover to another provider if Claude Max is down
- You need to inspect requests for debugging

**Requirements:** Claude Max subscription + OAuth token from `#5` already set up.

**Setup in TUI:**
1. `+` → name: e.g. `claude-max-proxy` → select `5b Claude (OAuth via proxy)`
2. API Key env: the same env var holding your OAuth token (e.g. `CLAUDE_CODE_OAUTH_TOKEN`)
3. Press Enter / Activate — proxy starts automatically via pm2

**How it works:**

```
Claude Code → http://localhost:<port> → proxy → api.anthropic.com
                                           ↑
                              Injects: Authorization: Bearer <oauth_token>
                              Forwards: all Claude Code headers unchanged
```

The proxy forwards all request headers from Claude Code transparently (including `anthropic-beta`, `anthropic-version`, etc.) and injects `Authorization: Bearer <token>` if the client doesn't supply auth.

**View logs:**
```bash
claude-mux logs claude-max-proxy
pm2 logs claude-max-proxy
```

---

## 6. Gemini (Google)

**Get API key:** https://aistudio.google.com/app/apikey

```bash
export GEMINI_API_KEY=AI...
```

**Default model mapping:**
- haiku → `gemini-2.0-flash`
- sonnet → `gemini-2.0-pro`
- opus → `gemini-2.5-pro`

**Endpoint:** `https://generativelanguage.googleapis.com/v1beta/openai`

---

## 7. z.ai

**Get API key:** https://www.z.ai/

```bash
export Z_AI_API_KEY=...
```

---

## 8. Custom (OpenAI-compatible)

Any OpenAI-compatible proxy or endpoint.

```bash
export CUSTOM_API_KEY=...
```

**Provide:**
- Provider URL: e.g. `https://my-proxy.example.com/v1`
- API Key env variable name
- Model mappings (haiku/sonnet/opus → your model IDs)

---

## Failover order

Heimsense tries providers in the order they were added. Configure failover priority by adding providers in the desired order.

See [FAILOVER.md](FAILOVER.md) for details.
