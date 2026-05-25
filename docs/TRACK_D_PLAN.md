# Track D — Platform, integration & UX — implementation plan

Detailed execution plan for ROADMAP Track D: **D1 ATAK plugin/server**, **D2 MANET
(Silvus/Meshtastic) + remote**, **D3 Electron→Tauri**, **D4 Rust oxidation**. Each
section states the *grounded current state* (with files/lines), the gaps, concrete work
items, and how to verify.

**Headline finding that reshapes D3:** the backend already serves the UI + API + WS
**same-origin** (`docs/REMOTE.md`; `ARES_FRONTEND_DIST`). So Tauri can load
`http://127.0.0.1:8000` directly and drop Electron's static server + `/api` proxy + WS
upgrade-forwarding (`electron/main.js:193-294`) — the single hardest piece disappears.

**Suggested Track-D order:** D3 first (self-contained, high value, no blockers) → D2
remote-hardening + Silvus auto-peer (IP, no new hardware) → D2 Meshtastic bridge → D1
radial-menu + CoT-receive depth → D1 build/CI/TAK-Server (blocked on tak.gov SDK) → D4
(opportunistic).

---

## Status (implemented on the `track-d` branch)

| Item | State | Verified |
|---|---|---|
| D3 Tauri shell (Option A: backend lifecycle, remote commands, init-script shim) | done | `cargo check` + clippy clean; **app launches + spawns backend + serves UI** |
| D3.3 first-run bootstrap · D3.5 menu/dark/single-instance · exit-cleanup | done | compiles; runs |
| D2.1 compact codec + transport seam | done | 6/6 harness |
| D2.2 Meshtastic bridge · D2.3 Silvus adapter · D2.5 built-in TLS | done | 4/4 harness (lib/radio lazy-imported) |
| D2.4 transitive gossip + hop-cap | already in `mesh.py` | — |
| D1.4b inbound LoB/fix CoT parse | done | 5/5 harness |
| D1.3 radial menu · D1.4a CoT listener · D1.2 CI matrix | written, **SDK-blocked** | not compiled — needs tak.gov SDK |
| D1.1 APK build · D1.5 TAK-Server live test | **blocked** | needs SDK / a running TAK Server |
| D4 PyO3 seam + baselines | done | crate `cargo check` clean; benchmark runs; pure-Python fallback |
| D4 ports: diffraction (5 models) + ITM `_hzns` | done | **bit-identical parity** vs Python (Δ=0); deygout **20.5×**, `_hzns` 3.6×; CI `native` job |
| D4 ports: DVB FEC chain (RS(204,188) + derandomise + Viterbi) | done | **byte-identical** parity (300+ RS trials, 20×2 Viterbi); RS **23×**, Viterbi **38×**; round-trip self-tests pass on both paths |

Everything verifiable in this environment is green; the only remainders are
hard-blocked on the tak.gov ATAK-CIV SDK + Android toolchain (D1.1/1.2/1.3/1.4a) or a
live TAK Server (D1.5).

---

## D1 — ATAK plugin / server

### Current state (mature)
- **Backend CoT is done** — `backend/app/core/cot.py`: `lob_cot` → `u-d-r` drawn route
  (`cot.py:151`), `fix_cot` → `a-u-G-U-C-I` point with CEP (`:180`), `geochat_cot` →
  `b-t-f` (`:263`); transports udp/mcast/tcp/**tls** with mutual-TLS + cert pinning to a
  TAK Server (`_tls_context` `:47`); a receive listener (`start_cot_listener` `:352`) that
  parses inbound **GeoChat** and routes it back to `chat_hub.ingest_cot` (`:340-349`).
- **Kotlin plugin fully wired, no TODO stubs** (`atak-plugin/app/.../plugin/`):
  `AresPlugin`/`AresPluginTool`/`AresMapComponent`/`AresDropDownReceiver`/`SettingsStore`/
  `CoverageOverlayRenderer`/`CoOptManager`/`DfManager` + `net/AresApiClient` (covers every
  backend route, token auth, self-signed certs) + `net/AresModels`.

### Gaps (from `atak-plugin/README.md:21-34` + code)
1. **Build verification** — never compiled here (no JDK / Android SDK / tak.gov SDK).
2. **CI matrix** per ATAK SDK line (~5.3 / 5.4 / 5.5), one APK each.
3. **Radial-menu items** — "Edit RF" / "Add LoB from here": plumbing exists
   (`AresDropDownReceiver.runCoverageRaw`, `DfManager.addLoB`) but no `MenuMapAdapter`
   entries.
4. **CoT-receive depth** — only GeoChat is re-ingested; inbound **LoB/fix** CoT from
   non-Ares EW kit isn't parsed. Co-Opt polls `MapView` positions instead of subscribing
   to `CotServiceRemote.CotEventListener` (deliberate, but no sensor-push path).
5. **"ATAK server"** — `tls://` to a TAK Server works in code but is untested against a
   real server; no documented data-package / cert-enrollment flow or server-side relay.

### Work items
- **D1.1 — Compile + sideload** (needs a provisioned host): JDK 11 Adoptium + Android
  Studio + tak.gov `atak-civ` SDK; set `sdk.dir` + `takdev.plugin` in `local.properties`;
  `./gradlew assembleCivDebug`; `adb install`. *Environment-blocked here.*
- **D1.2 — CI**: GitHub Actions matrix building `civ` flavors against pinned SDK versions,
  emitting per-line APK artifacts. The SDK isn't redistributable → pull it from a CI
  secret / private cache or use a self-hosted runner.
- **D1.3 — Radial menu**: add `MenuMapAdapter` entries + menu XML that call
  `runCoverageRaw` (Edit RF at a tapped point) and `DfManager.addLoB` (Add LoB from here).
- **D1.4 — CoT-receive depth**: (a) plugin — add a `CotServiceRemote.CotEventListener` in
  `AresMapComponent.onCreate` for sensor-pushed updates; (b) backend — generalize
  `_parse_geochat` (`cot.py:306`) into a `_parse_cot` that also ingests inbound `u-d-r` /
  `a-u-G-U-C-I` from external EW kit into the DF solver (treat as external LoB/fix
  observations, same path the mesh uses).
- **D1.5 — TAK Server feed**: test the `tls://` mutual-TLS path against a real TAK Server;
  document cert enrollment / data-package import; optional server-side mission relay.

### Verify
`assembleCivDebug` succeeds → sideload → enable ARES → run a coverage template from the
pane (renders Markers) → add a LoB → a suspected-emitter CoT appears on a second ATAK
client → GeoChat round-trips both ways → `tls://` connects to a TAK Server.

---

## D2 — MANET (Silvus / Meshtastic) + remote-access

### Current state (IP mesh works; remote works)
- **`backend/app/core/sdr/mesh.py`** — `PeerMesh`: one async WS loop per peer to
  `/api/v1/sdr/stream` (`_peer_loop:235`), ingests `lob`/`fix`/`chat`, dedups by
  `(origin_node, id)` (`_ingest_lob:294`), hop-bounded (`_MAX_HOPS=8` `:281`), exponential
  backoff. Peers via `ARES_MESH_PEERS` or `/api/v1/sdr/peers` CRUD
  (`sdr_routes.py:279-304`), persisted `data/.mesh_peers.json`.
- **`backend/app/core/meshsec.py`** — shared-secret HMAC-SHA256 over canonical LoB/chat
  fields (`_LOB_FIELDS:41`), signs/verifies, `hops` intentionally unsigned; WS accepts
  bearer **token or `?mesh_secret=`** (`sdr_routes.py:606`).
- **Because the mesh rides any IP link, a Silvus StreamCaster (an IP radio) already works
  today** as a transport — you just add peer URLs.
- **Remote** — `electron/main.js` remote.json + `ARES_AUTH=auto/true` +
  `ARES_ADMIN_PASSWORD` + admin-token loopback injection; backend serves the UI;
  `docs/REMOTE.md` covers Ethernet/Wi-Fi/USB-NIC/Thunderbolt/BT-PAN.

### Gaps
1. **Meshtastic** — absent. LoRa MTU ~237 B / low bandwidth → needs a **compact binary
   LoB/chat codec** + a Meshtastic bridge (serial/BLE/MQTT). `pyserial` is already a dep
   (`requirements.txt:24`).
2. **Silvus** — works over IP but no Silvus-specific **neighbor/link-state discovery** or
   auto-peering; no link-quality surfaced.
3. **Routing** — transitive gossip converges on partial meshes but isn't optimal for
   dense/arbitrary topologies (`mesh.py:19-22` notes "full flooding with hop-count is a
   follow-up").
4. **Remote hardening** — TLS is manual (front with Caddy/nginx); no built-in HTTPS, no
   off-LAN/NAT-traversal path.

### Work items
- **D2.1 — Transport abstraction + compact codec**: factor the WS peer loop into a
  `MeshTransport` interface ("ip" = today's WS). Add a binary LoB/chat codec (CBOR /
  packed struct) that preserves the HMAC `sig`; keep `meshsec` signing unchanged.
- **D2.2 — Meshtastic bridge**: new `backend/app/core/sdr/mesh_meshtastic.py` using the
  `meshtastic` Python API (serial/BLE/TCP). Encode LoB/chat with the compact codec, chunk
  to LoRa frames; on RX decode → `peer_mesh._on_lob` / `chat_hub.ingest_peer`. Config in
  the remote/mesh UI. Develop against the Meshtastic simulator; final test needs 2 radios.
- **D2.3 — Silvus adapter**: new `mesh_silvus.py` polling the StreamCaster JSON API (node
  list + link SNR) → auto add/remove IP peers and surface link metrics in
  `/api/v1/sdr/peers` status + the peers UI.
- **D2.4 — Routing**: on ingest, re-broadcast to this node's own subscribers (true gossip
  flood, TTL via existing `hops`); keep the `_seen_set` dedup; document convergence. Add an
  optional per-link cost later.
- **D2.5 — Remote hardening**: optional built-in uvicorn TLS (`ARES_TLS_CERT`/`KEY`) for
  https without a proxy; document a reverse-tunnel recipe (WireGuard / frp) for off-LAN;
  expose both in the Remote Access panel.

### Verify
Two backends on synthetic drivers, peer them → a LoB on node A fuses into node B's fix
(extend the validation-harness style). Meshtastic: simulator (or 2 radios) → a LoB crosses
the LoRa link. Silvus: mock API populates peers + link SNR. Remote: enable remote access,
connect from a phone browser over `wss://`, confirm token auth.

---

## D3 — Electron → Tauri

### Current state
`electron/main.js` (585 lines) does: backend subprocess spawn with `HOST`/`PORT`/
`ARES_AUTH`/`ARES_ADMIN_PASSWORD` env (`startBackend:304`); a loopback static server +
`/api`,`/ws` proxy with admin-token injection (`:193`) and **WS upgrade forwarding**
(`:267`); first-run venv+pip+`npm run build` with a splash+log (`:165-190`); `remote.json`
read/write (`:54-63`); IPC `remote:get`/`remote:set` (`registerIpc:387`); app menu, forced
dark mode, GeoClue geolocation, taskbar id. `preload.js` exposes
`window.electronAPI` (export/purge callbacks) + `window.aresDesktop`
(`isDesktop`,`getRemote`,`setRemote`) — the frontend detects desktop via
`window.aresDesktop` and drives the Remote Access panel through it.

### The simplification
The backend serves UI+API+WS same-origin, so **Tauri loads `http://127.0.0.1:<port>`
directly (Option A)** — no static server, no proxy, no WS forwarding. This is exactly the
"browser pointed at the appliance" path that already works in `docs/REMOTE.md`.

### Work items
- **D3.1 — Scaffold** `src-tauri/` + `tauri.conf.json`; reuse the existing React `dist`
  build; webview loads the backend URL after a health check. Keep Electron shipping until
  parity.
- **D3.2 — Backend lifecycle (Rust)**: spawn `uvicorn app.main:app` via Tauri `Command`
  with the same env; poll `/api/v1/health`; graceful SIGTERM→SIGKILL on exit (port
  `startBackend`/`waitForBackend`/`restartBackend`). Start with **system-Python + venv**
  (as today); defer a bundled-Python sidecar.
- **D3.3 — First-run setup (Rust)**: port `ensureBackendEnv` + `ensureFrontendBuilt` (venv
  create, `pip install -r`, `npm install && npm run build`) to Tauri `Command`s, streaming
  logs to a splash window via Tauri events.
- **D3.4 — Desktop bridge**: replicate `window.aresDesktop` with Tauri commands
  (`remote_get`/`remote_set`) and a **webview init script** that shims `window.aresDesktop`
  + `window.electronAPI` so the frontend needs **zero changes**. `remote.json` in Rust;
  toggling remote restarts the backend with new env. For the auth-ON case, inject the admin
  token into `localStorage` via the init script (replaces Electron's proxy token-injection).
- **D3.5 — Menus / permissions**: app menu (export / purge / devtools / about), forced
  dark, geolocation allowlist (GeoClue parity on Linux), single-instance, taskbar id,
  F12/F11.
- **D3.6 — Packaging**: Tauri bundler → AppImage/deb, nsis, dmg (replacing
  `electron-builder` in `electron/package.json`); CI builds all three; ~150 MB → ~40 MB.
- **D3.7 — Cutover**: ship both for one release, then default to Tauri; update
  `start-desktop.sh` + docs.

### Risks
Python bundling (keep system-python first); GeoClue/geolocation parity on Linux; the
auth-ON token-injection via init script instead of a proxy. None block Option A.

### Verify
`tauri dev` launches → backend boots → UI loads → DF/audio WS work (same-origin, so they
just work) → Remote Access toggle restarts the backend and a phone connects → bundles build
on all three OS → first-run on a clean machine bootstraps venv + UI build.

---

## D4 — Selective Rust oxidation (evidence-gated, not scheduled)

Set up the *machinery* now, port nothing until a trigger fires (see ROADMAP D4 table):
- A PyO3 crate skeleton (`backend/native/` via `maturin`), wired into `install.sh` as an
  optional build with a clean Python fallback when the wheel is absent.
- Baseline benchmarks (`pytest-benchmark` / `criterion`) on the four candidate paths
  (real-time IQ pipeline, multi-VFO channelizer, NIC modem, ITM inner loop) so each
  "trigger" is a measured number, not a guess.
- Port the **first** path only when its trigger fires; re-check the IQ candidate against a
  Python 3.13 free-threaded interpreter before committing (the GIL argument may have
  shrunk by then).

---

## Cross-cutting blockers
- **D1** needs the tak.gov SDK + JDK 11 + Android Studio, and the SDK can't be bundled into
  public CI (secret / self-hosted runner required).
- **D2** Meshtastic/Silvus *final* validation needs the physical radios; both can be
  developed against simulators / mock APIs first.
- Everything in **D3** and the IP-mesh / remote-hardening parts of **D2** are unblocked and
  can start immediately.
