#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# Ares — Linux/macOS Installer
# Supports: Pop OS 24, Kali Linux, Ubuntu 20+, macOS 12+
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOLD='\033[1m'
BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${BLUE}[MV]${NC} $*"; }
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; exit 1; }

OS="$(uname -s)"
ARCH="$(uname -m)"

# ── CLI options ──────────────────────────────────────────────────────────────
OFFLINE_BUNDLE=""
usage() {
    cat <<'EOF'
Ares ATAK installer

  ./install.sh [options]

Options:
  --offline-bundle <dir>   Pre-stage an "Ares-in-a-box" data bundle into
                           backend/data/  (the <dir> should contain a "packs/"
                           tree — terrain/osm/imagery/buildings/clutter — and
                           optionally "terrain/", "users.json", ".auth_secret").
                           Implies an air-gapped install: skips the online
                           terrain pre-download. Pair with ARES_NETWORK_POLICY=offline_only.
  -h, --help               Show this help.
EOF
}
while [ $# -gt 0 ]; do
    case "$1" in
        --offline-bundle) OFFLINE_BUNDLE="${2:-}"; shift 2 ;;
        --offline-bundle=*) OFFLINE_BUNDLE="${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) warn "Unknown option: $1"; usage; exit 1 ;;
    esac
done

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║              Ares  Installer v5.1 (authoritative)                ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ── 1. Check Python 3.10+ ─────────────────────────────────────────────────────
log "Checking Python version..."
PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" -c "import sys; print(sys.version_info[:2])")
        if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
            PYTHON="$cmd"
            ok "Found $PYTHON ($VER)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    warn "Python 3.10+ not found. Attempting to install..."
    if [ "$OS" = "Linux" ]; then
        if command -v apt &>/dev/null; then
            sudo apt update && sudo apt install -y python3 python3-pip python3-venv
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y python3 python3-pip
        elif command -v pacman &>/dev/null; then
            sudo pacman -Sy --noconfirm python python-pip
        else
            err "Cannot auto-install Python. Please install Python 3.10+ manually."
        fi
        PYTHON="python3"
    elif [ "$OS" = "Darwin" ]; then
        if command -v brew &>/dev/null; then
            brew install python@3.12
            PYTHON="python3"
        else
            err "Install Homebrew (brew.sh) then re-run this script."
        fi
    fi
fi

# ── 2. Check Node.js 18+ ──────────────────────────────────────────────────────
log "Checking Node.js..."
if command -v node &>/dev/null; then
    NODE_VER=$(node -e "console.log(process.version.slice(1).split('.')[0])")
    if [ "$NODE_VER" -ge 18 ] 2>/dev/null; then
        ok "Node.js $(node --version)"
    else
        warn "Node.js $NODE_VER found but 18+ required"
        INSTALL_NODE=true
    fi
else
    warn "Node.js not found"
    INSTALL_NODE=true
fi

if [ "${INSTALL_NODE:-false}" = "true" ]; then
    log "Installing Node.js 20 LTS..."
    if [ "$OS" = "Linux" ] && command -v apt &>/dev/null; then
        curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
        sudo apt install -y nodejs
    elif [ "$OS" = "Darwin" ] && command -v brew &>/dev/null; then
        brew install node@20
    else
        err "Please install Node.js 20+ from https://nodejs.org"
    fi
    ok "Node.js $(node --version)"
fi

# ── 3. GPU (CUDA) detection ───────────────────────────────────────────────────
log "Checking for NVIDIA GPU (CUDA)..."
GPU_FOUND=false
CUDA_MAJOR=""
if command -v nvidia-smi &>/dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1 2>/dev/null || echo "")
    CUDA_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || echo "")
    if [ -z "$CUDA_VER" ] || [[ "$CUDA_VER" == *"|"* ]]; then
        CUDA_VER=$(nvidia-smi 2>/dev/null | grep -oP "CUDA Version:\s*\K[0-9]+\.[0-9]+" | head -1 || echo "")
    fi
    if [ -z "$CUDA_VER" ]; then
        if command -v nvcc &>/dev/null; then
            CUDA_VER=$(nvcc --version 2>/dev/null | grep -oP "release \K[0-9]+\.[0-9]+" | head -1 || echo "12.0")
        else
            CUDA_VER="12.0"
        fi
    fi
    if [ -n "$GPU_NAME" ]; then
        ok "GPU: $GPU_NAME (CUDA $CUDA_VER)"
        GPU_FOUND=true
        CUDA_MAJOR=$(echo "$CUDA_VER" | cut -d. -f1)
        if [ "$CUDA_MAJOR" -ge 12 ] 2>/dev/null; then
            CUDA_MAJOR="12"
        elif [ "$CUDA_MAJOR" -ge 11 ] 2>/dev/null; then
            CUDA_MAJOR="11"
        else
            CUDA_MAJOR="12"
        fi
    fi
else
    warn "No NVIDIA GPU detected — GPU acceleration will be disabled (CPU-only mode)"
fi

# ── 4. Python virtual environment ─────────────────────────────────────────────
log "Setting up Python virtual environment..."
VENV_DIR="$SCRIPT_DIR/backend/.venv"

if [ "$OS" = "Linux" ] && command -v apt &>/dev/null; then
    PYTHON_PKG_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    if ! $PYTHON -m venv --help &>/dev/null 2>&1; then
        log "Installing python${PYTHON_PKG_VER}-venv..."
        sudo apt install -y "python${PYTHON_PKG_VER}-venv" python3-pip
    fi
fi

if [ -d "$VENV_DIR" ] && [ ! -f "$VENV_DIR/bin/activate" ]; then
    warn "Removing incomplete venv..."
    rm -rf "$VENV_DIR"
fi

if [ ! -d "$VENV_DIR" ]; then
    $PYTHON -m venv "$VENV_DIR"
    ok "Created venv at $VENV_DIR"
else
    ok "Using existing venv"
fi

source "$VENV_DIR/bin/activate"
PIP="$VENV_DIR/bin/python -m pip"
$PIP install --upgrade pip --quiet

if [ "$GPU_FOUND" = "true" ] && [ -n "$CUDA_MAJOR" ]; then
    log "Installing CuPy for GPU acceleration (CUDA $CUDA_MAJOR)..."
    CUPY_PKG="cupy-cuda${CUDA_MAJOR}x"
    $PIP install "$CUPY_PKG" --quiet && ok "CuPy installed ($CUPY_PKG)" || \
        warn "CuPy install failed — will use CPU. Install manually: pip install $CUPY_PKG"
fi

log "Installing Python dependencies..."
$PIP install -r "$SCRIPT_DIR/backend/requirements.txt" --quiet
ok "Python dependencies installed"

# ── 4b. Preserve user data ───────────────────────────────────────────────────
# Save states are JSON files downloaded to the user's filesystem — never touched here.
# Terrain tiles (backend/data/terrain/) and space weather cache are preserved across
# reinstalls; this installer never deletes backend/data/ or any user-created files.
DATA_DIR="$SCRIPT_DIR/backend/data"
if [ -d "$DATA_DIR" ]; then
    ok "User data directory preserved ($(du -sh "$DATA_DIR" 2>/dev/null | cut -f1 || echo '?') at backend/data/)"
else
    mkdir -p "$DATA_DIR"
    ok "Created backend/data/ directory"
fi
mkdir -p "$DATA_DIR/packs"/{terrain,osm,imagery,buildings,clutter}

# ── 4c. Offline data bundle ("Ares-in-a-box") or online terrain pre-download ──
if [ -n "$OFFLINE_BUNDLE" ]; then
    [ -d "$OFFLINE_BUNDLE" ] || err "--offline-bundle: '$OFFLINE_BUNDLE' is not a directory"
    log "Staging offline data bundle from $OFFLINE_BUNDLE ..."
    if command -v rsync &>/dev/null; then
        rsync -a "$OFFLINE_BUNDLE"/ "$DATA_DIR"/
    else
        cp -a "$OFFLINE_BUNDLE"/. "$DATA_DIR"/
    fi
    ok "Offline bundle staged ($(du -sh "$DATA_DIR/packs" 2>/dev/null | cut -f1 || echo '?') of packs at backend/data/packs/)"
    warn "Air-gapped deployment: run with  ARES_NETWORK_POLICY=offline_only  (and ARES_AUTH=true for field use)."
else
    log "Pre-downloading terrain tiles for offline use (UK area, ~1.4 GB) — existing tiles are kept..."
    log "  Tiles are cached in backend/data/terrain/ — skip with Ctrl+C if offline, or pass --offline-bundle <dir>."
    if "$VENV_DIR/bin/python" "$SCRIPT_DIR/backend/scripts/preload_terrain.py" 2>/dev/null; then
        ok "Terrain tiles preloaded"
    else
        warn "Terrain preload skipped or partially failed — tiles will download on first use when online (and grow the offline pack)."
    fi
fi

# ── 5. Frontend dependencies ──────────────────────────────────────────────────
log "Installing frontend dependencies..."
cd "$SCRIPT_DIR/frontend"
npm install --silent
ok "Frontend npm packages installed"

# ── 6. Build frontend ─────────────────────────────────────────────────────────
log "Building frontend..."
npm run build --silent
ok "Frontend built"

# ── 7. Electron (desktop app) ─────────────────────────────────────────────────
log "Installing Electron desktop dependencies..."
cd "$SCRIPT_DIR/electron"
npm install --silent
ok "Electron packages installed"

# ── 8. Create startup scripts ─────────────────────────────────────────────────
log "Creating startup scripts..."
VENV_ACTIVATE="$VENV_DIR/bin/activate"

cat > "$SCRIPT_DIR/start-backend.sh" << EOF
#!/bin/bash
# Ares — Start backend server
SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
source "\$SCRIPT_DIR/backend/.venv/bin/activate"
cd "\$SCRIPT_DIR/backend"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 "\$@"
EOF
chmod +x "$SCRIPT_DIR/start-backend.sh"

cat > "$SCRIPT_DIR/start-web.sh" << EOF
#!/bin/bash
# Ares — Open web browser UI
SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
source "\$SCRIPT_DIR/backend/.venv/bin/activate"
cd "\$SCRIPT_DIR/backend"
uvicorn app.main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=\$!
sleep 2
if command -v xdg-open &>/dev/null; then
    xdg-open http://localhost:3000
elif command -v open &>/dev/null; then
    open http://localhost:3000
fi
cd "\$SCRIPT_DIR/frontend" && npx vite preview --port 3000
kill \$BACKEND_PID 2>/dev/null || true
EOF
chmod +x "$SCRIPT_DIR/start-web.sh"

cat > "$SCRIPT_DIR/start-desktop.sh" << EOF
#!/bin/bash
# Ares — Start Electron desktop app
SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
source "\$SCRIPT_DIR/backend/.venv/bin/activate"
cd "\$SCRIPT_DIR/electron"
exec npx electron . "\$@"
EOF
chmod +x "$SCRIPT_DIR/start-desktop.sh"

ok "Startup scripts created"

# ── 9. Linux desktop shortcut ─────────────────────────────────────────────────
if [ "$OS" = "Linux" ]; then
    log "Creating Linux desktop shortcut..."

    ICON_DIR="$SCRIPT_DIR/frontend/public"
    mkdir -p "$ICON_DIR"

    if [ -f "$ICON_DIR/icon.png" ]; then
        ICON_FILE="$ICON_DIR/icon.png"
    else
        ICON_FILE="$ICON_DIR/icon.svg"
    fi

    DESKTOP_CONTENT="[Desktop Entry]
Version=1.0
Type=Application
Name=Ares
Comment=RF propagation and geolocation platform
Exec=bash $SCRIPT_DIR/start-desktop.sh
Icon=$ICON_FILE
Terminal=false
Categories=Science;Engineering;Education;
Keywords=rf;radio;propagation;antenna;terrain;geolocation;df;lob;
StartupWMClass=Ares
"

    DESKTOP_FILE="$HOME/Desktop/ares.desktop"
    APPS_FILE="$HOME/.local/share/applications/ares.desktop"

    echo "$DESKTOP_CONTENT" > "$DESKTOP_FILE"
    chmod +x "$DESKTOP_FILE"

    mkdir -p "$HOME/.local/share/applications"
    echo "$DESKTOP_CONTENT" > "$APPS_FILE"
    chmod +x "$APPS_FILE"

    if command -v update-desktop-database &>/dev/null; then
        update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    fi

    ok "Desktop shortcut created: $DESKTOP_FILE"
    ok "App menu entry created: $APPS_FILE"

    if command -v gio &>/dev/null; then
        gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true
    fi
fi

# ── 10. macOS app bundle hint ─────────────────────────────────────────────────
if [ "$OS" = "Darwin" ]; then
    log "Creating macOS launch script..."
    cat > "$HOME/Desktop/Ares.command" << EOF
#!/bin/bash
cd "$SCRIPT_DIR"
bash start-desktop.sh
EOF
    chmod +x "$HOME/Desktop/Ares.command"
    ok "macOS desktop launcher created"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║            Installation Complete! ✓              ║${NC}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Desktop app:${NC}  Double-click 'Ares' on your desktop"
echo -e "  ${BOLD}Alternative:${NC}  bash $SCRIPT_DIR/start-desktop.sh"
echo -e "  ${BOLD}Web browser:${NC}  bash $SCRIPT_DIR/start-web.sh"
echo -e "  ${BOLD}API docs:${NC}     http://localhost:8000/docs"
echo ""
if [ "$GPU_FOUND" = "true" ]; then
    echo -e "  ${GREEN}GPU: $GPU_NAME — GPU acceleration enabled!${NC}"
else
    echo -e "  ${YELLOW}GPU: Not detected — running in CPU mode${NC}"
fi
echo ""
