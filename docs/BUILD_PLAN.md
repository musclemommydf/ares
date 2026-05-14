# Ares — Build Plan

> **Note.** This document describes the original v1.x build plan. The current
> codebase replaces the *indicative* propagation/geolocation implementations with
> reference-grade ones — ITS Longley-Rice ITM, ML DF + covariance ellipse + GDOP +
> EKF, TDOA/FDOA, real SGP4, ITU-R P.533-style HF, per-pixel WorldCover clutter,
> measured-pattern import, CoT-over-TLS. Module-by-module breakdown:
> [`docs/Ares.md`](Ares.md).

> **What this is.** `Ares` is an RF-propagation/geolocation platform whose purpose is to ship four things on top of the current product:
>
> 1. **ARES-ATAK** — an open-source ATAK-CIV plugin that matches the CloudRF *SOOTHSAYER* ATAK plugin feature-for-feature against an **Ares** server, and adds Ares-exclusive capabilities (terrain-aware DF/geolocation, MANET, interference/EMCON, HF/space-weather, ray-trace).
> 2. **Offline-capable hybrid server** — Ares runs fully air-gapped from pre-staged data packs (worldwide 30 m terrain, OSM base maps + building footprints, AO imagery packs), **and** when internet is available behaves exactly as today: auto-fetching the highest-fidelity terrain / imagery / clutter it can and caching it for the next offline session.
> 3. **CesiumJS 3D globe** — a native globe view in the Ares web/desktop UI (Leaflet stays as the lightweight 2D mode), with coverage, line-of-sight, Fresnel zones and antenna patterns rendered on real 3D terrain.
> 4. **Live SDR / DF integration** — plug a KrakenSDR, an Epiq Matchstiq X40 (with an external DF pipeline), or any generic JSON-lines DF stream into Ares; bearings, fixes, CEP ellipses and *coverage simulations from the computed emitter location* stream live to the globe over WebSocket and to ATAK as CoT.
>
> The four are largely independent and run as **parallel workstreams**, but Workstream A (offline core) is a *prerequisite for the field story* of Workstream C (a Jetson/laptop "Ares-in-a-box" with the plugin pointed at it), so it leads slightly.

---

## 0. Locked technology decisions

| Area | Decision | Notes |
|---|---|---|
| ATAK plugin language / build | **Kotlin + Gradle**, ATAK-CIV SDK 5.x | Start on the current SDK (≈5.5, Jetpack Compose UI); maintain a 5.3/5.4 branch with XML UI. SDK from **tak.gov** (free account). |
| Plugin ↔ Ares transport | **HTTPS REST + WebSocket** (Retrofit / OkHttp / kotlinx-serialization) | `WS /api/v1/ws/simulate` for progress. Self-signed-cert support for field servers, with explicit pinning UI. |
| Plugin map rendering | **GeoJSON-first** (render Ares coverage contours as native ATAK vector layers); **KMZ** as the export/interop format | Optional server-side KMZ packer for SOOTHSAYER-style image-overlay parity. |
| Web/desktop globe | **CesiumJS** (Apache-2.0), added alongside the existing Leaflet 2D map | Leaflet remains the default lightweight 2D mode; Cesium is the "3D" toggle. |
| Cesium terrain source | **Local quantized-mesh tiles** generated from Ares DEMs (`cesium-terrain-builder` / `ctb-quantized-mesh`) | No Cesium Ion dependency — fully offline. Same DEM packs as Workstream A. |
| Optional online globe layer | Google Photorealistic 3D Tiles via Cesium `Cesium3DTileset`, **opt-in, requires user's own key, never a dependency** | For urban eye-candy + building geometry for ray-trace. |
| Mobile (Expo/React Native) | Stays on the flat `react-native-maps` view for now | Optional later: embed the Cesium web build in a WebView. Not in scope for v1. |
| Offline data store | New `data/packs/` layout (terrain / imagery / osm / buildings / clutter), each pack versioned with a manifest | See §A.2. |
| Server auth | Bearer-token auth + login endpoint (currently the API is unauthenticated) | Required for any networked/field deployment; needed by the plugin. See §A.1. |
| Repo hygiene | The `atak-plugin/` subdirectory and any Gradle paths must be **space-free** | Gradle (esp. on Windows) dislikes spaces in paths. Keep build paths clean regardless of the top folder name. |

---

## Workstream A — Offline-capable hybrid server core

**Goal:** Ares works with **no internet** from pre-staged packs, and with internet it behaves as it does today — auto-pull highest-fidelity data and cache it. One code path, two modes, controlled by data availability + a `network_policy` setting (`auto` | `online-only` | `offline-only`).

### A.1 Auth & deployment plumbing (also a plugin prerequisite)
- `POST /api/v1/auth/login` → bearer token (username/password; pluggable backend, default local users file; optional LDAP/AD later to match SOOTHSAYER's multi-user story).
- Token middleware on all `/api/v1/*` routes; `auth: disabled` escape hatch for single-user localhost dev.
- `GET /api/v1/server/info` → version, GPU present, which data packs are installed, online/offline status, disk free.
- "Ares-in-a-box" deployment image: extend `docker-compose.yml` / `install.sh` with a `--offline-bundle <dir>` option that mounts a packs directory; document Jetson Orin Nano / rugged-laptop / Pi-5 (links-only) targets — mirrors the SOOTHSAYER hardware spectrum.

### A.2 Data-pack architecture
```
backend/data/packs/
  terrain/    <region>/   quantized-mesh tiles + .hgt/.tif source + manifest.json   (Cesium-ready)
  osm/        <region>/   raster XYZ tiles (z0–14) and/or vector .mbtiles + manifest
  buildings/  <region>/   OSM building footprints (GeoPackage / FlatGeobuf) + manifest
  clutter/    <region>/   ESA WorldCover / customer landcover rasters + manifest
  imagery/    <region>/   MBTiles tile cache (AO packs) + manifest
```
- Each `manifest.json`: pack id, bbox/geometry, source, resolution/zoom range, build date, byte size, checksum.
- `GET /api/v1/packs` / `POST /api/v1/packs/download {bbox, layers, fidelity}` / `DELETE /api/v1/packs/{id}` — manage packs from the UI/CLI; "download full planet" is just an unbounded bbox job with progress over WS.
- **Fidelity tiers** (per layer, picked automatically by what's installed / reachable):
  - Terrain: customer LiDAR (if supplied) → Copernicus GLO-30 → SRTM 30 m → SRTM 90 m → OpenTopoData API (online only).
  - Clutter/landcover: customer data → ESA WorldCover 10 m → OSM landuse → none.
  - Imagery (web globe only): customer/Maxar → NAIP (US) → Sentinel-2 cloudless ~10 m → OSM raster → blank.
- **Sizes (planning numbers):** SRTM 90 m global ≈ 15–20 GB · SRTM/Copernicus 30 m global ≈ 150–250 GB · OSM raster z0–14 global ≈ tens of GB · OSM vector planet ≈ ~60–80 GB · Sentinel-2 10 m global mosaic ≈ hundreds of GB (optional). A "500 GB data disk" (the SOOTHSAYER spec) comfortably holds 30 m terrain + OSM + buildings + a regional 10 m imagery mosaic.

### A.3 Hybrid behavior (online ⇄ offline)
- Terrain/clutter/imagery loaders refactored to a **provider chain**: try local pack → if missing tile & online & policy allows → fetch from the best remote source → write into the pack → serve. So a connected box transparently *grows its own offline pack* as it's used.
- **Graceful degradation of cloud-only services** when offline:
  - NOAA SWPC space weather (`/space_weather`, `/hf/muf`): serve last-cached values with an explicit staleness flag; allow manual override entry; HF/atmospheric models run with documented default indices when nothing is available.
  - Weather APIs (`/weather/current`): same — last-known / manual entry; rain/fog attenuation defaults to clear-air if unknown.
  - Overpass/OSM building queries: served from the `buildings` pack when offline; live Overpass only when online.
- A small `network_status` field on every response that used (or wanted) a remote source, so the UI/plugin can show "offline — using cached terrain from 2026-04-30," etc.

### A.4 "What it can't be"
"Full-zoom imagery worldwide offline" is physically (petabytes) and legally (Bing/Google/Esri TOS) impossible — explicitly **out of scope**. The supported substitute is: global ~10 m mosaic (optional) + an **AO tile-cache tool** (`POST /api/v1/packs/imagery {bbox, max_zoom, source}` → MBTiles), which is exactly how ATAK itself handles offline imagery.

---

## Workstream B — CesiumJS 3D globe (web/desktop UI)

**Goal:** a real globe view in the React/Vite frontend (and therefore Electron), driven by the same Ares data, with RF-specific 3D rendering. Leaflet stays as the default 2D mode; a toolbar toggle switches to Cesium.

### B.1 Integration
- Add `cesium` (npm) + `vite-plugin-cesium`; new `<GlobeView>` React component sibling to the existing Leaflet `<MapView>`; shared app state (selected TX, layers, AO polygon, units) so 2D ↔ 3D is seamless.
- Camera/state sync: switching modes preserves center, heading, and a sensible pitch.

### B.2 Terrain & imagery
- **Terrain:** `CesiumTerrainProvider` pointed at the **local quantized-mesh tileset** produced in Workstream A (a small `ctb-quantized-mesh` step in the terrain-pack build pipeline). Online fallback: world ellipsoid (flat) or, if the user opts in, an external quantized-mesh service.
- **Imagery layers:** stacked `ImageryLayer`s — OSM raster from the local pack (default), Sentinel-2 / NAIP / customer where installed, optional online Bing/Esri when connected, optional **Google Photorealistic 3D Tiles** (opt-in, user key).

### B.3 RF rendering on the globe (the payoff)
- **Coverage:** Ares coverage GeoJSON contour bands → terrain-clamped `GeoJsonDataSource` polygons (graduated palette matching the 2D view); raster heatmap → `SingleTileImageryProvider` / `GridImageryProvider` / `GroundPrimitive`. `WS /ws/simulate` progress drives a live build-up.
- **Line-of-sight / terrain profile:** P2P link → 3D polyline draped on terrain + an obstruction marker where the path is blocked; `/terrain/profile` rendered as a vertical section panel synced to the globe.
- **Fresnel zones:** first-Fresnel ellipsoid rendered as a translucent 3D solid along the link — finally shows clearance over ridgelines.
- **Antenna patterns:** the existing polar patterns rendered as **3D radiation lobes** (mesh primitives) at the TX, oriented by azimuth/tilt — instantly communicates a directional/sector site.
- **Airborne & satellite:** TX/RX at altitude rendered at true ASL height (not clamped); `/simulate/satellite_visibility` footprints and look-angles in correct 3D geometry.
- **Markers / DF:** suspected-emitter markers + CAP/CEP ellipses from the geolocation tools (see Workstream C, shared math) shown on the globe; LoB bearing wedges as 3D fans.
- **KMZ:** import via `KmlDataSource`; export the globe scene/overlays to KMZ for ATAK/WinTAK interop.

### B.4 Performance
- LOD-tuned tile cache size, frustum culling defaults, optional `requestRenderMode` (render-on-change) for low-power devices, "lite globe" toggle (ellipsoid + 2D imagery, no terrain mesh) as a fallback. Bundle is ~30 MB JS — code-split so it loads only when the user enters 3D mode.

---

## Workstream C — ARES-ATAK plugin (SOOTHSAYER parity + Ares extras)

**Goal:** an open-source ATAK-CIV plugin that an existing SOOTHSAYER-plugin operator can pick up with zero retraining, talking to an Ares server, plus a DF/geolocation mode and propagation extras SOOTHSAYER has no answer to. Architecture: a **thin REST client + ATAK map renderer** — all physics stays on the Ares server.

### C.1 SOOTHSAYER feature parity → Ares endpoint mapping

| SOOTHSAYER plugin feature | ARES-ATAK implementation | Backing Ares endpoint(s) |
|---|---|---|
| Login / server config | Settings: Ares base URL (`http://jetson.lan:8000` or cloud), creds → token; syncs templates | `POST /api/v1/auth/login` *(new, §A.1)* |
| Radio templates (JSON, multi-azimuth, custom PNG icon) | `atak/ARES/templates/*.json` (sideload) **and** pull from server | `GET/PUT/DELETE /api/v1/atak/templates` *(new)*; seed from `/devices/presets`, `/antenna/catalogue` |
| Place transmitter | Map-tap → choose template → `MapItem` w/ Ares metadata | — |
| Single-site coverage | `POST /simulate/coverage` → GeoJSON contours as a layer (+ KMZ option) | `/simulate/coverage`, `WS /ws/simulate` |
| Multisite (GPU) coverage | All radios fused into one `ARES-MULTISITE` layer | `/simulate/multipoint`, `/simulate/super_layer`, `/simulate/best_server` |
| RF link mode (low-bandwidth) | Toggle → draw P2P link lines colored by margin | `/simulate/p2p` (batched), `/simulate/route` |
| Polygon tool (clip calc to a drawn area) | Reuse ATAK polygon → pass as bbox/clip | bbox params on coverage; `/simulate/best_site_polygon` |
| Best Site Analysis (Monte-Carlo, plasma heatmap) | "Best Site" over the polygon → ranked candidate heatmap | `/simulate/best_site`, `/simulate/best_site_polygon` |
| Satellite / airborne | "Air/Sat coverage" with resolution guardrails | `/simulate/satellite_visibility` |
| **Co-Opt (live coverage on a moving callsign)** | "Adopt callsign" → assign template → re-run coverage on CoT GPS update, throttled by **interval** or **distance**; replace the layer in place | `/simulate/coverage` on a timer; `WS /ws/simulate` |
| In-place TX edit (Height/Power/Freq/Azimuth/BW/Noise) | Radial-menu "Edit RF" sheet → patch request → instant recalc; template file untouched | same endpoints w/ edited params |
| KMZ export / send to contacts | Write to `atak/ARES/KMZ/`; hand to ATAK share (contacts, TAK server, Bluetooth, Drive, WinTAK, TAK-X) | optional `POST /api/v1/atak/export/kmz` *(new)* |
| Settings (layer type, coverage visibility, link display) | Same toggles, persisted | — |

### C.2 Ares-exclusive additions
- **DF / Geolocation mode** — radial-menu "Add LoB from here" on self/sensor markers; operator enters azimuth, RSSI, frequency, antenna pattern, observer height, confidence, device ID (DMR/IMEI/IMSI/MAC/callsign), timestamp. LoBs drawn as bearing wedges; same-frequency/-device LoBs auto-group → **Cut (2) / Fix (3+)**; intersections, centroid, **CAP/CEP ellipse** computed via a shared server endpoint `POST /api/v1/geolocate/fix` *(new — extract the existing `frontend/.../LoBUtils.js` math to Python so web, mobile, and ATAK share one solver)*. Each bearing is **terrain-capped** via `POST /lob/range_estimate` (signal-vs-terrain range for the observed RSSI). Output: a CoT "suspected emitter" marker (frequency, device id, confidence, contributing LoBs, CEP) auto-shared to the team; optional follow-on "model this emitter's coverage" → `/simulate/coverage` from the fix.
- **HF / space-weather panel** — pick two callsigns → `/hf/muf` + `/space_weather` → MUF/LUF, Kp/SFI, R/G storm class, "HF link open/marginal/closed."
- **MANET / mesh** — `/simulate/manet` over selected callsigns → connectivity graph + fused coverage.
- **Interference / EMCON** — `/simulate/interference` → "where am I jammed / where do I leak."
- **Ray-trace** — `/simulate/ray_trace` for a high-fidelity urban link.
- **Live weather** — auto-pull `/weather/current` for the AO so rain/fog attenuation is current (degrades gracefully offline per §A.3).
- **Offline-first selling point** — ships with the "Ares-in-a-box" deployment story (§A.1): Jetson/laptop in the vehicle, plugin → `http://<box-ip>:8000`, no internet.

### C.3 Plugin internals
- Gradle module `atak-plugin/` (space-free path) against ATAK-CIV SDK 5.x; Kotlin; Jetpack Compose UI on the current SDK, XML on the legacy-SDK branch.
- Retrofit + OkHttp + kotlinx-serialization; request models generated from the Ares schemas; OkHttp WebSocket for `/ws/simulate`; self-signed-cert handling + pinning UI.
- Map rendering: client renders coverage GeoJSON contour bands as ATAK vector layers (restyleable, no server change); KMZ is the export/interop format. Optional server KMZ packer if pixel-for-pixel SOOTHSAYER-style image overlays are wanted.
- State: placed RF sites & adopted callsigns persisted in plugin prefs + as CoT detail extensions (survive restart, optionally sync).
- CoT: subscribe to position reports (drives Co-Opt + DF); emit suspected-emitter (and optional coverage-summary) CoT events.
- Distribution: debug-signed APK for sideload/dev; submit to **tak.gov** + Google Play for signed release (as SOOTHSAYER does — both TAK Product Center and Play signed). License: match the SOOTHSAYER plugin's open-source license so units can audit + air-gap it. Maintain ~one APK per supported ATAK release line.

---

## Workstream D — Live SDR / DF integration (KrakenSDR, Matchstiq X40, generic)

**Goal:** plug physical direction-finding radios into Ares so that LoBs, fixes, CEP ellipses and *coverage simulations from the computed emitter location* all stream live — to the web/desktop globe (WebSocket) and to ATAK (CoT). Operators connect a device, point it at a frequency, and the rest of the system catches up automatically.

### D.1 Device adapters
- **KrakenSDR** — `krakensdr_doa` exposes a CSV "DOA data out" row at `http://<box>:8080/DOA_value`. The adapter polls every 0.5 s, parses `epoch, max_doa, confidence_dB, RSSI_dBm, freq_hz, ant_arrangement, lat, lon, gps_heading, compass_heading, …`, true-north-references the bearing (adds platform heading), maps confidence_dB → 0-100 %, and falls back to the device's configured GPS when the Kraken row reports 0,0.
- **Epiq Matchstiq X40** — the X40 itself has no built-in DF, so the integration point is "an Epiq-side DF pipeline (Sidekiq SDK / GNU Radio / proprietary) pushes pre-computed bearings to Ares as JSON-lines over TCP." Same wire format as `generic` below; tagged `device_type=matchstiq_x40` so the UI shows it correctly.
- **Generic JSON-lines TCP** — newline-delimited objects: `{"azimuth_deg":..., "frequency_hz":..., "rssi_dbm":..., "confidence_pct":..., "lat":..., "lon":..., "target_device_id":...}\n`. Any custom DF rig (a GNU Radio flow, a USRP B210, a hand-rolled correlative-interferometry pipeline) can stream into Ares in a one-line shell loop.

### D.2 Server-side pipeline
1. `app/core/sdr/manager.py` owns a registry of devices (persisted to `data/sdr_devices.json`), starts one async adapter task per enabled device, and accepts `LobEvent`s back.
2. Each LoB is buffered in a per-5 kHz-bin rolling deque (TTL 90 s), and the existing `app.core.geolocation.solve_fix` solver is re-run across all recent same-frequency LoBs — across devices — to update Cuts / Fixes / CAP-CEP ellipses.
3. Every LoB / fix / device-status change is fan-out-broadcast to WebSocket subscribers and pushed as CoT to all configured TAK targets.
4. When a new `fix` (or `cut`) is computed AND any contributing device has `auto_coverage` on, Ares schedules a real coverage simulation (`get_simulator().compute_coverage(...)`) from the emitter's centroid (33 dBm, 10 km radius, 144 radials by default — cheap enough to rerun on every fix; cooled-down per frequency bin so back-to-back LoBs don't queue) and broadcasts the GeoJSON over the same WS as a `coverage` event.

### D.3 CoT to ATAK / TAK Server
`app/core/cot.py` translates LoBs and fixes to CoT XML and pushes them to any set of targets (`udp://`, `mcast://`, `tcp://`) — configured via `ARES_COT_TARGETS` env or at runtime through `PUT /api/v1/sdr/cot/targets`. The default for a tactical LAN is `udp://239.2.3.1:6969` (the conventional ATAK multicast group). Schema:
- Each LoB → `u-d-r` drawn-route (device → bearing endpoint, length from RSSI-derived range), polyline shape, callsign `LoB <freq>MHz <bearing>°`, RSSI/range/device in remarks.
- Each fix → `a-u-G-U-C-I` (intel / unknown / ground) point with `ce=<CEP_m>` so ATAK draws the uncertainty circle natively, callsign `Ares Emitter <freq>MHz`.
The plugin doesn't need to do anything special — the CoT comes through ATAK's existing TAK-bus ingest.

### D.4 Endpoints
| | |
|---|---|
| `GET/POST/PUT/DELETE /api/v1/sdr/devices[...]` | Register / list / patch / unregister SDR/DF devices. |
| `POST /api/v1/sdr/devices/{id}/test` | TCP probe (does not start streaming). |
| `GET /api/v1/sdr/state` | Devices + recent LoBs + recent fixes + CoT targets in one shot. |
| `GET/PUT /api/v1/sdr/cot/targets` | List / replace the CoT push targets. |
| `POST /api/v1/sdr/lob` | Push one LoB manually (REST-friendly for external pipelines / tests). |
| `WS /api/v1/sdr/stream` | Live event stream — `snapshot`, `lob`, `fix`, `device_status`, `coverage`. |

### D.5 UI
- Header **📡 SDR / DF** button opens the `SdrPanel` (`frontend/src/components/Tools/SdrPanel.jsx`): device CRUD with status (`streaming` / `error` / counts), per-device `enabled` + `auto_coverage` toggles, TCP probe, CoT-target editor, latest-fix readout.
- The 2D map and the 3D globe render SDR LoBs / Cuts / Fixes / CEP ellipses through the **existing** `geolocationGeoJSON` pipeline — `SdrPanel` translates the server's `properties.type` (`lob` / `cep_ellipse` / `suspected_emitter`) to the `glx` tag the renderers already understand. No map-code changes were needed.
- The SDR auto-coverage GeoJSON lands in the maps as an additional `extraGeojsonLayers` entry (`id: sdr-auto-coverage`) layered on top of the operator's primary coverage.

---

## Server-side additions checklist (small, all reusable by web/mobile too)

| New endpoint | Purpose | Workstream |
|---|---|---|
| `POST /api/v1/auth/login` + token middleware | Auth for any networked/field deployment; plugin login | A.1 / C.1 |
| `GET /api/v1/server/info` | Version, GPU, installed packs, online/offline, disk | A.1 |
| `GET/POST/DELETE /api/v1/packs[...]` | List / download (incl. "full planet") / delete data packs; progress over WS | A.2 |
| `GET/PUT/DELETE /api/v1/atak/templates` | Template store mapping CloudRF-style JSON ↔ `CoverageRequest`; seeded from device/antenna catalogues | C.1 |
| `POST /api/v1/atak/export/kmz` *(optional)* | Wrap a coverage result into a KMZ for ATAK image-overlay import / TAK-bus sharing | C.1 |
| `POST /api/v1/geolocate/fix` | Server-side Cut/Fix/CAP-CEP solver (port of `LoBUtils.js`), shared by web/mobile/ATAK | C.2 |

Everything else the plugin and globe need already exists in `backend/app/api/routes.py`.

---

## Phased timeline & milestones

Workstreams A, B, C run in parallel; **A leads slightly** because it underpins C's field story. Rough sizing (1 engineer-equivalent; parallelizes with more).

| Phase | Workstream A — Offline core | Workstream B — Cesium globe | Workstream C — ARES-ATAK plugin |
|---|---|---|---|
| **P0 — Foundations** (1–2 wks) | Auth (`/auth/login` + middleware), `/server/info`, pack directory layout + manifests. | Add Cesium to the build; `<GlobeView>` skeleton with ellipsoid + OSM imagery; 2D⇄3D toggle. | tak.gov account + SDK 5.5; skeleton plugin, dropdown pane, one OkHttp call to `/simulate/coverage` rendered as a layer. |
| **P1 — Core capability** (4–6 wks) | Provider-chain refactor (local pack → online fetch → cache); `/packs` download incl. terrain region packs + "full planet"; quantized-mesh build step in the terrain pipeline. | Local quantized-mesh terrain on the globe; coverage GeoJSON + raster heatmap draped on terrain; KMZ import. | SOOTHSAYER parity core: token login, template store (`/atak/templates`), place TX, single-site + multisite coverage, RF-link mode, in-place TX edit, polygon clip, settings, KMZ export. |
| **P2 — Higher-order features** (3–4 wks) | OSM base-tile pack + building-footprint pack; graceful degradation for NOAA/weather/Overpass (last-known / manual entry + staleness flags). | LOS + Fresnel ellipsoid + 3D antenna lobes; airborne/satellite true-altitude rendering; `requestRenderMode` / lite-globe perf modes. | Co-Opt live coverage (interval/distance off CoT); Best Site Analysis over polygon; satellite/airborne with guardrails → **full SOOTHSAYER parity**. |
| **P3 — Ares differentiators** (4–6 wks) | AO imagery tile-cache tool (`/packs/imagery` → MBTiles); optional global Sentinel-2 10 m mosaic ingest. | Suspected-emitter + CAP/CEP + LoB fans on the globe; optional Google Photorealistic 3D Tiles layer (opt-in key). | `/geolocate/fix` solver; DF mode (add-LoB radial, wedges, Cut/Fix grouping, CEP, terrain-capping via `/lob/range_estimate`, CoT emitter marker, "model the emitter" follow-on). |
| **P4 — Extras & polish** (2–3 wks) | "Ares-in-a-box" offline bundle for Jetson/laptop/Pi-5; docs; LDAP/AD auth option. | Globe section-panel + vertical terrain profile; KMZ scene export; UX polish; mobile-WebView spike (optional). | HF/MUF + space-weather panel, MANET coverage, interference/EMCON, ray-trace link, live weather attenuation. |
| **P5 — Hardening & release** (2–3 wks) | Pack-integrity verification, disk-space guardrails, pack versioning/upgrade. | Cross-browser/Electron QA, large-scene stress test. | Multi-ATAK-version builds (5.3/5.4/5.5), self-signed-cert UX, memory guardrails for big airborne rasters (the SOOTHSAYER crash mode), tak.gov + Play signing submission. |

**Definition of done for v1:** (1) Ares runs fully offline from packs and transparently upgrades fidelity online; (2) the web/desktop app has a Cesium globe with coverage/LOS/Fresnel/antenna rendering on real terrain; (3) ARES-ATAK is feature-equal to the SOOTHSAYER plugin against an Ares server *plus* has DF mode, signed and published on tak.gov.

---

## Implementation status

### P0 — done (scaffold)
**Workstream A — server:**
- `backend/app/core/auth.py` — stateless HMAC-SHA256 bearer tokens, PBKDF2 password hashes, `data/users.json` store (random `admin` created on first run when auth is on), `require_auth` dependency. **Auth is off by default** (`ARES_AUTH=true` to enable); no new deps.
- `backend/app/api/auth_routes.py` — `POST /api/v1/auth/login`, `GET /api/v1/auth/me`.
- `backend/app/core/packs.py` — `data/packs/{terrain,osm,buildings,clutter,imagery}/<id>/manifest.json` layout, `PackManifest` schema, list/get/register/delete; `start_download` records a job and returns `status: not_implemented` (data-fetch pipeline lands P1–P3).
- `backend/app/api/system_routes.py` — `GET /api/v1/server/info` (version, GPU probe, pack counts, online/offline probe, disk), `GET/DELETE /api/v1/packs[...]`, `POST /api/v1/packs/download`, `GET /api/v1/packs/jobs[...]`.
- `backend/app/config.py` — `auth_enabled`, `auth_secret` (persisted to `data/.auth_secret`), `network_policy`, `PACKS_DIR`/`PACK_LAYERS`, app name "Ares".
- `backend/app/main.py` — new routers mounted; `ensure_default_user()` on startup; warns when auth is disabled.

**Workstream B — Cesium globe:**
- `frontend/package.json` — adds `cesium` + `vite-plugin-cesium` (run `npm install`); `frontend/vite.config.js` — `cesium()` plugin, `vendor-cesium` chunk split.
- `frontend/src/components/Map/GlobeView.jsx` — Cesium `Viewer` skeleton (Ion disabled, OSM imagery, flat ellipsoid, optional local quantized-mesh terrain via `CesiumTerrainProvider.fromUrl`, camera-sync via `onMoveEnd`). Standalone — **not yet imported by `App.jsx`** so the existing build is untouched until `npm install`.
- `frontend/src/hooks/useViewMode.js` — zustand `{ mode, view, setMode, toggleMode, setView }`; `frontend/src/components/Map/ViewModeToggle.jsx` — 2D/3D button; `frontend/src/components/Map/README.md` — exact `App.jsx` wiring snippet.

**Workstream C — ATAK plugin:**
- `atak-plugin/` — Gradle skeleton (`settings.gradle`, `build.gradle`, `gradle.properties`, `local.properties.example`) + `app/` module (`build.gradle`, `proguard-rules.pro`, `AndroidManifest.xml`, `assets/plugin.xml`, `res/values/strings.xml`).
- Kotlin: `AresPluginLifecycle`, `AresMapComponent`, `AresPluginTool`, `AresDropDownReceiver` (Settings→login, Coverage→run flows stubbed), `net/AresApiClient` (token auth + self-signed-cert support, `/auth/login`, `/server/info`, `/simulate/coverage`), `net/AresModels`.
- **Does not build yet** — needs the ATAK-CIV SDK from tak.gov; class bodies are skeletal with TODOs. See `atak-plugin/README.md`.

### P1 — done
**Workstream B — globe wired in + RF rendering:**
- `App.jsx` mounts `<GlobeView>` (lazy, own `vendor-cesium` chunk) instead of `<MapView>` when the view mode is `3d`; a floating `<ViewModeToggle>` (top-right of the map) flips between them via the `useViewMode` store. `<MapView>` is unchanged and still the default. App.jsx passes `coverageGeoJSON`, `extraGeojsonLayers`, `tx`, `rxPoint`, `minSignalDbm`.
- `GlobeView.jsx` renders, on flat-or-real terrain: **coverage** as a rasterised heatmap `ImageryLayer` (auto for >8 k points) or a ground-clamped colour-point cloud; **`extraGeojsonLayers`** (LineStrings → polylines, Points → markers); **TX/RX markers** + a dashed **LOS link line**; a translucent **first-Fresnel ellipsoid** along TX→RX; a translucent **antenna lobe** at the TX (directional cone if an azimuth is given, else an omni ring); plus **`lite` mode** (flat ellipsoid, no terrain mesh) and **`requestRenderMode`**. Real terrain via a `CustomHeightmapTerrainProvider` whose callback fetches per-tile int16 height grids from `/api/v1/terrain/heightmap/active` (see Workstream A below) — silent fallback to the flat ellipsoid when no terrain pack is installed.

**Workstream A — auth, packs, downloads, degradation:**
- `app/core/pack_builder.py` — real downloads: **terrain** = SRTM 1-arc-sec (~30 m) `.hgt` tiles for a bbox (or "full planet" clipped to SRTM coverage) from the AWS open-elevation bucket; **osm** = XYZ raster base-map tiles and **imagery** = XYZ satellite/aerial tiles (default ESRI World Imagery) for a bbox + zoom range from a configurable tile server (both via one shared `_build_xyz_pack` helper, rate-limited, hard-capped at 200 k tiles; point `source` at your own server for anything large); **buildings** = OSM building-footprint ways via Overpass (queried in 0.05° cells, max 600) → a GeoJSON file. Background job with progress on the job record; writes a manifest; disk-space guardrails. Clutter / land-cover rasters are still scaffolded only. (A few regional terrain packs — UK/N-Sea/Alps SRTM tiles — have actually been built into `backend/data/` in this environment.)
- `app/core/terrain_tiles.py` + `GET /api/v1/terrain/heightmap/{pack_id}` (`pack_id` may be `active`) — serves the 3D globe a w×h grid of int16 metres for a tile rectangle, sampled on the fly from the SRTM `.hgt` files in a terrain pack using the propagation engine's elevation interpolation. This is the chosen substitute for pre-generating a quantized-mesh tileset: no binary tooling, no new deps, works fully offline, and it's what `GlobeView.jsx`'s `CustomHeightmapTerrainProvider` consumes. So **"real terrain on the globe, offline" now works** end-to-end.
- `app/core/packs.py` — `start_download` queues buildable layers (`terrain`/`osm`); `system_routes.download_pack` schedules `pack_builder.run_job` as a background task → poll `GET /api/v1/packs/jobs/{id}`.
- `app/core/net_state.py` — online/offline probe (cached), last-known cache for cloud data, operator overrides, all persisted to `data/.net_cache.json`; `fetch_or_degrade(kind, fetcher)` returns `{source: live|cache|override, stale, as_of}`. Wired into `GET /api/v1/space_weather` (degrades gracefully offline instead of 503-ing). New routes: `GET /api/v1/net/status`, `PUT/DELETE /api/v1/net/override/{kind}`.

**Workstream C — server-side ATAK support + DF solver:**
- `app/core/geolocation.py` + `app/api/geo_routes.py` (`POST /api/v1/geolocate/fix`) — server-side port of the web UI's LoB math: group by frequency-tolerance + device identity → pairwise ENU bearing intersections → confidence-weighted centroid → CEP/CAP ellipse (intersection scatter ⊕ angular uncertainty) → classify `lob`/`cut`/`fix` → GeoJSON FeatureCollection (bearing wedges, ellipses, suspected-emitter points). With `options.terrain_aware`, each bearing lacking an explicit `estimated_distance_m` is terrain-capped via the propagation engine (the `/lob/range_estimate` maths) — the "DF that respects mountains" bit. Shared by web/mobile/ATAK.
- `app/core/templates.py` + `app/api/atak_routes.py` `…/atak/templates[...]` — Ares-native radio-template store (CRUD, 3 built-in seeds, `to_coverage_request(template, lat, lon, az)` flattener; `POST …/templates/{id}/coverage_request`).
- `app/core/kmz.py` + `POST /api/v1/atak/export/kmz` — rasterise a coverage GeoJSON (matching the web heatmap ramp) → PNG `GroundOverlay` → KMZ file download (ATAK "Image Overlay File" / WinTAK / Google Earth).
- `main.py` mounts the new `atak` and `geolocation` routers.

### P1+ — web ops console & map-prefs consolidation
- `frontend/src/components/Tools/AtakServerPanel.jsx` — the **"ATAK / Server" console** (modal from the header): server identity / GPU / online-offline / disk (`/server/info`), offline data packs per layer with a "download region pack" form (`POST /packs/download`) + job poller (`/packs/jobs`) + delete, and the radio templates the ATAK plugin would see (`/atak/templates`). This is the web/desktop counterpart of the plugin's Settings tab — the offline-ops console. Wired into `App.jsx`; new client helpers in `frontend/src/api/client.js` (`getServerInfo`, `getNetStatus`, `listDataPacks`, `downloadDataPack`, `listPackJobs`, `deleteDataPack`, `listAtakTemplates`, `geolocateFix`, …).
- `frontend/src/components/Map/MapSettingsCog.jsx` + `frontend/src/components/Map/mapPrefs.js` — a single ⚙ button on the map's floating toolbar that holds **all** map options (2D/3D view, distance/altitude units, coord system, compass rose, brightness, 3D coverage-render mode, feature colours), shared by both `MapView` (Leaflet 2D) and `GlobeView` (Cesium 3D) via a `useMapPrefs` zustand store so a basemap/colour chosen in one view applies to the other. Replaces the old bottom-panel "Map Options" tab.
- `frontend/src/components/Tools/DecibelCalculator.jsx` — bidirectional dBm/dBW/dBµW ↔ W/mW/µW/kW/MW converter with a log reference graph (also embeddable).
- `start-backend.sh` / `start-web.sh` / `start-desktop.sh` at the repo root — convenience launchers (activate `backend/.venv`, run uvicorn, open the browser or Electron). Electron `electron/main.js` refreshed (`npm install` done in `electron/`).

### Workstream D — Live SDR / DF integration
- `app/core/sdr/{manager,adapters}.py` — `SDRManager` (persisted device registry in `data/sdr_devices.json`, per-device async adapter task, per-frequency rolling LoB buffer, fan-out WS broadcast, CoT publishing, auto-coverage trigger with per-bucket cool-down) + three adapters: `KrakenSdrAdapter` (HTTP polling of `krakensdr_doa`'s `DOA_value` CSV row, true-north-references the bearing via the platform heading, falls back to the configured GPS when the Kraken row reports 0,0), `GenericJsonLinesAdapter` (newline-delimited JSON over TCP — the documented contract for any external DF pipeline), `MatchstiqX40Adapter` (= generic with a stable `device_type=matchstiq_x40` tag; the X40 itself has no built-in DF so an Epiq-side process pushes bearings in).
- `app/core/cot.py` — CoT XML emitter + transport pool. Targets are `udp://`, `mcast://`, `tcp://`; configured via `ARES_COT_TARGETS` or `PUT /api/v1/sdr/cot/targets`. LoBs become `u-d-r` drawn routes (device → bearing endpoint, range from RSSI-derived distance, polyline shape, RSSI/range/device in remarks); fixes become `a-u-G-U-C-I` intel-ground points with `ce=<CEP_m>` so ATAK draws the uncertainty circle natively. No CoT targets ⇒ all publishes are a fire-and-forget no-op.
- `app/api/sdr_routes.py` + `app/main.py` — `GET/POST/PUT/DELETE /api/v1/sdr/devices[...]`, `POST /api/v1/sdr/devices/{id}/test` (TCP probe), `GET /api/v1/sdr/state`, `GET/PUT /api/v1/sdr/cot/targets`, `POST /api/v1/sdr/lob` (manual push), and `WS /api/v1/sdr/stream` (live `snapshot` / `lob` / `fix` / `device_status` / `coverage`). The lifespan now starts/stops the manager and wires an `_auto_coverage_from_fix` runner that calls the existing `get_simulator().compute_coverage(...)` from each new emitter centroid (10 km, 144 radials, no per-tile imagery — cheap to rerun) and broadcasts the resulting GeoJSON on the same WS.
- `frontend/src/components/Tools/SdrPanel.jsx` + `frontend/src/api/client.js` — header **📡 SDR / DF** button opens the console: device CRUD with live status (`streaming`/`error`/LoB counts), per-device `enabled` and `auto_coverage` toggles, TCP probe, CoT-target editor, latest-fix readout. The panel subscribes to `WS /api/v1/sdr/stream` and lifts the server's `solve_fix` features + auto-coverage GeoJSON up to `App.jsx`, which merges them into the **existing** `geolocationGeoJSON` (translating `properties.type` → the `glx` tag) and into `extraGeojsonLayers` — so the 2D map and the 3D globe render live SDR bearings, Cut/Fix markers, CEP ellipses, and auto-updating coverage **with zero changes** to the map renderers themselves.
- Installer bumped to **v4.1** (`install.sh` + `install.bat`).

### P2 / P3 / P4 / P5 — feature-complete on the server / web / desktop side
- **Pack downloaders complete** — every layer in `PACK_LAYERS` now has a working downloader: terrain (SRTM30 .hgt), osm (XYZ raster), imagery (XYZ raster, default ESRI World Imagery), buildings (OSM/Overpass GeoJSON), **clutter** (`app/core/pack_builder.py:build_clutter_pack` — ESA WorldCover v200 2021 3°×3° 10 m GeoTIFFs from the AWS open-data bucket, cap of 64 tiles per job).
- **Provider chain (Workstream A.3)** — new `app/core/providers.py` implements *local pack → online fetch → cache* for terrain: `ensure_terrain_tiles(cells)` checks every installed terrain pack for each requested 1° SRTM cell, and when the cell is missing AND `network_policy=auto` AND `net_state.is_online()`, it downloads from the SRTM Skadi bucket into a self-maintained `terrain-auto` pack (manifest's bbox is the union of accumulated cells; `cesium_ready=true`). `terrain_tiles.sample_heightmap_rect` now samples across **all** installed terrain packs (so a cell from `terrain-auto` or any region pack is usable transparently); the heightmap route exposes a `grow` query (defaults to true) and returns an `X-Ares-Terrain-Source: pack|online|flat|unknown` header. End state: a connected box transparently **grows its own offline terrain pack** as new areas are viewed.
- **Graceful offline degradation (A.3, completed)** — `/space_weather` (already), **`/weather/current`** (refactored to call `net_state.fetch_or_degrade(weather:<lat>,<lon>, …)` — live → record, on failure last-known/override with `source`/`stale`/`as_of` fields), **`/terrain/buildings`** (when Overpass fails offline, served from any installed `buildings` pack that covers the point via the new `packs.buildings_near(lat,lon,radius_m)`, with `source: "pack"`).
- **Pack integrity & versioning (P5)** — `packs.verify_pack(pack_id, deep=False)` + `POST /api/v1/packs/{pack_id}/verify` check file count, size-vs-manifest delta, `ares_pack_version` upgrade-path warnings, and an optional deep re-hash. Wired into the web "ATAK / Server" console (⛉ button per pack).
- **LDAP / AD auth** — `app/core/auth.authenticate` is now backend-pluggable via `ARES_AUTH_BACKEND=local|ldap|ldap+local`; LDAP mode binds against `ARES_LDAP_SERVER` with `ARES_LDAP_USER_DN` (templated `{username}` — works for AD UPN or LDAP DN), optional admin-group membership lifts the user to `role=admin`. `ldap3` is an optional dep; if it's missing Ares logs a warning and behaves as `local`. Matches the SOOTHSAYER multi-user story.
- **"Ares-in-a-box" deployment** — `install.sh --offline-bundle <dir>` stages a pre-built bundle (the `data/packs/` tree + optional `terrain/`, `users.json`, `.auth_secret`) into `backend/data/` and skips the online terrain preload; `docker-compose.yml` exposes `ARES_PACKS_HOST_DIR` (bind-mount your packs SSD), `ARES_AUTH`, `ARES_NETWORK_POLICY`; new `docs/DEPLOYMENT.md` documents Jetson Orin Nano / rugged laptop / Pi 5 targets, the data-pack sizes, LDAP setup, and how the plugin points at the box.

### P1.x — globe consumes the offline packs + obstruction + pattern lobes
- `app/core/pack_builder.py` / `app/core/packs.py` — added the **imagery** XYZ downloader (refactored `build_osm_pack` into a shared `_build_xyz_pack`; default source = ESRI World Imagery, `.jpg`, z0–15); `imagery` joins `terrain`/`osm`/`buildings` as a `_BUILDABLE` layer in the dispatch and `start_download` messaging. (The `buildings` Overpass downloader already existed.) Static pack files are served by the existing `GET /api/v1/packs/{layer}/{pack_id}/{file_path}` route (`pack_id` may be `active`).
- `frontend/src/components/Map/GlobeView.jsx`:
  - **offline data packs on the globe** — on mount the globe fetches `/api/v1/packs?layer=osm|imagery|buildings`; each XYZ-raster pack is added as a `UrlTemplateImageryProvider` clamped to the pack's bbox (so Cesium never requests a tile the pack lacks; layered above the basemap, below the coverage raster); a `buildings` pack's GeoJSON is rendered as **extruded 3D building footprints** (height from `height_m`, default 8 m, base clamped to terrain, capped at 8 k). Packs downloaded mid-session appear after a 2D⇄3D toggle. Fully offline-safe (silent no-op when nothing is installed). The bottom-left status chip lists the loaded packs.
  - **LOS obstruction** — when a TX→RX pair is set, the globe pulls `GET /api/v1/terrain/profile` and runs a 4/3-earth clearance check; if the path is blocked it drops a red marker on the worst-blocking ridge (with the clearance deficit) and a red dashed segment TX→ridge, alongside the existing dashed LOS line and Fresnel ellipsoid.
  - **polar-pattern antenna lobe** — `addAntennaLobe` now shapes the lobe by the selected `polar_pattern`: a ground-clamped footprint polygon whose boundary radius tracks the azimuth-plane gain (peak along the antenna azimuth) + a 3D outline of that curve at antenna height + a boresight needle; omni antennas still get the simple extruded ring. `App.jsx` passes `antennaAzimuthDeg` / `antennaTiltDeg` / `antennaPattern` (previously unset, so the globe always drew an omni ring). Distances are illustrative (scaled by TX power), not metric. *(A full E-plane / 3D radiation-mesh lobe is the only remaining globe-side item.)*
  - **KMZ import / export on the globe** — 📥 toolbar button loads a `.kmz`/`.kml` via `Cesium.KmlDataSource.load` (ground-clamped) into a dedicated data source that's replaced on each import + auto-flies to the loaded scene; 💾 button rasterises the current coverage GeoJSON to a `GroundOverlay` KMZ via the existing `POST /api/v1/atak/export/kmz` and downloads it (ATAK image-overlay file / WinTAK / Google Earth interchange).

### P2–P4 — partial (ATAK plugin)
The plugin module is **fleshed out as a non-building skeleton** (still needs the tak.gov SDK): fuller `net/AresApiClient.kt` (token auth, self-signed certs, server info, packs, templates, coverage, p2p, manet, `geolocate/fix`, KMZ export, `/ws/simulate` progress) + `net/AresModels.kt` DTOs; `SettingsStore.kt` (persisted URL/token/Co-Opt policy/toggles); `CoOptManager.kt` (adopt-callsign → re-run coverage on time/distance triggers); `DfManager.kt` (collect LoBs → `/geolocate/fix` → suspected-emitter, terrain-refine hook); `CoverageOverlayRenderer.kt` (GeoJSON summarise/render to an ATAK overlay — stubbed at the `MapItem` boundary); `res/layout/ares_main.xml`. Everything touching `com.atakmap.*` / `transapps.*` (UI inflation, map overlays, radial-menu items, CoT publish/subscribe, KMZ import) is marked `TODO(...)`. Real ATAK rendering, the live CoT GPS feed for Co-Opt, the suspected-emitter CoT marker, multi-ATAK-version builds and tak.gov/Play signing are the remaining plugin work.

**SDK-blocked remainder (Workstream C only).** The Ares server, web/desktop globe, deployment, and ops surfaces are feature-complete against this plan. Everything that's still open lives in `atak-plugin/` and *cannot* be finished in this repo because it needs:
  1. The **ATAK-CIV SDK 5.x** (free tak.gov account) — to compile the plugin at all and to write the real `com.atakmap.*` rendering glue (overlay items, radial-menu actions, CoT publish/subscribe, KMZ ingest hooks) that the current Kotlin marks `TODO(...)`.
  2. **tak.gov + Google Play publisher accounts** — to sign and distribute the APK (one per supported ATAK release line, matching the SOOTHSAYER 5×APK cadence).
  3. A **CI matrix** against multiple ATAK SDK lines (5.3 / 5.4 / 5.5) for release-line parity.

The plugin's REST/WebSocket client, settings store, Co-Opt manager, DF manager, coverage-overlay renderer, models, and Gradle skeleton against SDK 5.x are all in place and structured — see `atak-plugin/README.md`. A full E-plane / 3D radiation-mesh antenna lobe on the globe is the only remaining web/desktop polish item.

### How to run / verify
- **Quick start:** `./start-backend.sh` (uvicorn on :8000 via `backend/.venv`), `./start-web.sh` (backend + `vite preview` on :3000 + opens a browser), `./start-desktop.sh` (Electron). A `backend/.venv` and `electron/node_modules` already exist in this environment. Air-gapped install: `./install.sh --offline-bundle <dir>` then `ARES_AUTH=true ARES_NETWORK_POLICY=offline_only ./start-backend.sh`. See `docs/DEPLOYMENT.md`.
- **Backend:** `cd backend && pip install -r requirements.txt && python -m app.main` — **no new mandatory Python deps**; LDAP auth additionally needs `pip install ldap3` (without it Ares falls back to local auth with a warning). Auth off → existing behavior. Try: `GET /api/v1/server/info`, `GET /api/v1/net/status`, `POST /api/v1/geolocate/fix`, `GET /api/v1/atak/templates`, `POST /api/v1/packs/download {"layers":["terrain","osm","imagery","buildings","clutter"],"bbox":[6,45,8,46]}` then `GET /api/v1/packs/jobs`, `POST /api/v1/packs/<id>/verify`, `GET /api/v1/terrain/heightmap/active?west=6&south=45&east=7&north=46&w=32&h=32` (returns the bytes and `X-Ares-Terrain-Source: pack|online|flat`), `GET /api/v1/weather/current?lat=51&lon=0`, `POST /api/v1/atak/export/kmz`. With `ARES_AUTH=true`: `POST /api/v1/auth/login` (admin password logged once on first run); set `ARES_AUTH_BACKEND=ldap` + `ARES_LDAP_*` for AD/LDAP. **SDR / DF (D):** `POST /api/v1/sdr/devices {"name":"kraken-1","type":"krakensdr","host":"kraken.lan","port":8080,"lat":51.5,"lon":-0.1,"frequency_hz":433920000}`, watch `WS /api/v1/sdr/stream` for `lob` / `fix` / `coverage` events, and configure CoT push with `ARES_COT_TARGETS=udp://239.2.3.1:6969,tcp://taksrv:8087` (or `PUT /api/v1/sdr/cot/targets`).
- **Frontend:** `cd frontend && npm install` (pulls `cesium` + `vite-plugin-cesium`) `&& npm run dev`. Click **2D/3D** in the map's ⚙ menu (top-right toolbar); run a coverage sim → it appears on the globe (raster heatmap or point cloud); a TX/RX pair shows the LOS line + Fresnel ellipsoid + (if blocked) a red obstruction marker; a directional antenna shows a pattern-shaped lobe footprint; if a terrain pack is installed the globe shows real relief, and installed osm/imagery/buildings packs are layered in (extruded buildings, offline imagery). The 📥 and 💾 toolbar buttons import a KMZ/KML and export the current coverage as a KMZ. Download / verify / delete packs from the header's **ATAK / Server** console.
- **Plugin:** still not buildable until the tak.gov SDK is configured — see `atak-plugin/README.md`.
- **Verification done here:** all backend `.py` pass `py_compile`; `App.jsx` + `GlobeView.jsx` bundle cleanly under esbuild (`--packages=external`); a few SRTM terrain packs have been downloaded into `backend/data/`. **Not** verified: backend at runtime in this environment, the frontend production bundle / Cesium rendering, the ATAK plugin (no SDK).

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| ATAK SDK version churn (plugins are tightly coupled; SOOTHSAYER maintains ~5 concurrent APKs) | Keep the plugin thin; isolate ATAK-specific code behind interfaces; CI matrix per supported SDK line. |
| API currently unauthenticated | Auth is P0 — non-negotiable before any networked deployment. |
| Big airborne rasters crash mobile (SOOTHSAYER's documented failure) | Enforce "radius_km × 2 ≈ resolution_m, keep < ~1 MP" client-side; reject oversize jobs server-side. |
| tak.gov / Google Play signing lead time | Start the tak.gov account + signing process in P0. |
| Coverage visual fidelity: GeoJSON contours vs. smooth raster heatmaps | Render enough contour bands; offer the optional server KMZ packer for pixel-parity. |
| DF math drift between JS/Python/Kotlin | Single server-side `/geolocate/fix` solver shared by all clients; unit tests against the existing `LoBUtils.js` results. |
| Cesium bundle weight / low-power devices | Code-split (loads only on entering 3D); `requestRenderMode`; "lite globe" fallback; Leaflet remains the default 2D view. |
| "Worldwide full-zoom imagery offline" expectation | Explicitly out of scope and documented; substitute = global 10 m mosaic + AO tile-cache packs (the ATAK model). |
| Disk: 30 m global terrain + imagery is hundreds of GB | Region packs by default; "full planet" is opt-in with a clear size estimate; disk-space guardrails; matches the SOOTHSAYER 500 GB-data-disk profile. |

---

## Reference: external context

- CloudRF *SOOTHSAYER*: <https://cloudrf.com/soothsayer/> · ATAK plugin docs: <https://cloudrf.com/documentation/06_atak_plugin.html> · plugin source: <https://github.com/Cloud-RF/SOOTHSAYER-ATAK-plugin>
- CloudRF API: <https://cloudrf.com/documentation/01_about.html>
- ATAK-CIV SDK: tak.gov (account required) · reference repo: <https://github.com/deptofdefense/AndroidTacticalAssaultKit-CIV> · plugin tutorials: <https://www.riis.com/blog/plugins-with-atak-civ-sdk-5-5>, <https://www.civtak.org/2024/12/04/atak-plugin-development-tutorial/>
- CesiumJS: <https://cesium.com/platform/cesiumjs/> · quantized-mesh terrain tooling: `cesium-terrain-builder` / `ctb-quantized-mesh`
- Existing Ares API surface: `backend/app/api/routes.py` (coverage, p2p, multipoint, super_layer, manet, best_server, interference, ray_trace, best_site[_polygon], satellite_visibility, route, terrain/*, hf/muf, space_weather, weather/current, antenna/*, materials, devices/presets, ws/simulate) · DF math: `frontend/src/components/Geolocation/{LoBUtils,LoBAutoDetect}.js`
