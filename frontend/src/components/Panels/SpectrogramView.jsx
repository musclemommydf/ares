// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * SpectrogramView — a scrolling waterfall under a SpectrumViewer (Workstream D).
 *
 * Renders a rolling history of PSD frames as a time–frequency image: x = frequency
 * (aligned to the SpectrumViewer's zoom window `view`), y = time (newest row at the
 * top, scrolling down), colour = power mapped from the latest frame's noise floor →
 * peak with a perceptual ramp. Lets the operator see *historical* activity — bursts,
 * hopping, intermittent emitters — that a single-frame spectrum misses.
 */
import { useEffect, useRef } from 'react'

// a compact perceptual ramp (low→high): deep blue → cyan → green → yellow → red
const STOPS = [[8, 12, 60], [18, 60, 130], [20, 160, 130], [120, 200, 60], [240, 200, 40], [240, 60, 30]]
function ramp(t) {
  t = Math.max(0, Math.min(1, t))
  const x = t * (STOPS.length - 1)
  const i = Math.min(STOPS.length - 2, Math.floor(x))
  const f = x - i
  const a = STOPS[i], b = STOPS[i + 1]
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f]
}

export default function SpectrogramView({ history = [], view = { a: 0, b: 1 }, height = 110 }) {
  const cv = useRef(null)
  const wrap = useRef(null)
  useEffect(() => {
    const c = cv.current, w = wrap.current
    if (!c || !w) return
    const W = (c.width = w.clientWidth || 600)
    const H = (c.height = height)
    const g = c.getContext('2d')
    g.fillStyle = '#06090d'; g.fillRect(0, 0, W, H)
    if (!history.length) { g.fillStyle = '#5b6b7a'; g.font = '10px monospace'; g.fillText('waterfall — collecting…', 6, 14); return }
    const last = history[history.length - 1]
    const floor = (typeof last?.noise_floor_dbm === 'number') ? last.noise_floor_dbm : -120
    const peak = (typeof last?.peak_dbm === 'number') ? last.peak_dbm : -40
    const span = Math.max(1, peak - floor)
    const img = g.createImageData(W, H)
    const rows = Math.min(history.length, H)
    for (let r = 0; r < rows; r++) {
      const fr = history[history.length - 1 - r]            // row 0 = newest, at the top
      const psd = fr?.power_dbm
      if (!psd || !psd.length) continue
      const n = psd.length
      const i0 = Math.max(0, Math.floor(view.a * n)), i1 = Math.min(n, Math.ceil(view.b * n))
      const visN = Math.max(2, i1 - i0)
      for (let px = 0; px < W; px++) {
        const idx = i0 + Math.floor(px / W * (visN - 1))
        const [cr, cg, cb] = ramp((psd[Math.min(n - 1, idx)] - floor) / span)
        const o = (r * W + px) * 4
        img.data[o] = cr; img.data[o + 1] = cg; img.data[o + 2] = cb; img.data[o + 3] = 255
      }
    }
    g.putImageData(img, 0, 0)
    // a thin "now" line at the top
    g.strokeStyle = 'rgba(255,255,255,0.25)'; g.beginPath(); g.moveTo(0, 0.5); g.lineTo(W, 0.5); g.stroke()
    g.fillStyle = '#7b8b9a'; g.font = '9px monospace'
    g.fillText(`waterfall · ${rows} frame${rows === 1 ? '' : 's'} · ${floor.toFixed(0)}→${peak.toFixed(0)} dBm`, 4, H - 3)
  }, [history, view, height])
  return <div ref={wrap} style={{ width: '100%', marginTop: 2 }}><canvas ref={cv} style={{ width: '100%', height, display: 'block', borderRadius: 4 }} /></div>
}
