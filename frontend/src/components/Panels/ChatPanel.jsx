// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * ChatPanel — group chat over the MANET (Workstream D), the "Chat" bottom-panel tab.
 *
 * Messages propagate node-to-node over the same peer mesh as the DF feed, and out
 * as CoT **GeoChat** to ATAK/WinTAK (and inbound GeoChat from ATAK comes back) — so
 * it's one conversation across Ares nodes and ATAK clients. Rooms / channels
 * namespace it (``All`` is the default). Polls ``GET /api/v1/chat/messages``; your
 * callsign is remembered in localStorage. Click a message with coordinates to fly
 * the map to it (when wired) — for now it just shows the position.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { getChatMessages, sendChatMessage } from '../../api/client'

const inp = { background: '#0d1117', border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3', fontSize: 12, padding: '5px 7px' }
const btn = { background: '#1f6feb', border: '1px solid #1f6feb', borderRadius: 4, color: '#fff', padding: '5px 12px', cursor: 'pointer', fontSize: 12, fontWeight: 600 }

function fmtTime(t) { try { return new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) } catch { return '' } }

export default function ChatPanel({ onLocate }) {
  const [callsign, setCallsign] = useState(() => { try { return localStorage.getItem('ares.callsign') || '' } catch { return '' } })
  const [room, setRoom] = useState(() => { try { return localStorage.getItem('ares.chatroom') || 'All' } catch { return 'All' } })
  const [rooms, setRooms] = useState(['All'])
  const [messages, setMessages] = useState([])
  const [text, setText] = useState('')
  const [attachLoc, setAttachLoc] = useState(false)
  const [err, setErr] = useState(null)
  const listRef = useRef(null)
  const atBottomRef = useRef(true)

  useEffect(() => {
    let stop = false
    const tick = async () => {
      try {
        const r = await getChatMessages(room === 'All' ? undefined : room, 150)
        if (stop) return
        setMessages(r.messages || [])
        setRooms(prev => Array.from(new Set([...(prev || []), ...(r.rooms || ['All'])])))
      } catch (e) { /* ignore */ }
    }
    tick(); const h = setInterval(() => { if (!document.hidden) tick() }, 1500)   // pause while hidden
    return () => { stop = true; clearInterval(h) }
  }, [room])

  // keep scrolled to the bottom unless the user has scrolled up
  useEffect(() => {
    const el = listRef.current
    if (el && atBottomRef.current) el.scrollTop = el.scrollHeight
  }, [messages])
  const onScroll = () => { const el = listRef.current; if (el) atBottomRef.current = (el.scrollHeight - el.scrollTop - el.clientHeight) < 40 }

  const send = async () => {
    const t = text.trim(); if (!t) return
    try { localStorage.setItem('ares.callsign', callsign); localStorage.setItem('ares.chatroom', room) } catch { /* noop */ }
    let lat, lon
    if (attachLoc && navigator.geolocation) {
      try { const p = await new Promise((res, rej) => navigator.geolocation.getCurrentPosition(res, rej, { timeout: 4000 })); lat = p.coords.latitude; lon = p.coords.longitude } catch { /* no fix */ }
    }
    try {
      await sendChatMessage({ text: t, room, callsign: callsign || undefined, ...(lat != null ? { lat, lon } : {}) })
      setText(''); setErr(null); atBottomRef.current = true
      const r = await getChatMessages(room === 'All' ? undefined : room, 150); setMessages(r.messages || [])
    } catch (e) { setErr(String(e?.response?.data?.detail || e?.message || e)) }
  }

  const shown = useMemo(() => (room === 'All' ? messages : messages.filter(m => m.room === room)), [messages, room])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, fontSize: 12, color: '#e6edf3' }}>
      {/* header: room + callsign */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 8px', borderBottom: '1px solid #21262d', flexWrap: 'wrap' }}>
        <span style={{ color: '#8b949e' }}>room</span>
        <select style={{ ...inp, width: 'auto' }} value={room} onChange={e => setRoom(e.target.value)}>
          {Array.from(new Set([...rooms, room])).map(r => <option key={r} value={r}>{r}</option>)}
        </select>
        <input style={{ ...inp, width: 110 }} placeholder="new room…" onKeyDown={e => { if (e.key === 'Enter' && e.target.value.trim()) { setRoom(e.target.value.trim()); e.target.value = '' } }} />
        <span style={{ flex: 1 }} />
        <span style={{ color: '#8b949e' }}>callsign</span>
        <input style={{ ...inp, width: 130 }} placeholder="your callsign" value={callsign} onChange={e => setCallsign(e.target.value)} />
        <span style={{ fontSize: 10, color: '#6e7681' }}>over MANET mesh + CoT GeoChat ↔ ATAK</span>
      </div>
      {/* messages */}
      <div ref={listRef} onScroll={onScroll} style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '6px 8px', display: 'flex', flexDirection: 'column', gap: 4 }}>
        {shown.length === 0 && <div style={{ color: '#8b949e' }}>No messages yet. Say something — it reaches every Ares node on the mesh and ATAK clients on the CoT bus.</div>}
        {shown.map((m, i) => {
          const mine = m.via === 'local'
          return (
            <div key={m.id || i} style={{ alignSelf: mine ? 'flex-end' : 'flex-start', maxWidth: '72%',
                          background: mine ? '#16314f' : (m.via === 'cot' ? '#3d2a16' : '#161b22'), border: '1px solid #21262d',
                          borderRadius: 8, padding: '4px 8px' }}>
              <div style={{ fontSize: 10, color: '#8b949e', display: 'flex', gap: 6, alignItems: 'baseline', flexWrap: 'wrap' }}>
                <strong style={{ color: mine ? '#58a6ff' : m.via === 'cot' ? '#d29922' : '#c9d1d9' }}>{m.from_label || m.callsign || m.from_node || '?'}</strong>
                {!mine && <span title="origin node">· {m.from_node}{m.via === 'cot' ? ' (ATAK)' : m.via === 'mesh' ? ` (hop ${m.hops})` : ''}</span>}
                {m.room && room === 'All' && <span>· #{m.room}</span>}
                <span style={{ marginLeft: 'auto' }}>{fmtTime(m.t)}</span>
              </div>
              <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{m.text}</div>
              {(typeof m.lat === 'number' && typeof m.lon === 'number') && (
                <div style={{ fontSize: 10, marginTop: 2 }}>
                  📍 <button style={{ background: 'none', border: 'none', color: '#6e7bff', cursor: 'pointer', fontSize: 10, padding: 0 }}
                            onClick={() => onLocate?.(m.lat, m.lon)}>{m.lat.toFixed(5)}, {m.lon.toFixed(5)}</button>
                </div>
              )}
            </div>
          )
        })}
      </div>
      {err && <div style={{ fontSize: 11, color: '#f85149', padding: '2px 8px' }}>{err}</div>}
      {/* compose */}
      <div style={{ display: 'flex', gap: 6, alignItems: 'center', padding: '6px 8px', borderTop: '1px solid #21262d' }}>
        <label style={{ display: 'inline-flex', alignItems: 'center', gap: 3, color: attachLoc ? '#06d6a0' : '#8b949e', fontSize: 11 }} title="Attach your browser location to the message">
          <input type="checkbox" checked={attachLoc} onChange={e => setAttachLoc(e.target.checked)} /> 📍
        </label>
        <input style={{ ...inp, flex: 1 }} placeholder={`message #${room}…`} value={text}
               onChange={e => setText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }} />
        <button style={btn} onClick={send}>Send</button>
      </div>
    </div>
  )
}
