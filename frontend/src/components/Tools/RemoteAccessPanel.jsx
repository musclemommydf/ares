/**
 * RemoteAccessPanel — turn remote control on/off entirely from the desktop app.
 *
 * Talks to the Electron main process (window.aresDesktop, exposed by preload):
 *   • getRemote() → { enabled, hasPassword, port, lanIps, urls }
 *   • setRemote({enabled, password}) → relaunches the bundled backend bound to
 *     0.0.0.0 with auth + the password (or back to loopback), returns fresh status.
 *
 * When enabled it shows the URL(s) + a QR a phone can scan to connect. In a plain
 * browser (no Electron bridge) it explains that setup lives in the desktop app —
 * which is moot, because reaching this in a browser means you're already remote.
 */
import { useEffect, useState } from 'react'
import { X, Wifi, Smartphone, Copy, Check, Loader2, ShieldCheck, ShieldOff } from 'lucide-react'
import { QRCodeSVG } from 'qrcode.react'

const desk = (typeof window !== 'undefined') ? window.aresDesktop : null

const overlay = {
  position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)', zIndex: 20000,
  display: 'flex', alignItems: 'flex-start', justifyContent: 'center', padding: '6vh 16px', overflowY: 'auto',
}
const card = {
  width: 'min(460px, 96vw)', background: '#0d1117', border: '1px solid #30363d', borderRadius: 12,
  color: '#e6edf3', boxShadow: '0 24px 70px rgba(0,0,0,0.7)', overflow: 'hidden',
}
const body = { padding: 'clamp(16px,4vw,22px)' }
const label = { display: 'block', fontSize: 12, color: '#8b949e', margin: '14px 0 4px' }
const input = {
  width: '100%', background: '#161b22', border: '1px solid #30363d', borderRadius: 8,
  color: '#e6edf3', padding: '11px 12px', fontSize: 16, outline: 'none',
}
const primary = {
  width: '100%', marginTop: 18, padding: '12px 14px', fontSize: 15, fontWeight: 700,
  border: 'none', borderRadius: 8, cursor: 'pointer', color: '#fff',
}

export default function RemoteAccessPanel({ onClose }) {
  const [status, setStatus] = useState(null)
  const [enabled, setEnabled] = useState(false)
  const [password, setPassword] = useState('')
  const [sel, setSel] = useState(0)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!desk) return
    desk.getRemote()
      .then((s) => { setStatus(s); setEnabled(!!s.enabled) })
      .catch((e) => setErr(String(e?.message || e)))
  }, [])

  const apply = async (nextEnabled) => {
    if (nextEnabled && !password && !status?.hasPassword) {
      setErr('Set a password first — it protects the connection.'); return
    }
    setBusy(true); setErr('')
    try {
      const s = await desk.setRemote({ enabled: nextEnabled, password })
      setStatus(s); setEnabled(!!s.enabled); setPassword(''); setSel(0)
    } catch (e) {
      setErr(String(e?.message || e))
    }
    setBusy(false)
  }

  const urls = status?.urls || []
  const url = urls[sel] || urls[0] || ''
  const copy = () => { try { navigator.clipboard.writeText(url); setCopied(true); setTimeout(() => setCopied(false), 1500) } catch { /* noop */ } }

  return (
    <div style={overlay} onClick={onClose}>
      <div style={card} onClick={(e) => e.stopPropagation()}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px', borderBottom: '1px solid #21262d' }}>
          <Smartphone size={18} color="#58a6ff" />
          <div style={{ fontSize: 15, fontWeight: 700, flex: 1 }}>Remote access</div>
          <button className="btn btn-ghost" style={{ padding: '2px 6px' }} onClick={onClose}><X size={15} /></button>
        </div>

        <div style={body}>
          {!desk && (
            <div style={{ fontSize: 13, color: '#c9d1d9', lineHeight: 1.5 }}>
              Remote access is configured from the <b>Ares desktop app</b> (it manages the backend).
              You’re seeing this in a browser — which means you’re already connected remotely. 🎉
            </div>
          )}

          {desk && (
            <>
              <div style={{ fontSize: 12.5, color: '#8b949e', lineHeight: 1.5 }}>
                Let a phone or another laptop drive this Ares over the network. The backend is
                exposed on all interfaces with a password; your desktop stays signed in automatically.
              </div>

              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 16,
                            padding: '10px 12px', background: '#161b22', border: '1px solid #21262d', borderRadius: 8 }}>
                {enabled ? <ShieldCheck size={18} color="#06d6a0" /> : <ShieldOff size={18} color="#6e7681" />}
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 600 }}>{enabled ? 'Remote access is ON' : 'Remote access is OFF'}</div>
                  <div style={{ fontSize: 11, color: '#6e7681' }}>{enabled ? 'Reachable from your network' : 'Loopback only (this computer)'}</div>
                </div>
              </div>

              <label style={label}>{status?.hasPassword ? 'Password (leave blank to keep current)' : 'Set a password'}</label>
              <input style={input} type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                     placeholder="admin password" autoComplete="new-password" />
              <div style={{ fontSize: 11, color: '#6e7681', marginTop: 4 }}>Username on the phone is <b>admin</b>.</div>

              {err && <div style={{ color: '#fca5a5', fontSize: 12, marginTop: 10 }}>{err}</div>}

              {!enabled ? (
                <button style={{ ...primary, background: '#1f6feb', opacity: busy ? 0.7 : 1 }} disabled={busy}
                        onClick={() => apply(true)}>
                  {busy ? <Loader2 size={16} style={{ animation: 'ares-spin 1s linear infinite', verticalAlign: -3 }} />
                        : <Wifi size={16} style={{ verticalAlign: -3 }} />}
                  <span style={{ marginLeft: 8 }}>{busy ? 'Applying…' : 'Enable remote access'}</span>
                </button>
              ) : (
                <div style={{ display: 'flex', gap: 8 }}>
                  <button style={{ ...primary, flex: 1, background: '#21262d', border: '1px solid #30363d', color: '#e6edf3', opacity: busy ? 0.7 : 1 }}
                          disabled={busy} onClick={() => apply(true)}>Update password</button>
                  <button style={{ ...primary, flex: 1, background: '#3d1a1a', border: '1px solid #7f1d1d', color: '#fca5a5', opacity: busy ? 0.7 : 1 }}
                          disabled={busy} onClick={() => apply(false)}>Turn off</button>
                </div>
              )}
              {busy && <div style={{ fontSize: 11, color: '#6e7681', marginTop: 8 }}>Restarting the radio backend — a live capture will blip for a few seconds.</div>}

              {enabled && url && (
                <div style={{ marginTop: 20, paddingTop: 16, borderTop: '1px solid #21262d' }}>
                  <div style={{ fontSize: 12, color: '#8b949e', marginBottom: 10 }}>Open this on your phone (same network), then sign in as <b>admin</b>:</div>
                  <div style={{ display: 'flex', gap: 16, alignItems: 'center', flexWrap: 'wrap' }}>
                    <div style={{ background: '#fff', padding: 8, borderRadius: 8 }}>
                      <QRCodeSVG value={url} size={132} includeMargin={false} />
                    </div>
                    <div style={{ flex: 1, minWidth: 180 }}>
                      {urls.length > 1 && (
                        <select value={sel} onChange={(e) => setSel(Number(e.target.value))}
                                style={{ ...input, padding: '8px 10px', fontSize: 13, marginBottom: 8 }}>
                          {urls.map((u, i) => <option key={u} value={i}>{u}</option>)}
                        </select>
                      )}
                      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                        <code style={{ flex: 1, fontSize: 13, color: '#58a6ff', wordBreak: 'break-all',
                                       background: '#161b22', border: '1px solid #21262d', borderRadius: 6, padding: '6px 8px' }}>{url}</code>
                        <button className="btn btn-ghost" style={{ padding: '6px 8px' }} onClick={copy} title="Copy">
                          {copied ? <Check size={14} color="#06d6a0" /> : <Copy size={14} />}
                        </button>
                      </div>
                      <div style={{ fontSize: 11, color: '#6e7681', marginTop: 8 }}>Scan the QR, or type the address into the phone’s browser.</div>
                    </div>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
        <style>{'@keyframes ares-spin{to{transform:rotate(360deg)}}'}</style>
      </div>
    </div>
  )
}
