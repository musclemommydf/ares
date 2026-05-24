// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * SpectrumViewer — a single SDR channel's power spectrum (Workstream D / DF panel).
 *
 * Renders a PSD frame `{power_dbm:[], center_hz, span_hz, noise_floor_dbm, peak_hz,
 * peak_dbm, df_threshold_dbm}` on a canvas. Scroll-wheel zooms the frequency axis
 * about the cursor (and pans on drag); the **y axis never moves** — it's pinned to
 * [noise_floor − pad, peak + pad] so the noise floor and the strongest signal are
 * always visible. Draws the noise-floor line, the DF threshold line, the peak
 * marker, and a tuner cursor at `tuneHz`; clicking sets the DF frequency.
 *
 * NB: until a SoapySDR / rtl-sdr / krakensdr-DAQ capture layer is wired the frames
 * are synthetic (`frame.source === "synthetic"`) — the viewer behaves identically.
 */
import { useEffect, useRef, useState } from 'react'
import SpectrogramView from './SpectrogramView'

export default function SpectrumViewer({ frame, label = '', tuneHz = null, onTune, height = 150, history = [] }) {
  const cv = useRef(null)
  const wrap = useRef(null)
  // visible frequency window, as a fraction [0,1] of the full span (1 = whole span)
  const [view, setView] = useState({ a: 0, b: 1 })
  const [showWaterfall, setShowWaterfall] = useState(false)
  const dragRef = useRef(null)

  useEffect(() => {
    const c = cv.current, w = wrap.current
    if (!c || !w || !frame || !Array.isArray(frame.power_dbm)) return
    const W = (c.width = w.clientWidth || 600)
    const H = (c.height = height)
    const g = c.getContext('2d')
    g.fillStyle = '#0a0e13'; g.fillRect(0, 0, W, H)
    const psd = frame.power_dbm
    const n = psd.length
    const f0 = frame.center_hz - frame.span_hz / 2
    const i0 = Math.max(0, Math.floor(view.a * n)), i1 = Math.min(n, Math.ceil(view.b * n))
    const visN = Math.max(2, i1 - i0)
    // y axis: fixed to [floor - 8, peak + 8] over the WHOLE frame (never moves on zoom)
    const floor = (typeof frame.noise_floor_dbm === 'number') ? frame.noise_floor_dbm
      : Math.min(...psd)
    const peak = (typeof frame.peak_dbm === 'number') ? frame.peak_dbm : Math.max(...psd)
    const yLo = Math.floor(floor) - 8, yHi = Math.ceil(peak) + 8
    const yToPx = (db) => H - ((db - yLo) / Math.max(1, yHi - yLo)) * H
    const xToPx = (i) => ((i - i0) / (visN - 1)) * W
    const idxToHz = (i) => f0 + (i / Math.max(1, n - 1)) * frame.span_hz
    const hzToX = (hz) => xToPx((hz - f0) / frame.span_hz * (n - 1))
    // grid + y labels
    g.strokeStyle = '#1c2530'; g.fillStyle = '#5b6b7a'; g.font = '9px monospace'
    for (let d = Math.ceil(yLo / 10) * 10; d < yHi; d += 10) {
      const y = yToPx(d); g.beginPath(); g.moveTo(0, y); g.lineTo(W, y); g.stroke()
      g.fillText(`${d}`, 2, y - 1)
    }
    // threshold (active-signal) line
    const thr = frame.df_threshold_dbm
    if (typeof thr === 'number' && thr > yLo && thr < yHi) {
      g.strokeStyle = '#f59e0b'; g.setLineDash([4, 3]); g.beginPath()
      g.moveTo(0, yToPx(thr)); g.lineTo(W, yToPx(thr)); g.stroke(); g.setLineDash([])
      g.fillStyle = '#f59e0b'; g.fillText(`thr ${thr} dBm`, W - 78, yToPx(thr) - 2)
    }
    // noise-floor line
    g.strokeStyle = '#2dd4bf'; g.setLineDash([2, 4]); g.beginPath()
    g.moveTo(0, yToPx(floor)); g.lineTo(W, yToPx(floor)); g.stroke(); g.setLineDash([])
    // the PSD trace + a filled area under it
    g.beginPath(); g.moveTo(0, H)
    for (let i = i0; i < i1; i++) g.lineTo(xToPx(i), yToPx(psd[i]))
    g.lineTo(W, H); g.closePath()
    g.fillStyle = 'rgba(56,189,248,0.12)'; g.fill()
    g.strokeStyle = '#38bdf8'; g.lineWidth = 1; g.beginPath()
    for (let i = i0; i < i1; i++) { const x = xToPx(i), y = yToPx(psd[i]); i === i0 ? g.moveTo(x, y) : g.lineTo(x, y) }
    g.stroke()
    // peak marker
    if (typeof frame.peak_hz === 'number') {
      const px = hzToX(frame.peak_hz)
      if (px >= 0 && px <= W) {
        g.fillStyle = '#fbbf24'; g.beginPath(); g.arc(px, yToPx(peak), 3, 0, 2 * Math.PI); g.fill()
        g.fillText(`${(frame.peak_hz / 1e6).toFixed(4)} MHz · ${peak} dBm`, Math.min(W - 130, px + 4), yToPx(peak) - 4)
      }
    }
    // tuner cursor
    if (typeof tuneHz === 'number') {
      const tx = hzToX(tuneHz)
      if (tx >= 0 && tx <= W) {
        g.strokeStyle = '#a78bfa'; g.lineWidth = 1.5; g.beginPath(); g.moveTo(tx, 0); g.lineTo(tx, H); g.stroke()
        g.fillStyle = '#a78bfa'; g.fillText('▼ DF', tx + 2, 10)
      }
    }
    // x labels (visible window edges + centre)
    g.fillStyle = '#7b8b9a'; g.font = '9px monospace'
    g.fillText(`${(idxToHz(i0) / 1e6).toFixed(4)}`, 2, H - 2)
    const cTxt = `${(idxToHz((i0 + i1) / 2) / 1e6).toFixed(4)} MHz`
    g.fillText(cTxt, W / 2 - g.measureText(cTxt).width / 2, H - 2)
    const rTxt = `${(idxToHz(i1 - 1) / 1e6).toFixed(4)}`
    g.fillText(rTxt, W - g.measureText(rTxt).width - 2, H - 2)
    if (label) { g.fillStyle = '#9ca3af'; g.fillText(label, W - g.measureText(label).width - 2, 10) }
  }, [frame, view, tuneHz, height])

  // scroll = zoom about the cursor; drag = pan; click = tune
  const onWheel = (e) => {
    e.preventDefault()
    const r = e.currentTarget.getBoundingClientRect()
    const fx = view.a + (e.clientX - r.left) / r.width * (view.b - view.a)   // fraction-of-span under cursor
    const k = e.deltaY > 0 ? 1.25 : 0.8
    let a = fx - (fx - view.a) * k, b = fx + (view.b - fx) * k
    a = Math.max(0, a); b = Math.min(1, b)
    if (b - a < 0.002) { const m = (a + b) / 2; a = m - 0.001; b = m + 0.001 }
    setView({ a: Math.max(0, a), b: Math.min(1, b) })
  }
  const onDown = (e) => { dragRef.current = { x: e.clientX, a: view.a, b: view.b, moved: false } }
  const onMove = (e) => {
    if (!dragRef.current) return
    const r = e.currentTarget.getBoundingClientRect()
    const d = (e.clientX - dragRef.current.x) / r.width * (view.b - view.a)
    if (Math.abs(e.clientX - dragRef.current.x) > 3) dragRef.current.moved = true
    let a = dragRef.current.a - d, b = dragRef.current.b - d
    if (a < 0) { b -= a; a = 0 } if (b > 1) { a -= (b - 1); b = 1 }
    setView({ a: Math.max(0, a), b: Math.min(1, b) })
  }
  const onUp = (e) => {
    const dr = dragRef.current; dragRef.current = null
    if (!dr || dr.moved || !onTune || !frame) return
    const r = e.currentTarget.getBoundingClientRect()
    const fx = view.a + (e.clientX - r.left) / r.width * (view.b - view.a)
    onTune(frame.center_hz - frame.span_hz / 2 + fx * frame.span_hz)
  }

  return (
    <div ref={wrap} style={{ width: '100%' }}>
      <canvas ref={cv} style={{ width: '100%', height, display: 'block', cursor: 'crosshair', borderRadius: 4 }}
              onWheel={onWheel} onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp} onMouseLeave={() => (dragRef.current = null)} />
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 9, color: '#5b6b7a', padding: '2px 2px 0', gap: 8 }}>
        <span>{frame?.source === 'hardware' ? `live (${frame.driver || 'SDR'})` : 'synthetic spectrum (install SoapySDR for live RF)'} · scroll to zoom, drag to pan, click to tune</span>
        <span style={{ display: 'inline-flex', gap: 8, flexShrink: 0 }}>
          <button style={{ background: 'none', border: 'none', color: showWaterfall ? '#22d3ee' : '#6e7bff', cursor: 'pointer', fontSize: 9 }}
                  title="Open / close the waterfall (spectrogram) under this spectrum — shows historical activity"
                  onClick={() => setShowWaterfall(v => !v)}>{showWaterfall ? '▦ hide waterfall' : '▦ waterfall'}</button>
          {(view.a > 0 || view.b < 1) && <button style={{ background: 'none', border: 'none', color: '#6e7bff', cursor: 'pointer', fontSize: 9 }} onClick={() => setView({ a: 0, b: 1 })}>reset zoom</button>}
        </span>
      </div>
      {showWaterfall && <SpectrogramView history={history} view={view} height={Math.max(80, Math.round(height * 0.7))} />}
    </div>
  )
}
