// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * ConnectGate — gates the app on a reachable, authenticated backend.
 *
 * On mount it probes GET /auth/me:
 *   • auth disabled (localhost dev / loopback)  → enter immediately, no screen;
 *   • auth enabled + valid token                → enter;
 *   • 401 (auth required, no/expired token)     → show the connect+login screen;
 *   • network error (wrong host / appliance off)→ show the connect screen.
 *
 * The screen carries a Host field, so a browser/phone/Electron pointed anywhere
 * can be retargeted at a remote Ares appliance (e.g. http://192.168.1.50:8000)
 * and authenticated. It's fully responsive (one centred card, large touch
 * targets) so it works on a laptop and a phone alike.
 */
import { useCallback, useEffect, useState } from 'react'
import { Radio, Loader2, Wifi, WifiOff, Lock } from 'lucide-react'
import { apiBase, getHost, setHost, getToken, setToken } from '../../api/host'

const wrap = {
  position: 'fixed', inset: 0, zIndex: 50000, display: 'flex',
  alignItems: 'center', justifyContent: 'center', padding: '5vh 16px',
  background: 'radial-gradient(1200px 600px at 50% -10%, #11203a 0%, #0d1117 60%)',
  color: '#e6edf3', fontFamily: "'Inter','Segoe UI',system-ui,sans-serif",
  overflowY: 'auto',
}
const card = {
  width: 'min(440px, 94vw)', background: '#0d1117', border: '1px solid #21262d',
  borderRadius: 12, padding: 'clamp(18px, 4vw, 28px)', boxShadow: '0 24px 70px rgba(0,0,0,0.6)',
}
const label = { display: 'block', fontSize: 12, color: '#8b949e', margin: '12px 0 4px' }
const input = {
  width: '100%', background: '#161b22', border: '1px solid #30363d', borderRadius: 8,
  color: '#e6edf3', padding: '11px 12px', fontSize: 16, outline: 'none',   // 16px → no iOS zoom-on-focus
}
const btn = {
  width: '100%', marginTop: 18, padding: '12px 14px', fontSize: 15, fontWeight: 700,
  background: '#1f6feb', color: '#fff', border: 'none', borderRadius: 8, cursor: 'pointer',
}

export default function ConnectGate({ children }) {
  const [phase, setPhase] = useState('checking')   // checking | ready | login | unreachable
  const [host, setHostInput] = useState(getHost() || (typeof window !== 'undefined' ? window.location.origin : ''))
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const probe = useCallback(async () => {
    setErr(''); setPhase('checking')
    try {
      const r = await fetch(apiBase() + '/auth/me', {
        headers: getToken() ? { Authorization: `Bearer ${getToken()}` } : {},
      })
      if (r.status === 401) { setPhase('login'); return }
      if (!r.ok) { setPhase('login'); return }
      const j = await r.json().catch(() => ({}))
      if (j.auth_enabled === false) { setPhase('ready'); return }
      setPhase('ready')   // auth on + /me 200 ⇒ token valid
    } catch {
      setPhase('unreachable')
    }
  }, [])

  useEffect(() => { probe() }, [probe])

  const connect = async (e) => {
    e?.preventDefault?.()
    setBusy(true); setErr('')
    setHost(host)                        // takes effect for apiBase() immediately
    try {
      // /auth/login returns a token even when auth is disabled (synthetic), so a
      // single path covers both modes.
      const r = await fetch(apiBase() + '/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      if (r.status === 401) { setErr('Invalid username or password'); setBusy(false); return }
      if (!r.ok) { setErr(`Login failed (HTTP ${r.status})`); setBusy(false); return }
      const j = await r.json()
      setToken(j.token)
      setBusy(false)
      await probe()
    } catch {
      setErr(`Cannot reach Ares at ${host || apiBase()}`); setPhase('unreachable'); setBusy(false)
    }
  }

  if (phase === 'ready') return children

  if (phase === 'checking') {
    return (
      <div style={wrap}>
        <div style={{ ...card, textAlign: 'center' }}>
          <Loader2 size={28} style={{ animation: 'ares-spin 1s linear infinite', color: '#58a6ff' }} />
          <div style={{ marginTop: 12, color: '#8b949e', fontSize: 13 }}>Connecting to Ares…</div>
          <style>{'@keyframes ares-spin{to{transform:rotate(360deg)}}'}</style>
        </div>
      </div>
    )
  }

  const unreachable = phase === 'unreachable'
  return (
    <div style={wrap}>
      <form style={card} onSubmit={connect}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
          <Radio size={26} color="#58a6ff" />
          <div>
            <div style={{ fontSize: 20, fontWeight: 800, letterSpacing: 0.3 }}>Ares</div>
            <div style={{ fontSize: 12, color: '#8b949e' }}>
              {unreachable ? 'Connect to a remote backend' : 'Sign in to control this backend'}
            </div>
          </div>
        </div>

        {unreachable && (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', background: '#3d1a1a',
                        border: '1px solid #7f1d1d', color: '#fca5a5', borderRadius: 8,
                        padding: '8px 10px', fontSize: 12, marginTop: 10 }}>
            <WifiOff size={15} /> Couldn’t reach Ares. Check the host, that the backend is running, and that you’re on the same network.
          </div>
        )}

        <label style={label}>Host</label>
        <input style={input} value={host} onChange={e => setHostInput(e.target.value)}
               placeholder="http://192.168.1.50:8000" autoCapitalize="off" autoCorrect="off"
               spellCheck={false} inputMode="url" />

        <label style={label}>Username</label>
        <input style={input} value={username} onChange={e => setUsername(e.target.value)}
               autoCapitalize="off" autoCorrect="off" spellCheck={false} autoComplete="username" />

        <label style={label}>Password</label>
        <input style={input} type="password" value={password} onChange={e => setPassword(e.target.value)}
               autoComplete="current-password" />

        {err && <div style={{ color: '#fca5a5', fontSize: 12, marginTop: 10 }}>{err}</div>}

        <button type="submit" style={{ ...btn, opacity: busy ? 0.7 : 1 }} disabled={busy}>
          {busy ? <Loader2 size={16} style={{ animation: 'ares-spin 1s linear infinite', verticalAlign: -3 }} />
                : <Wifi size={16} style={{ verticalAlign: -3 }} />}
          <span style={{ marginLeft: 8 }}>{busy ? 'Connecting…' : (unreachable ? 'Retry' : 'Connect')}</span>
        </button>

        <div style={{ display: 'flex', alignItems: 'center', gap: 6, justifyContent: 'center',
                      marginTop: 14, color: '#6e7681', fontSize: 11 }}>
          <Lock size={11} /> Token stored on this device · {apiBase()}
        </div>
        <style>{'@keyframes ares-spin{to{transform:rotate(360deg)}}'}</style>
      </form>
    </div>
  )
}
