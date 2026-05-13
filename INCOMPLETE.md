# Ares — Incomplete / Deferred Work

This file tracks every request that wasn't fully completed, with the reason. It's
updated whenever I defer or partially deliver something, and read back at the start
of every new prompt so the picture stays honest.

Format per entry:

    ## <short title>
    - **Asked:** <round / date>
    - **What was asked:** ...
    - **What I did do:** ...
    - **What's left:** ...
    - **Why:** ...
    - **What would unblock it:** ...

---

## Raster coverage "emitter moves to the incorrect location"
- **Asked:** round 5 (2026-05).
- **What was asked:** fix the raster-coverage path; the TX marker appeared to be at the wrong
  spot after running raster, and the result "wasn't rendering in accordance with the parameters set."
- **What I did do:** fixed the "not rendering per parameters" symptom — raster GeoJSON is now
  rendered as proper sized rectangles (one per cell, sized to the grid spacing) instead of sparse
  4-px circle markers; default grid_size bumped 56 → 72. Per-cell clutter from an ESA WorldCover
  pack now also applies in the raster path (was radial-only).
- **What's left:** the "emitter moves to the incorrect location" piece.
- **Why:** I can't find a code path that would move the TX marker on a coverage run — the marker
  is `[tx.lat, tx.lon]` always, the raster grid is centered on TX, nothing recenters the map, and
  there's no `setTx` side effect from raster. I couldn't reproduce it from inspection.
- **What would unblock it:** a screenshot of the broken state with the TX coords visible, and the
  parameters that produced it (radius, min_signal, antenna pattern).

## Live SDR hardware verification (SignalHound / USRP / Epiq Sidekiq / RTL-SDR)
- **Asked:** rounds 7–8.
- **What was asked:** native UAS demod + native DF pull IQ straight from the radios on the host,
  with multi-source support and the four named radio families.
- **What I did do:** built the IQ-capture layer (`backend/app/core/sdr/iq_capture.py`) over
  SoapySDR with the per-driver mapping (sh/uhd/sidekiq/rtlsdr + the open ones), wired it as the
  UAS demod's `IQ_PROVIDER` and a new DF `IQ_PROVIDER` in `dsp.py`. Added `POST /df/aoa_live` and
  `GET /df/iq_backend`. Added an SDR-console section showing detected SDRs and a "Solve AoA Live"
  button. **install.sh installs SoapySDR + every open device module by default** on apt-based
  distros (RTL-SDR / UHD / HackRF / Airspy / AirspyHF / Pluto / BladeRF / LimeSDR / MiriSDR) and
  bridges the system `python3-soapysdr` binding into the venv automatically; pass `--no-soapysdr`
  to skip. `scripts/bundle_vendor.sh` mirrors the SoapySDR vendor-module sources for offline builds.
- **What's left:** end-to-end verification with real hardware on a real host.
- **Why:** I can't plug a radio into the runtime here, and the vendor SDKs for SignalHound and
  Epiq Sidekiq are license-gated downloads from the manufacturer — they can't be bundled with
  Ares. The code path is in place; only physical testing remains.
- **What would unblock it:** running on a host with the radio attached and the relevant
  `SoapySDR_*` module installed; then `POST /df/aoa_live` should return a real AoA with
  `iq_source: "iq_provider"` instead of `"synthetic_iq"`.

## Region downloads (imagery / DTED / clutter / OSM / buildings) end-to-end
- **Asked:** round 7.
- **What was asked:** the Layer Manager workflow that downloads all mapping data for a state /
  country / region into a persistent library, with manual-only updates.
- **What I did do:** `core/regions.py` (213-region catalog: US states + Europe + Asia/MENA/Africa/
  Americas/Oceania + giant-country subdivisions), `POST /regions/{code}/estimate` (per-layer GB
  estimate) and `POST /regions/{code}/download` (kicks off the existing `/packs/download`
  pipeline), `POST /packs/{id}/update` (manual re-fetch), right-click map → "Download mapping
  data for this region", Layer Manager UI with the two-step estimate-then-download flow and the
  installed-packs library.
- **What's left:** verifying an actual download end-to-end against real tile / SRTM / Overpass /
  ESA WorldCover servers.
- **Why:** the offline runtime here has no internet to those data sources; the workflow / UI /
  persistence / manual-update / size-estimator are wired and the per-layer math is exact
  (validated with a few sample bboxes), but the actual fetch needs the target host's network.
- **What would unblock it:** running on a host with the internet (or an air-gapped tile mirror
  under `vendor/` / `--offline-bundle`).

---

_Last updated by the assistant at the end of round 10. Newer entries go above this line._
