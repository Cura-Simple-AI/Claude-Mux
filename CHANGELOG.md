# Changelog

All notable changes are documented here.

Format: [Semantic Versioning](https://semver.org/). Dates in ISO 8601.

---

## [0.1.3] — 2026-04-25

### Bugfixes
- `r` refresh key now works even when an Input widget is focused — `on_key` override at App level intercepts `r` before Input consumes it
- Test assertions updated to match English output strings (`"No active subscription"`, `"TO=none"`, `"ago"`, `"1h"`)

### Refactor
- `_run_proxy_test()` extracted as `@staticmethod` from `action_test()` — fully testable without TUI, reusable for CLI
- All code comments and UI strings translated to English (tools/ language rule)

### Tests: 137 → 147 (+10)
- `test_run_proxy_test.py`: 10 tests for `HeimsenseApp._run_proxy_test()` — 200, 429, 401, 503, connection error, truncation, invalid JSON, elapsed type

### Package
- `install.sh` — one-liner install script (pip + pipx modes, --dev flag, optional dep warnings)
- `Makefile` — install, test, lint, build, publish targets
- `docs/CLI.md` — full CLI reference: all 14 subcommands, --json, exit codes, TUI↔CLI parity table

### Documentation
- All docs translated to English (QUICKSTART, KEYBOARD, PROVIDERS, FAILOVER, CHANGELOG)
- `docs/CLI.md` — complete CLI reference

---

## [0.1.2] — 2026-04-25

### Bugfixes
- `_oauth_focus_paste_delayed()`: `lambda: Input.focus()` returned `AwaitComplete` to `set_timer` → TUI crash "Can't await screen.dismiss() from the screen's message handler"
- `_check_and_install_deps()`: `sys` not imported when calling `sys.executable` — now `import sys` BEFORE dep-check
- `sync_default()` bearer-mode: `CLAUDE_CODE_OAUTH_TOKEN` is now removed from settings.json when switching OAuth→bearer

### Package
- `claude_mux/tui.py`: TUI code is now embedded directly in the Python package
- `claude_mux/__main__.py`: `python -m claude_mux` now works
- `claude_mux/cli.py`: imports from `claude_mux.tui` (standalone, no scripts/ reference)

### Documentation
- `docs/QUICKSTART.md` — installation, first start, configuration files
- `docs/KEYBOARD.md` — all shortcuts + iPhone SSH (no Ctrl/F-keys/arrow keys/Tab/ESC)
- `docs/PROVIDERS.md` — setup guide for all 8 providers
- `docs/FAILOVER.md` — failover flow, auto-resume, log format

### iPhone keyboard
- `ConfirmModal`: `j`/`Enter`=yes, `n`/`q`/`b`=no
- `ForceModelModal`: `q`/`b`/`Enter`
- `LogViewer`: `q`/`b` bindings added
- `HelpModal`: updated with iPhone SSH alternatives

### Tests: 115 → 137 (+22)
- `test_providers.py`: 22 tests for all 8 providers (`generate_env` + `sync_default`)
- Parametrized over bearer, oauth, gh_token, x-goog-api-key

---

## [0.1.1] — 2026-04-25

### Bugfixes
- `_time_ago()` crash: `'float' object has no attribute 'replace'` — `test_res['ts']` stored as `time.time()` (float), but `_time_ago` assumed ISO string. Added `isinstance` check.
- `sync_default()` (bearer-mode): `CLAUDE_CODE_OAUTH_TOKEN` is now removed from settings.json when switching from OAuth → bearer — otherwise Claude kept using the OAuth token even with proxy active.

### Code quality
- Removed 7 dead code methods in `HeimsenseApp` (~90 lines): `_test_endpoint`, `_test_headers`, `_test_provider_url`, `_test_payload` (×2, duplicate), `_test_parse_reply` (×2, duplicate). `action_test()` has its own inline implementation.

### Tests
- **test isolation**: `conftest.py` removes FileHandlers after import — tests no longer write to `~/.claude-mux/heimsense-tui.log`
- `test_time_ago.py`: 7 tests for float-epoch + ISO string + edge cases
- `test_utils.py`: 20 tests for `_format_duration`, `_status_char`, `_status_color`
- **115 tests total** (up from 88)

---

## [0.1.0] — 2026-04-25

### Added
- **Claude Max (OAuth)** provider — `claude setup-token` flow, 1-year token, direct to api.anthropic.com
- **Subscription Failover** — automatic switch on HTTP 429/401/403/503 or rate-limit patterns
- **Failover log** — events written to `~/.claude-mux/failover.log` (FROM/TO/REASON)
- **Auto-resume** — reactivates original subscription after 10 min (configurable via `RETRY_ORIGINAL_AFTER_SECS`)
- **OAuth session death detection** — tmux session crash detected early with clear error message
- **GitHub Actions CI** — test matrix Python 3.11 + 3.12
- **Dependency self-check** — offers `pip install` of missing deps on first start
- **Standalone mode** — run directly from source (`python3 claude-mux.py --tui`)
- **n/p keyboard** — alternative to j/k for navigation (iPhone-friendly)
- **1-9 row-jump** — jump directly to subscription N in main table and provider picker
- **h/? help** — show all keybindings in HelpModal
- **x failover-check** — manual test + failover on active subscription
- **Hotload** — TUI automatically restarts on file change (2s countdown + cancel)
- **Force model** — temporarily override all model aliases to one model

### Providers
- Claude Max (OAuth)
- DeepSeek
- GitHub Copilot (gh_token)
- Gemini (Google)
- OpenAI / ChatGPT
- Anthropic (direct)
- z.ai
- Custom (OpenAI-compatible proxy)

### Architecture
- `ConfigManager` — CRUD for `~/.claude-mux/subscriptions.json` + atomic write
- `InstanceManager` — PM2 lifecycle + `.env` generation per subscription
- `SyncManager` — merge into `~/.claude/settings.json` (OAuth/bearer/proxy)
- `FailoverManager` — health-check, failover, retry-original, log

### Bugfixes (test-driven)
- `ConfigManager.update_subscription(model_maps=...)` now only replaces specified keys (partial merge)
- `ConfigManager.subscriptions` now returns a list copy — mutation no longer affects internal state
- `copy.deepcopy(EMPTY_SUBSCRIPTIONS)` — fixed shared-state bug with multiple empty ConfigManager instances

### Tests
- 60 unit tests (pytest)
- `InstanceManager.generate_env` — bearer, oauth, gh_token, file permissions
- `FailoverManager` — failover log, original_sub_id tracking, should_retry_original
- `SyncManager` — OAuth token, model maps, bearer URL, merge with existing settings
- `ConfigManager` — CRUD, persistence, isolation, edge cases
