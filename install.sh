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
_apt_install_native() {  # usage: _apt_install_native pkg1 pkg2 …
    maybe_sudo apt-get install "${APT_QUIET_OPTS[@]}" "$@"
}

# ── Package-manager abstraction (apt + dnf/yum). ─────────────────────────────
# The installer was originally Debian/apt-only; we now also support Rocky Linux 8/9
# (and RHEL-family / Fedora as a side-effect). The `apt_install` callsites below
# keep their Debian-style package names and dispatch through here — on dnf we
# translate to RPM names via `pkg_map`. Debian-only operations (debconf,
# add-apt-repository, NodeSource .deb script) stay gated on `[ "$PM" = "apt" ]`.
PM="none"
OS_FAMILY="unknown"
if command -v apt-get >/dev/null 2>&1; then PM="apt"; OS_FAMILY="debian"
elif command -v dnf >/dev/null 2>&1; then PM="dnf"; OS_FAMILY="rhel"
elif command -v yum >/dev/null 2>&1; then PM="yum"; OS_FAMILY="rhel"
elif command -v pacman >/dev/null 2>&1; then PM="pacman"; OS_FAMILY="arch"
elif command -v brew >/dev/null 2>&1; then PM="brew"; OS_FAMILY="macos"
fi

OS_ID=""; OS_VER_ID=""
if [ -r /etc/os-release ]; then
    OS_ID="$(. /etc/os-release 2>/dev/null; echo "${ID:-}")"
    OS_VER_ID="$(. /etc/os-release 2>/dev/null; echo "${VERSION_ID:-}")"
fi

DNF_QUIET_OPTS=(-y --setopt=install_weak_deps=False --skip-broken --nogpgcheck)

# Translate a Debian/Ubuntu-style package name → the local equivalent(s).
# May print multiple space-separated names (one-to-many) or empty (no equivalent —
# the caller will simply skip it). For unknown names we pass through unchanged
# so an exact-match RPM (cmake, git, sox, gpsd, …) just works.
pkg_map() {
    local p="$1"
    case "$PM" in
        apt) printf '%s' "$p" ;;
        dnf|yum)
            case "$p" in
                # ── core build tools ─────────────────────────────────────────
                build-essential) printf 'gcc gcc-c++ make' ;;
                pkg-config)      printf 'pkgconfig' ;;
                python3-pip)     printf 'python3-pip' ;;
                python3-venv|python3-full|python3.*-venv) printf '' ;;  # venv is built into RPM python3
                python3-numpy)   printf 'python3-numpy' ;;
                swig)            printf 'swig' ;;

                # ── SDR core (SoapySDR) ──────────────────────────────────────
                soapysdr-tools)              printf 'SoapySDR' ;;
                libsoapysdr-dev)             printf 'SoapySDR-devel' ;;
                python3-soapysdr)            printf 'python3-soapysdr' ;;
                soapysdr-module-rtlsdr)      printf 'SoapyRTLSDR' ;;
                soapysdr-module-uhd)         printf 'SoapyUHD' ;;
                soapysdr-module-hackrf)      printf 'SoapyHackRF' ;;
                soapysdr-module-airspy)      printf 'SoapyAirspy' ;;
                soapysdr-module-airspyhf)    printf 'SoapyAirspyHF' ;;
                soapysdr-module-plutosdr)    printf 'SoapyPlutoSDR' ;;
                soapysdr-module-bladerf)     printf 'SoapyBladeRF' ;;
                soapysdr-module-lms7)        printf 'SoapyLMS7' ;;
                soapysdr-module-mirisdr)     printf 'SoapyMiri' ;;
                soapysdr-module-remote)      printf 'SoapyRemote' ;;

                # ── SDR device drivers ───────────────────────────────────────
                rtl-sdr)           printf 'rtl-sdr' ;;
                librtlsdr-dev)     printf 'rtl-sdr-devel' ;;
                hackrf)            printf 'hackrf' ;;
                libhackrf-dev)     printf 'hackrf-devel' ;;
                airspy)            printf 'airspyone_host' ;;
                libairspy-dev)     printf 'airspyone_host-devel' ;;
                airspyhf)          printf 'airspyhf' ;;
                libairspyhf-dev)   printf 'airspyhf-devel' ;;
                libiio0|libiio-utils) printf 'libiio libiio-utils' ;;
                libad9361-0|libad9361-dev) printf 'libad9361-iio' ;;
                bladerf)           printf 'bladeRF' ;;
                libbladerf-dev)    printf 'bladeRF-devel' ;;
                libuhd-dev)        printf 'uhd-devel' ;;
                uhd-host)          printf 'uhd' ;;
                gpsd)              printf 'gpsd' ;;
                gpsd-clients)      printf 'gpsd-clients' ;;

                # ── DSP / audio dep libs ─────────────────────────────────────
                libsndfile1-dev)   printf 'libsndfile-devel' ;;
                libitpp-dev)       printf 'itpp-devel' ;;
                libpulse-dev)      printf 'pulseaudio-libs-devel' ;;
                libliquid-dev)     printf 'liquid-dsp-devel' ;;
                libusb-1.0-0-dev)  printf 'libusbx-devel' ;;
                libcodec2-dev)     printf 'codec2-devel' ;;
                libboost-dev|libboost-system-dev|libboost-thread-dev|libboost-program-options-dev)
                                   printf 'boost-devel' ;;
                pulseaudio-utils)  printf 'pulseaudio-utils' ;;
                sox)               printf 'sox' ;;
                multimon-ng)       printf 'multimon-ng' ;;
                libmbe-dev)        printf '' ;;     # not packaged on RHEL — source-builds
                rtl-ais)           printf '' ;;     # not in EPEL — source-build only
                dump1090-fa|dump1090-mutability) printf '' ;;  # Ares decodes in-process; RPM not packaged

                # ── GNU Radio + cellular ─────────────────────────────────────
                gnuradio)          printf 'gnuradio' ;;
                gnuradio-dev)      printf 'gnuradio-devel' ;;
                gr-osmosdr)        printf 'gr-osmosdr' ;;
                gr-gsm)            printf '' ;;                  # not in EPEL — source-build
                libosmocore-dev)   printf 'libosmocore-devel' ;;
                libosmocoding0t64|libosmocodec0t64) printf '' ;; # apt-only Ubuntu t64 suffix
                liborc-0.4-dev)    printf 'orc-devel' ;;
                libfftw3-dev)      printf 'fftw-devel' ;;
                libmbedtls-dev)    printf 'mbedtls-devel' ;;
                libsctp-dev)       printf 'lksctp-tools-devel' ;;
                libpcsclite-dev)   printf 'pcsc-lite-devel' ;;
                libudev-dev)       printf 'systemd-devel' ;;
                libconfig++-dev)   printf 'libconfig-devel' ;;
                libyaml-cpp-dev)   printf 'yaml-cpp-devel' ;;
                libgtest-dev)      printf 'gtest-devel' ;;
                libzmq3-dev)       printf 'zeromq-devel' ;;
                libspdlog-dev)     printf 'spdlog-devel' ;;
                libfmt-dev)        printf 'fmt-devel' ;;
                clang)             printf 'clang' ;;

                # ── WiFi / BT capture (Targets tab) ──────────────────────────
                aircrack-ng)       printf 'aircrack-ng' ;;
                hcxdumptool)       printf '' ;;       # not in EPEL — source-build
                hcxtools)          printf '' ;;
                bluez)             printf 'bluez' ;;
                bluez-tools)       printf 'bluez-tools' ;;
                tcpdump)           printf 'tcpdump' ;;
                kismet|kismet-capture-common) printf '' ;;  # not in EPEL

                # ── Misc ─────────────────────────────────────────────────────
                default-jre)       printf 'java-17-openjdk-headless' ;;

                # Pass-through: cmake/git/wget/unzip/sox/curl/etc. all exist by the same name.
                *) printf '%s' "$p" ;;
            esac
            ;;
        *) printf '%s' "$p" ;;   # other PMs — best-effort pass-through (unused)
    esac
}

dnf_install() {  # dnf_install pkg1 pkg2 ...  (Debian-style names; we translate)
    local mapped=() parts=()
    for p in "$@"; do
        local m; m="$(pkg_map "$p")"
        if [ -n "$m" ]; then
            read -ra parts <<<"$m"
            mapped+=("${parts[@]}")
        fi
    done
    [ ${#mapped[@]} -eq 0 ] && return 0
    maybe_sudo dnf install "${DNF_QUIET_OPTS[@]}" "${mapped[@]}"
}

# Generic dispatcher — keep the old name for source-compat, so the dozens of
# existing `apt_install ...` callsites below need no rewriting.
apt_install() {
    case "$PM" in
        apt)     _apt_install_native "$@" ;;
        dnf|yum) dnf_install "$@" ;;
        *)       return 1 ;;
    esac
}

# Replaces `command -v apt &>/dev/null` gates so dnf systems hit the same paths.
have_pkg_mgr() {
    case "$PM" in apt|dnf|yum) return 0 ;; *) return 1 ;; esac
}

# Unified "refresh the index" no-op-on-dnf wrapper for the few `apt-get update`
# calls that we want to keep idempotent across managers.
pm_refresh() {
    case "$PM" in
        apt)     maybe_sudo apt-get update -qq ;;
        dnf|yum) maybe_sudo dnf -q makecache 2>/dev/null || true ;;
    esac
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
WITH_SIGNALHOUND=true          # default-on: udev rules + SoapySignalHound bridge build (vendor SDK staged from ARES_SIGNALHOUND_SDK or pre-installed)
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
                           is handled by a dedicated --with-signalhound step
                           (default-on) — see below. Epiq Sidekiq (SoapySidekiq)
                           remains vendor-gated, install it per the manufacturer's
                           instructions.
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
  --no-signalhound         Skip the SignalHound bridge install. By default we
                           drop udev rules for the SignalHound USB vendor
                           (0x2817 — BB60C/D, SM200A/B/C, SA44/124, TG124A) and,
                           if the vendor API libs (libbb_api.so / libsm_api.so)
                           are on the system, source-build the community
                           SoapySignalHound module so the radio appears in
                           Ares' SDR console. The vendor SDK itself is closed-
                           source and not redistributable — install it once
                           manually (see the SignalHound download page) or
                           pass:
                              ARES_SIGNALHOUND_SDK=/path/to/extracted/sdk \\
                              ./install.sh
                           to have us copy the .so + .h files into /usr/local.
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
        --with-signalhound) WITH_SIGNALHOUND=true; shift ;;
        --no-signalhound)   WITH_SIGNALHOUND=false; shift ;;
        -h|--help) usage; exit 0 ;;
        *) warn "Unknown option: $1"; usage; exit 1 ;;
    esac
done

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║         Ares  Installer  v5.2 (alpha)            ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ── 0. RHEL-family bootstrap (Rocky 8/9, Alma, RHEL, CentOS Stream) ──────────
# Most of the SDR / DSP packages live in EPEL (Extra Packages for Enterprise Linux)
# and a chunk of the *-devel headers live in CRB (CodeReady Builder, called
# PowerTools on EL8). Enable both so the apt_install dispatcher actually finds
# things. We also pull python3.11 + nodejs 20 from AppStream so the Python /
# Node version checks below pass without manual setup.
if [ "$PM" = "dnf" ] || [ "$PM" = "yum" ]; then
    log "RHEL-family detected ($OS_ID $OS_VER_ID) — enabling EPEL + CRB and installing Python 3.11 / Node 20..."
    maybe_sudo dnf install "${DNF_QUIET_OPTS[@]}" epel-release 2>/dev/null \
        || warn "epel-release install failed — many SDR/DSP packages won't be found. Continuing."
    # CRB on EL9/10 / Fedora; PowerTools on EL8. Try both — whichever exists wins.
    if command -v dnf >/dev/null 2>&1; then
        # `dnf config-manager` lives in dnf-plugins-core; install if missing.
        maybe_sudo dnf install "${DNF_QUIET_OPTS[@]}" dnf-plugins-core 2>/dev/null || true
        maybe_sudo dnf config-manager --set-enabled crb        2>/dev/null \
            || maybe_sudo dnf config-manager --set-enabled powertools 2>/dev/null \
            || maybe_sudo dnf config-manager --set-enabled PowerTools 2>/dev/null || true
        # RHEL-proper (not Rocky/Alma) needs the codeready-builder subscription repo.
        if [ "$OS_ID" = "rhel" ]; then
            maybe_sudo subscription-manager repos --enable "codeready-builder-for-rhel-${OS_VER_ID%%.*}-$(uname -m)-rpms" 2>/dev/null || true
        fi
    fi
    # Python 3.11 lives in AppStream on EL8/9 as the unversioned `python3.11`
    # package. Don't touch the OS's default python3 (it's used by dnf itself).
    maybe_sudo dnf install "${DNF_QUIET_OPTS[@]}" python3.11 python3.11-devel python3.11-pip 2>/dev/null \
        || maybe_sudo dnf install "${DNF_QUIET_OPTS[@]}" python3.12 python3.12-devel python3.12-pip 2>/dev/null \
        || warn "Couldn't install python3.11/3.12 — the Python check below may fail."
    # Node.js 20 — Rocky 8 AppStream ships an nodejs:20 module; if it isn't
    # available we fall through to the NodeSource RPM script in step 2.
    if ! command -v node >/dev/null 2>&1; then
        maybe_sudo dnf module reset  -y nodejs   2>/dev/null || true
        maybe_sudo dnf module enable -y nodejs:20 2>/dev/null || true
        maybe_sudo dnf install "${DNF_QUIET_OPTS[@]}" nodejs npm 2>/dev/null || true
    fi
    # SELinux note: the backend listens on :8000 and the dev frontend on :3000.
    # Both are in the SELinux default unreserved-port range — no policy change
    # needed. If the user binds to a privileged port later they can run:
    #   sudo semanage port -a -t http_port_t -p tcp <port>
    # Firewalld is *not* opened here; that's a deployment decision.
    ok "RHEL-family bootstrap done."
fi

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
            # On EL8/9 the default `python3` is 3.6/3.9 (too old for Ares).
            # Try 3.11 (AppStream) first, then 3.12, then the unversioned default.
            maybe_sudo dnf install "${DNF_QUIET_OPTS[@]}" python3.11 python3.11-pip python3.11-devel 2>/dev/null \
                || maybe_sudo dnf install "${DNF_QUIET_OPTS[@]}" python3.12 python3.12-pip python3.12-devel 2>/dev/null \
                || maybe_sudo dnf install -y python3 python3-pip
        elif command -v pacman &>/dev/null; then
            maybe_sudo pacman -Sy --noconfirm python python-pip
        else
            err "Cannot auto-install Python. Please install Python 3.10+ manually."
        fi
        # Re-resolve: on RHEL the default `python3` is 3.6/3.9 — pick the newest 3.10+.
        for cmd in python3.13 python3.12 python3.11 python3.10 python3; do
            if command -v "$cmd" &>/dev/null && "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
                PYTHON="$cmd"; break
            fi
        done
        [ -z "$PYTHON" ] && err "Python 3.10+ still not on PATH after auto-install — install manually and re-run."
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
    elif [ "$OS" = "Linux" ] && command -v dnf &>/dev/null; then
        # Try the AppStream nodejs:20 module first (Rocky 8/9 / RHEL / Alma); fall back to NodeSource RPM.
        if maybe_sudo dnf module reset -y nodejs 2>/dev/null \
           && maybe_sudo dnf module enable -y nodejs:20 2>/dev/null \
           && maybe_sudo dnf install "${DNF_QUIET_OPTS[@]}" nodejs npm 2>/dev/null; then
            :
        else
            warn "AppStream nodejs:20 module unavailable — falling back to NodeSource RPM repo."
            if [ "$(id -u 2>/dev/null || echo 1)" = "0" ]; then
                curl -fsSL https://rpm.nodesource.com/setup_20.x | bash - \
                    && maybe_sudo dnf install -y nodejs
            elif command -v sudo >/dev/null 2>&1; then
                curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo -E bash - \
                    && maybe_sudo dnf install -y nodejs
            else
                err "need root to install Node.js — install 'sudo' or re-run as root."
            fi
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

if [ "$OS" = "Linux" ] && have_pkg_mgr; then
    PYTHON_PKG_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    # `python -m venv` may need the matching apt package on Debian-family. Names vary
    # by distro/version (python3.12-venv on Ubuntu, python3-venv as a fallback,
    # python3-full as a last resort). On RHEL-family the venv module is part of the
    # python3.x RPM itself — the pkg_map "" translation just makes these calls no-ops.
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
    if [ "$OS" = "Linux" ] && have_pkg_mgr; then
        log "Installing SoapySDR + open device modules (rtlsdr / uhd / hackrf / airspy / airspyhf / plutosdr / bladerf / lms7)..."
        # Each package is installed best-effort so one missing module doesn't fail the run
        # (older Ubuntu LTS won't have soapysdr-module-airspyhf, for instance; EPEL on
        # Rocky 8 ships only a subset of the SoapyXxx modules — they're translated by
        # pkg_map and the unavailable ones simply warn-skip).
        [ "$PM" = "apt" ] && maybe_sudo apt-get update -qq 2>/dev/null || true
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
        ok "SoapySDR installed (RTL-SDR / USRP / HackRF / Airspy / AirspyHF / Pluto / BladeRF / LimeSDR work out of the box; SignalHound is handled by the next step; Epiq Sidekiq needs vendor packages)."
    elif [ "$OS" = "Linux" ]; then
        warn "Unsupported package manager (need apt or dnf/yum) — install SoapySDR manually. Package names: Arch: 'soapysdr python-soapysdr soapyrtlsdr soapyuhd soapyhackrf soapyairspy soapyplutosdr'. After install, the venv will pick up the binding on next launch."
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
if [ "$WITH_SDR_UDEV" = "true" ] && [ "$OS" = "Linux" ] && have_pkg_mgr; then
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

# ── 4a^^. SignalHound SDR bridge (BB60C/D, SM200A/B/C, SA44/124, TG124A) ────
# SignalHound radios use a proprietary closed-source vendor API: libbb_api.so
# for the BB-series spectrum analyzers, libsm_api.so for the SM-series. The
# headers + .so files are a free download from signalhound.com but we cannot
# redistribute them. This block automates *everything else*:
#
#   1. Find a SignalHound SDK on this machine — check $ARES_SIGNALHOUND_SDK,
#      ~/Downloads (the usual place after a browser download — auto-extract
#      signal_hound_sdk_*.zip if found), ./vendor/signalhound/, and a few
#      system-wide locations. If the user runs ./install.sh as root via sudo
#      we also look in $SUDO_USER's ~/Downloads.
#   2. Drop a udev rule for the SignalHound USB vendor (0x2817) so the user
#      can claim the device without sudo (relies on plugdev membership from 4a^).
#   3. Stage libbb_api.so* / libsm_api.so* + the bundled libftd2xx.so + the
#      matching headers into /usr/local/{lib,include}, picking the right
#      per-arch (linux_x64 / aarch64) and per-distro (Red Hat 8 / Ubuntu 18.04
#      / …) folder for THIS host.
#   4. Run ldconfig + create the unversioned dev-link names (libbb_api.so →
#      libbb_api.so.5) that the SoapySignalHound CMake find_library expects.
#   5. Source-build the community SoapySignalHound module — that's the bridge
#      that makes the radio show up in Ares' SDR console.
#   6. If no SDK was found, print explicit instructions and skip the build.
#
# Works the same on Debian/Ubuntu apt and Rocky/RHEL dnf — the underlying
# build is just cmake against libsoapysdr-dev + the vendor libs.
if [ "$WITH_SIGNALHOUND" = "true" ] && [ "$OS" = "Linux" ] && have_pkg_mgr; then
    log "Configuring SignalHound SDR support (BB60C/D, SM200A/B/C, SA44/124, TG124A)..."

    # `unzip` is needed for auto-extracting a signal_hound_sdk_*.zip from
    # ~/Downloads. apt + dnf both have a `unzip` package by the same name.
    apt_install unzip 2>/dev/null || true

    # ── 1. udev rule ────────────────────────────────────────────────────────
    # Match the whole SignalHound vendor namespace so a future product ID just
    # works without us having to maintain a per-model list. (If we later find
    # the vendor's own sh_usb.rules inside the SDK, it gets installed over the
    # top — same VID match, but treating the vendor file as canonical.)
    if [ ! -f /etc/udev/rules.d/99-signalhound.rules ]; then
        maybe_sudo bash -c 'cat > /etc/udev/rules.d/99-signalhound.rules' <<'EOF'
# Ares — SignalHound SDRs (vendor 0x2817).
# Covers BB60C, BB60D, SM200A, SM200B, SM200C, SA44(B), SA124(B), TG124A.
SUBSYSTEMS=="usb", ATTRS{idVendor}=="2817", MODE="0666", GROUP="plugdev"
EOF
        maybe_sudo udevadm control --reload-rules 2>/dev/null || true
        maybe_sudo udevadm trigger 2>/dev/null || true
        ok "Wrote /etc/udev/rules.d/99-signalhound.rules"
    fi

    # ── 1b. Auto-discover a SignalHound SDK on this machine ─────────────────
    # If ARES_SIGNALHOUND_SDK isn't set, search the realistic places the user
    # might have left the SDK after downloading from signalhound.com:
    #   - $HOME/Downloads (and $SUDO_USER's Downloads if running via sudo)
    #   - $HOME/signalhound-sdk (where our standalone script extracts to)
    #   - ./vendor/signalhound/ inside the repo (for an air-gapped bundle)
    #   - /opt/signalhound, /usr/local/share/signalhound (system-wide installs)
    # We accept either an already-extracted dir (containing device_apis/) or a
    # signal_hound_sdk_*.zip we'll auto-extract into the cache.
    if [ -z "${ARES_SIGNALHOUND_SDK:-}" ]; then
        # Build the list of $HOME-like dirs to scan.
        sh_home_dirs=()
        if [ -n "${SUDO_USER:-}" ]; then
            _su_home="$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6)"
            [ -n "$_su_home" ] && sh_home_dirs+=("$_su_home")
        fi
        [ -n "${HOME:-}" ] && sh_home_dirs+=("$HOME")

        sh_cache_root="${XDG_CACHE_HOME:-$HOME/.cache}/ares-sdr/signalhound-sdk"

        # Try each candidate location in priority order.
        for h in "${sh_home_dirs[@]}"; do
            # (a) Already-extracted dir under $HOME (most likely if the user
            # ran our standalone script earlier, or unzipped manually).
            for d in "$h/signalhound-sdk/signal_hound_sdk" "$h/signalhound-sdk" \
                     "$h/Downloads/signal_hound_sdk" "$h/Downloads"/signal_hound_sdk_*; do
                if [ -d "$d/device_apis" ] || [ -d "$d/signal_hound_sdk/device_apis" ]; then
                    ARES_SIGNALHOUND_SDK="$d"; break 2
                fi
            done
            # (b) Zip in ~/Downloads — pick the newest signal_hound_sdk_*.zip
            # and auto-extract into the cache.
            _zip="$(ls -1t "$h/Downloads"/signal_hound_sdk*.zip 2>/dev/null | head -1)"
            if [ -n "$_zip" ] && [ -f "$_zip" ]; then
                if command -v unzip >/dev/null 2>&1; then
                    log "Auto-extracting $_zip → $sh_cache_root ..."
                    rm -rf "$sh_cache_root"; mkdir -p "$sh_cache_root"
                    if unzip -q "$_zip" -d "$sh_cache_root"; then
                        ARES_SIGNALHOUND_SDK="$sh_cache_root"; break
                    else
                        warn "unzip failed on $_zip — see if the archive is intact."
                    fi
                else
                    warn "Found $_zip but unzip isn't installed — install 'unzip' and re-run."
                fi
            fi
        done
        # (c) Repo-vendored layout. Air-gapped bundles can drop the unzipped
        # SDK at ./vendor/signalhound/ to make the install fully offline.
        if [ -z "${ARES_SIGNALHOUND_SDK:-}" ] && [ -d "$SCRIPT_DIR/vendor/signalhound" ]; then
            ARES_SIGNALHOUND_SDK="$SCRIPT_DIR/vendor/signalhound"
        fi
        # (d) System-wide installs (someone already ran the vendor's install.sh
        # to /opt or /usr/local/share).
        if [ -z "${ARES_SIGNALHOUND_SDK:-}" ]; then
            for d in /opt/signalhound /opt/signal_hound_sdk \
                     /usr/local/share/signalhound /usr/local/share/signal_hound_sdk \
                     /var/cache/signalhound; do
                if [ -d "$d/device_apis" ] || [ -d "$d/signal_hound_sdk/device_apis" ]; then
                    ARES_SIGNALHOUND_SDK="$d"; break
                fi
            done
        fi
        [ -n "${ARES_SIGNALHOUND_SDK:-}" ] && \
            ok "Auto-discovered SignalHound SDK at: $ARES_SIGNALHOUND_SDK"
    fi

    # ── 2. Vendor lib detection / staging from ARES_SIGNALHOUND_SDK ─────────
    # SignalHound's real-world SDK layout (signal_hound_sdk_<date>.zip) is:
    #   signal_hound_sdk/device_apis/{bb_series,sm_series,pcr_series,...}/include/*.h
    #   signal_hound_sdk/device_apis/<fam>/lib/aarch64/libbb_api.so.X.Y.Z
    #   signal_hound_sdk/device_apis/<fam>/lib/linux_x64/Ubuntu 18.04/libbb_api.so.X.Y.Z
    #   signal_hound_sdk/device_apis/<fam>/lib/linux_x64/Red Hat 8/libbb_api.so.X.Y.Z
    #   signal_hound_sdk/device_apis/<fam>/lib/linux_x64/sh_usb.rules     (vendor udev)
    #   signal_hound_sdk/device_apis/<fam>/lib/macos_arm/libbb_api.X.Y.Z.dylib
    # Two real consequences of that layout:
    #   (a) blindly walking the SDK with `find` would copy aarch64 + macOS
    #       .so/.dylib onto an x86_64 Linux host. We must pick the right
    #       per-arch + per-distro folder up front.
    #   (b) the SDK only ships versioned files (libbb_api.so.5.0.9); the
    #       SONAME symlinks (libbb_api.so.5) and the unversioned dev-link
    #       (libbb_api.so) don't exist. ldconfig creates the SONAME link
    #       from the binary's embedded soname; we add the unversioned link
    #       ourselves so `-lbb_api` resolves in the SoapySignalHound build.
    SH_LIBS_FOUND=false
    SH_FAMILIES=()
    sh_have_lib() {  # sh_have_lib bb|sm → 0 if /usr/local/lib (or distro-libdir) has libfoo_api.so*
        local fam="$1" d
        for d in /usr/local/lib /usr/lib /usr/lib64 \
                 /usr/local/lib/x86_64-linux-gnu /usr/lib/x86_64-linux-gnu \
                 /usr/local/lib/aarch64-linux-gnu /usr/lib/aarch64-linux-gnu; do
            ls "$d"/lib${fam}_api.so* >/dev/null 2>&1 && return 0
        done
        return 1
    }
    sh_have_lib bb && { SH_LIBS_FOUND=true; SH_FAMILIES+=("bb"); }
    sh_have_lib sm && { SH_LIBS_FOUND=true; SH_FAMILIES+=("sm"); }

    if [ -n "${ARES_SIGNALHOUND_SDK:-}" ] && [ -d "$ARES_SIGNALHOUND_SDK" ]; then
        # Locate the SDK root that contains device_apis/. Accept either the
        # extraction parent dir, the signal_hound_sdk/ dir itself, or any nested
        # dir up to 3 levels in — whichever the user pointed us at.
        SH_ROOT=""
        for cand in "$ARES_SIGNALHOUND_SDK" "$ARES_SIGNALHOUND_SDK/signal_hound_sdk"; do
            [ -d "$cand/device_apis" ] && SH_ROOT="$cand" && break
        done
        if [ -z "$SH_ROOT" ]; then
            _found="$(find "$ARES_SIGNALHOUND_SDK" -maxdepth 3 -type d -name device_apis 2>/dev/null | head -1)"
            [ -n "$_found" ] && SH_ROOT="$(dirname "$_found")"
        fi

        if [ -z "${SH_ROOT:-}" ] || [ ! -d "$SH_ROOT/device_apis" ]; then
            warn "Couldn't locate device_apis/ under $ARES_SIGNALHOUND_SDK — pass the SDK root that contains device_apis/."
        else
            log "Staging SignalHound SDK from $SH_ROOT ..."

            # Pick the source lib folder for THIS host. linux_x64/ is split per
            # distro inside the SDK — match against /etc/os-release.
            sh_arch="$(uname -m)"
            sh_lib_arch=""
            case "$sh_arch" in
                x86_64)  sh_lib_arch="linux_x64" ;;
                aarch64) sh_lib_arch="aarch64"   ;;
                *) warn "SignalHound SDK has no $sh_arch build — staging skipped."; sh_lib_arch="" ;;
            esac
            # Preference order inside linux_x64/. Newest + most-compatible first;
            # match family for the host distro. (Ubuntu 18.04 / Red Hat 8 builds
            # are the most recent in the SDK; the older variants are legacy.)
            sh_distro_pref=()
            case "${OS_ID:-}/${OS_FAMILY:-}" in
                rhel/rhel|rocky/rhel|almalinux/rhel|fedora/rhel|centos/rhel)
                    sh_distro_pref=("Red Hat 8" "Red Hat 7" "CentOS 7" "Ubuntu 18.04" "Ubuntu 14.04") ;;
                *)
                    sh_distro_pref=("Ubuntu 18.04" "Red Hat 8" "CentOS 7" "Red Hat 7" "Ubuntu 14.04") ;;
            esac

            SH_STAGED=()
            SH_UDEV_FROM=""
            for fam in bb_series sm_series; do
                fam_path="$SH_ROOT/device_apis/$fam"
                [ -d "$fam_path" ] || continue
                # Pick the source lib dir for this family.
                src_dir=""
                if [ "$sh_lib_arch" = "linux_x64" ]; then
                    for d in "${sh_distro_pref[@]}"; do
                        if [ -d "$fam_path/lib/linux_x64/$d" ]; then
                            src_dir="$fam_path/lib/linux_x64/$d"; break
                        fi
                    done
                elif [ -n "$sh_lib_arch" ] && [ -d "$fam_path/lib/$sh_lib_arch" ]; then
                    src_dir="$fam_path/lib/$sh_lib_arch"
                fi
                if [ -z "$src_dir" ]; then
                    warn "$fam: no SDK build for arch=$sh_arch on this distro — skipping."
                    continue
                fi
                log "  $fam ← $src_dir"
                # Copy every .so* in that dir (covers libbb_api.so.X.Y.Z, the
                # bundled libftd2xx.so, libsm_api.so.X.Y.Z, etc.).
                for so in "$src_dir"/*.so*; do
                    [ -f "$so" ] || continue
                    base="$(basename "$so")"
                    maybe_sudo install -m 755 "$so" "/usr/local/lib/$base" && SH_STAGED+=("$base")
                done
                # Headers from the family's include/.
                if [ -d "$fam_path/include" ]; then
                    for hdr in "$fam_path/include"/*.h; do
                        [ -f "$hdr" ] || continue
                        maybe_sudo install -m 644 "$hdr" "/usr/local/include/$(basename "$hdr")" \
                            && SH_STAGED+=("$(basename "$hdr")")
                    done
                fi
                # Vendor udev rule (same content across families; install once).
                # The SDK puts sh_usb.rules in the lib/<arch>/ dir, NOT inside
                # the per-distro subdir — check both locations.
                if [ -z "$SH_UDEV_FROM" ]; then
                    if   [ -f "$src_dir/sh_usb.rules" ];        then SH_UDEV_FROM="$src_dir/sh_usb.rules"
                    elif [ -f "$src_dir/../sh_usb.rules" ];     then SH_UDEV_FROM="$src_dir/../sh_usb.rules"
                    fi
                fi
            done

            if [ ${#SH_STAGED[@]} -gt 0 ]; then
                # ldconfig walks /usr/local/lib (it's in the default search path on
                # Debian + most distros via ld.so.conf.d/libc.conf) and creates the
                # SONAME symlinks from each .so's embedded soname.
                maybe_sudo ldconfig -n /usr/local/lib 2>/dev/null || true
                maybe_sudo ldconfig 2>/dev/null || true
                # Create the unversioned dev-link (libbb_api.so → libbb_api.so.5).
                # CMake / -lbb_api resolves against the unversioned name; the SDK
                # doesn't ship it.
                for stem in libbb_api libsm_api; do
                    soname="$(ls -1 /usr/local/lib/${stem}.so.* 2>/dev/null | grep -E "/${stem}\.so\.[0-9]+$" | sort -V | tail -1)"
                    if [ -n "$soname" ]; then
                        maybe_sudo ln -sf "$(basename "$soname")" "/usr/local/lib/${stem}.so"
                    fi
                done
                # If the vendor's udev rule was found and we don't already have one
                # in place, prefer it over our homegrown rule from step 1.
                if [ -n "$SH_UDEV_FROM" ] && [ -f "$SH_UDEV_FROM" ]; then
                    maybe_sudo install -m 644 "$SH_UDEV_FROM" /etc/udev/rules.d/99-signalhound.rules
                    maybe_sudo udevadm control --reload-rules 2>/dev/null || true
                    maybe_sudo udevadm trigger 2>/dev/null || true
                fi
                ok "Staged SignalHound vendor files into /usr/local (${#SH_STAGED[@]} files)."
                # Re-probe.
                SH_LIBS_FOUND=false; SH_FAMILIES=()
                sh_have_lib bb && { SH_LIBS_FOUND=true; SH_FAMILIES+=("bb"); }
                sh_have_lib sm && { SH_LIBS_FOUND=true; SH_FAMILIES+=("sm"); }
            else
                warn "Nothing was staged — check that the SDK contains lib/<arch>/<distro>/lib*_api.so* under device_apis/<bb_series|sm_series>/."
            fi
        fi
    fi

    # ── 3. Build the community SoapySignalHound bridge if vendor libs exist ─
    if [ "$SH_LIBS_FOUND" = "true" ]; then
        # Build prereqs (cmake/build-essential/libsoapysdr-dev/libusb already
        # pulled by the SoapySDR block above — listed here for idempotency
        # in case --no-soapysdr was passed).
        apt_install cmake build-essential libsoapysdr-dev libusb-1.0-0-dev pkg-config 2>/dev/null || true

        SH_CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/ares-sdr"
        SH_SRC="$SH_CACHE/SoapySignalHound"
        SH_LOG="$SH_CACHE/SoapySignalHound.log"
        mkdir -p "$SH_CACHE"

        # If the module is already registered, skip the rebuild.
        if command -v SoapySDRUtil >/dev/null 2>&1 && \
           SoapySDRUtil --info 2>/dev/null | grep -qi signalhound; then
            ok "SoapySDR module for SignalHound already registered — skipping rebuild."
        else
            if [ ! -d "$SH_SRC/.git" ]; then
                log "SoapySignalHound: cloning altaf-4-1/SoapySignalHound..."
                timeout 120 git clone --depth=1 https://github.com/altaf-4-1/SoapySignalHound.git "$SH_SRC" >>"$SH_LOG" 2>&1 \
                    || warn "SoapySignalHound clone failed — see $SH_LOG."
            fi
            if [ -d "$SH_SRC" ] && [ -f "$SH_SRC/CMakeLists.txt" ]; then
                log "SoapySignalHound: building (small module — ~10-30s; log $SH_LOG)..."
                { echo "==== build ($(date -Iseconds)) ===="; } >>"$SH_LOG"
                if ( cd "$SH_SRC" \
                     && cmake -B build -DCMAKE_BUILD_TYPE=Release >>"$SH_LOG" 2>&1 \
                     && cmake --build build -j"$(nproc 2>/dev/null || echo 2)" >>"$SH_LOG" 2>&1 ); then
                    if maybe_sudo cmake --install "$SH_SRC/build" >>"$SH_LOG" 2>&1; then
                        maybe_sudo ldconfig 2>/dev/null || true
                        if command -v SoapySDRUtil >/dev/null 2>&1 && \
                           SoapySDRUtil --info 2>/dev/null | grep -qi signalhound; then
                            ok "SoapySignalHound built + installed — SoapySDR now sees SignalHound devices (families: ${SH_FAMILIES[*]})."
                        else
                            warn "SoapySignalHound built but the module isn't visible to SoapySDRUtil. Check $SH_LOG."
                        fi
                    else
                        warn "SoapySignalHound install failed — see $SH_LOG."
                    fi
                else
                    warn "SoapySignalHound build failed — see $SH_LOG. Most common cause: vendor libs are for a different architecture than this host."
                fi
            fi
        fi
    else
        cat <<'EOF'

[!] SignalHound vendor SDK not detected. The SDR will not appear in Ares yet.
    The vendor API is closed-source and not in any distro repo, so we can't
    ship it with the installer — but the install is now zero-effort once you
    have the zip on the machine:

      1. Download the SDK (free, no auth required) from:
            https://signalhound.com/support/product-downloads-prdct-signal-hound/
         Pick your model → "Linux software" / "API" / "SDK". Save it to
         ~/Downloads/ (the default browser destination — that's where we look).
      2. Re-run THIS installer with no extra args:
            ./install.sh
         It auto-discovers the zip in ~/Downloads, auto-extracts it into
         the cache, picks the right per-arch + per-distro libs, and builds
         the SoapySignalHound bridge.
      3. The radio appears in Ares' SDR console on the next launch.

    Already have it somewhere else? Override the search:
        ARES_SIGNALHOUND_SDK=/path/to/sdk_or_zip_dir ./install.sh
    Or use the standalone helper after the rest of Ares is installed:
        sudo ./scripts/install-signalhound.sh ~/Downloads

EOF
    fi
fi

# ── 4a'. Optional: gpsd (so a USB GPS dongle is plug-and-play under the SDR console) ──
if [ "$WITH_GPSD" = "true" ]; then
    if [ "$OS" = "Linux" ] && have_pkg_mgr; then
        log "Installing gpsd + gpsd-clients..."
        # Debian pre-seeds so the postinst doesn't prompt. On dnf/yum the gpsd RPM
        # installs disabled — we enable it via systemctl below.
        if [ "$PM" = "apt" ]; then
            echo "gpsd gpsd/start_daemon boolean true"        | maybe_sudo debconf-set-selections
            echo "gpsd gpsd/usbauto         boolean true"     | maybe_sudo debconf-set-selections
            echo "gpsd gpsd/device          string  /dev/ttyUSB0" | maybe_sudo debconf-set-selections
        fi
        if apt_install gpsd gpsd-clients 2>/dev/null; then
            # RHEL: enable + start the socket-activated service so it picks up the dongle.
            if [ "$PM" = "dnf" ] || [ "$PM" = "yum" ]; then
                maybe_sudo systemctl enable --now gpsd.socket 2>/dev/null || true
            fi
            ok "gpsd installed — plug a USB GPS dongle in and choose 'USB GPS via gpsd' in the SDR console."
        else
            warn "gpsd install failed — install it manually with your distro's package manager."
        fi
    else
        warn "--with-gpsd only auto-installs on apt or dnf/yum systems."
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
if [ "$OS" = "Linux" ] && have_pkg_mgr; then
    log "Installing audio decoders (multimon-ng / dump1090 / rtl-ais)..."
    [ "$PM" = "apt" ] && maybe_sudo apt-get update -qq 2>/dev/null || true

    # Pre-seed dump1090-mutability so the post-install script doesn't open
    # a debconf TUI asking "auto-start via init-script?". We answer NO —
    # Ares does its own ADS-B / Mode-S decode in-process (see backend/app/
    # core/decoders/mode_s.py), and an auto-started dump1090 would hold the
    # RTL-SDR open and starve Ares of the device.
    # (Debian-only — dump1090-mutability isn't packaged on RHEL, and pkg_map
    # translates it to "" so the apt_install call below is a no-op on dnf.)
    if [ "$PM" = "apt" ]; then
        echo "dump1090-mutability dump1090-mutability/auto-start boolean false" \
            | maybe_sudo debconf-set-selections 2>/dev/null || true
    fi

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
            # APPEND (>>) so multi-step logs preserve every step's output —
            # cmake config + build + install each leave their own section.
            # A header line tags each step so the log stays readable.
            { echo ""; echo "==== $label  ($(date -Iseconds)) ===="; } >>"$logfile"
            ( "$@" >>"$logfile" 2>&1 ) &
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
if [ "$OS" = "Linux" ] && have_pkg_mgr; then
    CELL_CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/ares-cellular"
    mkdir -p "$CELL_CACHE"

    # ── GNU Radio + gr-gsm (in-process GSM decoder) ────────────────────────
    if [ "$WITH_GNURADIO" = "true" ]; then
        log "Installing GNU Radio + gr-osmosdr (+ gr-gsm on apt; source-build on dnf)..."
        # Pop/Ubuntu noble has `gr-gsm` as a binary package — way faster + more
        # reliable than the source-build path (which used to fail on shallow
        # clones because gr-gsm's install step depends on `git describe` to
        # construct the .so suffix). Notes on package names:
        #   - There is NO `libgnuradio-osmosdr-dev` on this distro.
        #   - There is NO `python3-gnuradio` on Noble — Python bindings ship
        #     inside the `gnuradio` meta-package itself. Including that name
        #     in a single apt_install call would fail the whole batch and
        #     leave libosmocore-dev / liborc unattached.
        # Install each one individually so one missing package can't poison
        # the batch.
        GR_APT_INSTALLED=()
        for pkg in gnuradio gnuradio-dev gr-osmosdr gr-gsm \
                    cmake build-essential libosmocore-dev liborc-0.4-dev \
                    libosmocoding0t64 libosmocodec0t64; do
            apt_install "$pkg" 2>/dev/null && GR_APT_INSTALLED+=("$pkg") || \
                warn "GNU Radio apt: $pkg not installed (not in repos or held back)."
        done
        if [ ${#GR_APT_INSTALLED[@]} -ge 4 ]; then
            ok "GNU Radio apt set installed: ${GR_APT_INSTALLED[*]}"
        else
            warn "GNU Radio core apt install partial — GSM decoder may be unavailable."
        fi

        # If the apt gr-gsm didn't install for any reason, try a SOURCE-BUILD
        # fallback. Critically: do a FULL clone (no --depth=1) so `git describe`
        # finds a tag and the install step's libgrgsm.so.<git-hash> name resolves.
        # Check both: the venv-side import AND the apt-installed binary package
        # (dpkg is Debian-only — dnf systems always fall through to source-build,
        # which is what we want since EPEL doesn't ship gr-gsm).
        if ! python3 -c "import grgsm" 2>/dev/null \
           && { [ "$PM" != "apt" ] || ! dpkg -s gr-gsm >/dev/null 2>&1; }; then
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
            # cmnalib (FetchContent'd by LTESniffer at configure time) does a
            # pkg_check_modules(... libudev) — without libudev-dev the whole
            # cmake step bails before it generates a Makefile (4-line log, no
            # progress). Install each prereq individually to make missing
            # ones obvious.
            for pkg in libuhd-dev uhd-host libfftw3-dev libmbedtls-dev libboost-program-options-dev \
                       libconfig++-dev libsctp-dev libpcsclite-dev libudev-dev; do
                apt_install "$pkg" 2>/dev/null || \
                    warn "LTESniffer prereq $pkg not installed — build may fail."
            done
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
        if [ "$PM" != "apt" ]; then
            warn "srsRAN auto-install uses the Ubuntu PPA (apt-only). On RHEL-family, build from source: git clone github.com/srsran/srsran_project && cmake build."
        else
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
        # kismet isn't in EPEL on RHEL — pkg_map translates it to "" so the
        # apt_install below is a no-op there.
        if [ "$PM" = "apt" ]; then
            echo "kismet-capture-common kismet-capture-common/install-users boolean true" \
                | maybe_sudo debconf-set-selections 2>/dev/null || true
        fi
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
if [ "$WITH_SDR_UDEV" = "true" ] && [ "$OS" = "Linux" ] && have_pkg_mgr; then
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
