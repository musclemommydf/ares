# Ares — Deployment ("Ares-in-a-box")

This is the field-deployment guide for **Ares** — a self-contained Ares server
that the ARES-ATAK plugin (and the web/desktop UI) talks to, on the same hardware
spectrum CloudRF's SOOTHSAYER targets. Three things make a deployment "field-ready":

1. **Offline data packs** staged on the box (Workstream A) — so it works with no
   internet, and *grows its own packs* when it does have internet.
2. **Auth on** (`ARES_AUTH=true`) — the plugin and any networked client need a
   bearer token; see [Auth](#auth).
3. A **stable address** the plugin can reach — `http://<box-ip>:8000` on the
   tactical LAN/MANET, or a cloud URL.

---

## 1. Hardware targets

| Target | Role | Notes |
|---|---|---|
| **NVIDIA Jetson Orin Nano / NX** | full server **with GPU** | CuPy gives GPU multisite/best-server/Monte-Carlo. The `install.sh` CUDA path picks the right CuPy wheel; or use the Docker image with the NVIDIA runtime (`deploy.resources.reservations.devices` in `docker-compose.yml`). |
| **Rugged x86 laptop** (Panasonic Toughbook, Dell Rugged, …) | full server, CPU (or eGPU) | Same `install.sh`. Pair with a 500 GB+ data SSD for the packs (see §3). |
| **Raspberry Pi 5 (8 GB)** | "links-only" server | CPU-only — fine for P2P link mode, RF-link / Co-Opt, DF (`/geolocate/fix`), HF/space-weather. Big airborne rasters / Monte-Carlo will be slow; keep `radius_km × 2 ≈ resolution_m` and < ~1 MP. |
| **Cloud VM** | shared server | Same as today's Ares; just turn auth on. |

The Ares server is a thin physics engine — the plugin and globe do the rendering —
so even the Pi-5 tier is a usable "vehicle box" for the low-bandwidth modes.

---

## 2. Install

### Bare metal / VM
```bash
git clone <ares> && cd ares
./install.sh                       # detects Python/Node/CUDA, builds the frontend, makes start-*.sh
ARES_AUTH=true ./start-backend.sh  # uvicorn on :8000 — see Auth below for the admin password
```

### Air-gapped — pre-staged "offline bundle"
On a connected machine, build the bundle once:
```bash
# point a fresh data dir at the box and download the AO packs you want…
cd ares/backend
ARES_DATA=./bundle python -m app.main &   # (or just use the running server's data/ dir)
# …via the web "ATAK / Server" console or the API:
#   POST /api/v1/packs/download {"layers":["terrain"],   "bbox":[-6,49,2,59]}        # SRTM30
#   POST /api/v1/packs/download {"layers":["osm"],        "bbox":[-6,49,2,59], "max_zoom":12}
#   POST /api/v1/packs/download {"layers":["imagery"],    "bbox":[-1.5,51,0.5,52],"max_zoom":15}
#   POST /api/v1/packs/download {"layers":["buildings"],  "bbox":[-0.2,51.4,0.05,51.6]}
#   POST /api/v1/packs/download {"layers":["clutter"],    "bbox":[-6,49,2,59]}            # ESA WorldCover 10 m
# the result lives under data/packs/{terrain,osm,imagery,buildings,clutter}/<id>/
```
Copy `backend/data/` (the `packs/` tree, plus `terrain/`, `users.json`, `.auth_secret`
if you want a fixed admin) onto the box, then:
```bash
./install.sh --offline-bundle /media/usb/ares-bundle
ARES_AUTH=true ARES_NETWORK_POLICY=offline_only ./start-backend.sh
```
`ARES_NETWORK_POLICY=offline_only` makes the box never attempt a remote fetch; cloud-only
services (NOAA space weather, weather, Overpass buildings) fall back to last-known / a covering
pack / documented defaults, each flagged `stale`/`source` in the response.

### Docker
```bash
ARES_AUTH=true ARES_PACKS_HOST_DIR=/media/usb/ares-packs ARES_NETWORK_POLICY=offline_only \
  docker compose up -d
```
`ARES_PACKS_HOST_DIR` (defaults to a named volume) bind-mounts a pre-staged `packs/` dir;
a connected box leaves it on the named volume and just accumulates packs over time.

---

## 3. Data packs & disk

| Layer | Source (auto-picked by what's installed / reachable) | Rough size |
|---|---|---|
| `terrain` | customer LiDAR → Copernicus GLO-30 → **SRTM 30 m** (`.hgt`) → SRTM 90 m → OpenTopoData (online) | SRTM 30 m global ≈ 150–250 GB; a region pack is a few GB |
| `osm` | XYZ raster z0–~14 (your tile server for anything large) | global z0–14 ≈ tens of GB; a region pack is MBs–GBs |
| `imagery` | customer/Maxar → NAIP → Sentinel-2 ~10 m → ESRI World Imagery → blank | AO tile-cache packs only — full-zoom worldwide offline is **out of scope** |
| `buildings` | OSM footprints via Overpass → a GeoJSON file (extruded on the 3D globe) | a city ≈ tens of MB |
| `clutter` | customer landcover → **ESA WorldCover 10 m** (3°×3° GeoTIFFs) → OSM landuse → none | ≈ 130 MB per 3° tile |

A **500 GB data SSD** (the SOOTHSAYER spec) comfortably holds 30 m terrain + OSM +
buildings + a regional 10 m imagery mosaic + WorldCover for the theatre. Region packs
are the default; "download full planet" is an opt-in unbounded-bbox job with a clear
size estimate and disk-headroom guardrails (it refuses if < pack-size + 2 GB free).

**Provider chain (online ⇄ offline).** With `ARES_NETWORK_POLICY=auto` (the default), a
missing terrain cell viewed on the globe is fetched from the open SRTM bucket *and written
into a `terrain-auto` pack* — so a connected box transparently grows its own offline pack.
The `/api/v1/terrain/heightmap/...` response carries an `X-Ares-Terrain-Source: pack|online|flat`
header. Run `POST /api/v1/packs/<id>/verify` (the ⛉ button in the web console) to check a
pack's integrity / version after copying it around.

---

## 4. Auth

```bash
ARES_AUTH=true ./start-backend.sh   # first run logs:  username=admin  password=<random>  (hashed in data/users.json)
# change it / add operators with the Python helper:
python -c "from app.core.auth import add_or_update_user; add_or_update_user('alice','s3cret','operator')"
```
- Tokens: `POST /api/v1/auth/login {username,password}` → `{token, expires}` (HMAC-SHA256, 12 h).
  The signing secret persists to `data/.auth_secret` (override with `ARES_AUTH_SECRET`).
- **LDAP / Active Directory** (matches SOOTHSAYER's multi-user story): `ARES_AUTH_BACKEND=ldap`
  (or `ldap+local`) + `ARES_LDAP_SERVER=ldaps://dc.corp...`, `ARES_LDAP_USER_DN="{username}@corp.example.com"`
  (AD UPN) or `"uid={username},ou=people,dc=corp,dc=com"`; optional `ARES_LDAP_ADMIN_GROUP=<group DN>`
  → members get `role=admin`. Needs `pip install ldap3`; if it's missing Ares logs a warning and stays local-only.
- `ARES_AUTH=false` (default) keeps the existing single-user/localhost behaviour — fine for a
  dev box, **not** for anything on a network.

---

## 5. Point ATAK at the box

In the ARES-ATAK plugin → **Settings**: Ares base URL `http://<box-ip>:8000` (or the cloud URL),
username/password → it stores a token and syncs radio templates. Self-signed certs are supported
with an explicit pinning UI. Then it behaves exactly like the SOOTHSAYER plugin against an Ares
server — single-site/multisite coverage, RF-link mode, Co-Opt, Best-Site, satellite/airborne —
plus DF mode, HF/space-weather, MANET, interference/EMCON, ray-trace, and live-weather attenuation.

> The plugin itself ships as a signed APK from **tak.gov** (TAK Product Center) and Google Play,
> one per supported ATAK release line — building/signing it needs the ATAK-CIV SDK from tak.gov and
> the Play/tak.gov publisher accounts (see `atak-plugin/README.md`). Everything *server-side* that
> the plugin needs is in this repo and works today.

---

## 6. SDR / DF radios (Workstream D)

Connect a direction-finding radio to Ares and live LoBs, fixes, CEP ellipses and
auto-coverage from the computed emitter location stream straight to the globe
and to ATAK.

### Supported devices
| Device | How it connects | Port |
|---|---|---|
| **KrakenSDR** (5-channel coherent RTL-SDR running `krakensdr_doa`) | HTTP polling of the `DOA_value` CSV row at `http://<box>:<port>/DOA_value` | 8080 |
| **Epiq Matchstiq X40** (wideband SDR; the X40 has no built-in DF — an Epiq-side process running on the radio pushes pre-computed bearings) | JSON-lines TCP stream from the Epiq process | 8401 |
| **Generic** (USRP B210, HackRF, GNU Radio flow, custom rig, …) | JSON-lines TCP — one object per LoB | 8400 |

### Register a device
From the web/desktop **📡 SDR / DF** header button or via REST:
```bash
curl -X POST http://ares.lan:8000/api/v1/sdr/devices \
  -H 'Content-Type: application/json' \
  -d '{
    "name":"kraken-1", "type":"krakensdr", "host":"192.168.10.42", "port":8080,
    "lat":51.5074, "lon":-0.1278, "altitude_m":12, "observer_height_m":3,
    "frequency_hz":433920000, "enabled":true, "auto_coverage":true
  }'
```
- `auto_coverage:true` reruns `/simulate/coverage` from every fresh fix that this device contributes to (cooled-down per frequency bin so back-to-back LoBs don't queue).
- `host` may also be `tcp://1.2.3.4:8401` for the generic / X40 paths.
- The device's `lat`/`lon` is the **antenna's** position; the KrakenSDR adapter prefers the Kraken GPS when it's reported non-zero, else falls back to this.

### Generic / X40 wire format
Newline-delimited JSON; only `azimuth_deg` and `frequency_hz` are required:
```jsonl
{"azimuth_deg":117.4,"frequency_hz":4.3392e8,"rssi_dbm":-67.3,"confidence_pct":75,"lat":51.51,"lon":-0.13,"t":1715300000.0,"target_device_id":"DMR-0xABCD"}
{"azimuth_deg":42.1, "frequency_hz":4.3392e8,"rssi_dbm":-71.0,"confidence_pct":60}
```
Anything you don't send uses the device's configured default.

### CoT to ATAK / TAK Server
Tell Ares where to push:
```bash
ARES_COT_TARGETS=udp://239.2.3.1:6969,tcp://taksrv.lan:8087 ./start-backend.sh
# or at runtime:
curl -X PUT http://ares.lan:8000/api/v1/sdr/cot/targets \
  -H 'Content-Type: application/json' \
  -d '{"targets":["udp://239.2.3.1:6969","tcp://taksrv.lan:8087"]}'
```
- `udp://239.2.3.1:6969` is the conventional **ATAK multicast group** — works for a same-LAN tactical setup, no TAK Server needed.
- `tcp://<host>:8087` is a plain (non-TLS) TAK Server input.
- CoT mapping: each LoB → `u-d-r` drawn-route (device → bearing endpoint, length from RSSI), callsign `LoB <freq>MHz <bearing>°`. Each fix → `a-u-G-U-C-I` (intel/unknown/ground) point with `ce=<CEP_m>` so ATAK draws the uncertainty circle natively, callsign `Ares Emitter <freq>MHz`.

### Verify end-to-end without a radio
You can drive the whole pipeline with a single POST:
```bash
# register a fake device
ID=$(curl -s -X POST http://ares.lan:8000/api/v1/sdr/devices -H 'Content-Type: application/json' \
     -d '{"name":"sim","type":"generic","host":"127.0.0.1","port":0,"lat":51.5,"lon":-0.1,"frequency_hz":4.3392e8,"enabled":false}' | jq -r .id)
# push three LoBs from different observer locations on the same freq → a "fix"
for AZ in 80 120 165; do
  curl -s -X POST http://ares.lan:8000/api/v1/sdr/lob -H 'Content-Type: application/json' \
       -d "{\"device_id\":\"$ID\",\"lat\":51.5,\"lon\":-0.1,\"azimuth_deg\":$AZ,\"frequency_hz\":4.3392e8,\"rssi_dbm\":-70}"
done
# watch WS /api/v1/sdr/stream — you'll see lob × 3, fix × 1, and (if auto_coverage was on) coverage × 1
```

## 7. Health & ops

- `GET /api/v1/server/info` — version, GPU, installed packs per layer, online/offline, disk free.
- `GET /api/v1/net/status` — online probe + last-known cloud data + operator overrides.
- `PUT /api/v1/net/override/space_weather` / `…/weather:<lat>,<lon>` — pin known conditions for an exercise.
- `GET /api/v1/sdr/state` — devices, recent LoBs, recent fixes, configured CoT targets — one shot.
- Web/desktop: the header's **🖥 ATAK / Server** console is the offline-ops panel (server identity,
  packs + region-download form + job poller + verify/delete, radio templates); the **📡 SDR / DF**
  console is the live DF picture (devices, status, per-device auto-coverage toggle, CoT targets,
  latest fix readout). Both subscribe to live state so a downloading pack or a streaming SDR
  reflects in real time without a refresh.
