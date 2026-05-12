# Ares v2.0 — "Authoritative" rewrite

This branch (`ares-authoritative`, `app_version = 2.0.0`, installer **v5.0**) replaces the
*indicative* implementations the [feature critique](./BUILD_PLAN.md) flagged with rigorous,
reference-grade ones, and adds the multilateration / measured-pattern / TLS pieces that were
missing entirely. It is API- and UI-compatible with v1.x — the frontend, the SDR/DF pipeline,
the ATAK plugin and the offline-pack machinery are unchanged; what changed is *what the physics
and geometry actually compute*.

Run the validation harness: `cd backend && python -m tests.test_authoritative` (28 checks).

---

## 1. ITM — the canonical ITS Longley-Rice  ✅ rigorous

**`backend/app/core/propagation/itm_its.py`** is a faithful Python port of the public-domain
NTIA/ITS Irregular Terrain Model (ITM v1.2.2; Hufford/Longley/Rice, NTIA Report 82-100 &
Tech Note 101) — point-to-point mode from a terrain profile *and* area mode, with the full
`lrprop` dispatch (`alos` / `adiff` / `ascat`), `qlrps` / `qlrpfl` / `qlra` setup, the terrain-prep
(`hzns` / `zlsq1` / `dlthx`), and the complete `avar` time/location/situation **variability**
machinery over all **7 climate zones**. This is the same algorithm SPLAT!, Radio Mobile and the
FCC's analyses use — not the simplified re-derivation that lived in `itm.py` (kept as the legacy
"fast/empirical" path). The simulation engine now calls this for the `itm` model
(`simulation.py` → `from app.core.propagation.itm_its import compute_itm_path_loss`).

Verified: free-space loss exact; loss monotone with distance; a 450 m mid-path ridge → ~74 dB
excess; residual σ ≈ 7–10 dB at 50 km (non-zero, climate-dependent); `q=0.5` recovers FSL + the
reference attenuation. *Known limitation:* the LOS↔transhorizon **classification label** can be
wrong for a deep single-ridge path (the `hzns` horizon detection there is approximate) even though
the *loss magnitude* tracks; reference-vector validation against the NTIA `itm.cpp` test cases is
the remaining hardening step (`itm_its.itm_reference_check()` prints the current outputs).

## 2. DF — maximum-likelihood fix + covariance error ellipse + GDOP + EKF track  ✅ rigorous

**`backend/app/core/geolocation.py`** — the centroid-of-pairwise-intersections estimator and the
fixed-aspect heuristic ellipse are gone. `solve_fix` now does **IRLS Gauss-Newton ML triangulation**
(`ml_fix`) — minimise Σ (Δθ_i / σ_i)² over emitter position, per-LoB σ from the reported confidence
(and the receiving array's −3 dB beamwidth when known) — and derives the **2×2 ENU position
covariance** from (JᵀWJ)⁻¹ (χ²-scaled when the fit is poor). The error ellipse comes from the
**eigendecomposition of that covariance**, so it stretches into a long thin cigar along bad-geometry
directions exactly like a real DF system's (verified: aspect ≈ 1.6 for well-spread observers, ≈ 97
for three near-collinear ones); **CEP** and the 95 % ellipse are σ-derived; **GDOP** (m per rad)
and the residual RMS are reported. `EmitterTrack` is a constant-velocity **extended Kalman filter**
the live SDR/DF manager now runs to smooth the stream of independent fixes into a track (position +
velocity + heading + filter σ) — broadcast on `WS /api/v1/sdr/stream` as part of each `fix` event.

## 3. TDOA / FDOA multilateration  ✅ new

**`backend/app/core/multilaterate.py`** + **`POST /api/v1/geolocate/multilaterate`** — hyperbolic
(time-difference-of-arrival) and, optionally, Doppler-difference (FDOA) emitter location from ≥3
receivers (the technique CRFS/Epiq/R&S networked systems use): Chan-style linearised closed-form for
the initial guess, weighted Gauss-Newton on the TDOA residuals (analytic Jacobian) + optional FDOA
residuals (finite-difference Jacobian), covariance → GDOP + a geometry-correct error ellipse,
GeoJSON of receivers / emitter / ellipse. Verified: noiseless recovery to ≈ 9 m; 15 ns TDOA noise →
~12 m fix, CEP ≈ 4 m, with four well-placed receivers.

## 4. SGP4 satellite propagation  ✅ rigorous

**`backend/app/core/propagation/sgp4_lib.py`** — uses the canonical **`sgp4` package** (Vallado's
reference code, with SDP4 deep-space) if installed; otherwise a **vendored faithful SGP4 near-earth**
propagator (WGS-72, Hoots & Roehrich / SPACETRACK REPORT #3 — the regime that covers ISS / Starlink /
imaging / weather sats, period < 225 min), with real TEME ECI → geodetic conversion and topocentric
look angles. `POST /api/v1/simulate/satellite_visibility` now reports footprints and true az/el/slant-
range, and flags deep-space objects (period ≥ 225 min) where it recommends `pip install sgp4` for
SDP4-grade accuracy. Verified against a real ISS TLE: ~417 km altitude, |r| ≈ Re + alt, error code 0,
stable across an orbit.

## 5. HF — ITU-R P.533-style sky-wave circuit prediction  ⚠️ much-improved (not P.533-exact)

**`backend/app/core/propagation/hf.py`** + the rewritten **`GET /api/v1/hf/muf`** — the
"MUF ≈ f(path length)" heuristic is replaced with a real circuit model: multi-hop F2 geometry on a
spherical earth (number of hops, ground hop length, take-off angle, angle of incidence at the F2
layer and at the 110 km D-region, slant ray-path length); a parameterised foF2 at each hop's control
point (solar activity via R12 / solar flux, diurnal via the solar zenith angle, a geomagnetic-latitude
term); the path **MUF / FOT (=0.85·MUF) / HPF / LUF** from the secant law; **non-deviative D-region
absorption** per hop via the ITU-R P.533 §4 formula, summed; **basic transmission loss** = free-space
(slant) + absorption + ground-reflection + an "other losses" term; received **SNR** vs an ITU-R P.372
noise floor; and **circuit reliability** combining the MUF day-to-day variability, the LUF/absorption
limit and the SNR margin. If a local `ITURHFPROP` / `voacapl` binary is on the PATH it is reported as
the backend (the reference engines carry the CCIR/URSI coefficient *maps*; our foF2 is a parameterised
"P.533-style" model, not those maps — wrapping ITURHFPROP/VOACAP is the path to P.533-exact). Verified:
MUF ≈ 12.7 MHz / FOT ≈ 10.8 MHz / LUF ≈ 8.8 MHz for a 1200 km mid-latitude noon circuit at R12=70;
night MUF < day MUF; the FOT band is reliable, well above MUF is not.

## 6. Clutter (land-cover) integrated into the propagation path  ✅ wired (needs `rasterio`)

**`backend/app/core/clutter.py`** — reads the ESA WorldCover 10 m GeoTIFF tiles from an installed
`clutter` data pack and maps each land-cover class to a *canopy height* (forest ≈ 15 m, urban ≈ 12 m,
shrub ≈ 3 m, …) added **per-sample** to the terrain profile in `simulation._compute_radial`, plus an
ITU-R P.833-style excess-loss figure — replacing the single scalar `clutter_height_m` (which still
works, and stacks on top). GeoTIFF decoding needs an optional dep — `rasterio` (windowed reads;
preferred) or `tifffile` — and gracefully no-ops to the scalar path when neither is installed:
`pip install rasterio` to activate. `GET /api/v1/clutter/status` (via `clutter.status()`) reports state.

## 7. Measured antenna-pattern import (NSMA / Planet MSI, NEC-2)  ✅ new

**`backend/app/core/propagation/pattern_import.py`** + **`POST /api/v1/antenna/import_pattern`** —
parses the NSMA / "Planet" (`.msi` / `.pln` / `.ant`) pattern files vendors ship (header: gain, H/V
beamwidths, F/B, polarisation, frequency; plus the 360-point HORIZONTAL and VERTICAL attenuation cuts)
and reconstructs a 2-D `gain_dbi[az][el]` grid (the "summing" cross-section method) as the JSON the
engine's `custom_pattern` consumes — so a *measured* pattern flows straight into the coverage path, not
just the analytic approximations. A best-effort NEC-2 `RP`-card parser is included too. Verified: a
17 dBi 90° sector MSI → 17 dBi at boresight, ≈ −13 dBi to the rear; round-trips through
`antenna._custom_pattern`.

## 9. Array direction-finding — phase interferometry + MUSIC/Capon/Bartlett  ✅ new

**`backend/app/core/df/interferometry.py`** + **`POST /api/v1/df/aoa`** (and **`GET /api/v1/df/info`**) —
the array-signal-processing layer Ares previously *didn't* have. It consumed bearings from
KrakenSDR / external pipelines; it can now *produce* them from a multi-element antenna array
(ULA, UCA, or arbitrary 2-D/3-D geometry given element positions in metres):

* **Phase interferometry** (`aoa_interferometry`) — the rigorous multi-baseline method. Build the
  array manifold over an (az, el) grid; the AoA estimate is the one whose *unwrapped* model
  phase-difference vector best matches the *wrapped* measured one (`m_i = arg(x_i/x_ref)`) — this is
  simultaneously a phase-only maximum-likelihood and a correlative-interferometry search. Refine with
  Gauss-Newton. Long baselines give precision, short baselines resolve the 2π ambiguity (verified: a
  4-element 3 λ ULA reaches σ_az ≈ 0.11° *and* resolves the wraps a single λ/2 array can't); the
  irreducible front/back mirror of a ULA is reported in `ambiguities`. **CRLB**: σ_az / σ_el =
  σ_φ · √diag((JᵀJ)⁻¹) from the manifold gradient — so the array's measured uncertainty is honest.
* **MUSIC** (`aoa_music`), **Capon/MVDR** (`aoa_capon`), **Bartlett** (`aoa_bartlett`) — covariance-based
  super-resolution / adaptive / conventional beamforming from IQ snapshots (N channels × K samples),
  with optional forward-backward spatial smoothing for coherent (multipath) sources; returns the
  spatial spectrum and the deterministic CRLB. Verified: a 5-element UCA resolves two sources at 60°
  and 140° to ≈0.1°.
* **`aoa_to_lob`** turns an AoA result straight into a `geolocation.LoB` (true bearing = AoA + the
  array's `heading_deg`, σ_az → confidence), so an array fix flows directly into the ML triangulation
  and its covariance error ellipse — *array DF → terrain-aware geolocation* end-to-end.
* The SDR pipeline learned to accept array snapshots: a `generic` adapter message of the form
  `{"array_phases_rad":[...], "frequency_hz":..., "array":{"type":"uca","n":5,"radius_m":0.21}}` (or
  `{"iq_real":[[...]], "iq_imag":[[...]], "method":"music", ...}`) is run through the interferometry
  engine to derive the bearing + confidence before it enters the LoB buffer / solver / CoT / EKF.

Azimuth is true-north (clockwise); a planar-horizontal array (ULA, UCA) is azimuth-only by design (a
horizontal array has no useful elevation observability); only a vertical or fully 3-D array also
resolves elevation. The KrakenSDR / external-DoA path still works in parallel — Ares can now *be* the
DoA estimator or *consume* one.

## 8. CoT over TLS (mutual-TLS to a TAK Server)  ✅ new

**`backend/app/core/cot.py`** — CoT push targets now accept `tls://host:port` (and `ssl://`) in
addition to `udp://` / `mcast://` / `tcp://`, with an SSL context built from `ARES_COT_TLS_CA`
(server truststore), `ARES_COT_TLS_CERT` / `ARES_COT_TLS_KEY` (client cert for **mutual-TLS** — what a
real TAK Server input needs) and `ARES_COT_TLS_INSECURE` (lab-only verify-off). Configure via
`ARES_COT_TARGETS=tls://taksrv.lan:8089,udp://239.2.3.1:6969` or `PUT /api/v1/sdr/cot/targets`.

## 10. SDR console — channels, GPS, DF panel, compass modes, calibration, waterfall, SoapySDR

* **Two source classes** — `single_channel` (monitor a spectrum / decode audio — *cannot* shoot a LoB; the manager rejects its LoBs and says so) and `multi_channel` (declare the channel count + array type/spacing — DF; more channels ⇒ tighter LoBs). A device-setup **LoB-accuracy estimate** (`GET /api/v1/sdr/accuracy_estimate`) shows the expected σ_az from the interferometry CRLB + a ~2.5° practical calibration floor that tightens with N.
* **Compass — three modes + calibration** (matching the EW-DF convention): **Absolute LOB** (true north — plottable on a map), **Relative LOB** (degrees off the antenna front, 0° = front), **Clock position** (off the antenna front). `Absolute LOB = (0° + heading) + Relative LOB`. **Compass calibration** (`POST /api/v1/sdr/devices/{id}/calibrate`, `GET /api/v1/sdr/compass/modes` for the 5-step procedure): aim the antenna at a target whose true bearing you know, read the Relative LOB, ⇒ `heading = (true − relative) mod 360`; the DF panel has the calibrate form with the instructions inline.
* **DF bottom-panel tab** — left ≈ ½: one (single-channel) or vertically-stacked (multi-channel, one per channel) **spectrum viewers** — scroll-zoom about the cursor, drag-pan, click-to-tune, **fixed y-axis** (pinned to noise-floor↔peak so it never moves), with the threshold/noise-floor/peak lines, **and a ▦ waterfall (spectrogram)** that opens *under each* viewer showing the time–frequency history; middle: a **compass** of the live LoB bearings (with the antenna-front reference + the latest LoB in all three reps); right: the DF options — tuner readout, threshold (min power for a bin to count active → shoot a LoB), gain/AGC, demodulate-and-listen, freq min/max, compass mode + calibrate, accuracy estimate, GPS status.
* **GPS** — `GET/POST /api/v1/sdr/gps` (also `gpsd`/an app can POST), used as the observer position for LoBs that arrive without one, broadcast on the WS, and rendered as a **"you are here" marker on the 2-D map and the 3-D globe** (heading arrow if known). LoBs from the SDR auto-plot from this location.
* **Spectrum / audio data path** — `GET /api/v1/sdr/devices/{id}/spectrum` returns a PSD frame (synthetic placeholder until hardware is wired); a **SoapySDR shim** (`app/core/sdr/soapy.py`) registers a real provider when the `SoapySDR` bindings are installed (RTL-SDR/HackRF/Airspy/USRP/Lime/Pluto/BladeRF/**Epiq Sidekiq-Matchstiq X40**/KrakenSDR tuners). `GET /api/v1/sdr/audio/modes` lists the decodable transmission modes (DMR all tiers, dPMR, **P25 P1+P2**, **TETRA**, **NXDN 4800+9600**, D-STAR, YSF/C4FM, M17, EDACS ProVoice, POCSAG/FLEX, AIS, ACARS, ADS-B + analog NFM/AM/SSB) and which open-source decoder programs are on the PATH; `POST .../audio` dispatches to one (op25/dsd-fme/sdrtrunk/tetra-rx/multimon-ng) or reports that it isn't installed / needs a baseband stream — the AMBE/ACELP vocoders can't be vendored.
* **ATAK integration on/off** — `settings.atak_enabled` (env `ARES_ATAK=false`), reported in `/server/info`, toggled via `POST /api/v1/atak/enabled` and an **ON/OFF button** on the ATAK/Server console (which is also where the **Cursor-on-Target push targets** now live — UDP / multicast / TCP / `tls://` mutual-TLS).

## 11. Distributed sensing — multi-source DF, same box *or* over a MANET

Ares fuses bearings from sources beyond one SDR:

* **Same server** — register several SDRs under *Devices*; the solver already groups LoBs by frequency *across devices*, so two/three antennas on one Ares produce a multi-sensor Cut/Fix automatically (no extra config — `_solve_and_publish` gathers every LoB in the frequency bucket regardless of which device shot it). The `device_id` passed to the solver is now the *emitter* identity (left empty when unknown) so different sensors looking at the same unidentified emitter at the same frequency *do* fuse.
* **Over a MANET** — `app/core/sdr/mesh.py` (`PeerMesh`): the node opens a WebSocket to each peer Ares node's `/api/v1/sdr/stream`, ingests their `lob` events into the local solver tagged with the peer's node id, and — because peers symmetrically subscribe to *its* stream — the union of every node's bearings is fused into one geolocation picture **on every node**. Runs over any IP-reachable mesh (the same network the CoT multicast rides). Loop-safe: a node never re-ingests a LoB whose origin is itself, dedups by `(origin_node, lob_id)`, and a LoB propagates transitively so a *partial* mesh still converges (full flooding with hop-count is a follow-up). Each `LobEvent` carries `origin_node` / `origin_device`; the node id lives in `data/.node_id` (the hostname is the human label). Peers: `ARES_MESH_PEERS` env, `GET/PUT/POST/DELETE /api/v1/sdr/peers`, `GET /api/v1/sdr/mesh`, or the **"Distributed sensing — mesh peers"** section in the SDR console (live per-peer connection status + LoB-in counts). Verified: 1 local LoB + 2 mesh-peer LoBs (three observer locations) → one fused `kind=fix` within ~5 m of the true emitter; dedup and the loop guard hold.

## 12.5 MANET group chat (+ ATAK GeoChat bridge)

`app/core/chat.py` (`ChatHub`) + `POST /api/v1/chat/send`, `GET /api/v1/chat/{messages,rooms}`, plus a
"Chat" bottom-panel tab. A message broadcasts on `WS /api/v1/sdr/stream` as `{"type":"chat",...}` — so
the peer mesh re-ingests it on every node (dedup by `(origin_node, msg_id)`, loop-safe, hop-count
TTL = 8, transitive through a partial mesh) — *and* goes out as a CoT **GeoChat** (`b-t-f`) to every
configured TAK target, so ATAK/WinTAK clients see and answer the same chat. Inbound GeoChat CoT is
routed back in by the **CoT listener** (`cot.start_cot_listener()` — binds a UDP receiver on each
mcast://`/`udp:// CoT port, joins the multicast group, parses `b-t-f` → `chat_hub.ingest_cot`), closing
the loop *Ares ⇄ Ares ⇄ ATAK* in one conversation. Rooms/channels namespace it (`All` default); a
rolling in-memory buffer per room (also in the WS snapshot for backfill); your callsign persists in
the browser. Verified: local send / mesh relay (hops=1) / dedup / loop-guard / inbound GeoChat-CoT /
GeoChat-XML round-trip all hold.

Also closed this pass: mesh **hop-count/TTL** on LoB forwarding (`LobEvent.hops`, capped at 8), so a
dense mesh doesn't echo bearings forever; a **raster-coverage checkbox** next to the Run button (Coverage
tab); the DF compass shows the latest LoB in **all three reps** (`abs … · rel … · N o'clock`); and
`coverageRaster` persists in the saved UI state.

## 12. Per-pixel raster coverage

`simulation.compute_coverage_raster(req, grid_size≤96)` + `POST /api/v1/simulate/coverage_raster` — one ITS-ITM path (TX→pixel) for every cell of a regular lat/lon grid over ±radius_km, with the full link budget (antenna pattern, atmospheric loss): **even coverage everywhere, no radial thinning at range**. A **"raster" checkbox** sits next to the Run button (Coverage tab). Heavier than the radial sweep (grid_size² ITM evaluations) — pick it when you want a uniform raster, the radial sweep when you want speed.

## 13. Security / trust pass

* **Mesh authentication + message integrity** (`app/core/meshsec.py`) — a shared mesh secret (`ARES_MESH_SECRET`
  env, or `data/.mesh_secret`, generated on first use *only* when a peer is added or you set the env). Every
  inter-node LoB and chat message carries an HMAC-SHA256 over its content (`origin_node`, `id`, lat/lon, az,
  freq, text, `t` — but *not* the mutable `hops`), so a node with a secret rejects unsigned / tampered /
  origin-replayed peer LoBs and chat — a rogue peer (or a spoofed UDP CoT) can't inject bogus bearings that
  bias every node's fixes. A node with *no* secret signs nothing and accepts everything (single-node /
  open-lab back-compat). Peer nodes connect with `?mesh_secret=…`.
* **WebSocket auth** — `WS /api/v1/sdr/stream`, when `ARES_AUTH` is on, requires a valid bearer token
  (`?token=<jwt>` — a UI client) *or* `?mesh_secret=<secret>` (a peer node); otherwise it's closed (4401).
  The web client passes a token from `localStorage['ares.token']` if present.
* **Rate limiting** (`app/core/security.py`) — a per-IP token-bucket middleware on `/api/v1/*` (generous
  default, a tighter bucket for `/simulate/*` and `/packs/download`), `429` when exceeded; WS / `/` / `/health`
  / `/docs` exempt. Tune with `ARES_RATE_LIMIT` / `ARES_RATE_LIMIT_SIM` (`0` disables).
* **Audit log** — `audit(event, **fields)` appends JSON lines to `data/audit.log` (size-rotated) for logins,
  the ATAK on/off toggle, CoT-target changes, compass calibration, mesh-peer add/remove, etc.
* **Posture surfacing** — `GET /api/v1/server/info` now carries `mesh_secret_set` and a `security_warning`
  string when auth is off *and* the server is bound to a non-loopback address (so the UI can warn loudly).
* **Repo + CI** — the v2 line is now a git repo with `.gitignore` (excludes `.venv` / `node_modules` /
  `data/` / secrets / logs) and a GitHub Actions workflow (`.github/workflows/ci.yml`): backend `compileall`
  + app-import + the 53-check validation harness, plus a frontend esbuild bundle-check + Vite build. v1.x
  (`../ares-atak`) is superseded by this branch.
* **ITM mode label fixed** — the LOS↔transhorizon classification now keys on the terrain take-off angles
  (`tha > 0` ⇒ obstructed) / the actual horizon sum (`dla`), not just the smooth-earth horizon (`dlsa`) —
  a deep mid-path ridge is labelled `diffraction`, flat ground `los`, as it should be (the loss magnitude
  was already right; only the label was wrong).

---

## 14. UAS video downlink scanner / decoder  ✅ new (decode chains via external tools / hardware)

`app/core/sdr/uas_video.py` + `app/api/uas_routes.py` add a drone-video pipeline for
SignalHound (BB60/SM200/SM435), Epiq Sidekiq/Matchstiq and Ettus/NI USRP SDRs (via
SoapySDR's per-vendor modules, the vendor SDK when importable, else a synthetic
provider so the UI works offline). What runs **here, with no external tooling**:

* a registry of analog + digital UAS video feed types — FM-analog NTSC/PAL/SECAM,
  legacy VSB/AM, DVB-T, DVB-T2, DVB-S, DVB-S2/S2X, ISDB-T 1-seg, generic COFDM and
  single-carrier-QAM MPEG-TS modems, plus the proprietary/encrypted ones (DJI OcuSync /
  Lightbridge, HDZero, Walksnail, CDL/BE-CDL) flagged **characterize-only**;
* known UAS/FPV video channel plans (900 MHz, 1.2/1.3 GHz, 2.4 GHz, L/S/C/Ku-band ISR,
  the 5.8 GHz raceband, …);
* a PSD-based feed **classifier** — occupied-band detection + bandwidth / flatness /
  channel-plan heuristics; when an IQ provider is wired it adds IQ-domain confirmations
  (an OFDM cyclic-prefix autocorrelation peak, an FM-video line-rate spectral line);
* a full **MISB ST 0601** (STANAG 4609 *UAS Datalink Local Set*) KLV **parser and
  encoder**, with the 16-bit checksum and the standard IMAP range mappings — so a decoded
  feed's metadata becomes a platform position, a sensor line-of-sight, and a ground-
  **footprint polygon** (explicit corner points, or corner offsets about the frame centre,
  or a coarse FOV/slant-range projection), exported as a GeoJSON FeatureCollection
  (`uas_glx` ∈ {platform, frame_center, los, footprint}) and pushed to ATAK as CoT.

**Indicative / needs external tools or hardware:** the actual video demod / TS
extraction is handed off — at runtime, via `$PATH` detection, exactly like the
audio-decode bridge — to `leandvb` (DVB-S/S2), a DVB-T/T2 receiver (`gr-dvbt` /
`dvbt2-blade` / SDRangel headless DATV), `ffmpeg` / `tsp` (TSDuck) for the
MPEG-TS → H.264/H.265 step, and a software analog-TV decoder for FM/VSB composite;
real IQ capture needs SoapySDR built with the SignalHound / Sidekiq / UHD module (or a
wired `IQ_PROVIDER`). When those aren't present the decode session reports exactly which
package would handle the feed, and metadata/footprints are driven by a synthetic MISB
0601 generator so the rest of the chain (map overlay, CoT) is exercised offline.
14 checks for it in the validation harness (`test_uas_video`).

**Digital-video exploitation (PED)** — `app/core/sdr/video_exploit.py` +
`app/api/uas_routes.py`'s `/uas/exploit/*` and `/uas/sessions/{id}/exploit`: the
"processing/exploitation" half, strictly passive (it does **not** touch the aircraft or
its link, break encryption, jam or spoof). Pure-Python: an **MPEG-TS demux** (PAT/PMT
parse, PID classification — H.264/H.265/MPEG-2 video, AAC/AC-3 audio, PCR, the STANAG
4609 / MISB KLV PID — PES reassembly, asynchronous-KLV extraction) → a time-ordered
**MISB ST 0601 metadata track** (each KLV unit → platform position, sensor LOS, footprint
polygon; the track becomes a moving line + footprint polygons in GeoJSON and streams out
as CoT); and a **digital-signal characterizer** — cumulant (`|C40/C42|` → PSK-vs-QAM and
constellation order) + cyclostationary (`|x|⁴` symbol-rate line) + an OFDM
cyclic-prefix-autocorrelation FFT-length / guard-interval estimate + a roll-off estimate —
i.e. "this looks like DVB-T 8 MHz, 8k COFDM, ≈1/4 GI" or "OFDM ~20 MHz — OcuSync-class".
Handed off (PATH-detected) for the rest: `ffmpeg` (H.264/H.265 elementary-stream + keyframe
grabs) and `tesseract` (in-frame burned-in-metadata OCR); the modulation classifier needs
an IQ backend (SoapySDR with the SignalHound / Sidekiq / UHD module, or a wired
`IQ_PROVIDER`) — without it the verdict falls back to the feed registry. A frontend
**"UAS Video" console** (`src/components/Tools/UasVideoPanel.jsx`) scans a band → lists the
detected feeds → decode/characterise → live MISB readout (platform/footprint) with "fly to
platform", "add platform+footprint to map" and "exploit (PED)" buttons. 12 more checks in
the harness (`test_video_exploit`). Total backend routes: 104; harness: 79/79.

**On decrypting OcuSync / Lightbridge / CDL video — Ares does not, and there is no public way to.** Those carry AES- (or COMSEC-) encrypted video under keys negotiated at pairing; a passive intercept cannot recover them. The registry flags them *characterize-only* (detect, fingerprint, DF, geolocate). The open, legitimate way to detect, ID and geolocate a UAS **and its operator** is the drone's unencrypted telemetry beacon — the `remote_id` (ASTM F3411 / FAA Remote ID, WiFi NAN/beacon + BT4/5) and `dji_droneid` feed-registry entries (`decodable: True`, with `decoder_chain` pointing at the published open tooling: `dji_droneid` / a Remote-ID decoder); a full Remote-ID/DroneID demux is the recommended next module. HDZero is reasonably open and DVB-* is unencrypted, so those are decode (not decrypt) targets and already handled.

**Remote ID / DJI DroneID demux** — `app/core/sdr/remote_id.py` + `/uas/rid/*`: a full **ASTM F3411** (FAA Remote ID / ASD-STAN prEN 4709-002 "Open Drone ID") message decoder *and* encoder — Basic ID, Location/Vector, Self-ID, System, Operator ID and Message Pack — so a captured 25-byte message / pack becomes `{serial, ua_type, drone lat/lon/alt/speed/track/vspeed/status, operator lat/lon/alt, operating-area radius, operator_id, …}`; plus a best-effort **DJI DroneID** plaintext parser (the documented v1 layout — serial, drone/home/pilot GPS, velocity; v2's obfuscated tail is identified and handed to the published key + the open `dji_droneid` decoder, which is a fixed published descrambling, not a comms decrypt). It emits GeoJSON (drone point · operator/pilot point · home-point · operating-area circle, tagged `rid_glx`) and CoT (UAS + operator markers), and a synthetic beacon drives the map/CoT path offline. The RF side — recovering the *bytes* from the air (a WiFi-monitor / BT sniffer for F3411; an OFDM receiver for the DJI burst) — is handed off (`rid-decoder` / `dji_droneid` / `tshark` with the OpenDroneID dissector, PATH-detected); `parse_f3411` / `parse_dji_droneid` work on any bytes you already have. `POST /uas/rid/parse` is the fully-working entry (hex in → struct + GeoJSON).

**Auto-detect feed type on decode** — `POST /uas/decode` now takes `feed_type` as *optional*: when omitted (or `"auto"`), Ares scans a narrow window around the tune frequency, picks the overlapping detection (preferring a decodable one, then nearest, then highest confidence), and — if the PSD is quiet — falls back to the catalogued UAS/FPV channel plan at that frequency; the chosen feed and its confidence land on the session as `auto_detected`. The "UAS Video" console exposes this as a "detect & decode @ freq" action and shows the live MISB readout / video pane for the session. 12 more checks in the harness (`test_remote_id`). Total backend routes: 110; harness: 91/91.

**Optional ML / GPU for signal identification.** The feed/modulation classifier is rule-based and interpretable by design (bandwidth · flatness · channel plan · cyclic-prefix autocorrelation · higher-order cumulants · cyclostationary symbol-rate lines), and that stays the default and the explainable fallback. On top of it: (1) a CuPy/CUDA path — `uas_video.gpu_available()` / `_xp()` — for the FFT/correlation DSP, worth enabling for wideband (SM200 ~160 MHz, USRP X310 ~200 MHz), continuous, or multi-channel processing and a no-op otherwise; (2) `uas_video.set_ml_classifier(fn)` — a pluggable ML signal classifier (e.g. a deployment-supplied CNN over the spectrogram / spectral-correlation function, plus torch or onnxruntime) whose verdict is **ensembled** with the heuristic one (it boosts on agreement, adds an alternative on disagreement, never silently overrides) and surfaces as `ml` on each detection / characterization. Especially worth it for the proprietary-OFDM family (OcuSync · Lightbridge · HDZero · Walksnail · generic COFDM modems), which bandwidth/flatness can't fully separate. Both are optional, same as every other Ares dependency — absent, the heuristic path runs as-is. Reported in `status()`; 4 more harness checks. Total backend routes: 110; harness: 95/95.

**Offline IQ for the classifier.** With no real capture layer, `_capture_iq` now returns a *representative synthetic IQ snapshot shaped per the catalogued channel plan* at the requested frequency — FM analog video (constant envelope + a ~15.734 kHz line-rate component) on the 1.2/2.4/5.8 GHz FPV bands, a 2k/8k COFDM burst on the DVB-T-class bands, a pulse-shaped QPSK stream on the satellite-style bands, noise elsewhere — so the IQ-domain stages (CP autocorrelation, cumulants, FM line-rate) and any registered ML classifier still run and produce sensible verdicts offline; everything that consumes it is tagged `synthetic_iq`. (Not a substitute for real RF — that path is unchanged.) Harness: 97/97; routes: 110.

## What's still indicative (and why)

* **Real-time RF / coherent IQ / vocoder audio** — the SoapySDR PSD shim, the JSON-lines IQ ingest →
  interferometry, and the op25/dsd-fme/sdrtrunk/tetra-rx audio bridge are all in place; they *activate*
  when the relevant native driver / decoder is installed (none can be vendored — coherent multi-channel
  capture needs the radio's own DAQ; AMBE/ACELP vocoders are licensed).
* **`App.jsx` is still a ~2700-line monolith** — the zustand stores (`useMapPrefs` / `useUserLayers` /
  `useViewMode`) show the decomposition pattern; applying it everywhere is a clean-up not yet done.
* **ITM bit-for-bit NTIA validation** — the port is structured to the reference and pinned by the harness,
  but verifying it line-for-line against the NTIA `itm.cpp` test vectors needs that C reference compiled.
* **Hardware-in-the-loop** — the SoapySDR→interferometry→fix→CoT chain and the multi-node mesh are
  unit-exercised; an end-to-end test needs a KrakenSDR and ≥2 running instances.
* **The ATAK plugin** — still SDK-blocked (tak.gov SDK + Play/tak.gov publisher accounts); unchanged.

`ARES_AUTH` now defaults to **`auto`**: auth is ON unless the server is bound to a loopback address —
so a networked / field deployment is authenticated out of the box, while localhost dev stays open. Force
it with `ARES_AUTH=true|false`. Frontend has a small Node-native test suite (`frontend/tests/`,
`node --test` — no extra deps) over the pure helpers (polar patterns, LoB maths), wired into CI; the
full component / jsdom test layer is still a follow-up, as is decomposing the ~2700-line `App.jsx`.
* **3-D urban ray tracing** — `ray_tracer.py` is still single-bounce terrain reflection, not a shooting-
  and-bouncing-rays GTD/UTD engine over a 3-D building model (Wireless InSite / WinProp territory).
* **HF foF2** — parameterised, not the CCIR/URSI coefficient maps; wrap `ITURHFPROP`/`VOACAP` for
  P.533-exact (the bridge hook is in `hf.py`).
* **ITM reference-vector validation** — the port is structured to the reference; pinning it against the
  NTIA `itm.cpp` test cases (and fixing the LOS↔transhorizon label edge case) is the remaining step.
* **The ATAK plugin** — still SDK-blocked (needs the tak.gov SDK + Play/tak.gov publisher accounts),
  unchanged from v1.x; see `atak-plugin/README.md`.
