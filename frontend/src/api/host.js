/**
 * Backend host resolution — lets the web UI (browser, Electron, Android) drive a
 * *remote* Ares backend, e.g. an SDR appliance on the LAN.
 *
 * Resolution order:
 *   1. a runtime override saved as localStorage['ares.host'] (set on the Connect
 *      screen) — a full origin like "http://192.168.1.50:8000";
 *   2. the build-time VITE_API_URL (its origin; a trailing "/api/v1" is stripped);
 *   3. same-origin (window.location.origin) — the case when the backend itself
 *      serves this SPA, so zero configuration is needed.
 *
 * All REST + WebSocket callers go through here, so pointing at a new host (and
 * carrying the auth token onto every socket) happens in exactly one place.
 */
const HOST_KEY = 'ares.host'
const TOKEN_KEY = 'ares.token'

const clean = (s) => String(s || '').trim().replace(/\/+$/, '')

export function getHost() {
  try { return clean(localStorage.getItem(HOST_KEY)) } catch { return '' }
}
export function setHost(h) {
  try {
    const v = clean(h)
    if (v) localStorage.setItem(HOST_KEY, v)
    else localStorage.removeItem(HOST_KEY)
  } catch { /* localStorage unavailable */ }
}

function origin() {
  const h = getHost()
  if (h) return h
  try {
    const v = import.meta.env?.VITE_API_URL
    if (v) return clean(v).replace(/\/api\/v1$/, '')
  } catch { /* no build env */ }
  if (typeof window !== 'undefined' && window.location?.origin) return window.location.origin
  return 'http://localhost:8000'
}

export function apiBase() { return origin() + '/api/v1' }
export function wsBase() { return origin().replace(/^http/i, 'ws') }   // http→ws, https→wss

export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || '' } catch { return '' }
}
export function setToken(t) {
  try { if (t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY) } catch { /* ignore */ }
}

/** Full ws(s):// URL for a backend WS path, with the auth token attached. */
export function wsUrl(path) {
  let u = wsBase() + path
  const t = getToken()
  if (t) u += (u.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(t)
  return u
}
