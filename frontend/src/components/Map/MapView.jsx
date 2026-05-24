// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Interactive Map View
 * Leaflet map with coverage heatmap overlay, TX/RX markers, terrain profile line,
 * extra TX markers, multi-TX coverage layers, and a ruler tool.
 */
import { useEffect, useRef, useCallback, useState } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { signalToColor } from '../../api/client'
import CoverageLegend from './CoverageLegend'
import { useViewMode } from '../../hooks/useViewMode'
import { BASEMAPS as TILE_LAYERS, DEFAULT_MAP_COLORS, useMapPrefs } from './mapPrefs'
import CompassRose from './CompassRose'
import { useTrackHistory, trackPositionAt } from '../../store/trackHistory'
import MapSettingsCog from './MapSettingsCog'
import { makeSidcIcon } from './NatoSymbols'
import { geocodeNominatim } from '../../utils/geocode'
import { formatDistance } from '../../utils/units'
import {
  destinationPoint,
  computeGroupIntersections,
  computeCentroid,
  computeCAPEllipse,
  lobGroupKey,
  computeLoBRenderDistance,
  INTERSECTION_FALLBACK_M,
} from '../Geolocation/LoBUtils'
import { lazy, Suspense } from 'react'
import { createDrawController, TOOL_KINDS } from './DrawTools'
import { loadFiles, SUPPORTED_EXTENSIONS } from '../../utils/fileLoaders'
import { formatCoordinate, toDDM, autoParseCoordinate, toMGRSAt, mgrsPrecisionForSize } from '../../utils/units'

// milsymbol is large (~860 kB unminified). Defer until the picker is opened.
const NatoSymbolPicker = lazy(() => import('./NatoSymbolPicker'))
import ErrorBoundary from '../Common/ErrorBoundary'

// DEFAULT_MAP_COLORS + TILE_LAYERS + geocodeNominatim are shared with the 3D
// globe — see ./mapPrefs and ../../utils/geocode.

// Build TX icon HTML from a given color
function makeTxIcon(color = '#00b4d8') {
  return L.divIcon({
    className: '',
    html: `<div style="
      width:28px;height:28px;border-radius:50%;
      background:linear-gradient(135deg,${color},${color}99);
      border:3px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,0.6),0 0 0 4px ${color}4d;
      display:flex;align-items:center;justify-content:center;cursor:grab;
    ">
      <svg width="12" height="12" fill="none" stroke="black" stroke-width="2"
           viewBox="0 0 24 24">
        <path d="M5 12.55a11 11 0 0 1 14.08 0M1.42 9a16 16 0 0 1 21.16 0
                 M8.53 16.11a6 6 0 0 1 6.95 0M12 20h.01"/>
      </svg>
    </div>`,
    iconSize: [28, 28],
    iconAnchor: [14, 14],
  })
}

// Semi-transparent compass rose overlay
// CompassRose moved to ./CompassRose.jsx so the 3D globe can render it with
// live camera heading. Imported below.

// Great-circle bearing between two points
function bearing(lat1, lon1, lat2, lon2) {
  const toRad = d => d * Math.PI / 180
  const toDeg = r => r * 180 / Math.PI
  const dLon = toRad(lon2 - lon1)
  const y = Math.sin(dLon) * Math.cos(toRad(lat2))
  const x = Math.cos(toRad(lat1)) * Math.sin(toRad(lat2)) -
    Math.sin(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.cos(dLon)
  return (toDeg(Math.atan2(y, x)) + 360) % 360
}

// Haversine distance in meters
function haversine(lat1, lon1, lat2, lon2) {
  const R = 6371000
  const toRad = d => d * Math.PI / 180
  const dLat = toRad(lat2 - lat1)
  const dLon = toRad(lon2 - lon1)
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a))
}

// Parse a string in any supported coordinate format (DD, DMS, DDM, MGRS, UTM,
// Maidenhead). Returns {lat, lon} or null.
function parseAnyCoord(q) {
  const s = String(q || '').trim()
  if (!s) return null
  // Maidenhead: 4 or 6 chars like "IO91" or "IO91WL"
  const mh = /^([A-R])([A-R])(\d)(\d)(?:([A-X])([A-X]))?$/i.exec(s.replace(/\s+/g, ''))
  if (mh) {
    const A = 'A'.charCodeAt(0)
    const lonField = mh[1].toUpperCase().charCodeAt(0) - A
    const latField = mh[2].toUpperCase().charCodeAt(0) - A
    let lon = lonField * 20 + parseInt(mh[3], 10) * 2 - 180
    let lat = latField * 10 + parseInt(mh[4], 10) - 90
    if (mh[5]) {
      const a = 'A'.charCodeAt(0)
      lon += (mh[5].toUpperCase().charCodeAt(0) - a) * (5 / 60) + 2.5 / 60
      lat += (mh[6].toUpperCase().charCodeAt(0) - a) * (2.5 / 60) + 1.25 / 60
    } else {
      lon += 1; lat += 0.5
    }
    if (lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180) return { lat, lon }
  }
  // DDM: "51°30.435'N 0°7.65'W"
  const norm = s.replace(/[°'"]/g, ' ').replace(/\s+/g, ' ').trim()
  const ddm = /^(\d+)\s+(\d+(?:\.\d+)?)\s*([NS])\s*[,\s]\s*(\d+)\s+(\d+(?:\.\d+)?)\s*([EW])$/i.exec(norm)
  if (ddm) {
    const lat = (parseInt(ddm[1], 10) + parseFloat(ddm[2]) / 60) * (ddm[3].toUpperCase() === 'S' ? -1 : 1)
    const lon = (parseInt(ddm[4], 10) + parseFloat(ddm[5]) / 60) * (ddm[6].toUpperCase() === 'W' ? -1 : 1)
    if (lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180) return { lat, lon }
  }
  // Fall back to existing autoParser (DD, DMS, MGRS, UTM)
  const auto = autoParseCoordinate(s)
  if (auto) return auto
  return parseCoords(s)
}

// Parse a string as lat/lon. Returns {lat, lon} or null.
function parseCoords(q) {
  // Remove degree symbols and normalize separators
  const s = q.replace(/[°'"]/g, ' ').replace(/\s+/g, ' ').trim()
  // Decimal: "51.5074, -0.1278" or "51.5074 -0.1278"
  const dec = s.match(/^(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)$/)
  if (dec) {
    const lat = parseFloat(dec[1]), lon = parseFloat(dec[2])
    if (lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180) return { lat, lon }
  }
  // DMS with N/S/E/W: "51 30 26 N 0 07 39 W"
  const dms = s.match(/^(\d+)\s+(\d+)\s+(\d*\.?\d*)\s*([NS])[,\s]+(\d+)\s+(\d+)\s+(\d*\.?\d*)\s*([EW])$/i)
  if (dms) {
    const lat = (parseFloat(dms[1]) + parseFloat(dms[2]) / 60 + parseFloat(dms[3]) / 3600) * (dms[4].toUpperCase() === 'S' ? -1 : 1)
    const lon = (parseFloat(dms[5]) + parseFloat(dms[6]) / 60 + parseFloat(dms[7]) / 3600) * (dms[8].toUpperCase() === 'W' ? -1 : 1)
    if (lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180) return { lat, lon }
  }
  return null
}

export default function MapView({
  tx, txLabel = 'TX 1', rxPoint, coverageGeoJSON, buildingGeoJSON, extraTxList = [], gpsFix = null, gpsTrackers = [],
  p2pProfile, activeTab, minSignalDbm,
  onMapClick, onTxDrag, onRxDrag, onExtraTxDrag,
  distUnit = 'metric', setDistUnit,
  coordSystem = 'latlon', setCoordSystem,
  // Draw tool props
  drawMode = null,   // null | 'bounds' | 'polygon' | 'route' | 'multipoint' | 'manet'
  onDrawComplete,    // callback(type, coords)
  onToggleBoundsDraw,  // toggle coverage-bounds draw — surfaced inside the ✎ tools dropdown (propagation mode)
  extraGeojsonLayers = [],   // [{id, geojson, color?}] — for MANET, route, satellite results
  bestSiteCandidates = [],   // [{lat, lon, label}] — candidate sites clicked on the map (Best Site tab)
  bestSiteResult = null,     // { sites: [{lat, lon, ...}], ... } — ranking result; sites[0] is the winner
  // Geolocation / LoB props
  lobs = [],         // array of LoB objects
  lobGroups = [],    // pre-computed frequency groups (from App.jsx useMemo)
  capGroups = {},    // { [groupKey]: boolean } — missing key = default true
  lobAlgorithm,      // line-length algorithm config (see LoBUtils.DEFAULT_LOB_ALGORITHM)
  lobPickingMode = false,  // cursor hint when picking observer location
  lobAzimuthPickingMode = false,  // cursor hint when picking azimuth target
  // Emitter placement
  txActive = false,  // false = no TX marker on map yet
  onAddEmitter,      // (lat, lon) → place/move primary emitter
  onAddLoBObserver,  // (lat, lon) → pre-fill LoB observer location
  onDownloadRegionAt,  // (lat, lon) → open the Layer Manager and pre-select the region containing that point
  onViewshedAt,        // (lat, lon) → compute and overlay viewshed from this point
  onContoursAt,        // (lat, lon) → compute and overlay contour lines around this point
  hasViewsheds = false, onClearViewsheds,  // Clear-all entries in the right-click menu —
  hasContours  = false, onClearContours,   // only shown when the corresponding layers exist.
  onSimulatePropagationFromFix,   // (groupSummary, lat, lon) → attach a propagation TX that tracks this fix/cut
  onAddLoBAzimuthTarget,  // (lat, lon) → set the bearing target for the LoB form
  // Map display options (now in the floating-toolbar ⚙ MapSettingsCog)
  showCompassRose = false, setShowCompassRose,
  mapBrightness = 100, setMapBrightness,
  // Location search / saved locations
  flyToTarget = null,   // {lat, lon, zoom?} — change ref to trigger fly
  onSaveLocation,       // (loc: {name, lat, lon}) => void
  // Imports — parent can register an api object to open file dialog or push files
  onImportApi,          // (api: { openFileDialog, importFiles }) => void
  // Unified user-layer hook (from useUserLayers)
  ul,
  // Optional: line-draw mode for standalone terrain profile
  terrainLineMode = false,
  onTerrainLineComplete,    // (path: [[lat, lon], ...]) => void
  // Persistent state-driven feature arrays (rendered as clickable markers so Delete-key /
  // trash-can can target them). MANET nodes carry stable ids; multipoint TXs / route waypoints
  // use their array index as the id so removal still works without renumbering hassle.
  multipointTxs = [],
  manetNodes = [],
  routeWaypoints = [],
  // Map-feature selection — set by marker click handlers, consumed by the Delete key in App.jsx.
  // mapSel: null | { kind: 'primary_tx'|'rx'|'extra_tx'|'multipoint_tx'|'manet_node'|'route_waypoint', id? }
  mapSel = null,
  onSelectFeature,            // (sel) => void
}) {
  const viewMode = useViewMode((s) => s.mode)
  const setViewMode = useViewMode((s) => s.setMode)
  const mapRef = useRef(null)
  const leafletRef = useRef(null)
  const txMarkerRef = useRef(null)
  const rxMarkerRef = useRef(null)
  const gpsMarkerRef = useRef(null)
  const coverageLayerRef = useRef(null)
  const extraTxMarkersRef = useRef({})   // id → L.Marker
  const multipointMarkersRef = useRef([]) // L.CircleMarker[], parallel to multipointTxs
  const manetMarkersRef = useRef({})      // id → L.Marker (driven by state, separate from result-geojson copy)
  const routeMarkersRef = useRef([])      // L.CircleMarker[], parallel to routeWaypoints
  const selectFeatureRef = useRef(onSelectFeature)
  const extraLayersRef = useRef({})      // id → L.GeoJSON layer
  const p2pLineRef = useRef(null)
  const buildingLayerRef = useRef(null)
  const globeDrawLayerRef = useRef(null)   // L.geoJSON layer mirroring ul.globeDrawnGeoJSON (drawn on the 3D globe)
  const tileRef = useRef(null)
  const activeTabRef = useRef(activeTab)
  const drawRef = useRef({ points: [], markers: [], lines: [], rect: null })
  const drawModeRef = useRef(drawMode)
  const extraGeoLayersRef = useRef({})   // id → L.GeoJSON layer
  const bestSiteLayerRef = useRef(null)  // L.LayerGroup for the Best-Site candidate markers
  const lobLayerGroupRef = useRef(null)  // L.LayerGroup for all LoB visuals
  const onMapClickRef = useRef(onMapClick)  // kept current so the stale click closure sees latest callback
  // Multi-ruler refs
  const pendingRulerRef = useRef({ points: [], markers: [] })
  const completedRulersRef = useRef([])  // [{id, line, markers, labelMarker}]
  const rulerModeRef = useRef(false)        // mirror of rulerMode for use inside marker handlers
  const addRulerPointRef = useRef(null)     // stable callback: (lat, lng) => void
  const lobPickingModeRef = useRef(false)   // mirror of lobPickingMode for marker handlers
  const onAddLoBObserverRef = useRef(null)  // mirror of onAddLoBObserver callback
  const lobAzimuthPickingModeRef = useRef(false)
  const onAddLoBAzimuthTargetRef = useRef(null)
  const mapColorsRef = useRef(DEFAULT_MAP_COLORS)
  const tileStyle = useMapPrefs((s) => s.basemapId)
  const setTileStyle = useMapPrefs((s) => s.setBasemapId)
  const [rulerMode, setRulerMode] = useState(false)
  const [rulerResults, setRulerResults] = useState([])   // [{id, dist, hdg}]
  const [pendingPoints, setPendingPoints] = useState(0)  // 0 or 1
  const [drawCount, setDrawCount] = useState(0)  // points in current draw
  const [ctxMenu, setCtxMenu] = useState(null)   // null | { x, y, lat, lon }
  const mapColors = useMapPrefs((s) => s.mapColors)
  const setMapColors = useMapPrefs((s) => s.setMapColors)
  const [centerOnOpen, setCenterOnOpen] = useState(false)
  // Location search
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState([])  // [{name, lat, lon, display_name}]
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchError, setSearchError] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const searchInputRef = useRef(null)

  // ── ATAK-style drawing tools + imported layers ──────────────────────────
  const drawCtrlRef = useRef(null)
  const fileInputRef = useRef(null)
  const [toolsOpen, setToolsOpen] = useState(false)
  const [natoOpen, setNatoOpen] = useState(false)
  const [toolsActive, setToolsActive] = useState(null)
  const [toolsStrokeColor, setToolsStrokeColor] = useState('#a855f7')
  const [drawnFeatures, setDrawnFeatures] = useState([])  // mirror of controller features
  const [pendingImageBounds, setPendingImageBounds] = useState(null)  // image awaiting bounds
  const [boundsForm, setBoundsForm] = useState({ north: '', south: '', east: '', west: '' })
  const [dragOver, setDragOver] = useState(false)
  const [importNotice, setImportNotice] = useState(null)  // { kind: 'ok'|'error', text }
  // Standalone terrain-profile line drawing
  const terrainLineRef = useRef({ points: [], markers: [], line: null })
  const [terrainLineCount, setTerrainLineCount] = useState(0)
  const terrainLineModeRef = useRef(false)
  useEffect(() => { terrainLineModeRef.current = terrainLineMode }, [terrainLineMode])

  // Keep refs current so map click closures can read them
  useEffect(() => { activeTabRef.current = activeTab }, [activeTab])
  useEffect(() => { drawModeRef.current = drawMode }, [drawMode])
  useEffect(() => { onMapClickRef.current = onMapClick }, [onMapClick])
  useEffect(() => { selectFeatureRef.current = onSelectFeature }, [onSelectFeature])
  useEffect(() => { mapColorsRef.current = mapColors }, [mapColors])
  useEffect(() => { lobPickingModeRef.current = lobPickingMode }, [lobPickingMode])
  useEffect(() => { onAddLoBObserverRef.current = onAddLoBObserver }, [onAddLoBObserver])
  useEffect(() => { lobAzimuthPickingModeRef.current = lobAzimuthPickingMode }, [lobAzimuthPickingMode])
  useEffect(() => { onAddLoBAzimuthTargetRef.current = onAddLoBAzimuthTarget }, [onAddLoBAzimuthTarget])

  // Attach feature-level click + contextmenu handlers to any layer (marker,
  // circleMarker, polyline, polygon). Click snaps to the layer's center when
  // ruler or LoB-picking mode is active. Right-click opens the standard map
  // context menu pinned to the feature's location, so the user can pick "Set
  // ruler start/endpoint" or "Add LoB Observer" using that feature's coords.
  const attachRulerClick = (layer) => {
    if (!layer || layer.__rulerHooked) return
    layer.__rulerHooked = true
    const layerLatLng = (e) => {
      let ll = null
      const tgt = e.target
      if (tgt && typeof tgt.getLatLng === 'function') {
        try { ll = tgt.getLatLng() } catch {}
      }
      if (!ll && tgt && typeof tgt.getBounds === 'function') {
        try { ll = tgt.getBounds().getCenter() } catch {}
      }
      if (!ll) ll = e.latlng
      return ll
    }
    layer.on('click', (e) => {
      // bindPopup attaches its open-handler before this one, so a popup briefly
      // opens; close it immediately so the click feels consumed.
      const consume = () => {
        L.DomEvent.stopPropagation(e)
        if (e.originalEvent) L.DomEvent.preventDefault(e.originalEvent)
        try { e.target?.closePopup?.() } catch {}
      }
      if (rulerModeRef.current) {
        const ll = layerLatLng(e)
        if (!ll) return
        addRulerPointRef.current?.(ll.lat, ll.lng)
        consume()
        return
      }
      if (lobPickingModeRef.current) {
        const ll = layerLatLng(e)
        if (!ll) return
        onAddLoBObserverRef.current?.(ll.lat, ll.lng)
        consume()
        return
      }
      if (lobAzimuthPickingModeRef.current) {
        const ll = layerLatLng(e)
        if (!ll) return
        onAddLoBAzimuthTargetRef.current?.(ll.lat, ll.lng)
        consume()
      }
    })
    layer.on('contextmenu', (e) => {
      // Yield right-click to draw / terrain-line modes that use it to finish
      if (drawCtrlRef.current?.getActiveTool()) return
      if (terrainLineModeRef.current) return
      const ll = layerLatLng(e)
      const map = leafletRef.current
      if (!ll || !map) return
      const pt = map.latLngToContainerPoint(ll)
      setCtxMenu({ x: pt.x, y: pt.y, lat: ll.lat, lon: ll.lng })
      L.DomEvent.stopPropagation(e)
      if (e.originalEvent) L.DomEvent.preventDefault(e.originalEvent)
    })
  }

  // ── Fly to target when prop changes ───────────────────────────────────────
  useEffect(() => {
    if (!flyToTarget || !leafletRef.current) return
    leafletRef.current.setView([flyToTarget.lat, flyToTarget.lon], flyToTarget.zoom ?? 12)
  }, [flyToTarget])

  // ── Initialize map ─────────────────────────────────────────────────────────
  useEffect(() => {
    if (leafletRef.current) return

    const map = L.map(mapRef.current, {
      center: [tx.lat, tx.lon],
      zoom: 10,
      zoomControl: false,
      // Canvas renderer for all vector overlays — a coverage result is thousands of
      // circleMarkers/rectangles, and the default SVG renderer re-rasterises every
      // path on each pan/zoom (the post-simulation lag). Canvas draws them in one
      // pass. padding>0 pre-renders a margin so panning doesn't reveal blank tiles.
      preferCanvas: true,
      renderer: L.canvas({ padding: 0.5 }),
    })
    leafletRef.current = map

    // Invalidate size whenever the container resizes (sidebar/bottom panel toggle, window resize).
    // Guard against stray callbacks after the map is removed (e.g. switching to the 3D globe).
    const ro = new ResizeObserver(() => {
      const m = leafletRef.current
      if (!m || !m._loaded) return
      try { m.invalidateSize() } catch { /* map mid-teardown */ }
    })
    ro.observe(mapRef.current)

    // Declutter the permanent feature labels (imported placemark names, etc.):
    //  1) they only appear once you're reasonably zoomed in — hidden below z LABEL_HIDE_BELOW,
    //     fading in to full opacity at z LABEL_FULL_AT (so a zoomed-out view isn't a wall of text);
    //  2) labels that would overlap each other are hidden, greedily, top-to-bottom — so the
    //     ones that do show stay legible (neither gets buried under another).
    const LABEL_HIDE_BELOW = 9, LABEL_FULL_AT = 13
    const updateLabelVisibility = () => {
      const pane = map.getPanes?.()?.tooltipPane
      if (!pane) return
      const z = map.getZoom()
      if (z < LABEL_HIDE_BELOW) { pane.style.display = 'none'; return }
      pane.style.display = ''
      pane.style.opacity = z >= LABEL_FULL_AT
        ? '1'
        : String(Math.max(0.12, Math.min(1, (z - (LABEL_HIDE_BELOW - 1)) / (LABEL_FULL_AT - (LABEL_HIDE_BELOW - 1)))))
      // overlap declutter — reset everything to visible, then hide labels whose
      // box collides with one we've already decided to keep (stable top→bottom order).
      const labels = pane.querySelectorAll('.mv-feature-label')
      for (const el of labels) el.style.visibility = ''
      if (labels.length < 2) return
      const measured = []
      for (const el of labels) {
        const r = el.getBoundingClientRect()
        if (r.width > 0 && r.height > 0) measured.push({ el, r })
      }
      measured.sort((a, b) => (a.r.top - b.r.top) || (a.r.left - b.r.left))
      const PAD = 2
      const kept = []
      for (const { el, r } of measured) {
        const hit = kept.some(k => !(r.right + PAD < k.left || r.left - PAD > k.right ||
                                     r.bottom + PAD < k.top || r.top - PAD > k.bottom))
        if (hit) el.style.visibility = 'hidden'
        else kept.push(r)
      }
    }
    let _labelTimer = null
    const scheduleLabelUpdate = () => {
      if (_labelTimer) return
      _labelTimer = setTimeout(() => { _labelTimer = null; updateLabelVisibility() }, 120)
    }
    map.on('zoomend moveend', updateLabelVisibility)
    map.on('layeradd layerremove', scheduleLabelUpdate)   // catch imported-layer label changes
    updateLabelVisibility()

    tileRef.current = L.tileLayer(TILE_LAYERS.dark.url, {
      attribution: TILE_LAYERS.dark.attribution,
      maxZoom: 19,
    }).addTo(map)

    L.control.zoom({ position: 'bottomright' }).addTo(map)
    L.control.scale({ position: 'bottomleft', metric: true, imperial: true }).addTo(map)

    map.on('click', (e) => {
      // Always dismiss context menu and panels on any click
      setCtxMenu(null)
      setCenterOnOpen(false)
      // Background-map click clears any previously-selected map feature so a stray Delete keypress
      // doesn't surprise-remove a TX/MANET-node/waypoint the user no longer thought was selected.
      // (Leaflet marker clicks don't propagate to the map, so this only fires on empty space.)
      if (!drawModeRef.current && !terrainLineModeRef.current) selectFeatureRef.current?.(null)

      // If a drawing tool is active, let the draw controller own the click
      if (drawCtrlRef.current?.getActiveTool()) return

      const { lat, lng } = e.latlng

      // Terrain-line draw mode (standalone profile)
      if (terrainLineModeRef.current) {
        const tl = terrainLineRef.current
        const dot = L.circleMarker([lat, lng], {
          radius: 5, fillColor: '#f59e0b', fillOpacity: 1,
          color: '#fff', weight: 1.5, interactive: false,
        }).addTo(map)
        tl.points.push([lat, lng])
        tl.markers.push(dot)
        if (tl.line) tl.line.remove()
        if (tl.points.length >= 2) {
          tl.line = L.polyline(tl.points, { color: '#f59e0b', weight: 2.5, dashArray: '6 4' }).addTo(map)
        }
        setTerrainLineCount(tl.points.length)
        return
      }

      if (rulerModeRef.current) {
        addRulerPointRef.current?.(lat, lng)
        return
      }

      // Draw mode handling
      const dm = drawModeRef.current
      if (dm) {
        const draw = drawRef.current
        draw.points.push([lat, lng])

        // Visual dot for each click
        const drawColor = mapColorsRef.current.draw
        const dot = L.circleMarker([lat, lng], {
          radius: 5, fillColor: drawColor, fillOpacity: 1,
          color: '#fff', weight: 1.5, interactive: false,
        }).addTo(map)
        draw.markers.push(dot)

        if (dm === 'bounds' && draw.points.length === 2) {
          // Rectangle from 2 corners
          const [p1, p2] = draw.points
          if (draw.rect) draw.rect.remove()
          const bounds = [[Math.min(p1[0], p2[0]), Math.min(p1[1], p2[1])],
                          [Math.max(p1[0], p2[0]), Math.max(p1[1], p2[1])]]
          draw.rect = L.rectangle(bounds, {
            color: drawColor, weight: 2, fill: true, fillOpacity: 0.1, interactive: false,
          }).addTo(map)
          onDrawComplete?.('bounds', { north: bounds[1][0], south: bounds[0][0], east: bounds[1][1], west: bounds[0][1] })
          // Reset
          draw.points = []
          draw.markers.forEach(m => m.remove())
          draw.markers = []
        } else if (dm === 'polygon') {
          // Update polygon preview
          draw.lines.forEach(l => l.remove())
          draw.lines = []
          if (draw.points.length >= 2) {
            draw.lines.push(L.polyline([...draw.points, draw.points[0]], {
              color: drawColor, weight: 2, dashArray: '5 3', interactive: false,
            }).addTo(map))
          }
          // Double-click or >=3 points + click on first point to close
          if (draw.points.length >= 3) {
            const last = draw.points[draw.points.length - 1]
            const first = draw.points[0]
            if (Math.abs(last[0] - first[0]) < 0.002 && Math.abs(last[1] - first[1]) < 0.002) {
              draw.points.pop()
              onDrawComplete?.('polygon', draw.points.map(p => ({ lat: p[0], lon: p[1] })))
              draw.points = []
              draw.markers.forEach(m => m.remove())
              draw.markers = []
              draw.lines.forEach(l => l.remove())
              draw.lines = []
            }
          }
        } else if (dm === 'route' || dm === 'multipoint') {
          // Each click adds a waypoint; right-click finishes
          draw.lines.forEach(l => l.remove())
          draw.lines = []
          if (draw.points.length >= 2) {
            draw.lines.push(L.polyline(draw.points, {
              color: '#00b4d8', weight: 2, dashArray: '4 3', interactive: false,
            }).addTo(map))
          }
          // Report all current points so far
          onDrawComplete?.(dm, draw.points.map(p => ({ lat: p[0], lon: p[1] })))
        } else if (dm === 'manet') {
          // Each click places a MANET node
          onDrawComplete?.('manet', { lat, lon: lng })
        }

        setDrawCount(draw.points.length)
        return
      }

      if (activeTabRef.current === 'p2p') {
        onMapClickRef.current?.(lat, lng, true)
      } else {
        onMapClickRef.current?.(lat, lng, false)
      }
    })

    map.on('contextmenu', (e) => {
      // Yield right-click to the draw controller when it's active (it uses it to finish)
      if (drawCtrlRef.current?.getActiveTool()) return

      // Finish terrain-line on right-click
      if (terrainLineModeRef.current) {
        const tl = terrainLineRef.current
        if (tl.points.length >= 2) {
          onTerrainLineComplete?.(tl.points.slice())
        }
        tl.points = []
        tl.markers.forEach(m => m.remove())
        tl.markers = []
        if (tl.line) { tl.line.remove(); tl.line = null }
        setTerrainLineCount(0)
        return
      }
      // Right-click finishes route/multipoint drawing first
      const dm = drawModeRef.current
      const draw = drawRef.current
      if ((dm === 'route' || dm === 'multipoint') && draw.points.length >= 2) {
        onDrawComplete?.(dm + '_finish', draw.points.map(p => ({ lat: p[0], lon: p[1] })))
        draw.points = []
        draw.markers.forEach(m => m.remove())
        draw.markers = []
        draw.lines.forEach(l => l.remove())
        draw.lines = []
        setDrawCount(0)
        return
      }

      // Show context menu for emitter / LoB placement
      const { lat, lng } = e.latlng
      const { x, y } = e.containerPoint
      setCtxMenu({ x, y, lat, lon: lng })
    })

    // Close context menu on map move / zoom
    map.on('movestart zoomstart', () => setCtxMenu(null))

    return () => {
      try { ro.disconnect() } catch { /* noop */ }
      if (_labelTimer) { clearTimeout(_labelTimer); _labelTimer = null }
      try { map.remove() } catch { /* noop */ }
      leafletRef.current = null
    }
  }, [])  // only once

  // Stable ruler-input helper: places a measurement point at (lat, lng).
  // Used by both the map click handler (empty space) and per-feature click
  // handlers (snap to feature center). Defined once; reads everything via refs.
  useEffect(() => {
    addRulerPointRef.current = (lat, lng) => {
      const map = leafletRef.current
      if (!map) return
      const pending = pendingRulerRef.current
      const color = mapColorsRef.current.ruler
      const dot = L.circleMarker([lat, lng], {
        radius: 5, fillColor: color, fillOpacity: 1,
        color: '#fff', weight: 1.5, interactive: false,
      }).addTo(map)
      pending.points.push([lat, lng])
      pending.markers.push(dot)
      setPendingPoints(pending.points.length)

      if (pending.points.length === 2) {
        const [p1, p2] = pending.points
        const d = haversine(p1[0], p1[1], p2[0], p2[1])
        const b = bearing(p1[0], p1[1], p2[0], p2[1])
        const line = L.polyline(pending.points, {
          color, weight: 2, dashArray: '6 4', interactive: false,
        }).addTo(map)
        const midLat = (p1[0] + p2[0]) / 2
        const midLon = (p1[1] + p2[1]) / 2
        const dText = d >= 1000
          ? `${(d / 1000).toFixed(2)} km`
          : `${Math.round(d)} m`
        const labelMarker = L.marker([midLat, midLon], {
          icon: L.divIcon({
            className: '',
            html: `<div style="background:rgba(0,0,0,0.78);color:${color};
                     padding:2px 7px;border-radius:4px;font-size:10px;font-weight:700;
                     white-space:nowrap;border:1px solid ${color}55;pointer-events:none;">
                     ${dText} · ${b.toFixed(1)}°
                   </div>`,
            iconSize: [null, null], iconAnchor: [0, 20],
          }),
          interactive: false,
        }).addTo(map)
        const id = Date.now() + Math.random()
        completedRulersRef.current.push({ id, line, markers: [...pending.markers], labelMarker })
        setRulerResults(prev => [...prev, { id, dist: d, hdg: b }])
        pending.points = []
        pending.markers = []
        setPendingPoints(0)
      }
    }
  }, [])

  // Toggle ruler mode side-effects: cursor + disable drag on draggable markers
  // (so a click on TX/RX/extra-TX snaps to the marker instead of starting a drag).
  useEffect(() => {
    rulerModeRef.current = rulerMode
    const map = leafletRef.current
    if (map) map.getContainer().style.cursor = rulerMode ? 'crosshair' : ''

    const draggables = [
      txMarkerRef.current,
      rxMarkerRef.current,
      ...Object.values(extraTxMarkersRef.current),
    ].filter(Boolean)
    draggables.forEach(m => {
      if (!m.dragging) return
      if (rulerMode) m.dragging.disable()
      else m.dragging.enable()
    })

    // Clear pending ruler dots when ruler mode is toggled off
    if (!rulerMode) {
      pendingRulerRef.current.markers.forEach(m => m.remove())
      pendingRulerRef.current = { points: [], markers: [] }
      setPendingPoints(0)
    }
  }, [rulerMode])

  // Clear terrain-line markers when mode toggled off
  useEffect(() => {
    if (terrainLineMode) return
    const tl = terrainLineRef.current
    tl.points = []
    tl.markers.forEach(m => m.remove())
    tl.markers = []
    if (tl.line) { tl.line.remove(); tl.line = null }
    setTerrainLineCount(0)
  }, [terrainLineMode])

  // Keep a stable ref to the layer hook so effects don't churn each render
  const ulRef = useRef(ul)
  useEffect(() => { ulRef.current = ul }, [ul])

  // operator GPS — "you are here" marker (hover lists the SDRs pinned to this fix)
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return
    if (gpsMarkerRef.current) { try { map.removeLayer(gpsMarkerRef.current) } catch { /* noop */ } gpsMarkerRef.current = null }
    if (gpsFix && typeof gpsFix.lat === 'number' && typeof gpsFix.lon === 'number') {
      const hdg = typeof gpsFix.heading_deg === 'number' ? `transform:rotate(${gpsFix.heading_deg}deg);` : ''
      const esc = (s) => String(s ?? '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]))
      const head = `<div><strong>You — GPS</strong> <span style="opacity:0.7">(${esc(gpsFix.source || 'manual')}${gpsFix.heading_deg != null ? ` · hdg ${Math.round(gpsFix.heading_deg)}°` : ''}${typeof gpsFix.accuracy_m === 'number' ? ` · ±${Math.round(gpsFix.accuracy_m)} m` : ''})</span></div>`
      const trk = (gpsTrackers || [])
      const list = trk.length
        ? `<div style="margin-top:3px;opacity:0.85">SDRs pinned to this GPS:</div>` +
          trk.map(t => `<div style="white-space:nowrap">• ${esc(t.name || t.id)} <span style="opacity:0.55">${esc(t.type)}${t.status && t.status !== 'streaming' ? ` · ${esc(t.status)}` : ''}</span></div>`).join('')
        : `<div style="margin-top:3px;opacity:0.6">No SDRs are using this GPS fix.</div>`
      const m = L.marker([gpsFix.lat, gpsFix.lon], {
        interactive: true, zIndexOffset: 1100,
        icon: L.divIcon({ className: '', iconSize: [22, 22], iconAnchor: [11, 11], html:
          `<div style="width:22px;height:22px;${hdg}"><svg viewBox="0 0 22 22" width="22" height="22">
             <circle cx="11" cy="11" r="9" fill="#22d3ee" fill-opacity="0.35" stroke="#22d3ee"/>
             <path d="M11 2 L15 13 L11 10 L7 13 Z" fill="#22d3ee" stroke="#fff" stroke-width="0.8"/>
           </svg></div>` }),
      }).addTo(map).bindTooltip(`${head}${list}`, { direction: 'top', opacity: 0.95 })
      gpsMarkerRef.current = m
    }
  }, [gpsFix, gpsTrackers])

  // ── Draw controller: initialize once map is ready ────────────────────────
  useEffect(() => {
    if (!leafletRef.current || drawCtrlRef.current) return
    const ctrl = createDrawController(leafletRef.current, {
      style: { color: toolsStrokeColor },
      onFeatureClick: (id) => { try { ulRef.current?.selectFeature?.({ kind: 'drawn', id }) } catch {} },
    })
    drawCtrlRef.current = ctrl
    const off = ctrl.onChange((list, active) => {
      setDrawnFeatures(list)
      setToolsActive(active)
    })
    // Bind to upstream layer manager so the unified panel sees draw features
    try { ulRef.current?.bindMap?.(leafletRef.current, ctrl) } catch {}
    return () => {
      off?.()
      try { ulRef.current?.unbindMap?.() } catch {}
      ctrl.destroy()
      drawCtrlRef.current = null
    }
  }, [])

  // Re-bind whenever the layer hook identity changes (without re-creating the controller)
  useEffect(() => {
    if (ul && drawCtrlRef.current && leafletRef.current) {
      try { ul.bindMap(leafletRef.current, drawCtrlRef.current) } catch {}
    }
  }, [ul])

  // Mirror features drawn on the 3D globe (ul.globeDrawnGeoJSON) onto the 2D map.
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return
    if (globeDrawLayerRef.current) { try { globeDrawLayerRef.current.remove() } catch {} globeDrawLayerRef.current = null }
    const fc = ul?.globeDrawnGeoJSON
    if (!fc?.features?.length) return
    const dflt = mapColorsRef.current?.draw || '#a855f7'
    const MILC = { milFriend: '#3b82f6', milHostile: '#ef4444', milNeutral: '#22c55e', milUnknown: '#facc15' }
    const MILL = { milFriend: 'F', milHostile: 'H', milNeutral: 'N', milUnknown: '?' }
    const layer = L.geoJSON(fc, {
      style: (f) => ({ color: f?.properties?.stroke || dflt, weight: 2, fillOpacity: 0.15 }),
      pointToLayer: (f, ll) => {
        const k = f?.properties?.mv_kind
        if (k === 'nato') {
          const arm = f.properties.natoArm
          const sidc = typeof arm === 'string' ? arm : (arm?.sidc || arm?.code || arm?.sic || null)
          try { if (sidc) return L.marker(ll, { icon: makeSidcIcon(String(sidc)) }) } catch { /* bad SIDC → fall through */ }
        }
        if (k && k.startsWith('mil')) {
          const c = MILC[k] || dflt
          return L.marker(ll, { icon: L.divIcon({ className: '', iconSize: [18, 18], iconAnchor: [9, 9],
            html: `<div style="width:18px;height:18px;border:2px solid #fff;background:${c};display:flex;align-items:center;justify-content:center;font:bold 10px sans-serif;color:#000;box-shadow:0 1px 3px rgba(0,0,0,0.6);${k === 'milFriend' ? '' : k === 'milHostile' ? 'transform:rotate(45deg);' : 'border-radius:50%;'}">${MILL[k] || ''}</div>` }) })
        }
        return L.circleMarker(ll, { radius: 5, color: f?.properties?.stroke || dflt, fillColor: f?.properties?.stroke || dflt, fillOpacity: 0.9 })
      },
    }).addTo(map)
    globeDrawLayerRef.current = layer
    return () => { try { layer.remove() } catch {} if (globeDrawLayerRef.current === layer) globeDrawLayerRef.current = null }
  }, [ul?.globeDrawnGeoJSON, mapColors])

  useEffect(() => {
    drawCtrlRef.current?.setStyle({ color: toolsStrokeColor })
  }, [toolsStrokeColor])

  // ── Track-history rendering ──────────────────────────────────────────────
  // Subscribes to useTrackHistory and renders each visible track as a polyline.
  // During playback, also renders a "now" marker at the interpolated position.
  const tracks = useTrackHistory((s) => s.tracks)
  const trackPlayback = useTrackHistory((s) => s.playback)
  const trackLayersRef = useRef(new Map())   // trackId → { line, dot, nowMarker }
  useEffect(() => {
    const map = leafletRef.current; if (!map) return
    const layers = trackLayersRef.current
    // Remove layers for deleted tracks
    for (const [id, rec] of layers) {
      if (!tracks[id]) {
        try { rec.line?.remove(); rec.dot?.remove(); rec.nowMarker?.remove() } catch {}
        layers.delete(id)
      }
    }
    // Add / update layers for current tracks
    for (const tr of Object.values(tracks)) {
      let rec = layers.get(tr.id)
      const pts = tr.points.map(p => [p.lat, p.lon])
      if (!rec) {
        const line = L.polyline(pts, { color: tr.color, weight: 2.5, opacity: 0.9 })
        rec = { line, dot: null, nowMarker: null }
        layers.set(tr.id, rec)
      } else {
        rec.line.setLatLngs(pts)
        rec.line.setStyle({ color: tr.color })
      }
      if (tr.visible && pts.length > 0) {
        if (!map.hasLayer(rec.line)) rec.line.addTo(map)
      } else {
        if (map.hasLayer(rec.line)) rec.line.remove()
      }
    }
    // Playback now-marker (a brighter dot at the interpolated position)
    if (trackPlayback) {
      const tr = tracks[trackPlayback.trackId]
      if (tr) {
        const p = trackPositionAt(tr, trackPlayback.t)
        const rec = layers.get(tr.id)
        if (p && rec) {
          if (!rec.nowMarker) {
            rec.nowMarker = L.circleMarker([p.lat, p.lon], {
              radius: 7, color: '#fff', weight: 2,
              fillColor: tr.color, fillOpacity: 1,
            }).addTo(map)
          } else {
            rec.nowMarker.setLatLng([p.lat, p.lon])
            if (!map.hasLayer(rec.nowMarker)) rec.nowMarker.addTo(map)
          }
        }
      }
    } else {
      // No playback → remove any now-markers
      for (const rec of layers.values()) {
        if (rec.nowMarker) { try { rec.nowMarker.remove() } catch {}; rec.nowMarker = null }
      }
    }
  }, [tracks, trackPlayback])

  // Esc cancels active draw tool
  useEffect(() => {
    if (!toolsActive) return
    const handler = (e) => {
      if (e.key === 'Escape') {
        const ctrl = drawCtrlRef.current
        // Esc unwinds one layer at a time so the operator can recover from
        // any intermediate state without losing the tool:
        //   1) finish edit-mode first (if a shape is being resized)
        //   2) cancel an in-progress shape (preserves the active tool)
        //   3) deactivate the tool itself
        if (ctrl?.getEditingId && ctrl.getEditingId()) ctrl.exitEditMode()
        else if (ctrl?.hasScratch && ctrl.hasScratch()) ctrl.cancelCurrent()
        else ctrl?.deactivate()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [toolsActive])

  const activateTool = (id) => {
    const ctrl = drawCtrlRef.current
    if (!ctrl) return
    if (ctrl.getActiveTool() === id) ctrl.deactivate()
    else {
      // Coverage-bounds draw and the annotation tools both own map clicks —
      // arming an annotation tool clears any pending bounds draw.
      if (drawMode === 'bounds') onToggleBoundsDraw?.()
      ctrl.activate(id)
    }
  }

  // Coverage-bounds draw lives in this same ✎ dropdown but runs through the
  // App-level drawMode (not the annotation draw controller), so free the
  // annotation tool first to avoid both reacting to the same click.
  const toggleBoundsDraw = () => {
    drawCtrlRef.current?.deactivate()
    onToggleBoundsDraw?.()
  }

  // ── File import (drag-drop + dialog) ─────────────────────────────────────
  // When `fit: false`, the layer is added without auto-zooming. Returns the
  // layer id so the caller can compute a union and fit at the end of a batch.
  const addImportedItem = useCallback((info, opts = {}) => {
    if (!ul) return null
    if (info.kind === 'geojson') {
      return ul.addGeoJSONLayer(info.geojson, {
        name: info.name, sourceFormat: info.sourceFormat, fit: opts.fit,
      })
    } else if (info.kind === 'image' && info.bounds) {
      return ul.addImageLayer(info, { name: info.name, fit: opts.fit })
    } else if (info.kind === 'terrain' && info.grid) {
      return ul.addTerrainGrid(info.grid, { name: info.name, fit: opts.fit })
    }
    return null
  }, [ul])

  const handleImportFiles = useCallback(async (files) => {
    if (!files || !files.length) return
    setImportNotice({ kind: 'ok', text: `Loading ${files.length} file${files.length > 1 ? 's' : ''}…` })
    let items
    try {
      items = await loadFiles(files)
    } catch (e) {
      setImportNotice({ kind: 'error', text: 'Load failed: ' + (e?.message || e) })
      return
    }
    let added = 0, errors = 0, needsBounds = 0, errorMsg = ''
    const addedLayerIds = []
    // Single-item drops fit naturally (each adder fits to its own bounds).
    // For multi-item drops we suppress per-item fit and fit to the union below.
    const realItems = items.filter(it => it.kind !== 'error' && !(it.kind === 'image' && it.needsBounds))
    const suppressFit = realItems.length > 1
    for (const item of items) {
      if (item.kind === 'error') {
        errors++
        if (!errorMsg) errorMsg = item.message
        continue
      }
      if (item.kind === 'image' && item.needsBounds) {
        if (!pendingImageBounds && needsBounds === 0) {
          setPendingImageBounds(item)
          setBoundsForm({ north: '', south: '', east: '', west: '' })
        }
        needsBounds++
        continue
      }
      const id = addImportedItem(item, { fit: !suppressFit })
      if (id) addedLayerIds.push(id)
      added++
    }
    // Fit to the union of every newly added layer
    if (suppressFit && leafletRef.current && addedLayerIds.length && ul?.getLayerBounds) {
      let union = null
      addedLayerIds.forEach(id => {
        const b = ul.getLayerBounds(id)
        if (b && b.isValid()) {
          union = union ? union.extend(b) : L.latLngBounds(b.getSouthWest(), b.getNorthEast())
        }
      })
      if (union && union.isValid()) {
        leafletRef.current.fitBounds(union, { padding: [40, 40] })
      }
    }
    const parts = []
    if (added) parts.push(`${added} layer${added > 1 ? 's' : ''} added`)
    if (needsBounds) parts.push(`${needsBounds} image${needsBounds > 1 ? 's' : ''} need bounds`)
    if (errors) parts.push(`${errors} error${errors > 1 ? 's' : ''}${errorMsg ? ': ' + errorMsg : ''}`)
    setImportNotice({
      kind: errors && !added ? 'error' : 'ok',
      text: parts.join(' · ') || 'Nothing imported',
    })
    setTimeout(() => setImportNotice(null), 5000)
  }, [addImportedItem, pendingImageBounds])

  const confirmImageBounds = () => {
    const item = pendingImageBounds
    if (!item) return
    const n = parseFloat(boundsForm.north), s = parseFloat(boundsForm.south)
    const e = parseFloat(boundsForm.east),  w = parseFloat(boundsForm.west)
    if ([n, s, e, w].some(v => !Number.isFinite(v))) {
      setImportNotice({ kind: 'error', text: 'Invalid bounds' })
      return
    }
    addImportedItem({ ...item, bounds: [[s, w], [n, e]], needsBounds: false })
    setPendingImageBounds(null)
  }

  // Drag & drop on the map container
  useEffect(() => {
    const el = mapRef.current
    if (!el) return
    let depth = 0
    const onDragEnter = (e) => {
      e.preventDefault()
      if (e.dataTransfer?.types?.includes('Files')) { depth++; setDragOver(true) }
    }
    const onDragOver = (e) => {
      if (e.dataTransfer?.types?.includes('Files')) {
        e.preventDefault()
        e.dataTransfer.dropEffect = 'copy'
      }
    }
    const onDragLeave = (e) => {
      e.preventDefault()
      depth = Math.max(0, depth - 1)
      if (depth === 0) setDragOver(false)
    }
    const onDrop = (e) => {
      e.preventDefault()
      depth = 0
      setDragOver(false)
      const files = e.dataTransfer?.files
      if (files && files.length) handleImportFiles(files)
    }
    el.addEventListener('dragenter', onDragEnter)
    el.addEventListener('dragover', onDragOver)
    el.addEventListener('dragleave', onDragLeave)
    el.addEventListener('drop', onDrop)
    return () => {
      el.removeEventListener('dragenter', onDragEnter)
      el.removeEventListener('dragover', onDragOver)
      el.removeEventListener('dragleave', onDragLeave)
      el.removeEventListener('drop', onDrop)
    }
  }, [handleImportFiles])

  // Expose import handler (and the map view getter/setter, for save/load) so the
  // parent (App.jsx) can trigger them.
  useEffect(() => {
    if (typeof onImportApi === 'function') {
      onImportApi({
        openFileDialog: () => fileInputRef.current?.click(),
        importFiles: handleImportFiles,
        getView: () => {
          const m = leafletRef.current
          if (!m) return null
          const c = m.getCenter()
          return { lat: c.lat, lon: c.lng, zoom: m.getZoom() }
        },
        setView: (v) => {
          const m = leafletRef.current
          if (!m || !v || typeof v.lat !== 'number' || typeof v.lon !== 'number') return
          m.setView([v.lat, v.lon], typeof v.zoom === 'number' ? v.zoom : m.getZoom())
        },
      })
    }
  }, [handleImportFiles])

  // Export drawn features as GeoJSON
  const exportDrawnGeoJSON = () => {
    const ctrl = drawCtrlRef.current
    if (!ctrl) return
    const fc = ctrl.exportGeoJSON()
    if (!fc.features.length) {
      setImportNotice({ kind: 'error', text: 'No drawn features to export' })
      setTimeout(() => setImportNotice(null), 2500)
      return
    }
    const blob = new Blob([JSON.stringify(fc, null, 2)], { type: 'application/geo+json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `ares-drawings-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.geojson`
    document.body.appendChild(a); a.click(); a.remove()
    URL.revokeObjectURL(url)
  }

  // Apply map brightness + night-vision filter to the tile pane. Night vision
  // composes a red palette via sepia(1) hue-rotate(310deg) saturate(5) — same
  // trick TAK uses for dark-adaptation preservation. Brightness still applies.
  const nightVision = useMapPrefs((s) => s.nightVision)
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return
    const pane = map.getPane('tilePane')
    if (!pane) return
    const parts = []
    if (mapBrightness !== 100) parts.push(`brightness(${mapBrightness}%)`)
    if (nightVision) parts.push('sepia(1) hue-rotate(310deg) saturate(5)')
    pane.style.filter = parts.join(' ')
  }, [mapBrightness, nightVision])

  // Clear all rulers helper (stable ref so it can be used inside JSX without dep issues)
  const clearRulers = () => {
    completedRulersRef.current.forEach(r => {
      r.line?.remove()
      r.markers?.forEach(m => m.remove())
      r.labelMarker?.remove()
    })
    completedRulersRef.current = []
    setRulerResults([])
    pendingRulerRef.current.markers.forEach(m => m.remove())
    pendingRulerRef.current = { points: [], markers: [] }
    setPendingPoints(0)
    setRulerMode(false)
  }

  const handleSearch = async () => {
    const q = searchQuery.trim()
    if (!q) return
    setSearchError('')
    setSearchResults([])
    // Try every supported coordinate format first (DD, DMS, DDM, MGRS, UTM,
    // Maidenhead). Falls through to geocoding if it doesn't look like a coord.
    const coords = parseAnyCoord(q)
    if (coords) {
      setSearchResults([{
        name: `${coords.lat.toFixed(5)}, ${coords.lon.toFixed(5)}`,
        display_name: 'Coordinates', lat: coords.lat, lon: coords.lon,
      }])
      if (leafletRef.current) leafletRef.current.setView([coords.lat, coords.lon], 12)
      return
    }
    setSearchLoading(true)
    try {
      const results = await geocodeNominatim(q)
      if (results.length === 0) { setSearchError('No results found'); return }
      setSearchResults(results)
      if (leafletRef.current) {
        const r = results[0]
        if (r.bounds) leafletRef.current.fitBounds(r.bounds, { padding: [30, 30] })
        else leafletRef.current.setView([r.lat, r.lon], 12)
      }
    } catch {
      setSearchError('Geocoding failed — check connection')
    } finally {
      setSearchLoading(false)
    }
  }

  // Clear draw state when drawMode changes
  useEffect(() => {
    const draw = drawRef.current
    draw.points = []
    draw.markers.forEach(m => m.remove())
    draw.markers = []
    draw.lines.forEach(l => l.remove())
    draw.lines = []
    if (draw.rect) { draw.rect.remove(); draw.rect = null }
    setDrawCount(0)
  }, [drawMode])

  // ── Update tile layer ──────────────────────────────────────────────────────
  useEffect(() => {
    const map = leafletRef.current
    if (!map || !tileRef.current) return
    tileRef.current.remove()
    tileRef.current = L.tileLayer(TILE_LAYERS[tileStyle].url, {
      attribution: TILE_LAYERS[tileStyle].attribution,
      maxZoom: 19,
    }).addTo(map)
    tileRef.current.bringToBack()
  }, [tileStyle])

  // ── Primary TX Marker ──────────────────────────────────────────────────────
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return

    if (!txActive) {
      if (txMarkerRef.current) { txMarkerRef.current.remove(); txMarkerRef.current = null }
      return
    }

    const txIcon = makeTxIcon(mapColors.emitter)

    if (txMarkerRef.current) {
      txMarkerRef.current.setLatLng([tx.lat, tx.lon])
    } else {
      txMarkerRef.current = L.marker([tx.lat, tx.lon], {
        icon: txIcon, draggable: true,
        title: `${txLabel} — Primary (drag to move · click to select, Delete to remove)`,
        zIndexOffset: 1000,
      }).addTo(map)

      txMarkerRef.current.on('dragend', (e) => {
        const { lat, lng } = e.target.getLatLng()
        onTxDrag?.(lat, lng)
      })
      txMarkerRef.current.on('click', () => selectFeatureRef.current?.({ kind: 'primary_tx' }))

      txMarkerRef.current.bindPopup(() =>
        `<div style="font-size:12px;min-width:140px">
          <strong style="color:${mapColors.emitter}">${txLabel} (Primary)</strong><br>
          Lat: ${tx.lat.toFixed(5)}<br>
          Lon: ${tx.lon.toFixed(5)}<br>
          Height: ${tx.height_m}m AGL<br>
          Power: ${tx.power_dbm} dBm
        </div>`
      )
      attachRulerClick(txMarkerRef.current)
      if (rulerModeRef.current) txMarkerRef.current.dragging?.disable()
    }
  }, [tx.lat, tx.lon, tx.height_m, tx.power_dbm, txActive])

  // Update TX icon when emitter color changes (without recreating the marker)
  useEffect(() => {
    if (txMarkerRef.current) txMarkerRef.current.setIcon(makeTxIcon(mapColors.emitter))
  }, [mapColors.emitter])

  // ── RX Marker ─────────────────────────────────────────────────────────────
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return

    if (!rxPoint) {
      if (rxMarkerRef.current) { rxMarkerRef.current.remove(); rxMarkerRef.current = null }
      return
    }

    const rxIcon = L.divIcon({
      className: '',
      html: `<div style="
        width:24px;height:24px;border-radius:50%;background:#a855f7;
        border:3px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,0.6);
        display:flex;align-items:center;justify-content:center;cursor:grab;
      ">
        <svg width="10" height="10" fill="none" stroke="black" stroke-width="2.5"
             viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/></svg>
      </div>`,
      iconSize: [24, 24], iconAnchor: [12, 12],
    })

    if (rxMarkerRef.current) {
      rxMarkerRef.current.setLatLng([rxPoint.lat, rxPoint.lon])
    } else {
      rxMarkerRef.current = L.marker([rxPoint.lat, rxPoint.lon], {
        icon: rxIcon, draggable: true, title: 'Receiver (click to select, Delete to remove)', zIndexOffset: 900,
      }).addTo(map)

      rxMarkerRef.current.on('dragend', (e) => {
        const { lat, lng } = e.target.getLatLng()
        onRxDrag?.(lat, lng)
      })
      rxMarkerRef.current.on('click', () => selectFeatureRef.current?.({ kind: 'rx' }))
      attachRulerClick(rxMarkerRef.current)
      if (rulerModeRef.current) rxMarkerRef.current.dragging?.disable()
    }
  }, [rxPoint])

  // ── Primary Coverage heatmap ───────────────────────────────────────────────
  // Raster mode (`metadata.mode === 'raster'`): each cell is rendered as a true rectangle
  // sized to the grid spacing, so adjacent cells touch and the heatmap actually tiles the area
  // it covers — instead of looking like a sparse polka-dot grid. Radial mode keeps the dots.
  useEffect(() => {
    const map = leafletRef.current
    if (!map || !coverageGeoJSON) return

    if (coverageLayerRef.current) { coverageLayerRef.current.remove(); coverageLayerRef.current = null }

    const isRaster = coverageGeoJSON.metadata?.mode === 'raster'
    let dLat = 0, dLon = 0
    if (isRaster) {
      // derive the cell size from the actual point spread (works regardless of grid_size)
      let lo = +Infinity, la = +Infinity, LO = -Infinity, LA = -Infinity
      const N = coverageGeoJSON.metadata?.grid_size || Math.max(2, Math.round(Math.sqrt(coverageGeoJSON.features?.length || 1)))
      for (const f of coverageGeoJSON.features || []) {
        const c = f?.geometry?.coordinates
        if (!c) continue
        if (c[0] < lo) lo = c[0]; if (c[0] > LO) LO = c[0]
        if (c[1] < la) la = c[1]; if (c[1] > LA) LA = c[1]
      }
      dLon = ((LO - lo) / Math.max(1, N - 1)) * 0.55     // ~half-cell each side → cells just barely touch
      dLat = ((LA - la) / Math.max(1, N - 1)) * 0.55
    }

    const layer = L.geoJSON(coverageGeoJSON, {
      pointToLayer: (feature, latlng) => {
        const dbm = feature.properties.signal_dbm
        if (!feature.properties.covered) return null
        const [r, g, b, a] = signalToColor(dbm, -120)
        const fill = `rgba(${r},${g},${b},${a / 255})`
        if (isRaster && dLat > 0 && dLon > 0) {
          return L.rectangle(
            [[latlng.lat - dLat, latlng.lng - dLon], [latlng.lat + dLat, latlng.lng + dLon]],
            { color: fill, weight: 0, fillColor: fill, fillOpacity: 0.55, interactive: false }
          )
        }
        return L.circleMarker(latlng, {
          radius: 4,
          fillColor: fill,
          fillOpacity: 0.8,
          stroke: false, interactive: false,
        })
      },
      filter: (f) => f.properties.covered,
    }).addTo(map)

    coverageLayerRef.current = layer
  }, [coverageGeoJSON])

  // ── OSM building footprint layer ──────────────────────────────────────────
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return
    if (buildingLayerRef.current) { buildingLayerRef.current.remove(); buildingLayerRef.current = null }
    if (!buildingGeoJSON?.features?.length) return

    buildingLayerRef.current = L.geoJSON(buildingGeoJSON, {
      style: (feature) => {
        const h = feature.properties?.height_m ?? 10
        // Taller buildings = more opaque red, shorter = translucent yellow
        const t = Math.min(1, h / 50)
        const r = Math.round(180 + t * 75)
        const g = Math.round(140 - t * 100)
        return {
          color: '#f59e0b',
          weight: 1,
          fillColor: `rgb(${r},${g},30)`,
          fillOpacity: 0.35 + t * 0.25,
          interactive: true,
        }
      },
      onEachFeature: (feature, layer) => {
        const p = feature.properties
        layer.bindTooltip(
          `<strong>${p.height_m.toFixed(0)} m</strong> · ${p.material} · −${p.rf_loss_db} dB`,
          { sticky: true, className: 'leaflet-tooltip-dark' }
        )
        attachRulerClick(layer)
      },
    }).addTo(map)
  }, [buildingGeoJSON])

  // ── Extra TX markers & coverage layers ────────────────────────────────────
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return

    const currentIds = new Set(extraTxList.map(e => String(e.id)))

    // Remove stale markers/layers
    Object.keys(extraTxMarkersRef.current).forEach(id => {
      if (!currentIds.has(id)) {
        extraTxMarkersRef.current[id]?.remove()
        delete extraTxMarkersRef.current[id]
      }
    })
    Object.keys(extraLayersRef.current).forEach(id => {
      if (!currentIds.has(id)) {
        extraLayersRef.current[id]?.remove()
        delete extraLayersRef.current[id]
      }
    })

    extraTxList.forEach(entry => {
      const id = String(entry.id)
      const { tx: etx, color, label, geojson, origin } = entry

      // Marker — visual style switches on entry.origin so the operator can
      // tell algorithm-tab fixes apart from DF-head fixes apart from manual TXs:
      //   df_head    → circle, solid border (default)
      //   algorithm  → diamond, dashed border + Σ glyph + dotted halo
      //   (manual)   → circle without the labelled origin badge
      if (extraTxMarkersRef.current[id]) {
        extraTxMarkersRef.current[id].setLatLng([etx.lat, etx.lon])
      } else {
        let html
        if (origin === 'algorithm' || origin === 'target') {
          // Σ glyph for algorithm fixes; ⌖ glyph for per-identifier target fixes.
          // Rotated square + dashed border so the algorithm-derived family of markers reads
          // distinctly from DF-head fixes (solid circles) and manual TXs (labelled).
          const glyph = (origin === 'target') ? '⌖' : 'Σ'
          const title = (origin === 'target') ? 'Target fix (by identifier)' : 'Algorithm fix'
          html = `<div title="${title}" style="
            width:28px;height:28px;
            display:flex;align-items:center;justify-content:center;
            transform:rotate(45deg); border:2px dashed #fff;
            background:${color};
            box-shadow:0 2px 8px rgba(0,0,0,0.6),0 0 0 3px ${color}55;
            cursor:grab;">
              <span style="transform:rotate(-45deg); font-size:13px; font-weight:700; color:#000;">${glyph}</span>
            </div>`
        } else if (origin === 'df_head') {
          // Circle + small badge so operators see which fixes come from a coherent array.
          html = `<div title="DF-head fix" style="
            width:26px;height:26px;border-radius:50%;
            background:${color};
            border:3px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,0.6),0 0 0 3px ${color}55;
            display:flex;align-items:center;justify-content:center;cursor:grab;
            font-size:9px;font-weight:700;color:#000;">DF</div>`
        } else {
          html = `<div style="
            width:24px;height:24px;border-radius:50%;
            background:${color};
            border:3px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,0.6),0 0 0 3px ${color}55;
            display:flex;align-items:center;justify-content:center;cursor:grab;font-size:9px;font-weight:700;color:#000;
          ">${label.replace('TX ', '')}</div>`
        }
        const icon = L.divIcon({
          className: '',
          html,
          iconSize: [28, 28], iconAnchor: [14, 14],
        })
        const marker = L.marker([etx.lat, etx.lon], {
          icon, draggable: true,
          title: `${label} · source: ${origin || 'manual'} (drag to move · click to select, Delete to remove)`,
          zIndexOffset: 990,
        }).addTo(map)

        marker.on('dragend', (e) => {
          const { lat, lng } = e.target.getLatLng()
          onExtraTxDrag?.(entry.id, lat, lng)
        })
        marker.on('click', () => selectFeatureRef.current?.({ kind: 'extra_tx', id: entry.id }))

        const originLine = origin === 'algorithm'
          ? `<br><span style="color:#22d3ee">⊕ Algorithm fix${entry.algorithm_method_id ? ' · ' + entry.algorithm_method_id : ''}${entry.algorithm_cep_m ? ' · CEP ' + Math.round(entry.algorithm_cep_m) + ' m' : ''}</span>`
          : origin === 'df_head' ? `<br><span style="color:#3fb950">⊞ DF-head fix${entry.trackingFixKey ? ' (live-tracked)' : ''}</span>` : ''
        marker.bindPopup(`<div style="font-size:12px;min-width:140px">
          <strong style="color:${color}">${label}</strong>${originLine}<br>
          Lat: ${etx.lat.toFixed(5)}<br>Lon: ${etx.lon.toFixed(5)}<br>
          Power: ${etx.power_dbm} dBm
        </div>`)

        attachRulerClick(marker)
        if (rulerModeRef.current) marker.dragging?.disable()
        extraTxMarkersRef.current[id] = marker
      }

      // Coverage layer
      if (geojson) {
        if (extraLayersRef.current[id]) {
          extraLayersRef.current[id].remove()
        }
        // Parse color to rgba for coverage dots
        const hex = color.replace('#', '')
        const rr = parseInt(hex.slice(0, 2), 16)
        const gg = parseInt(hex.slice(2, 4), 16)
        const bb = parseInt(hex.slice(4, 6), 16)

        extraLayersRef.current[id] = L.geoJSON(geojson, {
          pointToLayer: (feature, latlng) => {
            if (!feature.properties.covered) return null
            return L.circleMarker(latlng, {
              radius: 4,
              fillColor: `rgba(${rr},${gg},${bb},0.6)`,
              fillOpacity: 0.7,
              stroke: false, interactive: false,
            })
          },
          filter: (f) => f.properties.covered,
        }).addTo(map)
      }
    })
  }, [extraTxList])

  // ── Persistent input markers for multipoint TXs / MANET nodes / route waypoints ───
  // These arrays are placed via draw modes (multipoint / manet / route) and the temporary
  // draw.markers are cleaned up on draw-finish — so without this effect, you couldn't see or
  // interact with the inputs between drawing and running the analysis. Each marker is wired
  // to onSelectFeature so the Delete key (and the trash-can fallback) can remove it.

  // Multipoint TXs — small amber dots, indexed by array position.
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return
    multipointMarkersRef.current.forEach(m => m.remove())
    multipointMarkersRef.current = multipointTxs.map((p, i) => {
      const m = L.circleMarker([p.lat, p.lon], {
        radius: 6, fillColor: '#f59e0b', fillOpacity: 0.95, color: '#fff', weight: 2,
        interactive: true,
      }).addTo(map)
      m.bindTooltip(`Multipoint TX #${i + 1} — click to select, Delete to remove`,
                    { direction: 'top', offset: [0, -6] })
      m.on('click', () => selectFeatureRef.current?.({ kind: 'multipoint_tx', id: i }))
      return m
    })
    return () => {
      multipointMarkersRef.current.forEach(m => m.remove())
      multipointMarkersRef.current = []
    }
  }, [multipointTxs])

  // MANET nodes — teal dots with the node's label initial; keyed by stable id.
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return
    const seen = new Set()
    manetNodes.forEach(n => {
      const id = String(n.id)
      seen.add(id)
      if (manetMarkersRef.current[id]) {
        manetMarkersRef.current[id].setLatLng([n.lat, n.lon])
      } else {
        const initial = (n.label || '?').slice(0, 1)
        const icon = L.divIcon({
          className: '',
          html: `<div style="width:18px;height:18px;border-radius:50%;background:#06d6a0;border:2px solid #fff;
                  box-shadow:0 1px 4px rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;
                  font-size:9px;font-weight:700;color:#000;">${initial}</div>`,
          iconSize: [18, 18], iconAnchor: [9, 9],
        })
        const m = L.marker([n.lat, n.lon], {
          icon, title: `${n.label} — click to select, Delete to remove`, zIndexOffset: 980,
        }).addTo(map)
        m.on('click', () => selectFeatureRef.current?.({ kind: 'manet_node', id: n.id }))
        manetMarkersRef.current[id] = m
      }
    })
    Object.keys(manetMarkersRef.current).forEach(id => {
      if (!seen.has(id)) { manetMarkersRef.current[id].remove(); delete manetMarkersRef.current[id] }
    })
  }, [manetNodes])

  // Route waypoints — small blue dots along the P2P route, indexed by array position.
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return
    routeMarkersRef.current.forEach(m => m.remove())
    routeMarkersRef.current = routeWaypoints.map((p, i) => {
      const m = L.circleMarker([p.lat, p.lon], {
        radius: 5, fillColor: '#00b4d8', fillOpacity: 0.95, color: '#fff', weight: 2,
        interactive: true,
      }).addTo(map)
      m.bindTooltip(`Waypoint ${i + 1} — click to select, Delete to remove`,
                    { direction: 'top', offset: [0, -6] })
      m.on('click', () => selectFeatureRef.current?.({ kind: 'route_waypoint', id: i }))
      return m
    })
    return () => {
      routeMarkersRef.current.forEach(m => m.remove())
      routeMarkersRef.current = []
    }
  }, [routeWaypoints])

  // ── Extra GeoJSON layers (MANET, route, satellite, ray trace, etc.) ───────
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return

    const currentIds = new Set(extraGeojsonLayers.map(l => String(l.id)))

    // Remove stale
    Object.keys(extraGeoLayersRef.current).forEach(id => {
      if (!currentIds.has(id)) {
        extraGeoLayersRef.current[id]?.remove()
        delete extraGeoLayersRef.current[id]
      }
    })

    extraGeojsonLayers.forEach(layerDef => {
      const id = String(layerDef.id)
      if (!layerDef.geojson) {
        if (extraGeoLayersRef.current[id]) {
          extraGeoLayersRef.current[id].remove()
          delete extraGeoLayersRef.current[id]
        }
        return
      }

      if (extraGeoLayersRef.current[id]) {
        extraGeoLayersRef.current[id].remove()
      }

      const color = layerDef.color || '#a855f7'

      extraGeoLayersRef.current[id] = L.geoJSON(layerDef.geojson, {
        style: (feature) => {
          const ft = feature?.properties?.feature_type
          const connected = feature?.properties?.connected
          if (feature?.geometry?.type === 'LineString') {
            if (ft === 'route') return { color: '#00b4d8', weight: 2, opacity: 0.7 }
            if (ft === 'sat_los') return { color: '#06d6a0', weight: 1, opacity: 0.5, dashArray: '4 3' }
            if (ft === 'ray')    return { color: '#f59e0b', weight: 1, opacity: 0.4 }
            // MANET link
            if (connected === true)  return { color: '#06d6a0', weight: 2, opacity: 0.8 }
            if (connected === false) return { color: '#ef4444', weight: 1.5, opacity: 0.6, dashArray: '4 4' }
            return { color, weight: 1.5, opacity: 0.6 }
          }
          return {}
        },
        pointToLayer: (feature, latlng) => {
          const ft = feature?.properties?.feature_type
          const sig = feature?.properties?.signal_dbm
          const connected = feature?.properties?.connected

          if (ft === 'manet_node') {
            const icon = L.divIcon({
              className: '',
              html: `<div style="width:16px;height:16px;border-radius:50%;background:#06d6a0;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;font-size:8px;font-weight:700;color:#000;">${(feature.properties?.label || '?').slice(0,1)}</div>`,
              iconSize: [16, 16], iconAnchor: [8, 8],
            })
            return L.marker(latlng, { icon })
          }
          if (ft === 'satellite') {
            const vis = feature?.properties?.visible
            return L.circleMarker(latlng, {
              radius: 5,
              fillColor: vis ? '#06d6a0' : '#6b7280',
              fillOpacity: 0.9,
              color: vis ? '#fff' : '#444d56', weight: 1.5, interactive: true,
            })
          }
          if (ft === 'bounce') {
            return L.circleMarker(latlng, {
              radius: 5, fillColor: '#f59e0b', fillOpacity: 1, color: '#fff', weight: 1.5,
            })
          }

          // Generic signal point
          if (sig !== undefined && sig !== null) {
            const dbm = Number(sig)
            let fillColor = '#6b7280'
            if (dbm >= -60) fillColor = '#06d6a0'
            else if (dbm >= -75) fillColor = '#84cc16'
            else if (dbm >= -90) fillColor = '#f59e0b'
            else if (dbm >= -110) fillColor = '#ef4444'
            return L.circleMarker(latlng, {
              radius: 5, fillColor, fillOpacity: 0.85,
              color: 'transparent', weight: 0, interactive: true,
            })
          }

          return L.circleMarker(latlng, {
            radius: 4, fillColor: color, fillOpacity: 0.7, stroke: false,
          })
        },
        onEachFeature: (feature, layer) => {
          const p = feature?.properties || {}
          const ft = p.feature_type

          if (ft === 'satellite') {
            layer.bindPopup(
              `<div style="font-size:11px">
                <strong>${p.name}</strong><br>
                Alt: ${p.alt_km} km<br>
                Elevation: ${p.elevation_deg_from_ground}°<br>
                ${p.visible ? '<span style="color:#06d6a0">✓ Visible</span>' : '<span style="color:#6b7280">✗ Below horizon</span>'}
              </div>`
            )
          } else if (ft === 'manet_node') {
            layer.bindPopup(`<strong>${p.label}</strong><br>Height: ${p.height_m}m`)
          } else if (p.signal_dbm !== undefined && feature?.geometry?.type === 'Point') {
            layer.bindPopup(
              `<div style="font-size:11px">
                Signal: ${p.signal_dbm} dBm<br>
                ${p.distance_m !== undefined ? `Distance: ${(p.distance_m / 1000).toFixed(1)} km<br>` : ''}
                ${p.path_loss_db !== undefined ? `Path loss: ${p.path_loss_db} dB<br>` : ''}
                ${p.propagation_mode ? `Mode: ${p.propagation_mode}` : ''}
              </div>`
            )
          } else if (feature?.geometry?.type === 'LineString' && p.node_a) {
            layer.bindPopup(
              `<div style="font-size:11px">
                ${p.node_a} → ${p.node_b}<br>
                Signal: ${p.signal_dbm} dBm<br>
                Distance: ${(p.distance_m / 1000).toFixed(1)} km<br>
                ${p.connected ? '<span style="color:#06d6a0">✓ Connected</span>' : '<span style="color:#ef4444">✗ Disconnected</span>'}
              </div>`
            )
          }
          attachRulerClick(layer)
        },
      }).addTo(map)
    })
  }, [extraGeojsonLayers])

  // ── Cursor hint for LoB observer / azimuth picking ───────────────────────
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return
    map.getContainer().style.cursor = (lobPickingMode || lobAzimuthPickingMode) ? 'crosshair' : ''
  }, [lobPickingMode, lobAzimuthPickingMode])

  // ── LoB lines, Cut/Fix markers, and CAP ellipses ──────────────────────────
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return

    // Ensure layer group exists
    if (!lobLayerGroupRef.current) {
      lobLayerGroupRef.current = L.layerGroup().addTo(map)
    }
    lobLayerGroupRef.current.clearLayers()

    if (lobs.length === 0) return

    const grp = lobLayerGroupRef.current

    // Lookup: lob.id → list of peer LoBs in the same frequency/device group
    // (used by the 'intersection' length algorithm).
    const peersById = {}
    lobGroups.forEach(g => g.lobs.forEach(l => { peersById[l.id] = g.lobs }))

    const algoType = lobAlgorithm?.type || 'estimated'

    // --- Draw each LoB bearing line ---
    lobs.forEach(lob => {
      const color = mapColors.lobLineOverride || lob.color || '#f59e0b'
      const renderDist = computeLoBRenderDistance(lob, peersById[lob.id] || [lob], lobAlgorithm)
      const endPt = destinationPoint(lob.lat, lob.lon, lob.azimuth_deg, renderDist)

      // Whether the rendered length carries meaningful range information at the tip.
      // For 'intersection', only meaningful when an actual crossing was found
      // (otherwise the line is drawn at the long fallback length).
      const lengthIsMeaningful =
        (algoType === 'estimated' && lob.tx_power_dbm !== null)
        || algoType === 'fixed'
        || algoType === 'step'
        || (algoType === 'intersection' && renderDist < INTERSECTION_FALLBACK_M)

      const distLabel = renderDist >= 1000
        ? `${(renderDist / 1000).toFixed(1)} km`
        : `${Math.round(renderDist)} m`
      let lengthLine
      if (algoType === 'intersection') {
        lengthLine = lengthIsMeaningful
          ? ` · Crosses @ ${distLabel}`
          : ' · No crossing yet'
      } else if (algoType === 'fixed') {
        lengthLine = ` · Fixed ${distLabel}`
      } else if (algoType === 'step') {
        lengthLine = ` · Step ${distLabel}`
      } else {
        lengthLine = lob.tx_power_dbm !== null
          ? ` · Est. ${distLabel}`
          : ' · Dist. unknown (no TX pwr)'
      }

      // Bearing line (dashed)
      const bearingLine = L.polyline([[lob.lat, lob.lon], endPt], {
        color,
        weight: 2,
        opacity: 0.85,
        dashArray: '10 5',
        interactive: true,
      })
        .bindTooltip(
          `<div style="font-size:11px;line-height:1.5">
            <strong>${lob.label}</strong><br/>
            ${(lob.frequency_hz / 1e6).toFixed(3)} MHz<br/>
            Az ${lob.azimuth_deg.toFixed(1)}° · RSSI ${lob.rssi_dbm} dBm<br/>
            Conf ${lob.confidence_pct}%${lengthLine}<br/>
            ${lob.time || ''}
          </div>`,
          { sticky: true },
        )
        .addTo(grp)
      attachRulerClick(bearingLine)

      // Observer marker
      const observerMarker = L.circleMarker([lob.lat, lob.lon], {
        radius: 6,
        color: '#fff',
        weight: 1.5,
        fillColor: color,
        fillOpacity: 0.9,
        interactive: true,
      })
        .bindTooltip(`<strong>${lob.label}</strong><br/>${lob.lat.toFixed(5)}, ${lob.lon.toFixed(5)}`)
        .addTo(grp)
      attachRulerClick(observerMarker)

      // Range endpoint tick — drawn whenever the rendered length is meaningful.
      if (lengthIsMeaningful) {
        L.circleMarker(endPt, {
          radius: 3,
          color,
          weight: 1,
          fillColor: '#fff',
          fillOpacity: 0.6,
          interactive: false,
        }).addTo(grp)
      }

      // Azimuth label near observer
      L.marker([lob.lat, lob.lon], {
        icon: L.divIcon({
          className: '',
          html: `<span style="color:${color};font-size:9px;font-weight:700;
                   text-shadow:0 0 4px #000,0 0 4px #000;white-space:nowrap;
                   margin-left:10px;margin-top:-6px;display:block">
                   ${lob.label} ${lob.azimuth_deg.toFixed(0)}°
                 </span>`,
          iconSize: [80, 14],
          iconAnchor: [-2, 16],
        }),
        interactive: false,
        zIndexOffset: 200,
      }).addTo(grp)
    })

    // --- Draw group markers (Cut / Fix) and CAP ellipses ---
    lobGroups.forEach(group => {
      if (group.lobs.length < 2) return

      const intersections = computeGroupIntersections(group)
      if (intersections.length === 0) return

      const centroid = computeCentroid(intersections)
      if (!centroid) return

      const isFix = group.lobs.length >= 3
      const typeLabel = isFix ? 'FIX' : 'CUT'
      const color = isFix ? mapColors.lobFix : mapColors.lobCut
      const freqLabel = `${(group.frequency_hz / 1e6).toFixed(3)} MHz`

      // Type label marker
      L.marker([centroid.lat, centroid.lon], {
        icon: L.divIcon({
          className: '',
          html: `<div style="
            background:${color};color:#fff;
            font-size:9px;font-weight:800;
            padding:2px 6px;border-radius:3px;
            box-shadow:0 1px 5px rgba(0,0,0,0.7);
            white-space:nowrap;border:1px solid rgba(255,255,255,0.3);
          ">${typeLabel} · ${freqLabel}${group.device_id ? ` · ${group.device_id}` : ''}</div>`,
          iconSize: [null, null],
          iconAnchor: [isFix ? 36 : 32, -6],
        }),
        interactive: false,
        zIndexOffset: 600,
      }).addTo(grp)

      // CAP ellipse — render only; tooltip is shown on the centroid icon below.
      let capInfoHtml = ''
      if (capGroups[lobGroupKey(group)] !== false) {
        const ellipseFeature = computeCAPEllipse(group, intersections, lobAlgorithm)
        if (ellipseFeature) {
          const semiMajorM = ellipseFeature.properties.semiMajorM
          const semiMinorM = ellipseFeature.properties.semiMinorM
          const fullSize = Math.max(semiMajorM, semiMinorM) * 2
          const mgrsPrec = mgrsPrecisionForSize(fullSize)
          capInfoHtml = `
            <div style="margin-top:4px;border-top:1px solid #30363d;padding-top:3px">
              <strong>CAP Ellipse</strong><br/>
              Semi-major: ${(semiMajorM / 1000).toFixed(2)} km<br/>
              Semi-minor: ${(semiMinorM / 1000).toFixed(2)} km
            </div>
            <div style="margin-top:3px;font-family:monospace;font-size:10px">
              DD: ${centroid.lat.toFixed(5)}, ${centroid.lon.toFixed(5)}<br/>
              DMS: ${formatCoordinate(centroid.lat, centroid.lon, 'latlon_dms')}<br/>
              UTM: ${formatCoordinate(centroid.lat, centroid.lon, 'utm')}<br/>
              MGRS: ${toMGRSAt(centroid.lat, centroid.lon, mgrsPrec)}
            </div>`
          L.geoJSON(ellipseFeature, {
            style: {
              color,
              weight: 1.5,
              opacity: 0.8,
              fillColor: color,
              fillOpacity: 0.12,
              dashArray: '5 4',
            },
            onEachFeature: (_f, layer) => attachRulerClick(layer),
          }).addTo(grp)
        }
      }

      // Cross-hair / intersection marker — sole carrier of the CAP tooltip.
      const centroidMarker = L.circleMarker([centroid.lat, centroid.lon], {
        radius: 9,
        color: '#fff',
        weight: 2,
        fillColor: color,
        fillOpacity: 0.6,
        interactive: true,
      })
        .bindTooltip(
          `<div style="font-size:11px;line-height:1.5;min-width:180px">
            <strong style="color:${color}">${typeLabel}</strong><br/>
            ${freqLabel} · ${group.lobs.length} LoBs<br/>
            ${group.device_id ? `ID: ${group.device_id}<br/>` : ''}${centroid.lat.toFixed(5)}, ${centroid.lon.toFixed(5)}
            ${capInfoHtml}
          </div>`,
          { permanent: false, direction: 'top' },
        )
        .addTo(grp)
      attachRulerClick(centroidMarker)
      // Right-click on this fix/cut centroid opens the map context menu with
      // an extra "Simulate Propagation" entry, so the operator can attach a
      // propagation emitter that tracks this group's updating centroid.
      centroidMarker.on('contextmenu', (e) => {
        if (drawCtrlRef.current?.getActiveTool()) return
        if (terrainLineModeRef.current) return
        const map = leafletRef.current; if (!map) return
        const pt = map.latLngToContainerPoint(centroidMarker.getLatLng())
        setCtxMenu({
          x: pt.x, y: pt.y, lat: centroid.lat, lon: centroid.lon,
          target: { kind: 'fix-cut', groupSummary: {
            frequency_hz: group.frequency_hz,
            device_id: group.device_id || '',
            device_type: group.device_type || '',
            n_lobs: group.lobs.length,
            kind: isFix ? 'fix' : 'cut',
          } },
        })
        L.DomEvent.stopPropagation(e)
        if (e.originalEvent) L.DomEvent.preventDefault(e.originalEvent)
      })
    })
  }, [lobs, lobGroups, capGroups, mapColors, lobAlgorithm])

  // ── P2P path line ──────────────────────────────────────────────────────────
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return

    if (p2pLineRef.current) { p2pLineRef.current.remove(); p2pLineRef.current = null }

    if (activeTab === 'p2p' && rxPoint) {
      p2pLineRef.current = L.polyline(
        [[tx.lat, tx.lon], [rxPoint.lat, rxPoint.lon]],
        { color: '#a855f7', weight: 2, dashArray: '6 4', opacity: 0.8 }
      ).addTo(map)
    }
  }, [activeTab, tx.lat, tx.lon, rxPoint])

  // ── Best-Site candidate markers ────────────────────────────────────────────
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return
    if (bestSiteLayerRef.current) { try { bestSiteLayerRef.current.remove() } catch {} bestSiteLayerRef.current = null }
    if (!bestSiteCandidates.length) return
    const ranked = bestSiteResult?.sites || []
    const near = (a, b) => Math.abs(a.lat - b.lat) < 1e-6 && Math.abs(a.lon - b.lon) < 1e-6
    const winner = ranked[0]
    const grp = L.layerGroup().addTo(map)
    bestSiteCandidates.forEach((c, i) => {
      const isWinner = winner && near(c, winner)
      const rank = ranked.findIndex(s => near(c, s))
      const color = isWinner ? '#06d6a0' : '#f59e0b'
      L.circleMarker([c.lat, c.lon], {
        radius: isWinner ? 9 : 7, fillColor: color, fillOpacity: 0.85,
        color: '#0d1117', weight: 2,
      }).addTo(grp).bindTooltip(
        `<b style="color:${color}">${c.label || `Site ${i + 1}`}</b>${isWinner ? ' ★ best' : (rank >= 0 ? ` · rank ${rank + 1}` : '')}<br/>${c.lat.toFixed(5)}, ${c.lon.toFixed(5)}` +
          (rank >= 0 && ranked[rank]?.covered_area_km2 != null ? `<br/>${ranked[rank].covered_area_km2} km² covered` : ''),
        { direction: 'top', permanent: false },
      )
      L.marker([c.lat, c.lon], {
        icon: L.divIcon({ className: '', html: `<div style="font:700 10px sans-serif;color:#0d1117;text-align:center;width:14px">${i + 1}</div>`, iconSize: [14, 14], iconAnchor: [7, 7] }),
        interactive: false,
      }).addTo(grp)
    })
    bestSiteLayerRef.current = grp
    return () => { try { grp.remove() } catch {} if (bestSiteLayerRef.current === grp) bestSiteLayerRef.current = null }
  }, [bestSiteCandidates, bestSiteResult])

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <div style={{ position: 'relative', height: '100%', width: '100%' }}>
      <div ref={mapRef} style={{ height: '100%', width: '100%' }} />

      {/* Tile style switcher + ruler + search + color cog — top right */}
      <div className="map-overlay map-overlay-topright" style={{ padding: '6px 8px' }}>
        <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
          {Object.keys(TILE_LAYERS).map(s => (
            <button
              key={s}
              className={`btn ${tileStyle === s ? 'btn-primary' : 'btn-ghost'}`}
              style={{ padding: '3px 8px', fontSize: 11 }}
              onClick={() => setTileStyle(s)}
            >
              {TILE_LAYERS[s]?.label || (s.charAt(0).toUpperCase() + s.slice(1))}
            </button>
          ))}
          {/* Ruler tool — multiple rulers accumulate until cleared */}
          <button
            className={`btn ${rulerMode ? 'btn-primary' : 'btn-ghost'}`}
            style={{ padding: '3px 8px', fontSize: 11 }}
            title={rulerMode ? 'Stop adding rulers (completed rulers stay on map)' : 'Distance & heading ruler — each pair of clicks adds a ruler'}
            onClick={() => setRulerMode(r => !r)}
          >
            📏{rulerResults.length > 0 ? ` ${rulerResults.length}` : ''}
          </button>
          {rulerResults.length > 0 && (
            <button
              className="btn btn-ghost"
              style={{ padding: '3px 7px', fontSize: 12, color: '#8b949e', lineHeight: 1 }}
              title="Clear all rulers"
              onClick={clearRulers}
            >
              ×
            </button>
          )}
          {/* Center on feature */}
          <div style={{ position: 'relative' }}>
            <button
              className={`btn ${centerOnOpen ? 'btn-primary' : 'btn-ghost'}`}
              style={{ padding: '3px 8px', fontSize: 11 }}
              title="Center view on a feature"
              onClick={() => setCenterOnOpen(o => !o)}
            >
              ⊕
            </button>
            {centerOnOpen && (() => {
              const map = leafletRef.current
              if (!map) return null

              // Build the list of centerable features
              const items = []

              if (txActive) {
                items.push({
                  label: `${txLabel} (emitter)`,
                  action: () => map.setView([tx.lat, tx.lon], Math.max(map.getZoom(), 13)),
                })
              }

              extraTxList.forEach(e => {
                items.push({
                  label: `${e.label} (emitter)`,
                  action: () => map.setView([e.lat, e.lon], Math.max(map.getZoom(), 13)),
                })
              })

              if (rxPoint) {
                items.push({
                  label: 'RX receiver',
                  action: () => map.setView([rxPoint.lat, rxPoint.lon], Math.max(map.getZoom(), 13)),
                })
              }

              lobs.forEach(lob => {
                items.push({
                  label: `${lob.label} observer`,
                  action: () => map.setView([lob.lat, lob.lon], Math.max(map.getZoom(), 13)),
                })
              })

              // LoB Fix/Cut intersections
              lobGroups.forEach(group => {
                if (group.lobs.length < 2) return
                const ints = computeGroupIntersections(group)
                const centroid = computeCentroid(ints)
                if (!centroid) return
                const label = `${group.lobs.length >= 3 ? 'FIX' : 'CUT'} · ${(group.frequency_hz / 1e6).toFixed(3)} MHz`
                items.push({
                  label,
                  action: () => map.setView([centroid.lat, centroid.lon], Math.max(map.getZoom(), 13)),
                })
              })

              if (coverageGeoJSON) {
                items.push({
                  label: 'Coverage layer',
                  action: () => {
                    try {
                      const layer = L.geoJSON(coverageGeoJSON)
                      map.fitBounds(layer.getBounds(), { padding: [30, 30] })
                    } catch {}
                  },
                })
              }

              // Fit all visible features
              const allPts = [
                txActive ? [tx.lat, tx.lon] : null,
                rxPoint ? [rxPoint.lat, rxPoint.lon] : null,
                ...lobs.map(l => [l.lat, l.lon]),
                ...extraTxList.map(e => [e.lat, e.lon]),
              ].filter(Boolean)

              if (allPts.length > 1) {
                items.push({
                  label: 'Fit all features',
                  action: () => {
                    const bounds = L.latLngBounds(allPts)
                    map.fitBounds(bounds, { padding: [60, 60] })
                  },
                  divider: true,
                })
              }

              if (items.length === 0) {
                return (
                  <div style={{
                    position: 'absolute', top: '110%', right: 0, marginTop: 2,
                    background: '#161b22', border: '1px solid #30363d', borderRadius: 8,
                    padding: '10px 14px', zIndex: 9999, whiteSpace: 'nowrap',
                    fontSize: 12, color: '#484f58',
                    boxShadow: '0 6px 20px rgba(0,0,0,0.7)',
                  }}>
                    No features placed yet
                  </div>
                )
              }

              return (
                <div style={{
                  position: 'absolute', top: '110%', right: 0, marginTop: 2,
                  background: '#161b22', border: '1px solid #30363d', borderRadius: 8,
                  padding: '4px 0', zIndex: 9999, minWidth: 200,
                  boxShadow: '0 6px 20px rgba(0,0,0,0.7)',
                }}>
                  <div style={{ fontSize: 10, fontWeight: 700, color: '#484f58', padding: '3px 12px 5px', letterSpacing: 0.7 }}>
                    CENTER VIEW ON
                  </div>
                  {items.map((item, i) => (
                    <div key={i}>
                      {item.divider && <div style={{ height: 1, background: '#21262d', margin: '4px 0' }} />}
                      <button
                        style={{
                          display: 'block', width: '100%', padding: '6px 12px',
                          background: 'none', border: 'none', cursor: 'pointer',
                          color: '#c9d1d9', fontSize: 12, textAlign: 'left',
                          transition: 'background 100ms',
                        }}
                        onMouseEnter={e => e.currentTarget.style.background = '#21262d'}
                        onMouseLeave={e => e.currentTarget.style.background = 'none'}
                        onClick={() => { item.action(); setCenterOnOpen(false) }}
                      >
                        {item.label}
                      </button>
                    </div>
                  ))}
                </div>
              )
            })()}
          </div>

          {/* Location search icon */}
          <button
            className={`btn ${searchOpen ? 'btn-primary' : 'btn-ghost'}`}
            style={{ padding: '3px 8px', fontSize: 11 }}
            title="Search location"
            onClick={() => { setSearchOpen(o => !o); setSearchResults([]); setSearchError(''); setSearchQuery('') }}
          >
            🔍
          </button>

          {/* Tools dropdown — drawing, mapping, imports (ATAK-style) */}
          <div style={{ position: 'relative' }}>
            <button
              className={`btn ${toolsOpen || toolsActive || drawMode === 'bounds' ? 'btn-primary' : 'btn-ghost'}`}
              style={{ padding: '3px 8px', fontSize: 11 }}
              title="Drawing & mapping tools"
              onClick={() => setToolsOpen(o => !o)}
            >
              ✎{(toolsActive || drawMode === 'bounds') ? ' •' : ''}
            </button>
            {toolsOpen && (
              <div
                style={{
                  position: 'absolute', top: '110%', right: 0, marginTop: 2,
                  background: '#161b22', border: '1px solid #30363d', borderRadius: 8,
                  padding: '8px', minWidth: 260, zIndex: 9999,
                  boxShadow: '0 6px 20px rgba(0,0,0,0.7)',
                  maxHeight: '70vh', overflowY: 'auto',
                }}
                onClick={e => e.stopPropagation()}
              >
                {/* Coverage analysis — propagation-mode only (App passes the toggle) */}
                {onToggleBoundsDraw && (
                  <>
                    <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e',
                                  padding: '0 4px 6px', textTransform: 'uppercase', letterSpacing: 0.8 }}>
                      Analysis
                    </div>
                    <button
                      className={`btn ${drawMode === 'bounds' ? 'btn-primary' : 'btn-ghost'}`}
                      style={{ width: '100%', padding: '6px 8px', fontSize: 11, marginBottom: 8,
                               display: 'flex', alignItems: 'center', gap: 6, justifyContent: 'flex-start' }}
                      title="Draw a rectangle to constrain the coverage simulation area"
                      onClick={toggleBoundsDraw}>
                      <span style={{ fontSize: 13 }}>▭</span>
                      Draw Bounds{drawMode === 'bounds' ? ' ✓' : ''}
                    </button>
                  </>
                )}

                <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e',
                              padding: '0 4px 6px', textTransform: 'uppercase', letterSpacing: 0.8 }}>
                  Basic
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 4 }}>
                  {TOOL_KINDS.basic.map(t => (
                    <button key={t.id}
                      className={`btn ${toolsActive === t.id ? 'btn-primary' : 'btn-ghost'}`}
                      style={{ padding: '6px 4px', fontSize: 10, lineHeight: 1.2,
                               display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2 }}
                      title={t.label}
                      onClick={() => activateTool(t.id)}>
                      <span style={{ fontSize: 14 }}>{t.icon}</span>
                      <span>{t.label}</span>
                    </button>
                  ))}
                </div>

                <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e',
                              padding: '8px 4px 6px', textTransform: 'uppercase', letterSpacing: 0.8 }}>
                  Advanced
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 4 }}>
                  {TOOL_KINDS.advanced.map(t => (
                    <button key={t.id}
                      className={`btn ${toolsActive === t.id ? 'btn-primary' : 'btn-ghost'}`}
                      style={{ padding: '6px 4px', fontSize: 10, lineHeight: 1.2,
                               display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2 }}
                      title={t.label}
                      onClick={() => activateTool(t.id)}>
                      <span style={{ fontSize: 14 }}>{t.icon}</span>
                      <span>{t.label}</span>
                    </button>
                  ))}
                </div>

                <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e',
                              padding: '8px 4px 6px', textTransform: 'uppercase', letterSpacing: 0.8 }}>
                  Briefing
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 4 }}>
                  {TOOL_KINDS.briefing.map(t => (
                    <button key={t.id}
                      className={`btn ${toolsActive === t.id ? 'btn-primary' : 'btn-ghost'}`}
                      style={{ padding: '6px 4px', fontSize: 10, lineHeight: 1.2,
                               display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2 }}
                      title={t.label}
                      onClick={() => activateTool(t.id)}>
                      <span style={{ fontSize: 14 }}>{t.icon}</span>
                      <span>{t.label}</span>
                    </button>
                  ))}
                </div>

                <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e',
                              padding: '8px 4px 6px', textTransform: 'uppercase', letterSpacing: 0.8 }}>
                  Markers
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 4 }}>
                  {TOOL_KINDS.military.map(t => (
                    <button key={t.id}
                      className={`btn ${toolsActive === t.id ? 'btn-primary' : 'btn-ghost'}`}
                      style={{ padding: '6px 4px', fontSize: 10, lineHeight: 1.2,
                               display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2,
                               color: toolsActive === t.id ? undefined : t.color }}
                      title={t.label}
                      onClick={() => activateTool(t.id)}>
                      <span style={{ fontSize: 14 }}>{t.icon}</span>
                      <span>{t.label}</span>
                    </button>
                  ))}
                </div>

                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                              padding: '8px 4px 4px' }}>
                  <span style={{ fontSize: 10, fontWeight: 700, color: '#8b949e',
                                 textTransform: 'uppercase', letterSpacing: 0.8 }}>
                    NATO / Ranger Symbology
                  </span>
                  <button className="btn btn-ghost"
                    style={{ padding: '2px 6px', fontSize: 10,
                             color: natoOpen ? '#06d6a0' : '#8b949e' }}
                    onClick={() => setNatoOpen(o => !o)}>
                    {natoOpen ? '▾ Hide' : '▸ Open picker'}
                  </button>
                </div>
                {natoOpen && (
                  <div style={{ background: '#0d1117', border: '1px solid #30363d',
                                borderRadius: 6, padding: '4px 6px', marginBottom: 6 }}>
                    {/* ErrorBoundary surfaces any module-load or render error
                        from the lazy NatoSymbolPicker — without it, a thrown
                        error inside Suspense leaves the fallback visible
                        forever ("stuck loading"). */}
                    <ErrorBoundary label="NATO symbology picker" resetKey={natoOpen}>
                      <Suspense fallback={
                        <div style={{ fontSize: 11, color: '#8b949e', padding: 14, textAlign: 'center' }}>
                          Loading NATO symbology…
                        </div>
                      }>
                        <NatoSymbolPicker
                          ctrl={drawCtrlRef.current}
                          onArm={(arm) => {
                            drawCtrlRef.current?.setNatoSymbol?.(arm)
                            if (drawCtrlRef.current?.getActiveTool() !== 'nato') {
                              drawCtrlRef.current?.activate?.('nato')
                            }
                          }}
                        />
                      </Suspense>
                    </ErrorBoundary>
                  </div>
                )}

                <div style={{ height: 1, background: '#30363d', margin: '8px 0' }} />

                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
                  <span style={{ fontSize: 11, color: '#c9d1d9' }}>Stroke color</span>
                  <input type="color" value={toolsStrokeColor}
                    onChange={e => setToolsStrokeColor(e.target.value)}
                    style={{ width: 32, height: 22, border: '1px solid #30363d', padding: 1,
                             cursor: 'pointer', borderRadius: 3, background: 'none' }} />
                </div>

                <div style={{ display: 'flex', gap: 4, marginBottom: 8 }}>
                  <button className="btn btn-ghost"
                    style={{ flex: 1, fontSize: 11, padding: '4px 6px' }}
                    onClick={() => fileInputRef.current?.click()}>
                    📂 Load file…
                  </button>
                  <button className="btn btn-ghost"
                    style={{ flex: 1, fontSize: 11, padding: '4px 6px' }}
                    onClick={exportDrawnGeoJSON}>
                    💾 Export
                  </button>
                </div>
                <div style={{ display: 'flex', gap: 4, marginBottom: 4 }}>
                  <button className="btn btn-ghost"
                    style={{ flex: 1, fontSize: 11, padding: '4px 6px' }}
                    disabled={!toolsActive}
                    onClick={() => drawCtrlRef.current?.finishCurrent()}>
                    Finish
                  </button>
                  <button className="btn btn-ghost"
                    style={{ flex: 1, fontSize: 11, padding: '4px 6px', color: '#fca5a5' }}
                    onClick={() => drawCtrlRef.current?.clearAll()}>
                    Clear all
                  </button>
                </div>

                {drawnFeatures.length > 0 && (
                  <>
                    <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e',
                                  padding: '8px 4px 4px', textTransform: 'uppercase', letterSpacing: 0.8 }}>
                      Drawn ({drawnFeatures.length})
                    </div>
                    <div style={{ maxHeight: 130, overflowY: 'auto' }}>
                      {drawnFeatures.map(f => (
                        <div key={f.id} style={{ display: 'flex', alignItems: 'center', gap: 4,
                                                 padding: '3px 4px', fontSize: 11 }}>
                          <button className="btn btn-ghost"
                            style={{ flex: 1, textAlign: 'left', padding: '2px 6px', fontSize: 11,
                                     color: '#c9d1d9', overflow: 'hidden', whiteSpace: 'nowrap',
                                     textOverflow: 'ellipsis' }}
                            onClick={() => drawCtrlRef.current?.focusFeature(f.id)}
                            title={`${f.kind}: ${f.meta.name || ''}`}>
                            <span style={{ color: '#8b949e', marginRight: 4 }}>[{f.kind}]</span>
                            {f.meta.name || ''}
                          </button>
                          <button className="btn btn-ghost"
                            style={{ padding: '2px 5px', fontSize: 11, color: '#fca5a5', flexShrink: 0 }}
                            title="Delete"
                            onClick={() => drawCtrlRef.current?.removeFeature(f.id)}>×</button>
                        </div>
                      ))}
                    </div>
                  </>
                )}

                {ul && ul.layers.length > 0 && (
                  <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e',
                                padding: '8px 4px 0', textTransform: 'uppercase', letterSpacing: 0.8 }}>
                    {ul.layers.length} imported layer{ul.layers.length > 1 ? 's' : ''} —
                    open the <em style={{ color: '#06d6a0' }}>Layers</em> tab to manage
                  </div>
                )}

                <div style={{ fontSize: 9, color: '#484f58', padding: '8px 4px 0', lineHeight: 1.4 }}>
                  Drag &amp; drop KML, KMZ, GeoJSON, GPX, GeoTIFF, DTED or images onto the map.
                  Right-click or Esc to finish a draw tool.
                </div>
              </div>
            )}
          </div>

          {/* 2D / 3D view toggle (kept on the toolbar — same spot as the globe's) */}
          <button className="btn btn-ghost" style={{ padding: '3px 8px', fontSize: 13 }}
            title="Switch to the 3D globe" onClick={() => setViewMode('3d')}>3D</button>

          {/* Map settings ⚙ — shared with the 3D globe; units, coordinate system,
              compass rose, brightness, coverage render (3D), feature colours. */}
          <MapSettingsCog
            kind="2d"
            distUnit={distUnit} setDistUnit={setDistUnit}
            coordSystem={coordSystem} setCoordSystem={setCoordSystem}
            showCompassRose={showCompassRose} setShowCompassRose={setShowCompassRose}
            mapBrightness={mapBrightness} setMapBrightness={setMapBrightness}
          />
        </div>

        {/* Expanding search panel — renders below the toolbar row */}
        {searchOpen && (
          <div style={{ marginTop: 6, borderTop: '1px solid #30363d', paddingTop: 6 }}>
            <form
              onSubmit={e => { e.preventDefault(); handleSearch() }}
              style={{ display: 'flex', gap: 4 }}
            >
              <input
                ref={searchInputRef}
                autoFocus
                value={searchQuery}
                onChange={e => { setSearchQuery(e.target.value); if (!e.target.value) { setSearchResults([]); setSearchError('') } }}
                placeholder="Place, DD, DMS, DDM, MGRS, UTM, Maidenhead…"
                style={{
                  flex: 1, minWidth: 180, background: '#0d1117', border: '1px solid #30363d',
                  borderRadius: 4, color: '#e6edf3', fontSize: 11, padding: '4px 7px', outline: 'none',
                }}
              />
              <button
                type="submit"
                className="btn btn-ghost"
                style={{ padding: '3px 7px', fontSize: 11, flexShrink: 0 }}
                disabled={searchLoading}
              >
                {searchLoading ? '…' : 'Go'}
              </button>
            </form>
            {searchError && (
              <div style={{ fontSize: 10, color: '#ef4444', marginTop: 4 }}>{searchError}</div>
            )}
            {searchResults.length > 0 && (
              <div style={{ marginTop: 4, display: 'flex', flexDirection: 'column', gap: 4 }}>
                {searchResults.map((r, i) => (
                  <SearchResultRow key={i}
                    result={r}
                    onSelect={() => {
                      if (leafletRef.current) {
                        if (r.bounds) leafletRef.current.fitBounds(r.bounds, { padding: [30, 30] })
                        else leafletRef.current.setView([r.lat, r.lon], 12)
                      }
                    }}
                    onSave={onSaveLocation ? () => {
                      onSaveLocation({ name: r.name, lat: r.lat, lon: r.lon })
                      setSearchResults([]); setSearchQuery('')
                    } : null}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Ruler mode hint */}
      {rulerMode && (
        <div className="map-overlay" style={{
          top: 50, left: '50%', transform: 'translateX(-50%)',
          padding: '6px 14px', fontSize: 12, color: mapColors.ruler,
          pointerEvents: 'none', whiteSpace: 'nowrap',
        }}>
          {pendingPoints === 1 ? 'Click second point…' : 'Click to place ruler — each pair of points creates a measurement'}
        </div>
      )}

      {/* Coverage legend */}
      {(coverageGeoJSON || extraTxList.some(e => e.geojson)) && (
        <div className="map-overlay map-overlay-bottomright">
          <CoverageLegend minDbm={-120} maxDbm={0} />
          {/* Extra TX color legend */}
          {extraTxList.length > 0 && (
            <div style={{ marginTop: 8, fontSize: 11, color: '#8b949e' }}>
              <div style={{ marginBottom: 4, color: '#00b4d8' }}>● {txLabel} (Primary)</div>
              {extraTxList.map(e => (
                <div key={e.id} style={{ color: e.color, marginBottom: 2 }}>
                  ● {e.label}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── Right-click context menu ────────────────────────────────────── */}
      {ctxMenu && (
        <div
          style={{
            position: 'absolute',
            left: ctxMenu.x,
            top: ctxMenu.y,
            zIndex: 9500,
            background: '#161b22',
            border: '1px solid #30363d',
            borderRadius: 7,
            padding: '4px 0',
            minWidth: 230,
            maxWidth: 320,
            boxShadow: '0 6px 24px rgba(0,0,0,0.6)',
            userSelect: 'none',
          }}
          // Stop map clicks beneath the menu from propagating
          onMouseDown={e => e.stopPropagation()}
        >
          <div style={{
            fontSize: 10, fontWeight: 600, color: '#484f58',
            padding: '4px 14px 2px', letterSpacing: 0.8,
          }}>
            {ctxMenu.lat.toFixed(5)}, {ctxMenu.lon.toFixed(5)}
          </div>
          <div style={{ height: 1, background: '#21262d', margin: '4px 0' }} />
          <button
            style={{
              display: 'flex', alignItems: 'center', gap: 10,
              width: '100%', padding: '8px 14px',
              background: 'none', border: 'none', cursor: 'pointer',
              color: mapColors.ruler, fontSize: 13, textAlign: 'left',
              transition: 'background 120ms',
            }}
            onMouseEnter={e => e.currentTarget.style.background = '#21262d'}
            onMouseLeave={e => e.currentTarget.style.background = 'none'}
            onClick={() => {
              if (!rulerModeRef.current) setRulerMode(true)
              addRulerPointRef.current?.(ctxMenu.lat, ctxMenu.lon)
              setCtxMenu(null)
            }}
          >
            <span style={{ flexShrink: 0, fontSize: 14, lineHeight: 1 }}>📏</span>
            Add Ruler
          </button>
          <button
            style={{
              display: 'flex', alignItems: 'center', gap: 10,
              width: '100%', padding: '8px 14px',
              background: 'none', border: 'none', cursor: 'pointer',
              color: '#00b4d8', fontSize: 13, textAlign: 'left',
              transition: 'background 120ms',
            }}
            onMouseEnter={e => e.currentTarget.style.background = '#21262d'}
            onMouseLeave={e => e.currentTarget.style.background = 'none'}
            onClick={() => {
              onAddEmitter?.(ctxMenu.lat, ctxMenu.lon)
              setCtxMenu(null)
            }}
          >
            <svg width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2"
                 viewBox="0 0 24 24" style={{ flexShrink: 0 }}>
              <path d="M5 12.55a11 11 0 0 1 14.08 0M1.42 9a16 16 0 0 1 21.16 0
                       M8.53 16.11a6 6 0 0 1 6.95 0M12 20h.01"/>
            </svg>
            Add Emitter
          </button>
          <button
            style={{
              display: 'flex', alignItems: 'center', gap: 10,
              width: '100%', padding: '8px 14px',
              background: 'none', border: 'none', cursor: 'pointer',
              color: '#a78bfa', fontSize: 13, textAlign: 'left',
              transition: 'background 120ms',
            }}
            onMouseEnter={e => e.currentTarget.style.background = '#21262d'}
            onMouseLeave={e => e.currentTarget.style.background = 'none'}
            onClick={() => {
              onAddLoBObserver?.(ctxMenu.lat, ctxMenu.lon)
              setCtxMenu(null)
            }}
          >
            <svg width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2"
                 viewBox="0 0 24 24" style={{ flexShrink: 0 }}>
              <circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/>
              <line x1="12" y1="2" x2="12" y2="5"/>
              <line x1="12" y1="19" x2="12" y2="22"/>
              <line x1="2" y1="12" x2="5" y2="12"/>
              <line x1="19" y1="12" x2="22" y2="12"/>
            </svg>
            Add LoB Observer
          </button>
          <div style={{ height: 1, background: '#21262d', margin: '4px 0' }} />
          <button
            style={{
              display: 'flex', alignItems: 'center', gap: 10,
              width: '100%', padding: '8px 14px',
              background: 'none', border: 'none', cursor: 'pointer',
              color: '#06d6a0', fontSize: 13, textAlign: 'left',
              transition: 'background 120ms',
            }}
            onMouseEnter={e => e.currentTarget.style.background = '#21262d'}
            onMouseLeave={e => e.currentTarget.style.background = 'none'}
            onClick={() => {
              onDownloadRegionAt?.(ctxMenu.lat, ctxMenu.lon)
              setCtxMenu(null)
            }}
          >
            <span style={{ flexShrink: 0, fontSize: 14, lineHeight: 1 }}>🗺️</span>
            Download mapping data for this region…
          </button>
          {ctxMenu.target?.kind === 'fix-cut' && (
            <>
              <button
                style={{
                  display: 'flex', alignItems: 'center', gap: 10,
                  width: '100%', padding: '8px 14px',
                  background: 'none', border: 'none', cursor: 'pointer',
                  color: '#00b4d8', fontSize: 13, textAlign: 'left',
                  transition: 'background 120ms',
                }}
                onMouseEnter={e => e.currentTarget.style.background = '#21262d'}
                onMouseLeave={e => e.currentTarget.style.background = 'none'}
                onClick={() => {
                  onSimulatePropagationFromFix?.(ctxMenu.target.groupSummary, ctxMenu.lat, ctxMenu.lon)
                  setCtxMenu(null)
                }}
              >
                <span style={{ flexShrink: 0, fontSize: 14, lineHeight: 1 }}>📡</span>
                Simulate propagation from this {ctxMenu.target.groupSummary?.kind?.toUpperCase()}…
              </button>
              <div style={{ height: 1, background: '#21262d', margin: '4px 0' }} />
            </>
          )}
          <button
            style={{
              display: 'flex', alignItems: 'center', gap: 10,
              width: '100%', padding: '8px 14px',
              background: 'none', border: 'none', cursor: 'pointer',
              color: '#f59e0b', fontSize: 13, textAlign: 'left',
              transition: 'background 120ms',
            }}
            onMouseEnter={e => e.currentTarget.style.background = '#21262d'}
            onMouseLeave={e => e.currentTarget.style.background = 'none'}
            onClick={() => { onViewshedAt?.(ctxMenu.lat, ctxMenu.lon); setCtxMenu(null) }}
          >
            <span style={{ flexShrink: 0, fontSize: 14, lineHeight: 1 }}>👁</span>
            Viewshed from here…
          </button>
          <button
            style={{
              display: 'flex', alignItems: 'center', gap: 10,
              width: '100%', padding: '8px 14px',
              background: 'none', border: 'none', cursor: 'pointer',
              color: '#a78bfa', fontSize: 13, textAlign: 'left',
              transition: 'background 120ms',
            }}
            onMouseEnter={e => e.currentTarget.style.background = '#21262d'}
            onMouseLeave={e => e.currentTarget.style.background = 'none'}
            onClick={() => { onContoursAt?.(ctxMenu.lat, ctxMenu.lon); setCtxMenu(null) }}
          >
            <span style={{ flexShrink: 0, fontSize: 14, lineHeight: 1 }}>⛰️</span>
            Contour lines around here…
          </button>
          {(hasViewsheds || hasContours) && (
            <>
              <div style={{ height: 1, background: '#21262d', margin: '4px 0' }} />
              {hasViewsheds && (
                <button
                  style={{
                    display: 'flex', alignItems: 'center', gap: 10,
                    width: '100%', padding: '8px 14px',
                    background: 'none', border: 'none', cursor: 'pointer',
                    color: '#f59e0b', fontSize: 13, textAlign: 'left',
                    transition: 'background 120ms',
                  }}
                  onMouseEnter={e => e.currentTarget.style.background = '#21262d'}
                  onMouseLeave={e => e.currentTarget.style.background = 'none'}
                  onClick={() => { onClearViewsheds?.(); setCtxMenu(null) }}
                >
                  <span style={{ flexShrink: 0, fontSize: 14, lineHeight: 1 }}>🗑</span>
                  Clear viewsheds
                </button>
              )}
              {hasContours && (
                <button
                  style={{
                    display: 'flex', alignItems: 'center', gap: 10,
                    width: '100%', padding: '8px 14px',
                    background: 'none', border: 'none', cursor: 'pointer',
                    color: '#a78bfa', fontSize: 13, textAlign: 'left',
                    transition: 'background 120ms',
                  }}
                  onMouseEnter={e => e.currentTarget.style.background = '#21262d'}
                  onMouseLeave={e => e.currentTarget.style.background = 'none'}
                  onClick={() => { onClearContours?.(); setCtxMenu(null) }}
                >
                  <span style={{ flexShrink: 0, fontSize: 14, lineHeight: 1 }}>🗑</span>
                  Clear contour lines
                </button>
              )}
            </>
          )}
          <div style={{ height: 1, background: '#21262d', margin: '4px 0' }} />
          <div style={{
            fontSize: 9, fontWeight: 700, color: '#484f58',
            padding: '4px 14px 2px', letterSpacing: 0.8,
          }}>
            COORDINATES — click to copy
          </div>
          <CtxCoordRows lat={ctxMenu.lat} lon={ctxMenu.lon} />
        </div>
      )}


      {/* LoB observer pick hint */}
      {lobPickingMode && (
        <div className="map-overlay" style={{
          top: 60, left: '50%', transform: 'translateX(-50%)',
          padding: '8px 16px', textAlign: 'center',
          pointerEvents: 'none', border: '1px solid #a78bfa',
        }}>
          <p style={{ fontSize: 12, color: '#a78bfa', margin: 0, fontWeight: 600 }}>
            Click to set observer location for LoB
          </p>
        </div>
      )}

      {/* LoB azimuth pick hint */}
      {lobAzimuthPickingMode && (
        <div className="map-overlay" style={{
          top: 60, left: '50%', transform: 'translateX(-50%)',
          padding: '8px 16px', textAlign: 'center',
          pointerEvents: 'none', border: '1px solid #a78bfa',
        }}>
          <p style={{ fontSize: 12, color: '#a78bfa', margin: 0, fontWeight: 600 }}>
            Click a target — azimuth set from observer to that point
          </p>
        </div>
      )}

      {/* P2P hint */}
      {activeTab === 'p2p' && !rxPoint && !drawMode && (
        <div className="map-overlay" style={{
          top: '50%', left: '50%', transform: 'translate(-50%,-50%)',
          padding: '10px 16px', textAlign: 'center', pointerEvents: 'none',
          opacity: 0.9,
        }}>
          <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
            Click on the map to set the receiver location
          </p>
        </div>
      )}

      {/* Best site hint */}
      {activeTab === 'best_site' && !drawMode && (
        <div className="map-overlay" style={{
          top: 60, left: '50%', transform: 'translateX(-50%)',
          padding: '8px 14px', textAlign: 'center', pointerEvents: 'none',
          opacity: 0.9,
        }}>
          <p style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
            Click on the map to add candidate sites for best-site analysis
          </p>
        </div>
      )}

      {/* Compass rose */}
      {showCompassRose && (
        <div style={{
          position: 'absolute', bottom: 80, right: 12, zIndex: 900,
          opacity: 0.55, pointerEvents: 'none',
        }}>
          <CompassRose size={88} />
        </div>
      )}

      {/* Draw mode hint */}
      {drawMode && (
        <div className="map-overlay" style={{
          top: 60, left: '50%', transform: 'translateX(-50%)',
          padding: '8px 16px', textAlign: 'center',
          pointerEvents: 'none', border: '1px solid #a855f7',
        }}>
          <p style={{ fontSize: 12, color: '#a855f7', margin: 0, fontWeight: 600 }}>
            {drawMode === 'bounds' && `Draw Bounds — click 2 corners (${drawCount}/2)`}
            {drawMode === 'polygon' && `Draw Polygon — click points, click near first point to close (${drawCount} points)`}
            {drawMode === 'route' && `Draw Route — click waypoints, right-click to finish (${drawCount} waypoints)`}
            {drawMode === 'multipoint' && `Multipoint — click TX locations, right-click to finish (${drawCount} points)`}
            {drawMode === 'manet' && `MANET — click to place nodes (${drawCount} placed)`}
          </p>
          {(drawMode === 'route' || drawMode === 'multipoint') && drawCount >= 2 && (
            <p style={{ fontSize: 10, color: '#8b949e', margin: '2px 0 0', pointerEvents: 'none' }}>
              Right-click to finish drawing
            </p>
          )}
        </div>
      )}

      {/* Terrain-line draw mode hint */}
      {terrainLineMode && (
        <div className="map-overlay" style={{
          top: 60, left: '50%', transform: 'translateX(-50%)',
          padding: '8px 16px', textAlign: 'center',
          pointerEvents: 'none', border: '1px solid #f59e0b',
        }}>
          <p style={{ fontSize: 12, color: '#f59e0b', margin: 0, fontWeight: 600 }}>
            Click waypoints for terrain profile ({terrainLineCount} placed)
          </p>
          <p style={{ fontSize: 10, color: '#8b949e', margin: '2px 0 0' }}>
            Right-click to compute profile
          </p>
        </div>
      )}

      {/* Active draw-tool hint */}
      {toolsActive && (
        <div className="map-overlay" style={{
          top: 60, left: '50%', transform: 'translateX(-50%)',
          padding: '8px 16px', textAlign: 'center',
          pointerEvents: 'none', border: `1px solid ${toolsStrokeColor}`,
        }}>
          <p style={{ fontSize: 12, color: toolsStrokeColor, margin: 0, fontWeight: 600 }}>
            {toolHintText(toolsActive)}
          </p>
          <p style={{ fontSize: 10, color: '#8b949e', margin: '2px 0 0' }}>
            Esc or right-click to cancel/finish
          </p>
        </div>
      )}

      {/* Drag-and-drop overlay */}
      {dragOver && (
        <div style={{
          position: 'absolute', inset: 0, zIndex: 9700, pointerEvents: 'none',
          background: 'rgba(6, 214, 160, 0.10)',
          border: '3px dashed #06d6a0',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}>
          <div style={{ background: '#0d1117', border: '1px solid #06d6a0', padding: '14px 20px',
                        borderRadius: 8, color: '#06d6a0', fontSize: 14, fontWeight: 700,
                        boxShadow: '0 6px 20px rgba(0,0,0,0.7)' }}>
            Drop KML / KMZ / GeoJSON / GPX / image to import
          </div>
        </div>
      )}

      {/* Hidden file input for "Load file…" buttons */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={SUPPORTED_EXTENSIONS.join(',')}
        style={{ display: 'none' }}
        onChange={(e) => {
          const files = e.target.files
          if (files && files.length) handleImportFiles(files)
          e.target.value = ''
        }}
      />

      {/* Image bounds dialog (for ungeoreferenced raster drops) */}
      {pendingImageBounds && (
        <div style={{
          position: 'absolute', inset: 0, zIndex: 9800,
          background: 'rgba(0,0,0,0.55)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}
          onClick={() => setPendingImageBounds(null)}>
          <div onClick={e => e.stopPropagation()}
            style={{
              background: '#161b22', border: '1px solid #30363d', borderRadius: 10,
              padding: '16px 20px', minWidth: 320, maxWidth: 420, color: '#e6edf3',
              boxShadow: '0 8px 32px rgba(0,0,0,0.7)',
            }}>
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 6 }}>
              Set image bounds
            </div>
            <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 12 }}>
              {pendingImageBounds.name}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              {[
                ['north', 'North (lat)'],
                ['south', 'South (lat)'],
                ['east', 'East (lon)'],
                ['west', 'West (lon)'],
              ].map(([k, label]) => (
                <label key={k} style={{ fontSize: 11, color: '#8b949e' }}>
                  {label}
                  <input type="number" step="0.000001"
                    value={boundsForm[k]}
                    onChange={e => setBoundsForm(prev => ({ ...prev, [k]: e.target.value }))}
                    style={{ width: '100%', marginTop: 3, background: '#0d1117',
                             border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3',
                             padding: '4px 6px', fontSize: 12, outline: 'none' }} />
                </label>
              ))}
            </div>
            <div style={{ display: 'flex', gap: 6, marginTop: 14 }}>
              <button className="btn btn-ghost"
                style={{ flex: 1, fontSize: 11 }}
                onClick={() => {
                  // Use current map view bounds
                  const m = leafletRef.current
                  if (!m) return
                  const b = m.getBounds()
                  setBoundsForm({
                    north: b.getNorth().toFixed(6),
                    south: b.getSouth().toFixed(6),
                    east: b.getEast().toFixed(6),
                    west: b.getWest().toFixed(6),
                  })
                }}>
                Use map view
              </button>
              <button className="btn btn-ghost"
                style={{ flex: 1, fontSize: 11, color: '#fca5a5' }}
                onClick={() => setPendingImageBounds(null)}>
                Cancel
              </button>
              <button className="btn btn-primary"
                style={{ flex: 1, fontSize: 11 }}
                onClick={confirmImageBounds}>
                Add layer
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Import notice toast */}
      {importNotice && (
        <div style={{
          position: 'absolute', bottom: 30, left: '50%', transform: 'translateX(-50%)',
          background: '#0d1117',
          border: `1px solid ${importNotice.kind === 'error' ? '#7f1d1d' : '#06d6a0'}`,
          color: importNotice.kind === 'error' ? '#fca5a5' : '#06d6a0',
          padding: '6px 14px', borderRadius: 6, fontSize: 11, fontWeight: 600,
          zIndex: 9600, pointerEvents: 'none',
          boxShadow: '0 4px 16px rgba(0,0,0,0.6)',
        }}>
          {importNotice.text}
        </div>
      )}
    </div>
  )
}

function SearchResultRow({ result, onSelect, onSave }) {
  const rows = [
    { label: 'DD',         value: `${result.lat.toFixed(6)}, ${result.lon.toFixed(6)}` },
    { label: 'DMS',        value: formatCoordinate(result.lat, result.lon, 'latlon_dms') },
    { label: 'DDM',        value: toDDM(result.lat, result.lon) },
    { label: 'MGRS',       value: formatCoordinate(result.lat, result.lon, 'mgrs') },
    { label: 'UTM',        value: formatCoordinate(result.lat, result.lon, 'utm') },
  ]
  const copy = (v) => {
    if (navigator.clipboard?.writeText) navigator.clipboard.writeText(v).catch(() => {})
  }
  return (
    <div style={{ background: '#0d1117', borderRadius: 4, padding: '2px 4px 4px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        <button
          className="btn btn-ghost"
          style={{ flex: 1, textAlign: 'left', padding: '4px 6px', fontSize: 11, overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}
          title={result.display_name}
          onClick={onSelect}
        >
          {result.name}
        </button>
        {onSave && (
          <button
            className="btn btn-ghost"
            style={{ padding: '3px 5px', fontSize: 11, flexShrink: 0, color: '#f59e0b' }}
            title="Save this location"
            onClick={onSave}
          >
            ★
          </button>
        )}
      </div>
      <div style={{
        padding: '2px 4px 0', display: 'grid',
        gridTemplateColumns: '52px 1fr auto', gap: '2px 6px',
        fontSize: 10, alignItems: 'center',
      }}>
        {rows.map(r => (
          <SearchCoordRow key={r.label} label={r.label} value={r.value} onCopy={() => copy(r.value)} />
        ))}
      </div>
    </div>
  )
}

function SearchCoordRow({ label, value, onCopy }) {
  const [copied, setCopied] = useState(false)
  return (
    <>
      <span style={{ color: '#8b949e' }}>{label}</span>
      <span style={{
        color: '#c9d1d9', fontFamily: 'ui-monospace, monospace',
        background: '#161b22', padding: '2px 5px', borderRadius: 3,
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
      }} title={value}>{value}</span>
      <button className="btn btn-ghost"
        style={{ padding: '1px 4px', fontSize: 9, color: copied ? '#06d6a0' : '#8b949e', flexShrink: 0 }}
        onClick={() => { onCopy(); setCopied(true); setTimeout(() => setCopied(false), 1200) }}>
        {copied ? '✓' : '⧉'}
      </button>
    </>
  )
}

function CtxCoordRows({ lat, lon }) {
  const rows = [
    { label: 'DD',         value: `${lat.toFixed(6)}, ${lon.toFixed(6)}` },
    { label: 'DMS',        value: formatCoordinate(lat, lon, 'latlon_dms') },
    { label: 'DDM',        value: toDDM(lat, lon) },
    { label: 'MGRS',       value: formatCoordinate(lat, lon, 'mgrs') },
    { label: 'UTM',        value: formatCoordinate(lat, lon, 'utm') },
  ]
  const copy = (v) => {
    if (navigator.clipboard?.writeText) navigator.clipboard.writeText(v).catch(() => {})
  }
  return (
    <div style={{
      padding: '2px 10px 6px', display: 'grid',
      gridTemplateColumns: '60px 1fr auto', gap: '3px 6px',
      fontSize: 10, alignItems: 'center',
    }}>
      {rows.map(r => (
        <SearchCoordRow key={r.label} label={r.label} value={r.value} onCopy={() => copy(r.value)} />
      ))}
    </div>
  )
}

function toolHintText(tool) {
  switch (tool) {
    case 'point':       return 'Click to drop a point marker'
    case 'label':       return 'Click to place a text label'
    case 'line':        return 'Click waypoints — right-click or Esc to finish'
    case 'polygon':     return 'Click vertices — right-click or Esc to close'
    case 'rectangle':   return 'Click two opposite corners'
    case 'circle':      return 'Click centre, then radius point'
    case 'ellipse':     return 'Click centre, then long-axis end, then short-axis point'
    case 'freehand':    return 'Press & drag to sketch'
    case 'rangeRings':  return 'Click centre, then a point at first ring radius'
    case 'rb':          return 'Click start, then end of range/bearing line'
    case 'fan':         return 'Click apex, then start bearing/range, then end bearing'
    case 'geofence':    return 'Click vertices — right-click or Esc to close geofence'
    case 'milFriend':   return 'Click to drop friendly marker'
    case 'milHostile':  return 'Click to drop hostile marker'
    case 'milNeutral':  return 'Click to drop neutral marker'
    case 'milUnknown':  return 'Click to drop unknown marker'
    case 'pairing':     return 'Pairing line — click endpoint A then endpoint B; either is draggable for live range/bearing'
    case 'phaseLine':   return 'Phase Line — click waypoints, right-click/Esc to finish (you’ll be prompted for a name)'
    case 'flot':        return 'FLOT — click waypoints along the line; teeth point right of travel'
    case 'axisAdvance': return 'Axis of Advance — click waypoints from rear to objective; tip becomes the arrowhead'
    case 'boundary':    return 'Boundary — click waypoints; perpendicular tics mark each vertex'
    default:            return `Drawing: ${tool}`
  }
}
