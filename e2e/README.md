# End-to-end tests

```bash
make test-e2e        # pytest e2e  (must run in its own pytest process, not with tests/)
make coverage-e2e    # same, with coverage of every Python process the tests start
```

The suite drives claude-mux only through its user-facing entry points: the `claude-mux`
CLI and the proxy as real processes, the TUI through Textual's Pilot, and Claude Code
sessions in a real tmux server. Nothing inside claude-mux is mocked.

**Requirements:** Python deps from `pip install -e ".[dev]"`, plus `node` and `tmux` for
the session tests (`test_e2e_sessions.py`; skipped when either is missing).

## Isolation

- `HOME` points to a throw-away directory (set before `claude_mux` is imported, wiped
  between tests), so `~/.claude-mux` and `~/.claude/settings.json` are never touched.
- tmux uses a private socket directory (`TMUX_TMPDIR`): tests never see your sessions.
- Network: everything that is not localhost goes to a closed port.

## Stand-ins for external programs (`e2e_support/`)

| Real | Stand-in |
|---|---|
| Provider APIs | `fake_upstream.py`: HTTP server on 127.0.0.1, records every request |
| `pm2` | `stubs.py`: really starts/stops processes, state in `~/.pm2/` |
| `heimsense` | `stubs.py`: requires `run`, then serves claude-mux's own pass-through proxy — it does **not** translate to OpenAI format like the real binary |
| `claude` (Claude Code) | `fake_claude.py` behind a node entry point at `.../@anthropic-ai/claude-code/cli.js`, so processes look like an npm install |
| `gh` | `stubs.py`: not logged in |

## Claude Code behaviour model — check once with the real Claude Code

The session and live-switching tests verify claude-mux against `fake_claude.py`, which
assumes the following. Please confirm with a real Claude Code after upgrades:

1. A running session re-reads `env.ANTHROPIC_BASE_URL` from `~/.claude/settings.json` on
   its next request. *(observed)*
   Check: start two proxy providers, `claude-mux activate A`, start `claude`, send a
   message; `claude-mux activate B`; send a message; `claude-mux logs B` shows it.
2. Credentials are read at startup: `CLAUDE_CODE_OAUTH_TOKEN` *(observed)* and
   `ANTHROPIC_AUTH_TOKEN` *(unknown)*.
   Check for the latter: two direct providers (e.g. two z.ai keys), activate A, start
   `claude`, activate B, send a message: does B's provider accept it (live) or reject A's key?
3. Screens: prompt box between two `─` rules starting with `❯`, a status line with
   `ctx NN%` / `? for shortcuts`, exit dialogs ending in `Enter to confirm · Esc to cancel`
   (copied from Claude Code 2.1 captures in `tests/test_session.py`).
4. `claude attach <job>` exits when the background session is respawned
   (`claude respawn <job>`).
5. The real `heimsense` must listen on `127.0.0.1` and ignore the client's credentials
   (otherwise live switching sends the previous provider's key).
   Check: `ss -ltnp | grep heimsense` after `claude-mux start <provider>`.

Known open bugs are recorded as `xfail(strict=True)` tests and listed in
[`KNOWN_ISSUES.md`](../KNOWN_ISSUES.md): when one is fixed, its test starts passing, pytest
reports it as failing, and both the marker and the KNOWN_ISSUES entry must be removed.
