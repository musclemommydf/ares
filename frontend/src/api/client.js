/**
 * API client for the RF Propagation Simulator backend.
 */
import axios from 'axios'
import { apiBase, wsUrl } from './host'

const api = axios.create({
  baseURL: apiBase(),
  timeout: 300000, // 5 min for large coverage computations
  headers: { 'Content-Type': 'application/json' },
})

// Re-resolve the backend base + attach the bearer token (saved at login under
// localStorage['ares.token']) on every REST request. Re-resolving means a remote
// host picked on the Connect screen takes effect without a reload, and auth works
// when ARES_AUTH is enabled (networked deployments authenticate by default). The
// WebSockets carry the same token via ?token= — see api/host.js wsUrl(). Without
// this, an auth-enabled backend answers every REST call with 401.
api.interceptors.request.use((config) => {
  try {
    config.baseURL = apiBase()
    const t = (typeof localStorage !== 'undefined') && localStorage.getItem('ares.token')
    if (t) config.headers = { ...(config.headers || {}), Authorization: `Bearer ${t}` }
  } catch { /* localStorage unavailable (SSR / privacy mode) — send unauthenticated */ }
  return config
})

// If a token expires mid-session, drop it and reload so ConnectGate re-prompts.
// Guarded on "a token was present" so the unauthenticated/gate path can't loop.
api.interceptors.response.use(
  (r) => r,
  (error) => {
    try {
      if (error?.response?.status === 401 && localStorage.getItem('ares.token')) {
        localStorage.removeItem('ares.token')
        if (typeof window !== 'undefined') window.location.reload()
      }
    } catch { /* ignore */ }
    return Promise.reject(error)
  },
)

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

export async function getTerrainGrid(lat, lon, radius_km = 5, grid_size = 30, resolution) {
  const { data } = await api.get('/terrain/grid', { params: { lat, lon, radius_km, grid_size, ...(resolution ? { resolution } : {}) } })
  return data
}

// ── Bundled DSP + tracker + decoder ──────────────────────────────────────────
export async function dfChannelize(payload) { return (await api.post('/df/channelize', payload)).data }
export async function dfModclass(payload) { return (await api.post('/df/modclass', payload)).data }
export async function dfMultibaseline(payload) { return (await api.post('/df/multibaseline', payload)).data }
export async function dfMovingPlatform(payload) { return (await api.post('/df/moving_platform', payload)).data }
export async function dfWatchlists() { return (await api.get('/df/watchlists')).data }
export async function dfGmphdStep(observations) { return (await api.post('/df/gmphd/step', { observations })).data }
export async function dfGmphdState() { return (await api.get('/df/gmphd/state')).data }
export async function dfGmphdReset() { return (await api.post('/df/gmphd/reset')).data }
export async function dfSpoofCheck(payload) { return (await api.post('/df/spoof_check', payload)).data }
export async function dfModeSDecode(messages_hex) { return (await api.post('/df/decoders/mode_s', { messages_hex })).data }
export async function dfReplayList() { return (await api.get('/df/replay/list')).data }
export async function dfTimeSync() { return (await api.get('/df/time_sync')).data }
export async function dfHealth() { return (await api.get('/df/health')).data }
export async function dfTrackArchive(id) { return (await api.get(`/df/track_archive/${encodeURIComponent(id)}`)).data }
export async function dfTrackArchiveList() { return (await api.get('/df/track_archive')).data }
export async function dfRecordings() { return (await api.get('/df/recordings')).data }
export async function dfGnuradioStatus() { return (await api.get('/df/gnuradio/status')).data }
// Re-export the axios instance so components that use it directly (Heatmap, EmitterDetailCard) keep working.
export default api

// ── DF / DoA bundled pipeline ────────────────────────────────────────────────
export async function getDfDrivers() {
  const { data } = await api.get('/df/drivers'); return data
}
// Live IQ→bearing DF: instantiate a registry driver in-process (no external daemon).
export async function startLiveDf(body) {
  const { data } = await api.post('/df/live/start', body); return data
}
export async function stopLiveDf(id, remove = false) {
  const { data } = await api.post(`/df/live/${encodeURIComponent(id)}/stop`, null, { params: { remove } }); return data
}
// Re-configure an existing live-DF device in place (same id) and re-spawn it.
export async function updateLiveDf(id, body) {
  const { data } = await api.put(`/df/live/${encodeURIComponent(id)}`, body); return data
}
export async function listLiveDf() {
  const { data } = await api.get('/df/live'); return data
}
export async function dfPseudoSpectrum(payload) {
  const { data } = await api.post('/df/pseudo_spectrum', payload); return data
}
export async function dfSourceCount(payload) {
  const { data } = await api.post('/df/source_count', payload); return data
}
export async function dfTrackerStep(observations) {
  const { data } = await api.post('/df/tracker/step', { observations }); return data
}
export async function dfTrackerState() {
  const { data } = await api.get('/df/tracker/state'); return data
}
export async function dfTrackerReset() {
  const { data } = await api.post('/df/tracker/reset'); return data
}
export async function dfFuse(payload) {
  const { data } = await api.post('/df/fuse', payload); return data
}
export async function dfTaskingList() { return (await api.get('/df/tasking')).data }
export async function dfTaskingAdd(entry) { return (await api.post('/df/tasking', entry)).data }
export async function dfTaskingUpdate(id, entry) { return (await api.put(`/df/tasking/${id}`, entry)).data }
export async function dfTaskingDelete(id) { return (await api.delete(`/df/tasking/${id}`)).data }
export async function dfAntennas() { return (await api.get('/df/antennas')).data }
export async function dfSaveAntenna(profile) { return (await api.post('/df/antennas', profile)).data }
export async function dfDeleteAntenna(id) { return (await api.delete(`/df/antennas/${encodeURIComponent(id)}`)).data }
export async function dfArrayEstimate(payload) { return (await api.post('/df/array/estimate', payload)).data }
export async function dfLiveCalibrate(id) { return (await api.post(`/df/live/${encodeURIComponent(id)}/calibrate`)).data }
export async function dfCalibrationSave(payload) { return (await api.post('/df/calibration/save', payload)).data }
export async function dfCalibrationLoad(deviceId) { return (await api.get(`/df/calibration/${deviceId}`)).data }
export async function passiveRadarProcess(payload) {
  const { data } = await api.post('/df/passive_radar/process', payload); return data
}
export async function passiveRadarIlluminators(region) {
  const { data } = await api.get('/df/passive_radar/illuminators', { params: region ? { region } : {} })
  return data
}
export async function missionExport(payload) {
  // Returns raw bytes
  const resp = await api.post('/df/mission/export', payload, { responseType: 'arraybuffer' })
  return resp.data
}

// GeoJSON FeatureCollection of LineStrings at every `interval_m` of elevation
// within ±radius_km of (lat, lon). Backend uses matplotlib's contour generator
// over a sampled elevation grid; intervals/levels echoed in `metadata`.
export async function getTerrainContours(lat, lon, radius_km = 10, interval_m = 50, grid_size = 80, resolution) {
  const { data } = await api.get('/terrain/contours', {
    params: { lat, lon, radius_km, interval_m, grid_size, ...(resolution ? { resolution } : {}) },
  })
  return data
}

// Pure-geometry line-of-sight viewshed (no RF). Returns a FeatureCollection with
// one Polygon feature outlining the visible region. The earth_curvature flag
// uses k=4/3 effective-radius refraction for the LoS ray.
export async function getViewshed(params) {
  const { data } = await api.post('/viewshed', params)
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
  const ws = new WebSocket(wsUrl('/api/v1/ws/simulate'))

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

// ── Ares: server / offline packs / radio templates / net state ──────────────

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
export async function updateDataPack(packId, params = {}) {
  // re-fetch a fresher version of an installed pack (manual only — never automatic)
  const { data } = await api.post(`/packs/${encodeURIComponent(packId)}/update`, null, { params })
  return data
}

// ── Named regions (state/country) → bbox, and "download all mapping data for it" ──
export async function searchRegions(q, limit = 40) {
  const { data } = await api.get('/regions', { params: { ...(q ? { q } : {}), limit } })
  return data
}
export async function regionAtPoint(lat, lon) {
  const { data } = await api.get('/regions/at', { params: { lat, lon } })
  return data
}

// ── OSINT feeds (DeepState / GDELT / ADS-B / FIRMS / ACLED / AIS / scrape / generic) ──
export async function getOsintFeeds() {
  const { data } = await api.get('/osint/feeds'); return data
}
export async function fetchOsintFeed(id, body = {}) {
  const { data } = await api.post(`/osint/feeds/${encodeURIComponent(id)}/fetch`, body); return data
}
export async function addOsintFeed(body) {
  const { data } = await api.post('/osint/feeds', body); return data
}
export async function deleteOsintFeed(id) {
  const { data } = await api.delete(`/osint/feeds/${encodeURIComponent(id)}`); return data
}
export async function setOsintFeedConfig(id, body) {
  const { data } = await api.put(`/osint/feeds/${encodeURIComponent(id)}/config`, body); return data
}
export async function downloadRegionData(code, params = {}) {
  // params: { layers?:[...], max_zoom?, source? } — default layers = terrain+imagery+buildings+osm+clutter
  const { data } = await api.post(`/regions/${encodeURIComponent(code)}/download`, params)
  return data
}
export async function estimateRegionDownload(code, params = {}) {
  // returns { region, per_layer: {layer: {tiles, bytes, note, exceeds_cap?, max_zoom?}}, total_bytes, ... }
  const { data } = await api.post(`/regions/${encodeURIComponent(code)}/estimate`, params)
  return data
}
export async function listRegionCells(code) {
  // 0.5° sub-cells covering a parent region's bbox (z17-friendly download units).
  const { data } = await api.get(`/regions/${encodeURIComponent(code)}/cells`)
  return data
}
export async function estimateBboxDownload(bbox, params = {}) {
  // Freeform bbox (drawn on the map). Same response shape as estimateRegionDownload.
  const { data } = await api.post('/regions/by-bbox/estimate', { bbox, ...params })
  return data
}
export async function downloadBboxData(bbox, params = {}) {
  // Stage pack jobs for a freeform bbox — same persistent library as named-region downloads.
  const { data } = await api.post('/regions/by-bbox/download', { bbox, ...params })
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
export async function getGpsSource()                { const { data } = await api.get('/sdr/gps/source'); return data }
export async function setGpsSource(body)            { const { data } = await api.post('/sdr/gps/source', body); return data }
export async function getDfIqBackend()              { const { data } = await api.get('/df/iq_backend'); return data }
export async function solveAoaLive(body)            { const { data } = await api.post('/df/aoa_live', body); return data }
export async function getSdrSpectrum(id, params={}) { const { data } = await api.get(`/sdr/devices/${encodeURIComponent(id)}/spectrum`, { params }); return data }
export async function getDfAccuracyEstimate(params={}) { const { data } = await api.get('/sdr/accuracy_estimate', { params }); return data }
export async function getAudioModes()               { const { data } = await api.get('/sdr/audio/modes'); return data }
export async function identifyPtt(body)              { const { data } = await api.post('/sdr/audio/identify_ptt', body); return data }
export async function startSdrAudio(id, frequency_hz, mode) { const { data } = await api.post(`/sdr/devices/${encodeURIComponent(id)}/audio`, { frequency_hz, mode }); return data }
export async function getCompassModes()             { const { data } = await api.get('/sdr/compass/modes'); return data }
export async function calibrateCompass(id, body)   { const { data } = await api.post(`/sdr/devices/${encodeURIComponent(id)}/calibrate`, body); return data }
export async function listSdrNics()                 { const { data } = await api.get('/sdr/nic'); return data }
export async function createSdrNic(payload)         { const { data } = await api.post('/sdr/nic', payload); return data }
export async function deleteSdrNic(id)              { const { data } = await api.delete(`/sdr/nic/${encodeURIComponent(id)}`); return data }
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
// Human-readable cause for a WebSocket close code (RFC 6455 + common usage), so
// the SDR console's event log shows *why* the stream dropped, not just "error".
const WS_CLOSE_REASON = {
  1000: 'normal close', 1001: 'endpoint going away', 1002: 'protocol error',
  1003: 'unsupported data', 1005: 'no status received',
  1006: 'abnormal close — server unreachable or network dropped (no close frame)',
  1007: 'invalid frame payload', 1008: 'policy violation — auth rejected or token expired?',
  1009: 'message too big', 1010: 'required extension missing', 1011: 'server internal error',
  1012: 'server restarting', 1013: 'try again later', 1015: 'TLS handshake failed',
}

export function createSdrSocket(onMessage, onError = () => {}) {
  let ws = null, closed = false, backoff = 1000, retryTimer = null
  const url = wsUrl('/api/v1/sdr/stream')
  const open = () => {
    try { ws = new WebSocket(url) } catch (e) { onError({ kind: 'exception', detail: String(e?.message || e) }); return scheduleRetry() }
    ws.onmessage = (ev) => {
      try { onMessage(JSON.parse(ev.data)) } catch { /* ignore non-JSON */ }
    }
    // The browser `error` event carries no useful detail (intentionally), so the
    // real cause comes from `close` (code + reason); report it from there.
    ws.onerror = () => onError({ kind: 'error', detail: 'socket error' })
    ws.onclose = (ev) => {
      if (closed) return
      const code = ev?.code
      const reason = (ev?.reason || '').trim() || WS_CLOSE_REASON[code] || 'connection closed'
      onError({ kind: 'close', code, reason, wasClean: !!ev?.wasClean,
                detail: code != null ? `code ${code}: ${reason}` : reason })
      scheduleRetry()
    }
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

// ── UAS video downlink scanner / decoder / exploitation ─────────────────────
export async function getUasFeedTypes() { const { data } = await api.get('/uas/feed_types'); return data }
export async function getUasStatus() { const { data } = await api.get('/uas/status'); return data }
export async function getUasDecoders() { const { data } = await api.get('/uas/decoders'); return data }
export async function scanUas(params) { const { data } = await api.get('/uas/scan', { params }); return data }
export async function startUasDecode(body) { const { data } = await api.post('/uas/decode', body); return data }
export async function redemodUasSession(sid, body) { const { data } = await api.post(`/uas/sessions/${sid}/redemod`, body); return data }
export async function resetUasMaxHold(maxhold_key='default') { const { data } = await api.post('/uas/scan/maxhold/reset', null, { params: { maxhold_key } }); return data }
export async function getUasMaxHold(maxhold_key='default') { const { data } = await api.get('/uas/scan/maxhold', { params: { maxhold_key } }); return data }

// ── Algorithms tab (single-channel DF & multi-method fusion, all in-process) ─
export async function algorithmsList()          { const { data } = await api.get('/algorithms/methods'); return data }
export async function algorithmsFeasibility(b)  { const { data } = await api.post('/algorithms/feasibility', b); return data }
export async function algoRssPathLoss(b)        { const { data } = await api.post('/algorithms/rss_path_loss', b); return data }
export async function algoRssGradient(b)        { const { data } = await api.post('/algorithms/rss_gradient', b); return data }
export async function algoDopplerCpa(b)         { const { data } = await api.post('/algorithms/doppler_cpa', b); return data }
export async function algoFdoaTrack(b)          { const { data } = await api.post('/algorithms/fdoa_track', b); return data }
export async function algoSyntheticAperture(b)  { const { data } = await api.post('/algorithms/synthetic_aperture', b); return data }
export async function algoPhaseInterferometry(b){ const { data } = await api.post('/algorithms/phase_interferometry', b); return data }
export async function algoTdoaMultiReceiver(b)  { const { data } = await api.post('/algorithms/tdoa_multi_receiver', b); return data }
export async function algoMlGridFusion(b)       { const { data } = await api.post('/algorithms/ml_grid_fusion', b); return data }
export async function algoEkfTrack(b)           { const { data } = await api.post('/algorithms/ekf_track', b); return data }

// ── Targets tab (per-identifier tracker) ─────────────────────────────────────
export async function listTargets(params)        { const { data } = await api.get('/targets', { params }); return data }
export async function getTarget(kind, value, opts={})    { const { data } = await api.get(`/targets/${encodeURIComponent(kind)}/${encodeURIComponent(value)}`, { params: opts }); return data }
export async function getTargetRange(kind, value)        { const { data } = await api.get(`/targets/${encodeURIComponent(kind)}/${encodeURIComponent(value)}/range`); return data }
export async function recomputeTargetFix(kind, value)    { const { data } = await api.post(`/targets/${encodeURIComponent(kind)}/${encodeURIComponent(value)}/fix`); return data }
export async function forgetTarget(kind, value)          { const { data } = await api.delete(`/targets/${encodeURIComponent(kind)}/${encodeURIComponent(value)}`); return data }
export async function pushTargetObservation(kind, value, body) { const { data } = await api.post(`/targets/${encodeURIComponent(kind)}/${encodeURIComponent(value)}/observe`, body); return data }
export async function getTargetKinds()           { const { data } = await api.get('/targets/kinds'); return data }

// ── Cellular / WiFi / BLE passive monitors ───────────────────────────────────
export async function cellularCapabilities()     { const { data } = await api.get('/cellular/capabilities'); return data }
export async function startCellular(body)        { const { data } = await api.post('/cellular/start', body); return data }
export async function listCellularSessions()     { const { data } = await api.get('/cellular/sessions'); return data }
export async function getCellularSession(sid)    { const { data } = await api.get(`/cellular/sessions/${sid}`); return data }
export async function getCellularEvents(sid, since=0, limit=200) { const { data } = await api.get(`/cellular/sessions/${sid}/events`, { params: { since, limit } }); return data }
export async function stopCellularSession(sid)   { const { data } = await api.delete(`/cellular/sessions/${sid}`); return data }
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
