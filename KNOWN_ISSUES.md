# Known issues

Open bugs and limitations, by priority. Where a test is named, it is an
`xfail(strict=True)` test in `e2e/`: it starts passing when the bug is fixed, pytest then
reports it as failing, and the marker must be removed together with the entry here.

| Priority | Meaning |
|---|---|
| P1 | Breaks a headline feature; needs a decision or a fix soon |
| P2 | Real bug with a workaround or limited reach |
| P3 | Minor, cosmetic or hygiene |

---

## P1

### KI-1 — Claude Max via proxy: hitting the usage limit does not trigger failover  *(needs decision)*

**Symptom:** the OAuth-via-proxy provider answers `429` ("usage limit reached"), but
`claude-mux failover` reports `healthy — no failover needed`; the TUI's background check does
the same. The README's headline scenario ("Claude hits token limit → claude-mux switches")
does not happen for this provider type — the only OAuth mode that switches live.
**Cause:** `FailoverManager._test_proxy_http` deliberately treats `429` from `oauth_proxy` as
healthy ("auth is valid, just rate limited").
**Related (unverified):** plain OAuth providers are health-checked with `GET /v1/models`, which
probably also succeeds while the account is rate limited.
**Workaround:** `claude-mux activate <other>` by hand.
**Decision needed:** should a rate-limited account count as failed? Probably yes, at least when
another provider is configured.
*Verified with an E2E probe; no test yet.*

---

## P2

### KI-2 — `session restart` of a background session attached from a shell leaves the pane detached

**Symptom:** for a pane where `claude attach <job>` was started from a shell, the result is
`OK (nudge not submitted)` (exit 0), but the pane then shows a bare shell. The background
session itself was restarted correctly.
**Cause:** after `claude respawn`, the attach client exits but the shell keeps the pane alive,
so `Restarter._respawn_attached` never re-attaches. The stale attach screen above the shell
prompt is taken for the re-attached view, and the nudge is typed into the shell (then erased;
Enter is not sent).
**Workaround:** run `claude attach <job>` again in the pane. Panes whose process *is*
`claude attach` (e.g. `tmux new-window 'claude attach <job>'`) are not affected.
**Fix idea:** detect that the attach client is gone and re-run it in the shell; verify by
process, not by screen text.
*Test: `test_e2e_sessions.py::test_background_session_is_respawned_and_reattached[shell]`*

### KI-3 — Second `session restart` of a direct pane closes it when tmux's default shell is `sh`

**Symptom:** Claude Code running as the pane process (`tmux new-window 'claude ...'`) restarts
fine once. On the next restart the pane closes — with it the tmux session, if it was the last
pane — and the result is `FAILED`. Resume by hand with the logged command.
**Cause:** direct panes are relaunched with `tmux respawn-pane CMD`, which tmux runs as
`$SHELL -c CMD`. bash exec()s a single command; `sh`/dash (common in containers, i.e. the
`--container` use case; fish possibly too) keeps itself as the pane process with Claude as its
child. The pane then looks like a shell pane, and exiting Claude ends the shell.
**Workaround:** `set -g default-shell /bin/bash` in tmux.
**Fix idea:** relaunch with `exec CMD`.
*Test: `test_e2e_sessions.py::test_direct_pane_survives_repeated_restarts[/bin/sh]`*

### KI-4 — Failover that finds no working provider leaves Claude Code on the last one tried

**Symptom:** `failover` reports "no working subscription found" (exit 4), but Claude Code now
points at the last candidate instead of the provider you had. Running sessions see the
settings change during the attempt.
**Cause:** `FailoverManager.do_failover` writes each candidate into `settings.json` before
testing it.
**Workaround:** `claude-mux activate <original>`.
**Fix idea:** test first; write only the provider that works.
*Verified with an E2E probe; no test yet.*

### KI-5 — Two accounts on the same direct provider cannot be told apart

**Symptom:** with two subscriptions on the same URL (e.g. two z.ai keys), whichever is
activated, the first is reported as active — by `active`, `list`, `status`, the TUI (▶ marker,
hidden Sync button) — and `failover`/`test`/`probe` without a name act on it.
**Cause:** `SyncManager._match_from_settings` / `_match_from_claude_output` match direct
providers by `ANTHROPIC_BASE_URL` only.
**Fix idea:** among matching URLs, pick the subscription whose key equals `ANTHROPIC_AUTH_TOKEN`.
*Test: `test_e2e_activate.py::test_two_accounts_on_the_same_direct_provider`*

### KI-6 — The wizard's "Force model" setting is never saved

**Symptom:** choosing a force model in the Add/Edit wizard has no effect.
**Cause:** `ConfigManager.update_subscription` silently ignores fields outside its allow-list,
and `force_model` is not in it (`sync_default` would honour it).
**Note:** the CLI's `force-model` is a different feature (it changes one tier's mapping); the
wizard's forces all three tiers to one model. Consider renaming one of them.
*From code reading; no test yet (the wizard has no E2E tests).*

### KI-7 — Blacklisted models are still tested

**Symptom:** when the blacklist removes a tier's only candidate, `claude-mux test` and the TUI
Test button fall back to the built-in default for that tier — possibly the blacklisted model
itself. `probe` ignores the blacklist entirely. `blacklist --help` promises the opposite.
**Cause:** `TIER_FALLBACK_MODELS` is used without checking the blacklist (`cli.py` `cmd_test`,
`cmd_probe`; `tui.py` `action_test`).
**Fix idea:** report such a tier as "skipped (blacklisted)" (decide whether it affects the exit code).
*Test: `test_e2e_health.py::test_blacklisted_model_is_never_tested`*

### KI-8 — Recent failover events shown at the wrong time, or not at all

**Symptom:** in the TUI detail panel, west of UTC recent failover events are missing; east of
UTC they are expected to show "0s ago" and stay up to an hour too long (not tested).
`failover.log` itself and failover are correct.
**Cause:** `_log_failover_event` writes local time, `recent_events` parses it as UTC.
**Fix idea:** parse as local time (file format unchanged).
*Test: `test_e2e_tui.py::test_recent_failover_shows_in_detail_panel_west_of_utc`*

### KI-9 — `failover-log` shows `REASON=unknown` for failovers started from the CLI

**Cause:** `cmd_failover` calls `do_failover(sub_id)` without the reason it already has
(e.g. `HTTP 429 — …`). The TUI passes it.
*From code reading; no test yet.*

---

## P3

### KI-10 — The proxy ignores `LOG_FILE` and loads the whole TUI
`python -m claude_mux.proxy` imports the `claude_mux` package first; its config module
configures logging to `~/.claude-mux/claude-mux.log`, so the proxy's own log setup is a no-op.
Proxy errors therefore do not reach pm2's error log (`claude-mux logs`, TUI `l`), and every
proxy process loads Textual (~0.35 s, memory).
*Test: `test_e2e_proxy.py::test_honours_log_file`*

### KI-11 — Usage tracking is written but never read
The proxy appends to `~/.claude-mux/usage.log` on every request (unbounded growth) and writes
`rate-limits.json`; the only reader, `cli._compute_usage_windows`, is never called.
*Decision needed:* show 5h/7d usage in the status line, or remove both.

### KI-12 — Status line is slow and can lose its input
Every status line render runs `claude-mux statusline`, which loads the full TUI stack. Input is
read with a zero-timeout `select`: if Claude Code has not written the JSON yet, the line shows
only the provider name for that refresh.

### KI-13 — TUI may raise `NoMatches` when quitting
A table event arriving during shutdown calls `_show_detail` after the widgets are gone.
Seen once, under coverage timing.

### KI-14 — TUI freezes while `s` starts a proxy
`start` runs on the UI thread: ~0.5 s normally, up to 10 s when the proxy does not come up.

### KI-15 — Start button shown for direct providers
Pressing it can only produce "no proxy needed".

### KI-16 — `tests/test_number_key.py` tests a copy of the logic, not the app
It reimplements the key handler inside the test. The real behaviour is covered by
`e2e/test_e2e_tui.py`; delete or rewrite it.

### KI-17 — Local secret hygiene (matters only on machines shared with other users)
- `_atomic_write` creates the temp file with the default umask, so settings containing tokens
  are briefly world-readable before `chmod 600`.
- `fetch_copilot_models` passes the GitHub token on `curl`'s command line (visible in `ps`).
- `PROXY_DEBUG=1` logs the client's `x-api-key` header.

---

## Unverified / external

### KI-18 — heimsense (third-party binary) is not covered by the tests
The E2E suite uses a stand-in. Still to check with the real binary:
- it listens on `127.0.0.1` (`ss -ltnp | grep heimsense` after `claude-mux start <provider>`);
- it sends no permissive CORS headers (claude-mux's own proxy now refuses browser requests);
- it ignores the client's credentials — otherwise live switching sends the previous
  provider's key. Switching is reported to work, so this probably holds.

### KI-19 — Does a running Claude Code pick up a changed `ANTHROPIC_AUTH_TOKEN`?
Unknown. If not, switching between *direct* providers (e.g. the z.ai preset) needs a session
restart (`claude-mux session restart --profile <name>`), and neither `activate` nor the TUI
says so. See the checklist in `e2e/README.md`.

---

## Behaviour notes (by design)

- The proxy refuses requests whose `Host` is not `localhost`/`127.0.0.1`/`[::1]` (DNS-rebinding
  protection): point Claude Code at it through `localhost` (claude-mux always does).
- In the TUI, clicking an already highlighted row activates it (like `Enter`).
