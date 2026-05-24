#!/usr/bin/env bash
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

# ═══════════════════════════════════════════════════════════════════════════════
# bundle_vendor.sh — pre-fetch every vendor dependency so a downstream `install.sh`
# can run *fully offline*. Run this once on an internet-connected machine; the
# resulting `vendor/` directory ships with the project to the air-gapped target.
#
# What it populates (under <repo>/vendor/):
#   wheels/                    — every Python wheel from backend/requirements.txt
#                                (current host's interpreter + 'any' wheels)
#   npm/frontend/node_modules/ — exact frontend npm tree
#   npm/electron/node_modules/ — exact Electron npm tree
#   soapy/                     — public SoapySDR vendor-module sources (RTL-SDR /
#                                UHD / HackRF / Airspy / Pluto / SignalHound /
#                                Sidekiq) so the install can build them locally;
#                                the SignalHound + Sidekiq *SDKs* are vendor-only
#                                downloads and stay separate.
#
# After this runs, `install.sh` automatically uses the bundle (no flags needed) —
# it detects vendor/wheels and vendor/npm and installs from them instead of pip /
# npm hitting the network.
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
VENDOR="$HERE/vendor"
PY="${PYTHON:-python3}"

log()  { echo -e "\033[0;34m[bundle]\033[0m $*"; }
ok()   { echo -e "\033[0;32m[✓]\033[0m $*"; }
warn() { echo -e "\033[1;33m[!]\033[0m $*"; }

# ── 1. Python wheels ─────────────────────────────────────────────────────────
log "Pre-fetching Python wheels for backend/requirements.txt …"
mkdir -p "$VENDOR/wheels"
"$PY" -m pip download -r "$HERE/backend/requirements.txt" -d "$VENDOR/wheels" --quiet
ok "wheels: $(ls "$VENDOR/wheels" | wc -l) file(s) cached at vendor/wheels"

# ── 2. Frontend + Electron npm trees ─────────────────────────────────────────
log "Pre-fetching frontend npm tree …"
mkdir -p "$VENDOR/npm/frontend"
( cd "$HERE/frontend" && rm -rf node_modules && npm ci --silent --no-audit --no-fund )
rsync -a --delete "$HERE/frontend/node_modules/" "$VENDOR/npm/frontend/node_modules/"
ok "frontend: $(du -sh "$VENDOR/npm/frontend/node_modules" | cut -f1) at vendor/npm/frontend/node_modules"

log "Pre-fetching Electron npm tree …"
mkdir -p "$VENDOR/npm/electron"
( cd "$HERE/electron" && rm -rf node_modules && npm ci --silent --no-audit --no-fund )
rsync -a --delete "$HERE/electron/node_modules/" "$VENDOR/npm/electron/node_modules/"
ok "electron: $(du -sh "$VENDOR/npm/electron/node_modules" | cut -f1) at vendor/npm/electron/node_modules"

# ── 3. SoapySDR vendor-module sources (build them locally, no internet) ──────
# These are the public, open-source Soapy bindings for each radio's stack. The
# *device libraries* (libsignalhound, libsidekiq) are vendor-only and you must
# download them separately from the manufacturer — see vendor/soapy/README.txt.
log "Mirroring SoapySDR vendor-module sources …"
mkdir -p "$VENDOR/soapy"
clone_or_skip() {  # usage: clone_or_skip <url> <dest>
    local url="$1" dest="$2"
    if [ -d "$dest/.git" ]; then
        ( cd "$dest" && git fetch --depth=1 --quiet origin && git reset --hard --quiet "@{u}" 2>/dev/null || true )
    elif command -v git >/dev/null 2>&1; then
        git clone --depth=1 --quiet "$url" "$dest" || warn "skip: $url"
    else
        warn "git not present — skipping $url"
    fi
}
clone_or_skip https://github.com/pothosware/SoapyRTLSDR.git           "$VENDOR/soapy/SoapyRTLSDR"
clone_or_skip https://github.com/pothosware/SoapyUHD.git              "$VENDOR/soapy/SoapyUHD"
clone_or_skip https://github.com/pothosware/SoapyHackRF.git           "$VENDOR/soapy/SoapyHackRF"
clone_or_skip https://github.com/pothosware/SoapyAirspy.git           "$VENDOR/soapy/SoapyAirspy"
clone_or_skip https://github.com/pothosware/SoapyAirspyHF.git         "$VENDOR/soapy/SoapyAirspyHF"
clone_or_skip https://github.com/pothosware/SoapyPlutoSDR.git         "$VENDOR/soapy/SoapyPlutoSDR"
clone_or_skip https://github.com/pothosware/SoapyBladeRF.git          "$VENDOR/soapy/SoapyBladeRF"
clone_or_skip https://github.com/pothosware/SoapyLMS7.git             "$VENDOR/soapy/SoapyLMS7"
# Vendor-tied (sources public; SDK separate):
clone_or_skip https://github.com/signalhound/SoapySignalHound.git     "$VENDOR/soapy/SoapySignalHound" || true
clone_or_skip https://github.com/epiqsolutions/SoapySidekiq.git       "$VENDOR/soapy/SoapySidekiq"     || true
cat > "$VENDOR/soapy/README.txt" <<'EOF'
SoapySDR vendor-module sources mirrored here so the install can build them on
an air-gapped target. To finish setup:

  RTL-SDR / UHD / HackRF / Airspy / Pluto / BladeRF / LimeSDR
      `apt install soapysdr-module-{rtlsdr,uhd,hackrf,airspy,plutosdr,bladerf,lms7}`
      or build from source here. No vendor SDK required.

  SignalHound (BB60C / SM200B / SM435B / …)
      Download the **SignalHound SDK** from signalhound.com (free login).
      Build SoapySignalHound/ against it, then `make install`.

  Epiq Sidekiq / Matchstiq
      Download **libsidekiq** from epiq-solutions.com (license-gated).
      Build SoapySidekiq/ against it, then `make install`.

Once any of those modules is in SoapySDR's search path, Ares' native UAS
demod / DF will pull IQ from the radio with no further config.
EOF
ok "Soapy sources mirrored at vendor/soapy ($(ls "$VENDOR/soapy" | wc -l) module(s))"

cat <<EOF

────────────────────────────────────────────────────────────────────────────
  Vendor bundle ready at:  $VENDOR
  Total size:  $(du -sh "$VENDOR" | cut -f1)

  Ship the whole tree alongside the source (or commit it on a release branch),
  then \`install.sh\` on the air-gapped target uses it automatically.
────────────────────────────────────────────────────────────────────────────
EOF
