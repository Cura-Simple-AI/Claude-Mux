#!/usr/bin/env bash
# claude-mux uninstall script — removes claude-mux and all associated files.
# Usage:
#   curl -sSL https://raw.githubusercontent.com/Cura-Simple-AI/Claude-Mux/main/uninstall.sh | bash
#   ./uninstall.sh

set -euo pipefail

PACKAGE="claude-mux"
CLAUDE_MUX_DIR="${HOME}/.claude-mux"
SETTINGS_PATH="${HOME}/.claude/settings.json"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${GREEN}[claude-mux]${NC} $*"; }
warn()    { echo -e "${YELLOW}[claude-mux]${NC} $*"; }
error()   { echo -e "${RED}[claude-mux] ERROR:${NC} $*" >&2; exit 1; }

# --- Parse flags ---
DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --help|-h)
      echo "Usage: ./uninstall.sh [--dry-run]"
      echo ""
      echo "  --dry-run   Show what would be removed without making changes"
      exit 0
      ;;
  esac
done

if $DRY_RUN; then
  warn "Dry run — no changes will be made"
fi

run() {
  if $DRY_RUN; then
    echo "  [dry-run] $*"
  else
    "$@"
  fi
}

echo ""
echo "claude-mux uninstaller"
echo "━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# --- 1. Stop and remove PM2 processes ---
if command -v pm2 &>/dev/null; then
  PROCS=$(pm2 jlist 2>/dev/null | python3 -c "
import sys, json
try:
    procs = json.load(sys.stdin)
    names = [p['name'] for p in procs if p['name'].startswith('claude-mux')]
    print('\n'.join(names))
except Exception:
    pass
" 2>/dev/null || true)
  if [ -n "$PROCS" ]; then
    info "Removing PM2 processes:"
    while IFS= read -r name; do
      echo "  - $name"
      run pm2 delete "$name" 2>/dev/null || true
    done <<< "$PROCS"
    run pm2 save 2>/dev/null || true
  else
    info "No claude-mux PM2 processes found"
  fi
else
  warn "pm2 not found — skipping PM2 cleanup"
fi

# --- 2. Remove statusLine from ~/.claude/settings.json ---
if [ -f "$SETTINGS_PATH" ]; then
  HAS_STATUSLINE=$(python3 -c "
import json
with open('$SETTINGS_PATH') as f:
    s = json.load(f)
print('yes' if 'statusLine' in s else 'no')
" 2>/dev/null || echo "no")

  if [ "$HAS_STATUSLINE" = "yes" ]; then
    info "Removing statusLine from $SETTINGS_PATH"
    if ! $DRY_RUN; then
      python3 - <<'PYEOF'
import json, os, tempfile
path = os.path.expanduser("~/.claude/settings.json")
with open(path) as f:
    settings = json.load(f)
settings.pop("statusLine", None)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".cmux_uninstall_")
try:
    os.write(fd, json.dumps(settings, indent=2).encode())
    os.close(fd)
    os.rename(tmp, path)
except Exception:
    os.close(fd)
    os.unlink(tmp)
    raise
PYEOF
    fi
  else
    info "No statusLine entry in $SETTINGS_PATH"
  fi
else
  info "No Claude settings file found at $SETTINGS_PATH"
fi

# --- 3. Remove ~/.claude-mux/ directory ---
if [ -d "$CLAUDE_MUX_DIR" ]; then
  info "Removing $CLAUDE_MUX_DIR"
  run rm -rf "$CLAUDE_MUX_DIR"
else
  info "No data directory found at $CLAUDE_MUX_DIR"
fi

# --- 4. Uninstall Python package ---
if command -v pipx &>/dev/null && pipx list 2>/dev/null | grep -q "$PACKAGE"; then
  info "Uninstalling $PACKAGE via pipx"
  run pipx uninstall "$PACKAGE"
elif pip show "$PACKAGE" &>/dev/null 2>&1; then
  info "Uninstalling $PACKAGE via pip"
  run pip uninstall -y "$PACKAGE"
else
  warn "$PACKAGE not found in pip or pipx — already uninstalled?"
fi

echo ""
if $DRY_RUN; then
  warn "Dry run complete — run without --dry-run to apply changes"
else
  info "claude-mux uninstalled. Restart Claude Code to clear the status line."
fi
echo ""
