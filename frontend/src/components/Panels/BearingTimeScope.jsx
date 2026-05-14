/**
 * BearingTimeScope — a "B-scope" / bearing-time visualisation for DF.
 *
 * Y axis = bearing (0–360°), X axis = time (newest right), pixel colour = signal
 * power. Persistence makes mobile emitters visible as drifting diagonal traces;
 * stationary emitters appear as horizontal lines. Standard SIGINT display
 * found in CRFS RFEye Site, R&S PR100, krakensdr_doa, and Stone Soup.
 *
 * Renders to <canvas> for speed — at 60 FPS with hundreds of LoBs per second
 * we don't want the React reconciler in the hot path. Updates whenever the
 * `lobs` prop changes; each LoB plots one column-pixel at its bearing.
 */
import { useEffect, useRef, useState } from 'react'

const HEIGHT_DEG = 360
const COL_PERIOD_MS = 200          // each column ≈ 200ms of history

export default function BearingTimeScope({ lobs = [], width = null, height = 140,
                                            windowSec = 60, color = '#06d6a0' }) {
  const wrapRef = useRef(null)
  const canvasRef = useRef(null)
  const historyRef = useRef([])             // [{ t, az, snr_db }, ...]
  // Self-sizing — when `width` isn't provided, measure the parent and redraw.
  const [observedW, setObservedW] = useState(width ?? 0)
  useEffect(() => {
    if (width != null) { setObservedW(width); return }
    const el = wrapRef.current; if (!el) return
    const measure = () => setObservedW(Math.max(120, el.clientWidth))
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [width])

  useEffect(() => {
    historyRef.current = (lobs || []).map(l => ({
      t: (l.t || (Date.now() / 1000)),
      az: ((l.azimuth_deg ?? 0) % 360 + 360) % 360,
      snr_db: typeof l.snr_db === 'number' ? l.snr_db : 0,
      power_dbm: typeof l.power_dbm === 'number' ? l.power_dbm : (l.rssi_dbm ?? -90),
    }))
    draw()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lobs, observedW, height, windowSec, color])

  const draw = () => {
    const c = canvasRef.current; if (!c) return
    const w = Math.max(120, observedW || 240)
    const ctx = c.getContext('2d')
    c.width = w; c.height = height
    ctx.fillStyle = '#0a0e13'
    ctx.fillRect(0, 0, w, height)
    // Background grid
    ctx.strokeStyle = '#1c2530'; ctx.lineWidth = 1
    for (let az = 0; az < 360; az += 30) {
      const y = height * (1 - az / HEIGHT_DEG)
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke()
    }
    ctx.fillStyle = '#5b6b7a'; ctx.font = '9px system-ui,sans-serif'
    for (const a of [0, 90, 180, 270]) {
      const y = height * (1 - a / HEIGHT_DEG)
      ctx.fillText(`${a}°`, 2, y - 2)
    }
    // Plot LoBs as colored dots in the time-bearing window
    const now = Date.now() / 1000
    const t0 = now - windowSec
    for (const p of historyRef.current) {
      if (p.t < t0) continue
      const x = w * (1 - (now - p.t) / windowSec)
      const y = height * (1 - p.az / HEIGHT_DEG)
      // Map power to alpha — strong signals are bright, weak ones faint.
      const norm = Math.max(0, Math.min(1, (p.power_dbm + 100) / 60))
      ctx.fillStyle = withAlpha(color, 0.25 + 0.7 * norm)
      ctx.fillRect(x - 1, y - 1, 3, 3)
    }
    // Frame
    ctx.strokeStyle = '#30363d'
    ctx.strokeRect(0.5, 0.5, w - 1, height - 1)
  }

  return (
    <div ref={wrapRef} style={{ width: '100%' }}>
      <canvas ref={canvasRef} height={height}
        style={{ display: 'block', width: '100%', background: '#0a0e13', borderRadius: 4 }} />
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, color: '#484f58', padding: '2px 4px' }}>
        <span>-{windowSec}s</span><span>bearing (0° top → 360° bottom)</span><span>now</span>
      </div>
    </div>
  )
}

function withAlpha(hex, alpha) {
  if (!hex.startsWith('#')) return hex
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  return `rgba(${r},${g},${b},${alpha.toFixed(3)})`
}
