// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * CyberPanel — the left-panel UI for the Cyber tab (roadmap item 11 / C6).
 *
 * Surfaces pentest-class capabilities by what they do — sub-GHz, RFID (LF), NFC
 * (HF), infrared, iButton, GPIO, HID — never by device brand. Sub-GHz runs on the
 * real SDR; the contactless/IR/HID capabilities run over a connected USB field
 * tool. PASSIVE actions (scan/read/sniff/receive) need only the hardware; ACTIVE
 * actions (replay/transmit/emulate/clone/write/run) are refused by the backend
 * unless the Authorized-Active gate is on, and every attempt is audit-logged.
 */
import { memo, useCallback, useEffect, useMemo, useState } from 'react'
import { Bug, ShieldAlert, ShieldCheck, Lock, Unlock, Terminal } from 'lucide-react'
import {
  getCyberCapabilities, detectCyber, getCyberAuthorized, setCyberAuthorized,
  runCyberAction, getCyberCaptures, cyberRawCli,
} from '../../api/client'
import { usePolling } from '../../hooks/usePolling'

const MUTED = '#8b949e'
const BORDER = '#30363d'
const ORANGE = '#f0883e'
const GREEN = '#3fb950'
const RED = '#f85149'

// Which params each action collects from the operator (mirrors the backend templates).
const ACTION_PARAMS = {
  'subghz/scan': ['center_mhz'], 'subghz/capture': ['center_mhz', 'seconds'],
  'subghz/replay': ['capture_id', 'center_mhz'],
  'rfid_lf/emulate': ['uid'], 'nfc_hf/write': ['data'],
  'infrared/transmit': ['protocol', 'data'], 'ibutton/emulate': ['id'],
  'gpio/read': ['pin'], 'gpio/write': ['pin', 'value'], 'badusb/run': ['script'],
}
const PARAM_LABEL = {
  center_mhz: 'Center (MHz)', seconds: 'Seconds', capture_id: 'Capture',
  uid: 'UID (hex)', data: 'Data (hex)', protocol: 'Protocol', id: 'Key ID (hex)',
  pin: 'Pin', value: 'Value (0/1)', script: 'HID script',
}

export default function CyberPanel() {
  const [catalog, setCatalog] = useState([])
  const [detail, setDetail] = useState(null)        // /cyber/detect result
  const [authorized, setAuthorized] = useState(false)
  const [captures, setCaptures] = useState([])
  const [ack, setAck] = useState(false)             // operator acknowledgment before enabling
  const [busy, setBusy] = useState(false)
  const [gateErr, setGateErr] = useState('')         // surface auth-toggle errors so the button isn't silent

  const refreshCaptures = useCallback(() => {
    getCyberCaptures().then(r => setCaptures(r.captures || [])).catch(() => {})
  }, [])

  useEffect(() => {
    getCyberCapabilities().then(r => setCatalog(r.catalog || [])).catch(() => setCatalog([]))
    getCyberAuthorized().then(r => setAuthorized(!!r.authorized_active)).catch(() => {})
    refreshCaptures()
  }, [refreshCaptures])

  // Detect changes rarely (hardware plug/unplug). The backend caches /cyber/detect
  // for 10 s, so poll at that cadence — anything faster is wasted re-renders and
  // contributed to the panel feeling laggy.
  usePolling(async () => {
    try { setDetail(await detectCyber()) } catch { /* ignore */ }
  }, 10000, { deps: [] })

  const toggleGate = useCallback(async (on) => {
    setGateErr('')
    if (on && !ack) { setGateErr('Tick the acknowledgment first.'); return }
    setBusy(true)
    try {
      const r = await setCyberAuthorized(on)
      setAuthorized(!!r.authorized_active)
      if (!on) setAck(false)
    } catch (e) {
      setGateErr(e?.response?.data?.detail || e?.message || 'failed to toggle')
    } finally { setBusy(false) }
  }, [ack])

  const available = useMemo(
    () => new Set(detail?.available_capabilities || []),
    [detail?.available_capabilities?.join(',')]   // primitive-compare so identity doesn't flap every poll
  )

  return (
    <div style={{ padding: '10px 12px', overflowY: 'auto', height: '100%', fontSize: 12, color: '#c9d1d9' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <Bug size={18} color={ORANGE} />
        <span style={{ fontSize: 15, fontWeight: 700 }}>Cyber</span>
        <span style={{ fontSize: 10, color: MUTED }}>RF · RFID/NFC · IR · iButton · GPIO · HID</span>
      </div>

      {/* Authorized & lawful use / gate */}
      <GateCard authorized={authorized} ack={ack} setAck={setAck} busy={busy} onToggle={toggleGate} error={gateErr} />

      {/* (Connected-hardware detection lives in the SDR console as "Pentest tools".) */}

      {/* Capability cards */}
      {catalog.map(cat => (
        <CapabilityCard
          key={cat.id} cat={cat} authorized={authorized}
          available={available.has(cat.id)}
          captures={captures} onAfterCapture={refreshCaptures}
        />
      ))}

      {/* Raw tool console — works with any connected tool regardless of CLI grammar */}
      <RawConsole detail={detail} authorized={authorized} />
    </div>
  )
}

function RawConsole({ detail, authorized }) {
  const tools = detail?.tools || []
  const [toolId, setToolId] = useState('')
  const [cmd, setCmd] = useState('')
  const [out, setOut] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (tools.length && !tools.some(t => t.id === toolId)) setToolId(tools[0].id)
  }, [tools, toolId])

  const send = async () => {
    if (!cmd.trim() || !toolId) return
    setBusy(true); setErr(''); setOut(null)
    try {
      const r = await cyberRawCli(toolId, cmd)
      setOut(r)
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || 'failed')
    } finally { setBusy(false) }
  }

  const blocked = !authorized
  return (
    <div style={{ border: `1px solid ${BORDER}`, borderRadius: 6, padding: 10, marginBottom: 10 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 700, marginBottom: 4 }}>
        <Terminal size={14} color={ORANGE} /> Raw tool console
        <span style={{ fontSize: 10, color: RED, marginLeft: 'auto' }}>active</span>
      </div>
      <div style={{ fontSize: 11, color: MUTED, marginBottom: 7, lineHeight: 1.4 }}>
        Send commands straight to a connected tool's CLI — for any capability or grammar the
        named actions don't cover. Gated + audit-logged (it can transmit/emulate).
      </div>
      {tools.length === 0 ? (
        <div style={{ fontSize: 11, color: MUTED }}>No USB field tool connected.</div>
      ) : blocked ? (
        <div style={{ fontSize: 11, color: ORANGE }}>Enable active features above to use the raw console.</div>
      ) : (
        <>
          <div style={{ display: 'flex', gap: 6, marginBottom: 6 }}>
            <select value={toolId} onChange={e => setToolId(e.target.value)} style={inp(150)}>
              {tools.map(t => <option key={t.id} value={t.id}>{t.label}</option>)}
            </select>
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            <input value={cmd} onChange={e => setCmd(e.target.value)} placeholder="e.g. device_info"
                   onKeyDown={e => { if (e.key === 'Enter') send() }}
                   style={{ ...inp(0), flex: 1, fontFamily: 'monospace' }} />
            <button className="btn btn-secondary" disabled={busy || !cmd.trim()}
                    style={{ borderColor: RED, color: RED }} onClick={send}>
              {busy ? '…' : 'Send'}
            </button>
          </div>
        </>
      )}
      {err && <div style={{ marginTop: 7, fontSize: 11, color: RED }}>⚠ {err}</div>}
      {out && (
        <pre style={{ marginTop: 8, fontSize: 10, color: '#9da7b3', background: '#0d1117',
                      border: `1px solid ${BORDER}`, borderRadius: 4, padding: 8, maxHeight: 200,
                      overflow: 'auto', whiteSpace: 'pre-wrap' }}>
          <span style={{ color: GREEN }}>$ {out.command}</span>{'\n'}{out.response || '(no response)'}
        </pre>
      )}
    </div>
  )
}

function GateCard({ authorized, ack, setAck, busy, onToggle, error }) {
  return (
    <div style={{
      border: `1px solid ${authorized ? RED : BORDER}`, borderRadius: 6, padding: 10, marginBottom: 10,
      background: authorized ? 'rgba(248,81,73,0.07)' : 'rgba(240,136,62,0.05)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 700,
                    color: authorized ? RED : ORANGE, marginBottom: 4 }}>
        {authorized ? <Unlock size={14} /> : <Lock size={14} />}
        {authorized ? 'ACTIVE FEATURES ENABLED' : 'Active features disabled'}
      </div>
      <div style={{ fontSize: 11, color: MUTED, lineHeight: 1.45 }}>
        <strong>Authorized & lawful use only.</strong> Active features (transmit/replay, emulate,
        clone, write, HID) may be illegal without authorization. You are solely responsible for
        operating within applicable law (CFAA, Wiretap Act, FCC Part 15/97, local equivalents) and
        the scope of any written authorization. Passive scan/read/sniff stays passive.
      </div>
      {!authorized ? (
        <div style={{ marginTop: 8 }}>
          <label style={{ display: 'flex', gap: 6, alignItems: 'flex-start', fontSize: 11, cursor: 'pointer',
                          padding: 4, marginLeft: -4, borderRadius: 4,
                          background: error && !ack ? 'rgba(248,81,73,0.12)' : 'transparent' }}>
            <input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} style={{ marginTop: 2 }} />
            <span>I am authorized to operate active RF/pentest features in this environment.</span>
          </label>
          {/* Always clickable — a disabled button looks broken; instead, on click without
              the acknowledgment we surface a message and highlight the checkbox above. */}
          <button className="btn btn-primary" disabled={busy} style={{ marginTop: 8 }}
                  onClick={() => onToggle(true)}>
            <ShieldAlert size={13} style={{ marginRight: 4 }} /> {busy ? 'Enabling…' : 'Enable active features'}
          </button>
        </div>
      ) : (
        <button className="btn btn-secondary" disabled={busy} style={{ marginTop: 8 }}
                onClick={() => onToggle(false)}>
          <ShieldCheck size={13} style={{ marginRight: 4 }} /> {busy ? 'Disabling…' : 'Disable active features'}
        </button>
      )}
      {error && <div style={{ marginTop: 6, fontSize: 11, color: RED }}>⚠ {error}</div>}
    </div>
  )
}


const CapabilityCard = memo(function CapabilityCard({ cat, authorized, available, captures, onAfterCapture }) {
  const [params, setParams] = useState({ center_mhz: '433.92', seconds: '1', value: '0' })
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [running, setRunning] = useState(null)

  const setParam = (k, v) => setParams(p => ({ ...p, [k]: v }))

  const invoke = async (action) => {
    setRunning(action); setError(''); setResult(null)
    const need = ACTION_PARAMS[`${cat.id}/${action.id}`] || []
    const payload = {}
    for (const k of need) {
      if (k === 'center_mhz') payload.center_hz = Math.round(parseFloat(params.center_mhz || '0') * 1e6)
      else if (k === 'seconds') payload.seconds = parseFloat(params.seconds || '1')
      else payload[k] = params[k] ?? ''
    }
    try {
      const r = await runCyberAction(cat.id, action.id, payload)
      setResult(r)
      if (cat.id === 'subghz' && action.id === 'capture') onAfterCapture?.()
    } catch (e) {
      setError(e?.response?.data?.detail || e?.message || 'failed')
    } finally { setRunning(null) }
  }

  const need = (action) => ACTION_PARAMS[`${cat.id}/${action.id}`] || []
  const allNeeded = [...new Set(cat.actions.flatMap(need))]

  return (
    <div style={{ border: `1px solid ${BORDER}`, borderRadius: 6, padding: 10, marginBottom: 10,
                  opacity: available ? 1 : 0.55 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
        <span style={{ fontWeight: 700 }}>{cat.label}</span>
        <span style={{ fontSize: 10, color: MUTED }}>{cat.band}</span>
        {!available && <span style={{ fontSize: 10, color: ORANGE, marginLeft: 'auto' }}>no hardware</span>}
      </div>
      <div style={{ fontSize: 11, color: MUTED, margin: '3px 0 7px', lineHeight: 1.4 }}>{cat.desc}</div>

      {/* Param inputs relevant to this category */}
      {allNeeded.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 7 }}>
          {allNeeded.filter(k => k !== 'capture_id').map(k => (
            <label key={k} style={{ fontSize: 10, color: MUTED, display: 'flex', flexDirection: 'column', gap: 2 }}>
              {PARAM_LABEL[k] || k}
              {k === 'script' ? (
                <textarea value={params[k] || ''} onChange={e => setParam(k, e.target.value)} rows={2}
                          style={inp(180)} placeholder="DELAY 500&#10;STRING ..." />
              ) : (
                <input value={params[k] || ''} onChange={e => setParam(k, e.target.value)} style={inp(90)} />
              )}
            </label>
          ))}
          {allNeeded.includes('capture_id') && (
            <label style={{ fontSize: 10, color: MUTED, display: 'flex', flexDirection: 'column', gap: 2 }}>
              Capture
              <select value={params.capture_id || ''} onChange={e => setParam('capture_id', e.target.value)} style={inp(140)}>
                <option value="">— select —</option>
                {captures.map(c => <option key={c.id} value={c.id}>{c.id}</option>)}
              </select>
            </label>
          )}
        </div>
      )}

      {/* Action buttons */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
        {cat.actions.map(a => {
          const active = a.kind === 'active'
          const blocked = active && !authorized
          const disabled = !available || running === a.id || blocked
          return (
            <button key={a.id}
                    className={`btn ${active ? 'btn-secondary' : 'btn-ghost'}`}
                    disabled={disabled}
                    title={blocked ? 'Enable active features (authorization required)' : a.label}
                    style={{ padding: '3px 8px', fontSize: 11,
                             borderColor: active ? RED : undefined, color: active ? RED : undefined }}
                    onClick={() => invoke(a)}>
              {active && <ShieldAlert size={11} style={{ marginRight: 3, verticalAlign: 'text-bottom' }} />}
              {running === a.id ? '…' : a.label}
            </button>
          )
        })}
      </div>

      {/* Output */}
      {error && <div style={{ marginTop: 7, fontSize: 11, color: RED }}>⚠ {error}</div>}
      {result && <ResultView cat={cat} result={result} />}
    </div>
  )
})

function ResultView({ cat, result }) {
  // Sub-GHz scan → mini spectrum sparkline.
  if (cat.id === 'subghz' && Array.isArray(result.power_dbm)) {
    return (
      <div style={{ marginTop: 8 }}>
        <Sparkline data={result.power_dbm} />
        <div style={{ fontSize: 10, color: MUTED, marginTop: 3 }}>
          peak {((result.peak_hz || 0) / 1e6).toFixed(3)} MHz @ {result.peak_dbm} dBm ·
          noise {result.noise_floor_dbm} dBm · {result.radio}
        </div>
      </div>
    )
  }
  // Everything else → key facts + raw response.
  return (
    <pre style={{ marginTop: 8, fontSize: 10, color: '#9da7b3', background: '#0d1117',
                  border: `1px solid ${BORDER}`, borderRadius: 4, padding: 8, maxHeight: 160,
                  overflow: 'auto', whiteSpace: 'pre-wrap' }}>
      {result.response != null ? result.response : JSON.stringify(result, null, 2)}
    </pre>
  )
}

function Sparkline({ data }) {
  const n = data.length
  const lo = Math.min(...data), hi = Math.max(...data)
  const rng = hi - lo || 1
  const W = 240, H = 48
  const pts = data.map((v, i) => `${(i / (n - 1)) * W},${H - ((v - lo) / rng) * (H - 2) - 1}`).join(' ')
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
         style={{ background: '#0d1117', border: `1px solid ${BORDER}`, borderRadius: 4, display: 'block' }}>
      <polyline points={pts} fill="none" stroke={GREEN} strokeWidth="1" />
    </svg>
  )
}

const inp = (w) => ({ width: w, background: '#0d1117', color: '#c9d1d9', border: `1px solid ${BORDER}`,
                      borderRadius: 4, padding: '3px 6px', fontSize: 11 })
