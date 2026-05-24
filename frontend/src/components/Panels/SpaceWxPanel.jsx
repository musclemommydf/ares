// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * The "Space Wx" bottom-panel tab — NOAA SWPC space weather (geomagnetic Kp / F10.7 / storm
 * class, HF radio-blackout, VHF Sporadic-E) plus a quick **HF planning** read-out: a rough
 * daytime/nighttime MUF estimate from the solar flux, a band-by-band condition strip, NVIS
 * guidance, and a short legend. The MUF here is a coarse, location-agnostic estimate from
 * F10.7 + the K-index — for a real circuit use the HF skywave chart / the ITU-R P.533 model.
 */

// Amateur / common HF–6 m bands (label, centre MHz)
const HF_BANDS = [
  ['160 m', 1.9], ['80 m', 3.7], ['60 m', 5.35], ['40 m', 7.1], ['30 m', 10.1],
  ['20 m', 14.2], ['17 m', 18.1], ['15 m', 21.2], ['12 m', 24.9], ['10 m', 28.4], ['6 m', 50.1],
]

// Very rough MUF model: F10.7 ~65 (solar min) → ~14 MHz daytime MUF; ~230 (solar max) → ~38 MHz.
// Night MUF ≈ half of day. A geomagnetic storm (high Kp) depresses both. D-layer absorption from an
// X-ray blackout knocks out the LF/MF/low-HF bands during the day.
function hfPlan(sw) {
  const f107 = Math.max(60, Math.min(300, Number(sw.solar_flux_f107) || 70))
  const kp = Math.max(0, Math.min(9, Number(sw.kp_index) || 2))
  const stormFactor = kp >= 7 ? 0.65 : kp >= 5 ? 0.8 : kp >= 4 ? 0.92 : 1.0
  const mufDay = Math.round(Math.max(10, Math.min(45, (10 + (f107 - 65) * 0.135) * stormFactor)))
  const mufNight = Math.round(Math.max(4, Math.min(18, mufDay * 0.5)))
  // foF2 ≈ MUF / obliquity (~3.2 for a long path); NVIS (near-vertical) usable below foF2
  const fof2Day = Math.max(3, Math.round(mufDay / 3.2))
  const fof2Night = Math.max(2, Math.round(mufNight / 3.2))
  const r = sw.radio_blackout || sw.radio_blackout_class || 'None'
  const blackoutMHz = r === 'R5' ? 25 : r === 'R4' ? 18 : r === 'R3' ? 12 : r === 'R2' ? 8 : r === 'R1' ? 5 : 0
  const status = (freqMHz, mufMHz, isDay) => {
    if (isDay && freqMHz < blackoutMHz) return { s: 'absorbed', c: '#ef4444' }
    if (freqMHz <= mufMHz * 0.85) return { s: 'open', c: '#06d6a0' }
    if (freqMHz <= mufMHz * 1.05) return { s: 'marginal', c: '#f59e0b' }
    return { s: 'closed', c: '#6e7681' }
  }
  return {
    mufDay, mufNight, fof2Day, fof2Night, blackoutMHz,
    bands: HF_BANDS.map(([label, mhz]) => ({
      label, mhz, day: status(mhz, mufDay, true), night: status(mhz, mufNight, false),
    })),
  }
}

const SECTION = { fontSize: 10, fontWeight: 700, color: '#8b949e', marginBottom: 8, textTransform: 'uppercase', letterSpacing: 0.8 }

function BandStrip({ bands, when }) {
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
      {bands.map(b => {
        const st = b[when]
        return (
          <span key={b.label + when} title={`${b.label} (${b.mhz} MHz) — ${st.s}`}
            style={{ fontSize: 10, padding: '2px 7px', borderRadius: 10, border: `1px solid ${st.c}55`,
                     color: st.c, background: `${st.c}14`, whiteSpace: 'nowrap' }}>
            {b.label.replace(' ', '')}
          </span>
        )
      })}
    </div>
  )
}

export default function SpaceWxPanel({ spaceWeather }) {
  const sw = spaceWeather
  const kpColor = sw.kp_index >= 5 ? '#ef4444' : sw.kp_index >= 3 ? '#f59e0b' : '#06d6a0'
  const fetchedAt = sw.timestamp_utc
    ? new Date(sw.timestamp_utc).toLocaleString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short',
      })
    : null
  const hf = hfPlan(sw)
  return (
    <div style={{ padding: '16px 24px', display: 'flex', flexWrap: 'wrap', gap: 22, flex: 1, minHeight: 0, overflowY: 'auto', alignContent: 'flex-start' }}>
      {fetchedAt && (
        <div style={{ width: '100%', fontSize: 10, color: '#484f58', marginBottom: -10 }}>Current as of {fetchedAt} · Source: NOAA SWPC</div>
      )}

      <div>
        <div style={SECTION}>Geomagnetic</div>
        <div style={{ display: 'flex', gap: 4, alignItems: 'center', marginBottom: 6 }}>
          <div style={{ width: 10, height: 10, borderRadius: '50%', background: kpColor, flexShrink: 0 }} />
          <span style={{ fontSize: 13, color: '#e6edf3', fontWeight: 700 }}>Kp {sw.kp_index?.toFixed(1)}</span>
          {sw.storm_class !== 'None' && <span style={{ fontSize: 11, color: '#f59e0b', marginLeft: 6 }}>Storm {sw.storm_class}</span>}
        </div>
        <div style={{ fontSize: 11, color: '#8b949e' }}>F10.7 solar flux: <strong style={{ color: '#e6edf3' }}>{sw.solar_flux_f107?.toFixed(0)} sfu</strong></div>
      </div>

      <div>
        <div style={SECTION}>HF Propagation</div>
        <div style={{ fontSize: 11, color: sw.radio_blackout !== 'None' ? '#ef4444' : '#8b949e', marginBottom: 4 }}>
          Radio blackout: <strong style={{ color: '#e6edf3' }}>{sw.radio_blackout}</strong>
        </div>
        <div style={{ fontSize: 11, color: '#8b949e', maxWidth: 320, lineHeight: 1.5 }}>{sw.hf_propagation}</div>
      </div>

      <div>
        <div style={SECTION}>VHF / Sporadic-E</div>
        <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
          <div style={{ width: 10, height: 10, borderRadius: '50%', background: sw.vhf_sporadic_e_likely ? '#06d6a0' : '#30363d', flexShrink: 0 }} />
          <span style={{ fontSize: 11, color: sw.vhf_sporadic_e_likely ? '#06d6a0' : '#8b949e' }}>
            {sw.vhf_sporadic_e_likely ? 'Sporadic-E possible (6 m / 10 m short-skip)' : 'No Sporadic-E expected'}
          </span>
        </div>
      </div>

      {/* ── HF planning ──────────────────────────────────────────────────── */}
      <div style={{ width: '100%', borderTop: '1px solid #21262d', paddingTop: 14, marginTop: 2 }}>
        <div style={SECTION}>HF Planning — estimated band conditions</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 28 }}>
          <div>
            <div style={{ fontSize: 11, color: '#e6edf3', marginBottom: 4 }}>
              Day &nbsp;<span style={{ color: '#8b949e' }}>MUF ≈</span> <strong>{hf.mufDay} MHz</strong>
              &nbsp;·&nbsp;<span style={{ color: '#8b949e' }}>foF2 ≈</span> {hf.fof2Day} MHz
            </div>
            <BandStrip bands={hf.bands} when="day" />
          </div>
          <div>
            <div style={{ fontSize: 11, color: '#e6edf3', marginBottom: 4 }}>
              Night &nbsp;<span style={{ color: '#8b949e' }}>MUF ≈</span> <strong>{hf.mufNight} MHz</strong>
              &nbsp;·&nbsp;<span style={{ color: '#8b949e' }}>foF2 ≈</span> {hf.fof2Night} MHz
            </div>
            <BandStrip bands={hf.bands} when="night" />
          </div>
        </div>
        <div style={{ fontSize: 10, color: '#6e7681', marginTop: 8, maxWidth: 640, lineHeight: 1.5 }}>
          <span style={{ color: '#06d6a0' }}>● open</span> &nbsp; <span style={{ color: '#f59e0b' }}>● marginal (near MUF)</span> &nbsp;
          <span style={{ color: '#6e7681' }}>● closed (above MUF)</span> &nbsp; <span style={{ color: '#ef4444' }}>● D-layer absorbed</span>.
          {' '}NVIS / short-range (&lt;~400 km): use a frequency <strong>below foF2</strong> — ~{Math.max(2, hf.fof2Day - 1)}–{hf.fof2Day} MHz by day, ~{Math.max(2, hf.fof2Night)} MHz at night
          {hf.blackoutMHz > 0 ? `; an active blackout knocks out HF below ~${hf.blackoutMHz} MHz on the sunlit side` : ''}.
          {' '}This is a coarse F10.7 + Kp estimate — for a specific circuit use the <strong>HF skywave chart</strong> (Tools) or run a P2P with the <strong>Skywave</strong> wave type.
        </div>
      </div>

      <div style={{ width: '100%', borderTop: '1px solid #21262d', paddingTop: 12 }}>
        <div style={SECTION}>What it means</div>
        <div style={{ fontSize: 10, color: '#6e7681', maxWidth: 700, lineHeight: 1.55 }}>
          <strong style={{ color: '#8b949e' }}>F10.7</strong> (solar flux, sfu) drives the F2-layer ionisation → higher flux = higher MUF, the higher bands open. &nbsp;
          <strong style={{ color: '#8b949e' }}>Kp</strong> is the planetary geomagnetic index (0–9); Kp ≥ 5 = a geomagnetic storm → depressed MUF, polar/auroral paths degrade or close, increased absorption. &nbsp;
          <strong style={{ color: '#8b949e' }}>Radio blackout (R1–R5)</strong> is short-wave fadeout from an X-ray flare — sudden, sunlit-hemisphere only, hits the lower HF / MF first and recovers in minutes to hours.
        </div>
      </div>
    </div>
  )
}
