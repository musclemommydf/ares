/**
 * API client for the RF Propagation Simulator backend.
 */
import axios from 'axios'

const BASE_URL = import.meta.env.VITE_API_URL || '/api/v1'

const api = axios.create({
  baseURL: BASE_URL,
  timeout: 300000, // 5 min for large coverage computations
  headers: { 'Content-Type': 'application/json' },
})

// ── Core simulation endpoints ────────────────────────────────────────────────

export async function simulateCoverage(params) {
  const { data } = await api.post('/simulate/coverage', params)
  return data
}

export async function simulateP2P(params) {
  const { data } = await api.post('/simulate/p2p', params)
  return data
}

export async function simulateBestSite(params) {
  const { data } = await api.post('/simulate/best_site', params)
  return data
}

export async function getTerrainProfile(lat1, lon1, lat2, lon2, numPoints = 512) {
  const { data } = await api.get('/terrain/profile', {
    params: { lat1, lon1, lat2, lon2, num_points: numPoints },
  })
  return data
}

export async function getElevation(lat, lon) {
  const { data } = await api.get('/terrain/elevation', { params: { lat, lon } })
  return data
}

export async function getLoBRangeEstimate(params) {
  const { data } = await api.post('/lob/range_estimate', params)
  return data
}

export async function getBuildings(lat, lon, radius_m = 500) {
  const { data } = await api.get('/terrain/buildings', { params: { lat, lon, radius_m } })
  return data
}

export async function getTerrainGrid(lat, lon, radius_km = 5, grid_size = 30) {
  const { data } = await api.get('/terrain/grid', { params: { lat, lon, radius_km, grid_size } })
  return data
}

export async function getSpaceWeather() {
  const { data } = await api.get('/space_weather')
  return data
}

export async function getAntennaCatalogue() {
  const { data } = await api.get('/antenna/catalogue')
  return data
}

export async function getPropagationModels() {
  const { data } = await api.get('/propagation/models')
  return data
}

export async function getHfMuf(lat1, lon1, lat2, lon2) {
  const { data } = await api.get('/hf/muf', { params: { lat1, lon1, lat2, lon2 } })
  return data
}

export async function purgeCache() {
  const { data } = await api.delete('/cache/purge')
  return data
}

export async function getWeather(lat, lon, datetimeUtc = null) {
  const params = { lat, lon }
  if (datetimeUtc) params.datetime_utc = datetimeUtc
  const { data } = await api.get('/weather/current', { params })
  return data
}

export async function getDevicePresets() {
  const { data } = await api.get('/devices/presets')
  return data
}

export async function getAntennaPresets() {
  const { data } = await api.get('/antenna/presets')
  return data
}

export async function simulateRoute(params) {
  const { data } = await api.post('/simulate/route', params)
  return data
}

export async function simulateMultipoint(params) {
  const { data } = await api.post('/simulate/multipoint', params)
  return data
}

export async function simulateManet(params) {
  const { data } = await api.post('/simulate/manet', params)
  return data
}

export async function simulateBestServer(params) {
  const { data } = await api.post('/simulate/best_server', params)
  return data
}

export async function simulateInterference(signalGeojson, noiseGeojson) {
  const { data } = await api.post('/simulate/interference', {
    signal_geojson: signalGeojson,
    noise_geojson: noiseGeojson,
  })
  return data
}

export async function simulateSuperLayer(layers, gridDeg = 0.001) {
  const { data } = await api.post('/simulate/super_layer', { layers, grid_deg: gridDeg })
  return data
}

export async function simulateBestSitePolygon(params) {
  const { data } = await api.post('/simulate/best_site_polygon', params)
  return data
}

export async function simulateRayTrace(params) {
  const { data } = await api.post('/simulate/ray_trace', params)
  return data
}

export async function simulateSatelliteVisibility(params) {
  const { data } = await api.post('/simulate/satellite_visibility', params)
  return data
}

export async function getMaterials() {
  const { data } = await api.get('/materials')
  return data
}

// ── WebSocket for real-time simulation ──────────────────────────────────────

export function createSimulationSocket(params, onProgress, onResult, onError) {
  const wsBase = BASE_URL.replace('/api/v1', '').replace('http', 'ws')
  const ws = new WebSocket(`${wsBase}/api/v1/ws/simulate`)

  ws.onopen = () => {
    ws.send(JSON.stringify(params))
  }

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data)
    if (msg.type === 'progress') {
      onProgress?.(msg.value, msg.message)
    } else if (msg.type === 'result') {
      onResult?.(msg)
      ws.close()
    } else if (msg.type === 'error') {
      onError?.(new Error(msg.message))
      ws.close()
    }
  }

  ws.onerror = (e) => onError?.(new Error('WebSocket connection failed'))
  ws.onclose = () => {}

  return {
    cancel: () => ws.close(),
    socket: ws,
  }
}

// ── Frequency presets ─────────────────────────────────────────────────────────

export const FREQ_PRESETS = [
  { label: 'AM Broadcast (1 MHz)', hz: 1e6 },
  { label: 'HF — 7 MHz (40m Ham)', hz: 7e6 },
  { label: 'HF — 14 MHz (20m Ham)', hz: 14e6 },
  { label: 'HF — 21 MHz (15m Ham)', hz: 21e6 },
  { label: 'HF — 27 MHz (CB Radio)', hz: 27e6 },
  { label: 'VHF — 144 MHz (2m Ham)', hz: 144e6 },
  { label: 'VHF — 162 MHz (NOAA Weather)', hz: 162.4e6 },
  { label: 'VHF — 156 MHz (Marine VHF)', hz: 156.8e6 },
  { label: 'VHF TV — 174 MHz', hz: 174e6 },
  { label: 'UHF — 433 MHz (ISM)', hz: 433e6 },
  { label: 'UHF — 462 MHz (GMRS/FRS)', hz: 462e6 },
  { label: 'UHF — 700 MHz (LTE Band 12)', hz: 700e6 },
  { label: 'UHF — 850 MHz (Cellular)', hz: 850e6 },
  { label: 'UHF — 900 MHz (ISM)', hz: 915e6 },
  { label: 'L-Band — 1.5 GHz (GPS L1)', hz: 1575.42e6 },
  { label: 'S-Band — 2.4 GHz (WiFi)', hz: 2437e6 },
  { label: 'S-Band — 3.5 GHz (5G n78)', hz: 3500e6 },
  { label: 'C-Band — 5.8 GHz (WiFi)', hz: 5800e6 },
  { label: 'X-Band — 10 GHz (Radar)', hz: 10e9 },
  { label: 'Ku-Band — 12 GHz (Sat TV)', hz: 12e9 },
  { label: 'Ka-Band — 26 GHz (5G mmWave)', hz: 26e9 },
  { label: 'Ka-Band — 28 GHz (5G mmWave)', hz: 28e9 },
  { label: 'V-Band — 60 GHz (WiGig)', hz: 60e9 },
  { label: 'W-Band — 77 GHz (Auto Radar)', hz: 77e9 },
]

// ── Power presets ─────────────────────────────────────────────────────────────

export const POWER_PRESETS = [
  { label: '1 mW (0 dBm)', dbm: 0 },
  { label: '10 mW (10 dBm)', dbm: 10 },
  { label: '100 mW (20 dBm) — LoRa ISM', dbm: 20 },
  { label: '250 mW (24 dBm)', dbm: 24 },
  { label: '1 W (30 dBm) — Handheld radio', dbm: 30 },
  { label: '2 W (33 dBm)', dbm: 33 },
  { label: '5 W (37 dBm) — Ham HT max', dbm: 37 },
  { label: '10 W (40 dBm)', dbm: 40 },
  { label: '20 W (43 dBm)', dbm: 43 },
  { label: '50 W (47 dBm) — Mobile radio', dbm: 47 },
  { label: '100 W (50 dBm) — Base station', dbm: 50 },
  { label: '500 W (57 dBm)', dbm: 57 },
  { label: '1 kW (60 dBm) — AM TX', dbm: 60 },
  { label: '10 kW (70 dBm)', dbm: 70 },
]

// ── Signal color mapping ──────────────────────────────────────────────────────

export function signalToColor(dbm, minDbm = -120) {
  // Normalize 0–1
  const norm = Math.max(0, Math.min(1, (dbm - minDbm) / (0 - minDbm)))
  if (norm > 0.8) return [6, 214, 160, 200]      // excellent: green
  if (norm > 0.6) return [132, 204, 22, 200]      // good: lime
  if (norm > 0.4) return [245, 158, 11, 200]      // fair: amber
  if (norm > 0.2) return [239, 68, 68, 200]       // poor: red
  return [100, 100, 100, 80]                        // below threshold: grey
}

export function dbmToQuality(dbm) {
  if (dbm >= -60) return { label: 'Excellent', color: '#06d6a0', bars: 5 }
  if (dbm >= -75) return { label: 'Good', color: '#84cc16', bars: 4 }
  if (dbm >= -90) return { label: 'Fair', color: '#f59e0b', bars: 3 }
  if (dbm >= -100) return { label: 'Poor', color: '#ef4444', bars: 2 }
  if (dbm >= -110) return { label: 'Very Poor', color: '#ef4444', bars: 1 }
  return { label: 'None', color: '#6b7280', bars: 0 }
}

export function formatFreq(hz) {
  if (hz >= 1e12) return `${(hz / 1e12).toFixed(2)} THz`
  if (hz >= 1e9)  return `${(hz / 1e9).toFixed(3)} GHz`
  if (hz >= 1e6)  return `${(hz / 1e6).toFixed(3)} MHz`
  if (hz >= 1e3)  return `${(hz / 1e3).toFixed(1)} kHz`
  return `${hz.toFixed(0)} Hz`
}

export function formatDistance(m) {
  if (m >= 1000) return `${(m / 1000).toFixed(1)} km`
  return `${m.toFixed(0)} m`
}

// ── Ares ATAK: server / offline packs / radio templates / net state ──────────

export async function getServerInfo() {
  const { data } = await api.get('/server/info')
  return data
}

export async function getNetStatus() {
  const { data } = await api.get('/net/status')
  return data
}

export async function listDataPacks(layer) {
  const { data } = await api.get('/packs', { params: layer ? { layer } : {} })
  return data
}

export async function downloadDataPack(params) {
  // params: { layers:[...], bbox:[w,s,e,n]|null, max_zoom?, source? }
  const { data } = await api.post('/packs/download', params)
  return data
}

export async function listPackJobs() {
  const { data } = await api.get('/packs/jobs')
  return data
}

export async function deleteDataPack(packId) {
  const { data } = await api.delete(`/packs/${encodeURIComponent(packId)}`)
  return data
}

export async function verifyDataPack(packId, deep = false) {
  const { data } = await api.post(`/packs/${encodeURIComponent(packId)}/verify`, null, { params: { deep } })
  return data
}
export async function setAtakEnabled(enabled)       { const { data } = await api.post('/atak/enabled', { enabled }); return data }

// ── SDR / DF (Workstream D) ──────────────────────────────────────────────────
export async function listSdrDevices()              { const { data } = await api.get('/sdr/devices'); return data }
export async function createSdrDevice(payload)      { const { data } = await api.post('/sdr/devices', payload); return data }
export async function updateSdrDevice(id, patch)    { const { data } = await api.put(`/sdr/devices/${encodeURIComponent(id)}`, patch); return data }
export async function deleteSdrDevice(id)           { const { data } = await api.delete(`/sdr/devices/${encodeURIComponent(id)}`); return data }
export async function testSdrDevice(id)             { const { data } = await api.post(`/sdr/devices/${encodeURIComponent(id)}/test`); return data }
export async function getSdrState()                 { const { data } = await api.get('/sdr/state'); return data }
export async function getCotTargets()               { const { data } = await api.get('/sdr/cot/targets'); return data }
export async function setCotTargets(targets)        { const { data } = await api.put('/sdr/cot/targets', { targets }); return data }
export async function pushSdrLob(payload)           { const { data } = await api.post('/sdr/lob', payload); return data }
export async function getGpsFix()                   { const { data } = await api.get('/sdr/gps'); return data }
export async function setGpsFix(fix)                { const { data } = await api.post('/sdr/gps', fix); return data }
export async function getSdrSpectrum(id, params={}) { const { data } = await api.get(`/sdr/devices/${encodeURIComponent(id)}/spectrum`, { params }); return data }
export async function getDfAccuracyEstimate(params={}) { const { data } = await api.get('/sdr/accuracy_estimate', { params }); return data }
export async function getAudioModes()               { const { data } = await api.get('/sdr/audio/modes'); return data }
export async function startSdrAudio(id, frequency_hz, mode) { const { data } = await api.post(`/sdr/devices/${encodeURIComponent(id)}/audio`, { frequency_hz, mode }); return data }
export async function getCompassModes()             { const { data } = await api.get('/sdr/compass/modes'); return data }
export async function calibrateCompass(id, body)   { const { data } = await api.post(`/sdr/devices/${encodeURIComponent(id)}/calibrate`, body); return data }
export async function getSdrPeers()                 { const { data } = await api.get('/sdr/peers'); return data }
export async function setSdrPeers(peers)           { const { data } = await api.put('/sdr/peers', { peers }); return data }
export async function addSdrPeer(url)              { const { data } = await api.post('/sdr/peers', { url }); return data }
export async function removeSdrPeer(url)           { const { data } = await api.delete('/sdr/peers', { params: { url } }); return data }
export async function simulateCoverageRaster(payload, gridSize = 48) { const { data } = await api.post('/simulate/coverage_raster', payload, { params: { grid_size: gridSize } }); return data }
// ── MANET group chat ─────────────────────────────────────────────────────────
export async function getChatMessages(room, limit = 120) { const { data } = await api.get('/chat/messages', { params: { ...(room ? { room } : {}), limit } }); return data }
export async function getChatRooms()               { const { data } = await api.get('/chat/rooms'); return data }
export async function sendChatMessage(body)        { const { data } = await api.post('/chat/send', body); return data }

/** Open a WebSocket to /sdr/stream and dispatch events to `onMessage`. Returns
 * a `{ close }` handle. Auto-reconnects with exponential backoff. */
export function createSdrSocket(onMessage, onError = () => {}) {
  let ws = null, closed = false, backoff = 1000, retryTimer = null
  const url = (() => {
    const base = (typeof window !== 'undefined' && window.location)
      ? `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}` : 'ws://localhost:8000'
    let u = `${base}/api/v1/sdr/stream`
    try { const t = (typeof localStorage !== 'undefined') && localStorage.getItem('ares.token'); if (t) u += `?token=${encodeURIComponent(t)}` } catch { /* noop */ }
    return u
  })()
  const open = () => {
    try { ws = new WebSocket(url) } catch (e) { onError(e); return scheduleRetry() }
    ws.onmessage = (ev) => {
      try { onMessage(JSON.parse(ev.data)) } catch { /* ignore non-JSON */ }
    }
    ws.onerror = onError
    ws.onclose = () => { if (!closed) scheduleRetry() }
    ws.onopen = () => { backoff = 1000 }
  }
  const scheduleRetry = () => {
    clearTimeout(retryTimer)
    retryTimer = setTimeout(open, backoff)
    backoff = Math.min(30000, backoff * 2)
  }
  open()
  return { close: () => { closed = true; clearTimeout(retryTimer); try { ws && ws.close() } catch { /* noop */ } } }
}

export async function listAtakTemplates() {
  const { data } = await api.get('/atak/templates')
  return data
}

export async function exportCoverageKmz(geojson, name = 'Ares coverage', minSignalDbm = -120) {
  const res = await api.post('/atak/export/kmz', { geojson, name, min_signal_dbm: minSignalDbm },
    { responseType: 'blob' })
  return res.data  // a Blob
}

export async function geolocateFix(observations, options = {}) {
  const { data } = await api.post('/geolocate/fix', { observations, options })
  return data
}

// ── UAS video downlink scanner / decoder / exploitation (PED) ────────────────
export async function getUasFeedTypes() { const { data } = await api.get('/uas/feed_types'); return data }
export async function getUasStatus() { const { data } = await api.get('/uas/status'); return data }
export async function getUasDecoders() { const { data } = await api.get('/uas/decoders'); return data }
export async function scanUas(params) { const { data } = await api.get('/uas/scan', { params }); return data }
export async function startUasDecode(body) { const { data } = await api.post('/uas/decode', body); return data }
export async function getUasSessions() { const { data } = await api.get('/uas/sessions'); return data }
export async function getUasSessionMetadata(sid) { const { data } = await api.get(`/uas/sessions/${sid}/metadata`); return data }
export async function deleteUasSession(sid) { const { data } = await api.delete(`/uas/sessions/${sid}`); return data }
export async function exploitUasSession(sid) { const { data } = await api.post(`/uas/sessions/${sid}/exploit`); return data }
export async function characterizeUas(body) { const { data } = await api.post('/uas/exploit/characterize', body); return data }
export async function getUasExploitStatus() { const { data } = await api.get('/uas/exploit/status'); return data }

// Remote ID / DJI DroneID
export async function getRidStatus() { const { data } = await api.get('/uas/rid/status'); return data }
export async function parseRid(hex, format = 'auto') { const { data } = await api.post('/uas/rid/parse', { hex, format }); return data }
export async function decodeRid(body) { const { data } = await api.post('/uas/rid/decode', body); return data }
export async function getRidSessions() { const { data } = await api.get('/uas/rid/sessions'); return data }
export async function getRidSessionMetadata(sid) { const { data } = await api.get(`/uas/rid/sessions/${sid}/metadata`); return data }
export async function deleteRidSession(sid) { const { data } = await api.delete(`/uas/rid/sessions/${sid}`); return data }
