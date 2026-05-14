# Ares

Terrain-based RF propagation & geolocation platform with GPU acceleration. Rivals
and surpasses CloudRF, SPLAT!, RadioMobile, and Wireless InSite.

> Reference-grade physics: a faithful port of the **ITS Longley-Rice (ITM)** model
> (the SPLAT!/Radio Mobile/FCC algorithm, with full 7-climate variability);
> **phase-interferometry / MUSIC / Capon array direction-finding** (ULA/UCA/arbitrary
> geometry, ambiguity-resolved, with CRLB σ); a **maximum-likelihood DF fix** with a
> covariance-derived (geometry-correct) error ellipse, **GDOP**, and an **EKF emitter
> track**; **TDOA/FDOA multilateration**; a real **SGP4** (the `sgp4` package, or a
> vendored faithful near-earth SGP4); an **ITU-R P.533-style HF** circuit model;
> **per-pixel WorldCover clutter** in the path; **measured antenna-pattern import**
> (NSMA/Planet MSI, NEC-2); and **CoT over mutual-TLS** to a TAK Server. Module-by-
> module breakdown: [`docs/Ares.md`](docs/Ares.md). Run the validation harness:
> `cd backend && python -m tests.test_authoritative`.

> **This is the `Ares` branch** — adds: (1) **ARES-ATAK**, an
> open-source ATAK-CIV plugin matching the CloudRF SOOTHSAYER plugin and adding Ares
> DF/geolocation + propagation extras; (2) a **fully offline-capable hybrid server**
> (worldwide 30 m terrain / OSM base maps / building footprints / AO imagery packs, with
> online auto-fetch of the highest-fidelity data when connected); (3) a **CesiumJS 3D
> globe** view in the web/desktop UI. See **[`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md)**.

## Quick Start

### Linux (Pop OS, Kali) / macOS
```bash
cd ares
bash install.sh
# Then double-click "Ares" on your desktop
```

### Windows 10/11
```batch
cd ares
install.bat
# Then use the desktop shortcut or Start Menu entry
```

### Docker (any platform)
```bash
docker compose up
# Open http://localhost:3000
```

### Mobile (Android / iOS)
```bash
cd ares/mobile
npm install
npx expo start
# Scan QR code with Expo Go app, or build with EAS
```

---

## Features

### Propagation Models
| Model | Frequency Range | Description |
|-------|----------------|-------------|
| ITM / Longley-Rice | 20 MHz–20 GHz | Terrain-based (primary model) |
| Free Space (FSPL) | All | Theoretical LOS |
| Okumura-Hata Urban/Suburban/Rural | 150–1500 MHz | Empirical cellular |
| COST-231 Hata | 1.5–2 GHz | Extended Hata |
| Two-Ray Ground Reflection | All | Ground reflection model |
| ITU-R P.1546 | 30–3000 MHz | Point-to-area |
| ITU-R P.528 (Aeronautical) | 100 MHz–15.5 GHz | Air-to-ground |
| SUI (WiMAX) | 2–11 GHz | Stanford University Interim |
| Egli | 40–900 MHz | Rural empirical |
| Plane Earth | All | 4th-power law |

### Terrain Data (auto-downloaded)
- **SRTM 90m** (global, fast) — from CGIAR/USGS
- **SRTM 30m** (higher detail, slower)
- **Copernicus GLO-30** (Europe, 30m)
- **OpenTopoData API** (cloud fallback, always works)

### Atmospheric Effects
- Oxygen + water vapour absorption (ITU-R P.676)
- Rain attenuation (ITU-R P.838, up to 300 mm/hr)
- Fog/cloud attenuation (ITU-R P.840)
- Tropospheric ducting (refractivity gradient < -157 N/km)
- Ionospheric absorption (HF, D-layer)
- Sporadic-E (seasonal VHF enhancement)

### Space Weather (NOAA SWPC real-time)
- Solar flux index (F10.7)
- Kp geomagnetic index
- X-ray / Radio blackout class (R1–R5)
- Geomagnetic storm class (G1–G5)
- Polar cap absorption detection
- MUF / LUF for HF paths

### Antenna Patterns (27 presets)
Isotropic, Half-wave dipole, Quarter-wave monopole/whip, Collinear (2/4 el),
Yagi (3/5/9/15 el), Log-periodic, Sector (60°/90°/120°), Omni (2.15/5/9 dBi),
Parabolic dish, Horn, Patch, Helical, Loop, Phased array, Custom JSON/NEC

### GPU Acceleration
- **NVIDIA RTX 3070 Ti** (and all CUDA-capable GPUs): automatic detection
- Uses CuPy for batch matrix operations in coverage simulation
- Graceful CPU fallback on all other hardware

### Altitude Support
- Transmitter: 0–30,000+ ft ASL
- Receiver: 0–30,000 ft (airborne receivers, drones, aircraft)
- Uses ITU-R P.528 model for aeronautical paths
- Atmospheric parameters auto-corrected for altitude (ISA standard)

### Frequency Range
- **1 Hz to 300 GHz** (hardware permitting)
- HF: ionospheric effects, MUF/LUF, D-layer absorption
- VHF/UHF: standard terrain + atmospheric
- Microwave/mmWave: rain/fog/oxygen absorption dominant
- 77 presets for common frequencies

---

## Architecture

```
ares/
├── backend/           Python FastAPI + propagation engine
│   └── app/
│       ├── main.py    FastAPI app + WebSocket
│       ├── api/       REST routes
│       └── core/
│           ├── simulation.py      Coverage radial sweep engine
│           └── propagation/
│               ├── itm.py         Longley-Rice ITM (full implementation)
│               ├── models.py      FSPL, Hata, COST231, ITU-R, etc.
│               ├── terrain.py     SRTM download + elevation profiles
│               ├── atmosphere.py  Gas absorption, rain, ducting, HF
│               ├── antenna.py     27 antenna pattern models
│               └── space_weather.py  NOAA SWPC real-time
├── frontend/          React + Vite + Leaflet (web)
├── electron/          Desktop wrapper (Win/Mac/Linux)
├── mobile/            React Native + Expo (Android/iOS)
├── install.sh         Linux/macOS installer + desktop shortcut
└── install.bat        Windows installer + desktop/Start Menu shortcut
```

---

## API Reference

Once running, full interactive docs at: **http://localhost:8000/docs**

### Key endpoints
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/simulate/coverage` | Full coverage area simulation |
| POST | `/api/v1/simulate/p2p` | Point-to-point link budget |
| GET  | `/api/v1/terrain/profile` | Elevation profile between two points |
| GET  | `/api/v1/terrain/elevation` | Single point elevation |
| GET  | `/api/v1/space_weather` | Live NOAA SWPC data |
| GET  | `/api/v1/hf/muf` | HF MUF/LUF for a path |
| GET  | `/api/v1/antenna/catalogue` | All antenna presets |
| GET  | `/api/v1/propagation/models` | All propagation models |
| DELETE | `/api/v1/cache/purge` | Remove stale terrain cache |
| WS   | `/api/v1/ws/simulate` | Real-time coverage with progress |

---

## Cache Management
- Terrain data cached for **30 days**, then auto-deleted
- Building data cached for **7 days**
- Manual purge: Tools → Purge Terrain Cache (desktop) or `/cache/purge` API
- Cache location: `backend/data/`

---

## GPU Setup (NVIDIA)

The installer auto-detects your GPU. For manual setup:
```bash
# CUDA 12 (RTX 30xx/40xx series)
pip install cupy-cuda12x

# CUDA 11
pip install cupy-cuda11x

# Verify
python -c "import cupy; print(cupy.cuda.runtime.runtimeGetVersion())"
```
Then enable "GPU acceleration" in the UI or set `use_gpu: true` in API requests.
