#!/usr/bin/env bash
# claude-mux install script — installs claude-mux + all required dependencies.
# Usage:
#   curl -sSL https://raw.githubusercontent.com/Cura-Simple-AI/Claude-Mux/main/install.sh | bash
#   ./install.sh            # standard install
#   ./install.sh --pipx     # install in isolated pipx environment (recommended)
#   ./install.sh --dev      # editable install for development

set -euo pipefail

REPO="https://github.com/Cura-Simple-AI/Claude-Mux"
PACKAGE="claude-mux"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${GREEN}[claude-mux]${NC} $*"; }
warn()    { echo -e "${YELLOW}[claude-mux]${NC} $*"; }
error()   { echo -e "${RED}[claude-mux] ERROR:${NC} $*" >&2; exit 1; }

# --- Parse flags ---
USE_PIPX=false
DEV_MODE=false
for arg in "$@"; do
  case "$arg" in
    --pipx) USE_PIPX=true ;;
    --dev)  DEV_MODE=true ;;
    --help|-h)
      echo "Usage: install.sh [--pipx] [--dev]"
      echo "  --pipx   Install in isolated pipx environment (recommended)"
      echo "  --dev    Editable install from current directory"
      exit 0
      ;;
  esac
done

# --- Check Python ---
if command -v python3 &>/dev/null; then
  PYTHON=python3
elif command -v python &>/dev/null; then
  PYTHON=python
else
  error "Python 3.11+ required. Install from https://python.org"
fi

PY_VERSION=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$($PYTHON -c "import sys; print(sys.version_info.major)")
PY_MINOR=$($PYTHON -c "import sys; print(sys.version_info.minor)")

if [[ "$PY_MAJOR" -lt 3 ]] || [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 11 ]]; then
  error "Python 3.11+ required (found $PY_VERSION). Upgrade at https://python.org"
fi
info "Python $PY_VERSION found"

# --- Install ---
if $USE_PIPX; then
  if ! command -v pipx &>/dev/null; then
    warn "pipx not found — installing..."
    $PYTHON -m pip install --user pipx
    $PYTHON -m pipx ensurepath
    export PATH="$HOME/.local/bin:$PATH"
  fi
  if $DEV_MODE; then
    info "Installing in editable mode with pipx..."
    pipx install --editable .
  else
    info "Installing $PACKAGE with pipx..."
    pipx install "$PACKAGE"
  fi
elif $DEV_MODE; then
  info "Installing in editable mode with pip..."
  $PYTHON -m pip install -e ".[dev]"
else
  info "Installing $PACKAGE with pip..."
  $PYTHON -m pip install --upgrade "$PACKAGE"
fi

# --- Optional: pm2 ---
if ! command -v pm2 &>/dev/null; then
  warn "pm2 not found — needed for bearer/proxy providers (not required for Claude Max OAuth)"
  warn "Install with: npm install -g pm2"
fi

# --- Optional: claude CLI ---
if ! command -v claude &>/dev/null; then
  warn "claude CLI not found — needed for Claude Max OAuth setup"
  warn "Install with: npm install -g @anthropic-ai/claude-code"
fi

# --- Verify ---
if command -v claude-mux &>/dev/null; then
  VERSION=$(claude-mux --version 2>/dev/null || echo "unknown")
  info "✓ claude-mux installed: $VERSION"

  # --- Init: install statusline integration ---
  if command -v claude &>/dev/null; then
    info "Running: claude-mux init"
    claude-mux init || warn "claude-mux init failed — run manually to set up the Claude Code status line"
  else
    warn "claude CLI not found — skipping claude-mux init"
    warn "Run 'claude-mux init' manually after installing Claude Code"
  fi

  info ""
  info "Run: claude-mux"
  info "Docs: $REPO/docs/QUICKSTART.md"
else
  warn "claude-mux not found in PATH after install."
  warn "You may need to: export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
