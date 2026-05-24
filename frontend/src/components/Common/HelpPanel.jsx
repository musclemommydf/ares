// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * HelpPanel — Comprehensive documentation modal for Ares
 */
import { X, Radio, Satellite, Zap, Globe, Map, ChevronRight } from 'lucide-react'
import { useState } from 'react'

const SECTIONS = [
  {
    id: 'overview',
    title: 'Overview',
    content: `
Ares models radio signal coverage and point-to-point link analysis using
terrain data, atmospheric conditions, and established propagation models.

Key capabilities:
• Coverage simulation — plots signal strength across a radius around a transmitter
• Point-to-Point (P2P) — analyzes a specific link between TX and RX with full terrain profile
• Multiple transmitters — simulate several transmitters simultaneously on one map
• Real-world terrain — uses SRTM3 elevation data (30m resolution worldwide)
• Atmospheric accuracy — integrates real-time or historical weather data
• Device presets — pre-configured parameters for common tactical, satellite, and UAS radios
    `.trim(),
  },
  {
    id: 'modes',
    title: 'Simulation Modes',
    content: `
COVERAGE MODE
Computes signal strength at thousands of points around the transmitter in a radial pattern.
Results are displayed as a color-coded heatmap on the map:
  Green  → Excellent signal (> −70 dBm)
  Yellow → Good signal     (−70 to −85 dBm)
  Orange → Fair signal     (−85 to −100 dBm)
  Red    → Poor signal     (−100 to −120 dBm)

Parameters:
  Radius (km)         — How far out to compute coverage
  Radials             — Number of angular directions (more = higher resolution, slower)
  Points per radial   — Samples along each radial direction
  Min signal (dBm)    — Points below this threshold are not plotted

POINT-TO-POINT MODE
Analyzes a single link between the transmitter and a chosen receiver location.
Click anywhere on the map to place the receiver, then run simulation.
Results include path loss, received signal strength, SNR, link margin, and fresnel zone analysis.
The Terrain Profile tab shows an elevation cross-section with Fresnel zone overlay.
    `.trim(),
  },
  {
    id: 'transmitter',
    title: 'Transmitter Parameters',
    content: `
LOCATION
  Latitude / Longitude — Position in decimal degrees (drag marker on map to move)
  Height AGL (m/ft)    — Antenna height above ground level
  Altitude MSL (m/ft)  — Ground elevation above mean sea level (affects atmospheric model)

RADIO PARAMETERS
  Frequency (MHz)  — Carrier frequency; determines propagation model and path loss
  TX Power (dBm)   — Transmit power at antenna port
  Device Preset    — Auto-fills frequency, power, antenna type, and polarization

DEVICE PRESETS include:
  US Tactical HF/VHF/UHF: PRC-160, PRC-152A, PRC-148 MBITR, SINCGARS PRC-119, PRC-117G, VRC-90
  MANET radios: Silvus SC-4200/4400, TrellisWare TW-950, MPU-5, Domo TW-370
  Satellite terminals: Iridium 9603, Garmin inReach Mini, SHOUT nano, BGAN
  UAS links: DJI OcuSync 3 (2.4/5.8 GHz), DJI 900 MHz, Skydio X2D, Neros Archer, TrellisWare TW-970
  Russian Tactical: R-168-5UN-2 Akveduk (HF), Azart R-187-P1 (VHF/UHF MANET)
  Chinese PLA: CS/VRC8B (VHF SINCGARS-equivalent), PLA Type-030 JTDRS (MANET)
  NATO/Export: R&S M3AR (multiband manpack)
    `.trim(),
  },
  {
    id: 'devices',
    title: 'Device Reference',
    content: `
US TACTICAL HF
  AN/PRC-160(V) — L3Harris
    Freq: 1.6–60 MHz (HF/VHF) · Power: 20W · Sensitivity: −107 dBm
    ALE, ECCM freq-hopping, embedded COMSEC, backpack/vehicle
    Spec: https://www.l3harris.com/prc-160

  AN/PRC-117G — L3Harris
    Freq: 30–512 MHz · Power: 20W · Sensitivity: −113 dBm
    Wideband manpack, SATCOM-on-the-move capable, SINCGARS/HaveQuick
    Spec: https://www.l3harris.com/falcon-iii-prc-117g

US TACTICAL VHF/UHF
  AN/PRC-152A — L3Harris
    Freq: 30–512 MHz · Power: 5W · Sensitivity: −113 dBm
    Multiband MBITR successor, NSA Type 1 COMSEC, Android ATAK
    Spec: https://www.l3harris.com/prc-152a

  AN/PRC-148 MBITR — Thales
    Freq: 30–512 MHz · Power: 5W · Sensitivity: −113 dBm
    Multiband Inter/Intra Team Radio, waterproof, programmable fills
    Spec: https://www.thalesgroup.com/prc-148

  AN/PRC-119 SINCGARS — ITT/L3Harris
    Freq: 30–87.975 MHz · Power: 10W · Sensitivity: −113 dBm
    Squad net radio, ECCM frequency-hopping, field artillery integration
    Spec: MIL-STD-188-242 compliant

  AN/VRC-90 SINCGARS (vehicular) — ITT
    Freq: 30–87.975 MHz · Power: 50W · Sensitivity: −113 dBm
    Vehicle-mount version, 50W output, remote control unit
    Spec: TM 11-5820-890-10-3

MANET / MESH RADIOS
  Silvus SC-4200 — Silvus Technologies
    Freq: 4.4–5.9 GHz · Power: 1W · Sensitivity: −90 dBm
    2×2 MIMO StreamCaster MANET, IP mesh, ATAK integration
    Spec: https://silvustechnologies.com/products/sc-4200/

  Silvus SC-4400 — Silvus Technologies
    Freq: 4.4–5.9 GHz · Power: 2W · Sensitivity: −90 dBm
    4×4 MIMO, higher throughput, common on UAS platforms
    Spec: https://silvustechnologies.com/products/sc-4400/

  TrellisWare TW-950 TSM — TrellisWare
    Freq: 225–450 MHz · Power: 5W · Sensitivity: −110 dBm
    TSM waveform, AES-256 encryption, 10-hop MANET
    Spec: https://www.trellisware.com/tw-950/

  MPU-5 (Wave Relay) — Persistent Systems
    Freq: 2.3–2.5 GHz · Power: 500 mW · Sensitivity: −90 dBm
    Compact IP MANET, widely integrated on tactical UAS
    Spec: https://www.persistentsystems.com/mpu5/

  Domo TW-370 — Domo Tactical
    Freq: 4.4–5.9 GHz · Power: 1W · Sensitivity: −90 dBm
    MilSpec mesh radio, NATO STANAG encryption
    Spec: https://www.domosystems.com/product/tw-370/

SATELLITE TERMINALS
  Iridium 9603 SBD — Iridium
    Freq: 1616–1626.5 MHz · Power: 1.3W · Sensitivity: −106 dBm
    Short Burst Data (SBD) module, global L-band LEO coverage
    Spec: https://www.iridium.com/products/iridium-9603/

  Garmin inReach Mini — Garmin
    Freq: 1616–1626.5 MHz · Power: 500 mW · Sensitivity: −106 dBm
    2-way Iridium SBD messaging, tracking, weather, SOS
    Spec: https://www.garmin.com/inreach-mini/

  SHOUT nano — NAL Research
    Freq: 1616–1626.5 MHz · Power: 1W · Sensitivity: −106 dBm
    Compact Iridium SBD asset tracker, MIL-STD-810G, IP67
    Spec: https://www.nalresearch.com/shout-nano/

  BGAN Terminal — Inmarsat
    Freq: 1518–1675 MHz · Power: 2W · Sensitivity: −100 dBm
    Broadband GEO SATCOM (492 kbps), portable, global coverage
    Spec: https://www.inmarsat.com/bgan/

UAS DATALINKS
  DJI OcuSync 3 (2.4 GHz) — DJI
    Freq: 2.4–2.4835 GHz · Power: 1.2W · Sensitivity: −100 dBm
    Video/telemetry downlink, Mavic 3 / Mini 4 Pro / FPV series
    Spec: https://www.dji.com/mavic-3

  DJI OcuSync 3 (5.8 GHz) — DJI
    Freq: 5.725–5.850 GHz · Power: 0.4W · Sensitivity: −95 dBm
    Shorter range but less congested, automatic band switching
    Spec: https://www.dji.com/mavic-3

  DJI Mini 4 Pro (900 MHz) — DJI
    Freq: 902–928 MHz · Power: 1W · Sensitivity: −103 dBm
    Long-range mode (US only), ISM band, O4 protocol
    Spec: https://www.dji.com/mini-4-pro

  Skydio X2D — Skydio
    Freq: 902–928 MHz · Power: 1W · Sensitivity: −103 dBm
    Autonomy-focused tactical UAS, FIPS-140-2 encryption, 5.8 GHz fallback
    Spec: https://www.skydio.com/skydio-x2d

  Neros Archer — Neros
    Freq: 300 MHz–4.4 GHz · Power: 2W · Sensitivity: −100 dBm
    Wideband tactical UAS datalink, MIMO, IP67, ITAR-free config available
    Spec: https://neros.tech/archer

  TrellisWare TW-970 UAS — TrellisWare
    Freq: 2.3–2.5 GHz · Power: 1W · Sensitivity: −95 dBm
    Airborne MANET mesh node, TSM waveform, compact SWaP
    Spec: https://www.trellisware.com/tw-970/

RUSSIAN TACTICAL
  R-168-5UN-2 Akveduk — Elaks
    Freq: 1.5–30 MHz · Power: 20W · Sensitivity: −110 dBm
    HF manpack with ALE, replaces R-159/R-143 family; VHF add-on optional
    Export status: Russia domestic only

  Azart R-187-P1 — INTERN / Angstrem
    Freq: 30–512 MHz · Power: 5W · Sensitivity: −113 dBm
    Next-gen VHF/UHF MANET, ATAK-compatible, AES-256, freq-hopping
    Widely reported in service since 2018; export restricted

CHINESE PLA TACTICAL
  CS/VRC8B — CETC (China Electronics Technology Group)
    Freq: 30–87.975 MHz · Power: 10W · Sensitivity: −113 dBm
    PLA standard squad radio, functionally equivalent to SINCGARS
    Export controlled; limited open specifications

  PLA Type-030 JTDRS — CETC
    Freq: 2.3–2.5 GHz · Power: 1W · Sensitivity: −95 dBm
    Joint Tactical Data Radio System, IP MANET mesh waveform
    Reported in service c. 2019; specifications partially classified

NATO / WIDELY-EXPORTED
  R&S M3AR — Rohde & Schwarz
    Freq: 30–512 MHz · Power: 5W · Sensitivity: −113 dBm
    Wideband IP-enabled manpack, NATO STANAG-4204/4637/4691
    Widely exported to NATO and partner nations
    Spec: https://www.rohde-schwarz.com/product/m3ar
    `.trim(),
  },
  {
    id: 'antenna',
    title: 'Antenna Parameters',
    content: `
TX ANTENNA
  Type        — Antenna pattern used for gain calculation:
                  Isotropic, Dipole (half-wave), Yagi, Parabolic dish,
                  Patch, Helical, Phased array, Log periodic, Whip
  Polarization — Vertical, Horizontal, RHCP, LHCP
  Gain (dBi)  — Manual override; leave blank to use pattern-based calculation
  Tilt (°)    — Vertical beam tilt (positive = upward)
  Azimuth (°) — Horizontal beam direction (0° = North)

RX ANTENNA
  Same parameters apply to the receiver antenna
  Height AGL   — Receiver antenna height above ground
  Sensitivity  — Minimum detectable signal level (dBm)
  Noise figure — Receiver noise figure (dB)
  Required SNR — Link margin calculation target (dB)

POLARIZATION LOSS
  Mismatched polarization (e.g., vertical TX, horizontal RX) adds ~20 dB of additional loss.
  RHCP/LHCP mismatch adds ~30 dB. The model accounts for this automatically.
    `.trim(),
  },
  {
    id: 'propagation',
    title: 'Propagation Models',
    content: `
AUTO-SELECT (recommended)
  Automatically picks the best model based on frequency, altitude, and environment.
  Rules:
    • Airborne platform (>150m AGL) + 100 MHz–15.5 GHz → ITU-R P.528
    • < 30 MHz → ITM (Longley-Rice)
    • ≥ 3 GHz → Free-Space Path Loss
    • All other → ITM (Longley-Rice)

ITM — Irregular Terrain Model (Longley-Rice)  [20 MHz–20 GHz]
  Gold standard for ground-to-ground HF/VHF/UHF propagation with terrain.
  Accounts for terrain diffraction, tropospheric scatter, and ground wave.
  Most accurate for distances 1–2000 km.

ITU-R P.528  [100 MHz–15.5 GHz]
  Standard for air-ground and air-air links. Accounts for antenna altitude effects,
  line-of-sight, diffraction, and troposcatter. Required for UAV/airborne scenarios.

FSPL — Free-Space Path Loss
  Baseline model. No terrain or atmospheric effects. Use for quick estimates
  or when TX/RX both have clear line of sight above terrain.

HATA — Okumura-Hata  [150 MHz–1500 MHz]
  Empirical model for urban/suburban/rural environments. Best for mobile
  planning in built-up areas where terrain diffraction dominates less.

OTHER MODELS
  Cost-231, WINNER II, Ericsson — additional empirical models for specific scenarios.
    `.trim(),
  },
  {
    id: 'atmosphere',
    title: 'Atmospheric Parameters',
    content: `
MANUAL PARAMETERS
  Temperature (°C)     — Ambient temperature at transmitter location
  Pressure (hPa)       — Atmospheric pressure (affects refractivity)
  Humidity (%)         — Relative humidity (significant above 10 GHz)
  Rain rate (mm/hr)    — Precipitation rate (affects signal above 10 GHz)
  Visibility (km)      — Fog/mist attenuation
  Refractivity gradient (N-units/km) — Tropospheric bending:
    Standard:   −40 N/km (normal)
    Ducting:    > 0 N/km (anomalous propagation, extended range)
    Sub-refraction: < −79 N/km (reduced range)

AUTO-FILL FROM WEATHER
  Click "Auto-fill from weather at TX location" to fetch real-time conditions
  from Open-Meteo (free, no API key required).
  Use the datetime picker to fetch historical or forecast data.
  The atmospheric preset selector maps conditions to named presets:
    Standard, Tropical, Maritime, Arctic, Desert, Ducting, Super-refraction

ATMOSPHERIC PRESETS
  Standard    — ICAO standard atmosphere (15°C, 1013 hPa, 60% RH)
  Tropical    — High temperature and humidity (35°C, 1010 hPa, 90% RH)
  Maritime    — Moderate coastal conditions (18°C, 1015 hPa, 80% RH)
  Arctic      — Cold dry conditions (−20°C, 1020 hPa, 40% RH)
  Desert      — Hot dry conditions (40°C, 1005 hPa, 15% RH)
  Ducting     — Anomalous propagation conditions (positive refractivity gradient)
    `.trim(),
  },
  {
    id: 'multitx',
    title: 'Multiple Transmitters',
    content: `
ADDING TRANSMITTERS
  Click "+ Add TX" in the sidebar to add a second transmitter.
  Each additional transmitter has:
    • Its own color-coded map marker and coverage overlay
    • Independent frequency, power, antenna, and location settings
    • A label (TX 2, TX 3, etc.) shown in the legend

MANAGING TRANSMITTERS
  Each extra TX appears as a collapsible card in the sidebar.
  Click the × button to remove a transmitter.
  Drag the map marker to reposition.

SIMULATION
  All transmitters are simulated simultaneously (parallel requests).
  The primary TX (TX 1) results are shown in the Results panel.
  All coverage layers are displayed on the map with color coding.
  Note: simulation time scales with the number of transmitters.

FREQUENCY COORDINATION
  When simulating multiple transmitters at the same frequency, interference
  effects are not currently modeled — each transmitter is treated independently.
    `.trim(),
  },
  {
    id: 'maptools',
    title: 'Map Tools',
    content: `
NAVIGATION
  Scroll wheel / pinch — zoom in/out
  Click and drag      — pan the map
  Double-click        — zoom to point

MAP STYLES
  Dark (default) — CARTO dark basemap
  Satellite      — Esri World Imagery
  Topo           — OpenTopoMap with contours

TRANSMITTER MARKER
  Drag the teal circle marker to reposition the transmitter.
  Right-click the marker for additional options.

DISTANCE & HEADING RULER
  Click the ruler icon (📏) in the map toolbar to activate ruler mode.
  Click two points to measure the great-circle distance and bearing between them.
  The result is displayed in the selected unit system (metric or imperial).
  Click the × to exit ruler mode.

COORDINATE DISPLAY
  The map shows coordinates in the selected system (set in Display Settings):
    Lat/Lon DD  — Decimal degrees (e.g., 37.7749° N, 122.4194° W)
    Lat/Lon DMS — Degrees, minutes, seconds
    MGRS        — Military Grid Reference System (e.g., 10SEG7234512345)
    UTM         — Universal Transverse Mercator (e.g., 10N 549880 4179733)

P2P RECEIVER PLACEMENT
  In Point-to-Point mode, click the map to place the receiver.
  Drag the purple marker to adjust the receiver position.
    `.trim(),
  },
  {
    id: 'saveload',
    title: 'Save & Load State',
    content: `
SAVING STATE
  Use File → Save State (or the save button in the sidebar) to export all
  current parameters to a JSON file. This includes:
    • Primary transmitter location, frequency, power, antenna
    • All additional transmitters
    • Receiver parameters
    • Propagation model and settings
    • Atmospheric conditions

LOADING STATE
  Use File → Load State to restore a previously saved configuration.
  The file selector accepts .json files saved by this application.
  After loading, review parameters before running simulation.

USE CASES
  • Transfer scenarios between computers or devices
  • Save multiple configurations for different frequencies or environments
  • Share configurations with team members
  • Archive simulation setups for reproducibility
    `.trim(),
  },
  {
    id: 'display',
    title: 'Display Settings',
    content: `
ACCESS
  Click the globe icon (🌐) in the top toolbar to open Display Settings.

DISTANCE UNITS
  Metric   — Distances in meters (m) and kilometers (km)
  Imperial — Distances in feet (ft) and miles (mi)
  Altitude inputs in the sidebar switch units automatically.

COORDINATE SYSTEMS
  Lat/Lon DD  — Decimal degrees (e.g., 37.7749° N)
  Lat/Lon DMS — Degrees, minutes, seconds (e.g., 37°46'29.64"N)
  MGRS        — Military Grid Reference System (10-digit)
  UTM         — Universal Transverse Mercator with zone
  The coordinate shown below latitude/longitude inputs updates in real-time.
    `.trim(),
  },
  {
    id: 'gpu',
    title: 'GPU & Buildings',
    content: `
GPU ACCELERATION
  When enabled, certain computational stages use CUDA-capable GPUs.
  Requires an NVIDIA GPU with CUDA drivers installed on the backend server.
  Falls back to CPU automatically if no GPU is available.
  The "GPU" badge in the header lights up when GPU mode is active.

OSM BUILDING DATA
  When enabled, OpenStreetMap building footprints are used to add
  diffraction loss through urban areas.
  Most impactful at frequencies above 400 MHz in dense urban environments.
  Increases simulation time by ~20% in urban areas.

TERRAIN RESOLUTION
  SRTM3 — Shuttle Radar Topography Mission, ~90m resolution, global coverage (default)
  SRTM1 — ~30m resolution, available for most land areas
  Higher resolution improves accuracy in hilly terrain but increases computation time.

SPACE WEATHER
  When enabled, fetches real-time Kp index and solar flux (F10.7) from NOAA.
  These affect HF propagation via ionospheric absorption.
  High Kp values (≥5) cause HF degradation; warnings are shown automatically.
    `.trim(),
  },
  {
    id: 'results',
    title: 'Results & Link Budget',
    content: `
RESULTS PANEL
  After coverage simulation:
    • Computation time and propagation model used
    • Coverage area and percentage within radius
    • Number of simulated points and radials

  After P2P simulation:
    • Received signal level (dBm)
    • Path loss (dB) and propagation mode (LOS, diffraction, scatter)
    • Link margin (dB above/below sensitivity threshold)
    • SNR estimate
    • First Fresnel zone clearance

TERRAIN PROFILE
  Shows elevation along the TX–RX path with:
    • Blue shaded terrain
    • Orange/red Fresnel zone envelope
    • Line-of-sight line

LINK BUDGET (shown inline in the Results tab after a P2P run)
  Detailed breakdown of signal budget:
    TX power → cable loss → antenna gain → path loss → atmospheric loss →
    RX antenna gain → received power → receiver sensitivity → link margin

WARNINGS
  Simulation warnings appear as toast notifications and in the Results panel:
    • High Kp index (HF degradation)
    • Ducting conditions
    • Near-zero Fresnel zone clearance
    `.trim(),
  },
]

export default function HelpPanel({ onClose }) {
  const [activeSection, setActiveSection] = useState('overview')
  const section = SECTIONS.find(s => s.id === activeSection)

  return (
    <div
      style={{
        position: 'fixed', inset: 0, zIndex: 9999,
        background: 'rgba(0,0,0,0.7)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
      onClick={onClose}
    >
      <div
        style={{
          background: '#161b22',
          border: '1px solid #30363d',
          borderRadius: 12,
          width: 860,
          maxWidth: '95vw',
          height: 580,
          maxHeight: '90vh',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          boxShadow: '0 24px 64px rgba(0,0,0,0.8)',
        }}
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '14px 20px',
          borderBottom: '1px solid #30363d',
          flexShrink: 0,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Radio size={16} color="#00b4d8" />
            <span style={{ fontWeight: 700, fontSize: 15, color: '#e6edf3' }}>
              Ares — Help
            </span>
          </div>
          <button
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              color: '#8b949e', padding: 4, borderRadius: 4,
              display: 'flex', alignItems: 'center',
            }}
            onClick={onClose}
          >
            <X size={16} />
          </button>
        </div>

        {/* Body */}
        <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
          {/* Sidebar nav */}
          <nav style={{
            width: 200,
            flexShrink: 0,
            borderRight: '1px solid #30363d',
            overflowY: 'auto',
            padding: '8px 0',
          }}>
            {SECTIONS.map(s => (
              <button
                key={s.id}
                onClick={() => setActiveSection(s.id)}
                style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  width: '100%', textAlign: 'left',
                  padding: '8px 16px',
                  background: activeSection === s.id ? '#21262d' : 'none',
                  border: 'none',
                  borderLeft: activeSection === s.id ? '2px solid #00b4d8' : '2px solid transparent',
                  color: activeSection === s.id ? '#e6edf3' : '#8b949e',
                  cursor: 'pointer',
                  fontSize: 13,
                  transition: '120ms',
                }}
              >
                {s.title}
                {activeSection === s.id && <ChevronRight size={12} />}
              </button>
            ))}
          </nav>

          {/* Content */}
          <div style={{ flex: 1, overflowY: 'auto', padding: '20px 24px' }}>
            <h2 style={{ margin: '0 0 16px', fontSize: 16, color: '#e6edf3', fontWeight: 700 }}>
              {section?.title}
            </h2>
            <pre style={{
              margin: 0, whiteSpace: 'pre-wrap', fontFamily: 'inherit',
              fontSize: 13, lineHeight: 1.7,
              color: '#c9d1d9',
            }}>
              {section?.content}
            </pre>
          </div>
        </div>

        {/* Footer */}
        <div style={{
          padding: '10px 20px',
          borderTop: '1px solid #30363d',
          flexShrink: 0,
          fontSize: 11,
          color: '#484f58',
          display: 'flex', justifyContent: 'space-between',
        }}>
          <span>Ares</span>
          <span>ITM / Longley-Rice · SRTM3 Terrain · Open-Meteo Weather</span>
        </div>
      </div>
    </div>
  )
}
