# Keyboard Reference

Heimsense is optimized for iPhone SSH keyboards (Blink Shell, a-Shell, SSH.app) where Ctrl, F-keys, arrow keys, Tab, and ESC are hard to reach. **All critical actions have single-letter shortcuts.**

## Main screen (TUI)

| Key | Alternative | Action |
|---|---|---|
| `r` | — | Refresh table and status |
| `+` | — | Add subscription (Add wizard) |
| `Enter` | — | Activate selected subscription as default |
| `s` | — | Start/Stop toggle (proxy) |
| `t` | — | Test selected subscription (HTTP health-check) |
| `e` | — | Edit subscription |
| `d` | — | Delete subscription (confirm) |
| `f` | — | Force model (override all aliases to one model) |
| `z` | — | Reload TUI (hotload on file change) |
| `l` | — | View PM2 logs (proxy logs) |
| `L` | — | View failover log |
| `x` | — | Manual failover check |
| `h` | `?` | Show help modal |
| `q` | — | Quit (confirm if instances are running) |

## List navigation

| Key | Alternative | Action |
|---|---|---|
| `↓` | `j`, `n` | Next subscription |
| `↑` | `k`, `p` | Previous subscription |
| `1`–`9` | — | Jump directly to row N |

**iPhone tip:** `j`/`k` (vi-style) and `n`/`p` work without arrow keys.

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

### Confirmation dialog
| Key | Action |
|---|---|
| `j` / `Enter` | Confirm (Yes) |
| `n` / `q` | Cancel (No) |

### Force Model modal
| Key | Action |
|---|---|
| `Enter` | Select highlighted model |
| `j` / `↓` | Next model |
| `k` / `↑` | Previous model |
| `q` | Cancel |

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
Add provider:   +  →  [type name]  →  Enter  →  [press 1-8]  →  [type key]  →  Enter
Activate:       [select with j/k]  →  Enter
Start/Stop:     s
Test:           t
Failover check: x
Quit:           q  →  j
```

## All shortcuts (quick reference)

```
TUI:  r + Enter s t e d f z l L x h ? q
Nav:  j k n p 1-9
```
