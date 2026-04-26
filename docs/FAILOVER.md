# Failover & Auto-resume

claude-mux monitors your active provider and automatically switches on failure.

## When does failover trigger?

Failover is triggered on these HTTP error codes:

| Code | Description | Action |
|---|---|---|
| 429 | Too Many Requests (rate limit) | Switch to next |
| 401 | Unauthorized (invalid token) | Switch to next |
| 403 | Forbidden | Switch to next |
| 503 | Service Unavailable | Switch to next |

HTTP 404 and other 4xx errors are **not** considered fatal — the proxy is running but the endpoint is missing.

Failover is **not** triggered on 200, 400, 422 — these are normal API responses.

## Failover flow

```
Active: claude-max (port 18080)
     ↓
[429 detected]
     ↓
Test deepseek (port 18081)
     ↓ works
Switch to deepseek
LOG: 2026-04-25 10:30:00  FROM=claude-max  TO=deepseek  REASON=HTTP 429
     ↓
[10-minute timer starts]
     ↓
Auto-resume: test claude-max again
     ↓ works
Switch back to claude-max
LOG: 2026-04-25 10:40:05  FROM=deepseek  TO=claude-max  REASON=auto-resume after timeout
```

## Auto-resume (retry original)

After 10 minutes (configurable), claude-mux automatically tries to reactivate the original provider:

1. Tests the original provider's health endpoint
2. If OK → switches back and resets failure state
3. If still failing → waits another 10 minutes

**Configure timeout:**
```python
# In claude-mux.py:
FailoverManager.RETRY_ORIGINAL_AFTER_SECS = 600  # default: 10 min
```

## Manual failover

Press `x` to trigger a failover check manually:
- Tests active subscription
- If failing: tries next
- Shows status in TUI and notification

## Failover log

```bash
cat ~/.claude-mux/failover.log
```

```
2026-04-25 10:30:00  FROM=claude-max  TO=deepseek      REASON=HTTP 429
2026-04-25 10:40:05  FROM=deepseek    TO=claude-max    REASON=auto-resume after timeout
2026-04-25 11:15:22  FROM=claude-max  TO=none          REASON=rate-limit (no alternatives)
```

View log in TUI with `L` (Failover Log modal).

## Background health-check

claude-mux runs a periodic health-check (every 5 minutes) in the background:

1. Checks if `RETRY_ORIGINAL_AFTER_SECS` has elapsed → tries auto-resume
2. Tests active subscription
3. If failing → triggers automatic failover

## No alternatives

If **all** subscriptions fail:
- Notification: "No working subscription found!"
- Log: `TO=none`
- claude-mux stays on the last known subscription

## Configure failover order

Add providers in priority order. claude-mux tries them top to bottom (excluding already-failed ones).

**Example:**
1. Claude Max (primary — direct, fastest)
2. DeepSeek (backup — cheap, high rate limit)
3. Anthropic direct (reserve)
4. GitHub Copilot (last — requires active Copilot session)
