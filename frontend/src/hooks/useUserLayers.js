/**
 * useUserLayers — single source of truth for user-added map layers.
 *
 * Owns metadata for every imported KML / KMZ / GeoJSON / GPX / image / tile
 * source / DTED terrain grid, plus features drawn via the ATAK-style draw tools.
 * The actual Leaflet objects live in a private ref so they stay out of React
 * state but can still be addressed by id.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import L from 'leaflet'
import { findCotType, cotToSidc } from '../utils/cotMapping'
import { makeSidcIcon } from '../components/Map/NatoSymbols'

const MAX_ZOOM = 22

let SEQ = 1
const nextId = () => `ul_${Date.now().toString(36)}_${(SEQ++).toString(36)}`

function ensurePane(map, paneId, zIndex) {
  if (!map.getPane(paneId)) {
    const p = map.createPane(paneId)
    p.style.zIndex = String(zIndex)
  }
  return paneId
}

function makeMarkerIcon(color = '#06d6a0') {
  return L.divIcon({
    className: '',
    html: `<div style="width:14px;height:14px;border-radius:50%;background:${color};border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,0.5);"></div>`,
    iconSize: [14, 14], iconAnchor: [7, 7],
  })
}

function makeIconUrlMarker(url) {
  return L.icon({ iconUrl: url, iconSize: [22, 22], iconAnchor: [11, 11] })
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[<>&"']/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c]))
}

function vectorStyle(color = '#06d6a0', opacity = 0.85) {
  return { color, weight: 2, opacity, fillOpacity: opacity * 0.25 }
}

// Pull simplestyle / KML-style properties from a feature and turn them into
// a Leaflet path style. Falls back to the layer's default colour.
function featurePathStyle(feature, defaultColor, defaultOpacity) {
  const p = feature?.properties || {}
  const ug = p.uas_glx || p.rid_glx                       // UAS / Remote-ID feature tag, if any
  const isLine = feature?.geometry?.type === 'LineString' || feature?.geometry?.type === 'MultiLineString'
  const stroke = p.stroke || p['stroke-color'] || p.color || defaultColor
  const strokeOpacity = p['stroke-opacity'] != null ? Number(p['stroke-opacity']) : (ug ? 0.95 : defaultOpacity)
  const strokeWidth = p['stroke-width'] != null ? Math.max(1, Number(p['stroke-width'])) : 2
  const fill = p.fill || p['fill-color'] || (p.color || stroke)
  const fillOpacity = p['fill-opacity'] != null ? Number(p['fill-opacity']) : (isLine ? 0 : (ug ? 0.14 : defaultOpacity * 0.3))
  const st = { color: stroke, opacity: strokeOpacity, weight: strokeWidth, fillColor: fill, fillOpacity }
  if (ug === 'los' || ug === 'platform_track') st.dashArray = '6 4'
  return st
}

function pickPlacemarkLabelColor(feature, fallback) {
  const p = feature?.properties || {}
  return p['icon-color'] || p['label-color'] || p.stroke || p.fill || fallback
}

export function useUserLayers() {
  const mapRef = useRef(null)
  const drawCtrlRef = useRef(null)
  const layersRef = useRef(new Map())          // id → { meta, leafletLayer? }
  const terrainGridsRef = useRef(new Map())    // id → { bounds, cols, rows, dx, dy, data }
  const pendingRestoreRef = useRef(null)       // session queued for first bind
  const importSessionRef = useRef(null)        // forward ref to importSession (defined later)
  const exportSessionRef = useRef(null)        // forward ref to exportSession (defined later)
  const selectedRef = useRef(null)             // { kind: 'layer'|'drawn', id } — last feature clicked on the map

  const [layers, setLayers] = useState([])
  const [drawnFeatures, setDrawnFeatures] = useState([])
  // GeoJSON FeatureCollection of the drawn features — so the 3D globe can render
  // the same drawings the 2D map shows (the metadata in `drawnFeatures` has no geometry).
  const [drawnGeoJSON, setDrawnGeoJSON] = useState({ type: 'FeatureCollection', features: [] })
  // Features drawn on the 3D globe (the Leaflet draw controller can't run there).
  // Both maps render this collection too — so 3D-drawn annotations carry over to 2D.
  const [globeDrawnGeoJSON, setGlobeDrawnGeoJSON] = useState({ type: 'FeatureCollection', features: [] })
  const addGlobeDrawing = useCallback((feature) => {
    if (!feature) return
    setGlobeDrawnGeoJSON((fc) => ({ type: 'FeatureCollection', features: [...fc.features, feature] }))
  }, [])
  const removeGlobeDrawing = useCallback((id) => {
    setGlobeDrawnGeoJSON((fc) => ({ type: 'FeatureCollection', features: fc.features.filter((f) => f?.properties?.mv_id !== id) }))
  }, [])
  const clearGlobeDrawings = useCallback(() => setGlobeDrawnGeoJSON({ type: 'FeatureCollection', features: [] }), [])

  const refreshLayers = useCallback(() => {
    const list = Array.from(layersRef.current.values()).map(e => ({ ...e.meta }))
    setLayers(list)
  }, [])

  const drawUnsubRef = useRef(null)

  const applyZoomVisibility = useCallback(() => {
    const map = mapRef.current
    if (!map) return
    const z = map.getZoom()
    layersRef.current.forEach((entry) => {
      const m = entry.meta
      if (!entry.leafletLayer) return
      if (m.kind !== 'image' && m.kind !== 'tiles') return
      const inRange = z >= (m.minZoom ?? 0) && z <= (m.maxZoom ?? MAX_ZOOM)
      const shouldShow = m.visible && inRange
      const isOnMap = map.hasLayer(entry.leafletLayer)
      if (shouldShow && !isOnMap) entry.leafletLayer.addTo(map)
      else if (!shouldShow && isOnMap) entry.leafletLayer.remove()
    })
  }, [])

  const bindMap = useCallback((map, drawCtrl) => {
    // Idempotent: ignore re-binds with the same instances
    if (mapRef.current === map && drawCtrlRef.current === drawCtrl) return

    // 2D⇄3D round-trips replace the whole Leaflet map (and the draw controller). The Leaflet
    // layer objects in layersRef belong to the *old* map, and the new draw controller starts
    // empty — so snapshot what's loaded now and rebuild it on the new map, silently (no camera
    // jump). drawnGeoJSON survives in React state even after the old controller is gone.
    const hasContent = layersRef.current.size > 0 || ((drawnGeoJSON?.features?.length || 0) > 0)
    let carry = null
    if (hasContent && map && drawCtrl) {
      try { carry = exportSessionRef.current ? { ...exportSessionRef.current() } : null } catch { carry = null }
      if (!carry) carry = { version: 1, layers: [] }
      if (!carry.drawings?.features?.length && drawnGeoJSON?.features?.length) carry.drawings = drawnGeoJSON
    }

    // Tear down previous binding if any
    if (mapRef.current && mapRef.current.__userLayersZoomHandler) {
      mapRef.current.off('zoomend', mapRef.current.__userLayersZoomHandler)
      delete mapRef.current.__userLayersZoomHandler
    }
    if (drawUnsubRef.current) { drawUnsubRef.current(); drawUnsubRef.current = null }
    // The Leaflet layer objects are bound to the dead map — drop them; importSession rebuilds.
    if (carry) {
      layersRef.current.forEach(e => { try { e.leafletLayer?.remove?.() } catch {} })
      layersRef.current.clear(); terrainGridsRef.current.clear()
    }

    mapRef.current = map
    drawCtrlRef.current = drawCtrl
    if (map) {
      ensurePane(map, 'overlay-image', 350)
      ensurePane(map, 'overlay-tile', 250)
      const onZoom = () => applyZoomVisibility()
      map.on('zoomend', onZoom)
      map.__userLayersZoomHandler = onZoom
    }
    if (drawCtrl) {
      const syncDrawn = () => {
        setDrawnFeatures(drawCtrl.listFeatures())
        try { setDrawnGeoJSON(drawCtrl.exportGeoJSON()) } catch { /* noop */ }
      }
      drawUnsubRef.current = drawCtrl.onChange(syncDrawn)
      syncDrawn()
    }

    // Re-apply the carried-over layers/drawings (silently — keep the camera where it is).
    if (carry && mapRef.current && drawCtrlRef.current) {
      try { importSessionRef.current?.(carry, { fit: false }) } catch {}
    }
    // Apply any session that arrived before the map+draw controller were ready
    if (pendingRestoreRef.current && mapRef.current && drawCtrlRef.current) {
      const queued = pendingRestoreRef.current
      pendingRestoreRef.current = null
      try { importSessionRef.current?.(queued) } catch {}
    }
  }, [applyZoomVisibility, drawnGeoJSON])

  const unbindMap = useCallback(() => {
    const map = mapRef.current
    if (map && map.__userLayersZoomHandler) {
      map.off('zoomend', map.__userLayersZoomHandler)
      delete map.__userLayersZoomHandler
    }
    if (drawUnsubRef.current) { drawUnsubRef.current(); drawUnsubRef.current = null }
    mapRef.current = null
    drawCtrlRef.current = null
  }, [])

  // ── Public: add a GeoJSON layer (from KML, KMZ, GeoJSON, GPX) ───────────
  const addGeoJSONLayer = useCallback((geojson, opts = {}) => {
    const map = mapRef.current
    if (!map) return null
    const id = opts.id || nextId()
    const color = opts.color || '#06d6a0'
    const opacity = opts.opacity ?? 0.85

    const showLabels = opts.showLabels !== false

    const layer = L.geoJSON(geojson, {
      // Apply per-feature simplestyle (KML stroke/fill etc.) for line & polygon
      style: (feature) => featurePathStyle(feature, color, opacity),
      // Special handling for GroundOverlay polygons that togeojson emits
      filter: (feature) => {
        // Skip GroundOverlay placeholder polygons — those are loaded as image overlays
        // through parseKMZ() instead. Without this filter the polygon outline shows
        // up on top of the image with no fill.
        return feature?.properties?.['@geometry-type'] !== 'groundoverlay'
      },
      pointToLayer: (f, latlng) => {
        const p = f.properties || {}
        // 0. UAS / Remote-ID feature → a distinct, colour-coded marker (drone / operator / home / frame-centre).
        const ug = p.uas_glx || p.rid_glx
        if (ug) {
          const c = p.color || ({ drone: '#ef4444', platform: '#22d3ee', operator: '#a855f7', home: '#f59e0b', frame_center: '#f59e0b' }[ug] || color)
          const glyph = (ug === 'drone' || ug === 'platform') ? '\u25BE' : ug === 'operator' ? '\u25C9' : ug === 'home' ? '\u2302' : ug === 'frame_center' ? '\u2295' : '\u25CF'
          const sz = (ug === 'drone' || ug === 'platform') ? 18 : 16
          return L.marker(latlng, {
            title: String(p.serial || p.call_sign || ug),
            icon: L.divIcon({ className: '', iconSize: [sz, sz], iconAnchor: [sz / 2, sz / 2],
              html: `<div style="width:${sz}px;height:${sz}px;border-radius:50%;background:${c};border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;font-size:${Math.round(sz * 0.6)}px;color:#fff;font-weight:700;">${glyph}</div>` }),
          })
        }
        const labelColor = pickPlacemarkLabelColor(f, color)
        // 1. ATAK CoT type → MIL-STD-2525 SIDC rendered via milsymbol.
        //    Highest-fidelity option for ATAK exports because the CoT type
        //    is the authoritative symbology, not the raster icon.
        const cot = p.cotType || findCotType(p)
        if (cot) {
          const sidc = cotToSidc(cot)
          if (sidc) {
            try {
              return L.marker(latlng, {
                icon: makeSidcIcon(sidc, { size: 32, uniqueDesignation: p.name || p.Name || '' }),
              })
            } catch {}
          }
        }
        // 2. Resolved icon URL (http/https/data/blob — relative paths inside
        //    KMZs are turned into data URLs by parseKMZ before we get here).
        const iconUrl = p.icon
        if (iconUrl && /^(?:https?:|data:|blob:)/i.test(iconUrl)) {
          return L.marker(latlng, { icon: makeIconUrlMarker(iconUrl) })
        }
        // 3. Plain coloured dot fallback.
        return L.marker(latlng, { icon: makeMarkerIcon(labelColor) })
      },
      onEachFeature: (feature, lyr) => {
        const p = feature.properties || {}
        lyr.on('click', () => { selectedRef.current = { kind: 'layer', id } })   // click-to-select (Delete-key removes it)
        const ug = p.uas_glx || p.rid_glx
        if (ug) {
          const tip = String(p.serial || p.call_sign || ({ drone: 'UAS', platform: 'UAS', operator: 'operator', home: 'home', frame_center: 'frame centre', footprint: 'footprint', area: 'op. area', los: 'sensor LOS', platform_track: 'track' }[ug] || ug))
          if (showLabels) lyr.bindTooltip(escapeHtml(tip), { permanent: true, direction: feature.geometry?.type === 'Point' ? 'right' : 'center', className: 'mv-feature-label', offset: feature.geometry?.type === 'Point' ? [10, 0] : [0, 0], sticky: feature.geometry?.type !== 'Point' })
          const rows = [
            p.serial && `Serial: ${escapeHtml(String(p.serial))}`,
            (p.alt_m != null) && `Alt: ${Math.round(p.alt_m)} m`,
            (p.heading_deg != null) && `Heading: ${Math.round(p.heading_deg)}\u00B0`,
            (p.speed_m_s != null) && `Speed: ${p.speed_m_s} m/s`,
            (p.slant_range_m != null) && `Slant range: ${Math.round(p.slant_range_m)} m`,
            (p.elev_m != null) && `Elev: ${Math.round(p.elev_m)} m`,
            (p.operator_id) && `Operator ID: ${escapeHtml(String(p.operator_id))}`,
            (p.radius_m != null) && `Area radius: ${p.radius_m} m`,
            (p.status) && `Status: ${escapeHtml(String(p.status))}`,
          ].filter(Boolean)
          if (rows.length) lyr.bindPopup(`<div style="font-size:12px;line-height:1.45"><strong style="color:#e6edf3">${escapeHtml(tip)}</strong><br>${rows.join('<br>')}</div>`)
          return
        }
        const name = p.name || p.Name || p.NAME || ''
        const desc = p.description || p.desc || p.Description || ''

        // Always show the placemark label as a permanent tooltip when name is set
        if (name && showLabels) {
          const isPoint = feature.geometry?.type === 'Point'
          lyr.bindTooltip(escapeHtml(name), {
            permanent: true,
            direction: isPoint ? 'right' : 'center',
            className: 'mv-feature-label',
            offset: isPoint ? [10, 0] : [0, 0],
            sticky: !isPoint,
          })
        }

        if (name || desc) {
          // Strip dangerous tags but keep KML's basic HTML descriptions
          const safeDesc = String(desc)
            .replace(/<script\b[\s\S]*?<\/script>/gi, '')
            .replace(/<iframe\b[\s\S]*?<\/iframe>/gi, '')
            .replace(/on\w+\s*=\s*"[^"]*"/gi, '')
            .replace(/on\w+\s*=\s*'[^']*'/gi, '')
            .replace(/javascript:/gi, '')
          lyr.bindPopup(`<div style="font-size:12px;max-width:280px;line-height:1.45">
            ${name ? `<strong style="color:#e6edf3">${escapeHtml(name)}</strong>` : ''}
            ${safeDesc ? `<div style="margin-top:6px;color:#c9d1d9">${safeDesc}</div>` : ''}
          </div>`, { maxWidth: 320 })
        }
      },
    })

    if (opts.visible !== false) layer.addTo(map)

    const meta = {
      id, kind: 'geojson', name: opts.name || 'GeoJSON layer',
      sourceFormat: opts.sourceFormat || 'geojson',
      visible: opts.visible !== false, opacity, color,
      featureCount: (geojson?.features?.length) || 0,
    }
    layersRef.current.set(id, { meta, leafletLayer: layer, sourceData: geojson })
    refreshLayers()
    if (opts.fit !== false) {
      try {
        const b = layer.getBounds()
        if (b.isValid()) map.fitBounds(b, { padding: [40, 40] })
      } catch {}
    }
    return id
  }, [refreshLayers])

  // ── Public: add a single image overlay with bounds ──────────────────────
  const addImageLayer = useCallback((info, opts = {}) => {
    const map = mapRef.current
    if (!map) return null
    const id = opts.id || nextId()
    const opacity = opts.opacity ?? 0.85
    const minZoom = opts.minZoom ?? 0
    const maxZoom = opts.maxZoom ?? MAX_ZOOM

    const layer = L.imageOverlay(info.dataUrl, info.bounds, {
      opacity, pane: 'overlay-image', interactive: false,
    })
    if (opts.visible !== false) layer.addTo(map)

    const meta = {
      id, kind: 'image', name: opts.name || info.name || 'Image overlay',
      sourceFormat: info.sourceFormat || 'image',
      visible: opts.visible !== false, opacity, minZoom, maxZoom,
      bounds: info.bounds, mime: info.mime,
    }
    layersRef.current.set(id, { meta, leafletLayer: layer, sourceData: info })
    refreshLayers()
    applyZoomVisibility()
    if (opts.fit !== false) {
      try { map.fitBounds(info.bounds, { padding: [40, 40] }) } catch {}
    }
    return id
  }, [refreshLayers, applyZoomVisibility])

  // ── Public: add an XYZ / WMS tile source ────────────────────────────────
  const addTileLayer = useCallback((tileOpts) => {
    const map = mapRef.current
    if (!map) return null
    const id = tileOpts.id || nextId()
    const opacity = tileOpts.opacity ?? 0.9
    const minZoom = tileOpts.minZoom ?? 0
    const maxZoom = tileOpts.maxZoom ?? 18

    let layer
    if (tileOpts.type === 'wms') {
      layer = L.tileLayer.wms(tileOpts.url, {
        layers: tileOpts.wmsLayers || '',
        format: tileOpts.format || 'image/png',
        transparent: tileOpts.transparent !== false,
        attribution: tileOpts.attribution || '',
        minZoom, maxZoom, opacity, pane: 'overlay-tile',
      })
    } else {
      layer = L.tileLayer(tileOpts.url, {
        minZoom, maxZoom, opacity,
        attribution: tileOpts.attribution || '',
        pane: 'overlay-tile',
        subdomains: tileOpts.subdomains || 'abc',
      })
    }
    if (tileOpts.visible !== false) layer.addTo(map)

    const meta = {
      id, kind: 'tiles', name: tileOpts.name || 'Tile source',
      url: tileOpts.url, type: tileOpts.type || 'xyz',
      visible: tileOpts.visible !== false, opacity, minZoom, maxZoom,
      attribution: tileOpts.attribution || '',
      wmsLayers: tileOpts.wmsLayers || '',
    }
    layersRef.current.set(id, { meta, leafletLayer: layer, sourceData: { ...tileOpts } })
    refreshLayers()
    return id
  }, [refreshLayers])

  // ── Public: add a terrain grid (DTED / HGT / ASCII grid) ────────────────
  const addTerrainGrid = useCallback((grid, opts = {}) => {
    const map = mapRef.current
    if (!map) return null
    const id = opts.id || nextId()
    const opacity = opts.opacity ?? 0.35

    // Visualize coverage as a translucent rectangle
    const rect = L.rectangle(grid.bounds, {
      color: '#f59e0b', weight: 1.5, dashArray: '4 4',
      fillOpacity: opacity * 0.4, interactive: false,
    })
    if (opts.visible !== false) rect.addTo(map)

    const meta = {
      id, kind: 'terrain', name: opts.name || grid.name || 'Terrain grid',
      sourceFormat: grid.sourceFormat || 'dted',
      visible: opts.visible !== false, opacity,
      bounds: grid.bounds, cols: grid.cols, rows: grid.rows,
      dx: grid.dx, dy: grid.dy,
      minElev: grid.minElev, maxElev: grid.maxElev,
    }
    layersRef.current.set(id, { meta, leafletLayer: rect, sourceData: grid })
    terrainGridsRef.current.set(id, grid)
    refreshLayers()
    if (opts.fit !== false) {
      try { map.fitBounds(grid.bounds, { padding: [40, 40] }) } catch {}
    }
    return id
  }, [refreshLayers])

  // ── Public: mutators ────────────────────────────────────────────────────
  const removeLayer = useCallback((id) => {
    const entry = layersRef.current.get(id)
    if (!entry) return
    try { entry.leafletLayer?.remove?.() } catch {}
    layersRef.current.delete(id)
    terrainGridsRef.current.delete(id)
    refreshLayers()
  }, [refreshLayers])

  const setLayerProperty = useCallback((id, patch) => {
    const entry = layersRef.current.get(id)
    if (!entry) return
    const map = mapRef.current
    const meta = entry.meta = { ...entry.meta, ...patch }

    if ('opacity' in patch) {
      if (entry.leafletLayer?.setOpacity) entry.leafletLayer.setOpacity(meta.opacity)
      else if (entry.leafletLayer?.setStyle) {
        entry.leafletLayer.setStyle({ opacity: meta.opacity, fillOpacity: meta.opacity * 0.25 })
      }
    }
    if ('color' in patch && entry.leafletLayer?.setStyle) {
      entry.leafletLayer.setStyle({ color: meta.color })
    }
    if ('visible' in patch && map) {
      const inRange = meta.kind !== 'image' && meta.kind !== 'tiles'
        ? true
        : (map.getZoom() >= (meta.minZoom ?? 0) && map.getZoom() <= (meta.maxZoom ?? MAX_ZOOM))
      const shouldShow = meta.visible && inRange
      const onMap = map.hasLayer(entry.leafletLayer)
      if (shouldShow && !onMap) entry.leafletLayer.addTo(map)
      else if (!shouldShow && onMap) entry.leafletLayer.remove()
    }
    if (('minZoom' in patch || 'maxZoom' in patch)) applyZoomVisibility()
    refreshLayers()
  }, [refreshLayers, applyZoomVisibility])

  const getLayerBounds = useCallback((id) => {
    const entry = layersRef.current.get(id)
    if (!entry) return null
    try {
      if (entry.leafletLayer?.getBounds) {
        const b = entry.leafletLayer.getBounds()
        if (b?.isValid?.()) return b
      }
      if (entry.meta?.bounds) {
        const [[s, w], [n, e]] = entry.meta.bounds
        return L.latLngBounds([s, w], [n, e])
      }
    } catch {}
    return null
  }, [])

  const focusLayer = useCallback((id) => {
    const map = mapRef.current
    const entry = layersRef.current.get(id)
    if (!map || !entry) return
    try {
      if (entry.leafletLayer?.getBounds) {
        const b = entry.leafletLayer.getBounds()
        if (b.isValid()) map.fitBounds(b, { padding: [40, 40] })
      } else if (entry.meta.bounds) {
        map.fitBounds(entry.meta.bounds, { padding: [40, 40] })
      }
    } catch {}
  }, [])

  const renameLayer = useCallback((id, name) => setLayerProperty(id, { name }), [setLayerProperty])

  const clearAll = useCallback(() => {
    layersRef.current.forEach(entry => {
      try { entry.leafletLayer?.remove?.() } catch {}
    })
    layersRef.current.clear()
    terrainGridsRef.current.clear()
    setGlobeDrawnGeoJSON({ type: 'FeatureCollection', features: [] })
    refreshLayers()
  }, [refreshLayers])

  // ── Drawn features ──────────────────────────────────────────────────────
  const removeDrawnFeature = useCallback((fid) => {
    drawCtrlRef.current?.removeFeature(fid)
  }, [])
  const focusDrawnFeature = useCallback((fid) => {
    drawCtrlRef.current?.focusFeature(fid)
  }, [])
  const clearDrawn = useCallback(() => drawCtrlRef.current?.clearAll(), [])

  // ── Click-to-select + Delete-key removal ───────────────────────────────
  const selectFeature = useCallback((sel) => { selectedRef.current = sel || null }, [])
  const getSelectedFeature = useCallback(() => selectedRef.current, [])
  const removeSelected = useCallback(() => {
    const s = selectedRef.current
    if (!s) return null
    selectedRef.current = null
    if (s.kind === 'drawn') { drawCtrlRef.current?.removeFeature(s.id); return 'drawn' }
    if (s.kind === 'layer') { removeLayer(s.id); return 'layer' }
    return null
  }, [removeLayer])

  // ── Sample terrain at lat/lon — checks all loaded grids ────────────────
  const sampleTerrain = useCallback((lat, lon) => {
    for (const g of terrainGridsRef.current.values()) {
      const [[s, w], [n, e]] = g.bounds
      if (lat < s || lat > n || lon < w || lon > e) continue
      const col = ((lon - w) / g.dx) | 0
      const row = ((n - lat) / g.dy) | 0
      if (col < 0 || col >= g.cols || row < 0 || row >= g.rows) continue
      const v = g.data[row * g.cols + col]
      if (v == null || !Number.isFinite(v) || v <= -1000) continue
      return v
    }
    return null
  }, [])

  const hasTerrain = useCallback(() => terrainGridsRef.current.size > 0, [])

  // ── Sample terrain along a path; piecewise linear interpolation ────────
  const sampleTerrainAlongPath = useCallback((path, numPoints = 256) => {
    if (!path || path.length < 2) return null
    // Compute cumulative distance
    const R = 6371000
    const toRad = d => d * Math.PI / 180
    const segDists = []
    let total = 0
    for (let i = 1; i < path.length; i++) {
      const a = path[i - 1], b = path[i]
      const dLat = toRad(b[0] - a[0])
      const dLon = toRad(b[1] - a[1])
      const x = Math.sin(dLat/2)**2 +
        Math.sin(dLon/2)**2 * Math.cos(toRad(a[0])) * Math.cos(toRad(b[0]))
      const d = R * 2 * Math.atan2(Math.sqrt(x), Math.sqrt(1-x))
      segDists.push(d); total += d
    }
    const dists = [], elevs = []
    for (let i = 0; i < numPoints; i++) {
      const t = (i / (numPoints - 1)) * total
      let acc = 0
      let segIndex = 0
      for (; segIndex < segDists.length - 1; segIndex++) {
        if (acc + segDists[segIndex] >= t) break
        acc += segDists[segIndex]
      }
      const remaining = t - acc
      const segLen = segDists[segIndex] || 1
      const u = Math.min(1, remaining / segLen)
      const a = path[segIndex], b = path[segIndex + 1]
      const lat = a[0] + (b[0] - a[0]) * u
      const lon = a[1] + (b[1] - a[1]) * u
      dists.push(t)
      elevs.push(sampleTerrain(lat, lon))
    }
    return { distances_m: dists, elevations_m: elevs, totalM: total }
  }, [sampleTerrain])

  // ── Save / load session ────────────────────────────────────────────────
  const exportSession = useCallback(() => {
    const session = { version: 1, savedAt: new Date().toISOString(), layers: [], drawings: null }
    layersRef.current.forEach(entry => {
      const m = entry.meta
      const base = {
        id: m.id, kind: m.kind, name: m.name, opacity: m.opacity, visible: m.visible,
        minZoom: m.minZoom, maxZoom: m.maxZoom, bounds: m.bounds, color: m.color,
        sourceFormat: m.sourceFormat,
      }
      if (m.kind === 'geojson') {
        session.layers.push({ ...base, geojson: entry.sourceData })
      } else if (m.kind === 'image') {
        session.layers.push({ ...base, dataUrl: entry.sourceData?.dataUrl, mime: entry.sourceData?.mime })
      } else if (m.kind === 'tiles') {
        session.layers.push({ ...base, ...entry.sourceData })
      } else if (m.kind === 'terrain') {
        // Flatten the typed-array data so JSON can serialize it
        const g = entry.sourceData
        session.layers.push({
          ...base,
          terrain: {
            cols: g.cols, rows: g.rows, dx: g.dx, dy: g.dy,
            bounds: g.bounds, sourceFormat: g.sourceFormat,
            minElev: g.minElev, maxElev: g.maxElev,
            data: Array.from(g.data),
          },
        })
      }
    })
    if (drawCtrlRef.current) session.drawings = drawCtrlRef.current.exportGeoJSON()
    return session
  }, [])
  exportSessionRef.current = exportSession

  const importSession = useCallback((session, opts = {}) => {
    if (!session) return
    const fit = opts.fit !== false   // a 2D⇄3D re-bind carries layers over silently — don't move the camera
    clearAll()
    drawCtrlRef.current?.clearAll?.()
    if (!Array.isArray(session.layers)) session.layers = []
    session.layers.forEach(l => {
      try {
        if (l.kind === 'geojson' && l.geojson) {
          addGeoJSONLayer(l.geojson, { id: l.id, name: l.name, color: l.color, opacity: l.opacity, visible: l.visible, sourceFormat: l.sourceFormat, fit: false })
        } else if (l.kind === 'image' && l.dataUrl && l.bounds) {
          addImageLayer({ name: l.name, dataUrl: l.dataUrl, bounds: l.bounds, mime: l.mime, sourceFormat: l.sourceFormat },
            { id: l.id, name: l.name, opacity: l.opacity, visible: l.visible, minZoom: l.minZoom, maxZoom: l.maxZoom, fit: false })
        } else if (l.kind === 'tiles') {
          addTileLayer({ ...l, fit: false })
        } else if (l.kind === 'terrain' && l.terrain) {
          const g = {
            ...l.terrain,
            data: new Float32Array(l.terrain.data),
            name: l.name,
          }
          addTerrainGrid(g, { id: l.id, name: l.name, opacity: l.opacity, visible: l.visible, fit: false })
        }
      } catch {}
    })

    // Fit to the union of every imported layer's bounds
    const map = mapRef.current
    if (map) {
      let union = null
      layersRef.current.forEach(entry => {
        try {
          if (entry.leafletLayer?.getBounds) {
            const b = entry.leafletLayer.getBounds()
            if (b?.isValid?.()) union = union ? union.extend(b) : L.latLngBounds(b.getSouthWest(), b.getNorthEast())
          } else if (entry.meta?.bounds) {
            const [[s, w], [n, e]] = entry.meta.bounds
            const b = L.latLngBounds([s, w], [n, e])
            union = union ? union.extend(b) : b
          }
        } catch {}
      })
      if (fit && union && union.isValid()) {
        map.fitBounds(union, { padding: [40, 40] })
      }
    }

    // Restore drawn features
    if (session.drawings && drawCtrlRef.current?.importGeoJSON) {
      try { drawCtrlRef.current.importGeoJSON(session.drawings) } catch {}
    }
  }, [clearAll, addGeoJSONLayer, addImageLayer, addTileLayer, addTerrainGrid])

  // Defers a session restore until the map+draw controller are bound. App
  // calls this on mount before MapView has finished mounting.
  importSessionRef.current = importSession
  const restoreSnapshot = useCallback((session) => {
    if (!session) return
    if (mapRef.current && drawCtrlRef.current) {
      importSession(session)
    } else {
      pendingRestoreRef.current = session
    }
  }, [importSession])

  return {
    // state
    layers, drawnFeatures, drawnGeoJSON, globeDrawnGeoJSON,
    addGlobeDrawing, removeGlobeDrawing, clearGlobeDrawings,
    // binding
    bindMap, unbindMap,
    // adders
    addGeoJSONLayer, addImageLayer, addTileLayer, addTerrainGrid,
    // mutators
    removeLayer, setLayerProperty, renameLayer, focusLayer, getLayerBounds, clearAll,
    removeDrawnFeature, focusDrawnFeature, clearDrawn,
    // click-to-select / Delete-key
    selectFeature, getSelectedFeature, removeSelected,
    // queries
    sampleTerrain, hasTerrain, sampleTerrainAlongPath,
    // session
    exportSession, importSession, restoreSnapshot,
    // refs (internal use)
    _drawCtrlRef: drawCtrlRef,
    _mapRef: mapRef,
  }
}
