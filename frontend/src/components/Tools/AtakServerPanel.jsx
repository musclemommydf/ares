/**
 * AtakServerPanel — the "ATAK / Server" console (Workstream A/C).
 *
 * A modal opened from the header (the server/antenna button next to Run). Shows:
 *   - server identity / GPU / online-offline / disk (GET /api/v1/server/info)
 *   - Cursor-on-Target push targets (UDP / TCP / TLS to ATAK / WinTAK / TAK Server)
 *   - radio templates available to the ATAK plugin (GET /api/v1/atak/templates)
 *
 * Offline data packs (download form + library + verify) used to live here too. They were
 * moved to the Layer Manager — the region/cell picker and right-click "download this region"
 * make that the natural home; the bbox-draw shortcut was migrated along with them.
 */
import { useEffect, useState, useCallback } from 'react'
import { X, RefreshCw, HardDrive, Wifi, WifiOff, Cpu, Radio } from 'lucide-react'
import {
  getServerInfo, getNetStatus, listAtakTemplates, setAtakEnabled, getCotTargets, setCotTargets,
} from '../../api/client'

function fmtBytes(n) {
  if (!n && n !== 0) return '—'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0; let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`
}

function Section({ title, right, children }) {
  return (
    <div style={{ marginBottom: 18 }}>
      <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
        <div style={{ fontSize: 11, fontWeight: 700, color: '#8b949e', textTransform: 'uppercase', letterSpacing: 0.8, flex: 1 }}>{title}</div>
        {right}
      </div>
      {children}
    </div>
  )
}

const inputStyle = { background: '#0d1117', border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3', fontSize: 12, padding: '5px 7px' }
const btn = { display: 'inline-flex', alignItems: 'center', gap: 5, background: '#161b22', color: '#e6edf3', border: '1px solid #30363d', borderRadius: 5, fontSize: 12, padding: '5px 9px', cursor: 'pointer' }

export default function AtakServerPanel({ onClose }) {
  const [info, setInfo] = useState(null)
  const [net, setNet] = useState(null)
  const [templates, setTemplates] = useState([])
  const [busy, setBusy] = useState(false)
  const [errText, setErrText] = useState(null)
  // CoT push targets (the ATAK / TAK-server option set lives here)
  const [cotTargets, setCotTargetsState] = useState([])
  const [cotInput, setCotInput] = useState('')
  useEffect(() => { getCotTargets().then(r => setCotTargetsState(r.targets || [])).catch(() => {}) }, [])
  const toggleAtak = async () => {
    try { const r = await setAtakEnabled(!(info?.atak_enabled)); setInfo(prev => prev ? { ...prev, atak_enabled: r.atak_enabled } : prev) }
    catch (e) { setErrText(String(e?.response?.data?.detail || e?.message || e)) }
  }
  const applyCot = async () => {
    setErrText(null)
    const targets = cotInput.split(/[\n,]+/).map(s => s.trim()).filter(Boolean)
    try { const r = await setCotTargets(targets); setCotTargetsState(r.targets || []); setCotInput(''); setErrText(`✓ CoT targets: ${(r.targets || []).join(', ') || '(none)'}`) }
    catch (e) { setErrText(String(e?.response?.data?.detail || e?.message || e)) }
  }

  const refresh = useCallback(async () => {
    setBusy(true); setErrText(null)
    try {
      const [i, n, t] = await Promise.allSettled([
        getServerInfo(), getNetStatus(), listAtakTemplates(),
      ])
      if (i.status === 'fulfilled') setInfo(i.value)
      if (n.status === 'fulfilled') setNet(n.value)
      if (t.status === 'fulfilled') setTemplates(t.value.templates || [])
      if (i.status === 'rejected') setErrText(String(i.reason?.message || i.reason))
    } finally { setBusy(false) }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const online = info?.online
  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(1,4,9,0.6)', zIndex: 2000,
                                    display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div onClick={e => e.stopPropagation()} style={{ width: 'min(720px, 92vw)', maxHeight: '88vh', overflowY: 'auto',
        background: '#0d1117', border: '1px solid #30363d', borderRadius: 8, boxShadow: '0 10px 40px rgba(0,0,0,0.5)', padding: 20 }}>
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 16 }}>
          <Radio size={16} color="#58a6ff" />
          <div style={{ fontSize: 15, fontWeight: 700, color: '#e6edf3', marginLeft: 8, flex: 1 }}>ATAK / Server</div>
          <button style={{ ...btn, marginRight: 8 }} onClick={refresh} disabled={busy}><RefreshCw size={13} />{busy ? 'Refreshing…' : 'Refresh'}</button>
          <button style={btn} onClick={onClose}><X size={14} /></button>
        </div>

        {errText && <div style={{ background: '#3d1418', border: '1px solid #f85149', color: '#ff7b72', fontSize: 12, padding: '6px 10px', borderRadius: 5, marginBottom: 14 }}>{errText}</div>}

        {/* Server */}
        <Section title="Server" right={info && (
          <button style={{ ...btn, padding: '3px 9px', background: info.atak_enabled ? '#0f3d2e' : '#3d1414', borderColor: info.atak_enabled ? '#2ea043' : '#f85149' }} onClick={toggleAtak}>
            ATAK integration: {info.atak_enabled ? 'ON' : 'OFF'}
          </button>
        )}>
          {info && info.atak_enabled === false && (
            <div style={{ fontSize: 11, color: '#d29922', marginBottom: 6 }}>ATAK integration is OFF — data packs, radio templates, KMZ export and CoT push are disabled. Turn it on above.</div>
          )}
          {info ? (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, fontSize: 12, color: '#c9d1d9' }}>
              <span><b>{info.name}</b> v{info.version}</span>
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                {online ? <Wifi size={13} color="#3fb950" /> : <WifiOff size={13} color="#d29922" />}
                {online ? 'online' : online === false ? 'offline' : 'unknown'} ({info.network_policy})
              </span>
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                <Cpu size={13} color={info.gpu?.available ? '#3fb950' : '#6e7681'} /> GPU: {info.gpu?.available ? (info.gpu.names?.join(', ') || `${info.gpu.devices}×`) : 'none'}
              </span>
              <span>auth: {info.auth_enabled ? 'on' : 'off'}</span>
              {info.disk && <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}><HardDrive size={13} /> {fmtBytes(info.disk.free_bytes)} free</span>}
            </div>
          ) : <div style={{ fontSize: 12, color: '#8b949e' }}>connecting…</div>}
          {net && (net.last_known || net.overrides) && (
            <div style={{ fontSize: 11, color: '#6e7681', marginTop: 6 }}>
              {Object.keys(net.last_known || {}).length > 0 && <>cached cloud data: {Object.entries(net.last_known).map(([k, v]) => `${k} (${v.as_of})`).join(', ')}</>}
            </div>
          )}
        </Section>

        {/* CoT push targets — the cursor-on-target / TAK-server option set */}
        <Section title="Cursor-on-Target push (→ ATAK / WinTAK / TAK Server)">
          <div style={{ fontSize: 12, color: '#8b949e', marginBottom: 4 }}>
            LoBs and emitter fixes from the SDR console are pushed as CoT to every target below. One per line / comma-separated.<br/>
            <code>udp://239.2.3.1:6969</code> (ATAK multicast), <code>tcp://taksrv.lan:8087</code>, <code>tls://taksrv.lan:8089</code> (mutual-TLS — set <code>ARES_COT_TLS_CA/CERT/KEY</code>).
          </div>
          <div style={{ display: 'flex', gap: 6, alignItems: 'flex-start' }}>
            <textarea rows={2} style={{ ...inputStyle, flex: 1, fontFamily: 'monospace' }}
                      value={cotInput} onChange={e => setCotInput(e.target.value)}
                      placeholder={cotTargets.join('\n') || 'udp://239.2.3.1:6969'} />
            <button style={btn} onClick={applyCot}><RefreshCw size={12} /> Apply</button>
          </div>
          {cotTargets.length > 0 && <div style={{ fontSize: 11, color: '#6e7681', marginTop: 4 }}>active: {cotTargets.join(', ')}</div>}
        </Section>

        {/* How to connect ATAK */}
        <Section title="Connect an ATAK device to Ares">
          <div style={{ fontSize: 12, color: '#c9d1d9', lineHeight: 1.55 }}>
            <p style={{ margin: '0 0 8px' }}>
              The standalone ATAK plugin is tak.gov-SDK-blocked, but Ares can already push everything it produces
              (LoBs, emitter fixes, GeoChat, KMZ coverage) into ATAK via <b>Cursor-on-Target</b> — that's the standard
              ATAK input format. Pick the path that matches your network:
            </p>

            <div style={{ background: '#0b0f14', border: '1px solid #21262d', borderRadius: 6, padding: '8px 10px', marginBottom: 8 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: '#3fb950', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>
                A · Same Wi-Fi / hotspot · default multicast (easiest)
              </div>
              <ol style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>
                <li>Put the Ares laptop and the ATAK tablet on the <b>same</b> Wi-Fi or phone hotspot.</li>
                <li>In the <b>Cursor-on-Target push</b> box above, paste <code>udp://239.2.3.1:6969</code> and click Apply.</li>
                <li>On the tablet, open ATAK. No extra config — it already listens to the default SA multicast group.</li>
                <li>In Ares, drop a LoB / run a DF fix / send GeoChat. The marker should appear in ATAK within ~1 s.</li>
              </ol>
              <div style={{ fontSize: 11, color: '#8b949e', marginTop: 6 }}>
                ⚠ iPhone Personal Hotspot isolates clients — multicast may not pass. If you don't see markers, try a quick
                ping from the tablet to the laptop's IP; if ping fails, use path B.
              </div>
            </div>

            <div style={{ background: '#0b0f14', border: '1px solid #21262d', borderRadius: 6, padding: '8px 10px', marginBottom: 8 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: '#d29922', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>
                B · AP isolation / multicast blocked · direct unicast
              </div>
              <ol style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>
                <li>On the tablet, find its IP: <i>ATAK → Settings → About → IP address</i> (e.g. <code>172.20.10.3</code>).</li>
                <li>In ATAK: <i>Settings → Network Preferences → Network Connections → Manage Inputs → Add</i>. Pick
                    <b> UDP</b>, set port <code>4242</code>, save and enable it.</li>
                <li>In the <b>Cursor-on-Target push</b> box above, replace the line with <code>udp://&lt;tablet-ip&gt;:4242</code>
                    (e.g. <code>udp://172.20.10.3:4242</code>) and click Apply.</li>
                <li>Run something in Ares; marker should hit ATAK within ~1 s.</li>
              </ol>
              <div style={{ fontSize: 11, color: '#8b949e', marginTop: 6 }}>
                Note: hotspot IPs change when devices rejoin — you may need to update the target if you re-pair.
              </div>
            </div>

            <div style={{ background: '#0b0f14', border: '1px solid #21262d', borderRadius: 6, padding: '8px 10px', marginBottom: 8 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: '#58a6ff', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>
                C · You already run a TAK Server (production)
              </div>
              <ol style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>
                <li>Decide which port: <code>tcp://&lt;taksrv&gt;:8087</code> (plain) or <code>tls://&lt;taksrv&gt;:8089</code> (mutual-TLS).</li>
                <li>For TLS: set env vars before starting Ares — <code>ARES_COT_TLS_CA</code>, <code>ARES_COT_TLS_CERT</code>,
                    <code>ARES_COT_TLS_KEY</code> (PEMs from your TAK Server CA / a client certificate enrolled with it).</li>
                <li>Paste the URL in the box above and Apply.</li>
                <li>Any ATAK / WinTAK already logged into that TAK Server receives Ares' CoT automatically — no per-device input.</li>
              </ol>
            </div>

            <div style={{ background: '#0b0f14', border: '1px solid #21262d', borderRadius: 6, padding: '8px 10px' }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: '#a78bfa', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>
                Coverage maps into ATAK (no plugin needed)
              </div>
              <ol style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>
                <li>Run a coverage sim in the web UI.</li>
                <li>Export it as KMZ (header → Export → KMZ, or the API call <code>POST /api/v1/atak/export/kmz</code>).</li>
                <li>Sideload the <code>.kmz</code> onto the tablet (USB / Drive / etc.) and import in ATAK — it renders as a ground overlay.</li>
              </ol>
            </div>

            <div style={{ color: '#6e7681', fontSize: 10, marginTop: 10 }}>
              The full ATAK-CIV plugin (overlays, radial menu, Co-Opt, in-plugin DF) is tak.gov-SDK-blocked
              — see <code>atak-plugin/README.md</code> for what's needed to build it. Until then, paths A/B/C
              above deliver everything Ares produces straight into ATAK.
            </div>
          </div>
        </Section>

        {/* Radio templates */}
        <Section title={`Radio templates (${templates.length})`}>
          {templates.length === 0 ? <div style={{ fontSize: 12, color: '#8b949e' }}>None.</div> : (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
              {templates.map(t => {
                const f = t.transmitter?.frequency_hz, p = t.transmitter?.power_dbm
                return (
                  <div key={t.id} style={{ background: '#0b0f14', border: '1px solid #21262d', borderRadius: 6, padding: '6px 10px', fontSize: 12, color: '#c9d1d9' }}>
                    <div style={{ fontWeight: 600 }}>{t.name}</div>
                    <div style={{ color: '#6e7681', fontSize: 10 }}>{t.id}{f ? ` · ${(f / 1e6).toFixed(1)} MHz` : ''}{p != null ? ` · ${p} dBm` : ''}{t.antenna?.type ? ` · ${t.antenna.type}` : ''}</div>
                  </div>
                )
              })}
            </div>
          )}
          <div style={{ color: '#6e7681', fontSize: 10, marginTop: 8 }}>
            These templates are what the ARES-ATAK plugin loads. Point an ATAK device at this server: <b>{typeof window !== 'undefined' ? `${window.location.protocol}//${window.location.hostname}:8000` : 'http://&lt;this-host&gt;:8000'}</b> (Settings tab in the plugin). The plugin module lives in <code>atak-plugin/</code>.
          </div>
        </Section>
      </div>
    </div>
  )
}
