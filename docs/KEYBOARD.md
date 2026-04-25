# Keyboard Reference

Claude Mux is optimized for iPhone SSH keyboards (Blink Shell, a-Shell, SSH.app) where Ctrl, F-keys, arrow keys, Tab, and ESC are hard to reach. **All critical actions have single-letter shortcuts.**

## Main screen (TUI)

| Key | Alternative | Action |
|---|---|---|
| `r` | — | Reload TUI (hotload restart) |
| `+` | — | Add subscription (Add wizard) |
| `e` | — | Edit subscription (model maps, force-model, fields) |
| `d` | — | Delete subscription (confirm) |
| `s` | — | Start/Stop toggle (proxy providers only) |
| `t` | — | Test selected subscription (HTTP health-check) |
| `l` | — | View PM2 logs (proxy providers only) |
| `L` | — | View failover log |
| `x` | — | Manual failover check |
| `/` | — | Filter providers by name |
| `h` | `?` | Show help modal |
| `q` | — | Quit |

> **Note:** `Enter` no longer activates a provider. Use the **Activate** button or press `1`–`9`.

## List navigation

| Key | Alternative | Action |
|---|---|---|
| `↓` | `j`, `n` | Next subscription |
| `↑` | `k`, `p` | Previous subscription |
| `1`–`9` | — | Activate provider N directly (skips `*current settings` row) |

**iPhone tip:** `j`/`k` (vi-style) and `n`/`p` work without arrow keys.

## Provider activation

| Method | When to use |
|---|---|
| Click **Activate** button | OAuth / direct providers — writes token to settings.json |
| Click **Sync settings** button | Bearer/proxy providers — updates settings.json to point to local proxy |
| Press `1`–`9` | Instant activation by row number (same as Activate/Sync button) |

Active provider is marked with `●` in the list and in the detail panel.

## Filter (`/`)

1. Press `/` — filter input appears; active filter shown in header as `/ <query>`
2. Type to filter by provider name (case-insensitive)
3. Press `Enter` or `Escape` to close filter

## Modals and pop-ups

### General (all modals)
| Key | Action |
|---|---|
| `q` | Close / cancel |
| `b` | Back (wizard) |
| `ESC` | Close (if available) |

### Add / Edit Wizard
| Key | Action |
|---|---|
| `Enter` | Next step |
| `b` | Back to previous step |
| `Tab` | Next field (if available) |
| `1`–`8` | Select provider directly in provider picker |

> Force-model is configured in the **Edit wizard → Model Maps** step.

### Confirmation dialog
| Key | Action |
|---|---|
| `j` / `Enter` | Confirm (Yes) |
| `n` / `q` | Cancel (No) |

### Provider picker
| Key | Action |
|---|---|
| `1`–`8` | Select provider directly |
| `j`/`n`/`↓` | Next |
| `k`/`p`/`↑` | Previous |
| `Enter` | Select |
| `q`/`ESC` | Cancel |

### Hotload modal (file changed)
| Key | Action |
|---|---|
| `Enter` | Confirm reload |
| `q` | Cancel |

## iPhone SSH setup (Blink / a-Shell)

```
Recommended settings:
- Blink Shell: Settings → Keys → Enable Escape Key Alternatives
- a-Shell: No setup needed — j/k/n/p work out of the box
- SSH.app: Enable "Alt as ESC" in keyboard settings
```

**Critical flows using letter keys only:**

```
Add provider:      +  →  [type name]  →  Enter  →  [press 1-8]  →  [type key]  →  Enter
Activate (OAuth):  [select with j/k]  →  [Activate button]   OR   1-9
Sync (bearer):     [select with j/k]  →  [Sync settings button]  OR  1-9
Test:              t
Filter:            / [type query] Enter
Failover check:    x
Quit:              q  →  j
```

## All shortcuts (quick reference)

```
TUI:  r + e d s t l L x / h ? q
Nav:  j k n p 1-9
```
