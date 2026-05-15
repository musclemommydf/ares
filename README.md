# Ares

**Self-hosted RF propagation, geolocation, and passive-observation platform.** Coverage on
real terrain. Direction-finding from a single SDR up to a coherent array.
Passive cellular / WiFi / BLE observation. A 3-D globe. An ATAK plugin. All
offline-capable, all open source.

```bash
# Debian / Ubuntu / Pop!_OS / Kali
sudo apt-get -y install git && git clone https://github.com/musclemommydf/ares.git && cd ares && ./install.sh

# Rocky / Alma / RHEL / CentOS Stream 8 or 9
sudo dnf -y install git && git clone https://github.com/musclemommydf/ares.git && cd ares && ./install.sh
```

Then double-click **Ares** on your desktop, or `./start-web.sh`.

---

## What it does

- **Terrain RF propagation** — ITS Longley-Rice (the SPLAT! / Radio Mobile / FCC algorithm) plus a dozen empirical & ITU models, real diffraction (Deygout / Bullington / Epstein-Peterson / Giovanelli), atmospheric & space-weather corrections, 20+ analytic antenna patterns and measured-pattern import (NSMA / Planet MSI / NEC-2). Coverage as a heatmap or per-pixel raster; point-to-point links with terrain profile, Fresnel zone and LOS obstruction; multisite / best-server / best-site / interference / ray-trace / route / MANET coverage.
- **Direction finding** — maximum-likelihood bearing-only triangulation with a covariance-derived (geometry-correct) error ellipse, GDOP, and an EKF track. TDOA / FDOA hyperbolic multilateration. Phase-interferometry / MUSIC / Capon / Bartlett array DoA with the CRLB. Bearings are terrain-capped — DF that respects mountains.
- **Single-channel DF (the Algorithms tab)** — when you only have one SDR plus motion: RSS log-distance ML, RSS-gradient bearing, Doppler closest-point-of-approach, FDOA multi-pose grid, kinematic synthetic-aperture DoA, phase-interferometry along track, ML grid fusion, EKF kinematic tracker. All in-process numpy / scipy.
- **Per-identifier target tracking (the Targets tab)** — keyed by IMSI / TMSI / IMEI / RNTI / MAC / BLE / ICAO / DMR-RID / UAS serial / callsign. Peak-RSSI sampler, top-K, range estimate that auto-upgrades from Friis single-shot → multi-pose RSS-ML → AoA-fused ML grid as observations accumulate.
- **PTT modulation classifier + auto-decoder** — captures a short IQ window, identifies DMR / dPMR / P25 P1/P2 / TETRA / NXDN / D-STAR / YSF / M17 / POCSAG / FLEX / GSM / UMTS / LTE / 5G NR / WiFi, and routes the baseband to the right open-source decoder (dsd-fme, op25, sdrtrunk, tetra-rx, multimon-ng, m17-demod, gr-gsm, LTESniffer, 5GSniffer).
- **Passive cellular & WiFi/BLE monitors** — in-process GNU Radio + gr-gsm flowgraph for 2G BCCH/CCCH (cell-IDs, paging TMSIs); LTESniffer for LTE PDCCH/SIB1 (RNTIs, cell info); spritelab/5GSniffer for 5G NR SSB/MIB/SIB1; hcxdumptool / aircrack-ng for WiFi BSSIDs and STA MACs; BlueZ btmon for BLE advertising frames. Strictly passive: no decryption, no IMSI-catcher behaviour.
- **UAS / FPV video decode** — recovers viewable raster from NTSC / PAL / SECAM / VSB FM video without external software. Multi-detector search, H-sync PLL, V-sync via equalising pulses, deinterlace, chroma decode, frame averaging. 11 client-side colormaps (amber CRT, green phosphor, ironbow thermal, viridis, night-vision, …), brightness / contrast / gamma, snapshot to PNG, record to WebM. Spectrum max-hold for hunting hopping FPV downlinks.
- **3-D globe + offline data** — CesiumJS globe alongside the Leaflet 2-D map: coverage, LOS, Fresnel zones, antenna lobes on real heightmap terrain. KMZ import/export, GeoPackage, GeoTIFF / DTED, range rings, fans, NATO symbology. Offline packs for terrain / OSM / imagery / buildings / clutter — provider chain grows the pack online when reachable.
- **ATAK / TAK integration** — Cursor-on-Target out (UDP / multicast / TCP / mutual-TLS): LoBs as drawn routes, fixes as intel ground points with a CEP circle, chat as GeoChat. CoT receive listener brings ATAK GeoChat back into the same conversation. Open ATAK-CIV plugin in `atak-plugin/`.
- **Distributed sensing & chat** — multiple SDRs on one box cross-fuse automatically; over a MANET, peer Ares nodes share LoBs / fixes / chat — HMAC-signed, dedup'd, hop-bounded — so the fused picture lives on every node. Group chat with rooms, bridged to ATAK GeoChat.
- **HF & satellites** — ITU-R-P.533-style HF circuit model with multi-hop F2 geometry, MUF/FOT/LUF, D-region absorption, ITU-R P.372 noise floor, NOAA SWPC space-weather inputs. Real SGP4 satellite visibility (the `sgp4` package, or a vendored faithful near-earth SGP4).

---

## Quick start

```bash
# Debian / Ubuntu / Pop!_OS / Kali
sudo apt-get -y install git && git clone https://github.com/musclemommydf/ares.git && cd ares && ./install.sh

# Rocky / Alma / RHEL / CentOS Stream 8 or 9
sudo dnf -y install git && git clone https://github.com/musclemommydf/ares.git && cd ares && ./install.sh

# macOS (Homebrew already installed)
git clone https://github.com/musclemommydf/ares.git && cd ares && ./install.sh
```

The installer is non-interactive and idempotent. On apt-based distros it goes straight to work; on Rocky/RHEL it first enables EPEL + CRB and pulls `python3.11` + `nodejs:20` from AppStream, then takes the same path. It pulls SoapySDR + every open SDR driver module (RTL-SDR / USRP / HackRF / Airspy / Pluto / ANTSDR / BladeRF / LimeSDR), writes udev rules for KrakenSDR, ANTSDR e200, and SignalHound (BB60C/D / SM200 / SA44/124 / TG124A), blacklists the kernel DVB driver so RTL-SDR isn't hijacked, adds your user to `plugdev / dialout / audio`, source-builds the audio decoders (dsd-fme, m17-cxx-demod, acarsdec), and brings in GNU Radio + gr-gsm + LTESniffer + 5GSniffer for the cellular passive monitors.

SignalHound's vendor SDK is closed-source so we can't ship it with the installer, but the install is zero-touch once you have the zip on the machine: just download `signal_hound_sdk_*.zip` from [signalhound.com](https://signalhound.com/support/product-downloads-prdct-signal-hound/) into `~/Downloads/` and re-run `./install.sh`. The installer auto-discovers the zip, auto-extracts it, picks the right per-arch + per-distro libs (Red Hat 8 build on RHEL-family, Ubuntu 18.04 build on apt-family, aarch64 on Pis), and source-builds the SoapySignalHound bridge. To install or refresh later without a full re-run: `sudo ./scripts/install-signalhound.sh`.

```bash
./start-web.sh        # backend (:8000) + bundled UI (:3000)
./start-desktop.sh    # Electron desktop app
./start-backend.sh    # API only

docker compose up -d  # backend + frontend in containers
```

Air-gapped install: `./install.sh --offline-bundle <dir>` stages a pre-built terrain/imagery/buildings pack and skips the online download; then run with `ARES_NETWORK_POLICY=offline_only`.

Common opt-outs: `--no-soapysdr`, `--no-audio-decoders`, `--no-gnuradio`, `--no-lte-sniffer`, `--no-5g-sniffer`, `--no-wifi-bt`, `--no-sdr-udev`. Heavyweight opt-ins: `--with-op25` (pulls all of GNU Radio for P25, 30-60 min build), `--with-sdrtrunk`, `--with-tetra`, `--with-srsran`. See `./install.sh --help`.

---

## Hardware

| Class | What works |
|---|---|
| SDRs (open) | RTL-SDR · KrakenSDR (5×RTL-SDR coherent) · HackRF · Airspy / Airspy HF+ · BladeRF · LimeSDR · ANTSDR e200 · PlutoSDR · USRP (B/N/X via UHD) · MiriSDR |
| SDRs (vendor) | SignalHound (vendor SoapySDR module) · Epiq Sidekiq / Matchstiq X40 (vendor SoapySidekiq module) |
| GPS | gpsd (any USB GPS) · raw NMEA serial · SDR GPSDO · browser geolocation · manual |
| Hosts | NVIDIA Jetson Orin · rugged x86 laptop (CPU or eGPU) · Raspberry Pi 5 (links-only) · cloud VM |

GPU acceleration (CuPy on CUDA 12+) is auto-detected; the multisite Monte-Carlo and per-pixel raster paths use it when present, with a clean CPU fallback otherwise.

---

## Layout

```
ares/
├── backend/                   FastAPI + the physics + the DSP + the DF solver
│   └── app/
│       ├── api/               ~12 routers (sdr, df, geolocate, algorithms,
│       │                      targets, cellular, uas, atak, chat, …)
│       └── core/
│           ├── propagation/   ITM, models, terrain, atmosphere, antennas,
│           │                  space weather, ray-trace
│           ├── df/            single-channel, MUSIC/Capon/ESPRIT, GM-PHD,
│           │                  multi-baseline interferometry, fusion
│           ├── sdr/           manager, drivers, native demod, classifier,
│           │                  cellular/{gsm,lte,nr,umts}, wifi_bt
│           ├── targets/       per-identifier observation store + range/fix
│           ├── decoders/      Mode-S / ADS-B
│           ├── passive_radar/ cross-ambiguity surface
│           └── geolocation.py ML fix + covariance ellipse + GDOP + EKF
├── frontend/                  React + Vite + Leaflet + CesiumJS
├── electron/                  Desktop wrapper (Mac / Win / Linux)
├── atak-plugin/               ATAK-CIV plugin (Kotlin)
├── docs/                      Module-by-module reference + flyer + tutorial
└── install.sh                 The one installer
```

Interactive API docs at `http://localhost:8000/docs` once the backend is up.

---

## Docs

| Doc | What's in it |
|---|---|
| [`docs/Ares.md`](docs/Ares.md) | Module-by-module reference — what each piece computes + where it stands (rigorous / approximate / pending hardware) |
| [`docs/Ares_Flyer.pdf`](docs/Ares_Flyer.pdf) | Four-page capability overview |
| [`docs/Ares_Tutorial.pdf`](docs/Ares_Tutorial.pdf) | Seventeen-slide hands-on walkthrough |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Deployment targets · air-gapped · CoT-over-TLS · GPS sources · smoke test |
| [`atak-plugin/README.md`](atak-plugin/README.md) | Building / signing the ATAK plugin (needs the tak.gov SDK) |

---

## Verifying it works

```bash
cd backend
python -m tests.test_validation        # 115 checks: ITM, ML DF, TDOA, SGP4, HF, …
python -m tests.test_targets_tracker      #   7 checks: per-target peak/range/fix
python -m tests.test_cellular_classifier  #   6 checks: GSM/WCDMA/OFDM detection
```

The frontend has its own Node test suite:
```bash
cd frontend && node --test tests/
```

CI (`.github/workflows/ci.yml`) runs both on every push.

---

## License

Open source. The propagation engine (ITS Longley-Rice port + the empirical models) is a clean-room implementation. The patent-encumbered audio vocoders (AMBE / ACELP / IMBE) are **not** bundled — the installer source-builds the open dsd-fme / m17-cxx-demod / acarsdec on demand. GNU Radio + gr-gsm are GPL-3 and isolated to `backend/app/core/sdr/cellular/`; the rest of Ares stays MIT/Apache-clean.
