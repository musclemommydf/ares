import { useEffect, useState } from 'react'
import { Radio, Play, Square, RefreshCw, Wifi, Bluetooth, AlertTriangle } from 'lucide-react'
import {
  cellularCapabilities, startCellular, listCellularSessions, stopCellularSession, getCellularEvents,
} from '../../api/client'

const card = { background: '#0d1117', border: '1px solid #21262d', borderRadius: 6, padding: 8, marginBottom: 8 }
const inp  = { background: '#0d1117', color: '#c9d1d9', border: '1px solid #21262d', borderRadius: 4, padding: '3px 6px', fontSize: 11 }
const btn  = { background: '#21262d', border: '1px solid #30363d', borderRadius: 4, color: '#c9d1d9', padding: '3px 8px', fontSize: 11, cursor: 'pointer' }
const muted = { fontSize: 10, color: '#6e7681' }

function CapBadge({ ok, label }) {
  return <span style={{
    fontSize: 9, padding: '1px 5px', borderRadius: 3, marginRight: 4,
    background: ok ? '#1f6f3f' : '#3a1d1d', color: '#fff',
  }}>{ok ? '✓' : '✗'} {label}</span>
}

export default function CellularPanel({ devices = [] }) {
  const [caps, setCaps] = useState(null)
  const [sessions, setSessions] = useState([])
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [form, setForm] = useState({
    kind: 'gsm',
    device_id: '',
    interface: 'wlan0mon',
    frequency_mhz: 925,
    sample_rate_hz: 1_000_000,
    bandwidth_mhz: 10,
    scs_khz: 30,
    channel: '',
  })

  const refresh = async () => {
    try {
      const [c, s] = await Promise.all([cellularCapabilities(), listCellularSessions()])
      setCaps(c); setSessions(s.sessions || []); setErr('')
    } catch (e) { setErr(String(e?.response?.data?.detail || e?.message || e)) }
  }
  useEffect(() => { refresh(); const h = setInterval(refresh, 4000); return () => clearInterval(h) }, [])

  const start = async () => {
    setBusy(true); setErr('')
    try {
      const body = { kind: form.kind }
      if (['gsm','umts','lte','nr'].includes(form.kind)) {
        body.device_id = form.device_id || undefined
        body.frequency_hz = Number(form.frequency_mhz) * 1e6
        if (form.kind === 'gsm') body.sample_rate_hz = Number(form.sample_rate_hz) || 1_000_000
        if (form.kind === 'lte') body.bandwidth_hz = Number(form.bandwidth_mhz) * 1e6
        if (form.kind === 'nr')  body.scs_khz = Number(form.scs_khz) || 30
      } else {
        body.interface = form.interface
        if (form.channel) body.channel = Number(form.channel)
      }
      await startCellular(body)
      await refresh()
    } catch (e) { setErr(String(e?.response?.data?.detail || e?.message || e)) }
    finally { setBusy(false) }
  }
  const stop = async (sid) => { try { await stopCellularSession(sid); refresh() } catch (e) { setErr(String(e?.message || e)) } }

  const decoders = caps?.decoders || {}
  return (
    <div style={card}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
        <Radio size={13} color="#22d3ee" />
        <b style={{ fontSize: 11, color: '#e6edf3' }}>Cellular / WiFi / BLE passive monitors</b>
        <span style={{ flex: 1 }} />
        <button style={btn} onClick={refresh}><RefreshCw size={11} /></button>
      </div>

      {/* Capabilities */}
      <div style={{ marginBottom: 6 }}>
        <CapBadge ok={!!caps?.gnuradio_in_process} label="GNU Radio in-process" />
        <CapBadge ok={!!decoders.gsm?.available}  label="GSM" />
        <CapBadge ok={!!decoders.lte?.available}  label="LTE" />
        <CapBadge ok={!!decoders.nr?.available}   label="5G NR" />
        <CapBadge ok={!!decoders.umts?.available} label="UMTS (DF only)" />
        <CapBadge ok={!!decoders.wifi?.available} label="WiFi" />
        <CapBadge ok={!!decoders.ble?.available}  label="BLE" />
      </div>

      {/* Start form */}
      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', alignItems: 'center' }}>
        <select value={form.kind} onChange={e => setForm(f => ({ ...f, kind: e.target.value }))} style={inp}>
          <option value="gsm">GSM (2G)</option>
          <option value="umts">UMTS (3G — DF only)</option>
          <option value="lte">LTE (4G)</option>
          <option value="nr">5G NR</option>
          <option value="wifi">WiFi</option>
          <option value="ble">BLE</option>
        </select>
        {['gsm','umts','lte','nr'].includes(form.kind) ? (
          <>
            <select value={form.device_id} onChange={e => setForm(f => ({ ...f, device_id: e.target.value }))} style={{ ...inp, width: 130 }}>
              <option value="">(any SDR)</option>
              {devices.map(d => <option key={d.id} value={d.id}>{d.id} {d.coherent_rx ? '(coh)' : ''}</option>)}
            </select>
            <label style={muted}>freq
              <input type="number" value={form.frequency_mhz} step="0.001"
                     onChange={e => setForm(f => ({ ...f, frequency_mhz: e.target.value }))}
                     style={{ ...inp, width: 90, marginLeft: 3 }} /> MHz
            </label>
            {form.kind === 'gsm' && (
              <label style={muted}>fs
                <input type="number" value={form.sample_rate_hz}
                       onChange={e => setForm(f => ({ ...f, sample_rate_hz: e.target.value }))}
                       style={{ ...inp, width: 100, marginLeft: 3 }} /> S/s
              </label>
            )}
            {form.kind === 'lte' && (
              <label style={muted}>BW
                <input type="number" value={form.bandwidth_mhz} step="0.1"
                       onChange={e => setForm(f => ({ ...f, bandwidth_mhz: e.target.value }))}
                       style={{ ...inp, width: 60, marginLeft: 3 }} /> MHz
              </label>
            )}
            {form.kind === 'nr' && (
              <label style={muted}>SCS
                <input type="number" value={form.scs_khz} step="15"
                       onChange={e => setForm(f => ({ ...f, scs_khz: e.target.value }))}
                       style={{ ...inp, width: 50, marginLeft: 3 }} /> kHz
              </label>
            )}
          </>
        ) : (
          <>
            <label style={muted}>{form.kind === 'wifi' ? 'iface (monitor)' : 'hci'}
              <input value={form.interface}
                     onChange={e => setForm(f => ({ ...f, interface: e.target.value }))}
                     placeholder={form.kind === 'wifi' ? 'wlan0mon' : 'hci0'}
                     style={{ ...inp, width: 110, marginLeft: 3 }} />
            </label>
            {form.kind === 'wifi' && (
              <label style={muted}>ch
                <input type="number" value={form.channel}
                       onChange={e => setForm(f => ({ ...f, channel: e.target.value }))}
                       placeholder="auto"
                       style={{ ...inp, width: 50, marginLeft: 3 }} />
              </label>
            )}
          </>
        )}
        <button className="btn btn-primary" style={{ ...btn, background: '#1f6feb', borderColor: '#1f6feb', color: '#fff' }}
                 disabled={busy} onClick={start}>
          <Play size={11} /> Start
        </button>
      </div>

      {/* Active sessions */}
      {sessions.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div style={{ fontSize: 10, color: '#8b949e', marginBottom: 3 }}>Active sessions</div>
          {sessions.map(s => (
            <div key={s.sid} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 10,
                                        background: s.error ? '#3a1d1d' : '#0a0e13',
                                        border: '1px solid #21262d', borderRadius: 4, padding: '3px 6px', marginBottom: 3 }}>
              {s.kind === 'wifi' ? <Wifi size={11} /> : s.kind === 'ble' ? <Bluetooth size={11} /> : <Radio size={11} />}
              <code>{s.sid}</code>
              <span style={{ color: '#c9d1d9' }}>{s.kind}</span>
              <span style={muted}>{s.center_hz ? `${(s.center_hz/1e6).toFixed(3)} MHz` : (s.extra?.argv?.[0] || s.device_id)}</span>
              {s.error ? <span style={{ color: '#f0883e', display: 'flex', alignItems: 'center', gap: 3 }}>
                  <AlertTriangle size={11} /> {s.error}
                </span> : <span style={{ color: '#3fb950' }}>● {s.n_events} events</span>}
              <span style={{ flex: 1 }} />
              <button style={btn} onClick={() => stop(s.sid)}><Square size={11} /></button>
            </div>
          ))}
        </div>
      )}
      {err && <div style={{ fontSize: 10, color: '#f0883e', marginTop: 4 }}>{err}</div>}
      <div style={{ ...muted, marginTop: 6, fontStyle: 'italic' }}>
        Passive observation only — no decryption, no IMSI-catcher behaviour. Decoded identifiers stream into the Targets tab.
      </div>
    </div>
  )
}
