import { useEffect, useRef, useState } from 'react'
import { Camera, Video, Square, RefreshCcw, Sliders, Palette } from 'lucide-react'

// ─── Colormaps ───────────────────────────────────────────────────────────────
// Each entry is a function: luminance ∈ [0,1] → [r,g,b] ∈ [0,255]³.
// "off" passes through colour frames (RGB from chroma decode) unchanged; the
// other maps treat the source as a luminance image and recolour it.
function lerp(a, b, t) { return a + (b - a) * t }
function lerpRGB(c0, c1, t) { return [lerp(c0[0], c1[0], t), lerp(c0[1], c1[1], t), lerp(c0[2], c1[2], t)] }
function paletteSampler(stops) {
  // stops: [[pos∈[0,1], [r,g,b]], …]
  return (v) => {
    v = Math.max(0, Math.min(1, v))
    for (let i = 1; i < stops.length; i++) {
      if (v <= stops[i][0]) {
        const [p0, c0] = stops[i - 1]; const [p1, c1] = stops[i]
        const t = (p1 === p0) ? 0 : (v - p0) / (p1 - p0)
        return lerpRGB(c0, c1, t)
      }
    }
    return stops[stops.length - 1][1]
  }
}

export const COLORMAPS = {
  off:        { label: 'Native (colour if decoded)', sampler: null },
  grayscale:  { label: 'Grayscale',  sampler: (v) => [v * 255, v * 255, v * 255] },
  amber:      { label: 'Amber CRT',  sampler: (v) => [v * 255, v * 188, v * 80] },
  green:      { label: 'Green phosphor', sampler: (v) => [v * 60, v * 255, v * 80] },
  blue:       { label: 'Blue',       sampler: (v) => [v * 80, v * 130, v * 255] },
  red:        { label: 'Red',        sampler: (v) => [v * 255, v * 50, v * 50] },
  inferno:    { label: 'Inferno (heat)', sampler: paletteSampler([
                  [0.00, [0,0,4]], [0.20, [40,11,84]], [0.40, [101,21,110]],
                  [0.60, [171,52,84]], [0.80, [231,114,33]], [1.00, [252,255,164]] ]) },
  viridis:    { label: 'Viridis',    sampler: paletteSampler([
                  [0.00, [68,1,84]], [0.25, [59,82,139]], [0.50, [33,144,140]],
                  [0.75, [94,201,98]], [1.00, [253,231,37]] ]) },
  plasma:     { label: 'Plasma',     sampler: paletteSampler([
                  [0.00, [13,8,135]], [0.30, [126,3,168]], [0.55, [203,71,119]],
                  [0.80, [248,149,64]], [1.00, [240,249,33]] ]) },
  ironbow:    { label: 'Ironbow (thermal)', sampler: paletteSampler([
                  [0.00, [0,0,0]], [0.25, [80,0,80]], [0.50, [200,40,0]],
                  [0.75, [255,180,0]], [1.00, [255,255,200]] ]) },
  nightvis:   { label: 'Night-vision green', sampler: paletteSampler([
                  [0.00, [0,0,0]], [0.30, [0,40,0]], [0.70, [40,200,40]], [1.00, [180,255,180]] ]) },
  iceblue:    { label: 'Ice blue',   sampler: paletteSampler([
                  [0.00, [0,0,32]], [0.50, [0,140,200]], [1.00, [220,250,255]] ]) },
}

// ─── Build 256-entry LUT for fast canvas blit ────────────────────────────────
function buildLUT(sampler) {
  const lut = new Uint8ClampedArray(256 * 3)
  for (let i = 0; i < 256; i++) {
    const [r, g, b] = sampler(i / 255)
    lut[i * 3] = r; lut[i * 3 + 1] = g; lut[i * 3 + 2] = b
  }
  return lut
}

// ─── Adjust a frame: brightness/contrast/gamma → optional colormap ──────────
function applyAdjustments(srcImageData, opts) {
  const { brightness = 1.0, contrast = 1.0, gamma = 1.0, colormap = 'off' } = opts
  const cm = COLORMAPS[colormap]
  const src = srcImageData.data
  const out = new Uint8ClampedArray(src.length)
  const invGamma = 1.0 / Math.max(0.01, gamma)
  const lut = (cm && cm.sampler) ? buildLUT(cm.sampler) : null
  // Pre-compute brightness/contrast/gamma onto an 8-bit LUT, then optionally apply colour LUT.
  const adj = new Uint8ClampedArray(256)
  for (let i = 0; i < 256; i++) {
    let v = i / 255
    v = (v - 0.5) * contrast + 0.5
    v = v * brightness
    v = Math.max(0, Math.min(1, v))
    v = Math.pow(v, invGamma)
    adj[i] = Math.round(v * 255)
  }
  if (lut) {
    // Convert source to luminance, then map via colour LUT.
    for (let i = 0; i < src.length; i += 4) {
      const y = adj[Math.round(0.299 * src[i] + 0.587 * src[i + 1] + 0.114 * src[i + 2])]
      out[i] = lut[y * 3]
      out[i + 1] = lut[y * 3 + 1]
      out[i + 2] = lut[y * 3 + 2]
      out[i + 3] = 255
    }
  } else {
    // pass-through with B/C/γ on each channel independently
    for (let i = 0; i < src.length; i += 4) {
      out[i] = adj[src[i]]
      out[i + 1] = adj[src[i + 1]]
      out[i + 2] = adj[src[i + 2]]
      out[i + 3] = 255
    }
  }
  return new ImageData(out, srcImageData.width, srcImageData.height)
}

// ─── Reusable hidden image that drives the canvas pipeline ───────────────────
export function UasVideoCanvas({ src, tick, controls, onCanvas, style }) {
  const cvsRef = useRef(null)
  const imgRef = useRef(null)
  const lastImageData = useRef(null)

  // Whenever the source URL or tick changes, re-load the image.
  useEffect(() => {
    if (!src) return
    const img = new Image()
    img.crossOrigin = 'anonymous'
    img.onload = () => {
      const c = cvsRef.current
      if (!c) return
      c.width = img.naturalWidth
      c.height = img.naturalHeight
      const ctx = c.getContext('2d', { willReadFrequently: true })
      ctx.imageSmoothingEnabled = false
      ctx.drawImage(img, 0, 0)
      try { lastImageData.current = ctx.getImageData(0, 0, c.width, c.height) }
      catch { lastImageData.current = null }
      // Re-apply controls
      if (lastImageData.current) {
        const adjusted = applyAdjustments(lastImageData.current, controls)
        ctx.putImageData(adjusted, 0, 0)
      }
      onCanvas?.(c)
    }
    img.onerror = () => {}
    img.src = `${src}${src.includes('?') ? '&' : '?'}i=${tick}`
    imgRef.current = img
  }, [src, tick])     // eslint-disable-line react-hooks/exhaustive-deps

  // When controls change, re-apply without re-fetching.
  useEffect(() => {
    const c = cvsRef.current
    if (!c || !lastImageData.current) return
    const ctx = c.getContext('2d', { willReadFrequently: true })
    const adjusted = applyAdjustments(lastImageData.current, controls)
    ctx.putImageData(adjusted, 0, 0)
  }, [controls])

  return <canvas ref={cvsRef} style={{
    width: '100%', maxHeight: 320, objectFit: 'contain',
    background: '#000', borderRadius: 6, marginBottom: 8, imageRendering: 'pixelated', ...style,
  }} />
}

// ─── Snapshot helper: download canvas as PNG ────────────────────────────────
export function snapshotCanvas(canvas, label) {
  if (!canvas) return
  const dataURL = canvas.toDataURL('image/png')
  const a = document.createElement('a')
  a.href = dataURL
  a.download = `ares-uas-${label || 'frame'}-${new Date().toISOString().replace(/[:.]/g, '-')}.png`
  document.body.appendChild(a); a.click(); a.remove()
}

// ─── Recording helper (returns a controller with start/stop) ────────────────
export function startCanvasRecording(canvas, fps = 15) {
  if (!canvas || !canvas.captureStream) return null
  const stream = canvas.captureStream(fps)
  const mimeCandidates = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm']
  const mime = mimeCandidates.find(m => window.MediaRecorder && MediaRecorder.isTypeSupported(m))
  if (!mime) return null
  const rec = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: 1_500_000 })
  const chunks = []
  rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data) }
  return {
    rec, stream,
    start: () => rec.start(250),
    stop: () => new Promise((resolve) => {
      rec.onstop = () => {
        const blob = new Blob(chunks, { type: mime })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `ares-uas-clip-${new Date().toISOString().replace(/[:.]/g, '-')}.webm`
        document.body.appendChild(a); a.click(); a.remove()
        setTimeout(() => URL.revokeObjectURL(url), 5000)
        stream.getTracks().forEach(t => t.stop())
        resolve(blob)
      }
      rec.stop()
    }),
  }
}

// ─── Slider primitive ────────────────────────────────────────────────────────
const lbl = { fontSize: 10, color: '#8b949e', display: 'inline-block', minWidth: 92 }
const val = { fontSize: 10, color: '#c9d1d9', display: 'inline-block', minWidth: 50, textAlign: 'right' }
function Slider({ label, value, min, max, step = 0.01, onChange, format = (v) => v.toFixed(2), title }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }} title={title}>
      <span style={lbl}>{label}</span>
      <input type="range" min={min} max={max} step={step} value={value}
             onChange={(e) => onChange(parseFloat(e.target.value))}
             style={{ flex: 1, accentColor: '#58a6ff' }} />
      <span style={val}>{format(value)}</span>
    </div>
  )
}

function NumberInput({ label, value, min, max, step = 1, onChange, suffix = '', title, width = 80 }) {
  return (
    <label style={{ display: 'flex', alignItems: 'center', gap: 6 }} title={title}>
      <span style={lbl}>{label}</span>
      <input type="number" value={value ?? ''}
             min={min} max={max} step={step}
             onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
             style={{ width, background: '#0d1117', color: '#c9d1d9', border: '1px solid #21262d',
                       borderRadius: 4, padding: '2px 4px', fontSize: 11 }} />
      {suffix && <span style={{ fontSize: 10, color: '#6e7681' }}>{suffix}</span>}
    </label>
  )
}

// ─── Main controls panel ────────────────────────────────────────────────────
export function UasVideoControlsPanel({
  controls, setControls,        // local (display-side) controls: { colormap, brightness, contrast, gamma }
  demodOpts, setDemodOpts,      // backend-side demod options (analog_options dict)
  canvas, sessionId, onRedemod, // canvas for snapshot/record; onRedemod(opts) re-runs demod on the session
}) {
  const [expanded, setExpanded] = useState(true)
  const [demodExpanded, setDemodExpanded] = useState(false)
  const [recording, setRecording] = useState(null)
  const [busy, setBusy] = useState(false)
  const update = (k, v) => setControls(c => ({ ...c, [k]: v }))
  const updateD = (k, v) => setDemodOpts(d => ({ ...d, [k]: v }))
  const cardStyle = { background: '#0d1117', border: '1px solid #21262d', borderRadius: 8, padding: 8, marginBottom: 6 }

  const snap = () => snapshotCanvas(canvas, sessionId || 'frame')
  const toggleRec = async () => {
    if (recording) {
      setRecording(null)
      try { await recording.stop() } catch {}
    } else {
      const r = startCanvasRecording(canvas, 15)
      if (!r) { alert('Recording unsupported in this environment.'); return }
      r.start(); setRecording(r)
    }
  }
  const redemod = async () => {
    if (!onRedemod) return
    setBusy(true)
    try { await onRedemod(demodOpts) } finally { setBusy(false) }
  }

  return (
    <div style={cardStyle}>
      <div onClick={() => setExpanded(v => !v)} style={{ cursor: 'pointer', fontSize: 11, color: '#58a6ff', marginBottom: 6 }}>
        {expanded ? '▾' : '▸'} <Sliders size={11} style={{ verticalAlign: 'middle' }} /> Video display controls
      </div>
      {expanded && (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginBottom: 6 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={lbl}><Palette size={11} style={{ verticalAlign: 'middle' }} /> Colormap</span>
              <select value={controls.colormap} onChange={(e) => update('colormap', e.target.value)}
                      style={{ flex: 1, background: '#0d1117', color: '#c9d1d9', border: '1px solid #21262d', borderRadius: 4, padding: '2px 4px', fontSize: 11 }}>
                {Object.entries(COLORMAPS).map(([k, v]) => (
                  <option key={k} value={k}>{v.label}</option>
                ))}
              </select>
            </label>
            <div />
            <Slider label="Brightness" value={controls.brightness} min={0.2} max={3.0} step={0.05}
                    onChange={(v) => update('brightness', v)} />
            <Slider label="Contrast" value={controls.contrast} min={0.2} max={3.0} step={0.05}
                    onChange={(v) => update('contrast', v)} />
            <Slider label="Gamma" value={controls.gamma} min={0.3} max={3.0} step={0.05}
                    onChange={(v) => update('gamma', v)} />
            <Slider label="Scanline FPS" value={controls.scanlineFps} min={1} max={20} step={1}
                    onChange={(v) => update('scanlineFps', v)}
                    format={(v) => `${v.toFixed(0)} /s`} title="UI refresh rate of the decoded raster image." />
          </div>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 4 }}>
            <button className="btn btn-ghost" style={{ fontSize: 10, padding: '3px 8px', gap: 4 }} onClick={snap}>
              <Camera size={12} /> Snapshot
            </button>
            <button className={recording ? 'btn btn-primary' : 'btn btn-ghost'} style={{ fontSize: 10, padding: '3px 8px', gap: 4 }} onClick={toggleRec}>
              {recording ? <><Square size={12} /> Stop recording</> : <><Video size={12} /> Record</>}
            </button>
            <button className="btn btn-ghost" style={{ fontSize: 10, padding: '3px 8px', gap: 4 }}
                    onClick={() => setControls({ colormap: 'off', brightness: 1.0, contrast: 1.0, gamma: 1.0, scanlineFps: controls.scanlineFps })}>
              <RefreshCcw size={12} /> Reset display
            </button>
          </div>
        </>
      )}
      <div onClick={() => setDemodExpanded(v => !v)} style={{ cursor: 'pointer', fontSize: 11, color: '#58a6ff', marginTop: 8, marginBottom: 4 }}>
        {demodExpanded ? '▾' : '▸'} Demod parameters (backend re-run)
      </div>
      {demodExpanded && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={lbl}>System</span>
            <select value={demodOpts.system || ''} onChange={(e) => updateD('system', e.target.value || null)}
                    style={{ flex: 1, background: '#0d1117', color: '#c9d1d9', border: '1px solid #21262d', borderRadius: 4, padding: '2px 4px', fontSize: 11 }}>
              <option value="">auto from feed</option>
              <option value="ntsc">NTSC (525/59.94)</option>
              <option value="pal">PAL (625/50)</option>
              <option value="secam">SECAM</option>
              <option value="vsb">VSB / AM</option>
            </select>
          </label>
          <NumberInput label="Width (px)" value={demodOpts.width_px} min={64} max={1280} step={8}
                       onChange={(v) => updateD('width_px', v)} suffix="px" />
          <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11 }}>
            <input type="checkbox" checked={!!demodOpts.try_all_detectors}
                   onChange={(e) => updateD('try_all_detectors', e.target.checked)} />
            <span style={{ color: '#c9d1d9' }}>Try all detectors</span>
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11 }}>
            <input type="checkbox" checked={!!demodOpts.use_h_sync_pll}
                   onChange={(e) => updateD('use_h_sync_pll', e.target.checked)} />
            <span style={{ color: '#c9d1d9' }}>H-sync PLL</span>
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11 }}>
            <input type="checkbox" checked={!!demodOpts.use_v_sync_detect}
                   onChange={(e) => updateD('use_v_sync_detect', e.target.checked)} />
            <span style={{ color: '#c9d1d9' }}>V-sync detect</span>
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11 }}>
            <input type="checkbox" checked={!!demodOpts.deinterlace}
                   onChange={(e) => updateD('deinterlace', e.target.checked)} />
            <span style={{ color: '#c9d1d9' }}>Deinterlace</span>
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11 }}>
            <input type="checkbox" checked={!!demodOpts.decode_color}
                   onChange={(e) => updateD('decode_color', e.target.checked)} />
            <span style={{ color: '#c9d1d9' }}>Decode colour (NTSC/PAL)</span>
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11 }}>
            <input type="checkbox" checked={!!demodOpts.use_per_line_clamp}
                   onChange={(e) => updateD('use_per_line_clamp', e.target.checked)} />
            <span style={{ color: '#c9d1d9' }}>Per-line peak-hold</span>
          </label>
          <Slider label="Peak-hold τ" value={demodOpts.peak_hold_tau_s ?? 0.30}
                  min={0.05} max={2.0} step={0.05}
                  onChange={(v) => updateD('peak_hold_tau_s', v)}
                  format={(v) => `${(v * 1000).toFixed(0)} ms`} />
          <Slider label="Frame avg N" value={demodOpts.frame_avg_n ?? 0}
                  min={0} max={16} step={1}
                  onChange={(v) => updateD('frame_avg_n', Math.round(v))}
                  format={(v) => v.toFixed(0)} title="0 = off; ≥2 averages successive frames to reduce noise." />
          <NumberInput label="Capture (ms)" value={Math.round((demodOpts._capture_seconds ?? 0.045) * 1000)}
                       min={20} max={500} step={5}
                       onChange={(v) => updateD('_capture_seconds', (v ?? 45) / 1000)} suffix="ms"
                       title="Per re-demod IQ capture duration; longer → more frames per redemod but more latency." />
          <div style={{ gridColumn: '1 / -1', borderTop: '1px solid #21262d', paddingTop: 6, marginTop: 4 }}>
            <div style={{ fontSize: 10, color: '#6e7681', marginBottom: 4 }}>Operator overrides (leave blank for auto-tune)</div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
              <NumberInput label="Line rate" value={demodOpts.line_rate_hz} min={1000} max={200000} step={1}
                           onChange={(v) => updateD('line_rate_hz', v)} suffix="Hz" title="Forces scanline rate." />
              <NumberInput label="Frame rate" value={demodOpts.frame_rate_hz} min={1} max={240} step={0.01}
                           onChange={(v) => updateD('frame_rate_hz', v)} suffix="Hz" title="Forces frame rate." />
              <NumberInput label="Pixel rate" value={demodOpts.pixel_rate_hz ? demodOpts.pixel_rate_hz / 1e6 : null}
                           min={0.1} max={200} step={0.1}
                           onChange={(v) => updateD('pixel_rate_hz', v == null ? null : v * 1e6)} suffix="MHz" />
              <NumberInput label="Active line" value={demodOpts.active_duration_s ? demodOpts.active_duration_s * 1e6 : null}
                           min={5} max={100000} step={0.5}
                           onChange={(v) => updateD('active_duration_s', v == null ? null : v / 1e6)} suffix="µs" title="Active scanline duration." />
              <NumberInput label="H offset" value={demodOpts.h_offset_samples} min={-32768} max={32768} step={1}
                           onChange={(v) => updateD('h_offset_samples', v)} suffix="samples" />
              <NumberInput label="V offset" value={demodOpts.v_offset_lines} min={-512} max={512} step={1}
                           onChange={(v) => updateD('v_offset_lines', v)} suffix="lines" />
            </div>
          </div>
          <div style={{ gridColumn: '1 / -1', display: 'flex', gap: 6, marginTop: 6 }}>
            <button className="btn btn-primary" style={{ fontSize: 10, padding: '3px 10px' }} disabled={busy} onClick={redemod}>
              {busy ? 'Re-demodulating…' : 'Re-demodulate frames'}
            </button>
            <button className="btn btn-ghost" style={{ fontSize: 10, padding: '3px 10px' }}
                    onClick={() => setDemodOpts({})}>Reset demod params</button>
          </div>
        </div>
      )}
    </div>
  )
}

export const DEFAULT_DISPLAY_CONTROLS = {
  colormap: 'off',
  brightness: 1.0,
  contrast: 1.0,
  gamma: 1.0,
  scanlineFps: 5,
}

export const DEFAULT_DEMOD_OPTS = {
  try_all_detectors: true,
  use_h_sync_pll: true,
  use_v_sync_detect: true,
  use_per_line_clamp: true,
  deinterlace: true,
  decode_color: true,
  peak_hold_tau_s: 0.30,
  frame_avg_n: 0,
}
