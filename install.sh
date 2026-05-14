#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# Ares — Linux/macOS Installer
# Tested on: Ubuntu 20.04 / 22.04 / 24.04, Pop!_OS 22.04+, Kali Linux (rolling),
#            and other Debian/Ubuntu-derived distros (apt); macOS 12+ (Homebrew).
# Also works on Fedora (dnf) and Arch (pacman) for the Python/Node bootstrap.
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Non-interactive apt: never let a package post-install prompt block the run. ──
# Critical for things like dump1090-mutability which asks "auto-start via init?"
# (we *want* it off — Ares does ADS-B decoding in-process). The combination of
# DEBIAN_FRONTEND=noninteractive + the apt -o flags + our debconf pre-seed below
# means every dpkg-configure step picks its default answer silently.
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a   # answer "all" to needrestart's service-restart prompt
# Never let a git clone / submodule fetch hang on a credential prompt — if an
# upstream submodule points at a private/dead URL (Bitbucket auth, moved repo,
# etc.) we want it to fail fast rather than block the installer mid-build.
export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=/bin/true
APT_QUIET_OPTS=(-y -q
    -o Dpkg::Options::=--force-confdef
    -o Dpkg::Options::=--force-confold)

# Run apt/dnf/pacman with sudo only when we're not already root (Kali often runs as root,
# and `sudo` may not be installed there). On Linux non-root with no sudo → bail with a hint.
SUDO=""
if [ "$(id -u 2>/dev/null || echo 1)" != "0" ]; then
    if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; else SUDO=""; fi
fi
maybe_sudo() {  # usage: maybe_sudo apt install -y foo
    if [ "$(id -u 2>/dev/null || echo 1)" = "0" ]; then "$@"
    elif command -v sudo >/dev/null 2>&1; then sudo -E "$@"
    else echo "[!] need root for: $*  (install 'sudo' or re-run as root)"; return 1; fi
}

# Quiet apt-install wrapper: feeds the noninteractive flags + force-conf* options
# so we never get an "interactive prompt" from postinst scripts. Use everywhere.
apt_install() {  # usage: apt_install pkg1 pkg2 …
    maybe_sudo apt-get install "${APT_QUIET_OPTS[@]}" "$@"
}

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
WITH_SOAPYSDR=true
WITH_GPSD=false
WITH_AUDIO_DECODERS=true       # default-on: clone+build the *fast* decoders (dsd-fme/m17/acarsdec)
WITH_OP25=false                # opt-in: P25 → pulls all of GNU Radio, 30–60 min build
WITH_SDRTRUNK=false            # opt-in: downloads the SDRTrunk Java GUI release (~80 MB)
WITH_TETRA=false               # opt-in: TETRA via legacy osmocom-tetra (often needs hand-fix)
WITH_GNURADIO=true             # default-on: GNU Radio + gr-gsm (in-process flowgraph) — ~500 MB
WITH_LTE_SNIFFER=true          # default-on: LTE passive PDCCH/SIB1 sniffer
WITH_5G_SNIFFER=true           # default-on: 5G NR SSB/MIB/SIB1 sniffer (USRP-dependent, build can be flaky)
WITH_SRSRAN=false              # opt-in: srsRAN PPA install (full LTE/NR stack)
WITH_WIFI_BT=true              # default-on: hcxdumptool/airodump-ng/btmon for MAC tracking
WITH_SDR_UDEV=true             # install udev rules + DVB-driver blacklist + user-groups (Linux)
usage() {
    cat <<'EOF'
Ares installer

  ./install.sh [options]

Options:
  --offline-bundle <dir>   Pre-stage an "Ares-in-a-box" data bundle into
                           backend/data/  (the <dir> should contain a "packs/"
                           tree — terrain/osm/imagery/buildings/clutter — and
                           optionally "terrain/", "users.json", ".auth_secret").
                           Implies an air-gapped install: skips the online
                           terrain pre-download. Pair with ARES_NETWORK_POLICY=offline_only.
  --no-soapysdr            Skip the SoapySDR install. By default the installer
                           pulls SoapySDR + the open device modules (rtlsdr, uhd,
                           hackrf, airspy/airspyhf, plutosdr, bladerf, lms7) on
                           apt-based distros so the native UAS demod / DF pulls
                           IQ from a plugged-in SDR straight away. SignalHound
                           (SoapySDR_SignalHound) and Epiq Sidekiq (SoapySidekiq)
                           remain vendor-gated — install them per the
                           manufacturer's instructions.
  --with-gpsd              (apt only) install gpsd + gpsd-clients so the SDR
                           console's "USB GPS via gpsd" source just works with a
                           dongle on /dev/ttyUSB*. (You can also point at the raw
                           NMEA serial device without gpsd — that needs no extra
                           package, pyserial is in requirements.txt.)
  --no-audio-decoders      Skip the source-build pass that compiles dsd-fme,
                           m17-cxx-demod, and acarsdec. By default these (and the
                           apt-shipped multimon-ng / dump1090 / rtl-ais) install
                           automatically so the DF tab's "demodulate & listen"
                           dropdown is populated out-of-the-box. Each build is a
                           couple of minutes; source clones live in
                           ~/.cache/ares-audio/.
  --with-op25              Also clone + build boatbod/op25 for P25 Phase 1/2
                           decode. Pulls all of GNU Radio (~250 MB of apt deps)
                           and takes 30–60 minutes to build. Off by default
                           because it dominates installer runtime.
  --with-sdrtrunk          Also download the SDRTrunk Java GUI release into
                           /opt/sdrtrunk (~80 MB) and symlink it as
                           /usr/local/bin/sdrtrunk. Off by default.
  --with-tetra             Also try the legacy osmocom-tetra tetra-rx build.
                           Frequently needs hand-fixes on modern toolchains;
                           off by default.
  --no-gnuradio            Skip the GNU Radio + gr-gsm install. By default,
                           ./install.sh pulls gnuradio + gnuradio-dev + gr-osmosdr
                           + python3-gnuradio (~500 MB apt) and builds the gr-gsm
                           out-of-tree module — required for the in-process
                           2G GSM control-channel decoder (Targets tab).
  --no-lte-sniffer         Skip cloning + building LTESniffer. By default
                           it's pulled into ~/.cache/ares-cellular/ for the
                           LTE PDCCH / SIB1 passive sniff path.
  --no-5g-sniffer          Skip cloning + building 5GSniffer. By default
                           Ares pulls the 5G NR SSB/MIB/SIB1 passive sniffer
                           into ~/.cache/ares-cellular/. USRP-coupled and the
                           upstream fork is academic — skip with this flag if
                           the build keeps failing on your toolchain.
  --with-srsran            Also install srsRAN from the official PPA (full
                           LTE/NR stack — large). Off by default.
  --no-wifi-bt             Skip apt-installing the WiFi (hcxdumptool, airodump-
                           ng, kismet) and BLE (bluez tools) capture programs
                           used by the Targets tab's MAC tracking.
  --no-sdr-udev            Skip the SDR udev-rule + DVB-driver-blacklist + group-
                           membership step. By default, on Linux we install
                           rtl-sdr / hackrf / airspy udev rules, blacklist the
                           kernel DVB driver (so plugging in an RTL-SDR / Kraken
                           array doesn't get hijacked by dvb_usb_rtl28xxu), and
                           add the current user to plugdev / dialout / audio.
  -h, --help               Show this help.

Offline / vendored install:
  If ./vendor/wheels/ exists, Python deps install from it (no pip→network).
  If ./vendor/npm/frontend/node_modules and ./vendor/npm/electron/node_modules
  exist, the frontend + Electron trees are restored from them (no npm→network).
  Populate that bundle on a connected machine first with:  scripts/bundle_vendor.sh
EOF
}
while [ $# -gt 0 ]; do
    case "$1" in
        --offline-bundle) OFFLINE_BUNDLE="${2:-}"; shift 2 ;;
        --offline-bundle=*) OFFLINE_BUNDLE="${1#*=}"; shift ;;
        --with-soapysdr) WITH_SOAPYSDR=true; shift ;;   # back-compat no-op (now the default)
        --no-soapysdr) WITH_SOAPYSDR=false; shift ;;
        --with-gpsd) WITH_GPSD=true; shift ;;
        --no-audio-decoders) WITH_AUDIO_DECODERS=false; shift ;;
        --with-audio-decoders) WITH_AUDIO_DECODERS=true; shift ;;   # back-compat no-op (default)
        --with-op25) WITH_OP25=true; shift ;;
        --with-sdrtrunk) WITH_SDRTRUNK=true; shift ;;
        --with-tetra) WITH_TETRA=true; shift ;;
        --no-gnuradio) WITH_GNURADIO=false; shift ;;
        --with-gnuradio) WITH_GNURADIO=true; shift ;;
        --no-lte-sniffer) WITH_LTE_SNIFFER=false; shift ;;
        --with-lte-sniffer) WITH_LTE_SNIFFER=true; shift ;;
        --with-5g-sniffer) WITH_5G_SNIFFER=true; shift ;;
        --no-5g-sniffer)   WITH_5G_SNIFFER=false; shift ;;
        --with-srsran) WITH_SRSRAN=true; shift ;;
        --no-wifi-bt) WITH_WIFI_BT=false; shift ;;
        --with-wifi-bt) WITH_WIFI_BT=true; shift ;;
        --no-sdr-udev) WITH_SDR_UDEV=false; shift ;;
        -h|--help) usage; exit 0 ;;
        *) warn "Unknown option: $1"; usage; exit 1 ;;
    esac
done

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║         Ares  Installer  v5.2 (alpha)            ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ── 1. Check Python 3.10+ ─────────────────────────────────────────────────────
log "Checking Python version..."
PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3.10 python3; do
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
            maybe_sudo apt-get update -qq
            apt_install python3 python3-pip python3-venv || apt_install python3 python3-pip python3-full
        elif command -v dnf &>/dev/null; then
            maybe_sudo dnf install -y python3 python3-pip
        elif command -v pacman &>/dev/null; then
            maybe_sudo pacman -Sy --noconfirm python python-pip
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
        # Try NodeSource (recent Node on Ubuntu/Pop/Debian/Kali); fall back to the distro package.
        if [ "$(id -u 2>/dev/null || echo 1)" = "0" ]; then
            curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt_install nodejs || apt_install nodejs npm
        elif command -v sudo >/dev/null 2>&1; then
            curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && apt_install nodejs || apt_install nodejs npm
        else
            err "need root to install Node.js — install 'sudo', re-run as root, or install Node 18+ manually from https://nodejs.org"
        fi
    elif [ "$OS" = "Darwin" ] && command -v brew &>/dev/null; then
        brew install node@20
    else
        err "Please install Node.js 18+ from https://nodejs.org"
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
    # `python -m venv` may need the matching apt package. Names vary by distro/version
    # (python3.12-venv on Ubuntu, python3-venv as a fallback, python3-full as a last resort).
    if ! $PYTHON -m venv --help &>/dev/null 2>&1 || ! $PYTHON -c "import ensurepip" &>/dev/null 2>&1; then
        log "Installing the venv package for Python ${PYTHON_PKG_VER}..."
        apt_install "python${PYTHON_PKG_VER}-venv" 2>/dev/null \
            || apt_install python3-venv 2>/dev/null \
            || apt_install python3-full 2>/dev/null \
            || warn "couldn't install a python venv package automatically — if 'python3 -m venv' fails, install python3-venv (or python3-full) for your distro and re-run."
        apt_install python3-pip 2>/dev/null || true
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
VENDOR_WHEELS="$SCRIPT_DIR/vendor/wheels"
if [ -d "$VENDOR_WHEELS" ] && [ -n "$(ls -A "$VENDOR_WHEELS" 2>/dev/null)" ]; then
    ok "vendor/wheels/ present ($(ls "$VENDOR_WHEELS" | wc -l) wheel(s)) — installing offline"
    $PIP install --no-index --find-links "$VENDOR_WHEELS" -r "$SCRIPT_DIR/backend/requirements.txt" --quiet \
        || { warn "offline install from vendor/wheels failed — falling back to online pip"; $PIP install -r "$SCRIPT_DIR/backend/requirements.txt" --quiet; }
else
    $PIP install -r "$SCRIPT_DIR/backend/requirements.txt" --quiet
fi
ok "Python dependencies installed"

# ── 4a. SoapySDR + open device modules (live IQ from a plugged-in SDR) ──
# Default-on so the SDR console reads "Backend: soapysdr" instead of "synthetic_iq"
# the moment a radio is plugged in. Opt out with --no-soapysdr.
if [ "$WITH_SOAPYSDR" = "true" ]; then
    if [ "$OS" = "Linux" ] && command -v apt &>/dev/null; then
        log "Installing SoapySDR + open device modules (rtlsdr / uhd / hackrf / airspy / airspyhf / plutosdr / bladerf / lms7)..."
        # Each package is installed best-effort so one missing module doesn't fail the run
        # (older Ubuntu LTS won't have soapysdr-module-airspyhf, for instance).
        maybe_sudo apt-get update -qq 2>/dev/null || true
        # Core: library + Python binding + CLI tools.
        apt_install soapysdr-tools libsoapysdr-dev python3-soapysdr 2>/dev/null || \
            warn "core SoapySDR packages failed to install — check that universe/community is enabled on this distro."
        # Native device drivers + headers (so SoapySDR modules have something to talk to,
        # AND so KrakenSDR / RTL-SDR / HackRF / ANTSDR / Airspy work without manual setup).
        # rtl-sdr     — KrakenSDR is 5× RTL-SDR over a hub
        # hackrf      — HackRF One
        # airspyhf    — Airspy HF+ Discovery
        # libiio0     — PlutoSDR / ANTSDR e200 (USB or Ethernet libiio)
        # libad9361   — same family (Pluto / ANTSDR / Adalm)
        apt_install rtl-sdr librtlsdr-dev hackrf libhackrf-dev airspy libairspy-dev \
                     airspyhf libairspyhf-dev libiio0 libiio-utils libad9361-0 libad9361-dev \
                     bladerf libbladerf-dev libuhd-dev uhd-host 2>/dev/null \
            || warn "Some native SDR driver packages weren't available — Ares will still detect what was installed."
        # Pull UHD firmware/images for USRP at first use (small download; idempotent).
        if command -v uhd_images_downloader >/dev/null 2>&1; then
            maybe_sudo uhd_images_downloader 2>/dev/null || warn "uhd_images_downloader failed — run it manually if you use a USRP."
        fi
        # Open device modules — try each independently so one unavailable package doesn't skip the others.
        for mod in rtlsdr uhd hackrf airspy airspyhf plutosdr bladerf lms7 mirisdr remote; do
            apt_install "soapysdr-module-${mod}" 2>/dev/null || \
                warn "soapysdr-module-${mod} not available on this distro — skipping (the radio family it covers won't be detected unless built from source)."
        done

        # Mirror the system Python's SoapySDR binding into the venv. python3-soapysdr is a system
        # package, not a PyPI wheel, so the venv won't see it otherwise. We must probe the *system*
        # python (not the activated venv's python — `command -v python3` resolves to the venv here),
        # so try /usr/bin/python3 and friends explicitly, skipping anything inside the venv tree.
        SYS_PY=""
        for cand in /usr/bin/python3 /usr/bin/python3.13 /usr/bin/python3.12 /usr/bin/python3.11 /usr/bin/python3.10 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
            if [ -x "$cand" ] && [[ "$cand" != "$VENV_DIR"/* ]]; then SYS_PY="$cand"; break; fi
        done
        if [ -n "$SYS_PY" ]; then
            SOAPY_FILE="$("$SYS_PY" - <<'PYEOF' 2>/dev/null || true
try:
    import SoapySDR
    print(getattr(SoapySDR, "__file__", "") or "")
except Exception:
    pass
PYEOF
)"
            if [ -n "$SOAPY_FILE" ] && [ -f "$SOAPY_FILE" ]; then
                SOAPY_DIR="$(dirname "$SOAPY_FILE")"
                PY_VER="$($PYTHON -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
                SP="$VENV_DIR/lib/python${PY_VER}/site-packages"
                mkdir -p "$SP"
                ln -sf "$SOAPY_FILE" "$SP/SoapySDR.py" 2>/dev/null || true
                # The compiled extension lives alongside SoapySDR.py — name varies by Python ABI tag.
                for so in "$SOAPY_DIR"/_SoapySDR*.so; do
                    [ -f "$so" ] && ln -sf "$so" "$SP/$(basename "$so")" 2>/dev/null || true
                done
                # Sanity-check: can the venv import it now?
                if "$VENV_DIR/bin/python" -c "import SoapySDR" 2>/dev/null; then
                    ok "SoapySDR exposed to venv ($SOAPY_FILE)"
                else
                    warn "Linked SoapySDR into the venv but import still fails — try 'apt install python3-soapysdr' and re-run."
                fi
            else
                warn "python3-soapysdr is installed but the Python binding wasn't found in the system Python — the venv won't see Soapy until it is."
            fi
        fi
        ok "SoapySDR installed (RTL-SDR / USRP / HackRF / Airspy / AirspyHF / Pluto / BladeRF / LimeSDR work out of the box; SignalHound + Epiq Sidekiq need vendor packages)."
    elif [ "$OS" = "Linux" ]; then
        warn "Not an apt-based distro — install SoapySDR manually: the package names vary (Fedora: 'SoapySDR python3-SoapySDR soapy-sdr-module-*'; Arch: 'soapysdr python-soapysdr soapyrtlsdr soapyuhd soapyhackrf soapyairspy soapyplutosdr'). After install, the venv will pick up the binding on next launch."
    elif [ "$OS" = "Darwin" ]; then
        if command -v brew &>/dev/null; then
            log "Installing SoapySDR via Homebrew (open device modules included)..."
            brew install soapysdr 2>/dev/null || warn "brew install soapysdr failed — install manually."
            warn "macOS: the SoapySDR Python binding from Homebrew may need 'pip install --upgrade SoapySDR' from a wheel or a from-source build — until then the SDR console may stay on synthetic_iq."
        else
            warn "Homebrew not found — install brew, then 'brew install soapysdr'."
        fi
    fi
fi

# ── 4a^. SDR udev rules + DVB-driver blacklist + user-group membership ──────
# What this fixes on a stock Kali / Debian / Ubuntu image:
#   1. RTL-SDR (and therefore KrakenSDR — five RTL-SDRs over a USB hub) get
#      hijacked by the kernel DVB driver (dvb_usb_rtl28xxu) the moment they're
#      plugged in. We blacklist it so SoapySDR's rtlsdr module can claim the
#      device.
#   2. SDR udev rules ship with the device packages — make sure they're loaded.
#   3. Without plugdev / dialout membership the user can't open the USB device
#      or the GPSDO serial port. We add the running user (not root) to those
#      groups so re-login is the only step left.
#   4. KrakenSDR Direction-of-Arrival heads need 'dialout' too (their NMEA
#      passthrough is a USB-CDC serial port on the same physical device).
#
# Skip with --no-sdr-udev.
if [ "$WITH_SDR_UDEV" = "true" ] && [ "$OS" = "Linux" ] && command -v apt &>/dev/null; then
    log "Configuring SDR udev rules, DVB blacklist, and user groups..."

    # ── DVB driver blacklist ────────────────────────────────────────────────
    # Without this the kernel grabs the RTL2832U-based devices (RTL-SDR,
    # KrakenSDR, NESDR) as a DVB tuner and SoapySDR_rtlsdr can't open them.
    if [ ! -f /etc/modprobe.d/blacklist-rtl-sdr.conf ] || \
       ! grep -q 'dvb_usb_rtl28xxu' /etc/modprobe.d/blacklist-rtl-sdr.conf 2>/dev/null; then
        maybe_sudo bash -c 'cat > /etc/modprobe.d/blacklist-rtl-sdr.conf' <<'EOF'
# Ares — blacklist the kernel DVB driver so RTL-SDR / KrakenSDR / NESDR devices
# can be claimed by libusb-based SDR drivers (SoapySDR, rtl_sdr, etc.).
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
        ok "Wrote /etc/modprobe.d/blacklist-rtl-sdr.conf (reboot or 'rmmod dvb_usb_rtl28xxu rtl2832' to apply now)."
        # Unload the modules now if they're loaded, so the user doesn't need to reboot
        # to use a freshly-plugged-in radio. Failures are non-fatal (modules may already be absent).
        maybe_sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
        maybe_sudo modprobe -r rtl2832 2>/dev/null || true
    else
        ok "DVB driver blacklist already in place."
    fi

    # ── udev rules ──────────────────────────────────────────────────────────
    # The rtl-sdr / hackrf / airspy packages each drop their own rules in
    # /lib/udev/rules.d/. We just need to reload udev to pick them up.
    # KrakenSDR's "kraken-doa" plugin also ships a rule (krakensdr.rules) that
    # gives the operator's TTY access to the array's NMEA serial passthrough —
    # we drop a copy here in case the operator hasn't installed kraken-doa yet.
    if [ ! -f /etc/udev/rules.d/99-krakensdr.rules ]; then
        maybe_sudo bash -c 'cat > /etc/udev/rules.d/99-krakensdr.rules' <<'EOF'
# Ares — KrakenSDR (5× RTL2832U over a USB hub + an STM32 GPSDO/CDC-serial).
# The five RTL-SDR dongles are picked up by /lib/udev/rules.d/rtl-sdr.rules
# (provided by the rtl-sdr package). The CDC-serial passthrough that exposes
# the on-board GPSDO + heading/orientation NMEA stream needs an explicit rule
# so the user can read it without sudo.
SUBSYSTEMS=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="5740", MODE="0666", GROUP="dialout", SYMLINK+="krakensdr-gpsdo"
EOF
        ok "Wrote /etc/udev/rules.d/99-krakensdr.rules"
    fi
    # ANTSDR e200: a libiio device exposed over USB as a CDC-ACM and an Ethernet-over-USB.
    # The CDC-ACM port shows up as /dev/ttyACM* — already covered by dialout. The
    # libiio side is on USB bulk, claimed by libiio0; nothing else is required.
    if [ ! -f /etc/udev/rules.d/99-antsdr-pluto.rules ]; then
        maybe_sudo bash -c 'cat > /etc/udev/rules.d/99-antsdr-pluto.rules' <<'EOF'
# Ares — Analog Devices PlutoSDR / ANTSDR e200 (libiio over USB).
SUBSYSTEMS=="usb", ATTRS{idVendor}=="0456", ATTRS{idProduct}=="b673", MODE="0666", GROUP="plugdev"
# ANTSDR e200 vendor/product (Microphase) — kept open for libiio.
SUBSYSTEMS=="usb", ATTRS{idVendor}=="0456", ATTRS{idProduct}=="b674", MODE="0666", GROUP="plugdev"
EOF
        ok "Wrote /etc/udev/rules.d/99-antsdr-pluto.rules"
    fi
    # Reload udev so the rules take effect without re-plugging.
    maybe_sudo udevadm control --reload-rules 2>/dev/null || true
    maybe_sudo udevadm trigger 2>/dev/null || true

    # ── User groups ────────────────────────────────────────────────────────
    # Add the *invoking* user (not root) to the SDR-relevant groups. If the
    # installer is being run with `sudo`, $SUDO_USER points at the right user.
    TARGET_USER="${SUDO_USER:-$(id -un 2>/dev/null || echo "")}"
    if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != "root" ]; then
        ADDED_GROUPS=()
        for grp in plugdev dialout audio video; do
            if getent group "$grp" >/dev/null 2>&1 && ! id -nG "$TARGET_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$grp"; then
                maybe_sudo usermod -aG "$grp" "$TARGET_USER" 2>/dev/null && ADDED_GROUPS+=("$grp")
            fi
        done
        # 'usrp' is created by libuhd-dev's postinst; add it if it exists.
        if getent group usrp >/dev/null 2>&1 && ! id -nG "$TARGET_USER" 2>/dev/null | tr ' ' '\n' | grep -qx usrp; then
            maybe_sudo usermod -aG usrp "$TARGET_USER" 2>/dev/null && ADDED_GROUPS+=("usrp")
        fi
        if [ ${#ADDED_GROUPS[@]} -gt 0 ]; then
            ok "Added $TARGET_USER to: ${ADDED_GROUPS[*]} — log out and back in (or 'newgrp $grp') for it to take effect."
        else
            ok "User $TARGET_USER is already in the SDR-relevant groups."
        fi
    fi
fi

# ── 4a'. Optional: gpsd (so a USB GPS dongle is plug-and-play under the SDR console) ──
if [ "$WITH_GPSD" = "true" ]; then
    if [ "$OS" = "Linux" ] && command -v apt &>/dev/null; then
        log "Installing gpsd + gpsd-clients..."
        # Pre-seed: don't ask "auto-start gpsd?" — we want it on so the SDR console
        # sees the dongle, but unattended.
        echo "gpsd gpsd/start_daemon boolean true"        | maybe_sudo debconf-set-selections
        echo "gpsd gpsd/usbauto         boolean true"     | maybe_sudo debconf-set-selections
        echo "gpsd gpsd/device          string  /dev/ttyUSB0" | maybe_sudo debconf-set-selections
        apt_install gpsd gpsd-clients 2>/dev/null && \
            ok "gpsd installed — plug a USB GPS dongle in and choose 'USB GPS via gpsd' in the SDR console." || \
            warn "gpsd install failed — install it manually with 'apt install gpsd gpsd-clients'."
    else
        warn "--with-gpsd only auto-installs on apt-based distros."
    fi
fi

# ── 4a''. Audio decoders for the DF tool's "demodulate & listen" feature ─────
# The DF panel lists DMR / P25 / TETRA / NXDN / dPMR / D-STAR / YSF / M17 /
# POCSAG / FLEX / AIS / ACARS / ADS-B. Most of those are decoded by shelling
# out to an installed open-source program — Ares can't vendor them (AMBE /
# ACELP / IMBE vocoders are patent-encumbered, so the binaries can't be
# redistributed even though the source is open). We install the apt-shipped
# ones (multimon-ng, dump1090, rtl-ais) AND, by default, clone+build the
# source-build ones (dsd-fme, op25, m17-cxx-demod, acarsdec) into /usr/local
# and drop sdrtrunk into /opt. Opt out with --no-audio-decoders.
if [ "$OS" = "Linux" ] && command -v apt &>/dev/null; then
    log "Installing audio decoders (multimon-ng / dump1090 / rtl-ais)..."
    maybe_sudo apt-get update -qq 2>/dev/null || true

    # Pre-seed dump1090-mutability so the post-install script doesn't open
    # a debconf TUI asking "auto-start via init-script?". We answer NO —
    # Ares does its own ADS-B / Mode-S decode in-process (see backend/app/
    # core/decoders/mode_s.py), and an auto-started dump1090 would hold the
    # RTL-SDR open and starve Ares of the device.
    # Template key is `dump1090-mutability/auto-start` (verified against
    # /var/lib/dpkg/info/dump1090-mutability.templates on Debian 12 / Ubuntu 24.04).
    echo "dump1090-mutability dump1090-mutability/auto-start boolean false" \
        | maybe_sudo debconf-set-selections 2>/dev/null || true

    INSTALLED_DECODERS=()
    # multimon-ng: POCSAG, FLEX, AFSK, ZVEI
    apt_install multimon-ng 2>/dev/null \
        && INSTALLED_DECODERS+=("multimon-ng") \
        || warn "multimon-ng not in repos — POCSAG/FLEX decoding will be marked 'decoder not installed'."
    # dump1090 (ADS-B 1090 MHz) — flightaware fork preferred; fall back to mutability
    apt_install dump1090-fa 2>/dev/null \
        && INSTALLED_DECODERS+=("dump1090-fa") \
        || (apt_install dump1090-mutability 2>/dev/null \
            && INSTALLED_DECODERS+=("dump1090-mutability") \
            || warn "dump1090 not in repos — Ares' in-process ADS-B decoder still works without it.")
    # If the mutability daemon ended up enabled by some upstream override, disable it
    # so it doesn't fight Ares for the RTL-SDR every reboot.
    if systemctl list-unit-files 2>/dev/null | grep -q '^dump1090-mutability'; then
        maybe_sudo systemctl disable --now dump1090-mutability 2>/dev/null || true
    fi
    # rtl-ais (AIS marine)
    apt_install rtl-ais 2>/dev/null \
        && INSTALLED_DECODERS+=("rtl-ais") \
        || warn "rtl-ais not in repos — AIS decoding needs a manual build (github.com/dgiardini/rtl-ais)."
    # acarsdec (ACARS — usually not in apt; we source-build it below if missing)

    # ── Source-build the patent-encumbered decoders into /usr/local ─────────
    # Idempotent: each repo is cloned to ~/.cache/ares-audio/<name>, built once,
    # and skipped on subsequent runs if the binary is already on PATH.
    #
    # Default (fast) set: dsd-fme, m17-cxx-demod, acarsdec. Each is a couple of
    # minutes. The slow / unreliable ones (op25, sdrtrunk, tetra-rx) are opt-in
    # via --with-op25 / --with-sdrtrunk / --with-tetra so a normal install never
    # blocks for a 30-minute GNU Radio rebuild that looks like a hang.
    if [ "$WITH_AUDIO_DECODERS" = "true" ]; then
        AUDIO_CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/ares-audio"
        mkdir -p "$AUDIO_CACHE"
        log "Auto-building DMR / M17 / ACARS decoders into /usr/local (cached at $AUDIO_CACHE)..."
        # Build prerequisites — common to most of them.
        apt_install build-essential cmake git pkg-config \
                     libsndfile1-dev libitpp-dev libpulse-dev \
                     libliquid-dev libusb-1.0-0-dev \
                     librtlsdr-dev libhackrf-dev libairspy-dev libairspyhf-dev \
                     libcodec2-dev libboost-dev libboost-system-dev libboost-thread-dev \
                     pulseaudio-utils sox 2>/dev/null || \
            warn "Some build prerequisites failed to install — decoder builds may fall back to skip."

        # Run a long-running command with a heartbeat. Writes the command's stdout
        # and stderr to <logfile>, prints a "." every 2 seconds, and returns the
        # command's exit status. Lets the user see progress without dumping
        # thousands of compiler lines into the terminal.
        run_with_heartbeat() {  # run_with_heartbeat <logfile> <label> <cmd...>
            local logfile="$1"; local label="$2"; shift 2
            # Run in background so we can drop dots while it works
            ( "$@" >"$logfile" 2>&1 ) &
            local pid=$!
            local dot_count=0
            printf "    %s " "$label"
            while kill -0 "$pid" 2>/dev/null; do
                sleep 2
                printf "."
                dot_count=$((dot_count + 1))
                if [ "$dot_count" -ge 60 ]; then
                    # newline every 60 dots so the line doesn't get unmanageable
                    printf "\n      "; dot_count=0
                fi
            done
            wait "$pid"
            local rc=$?
            if [ "$rc" -eq 0 ]; then printf " done\n"; else printf " FAILED (exit %d)\n" "$rc"; fi
            return "$rc"
        }

        # Helper: clone-or-pull + cmake build + sudo install. Branch arg is optional
        # — pass empty string to use the repo's HEAD (the default branch). Logs go
        # to $AUDIO_CACHE/<name>.log for post-mortem when something fails.
        build_cmake() {  # build_cmake <name> <bin_check> <git_url> <branch_or_empty> [extra_cmake_args…]
            local name="$1" bin="$2" url="$3" branch="$4"; shift 4
            local extra=("$@")
            if command -v "$bin" >/dev/null 2>&1; then
                ok "$bin already on PATH — skipping rebuild."
                INSTALLED_DECODERS+=("$bin")
                return 0
            fi
            local src="$AUDIO_CACHE/$name"
            local log="$AUDIO_CACHE/$name.log"
            log "$name: cloning $url${branch:+ (branch=$branch)}..."
            if [ -d "$src/.git" ]; then
                (cd "$src" && git fetch --depth=1 origin "${branch:-HEAD}" >>"$log" 2>&1 && git reset --hard FETCH_HEAD >>"$log" 2>&1) || true
            else
                local clone_args=(--depth=1)
                [ -n "$branch" ] && clone_args+=(--branch "$branch")
                # 120-second timeout so a network hang can't freeze the installer here.
                if ! timeout 120 git clone "${clone_args[@]}" "$url" "$src" >>"$log" 2>&1; then
                    warn "$name: clone failed — see $log. Continuing."
                    return 1
                fi
            fi
            run_with_heartbeat "$log" "$name (cmake)" \
                bash -c "cd '$src' && cmake -B build -DCMAKE_BUILD_TYPE=Release ${extra[*]@Q}" || \
                { warn "$name: cmake config failed — see $log"; return 1; }
            run_with_heartbeat "$log" "$name (build)" \
                bash -c "cd '$src' && cmake --build build -j$(nproc 2>/dev/null || echo 2)" || \
                { warn "$name: build failed — see $log"; return 1; }
            if maybe_sudo cmake --install "$src/build" >>"$log" 2>&1; then
                maybe_sudo ldconfig 2>/dev/null || true
                ok "$name installed (binary: $bin)"
                INSTALLED_DECODERS+=("$bin")
                return 0
            else
                warn "$name: install step failed — see $log"
                return 1
            fi
        }

        # 1a) mbelib — open-source vocoder library that dsd-fme links against.
        # It's not in Debian/Ubuntu universe; try apt first, fall back to clone+build.
        # Without this dsd-fme's cmake config fails with "Could NOT find MBE".
        # NOTE: grep for `libmbe.so` exactly — plain "libmbe" also matches mbed-TLS
        # (libmbedtls, libmbedcrypto), which gives a false positive on systems where
        # LTESniffer's prereqs already pulled mbedTLS in.
        if ! ldconfig -p 2>/dev/null | grep -E -q '\blibmbe\.so' \
           && [ ! -f /usr/local/lib/libmbe.so ] && [ ! -f /usr/lib/libmbe.so ] \
           && [ ! -f /usr/local/lib/x86_64-linux-gnu/libmbe.so ]; then
            log "mbelib: trying apt, then source-build…"
            apt_install libmbe-dev 2>/dev/null || \
                build_cmake mbelib mbe https://github.com/szechyjs/mbelib.git master || true
        fi

        # 1) dsd-fme — DMR, dPMR, D-STAR, YSF, NXDN, EDACS-ProVoice
        #    Default branch is `audio_work` (verified against the upstream repo).
        build_cmake dsd-fme dsd-fme https://github.com/lwvmobile/dsd-fme.git audio_work || true

        # 2) m17-cxx-demod — M17 open-source digital voice (mobilinkd).
        # The repo's blaze submodule points at Bitbucket (`blaze-lib/blaze`), and
        # that URL now returns "Authentication failed" anonymously — the upstream
        # has restricted public access. Rewrite the submodule URL to the well-
        # maintained GitHub mirror at parsa/blaze before initialising. cmake's
        # internal `git submodule update --init --recursive` then succeeds.
        M17_SRC="$AUDIO_CACHE/m17-cxx-demod"
        # First make sure the repo is cloned (build_cmake won't touch it until we
        # call it, so do a clone-or-pull here).
        if [ ! -d "$M17_SRC/.git" ]; then
            timeout 120 git clone --depth=1 --branch master \
                https://github.com/mobilinkd/m17-cxx-demod.git "$M17_SRC" \
                >>"$AUDIO_CACHE/m17-cxx-demod.log" 2>&1 || true
        fi
        if [ -d "$M17_SRC/.git" ]; then
            # Rewrite the dead Bitbucket URL to the GitHub mirror, then sync + init.
            (cd "$M17_SRC" \
                && git config -f .gitmodules submodule.blaze.url https://github.com/parsa/blaze.git \
                && git submodule sync >>"$AUDIO_CACHE/m17-cxx-demod.log" 2>&1 \
                && timeout 180 git submodule update --init --recursive --depth=1 \
                    >>"$AUDIO_CACHE/m17-cxx-demod.log" 2>&1) || \
                warn "m17-cxx-demod: rewriting blaze URL → parsa/blaze failed. See $AUDIO_CACHE/m17-cxx-demod.log."
        fi
        if [ -d "$M17_SRC/blaze" ] && [ "$(ls -A "$M17_SRC/blaze" 2>/dev/null)" ]; then
            build_cmake m17-cxx-demod m17-demod https://github.com/mobilinkd/m17-cxx-demod.git master || true
        else
            warn "m17-cxx-demod: blaze submodule unavailable. Skipping build (run: cd $M17_SRC && git submodule update --init --recursive)."
        fi

        # 3) acarsdec — ACARS. Apt first, then source as fallback.
        if ! command -v acarsdec >/dev/null 2>&1; then
            if apt_install acarsdec 2>/dev/null; then
                INSTALLED_DECODERS+=("acarsdec")
            else
                build_cmake acarsdec acarsdec https://github.com/TLeconte/acarsdec.git master -DRTL=ON || true
            fi
        else
            INSTALLED_DECODERS+=("acarsdec")
        fi

        # 4) op25 — P25 Phase 1/2 — OPT-IN (--with-op25). Pulls ~250 MB of GNU
        # Radio apt deps and the upstream `./install.sh` builds for 30–60 min.
        if [ "$WITH_OP25" = "true" ]; then
            if ! command -v op25 >/dev/null 2>&1 && ! [ -d "/usr/local/share/op25" ]; then
                log "op25: pulling GNU Radio runtime (large download — first install only)..."
                # Package names on Ubuntu 24.04 / Pop noble: gr-osmosdr meta pulls
                # libgnuradio-osmosdr (versionless); don't pin the version-suffixed lib.
                apt_install gnuradio gnuradio-dev gr-osmosdr python3-numpy swig \
                             liborc-0.4-dev libitpp-dev 2>/dev/null \
                    || { warn "op25: GNU Radio prerequisites failed — skipping op25."; }
                if command -v gnuradio-config-info >/dev/null 2>&1; then
                    OP25_SRC="$AUDIO_CACHE/op25"
                    OP25_LOG="$AUDIO_CACHE/op25.log"
                    log "op25: cloning boatbod/op25 (this is the slow one — 30–60 min build follows)..."
                    if [ ! -d "$OP25_SRC/.git" ]; then
                        timeout 180 git clone --depth=1 https://github.com/boatbod/op25.git "$OP25_SRC" >>"$OP25_LOG" 2>&1 \
                            || warn "op25: clone failed — see $OP25_LOG. Skipping."
                    fi
                    if [ -d "$OP25_SRC" ] && [ -f "$OP25_SRC/install.sh" ]; then
                        log "op25: building (heartbeat dots = still alive; full log at $OP25_LOG)..."
                        run_with_heartbeat "$OP25_LOG" "op25 (gnuradio build)" \
                            bash -c "cd '$OP25_SRC' && ./install.sh </dev/null" \
                            && { ok "op25 installed"; INSTALLED_DECODERS+=("op25"); } \
                            || warn "op25 build failed — see $OP25_LOG; re-run manually with: cd $OP25_SRC && ./install.sh"
                    fi
                fi
            else
                INSTALLED_DECODERS+=("op25")
            fi
        fi

        # 5) sdrtrunk — Java GUI. OPT-IN (--with-sdrtrunk).
        if [ "$WITH_SDRTRUNK" = "true" ]; then
            if ! command -v sdrtrunk >/dev/null 2>&1 && ! [ -x /usr/local/bin/sdrtrunk ]; then
                apt_install default-jre wget unzip 2>/dev/null || true
                if command -v wget >/dev/null 2>&1 && command -v unzip >/dev/null 2>&1; then
                    log "sdrtrunk: locating latest release..."
                    ST_URL="$(timeout 30 curl -fsSL https://api.github.com/repos/DSheirer/sdrtrunk/releases/latest 2>/dev/null \
                        | grep -Eo 'https://github.com/DSheirer/sdrtrunk/releases/download/[^"]+linux-x86_64\.zip' | head -1 || true)"
                    # Fall back to wget if curl isn't available.
                    if [ -z "$ST_URL" ]; then
                        ST_URL="$(timeout 30 wget -qO- https://api.github.com/repos/DSheirer/sdrtrunk/releases/latest 2>/dev/null \
                            | grep -Eo 'https://github.com/DSheirer/sdrtrunk/releases/download/[^"]+linux-x86_64\.zip' | head -1 || true)"
                    fi
                    if [ -n "$ST_URL" ]; then
                        log "sdrtrunk: downloading $(basename "$ST_URL") (~80 MB; live progress below)..."
                        TMP_ZIP="$AUDIO_CACHE/sdrtrunk.zip"
                        if timeout 600 wget -O "$TMP_ZIP" --progress=bar:force "$ST_URL" 2>&1 | grep -E '%|saved' ; then
                            log "sdrtrunk: extracting to /opt/sdrtrunk..."
                            maybe_sudo rm -rf /opt/sdrtrunk && \
                                maybe_sudo mkdir -p /opt/sdrtrunk && \
                                maybe_sudo unzip -q "$TMP_ZIP" -d /opt/sdrtrunk
                            SD_BIN="$(maybe_sudo find /opt/sdrtrunk -maxdepth 3 -name 'sdrtrunk' -type f -executable | head -1)"
                            if [ -n "$SD_BIN" ]; then
                                maybe_sudo ln -sf "$SD_BIN" /usr/local/bin/sdrtrunk
                                ok "sdrtrunk installed (run 'sdrtrunk' to launch the GUI)"
                                INSTALLED_DECODERS+=("sdrtrunk")
                            else
                                warn "sdrtrunk: zip extracted but couldn't find the launcher — see /opt/sdrtrunk/"
                            fi
                        else
                            warn "sdrtrunk: download failed — re-run with --with-sdrtrunk or grab manually from github.com/DSheirer/sdrtrunk/releases"
                        fi
                    else
                        warn "Could not locate the latest sdrtrunk release URL — install it manually from github.com/DSheirer/sdrtrunk/releases."
                    fi
                fi
            else
                INSTALLED_DECODERS+=("sdrtrunk")
            fi
        fi

        # 6) TETRA — osmocom-tetra. OPT-IN (--with-tetra). Frequently needs hand-fixes.
        if [ "$WITH_TETRA" = "true" ]; then
            if ! command -v tetra-rx >/dev/null 2>&1; then
                TETRA_SRC="$AUDIO_CACHE/osmocom-tetra"
                TETRA_LOG="$AUDIO_CACHE/tetra.log"
                if [ ! -d "$TETRA_SRC/.git" ]; then
                    log "tetra-rx: cloning osmocom-tetra..."
                    timeout 120 git clone --depth=1 https://github.com/osmocom/osmo-tetra.git "$TETRA_SRC" >>"$TETRA_LOG" 2>&1 \
                        || warn "TETRA: clone failed — see $TETRA_LOG. Skipping."
                fi
                if [ -d "$TETRA_SRC" ] && [ -d "$TETRA_SRC/src" ]; then
                    run_with_heartbeat "$TETRA_LOG" "tetra-rx (make)" \
                        bash -c "cd '$TETRA_SRC/src' && make" \
                        && maybe_sudo install -m 755 "$TETRA_SRC/src/tetra-rx" /usr/local/bin/tetra-rx \
                        && { ok "tetra-rx installed"; INSTALLED_DECODERS+=("tetra-rx"); } \
                        || warn "tetra-rx build failed — see $TETRA_LOG."
                fi
            else
                INSTALLED_DECODERS+=("tetra-rx")
            fi
        fi
    else
        cat <<'EOF'
──────────────────────────────────────────────────────────────────────────────
[i] --no-audio-decoders set — skipping the source-build pass for dsd-fme,
    m17-cxx-demod, acarsdec. The DF tab's "demodulate & listen" dropdown will
    show only the modes whose decoders are already on PATH.
──────────────────────────────────────────────────────────────────────────────
EOF
    fi
    # Tell the user about the heavy opt-in decoders we did *not* install.
    SKIPPED_OPTIONAL=()
    [ "$WITH_OP25" != "true" ]     && SKIPPED_OPTIONAL+=("op25 (P25 — re-run with --with-op25)")
    [ "$WITH_SDRTRUNK" != "true" ] && SKIPPED_OPTIONAL+=("sdrtrunk (multi-system GUI — re-run with --with-sdrtrunk)")
    [ "$WITH_TETRA" != "true" ]    && SKIPPED_OPTIONAL+=("tetra-rx (TETRA — re-run with --with-tetra)")
    if [ ${#SKIPPED_OPTIONAL[@]} -gt 0 ]; then
        echo -e "  ${YELLOW}[i] Heavy/opt-in decoders skipped by default:${NC}"
        for d in "${SKIPPED_OPTIONAL[@]}"; do echo "    • $d"; done
    fi
    if [ ${#INSTALLED_DECODERS[@]} -gt 0 ]; then
        ok "Audio decoders ready: ${INSTALLED_DECODERS[*]}"
    else
        warn "No audio decoders auto-installed — the DF tab's 'demodulate & listen' dropdown will be empty."
    fi
fi

# ── 4a'''. Cellular / WiFi / BLE passive monitors ─────────────────────────
# Powers the Targets tab + Cellular section of the SDR console.
#
#   --with-gnuradio   (default ON)  — GNU Radio + gr-osmosdr + python3-gnuradio,
#                                     then clones+builds gr-gsm; in-process GSM
#                                     BCCH/CCCH/SDCCH decoder (gives MCC/MNC/LAC/CI,
#                                     paging TMSI, reattach IMSI when plaintext).
#   --with-lte-sniffer (default ON) — clones LTESniffer (USRP/Soapy); LTE PDCCH RNTI
#                                     + SIB1 (MCC/MNC/TAC/CellID) passive decode.
#   --with-5g-sniffer  (default ON) — clones 5GSniffer (heavy, USRP-dependent).
#   --with-srsran      (opt-in)     — apt installs srsRAN PPA (full LTE/NR stack).
#   --with-wifi-bt    (default ON)  — hcxdumptool / airodump-ng / kismet / bluez.
if [ "$OS" = "Linux" ] && command -v apt &>/dev/null; then
    CELL_CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/ares-cellular"
    mkdir -p "$CELL_CACHE"

    # ── GNU Radio + gr-gsm (in-process GSM decoder) ────────────────────────
    if [ "$WITH_GNURADIO" = "true" ]; then
        log "Installing GNU Radio + gr-osmosdr + gr-gsm via apt (Debian/Ubuntu/Pop ship them prebuilt)..."
        # Pop/Ubuntu noble has `gr-gsm` as a binary package — way faster + more
        # reliable than the source-build path (which used to fail on shallow
        # clones because gr-gsm's install step depends on `git describe` to
        # construct the .so suffix). gr-osmosdr is the right meta — there is
        # no `libgnuradio-osmosdr-dev` package on this distro.
        apt_install gnuradio gnuradio-dev gr-osmosdr gr-gsm python3-gnuradio \
                     cmake build-essential libosmocore-dev liborc-0.4-dev 2>/dev/null \
            || warn "GNU Radio core apt install failed — GSM decoder will be unavailable."

        # If the apt gr-gsm didn't install for any reason, try a SOURCE-BUILD
        # fallback. Critically: do a FULL clone (no --depth=1) so `git describe`
        # finds a tag and the install step's libgrgsm.so.<git-hash> name resolves.
        if ! python3 -c "import grgsm" 2>/dev/null && ! dpkg -s gr-gsm >/dev/null 2>&1; then
            GRGSM_SRC="$CELL_CACHE/gr-gsm"
            GRGSM_LOG="$CELL_CACHE/gr-gsm.log"
            # Nuke any prior shallow clone so the full one can land.
            [ -d "$GRGSM_SRC/.git" ] && [ "$(cd "$GRGSM_SRC" && git rev-list --count HEAD 2>/dev/null || echo 1)" -le 1 ] && rm -rf "$GRGSM_SRC"
            if [ ! -d "$GRGSM_SRC/.git" ]; then
                log "gr-gsm: full clone of ptrkrysik/gr-gsm (no --depth=1 — needs the full history for git-describe)..."
                timeout 240 git clone https://github.com/ptrkrysik/gr-gsm.git "$GRGSM_SRC" >>"$GRGSM_LOG" 2>&1 \
                    || warn "gr-gsm clone failed — see $GRGSM_LOG."
            fi
            if [ -d "$GRGSM_SRC" ] && [ -f "$GRGSM_SRC/CMakeLists.txt" ]; then
                log "gr-gsm: building (a few minutes; heartbeat = alive; log $GRGSM_LOG)..."
                run_with_heartbeat "$GRGSM_LOG" "gr-gsm (cmake)" \
                    bash -c "cd '$GRGSM_SRC' && cmake -B build -DCMAKE_BUILD_TYPE=Release" || \
                    warn "gr-gsm cmake failed — see $GRGSM_LOG."
                run_with_heartbeat "$GRGSM_LOG" "gr-gsm (build)" \
                    bash -c "cd '$GRGSM_SRC' && cmake --build build -j$(nproc 2>/dev/null || echo 2)" || \
                    warn "gr-gsm build failed — see $GRGSM_LOG."
                if maybe_sudo cmake --install "$GRGSM_SRC/build" >>"$GRGSM_LOG" 2>&1; then
                    maybe_sudo ldconfig 2>/dev/null || true
                    ok "gr-gsm built + installed (python: import grgsm)"
                else
                    warn "gr-gsm install failed — see $GRGSM_LOG."
                fi
            fi
        else
            ok "gr-gsm available — skipping source build."
        fi

        # Mirror gnuradio + grgsm Python bindings into the venv (parallels the
        # SoapySDR bridging earlier in this script). gnuradio + grgsm install
        # to /usr/lib/python3/dist-packages; the venv won't see them otherwise.
        if [ -n "$SYS_PY" ] && [ -d "$VENV_DIR/lib" ]; then
            PY_VER="$($PYTHON -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
            SP="$VENV_DIR/lib/python${PY_VER}/site-packages"
            mkdir -p "$SP"
            for mod_name in gnuradio grgsm; do
                MOD_PATH="$("$SYS_PY" -c "import ${mod_name}, os; print(os.path.dirname(${mod_name}.__file__))" 2>/dev/null || true)"
                if [ -n "$MOD_PATH" ] && [ -d "$MOD_PATH" ]; then
                    ln -sf "$MOD_PATH" "$SP/${mod_name}" 2>/dev/null || true
                fi
            done
            if "$VENV_DIR/bin/python" -c "import gnuradio, grgsm" 2>/dev/null; then
                ok "GNU Radio + gr-gsm exposed to venv → in-process GSM decoder is available."
            else
                warn "GNU Radio installed but the venv can't import it — Targets tab will fall back to LTE/wifi only."
            fi
        fi
    fi

    # ── LTESniffer (passive LTE PDCCH / SIB1) ──────────────────────────────
    if [ "$WITH_LTE_SNIFFER" = "true" ]; then
        if ! command -v LTESniffer >/dev/null 2>&1; then
            LTES_SRC="$CELL_CACHE/LTESniffer"
            LTES_LOG="$CELL_CACHE/lte-sniffer.log"
            log "LTESniffer: installing build prerequisites..."
            apt_install libuhd-dev uhd-host libfftw3-dev libmbedtls-dev libboost-program-options-dev \
                         libconfig++-dev libsctp-dev libpcsclite-dev 2>/dev/null || \
                warn "Some LTESniffer prerequisites failed to install — build may fail."
            if [ ! -d "$LTES_SRC/.git" ]; then
                log "LTESniffer: cloning SysSec-KAIST/LTESniffer..."
                # The Princeton-named fork (YaxiongXiePrinceton/LTEsniffer) is dead.
                # The maintained repo lives at github.com/SysSec-KAIST/LTESniffer.
                timeout 180 git clone --depth=1 https://github.com/SysSec-KAIST/LTESniffer.git "$LTES_SRC" >>"$LTES_LOG" 2>&1 \
                    || warn "LTESniffer clone failed — see $LTES_LOG."
            fi
            if [ -d "$LTES_SRC" ] && [ -f "$LTES_SRC/CMakeLists.txt" ]; then
                log "LTESniffer: building (a few minutes; heartbeat = alive; log $LTES_LOG)..."
                run_with_heartbeat "$LTES_LOG" "LTESniffer (cmake)" \
                    bash -c "cd '$LTES_SRC' && cmake -B build -DCMAKE_BUILD_TYPE=Release" || \
                    warn "LTESniffer cmake failed — see $LTES_LOG."
                run_with_heartbeat "$LTES_LOG" "LTESniffer (build)" \
                    bash -c "cd '$LTES_SRC' && cmake --build build -j$(nproc 2>/dev/null || echo 2)" || \
                    warn "LTESniffer build failed — see $LTES_LOG."
                if maybe_sudo cmake --install "$LTES_SRC/build" >>"$LTES_LOG" 2>&1; then
                    maybe_sudo ldconfig 2>/dev/null || true
                    ok "LTESniffer installed"
                else
                    warn "LTESniffer install failed — see $LTES_LOG."
                fi
            fi
        else
            ok "LTESniffer already on PATH — skipping rebuild."
        fi
    fi

    # ── 5GSniffer (spritelab — IEEE S&P 2023) ──────────────────────────────
    # The canonical 5G NR PDCCH passive sniffer. Decodes the broadcast SSB →
    # MIB → SIB1 chain plus PDCCH DCIs (per-UE RNTIs + RSRP). srsRAN ships as
    # a git submodule, so we must --recurse-submodules; the build requires
    # clang 14+ and a long list of -dev packages. Override the URL via
    # ARES_5GSNIFFER_GIT_URL=<url> to point at a fork (e.g. asset-group/Sni5Gect
    # for the USENIX 2024 follow-on with downlink-injection + Wireshark).
    if [ "$WITH_5G_SNIFFER" = "true" ]; then
        if ! command -v 5g_sniffer >/dev/null 2>&1 && ! command -v 5GSniffer >/dev/null 2>&1; then
            NRS_URL="${ARES_5GSNIFFER_GIT_URL:-https://github.com/spritelab/5GSniffer.git}"
            NRS_SRC="$CELL_CACHE/5GSniffer"
            NRS_LOG="$CELL_CACHE/5g-sniffer.log"
            log "5GSniffer: installing build prerequisites (apt + clang)..."
            apt_install clang cmake build-essential \
                         libuhd-dev libfftw3-dev libmbedtls-dev libsctp-dev \
                         libyaml-cpp-dev libgtest-dev libliquid-dev libconfig++-dev \
                         libzmq3-dev libspdlog-dev libfmt-dev 2>/dev/null \
                || warn "Some 5GSniffer prerequisites failed to install — build may fail."
            if [ ! -d "$NRS_SRC/.git" ]; then
                log "5GSniffer: cloning $NRS_URL (with srsRAN submodule — this is large)..."
                timeout 600 git clone --recurse-submodules "$NRS_URL" "$NRS_SRC" >>"$NRS_LOG" 2>&1 \
                    || warn "5GSniffer clone failed — see $NRS_LOG."
            fi
            if [ -d "$NRS_SRC" ] && [ -f "$NRS_SRC/CMakeLists.txt" ]; then
                # The submodule may not have come in cleanly on the first clone;
                # init it explicitly so the cmake doesn't have to.
                (cd "$NRS_SRC" && timeout 300 git submodule update --init --recursive >>"$NRS_LOG" 2>&1) || true
                log "5GSniffer: building with clang (long build — heartbeat = alive; log $NRS_LOG)..."
                run_with_heartbeat "$NRS_LOG" "5GSniffer (cmake)" \
                    bash -c "cd '$NRS_SRC' && CC=clang CXX=clang++ cmake -B build -DCMAKE_BUILD_TYPE=Release" || \
                    warn "5GSniffer cmake failed — see $NRS_LOG."
                run_with_heartbeat "$NRS_LOG" "5GSniffer (build)" \
                    bash -c "cd '$NRS_SRC' && cmake --build build -j$(nproc 2>/dev/null || echo 2)" || \
                    warn "5GSniffer build failed — see $NRS_LOG."
                # 5GSniffer's CMake doesn't install by default; symlink the binary
                # we built to /usr/local/bin so it's on PATH for the Ares cellular
                # decoder. The real binary is build/src/5g_sniffer.
                NRS_BIN="$(find "$NRS_SRC/build" -name '5g_sniffer' -type f -executable 2>/dev/null | head -1)"
                if [ -n "$NRS_BIN" ] && [ -x "$NRS_BIN" ]; then
                    maybe_sudo ln -sf "$NRS_BIN" /usr/local/bin/5g_sniffer
                    ok "5GSniffer installed (binary at $NRS_BIN, symlinked to /usr/local/bin/5g_sniffer)"
                else
                    warn "5GSniffer built but the 5g_sniffer binary wasn't found — see $NRS_LOG."
                fi
            fi
        else
            ok "5g_sniffer already on PATH — skipping rebuild."
        fi
    fi

    # ── srsRAN (opt-in) ────────────────────────────────────────────────────
    if [ "$WITH_SRSRAN" = "true" ]; then
        log "srsRAN: adding PPA + installing srsran..."
        if ! command -v srsue >/dev/null 2>&1; then
            maybe_sudo add-apt-repository -y ppa:srsran/ppa 2>/dev/null || \
                warn "Couldn't add srsRAN PPA — install software-properties-common or build from source."
            apt_install srsran 2>/dev/null && ok "srsRAN installed (srsue / srsenb on PATH)" \
                || warn "srsran apt install failed — fall back to: git clone github.com/srsran/srsran_project && cmake build."
        else
            ok "srsRAN already installed."
        fi
    fi

    # ── WiFi / BLE capture tools ───────────────────────────────────────────
    if [ "$WITH_WIFI_BT" = "true" ]; then
        log "Installing WiFi/BLE capture tools (Targets-tab MAC tracking)..."
        # kismet is on Kali but not Pop/Ubuntu repos — install each package
        # individually so a missing kismet doesn't block hcxdumptool + bluez.
        WIFI_BT_INSTALLED=()
        for pkg in aircrack-ng hcxdumptool hcxtools bluez bluez-tools tcpdump; do
            apt_install "$pkg" 2>/dev/null && WIFI_BT_INSTALLED+=("$pkg") || \
                warn "wifi-bt: $pkg not in repos — skipping."
        done
        # kismet attempt (only on Kali) — preseed the install-users prompt so the
        # post-install script doesn't open a debconf TUI on systems that have it.
        echo "kismet-capture-common kismet-capture-common/install-users boolean true" \
            | maybe_sudo debconf-set-selections 2>/dev/null || true
        apt_install kismet kismet-capture-common 2>/dev/null \
            && WIFI_BT_INSTALLED+=("kismet") \
            || true   # silently OK on non-Kali — Ares' WiFi monitor prefers hcxdumptool anyway
        if [ ${#WIFI_BT_INSTALLED[@]} -gt 0 ]; then
            ok "WiFi/BLE capture tools installed: ${WIFI_BT_INSTALLED[*]}"
        else
            warn "No WiFi/BLE capture tools installed — Targets MAC tracking unavailable."
        fi
        cat <<EOF

[i] To put a WiFi adapter into monitor mode (out-of-band step, Ares doesn't auto-toggle):
        sudo airmon-ng start <iface>            # creates <iface>mon, kills conflicts
    or  sudo ip link set <iface> down && sudo iw dev <iface> set monitor control && sudo ip link set <iface> up
    Then point the Targets/WiFi session at the monitor-mode interface (e.g. wlan0mon).

EOF
    fi
fi

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
VENDOR_FE="$SCRIPT_DIR/vendor/npm/frontend/node_modules"
if [ -d "$VENDOR_FE" ] && [ -z "${ARES_VENDOR_REFRESH:-}" ]; then
    ok "vendor/npm/frontend/node_modules present ($(du -sh "$VENDOR_FE" 2>/dev/null | cut -f1)) — restoring offline"
    rm -rf node_modules
    if command -v rsync >/dev/null 2>&1; then rsync -a "$VENDOR_FE"/ node_modules/
    else cp -a "$VENDOR_FE" node_modules; fi
else
    npm install --silent
fi
ok "Frontend npm packages installed"

# ── 6. Build frontend ─────────────────────────────────────────────────────────
log "Building frontend..."
npm run build --silent
ok "Frontend built"

# ── 7. Electron (desktop app) ─────────────────────────────────────────────────
log "Installing Electron desktop dependencies..."
cd "$SCRIPT_DIR/electron"
VENDOR_EL="$SCRIPT_DIR/vendor/npm/electron/node_modules"
if [ -d "$VENDOR_EL" ] && [ -z "${ARES_VENDOR_REFRESH:-}" ]; then
    ok "vendor/npm/electron/node_modules present ($(du -sh "$VENDOR_EL" 2>/dev/null | cut -f1)) — restoring offline"
    rm -rf node_modules
    if command -v rsync >/dev/null 2>&1; then rsync -a "$VENDOR_EL"/ node_modules/
    else cp -a "$VENDOR_EL" node_modules; fi
else
    npm install --silent
fi
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
# Tell the user about post-install steps they may need: a re-login for groups, or
# a re-plug to make the DVB-driver unload + udev rules apply to currently-attached SDRs.
if [ "$WITH_SDR_UDEV" = "true" ] && [ "$OS" = "Linux" ] && command -v apt &>/dev/null; then
    TARGET_USER="${SUDO_USER:-$(id -un 2>/dev/null || echo "")}"
    if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != "root" ]; then
        echo ""
        echo -e "  ${YELLOW}SDR setup:${NC}"
        echo -e "    • Log out and back in (or run 'newgrp plugdev') so the plugdev/dialout/audio"
        echo -e "      group additions for '${TARGET_USER}' take effect."
        echo -e "    • Unplug + re-plug any SDR that was attached during install so the kernel DVB"
        echo -e "      driver releases the device and SoapySDR can claim it."
        echo -e "    • Verify a device is seen: ${BOLD}SoapySDRUtil --find${NC}"
    fi
fi
echo ""
