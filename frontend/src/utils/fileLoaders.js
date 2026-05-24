// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * File loaders for KML, KMZ, GeoJSON, GPX and georeferenced/raster imagery.
 * Returns plain objects describing layers that can be added to the map.
 *
 * Public API:
 *   loadFiles(files): Promise<LoadedItem[]>
 *
 *   LoadedItem =
 *     | { kind: 'geojson', name, geojson, sourceFormat }
 *     | { kind: 'image',   name, dataUrl, bounds: [[s,w],[n,e]] | null, mime, needsBounds }
 *     | { kind: 'imageWorldfile', ... internal — paired with image }
 *     | { kind: 'error', name, message }
 */
import JSZip from 'jszip'
import { kml as kmlToGeoJSON, gpx as gpxToGeoJSON } from '@tmcw/togeojson'

const IMAGE_EXTS = new Set([
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'tif', 'tiff'
])
const GEO_EXTS = new Set(['geojson', 'json'])
const KML_EXTS = new Set(['kml'])
const KMZ_EXTS = new Set(['kmz'])
const GPX_EXTS = new Set(['gpx'])
const GPKG_EXTS = new Set(['gpkg'])
const WORLDFILE_EXTS = new Set(['pgw', 'jgw', 'tfw', 'wld', 'gfw', 'bpw', 'sdw'])
const DTED_EXTS = new Set(['dt0', 'dt1', 'dt2'])
const HGT_EXTS = new Set(['hgt'])
const ASC_EXTS = new Set(['asc'])

function extOf(name) {
  const m = /\.([a-z0-9]+)$/i.exec(name || '')
  return m ? m[1].toLowerCase() : ''
}

function baseOf(name) {
  return name.replace(/\.[a-z0-9]+$/i, '')
}

function readAsText(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(r.result)
    r.onerror = () => reject(new Error('Failed to read ' + file.name))
    r.readAsText(file)
  })
}

function readAsArrayBuffer(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(r.result)
    r.onerror = () => reject(new Error('Failed to read ' + file.name))
    r.readAsArrayBuffer(file)
  })
}

function readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(r.result)
    r.onerror = () => reject(new Error('Failed to read ' + file.name))
    r.readAsDataURL(file)
  })
}

function bufferToDataURL(buf, mime) {
  let binary = ''
  const bytes = new Uint8Array(buf)
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk))
  }
  return `data:${mime};base64,${btoa(binary)}`
}

function parseKMLText(text, name) {
  const xml = new DOMParser().parseFromString(text, 'application/xml')
  const err = xml.getElementsByTagName('parsererror')[0]
  if (err) throw new Error('Invalid KML: ' + err.textContent.slice(0, 80))
  const geojson = kmlToGeoJSON(xml)
  return { kind: 'geojson', name, geojson, sourceFormat: 'kml' }
}

// GeoPackage (.gpkg) — SQLite-backed OGC format. Lib is heavy (~5 MB minified),
// so we dynamic-import it; users who don't need .gpkg pay no bundle cost. If
// the dep isn't installed, surface a one-line install command rather than a
// cryptic module-not-found error.
//
// We bypass Rollup's static-import analysis via `new Function` so the bundler
// doesn't try to resolve @ngageoint/geopackage at build time — if it isn't
// installed, this file still bundles and the runtime catch block fires.
const _dynImport = new Function('p', 'return import(p)')

async function parseGeoPackage(file) {
  let mod
  try {
    mod = await _dynImport('@ngageoint/geopackage')
  } catch {
    throw new Error(
      'GeoPackage support requires @ngageoint/geopackage. Install it in frontend/ ' +
      'with `npm install @ngageoint/geopackage` and reload.'
    )
  }
  const { GeoPackageAPI } = mod
  const buf = await readAsArrayBuffer(file)
  const gp = await GeoPackageAPI.open(new Uint8Array(buf))
  const out = []
  try {
    const featureTables = gp.getFeatureTables() || []
    for (const tableName of featureTables) {
      const features = []
      const iter = gp.iterateGeoJSONFeatures(tableName)
      for (const f of iter) features.push(f)
      if (!features.length) continue
      out.push({
        kind: 'geojson',
        name: `${file.name} :: ${tableName}`,
        geojson: { type: 'FeatureCollection', features },
        sourceFormat: 'gpkg',
      })
    }
  } finally {
    try { gp.close() } catch {}
  }
  if (!out.length) {
    out.push({ kind: 'error', name: file.name,
               message: 'GeoPackage opened but contains no feature tables' })
  }
  return out
}

function parseGPXText(text, name) {
  const xml = new DOMParser().parseFromString(text, 'application/xml')
  const geojson = gpxToGeoJSON(xml)
  return { kind: 'geojson', name, geojson, sourceFormat: 'gpx' }
}

async function parseKMZ(file) {
  const buf = await readAsArrayBuffer(file)
  const zip = await JSZip.loadAsync(buf)
  // Locate primary KML (typically doc.kml). Fall back to first .kml in archive.
  let kmlEntry = zip.file('doc.kml')
  let kmlPath = 'doc.kml'
  if (!kmlEntry) {
    const candidates = []
    zip.forEach((path, entry) => {
      if (!entry.dir && /\.kml$/i.test(path)) candidates.push({ path, entry })
    })
    if (candidates[0]) {
      kmlEntry = candidates[0].entry
      kmlPath = candidates[0].path
    }
  }
  if (!kmlEntry) throw new Error('No .kml found inside KMZ')
  const text = await kmlEntry.async('string')
  const xml = new DOMParser().parseFromString(text, 'application/xml')
  const geojson = kmlToGeoJSON(xml)

  // Pull ATAK CoT types out of the KML before togeojson drops the namespaced
  // tags. ATAK writes <atom:type>a-f-G-U-C-I</atom:type> on each Placemark.
  // Walk Placemarks, build name→cotType map, then merge into feature props.
  const cotByName = new Map()
  const cotByGeom = []  // fallback by lat/lon when names collide
  const placemarks = xml.getElementsByTagName('Placemark')
  for (let i = 0; i < placemarks.length; i++) {
    const pm = placemarks[i]
    let cot = null
    // <atom:type> or any *:type child
    for (const c of Array.from(pm.children)) {
      if (/^(?:[a-z]+:)?type$/i.test(c.tagName) && /^a-[a-zA-Z]/.test(c.textContent || '')) {
        cot = c.textContent.trim(); break
      }
    }
    // <ExtendedData><Data name="type"><value>…</value></Data></ExtendedData>
    if (!cot) {
      const data = pm.querySelector('ExtendedData > Data[name="type"] > value, ExtendedData > Data[name="type"]')
      const v = data?.textContent?.trim()
      if (v && /^a-[a-zA-Z]/.test(v)) cot = v
    }
    if (!cot) continue
    const name = pm.querySelector(':scope > name')?.textContent?.trim()
    if (name) cotByName.set(name, cot)
    const coords = pm.querySelector('Point > coordinates')?.textContent?.trim()
    if (coords) {
      const [lon, lat] = coords.split(',').map(Number)
      if (Number.isFinite(lat) && Number.isFinite(lon)) cotByGeom.push({ lat, lon, cot })
    }
  }
  if (cotByName.size || cotByGeom.length) {
    for (const f of geojson?.features || []) {
      if (f.properties?.cotType) continue
      const nm = f.properties?.name || f.properties?.Name
      let cot = nm && cotByName.get(nm)
      if (!cot && f.geometry?.type === 'Point') {
        const [lon, lat] = f.geometry.coordinates
        const hit = cotByGeom.find(g =>
          Math.abs(g.lat - lat) < 1e-7 && Math.abs(g.lon - lon) < 1e-7)
        if (hit) cot = hit.cot
      }
      if (cot) f.properties = { ...(f.properties || {}), cotType: cot }
    }
  }

  // Resolve relative icon hrefs in Placemark IconStyle against zip entries.
  // ATAK exports typically reference icons as e.g. "files/atoms/atoms/a-f-G-U-C-I.png"
  // — togeojson hands us that string verbatim as properties.icon.
  const dirOf = (p) => {
    const i = p.lastIndexOf('/')
    return i >= 0 ? p.slice(0, i + 1) : ''
  }
  const kmlDir = dirOf(kmlPath)
  const iconCache = new Map()
  const resolveZipIcon = async (href) => {
    if (!href) return null
    if (/^(?:https?:|data:|blob:)/i.test(href)) return href
    if (iconCache.has(href)) return iconCache.get(href)
    const tries = [
      href,
      href.replace(/^\.?\//, ''),
      kmlDir + href.replace(/^\.?\//, ''),
      'files/' + href.replace(/^\.?\//, ''),  // ATAK 4.x layout
    ]
    let entry = null
    for (const t of tries) {
      entry = zip.file(t)
      if (entry) break
    }
    if (!entry) {
      // Last resort: case-insensitive exact filename match
      const want = href.split('/').pop().toLowerCase()
      zip.forEach((path, e) => {
        if (!entry && !e.dir && path.toLowerCase().endsWith('/' + want)) entry = e
      })
    }
    if (!entry) { iconCache.set(href, null); return null }
    const ab = await entry.async('arraybuffer')
    const ext = extOf(entry.name)
    const mime = ext === 'jpg' || ext === 'jpeg' ? 'image/jpeg'
               : ext === 'png' ? 'image/png'
               : ext === 'gif' ? 'image/gif'
               : ext === 'svg' ? 'image/svg+xml'
               : 'image/png'
    const dataUrl = bufferToDataURL(ab, mime)
    iconCache.set(href, dataUrl)
    return dataUrl
  }
  for (const f of geojson?.features || []) {
    const ic = f.properties?.icon
    if (typeof ic === 'string' && !/^(?:https?:|data:|blob:)/i.test(ic)) {
      const resolved = await resolveZipIcon(ic)
      if (resolved) f.properties.icon = resolved
    }
  }

  // Convert any embedded image overlays / referenced imagery into image layers
  const imageItems = []
  // Look for GroundOverlays in the original KML for georeferenced imagery
  const overlays = xml.getElementsByTagName('GroundOverlay')
  for (let i = 0; i < overlays.length; i++) {
    const o = overlays[i]
    const href = o.querySelector('Icon > href')?.textContent?.trim()
    const box = o.querySelector('LatLonBox')
    if (!href || !box) continue
    const north = parseFloat(box.querySelector('north')?.textContent)
    const south = parseFloat(box.querySelector('south')?.textContent)
    const east  = parseFloat(box.querySelector('east')?.textContent)
    const west  = parseFloat(box.querySelector('west')?.textContent)
    if ([north, south, east, west].some(v => !Number.isFinite(v))) continue

    // Resolve image inside zip if relative
    const entry = zip.file(href) || zip.file(href.replace(/^\.\//, ''))
    if (!entry) continue
    const imgBuf = await entry.async('arraybuffer')
    const ext = extOf(href)
    const mime = ext === 'jpg' || ext === 'jpeg' ? 'image/jpeg'
               : ext === 'png' ? 'image/png'
               : ext === 'gif' ? 'image/gif'
               : 'application/octet-stream'
    imageItems.push({
      kind: 'image',
      name: `${file.name}: ${href}`,
      dataUrl: bufferToDataURL(imgBuf, mime),
      bounds: [[south, west], [north, east]],
      mime,
      needsBounds: false,
    })
  }

  return [
    { kind: 'geojson', name: file.name, geojson, sourceFormat: 'kmz' },
    ...imageItems,
  ]
}

async function parseGeoJSON(file) {
  const text = await readAsText(file)
  const geojson = JSON.parse(text)
  return { kind: 'geojson', name: file.name, geojson, sourceFormat: 'geojson' }
}

// Worldfile: 6 lines = pixelSizeX, rotY, rotX, pixelSizeY, originX, originY
function parseWorldfileText(text) {
  const nums = text.split(/\s+/).filter(Boolean).map(Number)
  if (nums.length < 6 || nums.some(n => !Number.isFinite(n))) return null
  const [A, /*D*/, /*B*/, E, C, F] = nums
  // C,F is the centre of the upper-left pixel. We'll convert at apply time when
  // image dimensions are known.
  return { A, E, C, F }
}

async function loadImage(blobUrl) {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve({ width: img.naturalWidth, height: img.naturalHeight })
    img.onerror = () => reject(new Error('Image decode failed'))
    img.src = blobUrl
  })
}

function worldfileToBounds(wf, width, height) {
  // West (x at upper-left pixel centre minus half-pixel)
  const west  = wf.C - wf.A / 2
  const north = wf.F - wf.E / 2  // E is typically negative
  const east  = west  + wf.A * width
  const south = north + wf.E * height
  return [[Math.min(north, south), Math.min(east, west)],
          [Math.max(north, south), Math.max(east, west)]]
}

async function parseGeoTIFF(file) {
  // Lazy import — large dependency
  const { fromArrayBuffer } = await import('geotiff')
  const buf = await readAsArrayBuffer(file)
  const tiff = await fromArrayBuffer(buf)
  const image = await tiff.getImage()
  const bbox = image.getBoundingBox()  // [west, south, east, north]
  if (!bbox || bbox.length !== 4) throw new Error('GeoTIFF has no geographic bounds')

  // Render the GeoTIFF onto a canvas → data URL so Leaflet can use it
  const width = image.getWidth()
  const height = image.getHeight()
  const data = await image.readRasters({ interleave: true })
  const samples = image.getSamplesPerPixel()
  const canvas = document.createElement('canvas')
  canvas.width = width; canvas.height = height
  const ctx = canvas.getContext('2d')
  const out = ctx.createImageData(width, height)

  // Stretch to 0-255 from min/max for single-band rasters; pass-through for RGB(A)
  if (samples === 1) {
    let mn = Infinity, mx = -Infinity
    for (let i = 0; i < data.length; i++) {
      const v = data[i]; if (Number.isFinite(v)) { if (v < mn) mn = v; if (v > mx) mx = v }
    }
    const span = (mx - mn) || 1
    for (let i = 0, j = 0; i < data.length; i++, j += 4) {
      const v = Math.round(((data[i] - mn) / span) * 255)
      out.data[j] = v; out.data[j+1] = v; out.data[j+2] = v; out.data[j+3] = 255
    }
  } else if (samples === 3) {
    for (let i = 0, j = 0; i < data.length; i += 3, j += 4) {
      out.data[j] = data[i]; out.data[j+1] = data[i+1]; out.data[j+2] = data[i+2]; out.data[j+3] = 255
    }
  } else if (samples === 4) {
    out.data.set(data)
  } else {
    throw new Error(`Unsupported GeoTIFF samples per pixel: ${samples}`)
  }
  ctx.putImageData(out, 0, 0)
  const dataUrl = canvas.toDataURL('image/png')

  return {
    kind: 'image',
    name: file.name,
    dataUrl,
    bounds: [[bbox[1], bbox[0]], [bbox[3], bbox[2]]],
    mime: 'image/png',
    needsBounds: false,
    sourceFormat: 'geotiff',
  }
}

async function parseRasterImage(file, worldfileText) {
  const dataUrl = await readAsDataURL(file)
  const ext = extOf(file.name)
  let mime = file.type
  if (!mime) {
    mime = ext === 'jpg' || ext === 'jpeg' ? 'image/jpeg'
         : ext === 'png' ? 'image/png'
         : ext === 'gif' ? 'image/gif'
         : ext === 'webp' ? 'image/webp'
         : ext === 'bmp' ? 'image/bmp'
         : ext === 'svg' ? 'image/svg+xml'
         : 'application/octet-stream'
  }

  // Try worldfile if provided
  if (worldfileText) {
    try {
      const wf = parseWorldfileText(worldfileText)
      if (wf) {
        const { width, height } = await loadImage(dataUrl)
        const bounds = worldfileToBounds(wf, width, height)
        return { kind: 'image', name: file.name, dataUrl, bounds, mime, needsBounds: false }
      }
    } catch {}
  }

  // Try filename pattern: "name_S_W_N_E.png" or "name_lat1,lon1_lat2,lon2.png"
  const bn = baseOf(file.name)
  const m = /(?:^|[_-])(-?\d+\.?\d*)[_,](-?\d+\.?\d*)[_,](-?\d+\.?\d*)[_,](-?\d+\.?\d*)$/.exec(bn)
  if (m) {
    const a = parseFloat(m[1]), b = parseFloat(m[2]), c = parseFloat(m[3]), d = parseFloat(m[4])
    if ([a,b,c,d].every(Number.isFinite)) {
      // Heuristic: assume lat,lon,lat,lon (S,W,N,E) ordering
      return {
        kind: 'image', name: file.name, dataUrl,
        bounds: [[Math.min(a,c), Math.min(b,d)], [Math.max(a,c), Math.max(b,d)]],
        mime, needsBounds: false,
      }
    }
  }

  return { kind: 'image', name: file.name, dataUrl, bounds: null, mime, needsBounds: true }
}

// ── Terrain grid parsers ────────────────────────────────────────────────────

// SRTM HGT: square tile of NxN big-endian int16 samples; lat/lon from filename.
// e.g. N51W001.hgt → SW corner is (51, -1), tile spans 1°.
function parseHGT(buf, name) {
  const bytes = new Uint8Array(buf)
  const samples = bytes.length / 2
  const n = Math.round(Math.sqrt(samples))
  if (n * n * 2 !== bytes.length) throw new Error('HGT file is not a square int16 grid')

  const m = /^([NS])(\d{2})([EW])(\d{3})/i.exec(name)
  if (!m) throw new Error('HGT filename must encode lat/lon (e.g. N51W001.hgt)')
  const lat = (m[1].toUpperCase() === 'N' ? 1 : -1) * parseInt(m[2], 10)
  const lon = (m[3].toUpperCase() === 'E' ? 1 : -1) * parseInt(m[4], 10)
  const south = lat, north = lat + 1, west = lon, east = lon + 1
  const cols = n, rows = n
  const dx = (east - west) / (cols - 1)
  const dy = (north - south) / (rows - 1)
  const data = new Float32Array(cols * rows)
  let mn = Infinity, mx = -Infinity
  const dv = new DataView(buf)
  for (let i = 0; i < samples; i++) {
    const v = dv.getInt16(i * 2, false)
    const f = (v === -32768 || v === -9999) ? NaN : v
    data[i] = f
    if (Number.isFinite(f)) { if (f < mn) mn = f; if (f > mx) mx = f }
  }
  return {
    sourceFormat: 'hgt', name, bounds: [[south, west], [north, east]],
    cols, rows, dx, dy, data,
    minElev: Number.isFinite(mn) ? mn : 0, maxElev: Number.isFinite(mx) ? mx : 0,
  }
}

// DTED Level 0/1/2. Header layout:
//  - 80 bytes UHL (User Header Label) starting "UHL"
//  - 648 bytes DSI
//  - 2700 bytes ACC
// Each data record: 8-byte prefix, 2 * rows int16 BE samples, 4-byte checksum/postfix.
function parseDTED(buf, name) {
  const dv = new DataView(buf)
  const u8 = new Uint8Array(buf)
  // Verify UHL signature
  if (u8[0] !== 0x55 || u8[1] !== 0x48 || u8[2] !== 0x4C) {
    throw new Error('Not a DTED file (missing UHL header)')
  }
  // Helper: read DDDMMSSH at given offset
  const readLatLon = (off, len) => {
    let s = ''
    for (let i = 0; i < len; i++) s += String.fromCharCode(u8[off + i])
    return s.trim()
  }
  const parseDMS = (s) => {
    const hemi = s.slice(-1)
    const num = s.slice(0, -1)
    let deg, min, sec
    if (num.length === 7) {       // DDDMMSS
      deg = parseInt(num.slice(0, 3), 10)
      min = parseInt(num.slice(3, 5), 10)
      sec = parseInt(num.slice(5, 7), 10)
    } else if (num.length === 6) { // DDMMSS
      deg = parseInt(num.slice(0, 2), 10)
      min = parseInt(num.slice(2, 4), 10)
      sec = parseInt(num.slice(4, 6), 10)
    } else {
      deg = parseInt(num, 10) || 0; min = 0; sec = 0
    }
    let v = deg + min / 60 + sec / 3600
    if (hemi === 'S' || hemi === 'W') v = -v
    return v
  }

  // UHL fields
  const lonOrigin = parseDMS(readLatLon(4, 8))
  const latOrigin = parseDMS(readLatLon(12, 8))
  // Data interval: tenths of arc-seconds
  const lonIntervalTAS = parseInt(readLatLon(20, 4), 10) || 0
  const latIntervalTAS = parseInt(readLatLon(24, 4), 10) || 0
  const dx = lonIntervalTAS / 36000  // degrees per cell
  const dy = latIntervalTAS / 36000
  const cols = parseInt(readLatLon(47, 4), 10) || 0  // num lon lines
  const rows = parseInt(readLatLon(51, 4), 10) || 0  // num lat points per col

  if (!cols || !rows || !dx || !dy) throw new Error('DTED header incomplete')

  const south = latOrigin, west = lonOrigin
  const north = south + (rows - 1) * dy
  const east  = west  + (cols - 1) * dx

  const data = new Float32Array(cols * rows)
  let mn = Infinity, mx = -Infinity

  const HEADER_BYTES = 80 + 648 + 2700  // 3428
  let p = HEADER_BYTES
  for (let c = 0; c < cols; c++) {
    p += 8  // record prefix
    for (let r = 0; r < rows; r++) {
      // DTED stores rows from south to north; we store rows from north to south
      // so we can index by (north-lat)/dy.
      // Sign-magnitude int16: if MSB is set, negative.
      const hi = u8[p], lo = u8[p + 1]
      let v
      if (hi & 0x80) v = -(((hi & 0x7f) << 8) | lo)
      else v = (hi << 8) | lo
      const f = (v === -32767 || v === -32768) ? NaN : v
      const destRow = (rows - 1) - r
      data[destRow * cols + c] = f
      if (Number.isFinite(f)) { if (f < mn) mn = f; if (f > mx) mx = f }
      p += 2
    }
    p += 4  // record postfix / checksum
  }

  return {
    sourceFormat: 'dted', name,
    bounds: [[south, west], [north, east]],
    cols, rows, dx, dy, data,
    minElev: Number.isFinite(mn) ? mn : 0, maxElev: Number.isFinite(mx) ? mx : 0,
  }
}

// ESRI ASCII grid (.asc): plain text header + raster
function parseASCIIGrid(text, name) {
  const lines = text.split(/\r?\n/)
  const meta = {}
  let i = 0
  for (; i < lines.length; i++) {
    const line = lines[i].trim()
    if (!line) continue
    const m = /^([a-zA-Z_]+)\s+(\S+)/.exec(line)
    if (!m) break
    meta[m[1].toLowerCase()] = m[2]
  }
  const cols = parseInt(meta.ncols, 10)
  const rows = parseInt(meta.nrows, 10)
  const xll  = parseFloat(meta.xllcorner ?? meta.xllcenter)
  const yll  = parseFloat(meta.yllcorner ?? meta.yllcenter)
  const cell = parseFloat(meta.cellsize)
  const nodata = parseFloat(meta.nodata_value ?? '-9999')
  if (![cols, rows, xll, yll, cell].every(Number.isFinite)) {
    throw new Error('ASCII grid header incomplete')
  }
  const south = yll, north = yll + cell * rows
  const west  = xll, east  = xll + cell * cols
  const data = new Float32Array(cols * rows)
  let mn = Infinity, mx = -Infinity
  let r = 0, c = 0
  for (; i < lines.length && r < rows; i++) {
    const tokens = lines[i].trim().split(/\s+/)
    if (tokens.length === 1 && !tokens[0]) continue
    for (const tok of tokens) {
      if (c >= cols) { r++; c = 0 }
      if (r >= rows) break
      const v = parseFloat(tok)
      const f = v === nodata ? NaN : v
      data[r * cols + c] = f
      if (Number.isFinite(f)) { if (f < mn) mn = f; if (f > mx) mx = f }
      c++
    }
  }
  return {
    sourceFormat: 'asc', name,
    bounds: [[south, west], [north, east]],
    cols, rows, dx: cell, dy: cell, data,
    minElev: Number.isFinite(mn) ? mn : 0, maxElev: Number.isFinite(mx) ? mx : 0,
  }
}

/**
 * Main entry: accepts a FileList or array of File. Returns array of LoadedItem.
 * Pairs raster images with co-dropped worldfiles automatically.
 */
export async function loadFiles(fileList) {
  const files = Array.from(fileList || [])
  if (!files.length) return []

  // Index worldfiles by base filename so we can pair them with images
  const worldfileMap = new Map()
  const dataFiles = []
  for (const f of files) {
    const ext = extOf(f.name)
    if (WORLDFILE_EXTS.has(ext)) {
      try {
        const text = await readAsText(f)
        worldfileMap.set(baseOf(f.name).toLowerCase(), text)
      } catch {}
    } else {
      dataFiles.push(f)
    }
  }

  const out = []
  for (const f of dataFiles) {
    const ext = extOf(f.name)
    try {
      if (KML_EXTS.has(ext)) {
        out.push(parseKMLText(await readAsText(f), f.name))
      } else if (KMZ_EXTS.has(ext)) {
        const items = await parseKMZ(f)
        out.push(...items)
      } else if (GPX_EXTS.has(ext)) {
        out.push(parseGPXText(await readAsText(f), f.name))
      } else if (GPKG_EXTS.has(ext)) {
        out.push(...(await parseGeoPackage(f)))
      } else if (GEO_EXTS.has(ext)) {
        out.push(await parseGeoJSON(f))
      } else if (DTED_EXTS.has(ext)) {
        const buf = await readAsArrayBuffer(f)
        const grid = parseDTED(buf, f.name)
        out.push({ kind: 'terrain', name: f.name, grid })
      } else if (HGT_EXTS.has(ext)) {
        const buf = await readAsArrayBuffer(f)
        const grid = parseHGT(buf, f.name)
        out.push({ kind: 'terrain', name: f.name, grid })
      } else if (ASC_EXTS.has(ext)) {
        const text = await readAsText(f)
        const grid = parseASCIIGrid(text, f.name)
        out.push({ kind: 'terrain', name: f.name, grid })
      } else if (ext === 'tif' || ext === 'tiff') {
        // Try GeoTIFF first; fall back to plain raster if it fails
        try {
          out.push(await parseGeoTIFF(f))
        } catch {
          const wf = worldfileMap.get(baseOf(f.name).toLowerCase())
          out.push(await parseRasterImage(f, wf))
        }
      } else if (IMAGE_EXTS.has(ext) || (f.type && f.type.startsWith('image/'))) {
        const wf = worldfileMap.get(baseOf(f.name).toLowerCase())
        out.push(await parseRasterImage(f, wf))
      } else {
        out.push({ kind: 'error', name: f.name, message: `Unsupported file type: .${ext}` })
      }
    } catch (e) {
      out.push({ kind: 'error', name: f.name, message: e?.message || String(e) })
    }
  }
  return out
}

export const SUPPORTED_EXTENSIONS = [
  '.kml', '.kmz', '.geojson', '.json', '.gpx', '.gpkg',
  '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg',
  '.tif', '.tiff',
  '.pgw', '.jgw', '.tfw', '.wld',
  '.dt0', '.dt1', '.dt2', '.hgt', '.asc',
]
