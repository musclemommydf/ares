/**
 * ATAK-style drawing tools for the Ares map.
 *
 * Exposes a `createDrawController(map, opts)` factory that owns an L.LayerGroup
 * of user-drawn features and exposes activate/deactivate/finish/clear/export
 * helpers. State is intentionally external so the React UI can drive it.
 *
 * Tool kinds supported:
 *   - point        : single click drops a labelled marker
 *   - line         : click waypoints, right-click / Esc to finish
 *   - polygon      : click vertices, click near first or right-click to close
 *   - rectangle    : two corner clicks
 *   - circle       : click center → click radius
 *   - ellipse      : click center → click X-radius point → click Y-radius point
 *   - freehand     : mousedown-drag to sketch a polyline
 *   - label        : single click drops a text label
 *   - rangeRings   : click center → 4 concentric rings at user-set spacing
 *   - fan          : click apex → click radius/start → drag azimuth / type span
 *   - rb           : range & bearing line — click start → click end
 *   - geofence     : polygon + alert label
 *   - milMarker    : ATAK-flavoured friend/foe/neutral/unknown markers
 */
import L from 'leaflet'
import { promptUser } from '../Common/PromptDialog'

const DEFAULT_STYLE = { color: '#a855f7', weight: 2, fillOpacity: 0.15 }
const TOOL_CURSOR = 'crosshair'

const MIL_AFFILIATIONS = {
  friend:  { fill: '#3b82f6', shape: 'rect',     label: 'F' },
  hostile: { fill: '#ef4444', shape: 'diamond',  label: 'H' },
  neutral: { fill: '#22c55e', shape: 'square',   label: 'N' },
  unknown: { fill: '#facc15', shape: 'cloverish', label: '?' },
}

function haversine(a, b) {
  const R = 6371000
  const toRad = d => d * Math.PI / 180
  const dLat = toRad(b.lat - a.lat)
  const dLon = toRad(b.lng - a.lng)
  const lat1 = toRad(a.lat), lat2 = toRad(b.lat)
  const x = Math.sin(dLat/2)**2 + Math.sin(dLon/2)**2 * Math.cos(lat1)*Math.cos(lat2)
  return R * 2 * Math.atan2(Math.sqrt(x), Math.sqrt(1-x))
}

function bearing(a, b) {
  const toRad = d => d * Math.PI / 180
  const toDeg = r => r * 180 / Math.PI
  const φ1 = toRad(a.lat), φ2 = toRad(b.lat)
  const λ1 = toRad(a.lng), λ2 = toRad(b.lng)
  const y = Math.sin(λ2-λ1) * Math.cos(φ2)
  const x = Math.cos(φ1)*Math.sin(φ2) - Math.sin(φ1)*Math.cos(φ2)*Math.cos(λ2-λ1)
  return (toDeg(Math.atan2(y,x)) + 360) % 360
}

function destinationPoint(lat, lon, bearingDeg, distanceM) {
  const R = 6371000
  const δ = distanceM / R
  const θ = bearingDeg * Math.PI / 180
  const φ1 = lat * Math.PI / 180
  const λ1 = lon * Math.PI / 180
  const φ2 = Math.asin(Math.sin(φ1) * Math.cos(δ) + Math.cos(φ1) * Math.sin(δ) * Math.cos(θ))
  const λ2 = λ1 + Math.atan2(Math.sin(θ) * Math.sin(δ) * Math.cos(φ1),
                              Math.cos(δ) - Math.sin(φ1) * Math.sin(φ2))
  return { lat: φ2 * 180 / Math.PI, lon: ((λ2 * 180 / Math.PI) + 540) % 360 - 180 }
}

function ellipsePolygonLatLngs(centerLatLng, axisAm, axisBm, rotationDeg, steps = 64) {
  // Approximate ellipse on the sphere by sampling points around the centre with
  // varying radius/bearing. axisA = bearing of long axis offset, axisB = perpendicular.
  const pts = []
  for (let i = 0; i < steps; i++) {
    const t = (i / steps) * 2 * Math.PI
    const r = (axisAm * axisBm) / Math.sqrt(
      (axisBm * Math.cos(t))**2 + (axisAm * Math.sin(t))**2
    )
    const az = ((rotationDeg + (t * 180 / Math.PI)) % 360 + 360) % 360
    const p = destinationPoint(centerLatLng.lat, centerLatLng.lng, az, r)
    pts.push([p.lat, p.lon])
  }
  pts.push(pts[0])
  return pts
}

function fanLatLngs(centerLatLng, radiusM, bearing1Deg, bearing2Deg, steps = 48) {
  // bearing2 is reached by sweeping clockwise from bearing1
  let span = (bearing2Deg - bearing1Deg + 360) % 360
  if (span === 0) span = 360
  const pts = [[centerLatLng.lat, centerLatLng.lng]]
  for (let i = 0; i <= steps; i++) {
    const az = bearing1Deg + (span * i / steps)
    const p = destinationPoint(centerLatLng.lat, centerLatLng.lng, az, radiusM)
    pts.push([p.lat, p.lon])
  }
  pts.push([centerLatLng.lat, centerLatLng.lng])
  return pts
}

function makeMilIcon(aff, label) {
  const { fill, shape } = MIL_AFFILIATIONS[aff] || MIL_AFFILIATIONS.unknown
  const txt = (label || MIL_AFFILIATIONS[aff]?.label || '').slice(0, 3)
  let inner = ''
  const stroke = '#fff'
  if (shape === 'rect') {
    inner = `<rect x="3" y="6" width="22" height="16" fill="${fill}" stroke="${stroke}" stroke-width="2"/>`
  } else if (shape === 'square') {
    inner = `<rect x="5" y="5" width="18" height="18" fill="${fill}" stroke="${stroke}" stroke-width="2"/>`
  } else if (shape === 'diamond') {
    inner = `<polygon points="14,2 26,14 14,26 2,14" fill="${fill}" stroke="${stroke}" stroke-width="2"/>`
  } else { // cloverish (unknown / pending)
    inner = `<path d="M14 3 a8 8 0 0 1 8 8 a8 8 0 0 1 -8 8 a8 8 0 0 1 -8 -8 a8 8 0 0 1 8 -8 z"
      fill="${fill}" stroke="${stroke}" stroke-width="2"/>`
  }
  return L.divIcon({
    className: '',
    html: `<div style="position:relative;width:28px;height:28px;filter:drop-shadow(0 1px 2px rgba(0,0,0,.6));">
      <svg width="28" height="28" viewBox="0 0 28 28">${inner}
        <text x="14" y="18" text-anchor="middle" fill="#000" font-size="10" font-weight="700"
              font-family="system-ui,sans-serif">${txt}</text>
      </svg>
    </div>`,
    iconSize: [28, 28],
    iconAnchor: [14, 14],
  })
}

function makeLabelIcon(text, color = '#facc15') {
  const safe = String(text || '').replace(/[<>&"]/g, c => ({ '<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;' }[c]))
  return L.divIcon({
    className: '',
    html: `<div style="
      background:rgba(0,0,0,0.78);color:${color};
      padding:3px 8px;border-radius:4px;font-size:11px;font-weight:700;
      white-space:nowrap;border:1px solid ${color}66;pointer-events:auto;
      transform:translate(-50%, -120%);">${safe}</div>`,
    iconSize: [null, null],
    iconAnchor: [0, 0],
  })
}

function makePinIcon(color = '#a855f7') {
  return L.divIcon({
    className: '',
    html: `<div style="
      width:18px;height:18px;border-radius:50%;background:${color};
      border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,0.6);
    "></div>`,
    iconSize: [18, 18], iconAnchor: [9, 9],
  })
}

// Small square handle used as a draggable anchor on editable shapes (pairing
// line endpoints, future edit-mode vertex handles, etc.).
function makeAnchorIcon(color = '#facc15') {
  return L.divIcon({
    className: '',
    html: `<div style="
      width:11px;height:11px;background:${color};
      border:1.5px solid #fff;box-shadow:0 1px 2px rgba(0,0,0,0.6);
      cursor:grab;
    "></div>`,
    iconSize: [11, 11], iconAnchor: [5.5, 5.5],
  })
}

let FEATURE_ID = 1
function nextFeatureId() { return `f_${Date.now().toString(36)}_${(FEATURE_ID++).toString(36)}` }

export function createDrawController(map, opts = {}) {
  const layerGroup = L.layerGroup().addTo(map)
  const features = new Map()  // id → { id, kind, layers: L.Layer[], meta }
  const listeners = new Set()
  let activeTool = null
  let activeStyle = { ...DEFAULT_STYLE, ...(opts.style || {}) }
  let scratch = null  // working state for in-progress drawing
  let prevCursor = ''
  // NATO symbol the user has armed for placement (sidc with '*' affiliation)
  let natoArm = { sidc: null, affiliation: 'F', name: '', label: '' }
  // Async prompt for per-ring labels (rangeRings tool). Resolves to a
  // comma-separated string or null (blank input ⇒ default 4 numeric rings).
  // Uses the Electron-safe PromptDialog modal — window.prompt() silently
  // returns null in Electron BrowserWindow contexts.
  let rangeRingsPrompt = (stepM) => {
    const auto = stepM >= 1000 ? `${(stepM / 1000).toFixed(2)} km` : `${Math.round(stepM)} m`
    return promptUser({
      title: 'Range-ring labels',
      message: `Comma-separated labels (e.g. "Kill,Casualty,Lethal"). Leave blank for 4 numeric rings at ${auto} steps.`,
      defaultValue: '',
      placeholder: 'Kill,Casualty,Lethal',
    })
  }

  function notify() {
    const list = Array.from(features.values()).map(f => ({
      id: f.id, kind: f.kind, meta: f.meta,
    }))
    listeners.forEach(l => l(list, activeTool))
  }

  function setCursor(on) {
    const c = map.getContainer()
    if (on) { prevCursor = c.style.cursor; c.style.cursor = TOOL_CURSOR }
    else c.style.cursor = prevCursor || ''
  }

  function clearScratch() {
    if (!scratch) return
    if (scratch.layers) scratch.layers.forEach(l => map.removeLayer(l))
    scratch = null
  }

  // Single-click point features whose extent equals their click location.
  // No need to autoscale to these — the click is already in view.
  const POINT_KINDS = new Set(['point', 'label', 'mil-friend', 'mil-hostile',
    'mil-neutral', 'mil-unknown', 'nato'])

  // Build an invisible wide-stroke hit-target for a polyline/polygon so the
  // user can click *near* a thin line and still hit it. Returns null for
  // non-Polyline layers (markers, circles, dots — those already have plenty
  // of pixel area). The hit target reads the latlngs from the visible layer
  // at attach time and is kept in sync via a `move` listener on the source.
  function makeHitTarget(visibleLayer, onClick) {
    const stop = (e) => { try { onClick(e) } finally { L.DomEvent.stopPropagation(e) } }
    if (visibleLayer instanceof L.Polyline) {
      const isPolygon = visibleLayer instanceof L.Polygon
      const opts = { color: '#fff', weight: 18, opacity: 0, fillOpacity: 0, interactive: true }
      const hit = isPolygon ? L.polygon(visibleLayer.getLatLngs(), opts)
                            : L.polyline(visibleLayer.getLatLngs(), opts)
      hit.on('click', stop)
      const sync = () => { try { hit.setLatLngs(visibleLayer.getLatLngs()) } catch {} }
      try { visibleLayer.on('move add', sync) } catch {}
      return hit
    }
    // L.Circle (range rings, circle tool, edit-mode circle): widen the
    // stroke so the user can click anywhere near the ring. L.CircleMarker
    // already has plenty of pixel area, so we leave those alone.
    if (visibleLayer instanceof L.Circle) {
      const hit = L.circle(visibleLayer.getLatLng(), {
        radius: visibleLayer.getRadius(),
        color: '#fff', weight: 16, opacity: 0, fillOpacity: 0, interactive: true,
      })
      hit.on('click', stop)
      const sync = () => {
        try { hit.setLatLng(visibleLayer.getLatLng()); hit.setRadius(visibleLayer.getRadius()) } catch {}
      }
      try { visibleLayer.on('move add', sync) } catch {}
      return hit
    }
    return null
  }

  function pushFeature(kind, layers, meta = {}) {
    const id = nextFeatureId()
    layers.forEach(l => layerGroup.addLayer(l))
    // Bind a popup with name + delete control on the primary layer
    const primary = layers[0]
    if (primary) {
      const name = meta.name || defaultName(kind)
      meta.name = name
      primary.bindPopup(() => buildPopup(id, name, meta))
    }
    // Click-to-select on every layer of the feature, so the Delete key can remove it.
    const selectHandler = () => opts.onFeatureClick?.(id)
    layers.forEach(l => { try { l.on?.('click', selectHandler) } catch {} })

    // Add invisible wide-stroke hit-targets behind thin lines/polygons so
    // they're easy to click. Each hit-target shares the select handler and
    // joins the feature's layer list so removal cleans them up.
    const hitTargets = []
    for (const lyr of layers) {
      const t = makeHitTarget(lyr, selectHandler)
      if (t) {
        // Insert hit target BEHIND its visible layer using pane management —
        // Leaflet renders later-added overlays on top by default, so add it
        // first (we just added the visible layer above, so addLayer-then-bringToBack).
        layerGroup.addLayer(t)
        try { t.bringToBack && t.bringToBack() } catch {}
        hitTargets.push(t)
      }
    }
    const allLayers = [...layers, ...hitTargets]
    features.set(id, { id, kind, layers: allLayers, meta })

    // Auto-fit to the new feature unless it's a single point (which is at the
    // click location, already visible). Cap at current zoom so we don't zoom
    // *in* on a small drawn shape — only zoom *out* if needed to fit.
    if (!POINT_KINDS.has(kind)) {
      try {
        const bounds = layers.reduce((acc, lyr) => {
          if (lyr?.getBounds) {
            const b = lyr.getBounds()
            if (b?.isValid?.()) return acc ? acc.extend(b) : L.latLngBounds(b.getSouthWest(), b.getNorthEast())
          } else if (lyr?.getLatLng) {
            const ll = lyr.getLatLng()
            return acc ? acc.extend(ll) : L.latLngBounds(ll, ll)
          }
          return acc
        }, null)
        if (bounds && bounds.isValid()) {
          const z = map.getZoom()
          map.fitBounds(bounds, { padding: [40, 40], maxZoom: z, animate: true })
        }
      } catch {}
    }

    notify()
    return id
  }

  function buildPopup(id, name, meta) {
    const el = document.createElement('div')
    el.style.cssText = 'font-size:12px;min-width:160px'
    el.innerHTML = `
      <div style="font-weight:700;color:#e6edf3;margin-bottom:4px">${escapeHtml(name)}</div>
      ${meta.description ? `<div style="color:#8b949e;margin-bottom:6px">${escapeHtml(meta.description)}</div>` : ''}
      ${meta.note ? `<div style="color:#8b949e;margin-bottom:6px">${escapeHtml(meta.note)}</div>` : ''}
      <div style="display:flex;gap:6px;margin-top:6px">
        <button data-act="edit" style="flex:1;padding:3px 6px;font-size:11px;background:#1a2f4a;color:#7dd3fc;border:1px solid #1e3a5f;border-radius:4px;cursor:pointer">${editing?.id === id ? '✓ Done' : '✏ Edit'}</button>
        <button data-act="rename" style="flex:1;padding:3px 6px;font-size:11px;background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;cursor:pointer">Rename</button>
        <button data-act="delete" style="flex:1;padding:3px 6px;font-size:11px;background:#3f1d1d;color:#fca5a5;border:1px solid #7f1d1d;border-radius:4px;cursor:pointer">Delete</button>
      </div>`
    el.querySelector('[data-act="delete"]').addEventListener('click', () => {
      removeFeature(id)
      map.closePopup()
    })
    el.querySelector('[data-act="rename"]').addEventListener('click', async () => {
      const v = await promptUser({ title: 'Rename feature', defaultValue: name })
      if (v && v.trim()) {
        const f = features.get(id)
        if (f) { f.meta.name = v.trim(); notify() }
        map.closePopup()
      }
    })
    el.querySelector('[data-act="edit"]').addEventListener('click', () => {
      editFeature(id)
      map.closePopup()
    })
    return el
  }

  function escapeHtml(s) {
    return String(s).replace(/[<>&"']/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c]))
  }

  function defaultName(kind) {
    const n = Array.from(features.values()).filter(f => f.kind === kind).length + 1
    return `${kind}-${n}`
  }

  function removeFeature(id) {
    const f = features.get(id)
    if (!f) return
    f.layers.forEach(l => layerGroup.removeLayer(l))
    features.delete(id)
    notify()
  }

  // ── editing: drag-handle resize / vertex move ────────────────────────────
  // editFeature(id) puts a finished feature into "edit mode": existing visible
  // layers are hidden and replaced by editable handles. Drag rebuilds the
  // primary visual. exitEditMode() commits the new geometry and re-renders.
  // Only the most-common kinds are supported here — ellipse / fan / rangeRings
  // / axisAdvance still get a fallback "move centroid" handle.
  let editing = null   // { id, handles: L.Marker[], hiddenLayers: L.Layer[], cleanup: fn }
  function exitEditMode() {
    if (!editing) return
    const { handles, hiddenLayers, cleanup } = editing
    try { cleanup?.() } catch {}
    handles.forEach((h) => { try { layerGroup.removeLayer(h) } catch {} })
    hiddenLayers.forEach((l) => { try { layerGroup.addLayer(l) } catch {} })
    editing = null
    notify()
  }
  function editFeature(id) {
    if (editing && editing.id === id) { exitEditMode(); return }
    if (editing) exitEditMode()
    const f = features.get(id); if (!f) return
    const color = f.meta?.color || activeStyle.color
    const handles = []
    const hiddenLayers = [...f.layers]
    let cleanup = null

    const mkHandle = (latlng, onDrag) => {
      const h = L.marker(latlng, { icon: makeAnchorIcon('#facc15'), draggable: true,
                                    keyboard: false, zIndexOffset: 1000 })
      h.on('drag', () => onDrag(h.getLatLng()))
      layerGroup.addLayer(h)
      handles.push(h)
      return h
    }

    // vertex-edit for the line-collection family
    const isVertexFamily = ['line', 'polygon', 'geofence', 'freehand',
                            'phaseLine', 'flot', 'boundary'].includes(f.kind)
    if (isVertexFamily) {
      // Hide originals, render a live preview polyline/polygon. The first layer
      // in the feature drives the geometry; handle drag rebuilds the meta points
      // array and the preview.
      hiddenLayers.forEach((l) => layerGroup.removeLayer(l))
      const points = (f.meta?.points || []).map((p) => Array.isArray(p) ? [...p] : [p.lat, p.lng])
      if (points.length === 0) return
      const isClosed = f.kind === 'polygon' || f.kind === 'geofence'
      const preview = isClosed
        ? L.polygon(points, { color, weight: 2, fillOpacity: 0.15, dashArray: f.kind === 'geofence' ? '6 4' : null })
        : L.polyline(points, { color, weight: 2, dashArray: '4 4' })
      layerGroup.addLayer(preview)
      points.forEach((p, i) => {
        mkHandle(L.latLng(p[0], p[1]), (ll) => {
          points[i] = [ll.lat, ll.lng]
          preview.setLatLngs(isClosed ? points : points)
        })
      })
      cleanup = () => {
        try { layerGroup.removeLayer(preview) } catch {}
        f.meta.points = points
        // Rebuild the feature's primary visual from the new points. We just
        // recreate the geometry on the same layer the feature is exposed as.
        if (isClosed) {
          const poly = L.polygon(points, { color, weight: 2, fillOpacity: 0.15,
                                            dashArray: f.kind === 'geofence' ? '6 4' : null })
          f.layers = [poly, ...f.layers.slice(1)]
        } else {
          const line = L.polyline(points, { color, weight: 2 })
          f.layers = [line, ...f.layers.slice(1)]
        }
        // Drop the old primary from hiddenLayers so exitEditMode doesn't re-add it.
        hiddenLayers[0] = f.layers[0]
      }
    } else if (f.kind === 'circle') {
      const center = f.meta?.center; const r = f.meta?.radiusM
      if (center == null || r == null) return
      hiddenLayers.forEach((l) => layerGroup.removeLayer(l))
      const circle = L.circle([center.lat, center.lng], { radius: r, color, weight: 2, fillOpacity: 0.15 })
      layerGroup.addLayer(circle)
      let c = L.latLng(center.lat, center.lng)
      let radM = r
      const edge = destinationPoint(c.lat, c.lng, 90, radM)
      const edgeLL = L.latLng(edge.lat, edge.lon)
      const hCenter = mkHandle(c, (ll) => {
        c = ll; circle.setLatLng(ll)
        const e = destinationPoint(ll.lat, ll.lng, 90, radM)
        hEdge.setLatLng([e.lat, e.lon])
      })
      const hEdge = mkHandle(edgeLL, (ll) => {
        radM = haversine(c, ll); circle.setRadius(radM)
      })
      cleanup = () => {
        try { layerGroup.removeLayer(circle) } catch {}
        f.meta.center = { lat: c.lat, lng: c.lng }
        f.meta.radiusM = radM
        const newCircle = L.circle([c.lat, c.lng], { radius: radM, color, weight: 2, fillOpacity: 0.15 })
        f.layers = [newCircle, ...f.layers.slice(1)]
        hiddenLayers[0] = f.layers[0]
      }
    } else if (f.kind === 'rectangle') {
      const b = f.meta?.bounds
      if (!b) return
      hiddenLayers.forEach((l) => layerGroup.removeLayer(l))
      let sw = L.latLng(b._southWest?.lat ?? b[0][0], b._southWest?.lng ?? b[0][1])
      let ne = L.latLng(b._northEast?.lat ?? b[1][0], b._northEast?.lng ?? b[1][1])
      const rect = L.rectangle([sw, ne], { color, weight: 2, fillOpacity: 0.15 })
      layerGroup.addLayer(rect)
      const setBounds = () => rect.setBounds([sw, ne])
      const hSW = mkHandle(sw, (ll) => { sw = ll; setBounds() })
      const hNE = mkHandle(ne, (ll) => { ne = ll; setBounds() })
      const hNW = mkHandle(L.latLng(ne.lat, sw.lng), (ll) => { sw = L.latLng(sw.lat, ll.lng); ne = L.latLng(ll.lat, ne.lng); setBounds() })
      const hSE = mkHandle(L.latLng(sw.lat, ne.lng), (ll) => { sw = L.latLng(ll.lat, sw.lng); ne = L.latLng(ne.lat, ll.lng); setBounds() })
      void hSW; void hNE; void hNW; void hSE
      cleanup = () => {
        try { layerGroup.removeLayer(rect) } catch {}
        f.meta.bounds = [[sw.lat, sw.lng], [ne.lat, ne.lng]]
        const newRect = L.rectangle([sw, ne], { color, weight: 2, fillOpacity: 0.15 })
        f.layers = [newRect, ...f.layers.slice(1)]
        hiddenLayers[0] = f.layers[0]
      }
    } else if (f.kind === 'rb') {
      let start = L.latLng(f.meta?.start?.lat, f.meta?.start?.lng)
      let end = L.latLng(f.meta?.end?.lat, f.meta?.end?.lng)
      hiddenLayers.forEach((l) => layerGroup.removeLayer(l))
      const line = L.polyline([start, end], { color, weight: 2, dashArray: '4 4' })
      layerGroup.addLayer(line)
      const hA = mkHandle(start, (ll) => { start = ll; line.setLatLngs([start, end]) })
      const hB = mkHandle(end,   (ll) => { end = ll; line.setLatLngs([start, end]) })
      void hA; void hB
      cleanup = () => {
        try { layerGroup.removeLayer(line) } catch {}
        const dist = haversine(start, end)
        const az = bearing(start, end)
        const newLine = L.polyline([start, end], { color, weight: 2 })
        const mid = L.latLng((start.lat + end.lat) / 2, (start.lng + end.lng) / 2)
        const txt = `${dist >= 1000 ? (dist/1000).toFixed(2)+' km' : Math.round(dist)+' m'} · ${az.toFixed(1)}°`
        const lbl = L.marker(mid, { icon: makeLabelIcon(txt, color), interactive: false })
        f.meta = { ...f.meta, start: { lat: start.lat, lng: start.lng },
                              end: { lat: end.lat, lng: end.lng }, distM: dist, azimuth: az }
        f.layers = [newLine, lbl]
        hiddenLayers.length = 0   // we built fresh layers; don't restore old ones
      }
    } else {
      // Unsupported kinds: bail without entering edit mode (preserves the popup
      // UX — nothing changes if the kind isn't editable yet).
      return
    }

    editing = { id, handles, hiddenLayers, cleanup }
    notify()
  }
  function getEditingId() { return editing?.id || null }

  function focusFeature(id) {
    const f = features.get(id)
    if (!f) return
    const primary = f.layers[0]
    if (!primary) return
    if (primary.getBounds) {
      try { map.fitBounds(primary.getBounds(), { padding: [40, 40] }); return } catch {}
    }
    if (primary.getLatLng) {
      map.setView(primary.getLatLng(), Math.max(map.getZoom(), 13))
    }
  }

  function clearAll() {
    features.forEach(f => f.layers.forEach(l => layerGroup.removeLayer(l)))
    features.clear()
    notify()
  }

  // ── Click handler dispatch ──────────────────────────────────────────────
  async function onMapClick(e) {
    if (!activeTool) return
    L.DomEvent.stop(e.originalEvent)
    const { lat, lng } = e.latlng
    const ll = L.latLng(lat, lng)
    const tool = activeTool
    const color = activeStyle.color

    if (tool === 'point' || tool === 'milFriend' || tool === 'milHostile' || tool === 'milNeutral' || tool === 'milUnknown') {
      if (tool === 'point') {
        const m = L.marker(ll, { icon: makePinIcon(color), draggable: true })
        pushFeature('point', [m], { color })
      } else {
        const aff = tool.replace('mil', '').toLowerCase()
        const lbl = (await promptUser({
          title: `${aff[0].toUpperCase() + aff.slice(1)} marker label`,
          defaultValue: MIL_AFFILIATIONS[aff].label,
          placeholder: 'short label (1–3 chars)',
        })) || ''
        const m = L.marker(ll, { icon: makeMilIcon(aff, lbl), draggable: true })
        pushFeature(`mil-${aff}`, [m], { affiliation: aff, label: lbl })
      }
      return
    }

    // NATO MIL-STD-2525 / APP-6 symbol — picker pre-builds the L.divIcon and
    // passes it via setNatoSymbol({ sidc, icon, ... }).
    if (tool === 'nato') {
      if (!natoArm.sidc || !natoArm.icon) return
      const m = L.marker(ll, { icon: natoArm.icon, draggable: true })
      pushFeature('nato', [m], {
        sidc: natoArm.sidc,
        affiliation: natoArm.affiliation,
        label: natoArm.label,
        name: natoArm.name || natoArm.sidc,
      })
      return
    }

    // ATAK-style bullseye: centre, then radius for ring spacing. Drops 4
    // labelled rings + a centre marker + a North bearing tick.
    if (tool === 'bullseye') {
      if (!scratch) {
        const dot = L.circleMarker(ll, { radius: 5, color, fillColor: color, fillOpacity: 1 }).addTo(map)
        scratch = { kind: 'bullseye', center: ll, layers: [dot] }
      } else {
        const step = haversine(scratch.center, ll)
        const layers = []
        for (let k = 1; k <= 4; k++) {
          layers.push(L.circle(scratch.center, {
            radius: step * k, color, weight: k === 4 ? 1.5 : 1,
            opacity: 1 - k * 0.12, fill: false,
            dashArray: k % 2 === 0 ? '5 4' : null,
          }))
          const east = destinationPoint(scratch.center.lat, scratch.center.lng, 90, step * k)
          const lbl = step * k >= 1000 ? `${(step * k / 1000).toFixed(1)} km` : `${Math.round(step * k)} m`
          layers.push(L.marker([east.lat, east.lon], {
            icon: makeLabelIcon(lbl, color), interactive: false,
          }))
        }
        // Bearing ticks every 30°
        for (let az = 0; az < 360; az += 30) {
          const tip = destinationPoint(scratch.center.lat, scratch.center.lng, az, step * 4)
          const inner = destinationPoint(scratch.center.lat, scratch.center.lng, az, step * 4 - step * 0.05)
          layers.push(L.polyline([[inner.lat, inner.lon], [tip.lat, tip.lon]],
            { color, weight: 1.5, opacity: 0.8 }))
          if (az % 90 === 0) {
            const lbl = az === 0 ? 'N' : az === 90 ? 'E' : az === 180 ? 'S' : 'W'
            const out = destinationPoint(scratch.center.lat, scratch.center.lng, az, step * 4.15)
            layers.push(L.marker([out.lat, out.lon], {
              icon: makeLabelIcon(lbl, color), interactive: false,
            }))
          }
        }
        // Centre crosshair marker
        layers.push(L.marker(scratch.center, { icon: makePinIcon(color) }))
        const center = scratch.center
        clearScratch()
        pushFeature('bullseye', layers, {
          color, stepM: step, rings: 4,
          center: { lat: center.lat, lng: center.lng },
        })
      }
      return
    }

    if (tool === 'label') {
      const text = (await promptUser({
        title: 'Label',
        placeholder: 'Type the label text…',
        defaultValue: '',
      })) || ''
      if (!text.trim()) return
      // Anchor dot (small dot at the original click). Stays put; if the label is
      // dragged away, a leader line appears connecting them — same pattern as
      // PowerPoint callouts and ATAK telestrate labels.
      const anchor = L.circleMarker(ll, { radius: 3, color, fillColor: color, fillOpacity: 1, weight: 1.5 })
      const m = L.marker(ll, { icon: makeLabelIcon(text.trim(), color), draggable: true })
      let leader = null
      const ensureLeader = (target) => {
        const d = haversine(ll, target)
        if (d < 25) {
          if (leader) { try { layerGroup.removeLayer(leader); leader.remove() } catch {} ; leader = null }
          return
        }
        if (!leader) {
          leader = L.polyline([ll, target], { color, weight: 1.5, dashArray: '3 3', interactive: false })
          // add into the same feature later — we register it when first created
          layerGroup.addLayer(leader)
        } else {
          leader.setLatLngs([ll, target])
        }
      }
      m.on('drag', (ev) => ensureLeader(ev.target.getLatLng()))
      m.on('dragend', (ev) => ensureLeader(ev.target.getLatLng()))
      // The feature owns the marker + anchor dot + (lazily-added) leader. We pass
      // a getter so pushFeature's persistence picks the leader up when it exists.
      const layers = [anchor, m]
      const fid = pushFeature('label', layers, { text: text.trim(), color, name: text.trim(),
                                                  anchor: { lat: ll.lat, lng: ll.lng } })
      // Keep the feature record's layer list in sync as the leader appears or
      // disappears — otherwise removeFeature/export won't see it.
      const refreshFeatureLeader = () => {
        const feat = features.get(fid)
        if (!feat) return
        const has = feat.layers.includes(leader)
        if (leader && !has) feat.layers.push(leader)
        else if (!leader && has) feat.layers = feat.layers.filter(l => l !== leader)
      }
      m.on('drag dragend', refreshFeatureLeader)
      return
    }

    if (tool === 'rectangle') {
      if (!scratch) {
        const dot = L.circleMarker(ll, { radius: 4, color, fillColor: color, fillOpacity: 1 }).addTo(map)
        scratch = { kind: 'rectangle', start: ll, layers: [dot] }
      } else {
        const a = scratch.start
        const bounds = [[Math.min(a.lat, lat), Math.min(a.lng, lng)],
                        [Math.max(a.lat, lat), Math.max(a.lng, lng)]]
        const rect = L.rectangle(bounds, { color, weight: 2, fillOpacity: activeStyle.fillOpacity })
        clearScratch()
        pushFeature('rectangle', [rect], { color, bounds })
      }
      return
    }

    if (tool === 'circle') {
      if (!scratch) {
        const dot = L.circleMarker(ll, { radius: 4, color, fillColor: color, fillOpacity: 1 }).addTo(map)
        scratch = { kind: 'circle', center: ll, layers: [dot] }
      } else {
        const radius = haversine(scratch.center, ll)
        const center = scratch.center
        const c = L.circle(center, { radius, color, weight: 2, fillOpacity: activeStyle.fillOpacity })
        clearScratch()
        pushFeature('circle', [c], { color, center: { lat: center.lat, lng: center.lng }, radiusM: radius })
      }
      return
    }

    if (tool === 'ellipse') {
      if (!scratch) {
        const dot = L.circleMarker(ll, { radius: 4, color, fillColor: color, fillOpacity: 1 }).addTo(map)
        scratch = { kind: 'ellipse', center: ll, layers: [dot] }
      } else if (!scratch.aPoint) {
        scratch.aPoint = ll
        scratch.axisA = haversine(scratch.center, ll)
        scratch.rotation = bearing(scratch.center, ll)
        const dot = L.circleMarker(ll, { radius: 4, color, fillColor: color, fillOpacity: 1 }).addTo(map)
        scratch.layers.push(dot)
      } else {
        const axisB = haversine(scratch.center, ll)
        const ring = L.polygon(
          ellipsePolygonLatLngs(scratch.center, scratch.axisA, axisB, scratch.rotation),
          { color, weight: 2, fillOpacity: activeStyle.fillOpacity }
        )
        const center = scratch.center
        const meta = { color, axisA: scratch.axisA, axisB, rotation: scratch.rotation, center: { lat: center.lat, lng: center.lng } }
        clearScratch()
        pushFeature('ellipse', [ring], meta)
      }
      return
    }

    if (tool === 'rangeRings') {
      if (!scratch) {
        const dot = L.circleMarker(ll, { radius: 4, color, fillColor: color, fillOpacity: 1 }).addTo(map)
        scratch = { kind: 'rangeRings', center: ll, layers: [dot] }
      } else {
        const step = haversine(scratch.center, ll)
        // Optional per-ring labels (e.g. "Kill, Casualty, Lethal") — the number
        // of comma-separated entries determines how many rings get drawn (1-10).
        // Blank input falls back to 4 rings with auto km/m labels.
        let labels = null
        if (rangeRingsPrompt) {
          const v = await rangeRingsPrompt(step)
          if (v && v.trim()) {
            labels = v.split(',').map(s => s.trim()).filter(Boolean).slice(0, 10)
          }
        }
        const ringCount = labels?.length || 4
        const layers = []
        for (let k = 1; k <= ringCount; k++) {
          layers.push(L.circle(scratch.center, {
            radius: step * k, color, weight: 1.5, opacity: 0.9 - k * 0.15,
            fill: false, dashArray: k === 1 ? null : '5 5',
          }))
          const p = destinationPoint(scratch.center.lat, scratch.center.lng, 90, step * k)
          const auto = step * k >= 1000 ? `${(step * k / 1000).toFixed(2)} km` : `${Math.round(step * k)} m`
          const text = labels ? `${labels[k - 1]} · ${auto}` : auto
          layers.push(L.marker([p.lat, p.lon], { icon: makeLabelIcon(text, color), interactive: false }))
        }
        const center = scratch.center
        clearScratch()
        pushFeature('rangeRings', layers, {
          color, stepM: step, rings: ringCount,
          labels: labels || null,
          center: { lat: center.lat, lng: center.lng },
        })
      }
      return
    }

    if (tool === 'rb') {
      if (!scratch) {
        const dot = L.circleMarker(ll, { radius: 4, color, fillColor: color, fillOpacity: 1 }).addTo(map)
        scratch = { kind: 'rb', start: ll, layers: [dot] }
      } else {
        const dist = haversine(scratch.start, ll)
        const az = bearing(scratch.start, ll)
        const line = L.polyline([scratch.start, ll], { color, weight: 2 })
        const mid = L.latLng((scratch.start.lat + lat) / 2, (scratch.start.lng + lng) / 2)
        const txt = `${dist >= 1000 ? (dist/1000).toFixed(2)+' km' : Math.round(dist)+' m'} · ${az.toFixed(1)}°`
        const lbl = L.marker(mid, { icon: makeLabelIcon(txt, color), interactive: false })
        const start = scratch.start
        clearScratch()
        pushFeature('rb', [line, lbl], { color, start: { lat: start.lat, lng: start.lng }, end: { lat, lng }, distM: dist, azimuth: az })
      }
      return
    }

    if (tool === 'pairing') {
      // Pairing line — two draggable endpoints with a live range+bearing label.
      // Move either endpoint and the line + label rebuild themselves. Same
      // primitive as rb but mutable.
      if (!scratch) {
        const dot = L.circleMarker(ll, { radius: 5, color, fillColor: color, fillOpacity: 1, weight: 2 }).addTo(map)
        scratch = { kind: 'pairing', start: ll, layers: [dot] }
      } else {
        const startLL = scratch.start
        const a = L.marker(startLL, { icon: makeAnchorIcon(color), draggable: true })
        const b = L.marker(ll, { icon: makeAnchorIcon(color), draggable: true })
        const line = L.polyline([a.getLatLng(), b.getLatLng()], { color, weight: 2 })
        const lbl = L.marker(L.latLng((startLL.lat + lat) / 2, (startLL.lng + lng) / 2),
                             { icon: makeLabelIcon('', color), interactive: false })
        const refresh = () => {
          const ap = a.getLatLng(), bp = b.getLatLng()
          const d = haversine(ap, bp)
          const az = bearing(ap, bp)
          line.setLatLngs([ap, bp])
          const mid = L.latLng((ap.lat + bp.lat) / 2, (ap.lng + bp.lng) / 2)
          const txt = `${d >= 1000 ? (d/1000).toFixed(2)+' km' : Math.round(d)+' m'} · ${az.toFixed(1)}°`
          lbl.setLatLng(mid)
          lbl.setIcon(makeLabelIcon(txt, color))
        }
        a.on('drag', refresh); b.on('drag', refresh)
        refresh()
        clearScratch()
        pushFeature('pairing', [line, a, b, lbl], {
          color,
          start: { lat: startLL.lat, lng: startLL.lng },
          end: { lat, lng },
        })
      }
      return
    }

    if (tool === 'fan') {
      if (!scratch) {
        const dot = L.circleMarker(ll, { radius: 4, color, fillColor: color, fillOpacity: 1 }).addTo(map)
        scratch = { kind: 'fan', center: ll, layers: [dot] }
      } else if (!scratch.startBearing) {
        scratch.startBearing = bearing(scratch.center, ll)
        scratch.radius = haversine(scratch.center, ll)
        const dot = L.circleMarker(ll, { radius: 4, color, fillColor: color, fillOpacity: 1 }).addTo(map)
        scratch.layers.push(dot)
      } else {
        const endBearing = bearing(scratch.center, ll)
        const poly = L.polygon(
          fanLatLngs(scratch.center, scratch.radius, scratch.startBearing, endBearing),
          { color, weight: 2, fillOpacity: activeStyle.fillOpacity }
        )
        const center = scratch.center
        const meta = { color, radius: scratch.radius, b1: scratch.startBearing, b2: endBearing,
                       center: { lat: center.lat, lng: center.lng } }
        clearScratch()
        pushFeature('fan', [poly], meta)
      }
      return
    }

    if (tool === 'line' || tool === 'polygon' || tool === 'geofence'
        || tool === 'phaseLine' || tool === 'flot' || tool === 'axisAdvance' || tool === 'boundary') {
      if (!scratch) scratch = { kind: tool, points: [], layers: [] }
      scratch.points.push([lat, lng])
      const dot = L.circleMarker(ll, { radius: 4, color, fillColor: color, fillOpacity: 1 }).addTo(map)
      scratch.layers.push(dot)
      // Live preview — all "line-collection" tools share the dashed-line preview.
      if (scratch.preview) { map.removeLayer(scratch.preview); scratch.preview = null }
      const isPolyish = tool === 'polygon' || tool === 'geofence'
      if (!isPolyish && scratch.points.length >= 2) {
        scratch.preview = L.polyline(scratch.points, { color, weight: 2, dashArray: '5 4' }).addTo(map)
      } else if (isPolyish && scratch.points.length >= 2) {
        scratch.preview = L.polyline([...scratch.points, scratch.points[0]],
          { color, weight: 2, dashArray: '5 4' }).addTo(map)
      }
    }
  }

  function onMapDblClick(e) {
    if (!activeTool || !scratch) return
    L.DomEvent.stop(e.originalEvent)
    finishCurrent()
  }

  // Tools that build a feature via N consecutive clicks (not a polyline of
  // arbitrary points). For these, a right-click mid-draw cancels the
  // in-progress shape; for line-collection tools right-click commits.
  const _MULTICLICK_KINDS = new Set([
    'rb', 'pairing', 'rangeRings', 'fan', 'ellipse', 'rectangle', 'circle',
  ])

  function onMapContextMenu(e) {
    if (!activeTool || !scratch) return
    L.DomEvent.stop(e.originalEvent)
    if (_MULTICLICK_KINDS.has(scratch.kind)) {
      // Mid-draw cancel — operator wants out of this shape but stays on the tool.
      cancelCurrent()
    } else {
      // Line / polygon / geofence / freehand / phaseLine / flot / axisAdvance /
      // boundary → r-click commits.
      finishCurrent()
    }
  }

  function cancelCurrent() {
    if (!scratch) return
    clearScratch()
    notify()
  }

  // Freehand: mousedown → drag → mouseup
  let freehandActive = false
  function onMouseDown(e) {
    if (activeTool !== 'freehand') return
    L.DomEvent.stop(e.originalEvent)
    freehandActive = true
    map.dragging.disable()
    scratch = { kind: 'freehand', points: [[e.latlng.lat, e.latlng.lng]], layers: [] }
  }
  function onMouseMove(e) {
    if (!freehandActive || !scratch || activeTool !== 'freehand') return
    scratch.points.push([e.latlng.lat, e.latlng.lng])
    if (scratch.preview) { map.removeLayer(scratch.preview) }
    scratch.preview = L.polyline(scratch.points, { color: activeStyle.color, weight: 2 }).addTo(map)
  }
  function onMouseUp() {
    if (!freehandActive) return
    freehandActive = false
    map.dragging.enable()
    finishCurrent()
  }

  async function finishCurrent() {
    if (!scratch) return
    const tool = scratch.kind
    const color = activeStyle.color
    if (tool === 'line' && scratch.points.length >= 2) {
      const points = scratch.points
      const line = L.polyline(points, { color, weight: 2 })
      clearScratch()
      pushFeature('line', [line], { color, points })
    } else if ((tool === 'polygon' || tool === 'geofence') && scratch.points.length >= 3) {
      const poly = L.polygon(scratch.points,
        { color, weight: 2, fillOpacity: activeStyle.fillOpacity, dashArray: tool === 'geofence' ? '6 4' : null })
      const layers = [poly]
      if (tool === 'geofence') {
        const centre = poly.getBounds().getCenter()
        layers.push(L.marker(centre, { icon: makeLabelIcon('GEOFENCE', '#ef4444'), interactive: false }))
      }
      clearScratch()
      pushFeature(tool, layers, { color })
    } else if (tool === 'freehand' && scratch.points && scratch.points.length >= 2) {
      const line = L.polyline(scratch.points, { color, weight: 2 })
      clearScratch()
      pushFeature('freehand', [line], { color })
    } else if (tool === 'phaseLine' && scratch.points.length >= 2) {
      const pts = scratch.points
      const line = L.polyline(pts, { color, weight: 2.5 })
      // Midpoint label "PL <name>" — name prompted on finish; blank → generic "PL".
      let name = ''
      try {
        name = (await promptUser({ title: 'Phase line name', placeholder: 'e.g. ALPHA', defaultValue: '' })) || ''
      } catch {}
      const mid = pts[Math.floor(pts.length / 2)]
      const lbl = L.marker(mid, { icon: makeLabelIcon(`PL ${name || '—'}`, color), interactive: false })
      clearScratch()
      pushFeature('phaseLine', [line, lbl], { color, points: pts, name })
    } else if (tool === 'flot' && scratch.points.length >= 2) {
      // FLOT = Forward Line of Own Troops. Drawn as the spine polyline plus
      // half-circle teeth at every segment midpoint, biased to the right of the
      // direction of travel (a US convention; flip via opts.flotSide if needed).
      const pts = scratch.points
      const spine = L.polyline(pts, { color, weight: 2.5 })
      const teeth = []
      const toothR = 60   // metres — scales fine at typical operational zoom
      for (let i = 0; i < pts.length - 1; i++) {
        const a = { lat: pts[i][0], lng: pts[i][1] }
        const b = { lat: pts[i + 1][0], lng: pts[i + 1][1] }
        const segBearing = bearing(a, b)
        const dist = haversine(a, b)
        // place teeth every ~120m along the segment
        const n = Math.max(1, Math.floor(dist / 120))
        for (let k = 0; k < n; k++) {
          const t = (k + 0.5) / n
          const mid = destinationPoint(a.lat, a.lng, segBearing, dist * t)
          // perpendicular to the right
          const cx = destinationPoint(mid.lat, mid.lon, (segBearing + 90) % 360, toothR * 0.5)
          // half-circle (10 segments) of radius toothR/2 centered at cx, bulging right
          const arc = []
          for (let s = 0; s <= 10; s++) {
            const ang = (segBearing - 90) + (180 * s / 10)
            const p = destinationPoint(cx.lat, cx.lon, (ang + 360) % 360, toothR * 0.5)
            arc.push([p.lat, p.lon])
          }
          teeth.push(L.polyline(arc, { color, weight: 2 }))
        }
      }
      const layers = [spine, ...teeth]
      clearScratch()
      pushFeature('flot', layers, { color, points: pts })
    } else if (tool === 'axisAdvance' && scratch.points.length >= 2) {
      // Axis of Advance: a broad-arrow polygon along the polyline. We thicken the
      // line into a parallelogram and add a triangular head at the last point.
      const pts = scratch.points
      const widthM = 200          // half-width of the axis body in metres
      const headLenM = 350        // arrow head length along axis
      const headWidM = 380        // half-width of the head base
      const leftEdge = []
      const rightEdge = []
      for (let i = 0; i < pts.length; i++) {
        const here = { lat: pts[i][0], lng: pts[i][1] }
        // Tangent from neighbouring segments
        let segB
        if (i === 0) segB = bearing(here, { lat: pts[i + 1][0], lng: pts[i + 1][1] })
        else if (i === pts.length - 1) segB = bearing({ lat: pts[i - 1][0], lng: pts[i - 1][1] }, here)
        else {
          const b1 = bearing({ lat: pts[i - 1][0], lng: pts[i - 1][1] }, here)
          const b2 = bearing(here, { lat: pts[i + 1][0], lng: pts[i + 1][1] })
          // Average two bearings via unit-vector mean (handles wrap)
          const rad = d => d * Math.PI / 180
          const x = Math.cos(rad(b1)) + Math.cos(rad(b2))
          const y = Math.sin(rad(b1)) + Math.sin(rad(b2))
          segB = ((Math.atan2(y, x) * 180 / Math.PI) + 360) % 360
        }
        if (i < pts.length - 1) {
          const L_ = destinationPoint(here.lat, here.lng, (segB - 90 + 360) % 360, widthM)
          const R_ = destinationPoint(here.lat, here.lng, (segB + 90) % 360, widthM)
          leftEdge.push([L_.lat, L_.lon])
          rightEdge.push([R_.lat, R_.lon])
        }
      }
      // Arrow head: at the tip, expand to headWidM and project forward headLenM.
      const tip = { lat: pts[pts.length - 1][0], lng: pts[pts.length - 1][1] }
      const tipBearing = bearing({ lat: pts[pts.length - 2][0], lng: pts[pts.length - 2][1] }, tip)
      const tipFwd = destinationPoint(tip.lat, tip.lng, tipBearing, headLenM)
      const headL = destinationPoint(tip.lat, tip.lng, (tipBearing - 90 + 360) % 360, headWidM)
      const headR = destinationPoint(tip.lat, tip.lng, (tipBearing + 90) % 360, headWidM)
      const polyPts = [
        ...leftEdge,
        [headL.lat, headL.lon],
        [tipFwd.lat, tipFwd.lon],
        [headR.lat, headR.lon],
        ...rightEdge.reverse(),
      ]
      const poly = L.polygon(polyPts, { color, weight: 2, fillOpacity: activeStyle.fillOpacity * 0.7 })
      clearScratch()
      pushFeature('axisAdvance', [poly], { color, points: pts, widthM, headLenM, headWidM })
    } else if (tool === 'boundary' && scratch.points.length >= 2) {
      // Boundary = alternating long-dash line with short perpendicular tic marks at
      // each interior vertex. Operational symbology for unit boundaries.
      const pts = scratch.points
      const line = L.polyline(pts, { color, weight: 3, dashArray: '14 8' })
      const tics = []
      for (let i = 0; i < pts.length - 1; i++) {
        const a = { lat: pts[i][0], lng: pts[i][1] }
        const b = { lat: pts[i + 1][0], lng: pts[i + 1][1] }
        const segB = bearing(a, b)
        const tStart = destinationPoint(a.lat, a.lng, (segB - 90 + 360) % 360, 120)
        const tEnd = destinationPoint(a.lat, a.lng, (segB + 90) % 360, 120)
        tics.push(L.polyline([[tStart.lat, tStart.lon], [tEnd.lat, tEnd.lon]],
          { color, weight: 2 }))
      }
      clearScratch()
      pushFeature('boundary', [line, ...tics], { color, points: pts })
    } else {
      clearScratch()
    }
  }

  function activate(tool) {
    if (activeTool === tool) return
    deactivate()
    activeTool = tool
    setCursor(true)
    map.on('click', onMapClick)
    map.on('dblclick', onMapDblClick)
    map.on('contextmenu', onMapContextMenu)
    if (tool === 'freehand') {
      map.on('mousedown', onMouseDown)
      map.on('mousemove', onMouseMove)
      map.on('mouseup', onMouseUp)
      map.doubleClickZoom.disable()
    }
    notify()
  }

  function deactivate() {
    if (!activeTool) { activeTool = null; notify(); return }
    setCursor(false)
    clearScratch()
    map.off('click', onMapClick)
    map.off('dblclick', onMapDblClick)
    map.off('contextmenu', onMapContextMenu)
    map.off('mousedown', onMouseDown)
    map.off('mousemove', onMouseMove)
    map.off('mouseup', onMouseUp)
    try { map.doubleClickZoom.enable() } catch {}
    if (freehandActive) { try { map.dragging.enable() } catch {}; freehandActive = false }
    activeTool = null
    notify()
  }

  function setStyle(style) {
    activeStyle = { ...activeStyle, ...style }
  }

  function exportGeoJSON() {
    const fc = { type: 'FeatureCollection', features: [] }
    features.forEach(f => {
      f.layers.forEach((layer, idx) => {
        try {
          if (typeof layer.toGeoJSON !== 'function') return
          const gj = layer.toGeoJSON()
          if (gj.type === 'Feature') {
            gj.properties = { ...gj.properties, mv_id: f.id, mv_kind: f.kind, mv_part: idx, ...f.meta }
            fc.features.push(gj)
          } else if (gj.type === 'FeatureCollection') {
            gj.features.forEach(g => {
              g.properties = { ...g.properties, mv_id: f.id, mv_kind: f.kind, mv_part: idx, ...f.meta }
              fc.features.push(g)
            })
          }
        } catch {}
      })
    })
    return fc
  }

  // Rebuild draw features from a FeatureCollection produced by exportGeoJSON.
  // Groups features by mv_id and reconstructs Leaflet layers per geometry.
  // Multi-part composite features (bullseye, rangeRings, rb, etc.) recover
  // their geometric shape; minor decorative labels may render as plain pins.
  function importGeoJSON(fc) {
    if (!fc || !Array.isArray(fc.features) || fc.features.length === 0) return
    const groups = new Map()  // mv_id → { kind, meta, parts: [{part, feature}] }
    fc.features.forEach(feat => {
      const props = feat?.properties || {}
      const mvId = props.mv_id || `__imp_${Math.random().toString(36).slice(2)}`
      if (!groups.has(mvId)) {
        const meta = {}
        Object.keys(props).forEach(k => {
          if (k === 'mv_id' || k === 'mv_kind' || k === 'mv_part') return
          meta[k] = props[k]
        })
        groups.set(mvId, { kind: props.mv_kind || 'imported', meta, parts: [] })
      }
      groups.get(mvId).parts.push({ part: Number(props.mv_part) || 0, feature: feat })
    })

    groups.forEach(g => {
      g.parts.sort((a, b) => a.part - b.part)
      const layers = []
      g.parts.forEach(({ feature }) => {
        const lyr = featureToLayer(feature, g.kind, g.meta)
        if (lyr) layers.push(lyr)
      })
      if (layers.length === 0) return
      const id = nextFeatureId()
      layers.forEach(l => layerGroup.addLayer(l))
      const primary = layers[0]
      const name = g.meta.name || defaultName(g.kind)
      g.meta.name = name
      if (primary?.bindPopup) primary.bindPopup(() => buildPopup(id, name, g.meta))
      layers.forEach(l => { try { l.on?.('click', () => opts.onFeatureClick?.(id)) } catch {} })
      features.set(id, { id, kind: g.kind, layers, meta: g.meta })
    })
    notify()
  }

  function featureToLayer(feature, kind, meta) {
    const g = feature?.geometry
    if (!g) return null
    const color = meta.color || activeStyle.color
    const fillOpacity = activeStyle.fillOpacity ?? 0.15
    const props = feature.properties || {}

    if (g.type === 'Point') {
      const [lon, lat] = g.coordinates
      const ll = L.latLng(lat, lon)
      // L.Circle.toGeoJSON serializes as Point with properties.radius
      if (props.radius != null && Number.isFinite(Number(props.radius))) {
        return L.circle(ll, { radius: Number(props.radius), color, weight: 2, fillOpacity })
      }
      if (kind === 'label' && (meta.text || meta.name)) {
        return L.marker(ll, { icon: makeLabelIcon(meta.text || meta.name, color), draggable: true })
      }
      if (typeof kind === 'string' && kind.startsWith('mil-')) {
        const aff = kind.slice(4)
        return L.marker(ll, { icon: makeMilIcon(aff, meta.label || ''), draggable: true })
      }
      return L.marker(ll, { icon: makePinIcon(color), draggable: kind === 'point' })
    }
    if (g.type === 'LineString') {
      return L.polyline(g.coordinates.map(([x, y]) => [y, x]), { color, weight: 2 })
    }
    if (g.type === 'MultiLineString') {
      return L.polyline(g.coordinates.map(line => line.map(([x, y]) => [y, x])), { color, weight: 2 })
    }
    if (g.type === 'Polygon') {
      return L.polygon(g.coordinates.map(r => r.map(([x, y]) => [y, x])), { color, weight: 2, fillOpacity })
    }
    if (g.type === 'MultiPolygon') {
      return L.polygon(g.coordinates.map(p => p.map(r => r.map(([x, y]) => [y, x]))), { color, weight: 2, fillOpacity })
    }
    return null
  }

  function destroy() {
    deactivate()
    layerGroup.clearLayers()
    map.removeLayer(layerGroup)
    listeners.clear()
    features.clear()
  }

  function setNatoSymbol(arm) {
    natoArm = { ...natoArm, ...arm }
  }
  function getNatoSymbol() { return { ...natoArm } }

  return {
    activate,
    deactivate,
    setStyle,
    setNatoSymbol,
    getNatoSymbol,
    finishCurrent,
    cancelCurrent,
    hasScratch: () => scratch !== null,
    clearAll,
    removeFeature,
    focusFeature,
    editFeature,
    exitEditMode,
    getEditingId,
    exportGeoJSON,
    importGeoJSON,
    destroy,
    onChange: (fn) => { listeners.add(fn); return () => listeners.delete(fn) },
    listFeatures: () => Array.from(features.values()).map(f => ({ id: f.id, kind: f.kind, meta: f.meta })),
    getActiveTool: () => activeTool,
    getLayerGroup: () => layerGroup,
  }
}

export const TOOL_KINDS = {
  basic: [
    { id: 'point',     label: 'Point',     icon: '⦿' },
    { id: 'label',     label: 'Label',     icon: 'A' },
    { id: 'line',      label: 'Line',      icon: '╱' },
    { id: 'polygon',   label: 'Polygon',   icon: '◊' },
    { id: 'rectangle', label: 'Rectangle', icon: '▭' },
    { id: 'circle',    label: 'Circle',    icon: '○' },
    { id: 'freehand',  label: 'Freehand',  icon: '✎' },
  ],
  advanced: [
    { id: 'ellipse',     label: 'Ellipse',     icon: '⬭' },
    { id: 'rb',          label: 'Range/Bearing', icon: '⤳' },
    { id: 'rangeRings',  label: 'Range Rings', icon: '◎' },
    { id: 'bullseye',    label: 'Bullseye',    icon: '◉' },
    { id: 'fan',         label: 'Fan / Wedge', icon: '⌖' },
    { id: 'geofence',    label: 'Geofence',    icon: '⚠' },
    { id: 'pairing',     label: 'Pairing',     icon: '⇋' },
  ],
  briefing: [
    { id: 'phaseLine',   label: 'Phase Line',  icon: '┄' },
    { id: 'flot',        label: 'FLOT',        icon: '⌒' },
    { id: 'axisAdvance', label: 'Axis Advance', icon: '➤' },
    { id: 'boundary',    label: 'Boundary',    icon: '┼' },
  ],
  military: [
    { id: 'milFriend',  label: 'Friendly',  icon: '▭', color: '#3b82f6' },
    { id: 'milHostile', label: 'Hostile',   icon: '◆', color: '#ef4444' },
    { id: 'milNeutral', label: 'Neutral',   icon: '■', color: '#22c55e' },
    { id: 'milUnknown', label: 'Unknown',   icon: '?', color: '#facc15' },
  ],
}
