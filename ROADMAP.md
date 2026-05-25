# Ares — Roadmap

Implementation plan for the README roadmap, grouped into four subsystem tracks that can
progress in parallel. Two starting facts:

- **AD-FMCOMMS5 is already implemented** (`backend/app/core/sdr/drivers/fmcomms5.py`, full
  4-channel coherent MCS driver). The remaining FMCOMMS5 work is the **spectrum sweep** and
  the long-term **18 GHz downconversion**.
- The **pentest-device capabilities stay in scope**, with an "Authorized & lawful use"
  statement (below) replacing the old "strictly passive" framing, and every active/TX path
  gated behind `ARES_AUTHORIZED_ACTIVE=1` (default off) + audit log.

### Roadmap items (from the README)
1. Signalhound, AD-FMCOMMS5 support  *(FMCOMMS5 ✅ done; SignalHound ❌)*
2. Tactical FPGA host compatibility (Matchstiq X40, ZC706 SoC, Jetson)
3. ATAK plugin/server development
4. DSP/DF refinement/correction, Satphone decode, TEMPEST emissions
5. MANET (Silvus/Meshtastic) and Remote-access development
6. Switch from electron to Rust/Tauri
7. Power draw management
8. ML-based signals classification on Jetson Orin, Custom signals database (sigidwiki)
9. Sweep spectrum for FMCOMMS5
10. Malicious signals identification: IMSI-catcher detection (rogue eNodeB), GPS/cell
    jamming/spoofing, TPMS trackers, cellular downgrade attacks, cell/PTT DoS, evil-twin
    APs, deauth floods, rogue AirTag, replay attacks, drone swarms, sub-carrier hides,
    rogue FM/ISM, pentest-device identification
11. Pentest: Flipper Zero / HackRF / H4M / Proxmark3 capabilities
12. 18 GHz downconversion for FMCOMMS5 (long-term)

### Current-state snapshot
| Area | State | Anchor |
|---|---|---|
| SDR driver contract (6 methods) | mature, clean | `backend/app/core/sdr/drivers/base.py:53` |
| FMCOMMS5 driver | done | `…/drivers/fmcomms5.py` |
| SignalHound | absent (SoapySDR `driver=sh` only) | `…/sdr/iq_capture.py:13` |
| Spectrum | per-request snapshots; **no sweep engine** | `…/sdr/manager.py:290` |
| DF solvers + calibration | mature (MUSIC/Capon/ESPRIT/WW + cal) | `…/df/algorithms.py`, `…/df/calibration.py` |
| Classifier (rule-based) + ML scaffold | rule-based done; ML loader ready, no models | `…/sdr/ptt_classifier.py:480`, `…/sdr/ml_signal_classifier.py` |
| Malicious-signal detection | **only** RID anti-spoof exists | `…/df/spoof.py:50` |
| Signals database (sigidwiki) | absent | — |
| Satphone / TEMPEST | absent | — |
| Cellular/WiFi/BLE monitors (data feeds) | working | `…/sdr/cellular/`, `…/sdr/wifi_bt.py` |
| MANET mesh | working (IP/WS, HMAC, gossip) | `…/sdr/mesh.py`, `…/meshsec.py` |
| ATAK plugin + CoT | CoT done; Kotlin plugin uncompiled | `atak-plugin/`, `…/core/cot.py` |
| Electron wrapper | working, ~586 lines to port | `electron/main.js` |
| Pentest-device integration | none | — |

---

## Track A — SDR drivers & hardware hosts

**Roadmap items: 1 (SignalHound), 2 (FPGA hosts), 7 (power), 9 (sweep), 12 (18 GHz).**

Everything here plugs into the existing driver contract — subclass `SdrDriver` and
implement `open/close/set_frequency/set_sample_rate/set_gain/_read_iq_impl`
(`base.py:53`), then register in `…/drivers/__init__.py:36`. No architecture change needed.

- **A1 — SignalHound native driver.** New `…/drivers/signalhound.py` (single-channel
  spectrum monitor: BB60C/D, SM200). Prefer the vendor SDK the installer already wires
  (`scripts/install-signalhound.sh`); fall back to SoapySDR `driver=sh`. Set
  `DriverCapabilities(coherent=False, on_device_fft=True, …)` so the UI shows it as a
  monitor, not a DF array. Register in `__init__.py`.
- **A2 — FMCOMMS5 spectrum sweep (item 9).** New `spectrum_sweep(start_hz, stop_hz,
  step, dwell)` in `…/sdr/manager.py` (extend the `ondemand_spectrum` path at
  `manager.py:290`): retune across the band, Welch-PSD each block (reuse `…/sdr/dsp.py`),
  stitch into a panorama + freq×time waterfall. New API
  `/api/v1/sdr/devices/{id}/spectrum_sweep`; render in the DF/spectrum panel. Generic —
  works for any driver, FMCOMMS5 first.
- **A3 — Tactical FPGA host compatibility (item 2).** (a) *Matchstiq X40 / ZC706*:
  validate the existing `matchstiq.py` + `fmcomms5.py` over libiio-network; add host
  bring-up docs. (b) *Jetson Orin*: add an aarch64/Jetson install path in `install.sh`
  (JetPack wheels), confirm CuPy auto-detect (already present) lights up the GPU paths.
  (c) *FPGA offload*: fill the `on_device_fft` / `on_device_doa` hooks following the UHD
  RFNoC stub pattern (`uhd.py` `fpga_doa()`), starting with on-device FFT.
- **A4 — Power-draw management (item 7).** New `…/core/power.py`: read host power
  telemetry (Jetson `tegrastats`/INA3221 sysfs, x86 RAPL, USB current), add an optional
  `power_w` profile to `DriverCapabilities`, and tie an idle-throttle into the existing
  idle-aware DF loop. New `/api/v1/power` + a small UI widget.
- **A5 — 18 GHz downconversion (item 12, hardware-blocked, long-term).** No silicon yet,
  so ship the *software seam*: a `Downconverter` band-plan abstraction (external-LO
  offset + image handling) the tuner consults, so a future block-downconverter is just a
  frequency-translation wrapper over an existing driver. Stub + interface only; mark
  blocked-pending-hardware.

---

## Track B — DSP / DF / classification

**Roadmap items: 4 (DSP/DF refine, satphone, TEMPEST), 8 (ML classifier + signals DB).**

- **B1 — DSP/DF refinement & correction (item 4a).** In `…/df/algorithms.py`: add
  **forward-backward spatial smoothing** (decorrelate coherent multipath before MUSIC),
  **auto-tuned diagonal loading** for Capon, and **mutual-coupling compensation** in the
  steering-vector build. In `…/df/calibration.py`: extend beacon/noise cal with
  antenna-pattern-aware steering. Close the documented gap by running **ITM reference
  validation** against the NTIA test CSV (`docs/Ares.md` flags this pending).
- **B2 — Satphone decode (item 4b).** Passive decoders routed exactly like the PTT
  decoder chain (`ptt_classifier` → external tool): **Iridium** (gr-iridium /
  iridium-toolkit), **Inmarsat STD-C / AERO** (JAERO / scytale-C). New
  `…/sdr/satcom/` module + installer opt-in flags mirroring `--with-op25`. Strictly
  passive, no decryption.
- **B3 — TEMPEST emissions (item 4c).** Van-Eck-style reconstruction of a display from
  unintended EM emissions. Reuse the **UAS video raster pipeline** (`…/sdr/uas_video.py`
  — H-sync PLL, V-sync, deinterlace already exist) fed from wideband IQ at a chosen
  harmonic; new "TEMPEST" capture mode + screen-geometry search. Research/lab feature.
- **B4 — ML signal classification on Jetson Orin (item 8a).** The loader already supports
  ONNX/TorchScript/TFLite (`…/sdr/ml_signal_classifier.py`) and a 14-feature vector.
  Remaining work: a **capture→label→train** pipeline, train a model on Ares-captured IQ,
  export ONNX, **ensemble it with the rule-based classifier** (`set_ml_classifier` hook),
  and deploy on **Jetson Orin via TensorRT / onnxruntime-gpu**. Keep the rule-based path
  as the offline fallback (DSP stays local/in-process — no cloud).
- **B5 — Custom signals database / sigidwiki (item 8b).** New signal-fingerprint DB
  (freq range, bandwidth, modulation family, symbol rate, ACF/cyclostationary signature)
  stored in **SQLite via the existing `store.py`**, seeded from sigidwiki metadata. A
  matcher takes the classifier's feature vector and returns ranked candidates; surfaced
  in the classifier/Targets UI. This DB is also the lookup backbone for several Track-C
  detectors.

---

## Track C — Detection & security (the big track)

**Roadmap items: 10 (malicious-signal identification) + 11 (pentest devices).**

These all share one shape, so build the framework once, then add detectors. **Mirror the
existing `…/df/spoof.py` pattern**: a detector is a pure-ish function that consumes
already-collected observations and returns annotation dicts with a `verdict`
(`agree` / `spoof_candidate` / `threat`). The data feeds already exist — the cellular/
WiFi/BLE monitors push events through `CellularSession.emit()` (`…/cellular/session.py:69`)
into the targets tracker; spectrum comes from Track A's sweep.

- **C0 — Detection framework.** New package `…/core/detect/` with: a `Detector` base +
  **registry** (same idea as the SDR driver registry), a **baseline/anomaly engine**
  (per-band noise-floor & emitter baselines so "novel/rogue" is measurable), and a
  background **detection loop** (idle-aware, like the DF loop). New router
  `/api/v1/detect` (active threats, ack/dismiss, history) and a new frontend
  **"Threats" panel** (register in `frontend/src/components/Panels/BottomPanelTabs.jsx`
  + dispatch in `BottomPanelContent.jsx`). Threats also emit to ATAK via the existing
  `cot.py` as alert points.
- **C1 — Cellular threats.** *IMSI-catcher / rogue eNodeB*: flag new/abnormal cells
  (unknown CID at a known TAC/LAC, LAC-change reselection bait, missing neighbor lists,
  abnormal SIB/cell-barring, 2G-only forcing) from the GSM/LTE/NR observations. *Cellular
  downgrade*: detect a tracked device/cell dropping generation (5G→3G→2G). *Cell DoS /
  jamming*: control-channel loss + noise-floor rise in cellular bands.
- **C2 — GNSS threats.** *GPS jamming*: noise-floor rise at L1 1575.42 MHz (Track-A
  sweep) + gpsd fix loss. *GPS spoofing*: implausible position/time jumps, abnormally
  uniform C/N0. Consumes the existing GPS plumbing.
- **C3 — WiFi/BLE threats.** *Evil-twin AP*: same SSID with a new/anomalous BSSID
  (channel/security/OUI mismatch). *Deauth flood*: 802.11 management-frame rate spike
  (mgmt-frame capture via the WiFi monitor). *Rogue AirTag / unwanted tracker*: Find My
  (0x004C) / Tile / SmartTag BLE adverts that **persist across your movement** — reuses
  the targets tracker's per-identifier history.
- **C4 — Sub-GHz / ISM threats.** *TPMS trackers* (315/433 MHz — decode sensor IDs via
  rtl_433-style demod, flag a sensor ID that follows you). *Replay attacks* (identical
  ISM burst repeated — burst autocorrelation). *Rogue FM/ISM* (emitter absent from the
  Track-B baseline / signals DB). *Sub-carrier hides* (cyclostationary/spectral-
  correlation anomaly inside a host signal).
- **C5 — Aggregate & device threats.** *Drone swarms*: cluster correlated UAS/RID
  emitters + video downlinks in frequency/space. *Pentest-device identification*:
  fingerprint HackRF/USRP/bladeRF/Flipper transmissions (LO leakage, clock spurs,
  characteristic sweep patterns) against the signals DB.
- **C6 — Pentest-device capabilities (item 11, active).** Bridges for **Flipper Zero**
  (USB/BLE CLI), **Proxmark3** (UART), **HackRF/H4M** (TX). Two parts: *detect/identify*
  these devices in the environment (passive, C5) and *operate* them for authorized
  testing (active). **All active/TX paths are gated** behind `ARES_AUTHORIZED_ACTIVE=1`
  + audit log (see "Authorized & lawful use") and default OFF.

---

## Track D — Platform, integration & UX

**Roadmap items: 3 (ATAK plugin/server), 5 (MANET + remote), 6 (Tauri).**

- **D1 — ATAK plugin/server (item 3).** CoT encode/receive is done (`…/core/cot.py`).
  Remaining: **compile & sign the Kotlin plugin** (needs the tak.gov SDK + JDK/Android
  SDK), add a **CI matrix** for ATAK SDK 5.3/5.4/5.5, wire the radial-menu actions
  ("Add LoB from here") to `MenuMapAdapter`, and optionally a TAK-Server-side relay.
  Files under `atak-plugin/`.
- **D2 — MANET Silvus/Meshtastic + remote (item 5).** `mesh.py` already gossips
  HMAC-signed LoBs/chat over IP/WebSocket. Add **transport adapters**: *Meshtastic*
  (serial/BLE/MQTT — needs a compact binary LoB/chat encoding for the tiny link budget)
  and *Silvus StreamCaster* (IP radio — runs over the existing IP mesh; add the Silvus
  API for neighbor/link-state). Harden **remote-access** auth (`docs/REMOTE.md`).
- **D3 — Electron → Tauri (item 6).** Port `electron/main.js` (~586 lines) to Tauri/Rust:
  backend subprocess spawn, static file serving, `/api` proxy, IPC commands, `remote.json`,
  splash, first-run venv/npm. **Hardest part = the WebSocket upgrade/forward** for
  `/api/v1/sdr/stream` & audio (Tokio-tungstenite). Keep Electron shipping until Tauri
  reaches parity; ~40 MB vs ~150 MB bundle is the payoff.
- **D4 — Selective Rust oxidation (PyO3), not a rewrite.** Keep Python as the
  orchestration / API / ecosystem layer (numpy/scipy, SoapySDR, pyadi-iio, gr-gsm,
  ONNX, the external decoders — the breadth that makes Ares possible lives here). Move
  *only* the paths below into Rust via PyO3, **each gated on a profiling trigger** so we
  oxidize on evidence, not faith. `docs/Ares.md` already anticipates this with its
  "PyO3/Rust accelerator fallback" for ITM.

  | Candidate | File(s) | Why Rust | Profiling trigger to justify it |
  |---|---|---|---|
  | Real-time multi-channel IQ pipeline | `…/sdr/live_df.py`, `…/sdr/manager.py` | GIL-bound when driving several radios at once; needs predictable, GC-free latency | dropped-frame rate climbs (see `sdr_health`) or CPU saturates with ≥2 coherent radios |
  | Multi-VFO channelizer / squelch | `…/sdr/` (VFO carve-out) | many small per-frame ops dominated by Python glue, not numpy C calls | per-VFO overhead > ~10–15% of frame budget when running many narrowband channels |
  | DBPSK NIC modem (TAP/TUN over RF) | `…/sdr/tap_nic` | tight real-time mod/demod loop; throughput- and latency-sensitive | link throughput capped by CPU rather than the radio/SNR |
  | ITM inner loop | `…/core/propagation/` | already flagged as a Rust-accelerator candidate; embarrassingly parallel per-pixel | per-pixel raster / Monte-Carlo dominates wall-clock **and** no CuPy GPU present |

  **Explicitly NOT oxidized:** the DF linear algebra (`df/algorithms.py` — already
  LAPACK/FFTW under numpy; Rust likely *slower* unless bound to the same BLAS), and the
  external decoders (dsd-fme / gr-gsm / sniffers — they're separate C/C++ processes, so
  rewriting our Python glue wouldn't remove their memory-safety risk anyway). Note also
  that Python 3.13+ free-threading erodes the GIL argument over time — re-check the IQ
  candidate against a no-GIL interpreter before committing to the port.

---

## Authorized & lawful use (statement rewrite)

The old "strictly passive / lawful-passive" framing no longer matches a build that ships
active pentest tooling. Replace it in **`README.md`** (intro + License section),
**`CONTRIBUTING.md`** (house rules), and **`SECURITY.md`** with the notice below, and
**code-gate every active/TX path** behind `ARES_AUTHORIZED_ACTIVE=1` (default off) + audit
log:

> **Authorized & lawful use only.** Ares includes active RF and pentest-tool features
> (e.g. HackRF/H4M transmit, Flipper Zero, Proxmark3). These are provided **solely for
> lawful, authorized use** — security research, training, CTFs, and engagements you have
> explicit written authorization to conduct. Transmitting on regulated spectrum,
> intercepting communications you are not authorized to access, or interfering with
> networks or devices may be illegal in your jurisdiction. You are solely responsible for
> operating Ares within applicable law (e.g. U.S. CFAA, the Wiretap Act, FCC Part 15/97,
> and your local equivalents) and within the scope of any authorization. The passive
> monitoring features remain passive and perform no decryption; the active features are
> disabled by default and must not be enabled outside an authorized scope.

---

## Suggested build order (within the parallel tracks)

1. **Track C framework (C0)** + **Track A sweep (A2)** first — they unlock the most
   downstream value (detectors need the framework; many detectors need the sweep).
2. Then the **highest-signal detectors** that ride existing feeds with zero new hardware:
   C1 (cellular/IMSI-catcher), C3 (evil-twin/deauth/AirTag), C2 (GPS).
3. In parallel: **B5 signals DB** (feeds C4/C5), **A1 SignalHound**, **B1 DSP refinement**.
4. Then the heavier lifts: **B4 ML on Jetson**, **D3 Tauri**, **D2 Meshtastic**,
   **B2 satphone**, **C6 pentest devices** (gated).
5. Last / blocked: **D1 ATAK compile** (SDK-blocked), **A5 18 GHz** (hardware-blocked),
   **B3 TEMPEST** (research).
6. **D4 oxidation is not scheduled** — port a path only when its profiling trigger fires.
   The one exception is the Tauri shell (D3), which is worth doing on its own merits.

---

## Verification

- **Per detector / DSP change:** extend the existing validation harness style —
  `backend/tests/test_validation.py` (115 checks today), plus a new `test_detect_*.py`
  feeding synthetic IMSI-catcher / deauth / GPS-spoof event streams and asserting
  `verdict`. Run `cd backend && python -m tests.test_validation`.
- **New drivers (SignalHound, sweep):** exercise via the **synthetic driver** path
  (offline, no hardware) — the registry already supports it. Confirm
  `/api/v1/sdr/devices/{id}/spectrum_sweep` returns a stitched panorama.
- **Frontend panels (Threats, power, sweep waterfall):** `cd frontend && node --test tests/`,
  then run the app and eyeball — start backend on **127.0.0.1** via `./start-web.sh`,
  open the new panels.
- **End-to-end smoke:** with the synthetic array, inject a spoofed Remote-ID + a rogue
  cell event, confirm a Threat appears in the panel **and** is emitted to ATAK via CoT.
- **CI:** `.github/workflows/ci.yml` already runs backend + frontend on every push — add
  the new tests there and keep the SPDX-header check green.

---

## Appendix — learning the stack (for new contributors)

The three topics that together explain ~90% of the codebase, each mapped to files you can
open and read.

### 1. Programming language → **Python**
The entire backend — DSP, DF solvers, propagation, detection, the FastAPI API — is Python
+ NumPy/SciPy. *Read alongside:* `backend/app/core/df/algorithms.py`, `…/geolocation.py`.
- [Python Tutorial](https://docs.python.org/3/tutorial/) + *Python Crash Course* (Matthes).
- [SciPy Lecture Notes](https://lectures.scientific-python.org/) and the
  [NumPy basics](https://numpy.org/doc/stable/user/absolute_beginners.html).
- [FastAPI docs](https://fastapi.tiangolo.com/tutorial/) for `backend/app/api/`.
- [Real Python](https://realpython.com/) for practice.

### 2. Math/science → **Digital Signal Processing for SDR** (IQ, FFT, and the linear algebra of array DF)
Complex-baseband (IQ) signals, the DFT/FFT, filtering, and the covariance-matrix +
eigendecomposition behind MUSIC/MVDR. *Read alongside:* `…/sdr/dsp.py`,
`…/df/algorithms.py`, `…/sdr/ptt_classifier.py`.
- **Start here:** [**PySDR: A Guide to SDR and DSP using Python**](https://pysdr.org/) —
  free, Python, covers IQ/FFT/filtering **and** DOA/beamforming.
- [*The Scientist and Engineer's Guide to DSP*](https://www.dspguide.com/) (free);
  *Understanding Digital Signal Processing* (Richard Lyons).
- Linear algebra: 3Blue1Brown
  [*Essence of Linear Algebra*](https://www.3blue1brown.com/topics/linear-algebra), then
  [MIT 18.06](https://ocw.mit.edu/courses/18-06-linear-algebra-spring-2010/) (Strang).
- Advanced/canonical: H. L. Van Trees, *Optimum Array Processing* (MUSIC, MVDR, CRLB).

### 3. Pentest/cyber → **Wireless & RF security (SDR-based)**
This *is* Track C — IMSI catchers, jamming/spoofing, evil-twin/deauth, replay, TPMS,
AirTags. *Read alongside:* `…/sdr/cellular/`, `…/sdr/wifi_bt.py`, `…/df/spoof.py`, and the
new `…/core/detect/`.
- **Start here (maps ~1:1 to Track C):** *Inside Radio: An Attack and Defense Guide*
  (Yang & Huang, Springer) — GPS spoofing, TPMS, keyfob replay, ISM attacks.
- Hands-on (free): Great Scott Gadgets
  [*SDR with HackRF*](https://greatscottgadgets.com/sdr/) (Ossmann);
  [RTL-SDR blog](https://www.rtl-sdr.com/).
- Cellular: [Osmocom](https://osmocom.org/) + `gr-gsm`; SRLabs
  [SnoopSnitch / IMSI-catcher research](https://opensource.srlabs.de/) (basis for C1).
- WiFi/BLE: [aircrack-ng](https://www.aircrack-ng.org/); Mike Ryan's `crackle`;
  *Practical IoT Hacking* (No Starch).
- Signal ID: [sigidwiki.com](https://www.sigidwiki.com/) — the source for the Track-B DB.
- Course (paid): SANS SEC617 — Wireless Penetration Testing.

### 4. Systems language → **Rust** (the platform layer Ares is moving toward)
Track D replaces the Electron shell with **Tauri** (Rust), and the D4 plan oxidizes the
real-time hot paths into Rust via **PyO3**. You don't need Rust to read most of Ares, but
you need it for the platform/perf work. *Read alongside:* `src-tauri/` (the new desktop
shell) vs `electron/main.js` (what it replaces); later the PyO3 candidates in
`…/sdr/live_df.py`.
- **Start here:** [*The Rust Programming Language* — "the Book"](https://doc.rust-lang.org/book/) (free, official).
- Practice: [Rustlings](https://github.com/rust-lang/rustlings) (guided exercises) +
  [Rust by Example](https://doc.rust-lang.org/rust-by-example/).
- Reference depth: *Programming Rust* (Blandy, Orendorff & Tindall, O'Reilly).
- For Ares specifically: [Tauri v2 docs](https://v2.tauri.app/) (the desktop shell, D3) and
  the [PyO3 user guide](https://pyo3.rs/) (calling Rust from Python — the D4 oxidation path).
- Concurrency, which is *why* Rust helps the IQ pipeline: the
  [Tokio tutorial](https://tokio.rs/tokio/tutorial) and *Rust Atomics and Locks*
  (Mara Bos — free online).

**A path through it:** Python → PySDR (ties Python to DSP) → *Inside Radio* (ties DSP to
the threats), then Rust (the Book → Tauri/PyO3) once you reach Track D. At each step, open
the Ares file listed under that topic and read the code next to the theory.

---

## Deferred work (carried over from the old INCOMPLETE.md tracker)

These predate the roadmap above and are blocked on real hardware / network, not design.

### Raster coverage "emitter moves to the incorrect location"
- **What was asked:** fix the raster-coverage path; the TX marker appeared at the wrong
  spot after running raster, and the result "wasn't rendering in accordance with the
  parameters set."
- **What was done:** fixed the "not rendering per parameters" symptom — raster GeoJSON now
  renders as proper sized rectangles (one per cell, sized to grid spacing) instead of
  sparse 4-px circles; default grid_size bumped 56 → 72; per-cell ESA WorldCover clutter
  now applies in the raster path (was radial-only).
- **What's left:** the "emitter moves to the incorrect location" piece.
- **Why:** no code path found that moves the TX marker on a coverage run — marker is
  `[tx.lat, tx.lon]` always, the grid is centered on TX, nothing recenters the map, no
  `setTx` side effect from raster. Not reproducible from inspection.
- **What would unblock it:** a screenshot of the broken state with TX coords visible + the
  parameters that produced it (radius, min_signal, antenna pattern).

### Live SDR hardware verification (SignalHound / USRP / Epiq Sidekiq / RTL-SDR)
- **What was asked:** native UAS demod + native DF pulling IQ straight from the radios on
  the host, multi-source, with the four named radio families.
- **What was done:** IQ-capture layer (`backend/app/core/sdr/iq_capture.py`) over SoapySDR
  with per-driver mapping (sh/uhd/sidekiq/rtlsdr + open ones), wired as the UAS demod's
  `IQ_PROVIDER` and a new DF `IQ_PROVIDER` in `dsp.py`; `POST /df/aoa_live`,
  `GET /df/iq_backend`; SDR-console section + "Solve AoA Live" button. `install.sh`
  installs SoapySDR + every open device module by default and bridges the system
  `python3-soapysdr` into the venv (`--no-soapysdr` to skip);
  `scripts/bundle_vendor.sh` mirrors vendor-module sources for offline builds.
- **What's left:** end-to-end verification with real hardware on a real host.
- **Why:** no radio can be attached to the runtime here; SignalHound/Sidekiq vendor SDKs
  are license-gated downloads. The code path is in place; only physical testing remains.
- **What would unblock it:** a host with the radio attached and the relevant `SoapySDR_*`
  module installed; then `POST /df/aoa_live` should return a real AoA with
  `iq_source: "iq_provider"` instead of `"synthetic_iq"`.

### Region downloads (imagery / DTED / clutter / OSM / buildings) end-to-end
- **What was asked:** the Layer Manager workflow that downloads all mapping data for a
  state / country / region into a persistent library, manual-only updates.
- **What was done:** `core/regions.py` (213-region catalog), `POST /regions/{code}/estimate`
  (per-layer GB estimate) + `POST /regions/{code}/download` (kicks the existing
  `/packs/download` pipeline), `POST /packs/{id}/update` (manual re-fetch), right-click map
  → "Download mapping data for this region", Layer Manager UI with the two-step
  estimate-then-download flow + installed-packs library.
- **What's left:** verifying an actual download end-to-end against real tile / SRTM /
  Overpass / ESA WorldCover servers.
- **Why:** the offline runtime here has no internet to those sources; the workflow / UI /
  persistence / size-estimator are wired and the per-layer math is exact (validated with
  sample bboxes), but the fetch needs the target host's network.
- **What would unblock it:** a host with internet (or an air-gapped tile mirror under
  `vendor/` / `--offline-bundle`).
