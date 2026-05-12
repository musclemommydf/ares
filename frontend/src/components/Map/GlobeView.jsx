/**
 * GlobeView — CesiumJS 3D globe (Workstream B).
 *
 * Sibling to <MapView> (Leaflet). The app stays on the lightweight 2D Leaflet
 * map by default; switching to "3D" mounts this component. It is heavy (~30 MB
 * of Cesium), so App.jsx imports it lazily.
 *
 * Renders, on real (or flat) terrain:
 *   - **coverage**: the primary coverage GeoJSON (Point FeatureCollection w/
 *     `signal_dbm`) as either a rasterised heatmap ImageryLayer (default) or a
 *     ground-clamped colour-point cloud (`coverageMode="points"`).
 *   - **extra layers**: `extraGeojsonLayers` ({id,geojson,color}) — LineStrings
 *     as polylines, Points as small markers (coloured by signal_dbm or layer color).
 *   - **TX / RX markers** and a **LOS link line** TX→RX; if a `/terrain/profile`
 *     query shows the path is blocked, a red obstruction marker is dropped on the
 *     blocking ridge (4/3-earth clearance check).
 *   - a translucent **first-Fresnel ellipsoid** along the TX→RX path.
 *   - an **antenna lobe** at the TX shaped by the selected polar pattern — a
 *     ground footprint + a 3D outline tracing the azimuth pattern (oriented by
 *     azimuth); an omni ring for omnidirectional antennas.
 *   - **offline data packs**: if OSM-base / satellite-imagery packs are installed
 *     (`/api/v1/packs/<layer>/<id>/{z}/{x}/{y}.…`) they're added as imagery layers
 *     over their bbox; an installed **buildings** pack is rendered as extruded 3D
 *     building footprints.
 *   - **lite mode** (flat ellipsoid + 2D imagery, no terrain mesh) and
 *     `requestRenderMode` for low-power devices.
 *
 * Still to do (docs/BUILD_PLAN.md §B): Sentinel-2 / NAIP / Google 3D Tiles;
 * E-plane / full-3D polar lobe meshes; DF emitter markers + CEP ellipses on the
 * globe; KMZ import/export.
 */
import { useEffect, useRef, useState, lazy, Suspense } from 'react'
import * as Cesium from 'cesium'
import 'cesium/Build/Cesium/Widgets/widgets.css'
import { signalToColor, exportCoverageKmz } from '../../api/client'
import { useViewMode } from '../../hooks/useViewMode'
import { BASEMAPS, useMapPrefs } from './mapPrefs'
import MapSettingsCog from './MapSettingsCog'
import { geocodeNominatim } from '../../utils/geocode'
import { polarPatternGainDb, POLAR_PATTERNS } from '../../utils/polarPatterns'

const NatoSymbolPicker = lazy(() => import('./NatoSymbolPicker'))
let _msPromise = null
const getMilsymbol = () => (_msPromise ??= import('milsymbol').then((m) => m.default || m))

// No Cesium Ion (cloud) — offline-safe. We bring our own imagery/terrain.
// (undefined, not "" — "" makes Cesium attempt auth with an empty token.)
Cesium.Ion.defaultAccessToken = undefined
// vite-plugin-cesium defines CESIUM_BASE_URL at build time; surface a hint if it didn't.
try { if (typeof CESIUM_BASE_URL === 'undefined') console.warn('[GlobeView] CESIUM_BASE_URL is undefined — vite-plugin-cesium may not be active; globe tiles/workers may fail to load') } catch { /* CESIUM_BASE_URL not in scope = the same warning */ }

const DEFAULT_VIEW = { lon: -98.5, lat: 39.8, heightM: 12_000_000 }
const POINT_MODE_THRESHOLD = 8_000 // ≤ this many points ⇒ default to the point cloud, else raster
const MAX_RASTER_PX = 2048

export default function GlobeView({
  center,
  onMoveEnd,
  coverageGeoJSON = null,
  extraGeojsonLayers = [],
  tx = null,                 // { lat, lon, height_m?, altitude_m?, frequency_hz?, power_dbm? }
  rxPoint = null,            // { lat, lon, height_m? }
  antennaAzimuthDeg = null,  // boresight bearing for a directional pattern (ignored for omni)
  antennaTiltDeg = 0,
  antennaPattern = 'omni',   // polar_pattern id (see utils/polarPatterns) — shapes the lobe footprint
  minSignalDbm = -120,
  lite = false,              // flat ellipsoid + 2D imagery, no terrain mesh
  requestRenderMode = false, // render-on-change (low power)
  terrain = true,
  terrainUrl = '/api/v1/packs/terrain/active',          // quantized-mesh tileset (best-effort; falls back to flat)
  offlineImagery = false,                                // use a downloaded OSM raster pack instead of online OSM
  osmPackUrl = '/api/v1/packs/osm/active/{z}/{x}/{y}.png',
  // shared with the 2D map's ⚙ — passed straight to <MapSettingsCog>
  distUnit, setDistUnit, coordSystem, setCoordSystem,
  showCompassRose, setShowCompassRose, mapBrightness = 100, setMapBrightness,
  ul,                                                    // useUserLayers — its drawn features are rendered on the globe too
  drawMode = null,                                       // null | 'bounds' | 'polygon' | 'route' | 'multipoint' | 'manet'
  onDrawComplete,                                        // (type, data) — same callback the 2D map uses
  extraTxList = [],                                      // additional transmitters → TX markers on the globe
  geolocationGeoJSON = null,                             // DF picture: bearing wedges, Cut/Fix centroids, CAP/CEP ellipses
  gpsFix = null,                                         // {lat, lon, heading_deg?, source} — operator "you are here" marker
}) {
  const containerRef = useRef(null)
  const viewerRef = useRef(null)
  const layersRef = useRef({ coveragePoints: null, coverageRaster: null })
  const imageryLayerRef = useRef(null)   // current basemap imagery layer
  const offlineLayersRef = useRef([])    // installed osm/imagery-pack ImageryLayers
  const dsRef = useRef(null)        // Cesium.CustomDataSource for vectors/markers
  const buildingsDsRef = useRef(null)    // Cesium.CustomDataSource for the installed buildings pack
  const rulerRef = useRef({ handler: null, pts: [], ds: null })
  const kmzFileInputRef = useRef(null)
  const kmzDsRef = useRef(null)         // last-loaded KmlDataSource (replaced on each import)
  const requestRenderRef = useRef(() => {})
  const natoArmRef = useRef(null)   // last-armed NATO symbol (from NatoSymbolPicker); used by the 'nato' draw tool
  const natoBillboardsRef = useRef(new Map())   // mv_id → entity, for async-rendered NATO billboards
  const [err, setErr] = useState(null)
  const setViewMode = useViewMode((s) => s.setMode)
  // shared map prefs (same store the 2D toolbar uses) — basemap, feature colours, coverage render mode
  const basemapId = useMapPrefs((s) => s.basemapId)
  const setBasemapId = useMapPrefs((s) => s.setBasemapId)
  const mapColors = useMapPrefs((s) => s.mapColors)
  const covMode = useMapPrefs((s) => s.coverageMode)
  // toolbar UI state
  const [searchOpen, setSearchOpen] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState([])
  const [searching, setSearching] = useState(false)
  const [rulerActive, setRulerActive] = useState(false)
  const [offlinePacks, setOfflinePacks] = useState([])   // [{layer, name}] of installed imagery/buildings packs being shown
  const [drawPaletteOpen, setDrawPaletteOpen] = useState(false)
  const [natoPickerOpen, setNatoPickerOpen] = useState(false)
  const [drawTool, setDrawTool] = useState(null)   // null | point | line | polygon | rectangle | circle | ellipse | freehand | rangeRings | fan | rb | geofence | mil* | nato

  // ── create / destroy the viewer ────────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return
    let viewer
    try {
      // Create with NO base imagery — the globe then renders immediately as a
      // solid sphere (baseColor below). Imagery is added afterwards, additively,
      // so a failing tile source never leaves you staring at empty space.
      viewer = new Cesium.Viewer(containerRef.current, {
        baseLayer: false,
        terrainProvider: new Cesium.EllipsoidTerrainProvider(),
        baseLayerPicker: false, geocoder: false, homeButton: false, sceneModePicker: false,
        navigationHelpButton: false, animation: false, timeline: false, fullscreenButton: false,
        selectionIndicator: false, infoBox: false,
        ...(requestRenderMode ? { requestRenderMode: true, maximumRenderTimeChange: Infinity } : {}),
      })
    } catch (e) {
      console.error('[GlobeView] Cesium Viewer failed to initialise:', e)
      setErr(String(e?.message || e))
      return
    }
    viewerRef.current = viewer
    // make the globe visibly a globe even before/without imagery tiles
    viewer.scene.globe.show = true
    viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString('#0d2438')
    viewer.scene.globe.showGroundAtmosphere = true
    if (viewer.scene.skyAtmosphere) viewer.scene.skyAtmosphere.show = true
    requestRenderRef.current = () => { try { viewer.scene.requestRender() } catch { /* noop */ } }
    // (basemap imagery is applied by its own effect below, so it tracks the shared store)

    const ds = new Cesium.CustomDataSource('ares')
    viewer.dataSources.add(ds)
    // declutter overlapping markers/labels — they merge into a count badge when zoomed out
    try {
      ds.clustering.enabled = true
      ds.clustering.pixelRange = 36
      ds.clustering.minimumClusterSize = 2
    } catch { /* older Cesium */ }
    dsRef.current = ds
    // separate data source for the installed buildings pack (extruded footprints) —
    // it must survive the vector-overlays effect's `ds.entities.removeAll()`.
    const bds = new Cesium.CustomDataSource('ares-buildings')
    viewer.dataSources.add(bds)
    buildingsDsRef.current = bds

    // terrain — if an installed terrain pack reports cesium_ready, drive a Cesium
    // CustomHeightmapTerrainProvider whose callback fetches per-tile int16 height
    // grids from /api/v1/terrain/heightmap/active (sampled on the fly from the
    // pack's SRTM .hgt files). Tiles outside the pack's bbox return a flat grid
    // (no network), so there's no all-globe request storm. No pack ⇒ stay flat.
    if (terrain && !lite && Cesium.CustomHeightmapTerrainProvider) {
      fetch('/api/v1/packs?layer=terrain')
        .then((r) => (r.ok ? r.json() : { packs: [] }))
        .then((d) => {
          const pack = (d.packs || []).find((p) => p && (p.cesium_ready || p.format === 'hgt') && Array.isArray(p.bbox) && p.bbox.length === 4)
          if (!pack || !viewerRef.current || viewerRef.current.isDestroyed()) return
          const [pw, ps, pe, pn] = pack.bbox
          const W = 65, H = 65
          const scheme = new Cesium.GeographicTilingScheme()
          const flat = () => new Int16Array(W * H)   // never return undefined → no Cesium retry storm
          const provider = new Cesium.CustomHeightmapTerrainProvider({
            width: W, height: H, tilingScheme: scheme,
            callback: (x, y, level) => {
              let rect
              try { rect = scheme.tileXYToRectangle(x, y, level) } catch { return flat() }
              const w = Cesium.Math.toDegrees(rect.west), s = Cesium.Math.toDegrees(rect.south)
              const e = Cesium.Math.toDegrees(rect.east), n = Cesium.Math.toDegrees(rect.north)
              if (!(e > pw && w < pe && n > ps && s < pn)) return flat()  // tile outside the pack → flat
              return fetch(`/api/v1/terrain/heightmap/active?west=${w}&south=${s}&east=${e}&north=${n}&w=${W}&h=${H}`)
                .then((r2) => (r2.ok ? r2.arrayBuffer() : null))
                .then((buf) => (buf ? new Int16Array(buf) : flat()))
                .catch(() => flat())
            },
          })
          viewerRef.current.terrainProvider = provider
          viewerRef.current.scene.globe.depthTestAgainstTerrain = true
          requestRenderRef.current()
        })
        .catch(() => { /* stay on the flat ellipsoid */ })
    }
    // depth-test against terrain only once we actually have terrain; with the flat
    // ellipsoid + ground-clamped markers it can hide things, so leave it off here.
    viewer.scene.globe.depthTestAgainstTerrain = false

    flyTo(viewer, center, false)
    const reportMove = () => {
      if (!onMoveEnd) return
      const carto = viewer.camera.positionCartographic
      onMoveEnd({
        lat: Cesium.Math.toDegrees(carto.latitude), lon: Cesium.Math.toDegrees(carto.longitude),
        heightM: carto.height, heading: Cesium.Math.toDegrees(viewer.camera.heading),
        pitch: Cesium.Math.toDegrees(viewer.camera.pitch),
      })
    }
    viewer.camera.moveEnd.addEventListener(reportMove)
    return () => {
      viewer.camera.moveEnd.removeEventListener(reportMove)
      try { rulerRef.current.handler?.destroy() } catch { /* noop */ }
      if (!viewer.isDestroyed()) viewer.destroy()
      viewerRef.current = null
      layersRef.current = { coveragePoints: null, coverageRaster: null }
      imageryLayerRef.current = null
      offlineLayersRef.current = []
      dsRef.current = null
      buildingsDsRef.current = null
      rulerRef.current = { handler: null, pts: [], ds: null }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── basemap imagery (tracks the shared store; same choices as the 2D map) ──
  useEffect(() => {
    const v = viewerRef.current
    if (!v || v.isDestroyed()) return
    try {
      let provider
      if (offlineImagery) {
        provider = new Cesium.UrlTemplateImageryProvider({ url: osmPackUrl, maximumLevel: 19 })
      } else {
        const bm = BASEMAPS[basemapId] || BASEMAPS.dark
        provider = new Cesium.UrlTemplateImageryProvider({
          url: bm.url.replace('{r}', ''),
          subdomains: bm.subdomains ? bm.subdomains.split('') : undefined,
          maximumLevel: 19,
          credit: bm.attribution,
        })
      }
      const layer = v.imageryLayers.addImageryProvider(provider)
      try { layer.brightness = (mapBrightness ?? 100) / 100 } catch { /* noop */ }
      // keep coverage raster (added later, on top) above the basemap; lower the new basemap to the bottom
      v.imageryLayers.lowerToBottom(layer)
      if (imageryLayerRef.current) { try { v.imageryLayers.remove(imageryLayerRef.current) } catch { /* noop */ } }
      imageryLayerRef.current = layer
      requestRenderRef.current()
    } catch (e) {
      console.warn('[GlobeView] basemap imagery failed:', e)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [basemapId, offlineImagery])

  // ── map brightness (applies the ⚙'s slider to the Cesium basemap layer) ──
  useEffect(() => {
    const l = imageryLayerRef.current
    if (l) { try { l.brightness = (mapBrightness ?? 100) / 100; requestRenderRef.current() } catch { /* noop */ } }
  }, [mapBrightness])

  // ── offline data packs: imagery (osm-base / satellite) layers + extruded buildings ──
  // Detected once on mount. Each XYZ-raster pack becomes a UrlTemplateImageryProvider
  // clamped to the pack's bbox (so Cesium never requests a tile the pack lacks); a
  // buildings pack becomes extruded 3D footprints. Packs downloaded mid-session show
  // after a 2D⇄3D toggle. Silent no-op when no packs are installed (offline-safe).
  useEffect(() => {
    let cancelled = false
    const v = viewerRef.current
    if (!v || v.isDestroyed()) return
    const loaded = []
    const addRasterPacks = async (layer) => {
      let packs = []
      try {
        const r = await fetch(`/api/v1/packs?layer=${layer}`)
        packs = r.ok ? (await r.json()).packs || [] : []
      } catch { return }
      for (const p of packs) {
        if (cancelled || !viewerRef.current || viewerRef.current.isDestroyed()) return
        if (!p || !Array.isArray(p.bbox) || p.bbox.length !== 4) continue
        const tpl = p.tile_template || '{z}/{x}/{y}.png'
        try {
          const provider = new Cesium.UrlTemplateImageryProvider({
            url: `/api/v1/packs/${layer}/${encodeURIComponent(p.id)}/${tpl}`,
            maximumLevel: p.zoom_max ?? 18,
            rectangle: Cesium.Rectangle.fromDegrees(p.bbox[0], p.bbox[1], p.bbox[2], p.bbox[3]),
            credit: layer === 'imagery' ? 'Offline imagery pack' : 'Offline OSM pack',
          })
          const lyr = viewerRef.current.imageryLayers.addImageryProvider(provider)
          // keep these above the basemap but below the coverage raster (added later)
          if (layersRef.current.coverageRaster) {
            try { viewerRef.current.imageryLayers.raiseToTop(layersRef.current.coverageRaster) } catch { /* noop */ }
          }
          offlineLayersRef.current.push(lyr)
          loaded.push({ layer, name: p.name || p.id })
        } catch { /* skip a bad pack */ }
      }
    }
    const addBuildings = async () => {
      const bds = buildingsDsRef.current
      if (!bds) return
      let packs = []
      try {
        const r = await fetch('/api/v1/packs?layer=buildings')
        packs = r.ok ? (await r.json()).packs || [] : []
      } catch { return }
      const MAX = 8000
      let n = 0
      for (const p of packs) {
        if (cancelled || n >= MAX) break
        const file = p.file || 'buildings.geojson'
        let fc = null
        try {
          const r = await fetch(`/api/v1/packs/buildings/${encodeURIComponent(p.id)}/${file}`)
          fc = r.ok ? await r.json() : null
        } catch { continue }
        for (const f of fc?.features || []) {
          if (cancelled || n >= MAX) break
          if (f?.geometry?.type !== 'Polygon') continue
          const ring = f.geometry.coordinates?.[0]
          if (!Array.isArray(ring) || ring.length < 4) continue
          let hm = Number(f.properties?.height_m)
          if (!isFinite(hm) || hm <= 0) hm = 8
          try {
            bds.entities.add({ polygon: {
              hierarchy: Cesium.Cartesian3.fromDegreesArray(ring.flat()),
              extrudedHeight: hm, extrudedHeightReference: Cesium.HeightReference.RELATIVE_TO_GROUND,
              heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
              material: Cesium.Color.fromCssColorString('#c9b89a').withAlpha(0.85),
              outline: true, outlineColor: Cesium.Color.fromCssColorString('#5b513c').withAlpha(0.7),
              closeTop: true, closeBottom: false } })
            n++
          } catch { /* skip a malformed footprint */ }
        }
        if (n) loaded.push({ layer: 'buildings', name: `${p.name || p.id} (${n} footprints${n >= MAX ? '+, capped' : ''})` })
      }
    }
    Promise.all([addRasterPacks('imagery'), addRasterPacks('osm'), addBuildings()]).then(() => {
      if (!cancelled) { setOfflinePacks(loaded); requestRenderRef.current() }
    })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── recenter ───────────────────────────────────────────────────────────────
  useEffect(() => {
    const v = viewerRef.current
    if (v && center) { flyTo(v, center, true); requestRenderRef.current() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [center?.lat, center?.lon, center?.zoom])

  // ── ruler (basic 2-click distance/bearing on the globe) ────────────────────
  useEffect(() => {
    const v = viewerRef.current
    if (!v || v.isDestroyed()) return
    if (!rulerActive) {
      try { rulerRef.current.handler?.destroy() } catch { /* noop */ }
      if (rulerRef.current.ds) { try { v.dataSources.remove(rulerRef.current.ds) } catch { /* noop */ } }
      rulerRef.current = { handler: null, pts: [], ds: null }
      v.canvas.style.cursor = ''
      requestRenderRef.current()
      return
    }
    const ds = new Cesium.CustomDataSource('ares-ruler')
    v.dataSources.add(ds)
    v.canvas.style.cursor = 'crosshair'
    const handler = new Cesium.ScreenSpaceEventHandler(v.canvas)
    rulerRef.current = { handler, pts: [], ds }
    handler.setInputAction((click) => {
      const ray = v.camera.getPickRay(click.position)
      const pos = ray && v.scene.globe.pick(ray, v.scene)
      if (!pos) return
      const carto = Cesium.Cartographic.fromCartesian(pos)
      rulerRef.current.pts.push({ lat: Cesium.Math.toDegrees(carto.latitude), lon: Cesium.Math.toDegrees(carto.longitude), c: pos })
      ds.entities.add({ position: pos, point: { pixelSize: 7, color: Cesium.Color.fromCssColorString(mapColors.ruler || '#f59e0b'),
                                                outlineColor: Cesium.Color.BLACK, outlineWidth: 1, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND } })
      const pts = rulerRef.current.pts
      if (pts.length === 2) {
        const a = pts[0], b = pts[1]
        const g = new Cesium.EllipsoidGeodesic(Cesium.Cartographic.fromDegrees(a.lon, a.lat), Cesium.Cartographic.fromDegrees(b.lon, b.lat))
        const dist = g.surfaceDistance
        const hdg = (Cesium.Math.toDegrees(g.startHeading) + 360) % 360
        const mid = g.interpolateUsingFraction(0.5)
        ds.entities.add({ polyline: { positions: [a.c, b.c], width: 2, clampToGround: true,
                                      material: Cesium.Color.fromCssColorString(mapColors.ruler || '#f59e0b') } })
        ds.entities.add({ position: Cesium.Cartesian3.fromRadians(mid.longitude, mid.latitude, 0),
          label: { text: `${dist >= 1000 ? (dist / 1000).toFixed(2) + ' km' : dist.toFixed(0) + ' m'}  ·  ${hdg.toFixed(0)}°`,
                   font: '11px sans-serif', fillColor: Cesium.Color.WHITE, showBackground: true,
                   backgroundColor: Cesium.Color.fromCssColorString('#161b22').withAlpha(0.85),
                   pixelOffset: new Cesium.Cartesian2(0, -14), heightReference: Cesium.HeightReference.CLAMP_TO_GROUND } })
        rulerRef.current.pts = []   // next pair starts a new ruler
      }
      requestRenderRef.current()
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK)
    return () => {
      try { handler.destroy() } catch { /* noop */ }
      try { v.dataSources.remove(ds) } catch { /* noop */ }
      v.canvas.style.cursor = ''
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rulerActive])

  // ── analysis-draw tools on the globe (bounds / polygon / route / multipoint / manet) ──
  // Mirrors the 2D map: same drawMode values, same onDrawComplete(type, data) shapes,
  // so "Draw Bounds", "Best-Site Polygon", "Draw Route", etc. work in 3D too.
  useEffect(() => {
    const v = viewerRef.current
    if (!v || v.isDestroyed() || !drawMode || !['bounds', 'polygon', 'route', 'multipoint', 'manet'].includes(drawMode)) return
    const ds = new Cesium.CustomDataSource('ares-draw')
    v.dataSources.add(ds)
    v.canvas.style.cursor = 'crosshair'
    const handler = new Cesium.ScreenSpaceEventHandler(v.canvas)
    const pts = []   // [{ lat, lon, c }]
    let preview = null
    const C = Cesium.Color.fromCssColorString(mapColors?.draw || '#a855f7')
    const pickLL = (winPos) => {
      const ray = v.camera.getPickRay(winPos)
      const c = ray && v.scene.globe.pick(ray, v.scene)
      if (!c) return null
      const carto = Cesium.Cartographic.fromCartesian(c)
      return { lat: Cesium.Math.toDegrees(carto.latitude), lon: Cesium.Math.toDegrees(carto.longitude), c }
    }
    const clearPreview = () => { if (preview) { try { ds.entities.remove(preview) } catch { /* noop */ } preview = null } }
    const previewPolyline = (extraC) => {
      clearPreview()
      const positions = pts.map(p => p.c).concat(extraC ? [extraC] : [])
      if (positions.length >= 2) preview = ds.entities.add({ polyline: { positions, width: 2, material: C, clampToGround: true } })
    }
    const previewRect = (a, b) => {
      clearPreview()
      if (!a || !b) return
      preview = ds.entities.add({ rectangle: { coordinates: Cesium.Rectangle.fromDegrees(Math.min(a.lon, b.lon), Math.min(a.lat, b.lat), Math.max(a.lon, b.lon), Math.max(a.lat, b.lat)),
        material: C.withAlpha(0.18), outline: true, outlineColor: C } })
    }
    const dotAt = (p) => ds.entities.add({ position: p.c, point: { pixelSize: 7, color: C, outlineColor: Cesium.Color.BLACK, outlineWidth: 1, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND } })
    const reset = () => { pts.length = 0; ds.entities.removeAll(); preview = null }
    const toLL = (arr) => arr.map(p => ({ lat: p.lat, lon: p.lon }))

    handler.setInputAction((click) => {
      const ll = pickLL(click.position); if (!ll) return
      pts.push(ll); dotAt(ll)
      if (drawMode === 'bounds') {
        if (pts.length === 2) {
          const a = pts[0], b = pts[1]
          onDrawComplete?.('bounds', { north: Math.max(a.lat, b.lat), south: Math.min(a.lat, b.lat), east: Math.max(a.lon, b.lon), west: Math.min(a.lon, b.lon) })
          reset()
        }
      } else if (drawMode === 'route' || drawMode === 'multipoint') {
        onDrawComplete?.(drawMode, toLL(pts))
      } else if (drawMode === 'manet') {
        onDrawComplete?.('manet', { lat: ll.lat, lon: ll.lon })
        pts.length = 0
      }
      requestRenderRef.current()
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK)

    handler.setInputAction((m) => {
      const ll = pickLL(m.endPosition); if (!ll) return
      if (drawMode === 'bounds' && pts.length === 1) previewRect(pts[0], ll)
      else if ((drawMode === 'polygon' || drawMode === 'route') && pts.length >= 1) previewPolyline(ll.c)
      requestRenderRef.current()
    }, Cesium.ScreenSpaceEventType.MOUSE_MOVE)

    const finish = () => {
      if (drawMode === 'polygon' && pts.length >= 3) { onDrawComplete?.('polygon', toLL(pts)); reset() }
      else if ((drawMode === 'route' || drawMode === 'multipoint') && pts.length >= 1) { onDrawComplete?.(drawMode + '_finish', toLL(pts)); reset() }
      requestRenderRef.current()
    }
    handler.setInputAction(finish, Cesium.ScreenSpaceEventType.RIGHT_CLICK)

    return () => {
      try { handler.destroy() } catch { /* noop */ }
      try { v.dataSources.remove(ds) } catch { /* noop */ }
      v.canvas.style.cursor = ''
      requestRenderRef.current()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drawMode])

  // ── ✎ annotation draw on the globe — full DrawTools tool set ──────────────
  // Produces GeoJSON Features (with mv_kind/style props) → ul.addGlobeDrawing →
  // both maps render them, so drawings carry over 2D ⇄ 3D.
  useEffect(() => {
    const v = viewerRef.current
    if (!v || v.isDestroyed() || !drawTool) return
    const tool = drawTool
    const ds = new Cesium.CustomDataSource('ares-anno')
    v.dataSources.add(ds)
    v.canvas.style.cursor = 'crosshair'
    const handler = new Cesium.ScreenSpaceEventHandler(v.canvas)
    const ctrl = v.scene.screenSpaceCameraController
    const C = mapColors?.draw || '#a855f7'
    const CC = Cesium.Color.fromCssColorString(C)
    const MILC = { milFriend: '#3b82f6', milHostile: '#ef4444', milNeutral: '#22c55e', milUnknown: '#facc15' }
    const M_DEG = 111320
    const mpdLon = (lat) => M_DEG * Math.cos(lat * Math.PI / 180)
    const destPt = (lat, lon, brgDeg, distM) => {
      const b = brgDeg * Math.PI / 180
      return [lon + (distM * Math.sin(b)) / mpdLon(lat), lat + (distM * Math.cos(b)) / M_DEG]
    }
    const circleRing = (lat, lon, rM, n = 64) => { const r = []; for (let i = 0; i <= n; i++) r.push(destPt(lat, lon, 360 * i / n, rM)); return r }
    const ellipseRing = (lat, lon, aM, bM, rotDeg, n = 64) => {
      const rot = rotDeg * Math.PI / 180, r = []
      for (let i = 0; i <= n; i++) { const t = 2 * Math.PI * i / n, xl = aM * Math.cos(t), yl = bM * Math.sin(t)
        const xe = xl * Math.sin(rot) + yl * Math.cos(rot), yn = xl * Math.cos(rot) - yl * Math.sin(rot)
        r.push([lon + xe / mpdLon(lat), lat + yn / M_DEG]) }
      return r
    }
    const arcRing = (lat, lon, rM, a0, a1, n = 32) => { const r = [[lon, lat]]; for (let i = 0; i <= n; i++) r.push(destPt(lat, lon, a0 + (a1 - a0) * i / n, rM)); r.push([lon, lat]); return r }
    const geodist = (a, b) => { const g = new Cesium.EllipsoidGeodesic(Cesium.Cartographic.fromDegrees(a.lon, a.lat), Cesium.Cartographic.fromDegrees(b.lon, b.lat)); return { d: g.surfaceDistance, brg: (Cesium.Math.toDegrees(g.startHeading) + 360) % 360 } }
    const pickLL = (winPos) => { const ray = v.camera.getPickRay(winPos); const c = ray && v.scene.globe.pick(ray, v.scene); if (!c) return null; const carto = Cesium.Cartographic.fromCartesian(c); return { lat: Cesium.Math.toDegrees(carto.latitude), lon: Cesium.Math.toDegrees(carto.longitude), c } }
    const cart = ([lon, lat]) => Cesium.Cartesian3.fromDegrees(lon, lat)
    const dot = (p) => ds.entities.add({ position: p.c, point: { pixelSize: 6, color: CC, outlineColor: Cesium.Color.BLACK, outlineWidth: 1, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND } })
    const pts = []
    let preview = null, dragging = false
    const clearPv = () => { if (preview) { try { ds.entities.remove(preview) } catch { /* noop */ } preview = null } }
    const pvLine = (extra) => { clearPv(); const ps = pts.map(p => p.c).concat(extra ? [extra] : []); if (ps.length >= 2) preview = ds.entities.add({ polyline: { positions: ps, width: 2, material: CC, clampToGround: true } }) }
    const pvRing = (ring) => { clearPv(); if (ring && ring.length >= 2) preview = ds.entities.add({ polyline: { positions: ring.map(cart), width: 2, material: CC, clampToGround: true } }) }
    const pvRect = (a, b) => { clearPv(); if (!a || !b) return; preview = ds.entities.add({ rectangle: { coordinates: Cesium.Rectangle.fromDegrees(Math.min(a.lon, b.lon), Math.min(a.lat, b.lat), Math.max(a.lon, b.lon), Math.max(a.lat, b.lat)), material: CC.withAlpha(0.18), outline: true, outlineColor: CC } }) }
    const reset = () => { pts.length = 0; ds.entities.removeAll(); preview = null }
    const uid = () => 'g_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
    const emit = (geometry, extra = {}) => { ul?.addGlobeDrawing?.({ type: 'Feature', geometry, properties: { mv_kind: tool, mv_id: uid(), stroke: C, fill: C, ...extra } }); reset(); setDrawTool(null); v.scene.requestRender() }
    const closeRing = (arr) => { const r = arr.map(p => [p.lon, p.lat]); if (r.length && (r[0][0] !== r[r.length - 1][0] || r[0][1] !== r[r.length - 1][1])) r.push(r[0]); return r }
    const isPointTool = tool === 'point' || tool === 'nato' || tool.startsWith('mil')

    handler.setInputAction((click) => {
      const ll = pickLL(click.position); if (!ll) return
      if (isPointTool) {
        if (tool === 'nato') emit({ type: 'Point', coordinates: [ll.lon, ll.lat] }, { natoArm: natoArmRef.current || null })
        else emit({ type: 'Point', coordinates: [ll.lon, ll.lat] }, tool.startsWith('mil') ? { affiliation: tool.replace('mil', '').toLowerCase() } : {})
        return
      }
      pts.push(ll); dot(ll)
      if (tool === 'rectangle' && pts.length === 2) {
        const a = pts[0], b = pts[1], w = Math.min(a.lon, b.lon), e = Math.max(a.lon, b.lon), s = Math.min(a.lat, b.lat), n = Math.max(a.lat, b.lat)
        emit({ type: 'Polygon', coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] })
      } else if ((tool === 'circle' || tool === 'rangeRings') && pts.length === 2) {
        const { d } = geodist(pts[0], pts[1])
        if (tool === 'circle') emit({ type: 'Polygon', coordinates: [circleRing(pts[0].lat, pts[0].lon, d)] }, { radius_m: Math.round(d) })
        else emit({ type: 'MultiLineString', coordinates: [1, 2, 3, 4].map(k => circleRing(pts[0].lat, pts[0].lon, d * k)) }, { spacing_m: Math.round(d), rings: 4 })
      } else if (tool === 'ellipse' && pts.length === 3) {
        const { d: aM, brg } = geodist(pts[0], pts[1]); const { d: bM } = geodist(pts[0], pts[2])
        emit({ type: 'Polygon', coordinates: [ellipseRing(pts[0].lat, pts[0].lon, aM, bM, brg)] }, { a_m: Math.round(aM), b_m: Math.round(bM), rot_deg: Math.round(brg) })
      } else if (tool === 'fan' && pts.length === 3) {
        const { d: rM, brg: a0 } = geodist(pts[0], pts[1]); const { brg: a1 } = geodist(pts[0], pts[2])
        emit({ type: 'Polygon', coordinates: [arcRing(pts[0].lat, pts[0].lon, rM, a0, a1)] }, { radius_m: Math.round(rM), start_deg: Math.round(a0), end_deg: Math.round(a1) })
      } else if (tool === 'rb' && pts.length === 2) {
        const { d, brg } = geodist(pts[0], pts[1])
        emit({ type: 'LineString', coordinates: pts.map(p => [p.lon, p.lat]) }, { distance_m: Math.round(d), bearing_deg: Math.round(brg) })
      }
      v.scene.requestRender()
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK)

    handler.setInputAction((m) => {
      const ll = pickLL(m.endPosition); if (!ll) return
      if (tool === 'freehand' && dragging) { pts.push(ll); pvLine() }
      else if (tool === 'rectangle' && pts.length === 1) pvRect(pts[0], ll)
      else if ((tool === 'line' || tool === 'polygon' || tool === 'geofence' || tool === 'rb') && pts.length >= 1) pvLine(ll.c)
      else if ((tool === 'circle' || tool === 'rangeRings') && pts.length === 1) { const { d } = geodist(pts[0], ll); pvRing(circleRing(pts[0].lat, pts[0].lon, d)) }
      else if (tool === 'ellipse' && pts.length >= 1) { const { d: aM, brg } = geodist(pts[0], pts[pts.length === 1 ? 0 : 1].lat !== undefined && pts.length > 1 ? pts[1] : ll); pvRing(ellipseRing(pts[0].lat, pts[0].lon, Math.max(1, aM), Math.max(1, aM * 0.55), brg)) }
      else if (tool === 'fan' && pts.length === 2) { const { d: rM, brg: a0 } = geodist(pts[0], pts[1]); const { brg: a1 } = geodist(pts[0], ll); pvRing(arcRing(pts[0].lat, pts[0].lon, rM, a0, a1)) }
      v.scene.requestRender()
    }, Cesium.ScreenSpaceEventType.MOUSE_MOVE)

    if (tool === 'freehand') {
      ctrl.enableRotate = false; ctrl.enableTranslate = false; ctrl.enableTilt = false; ctrl.enableLook = false
      handler.setInputAction((d) => { const ll = pickLL(d.position); if (ll) { dragging = true; pts.length = 0; pts.push(ll) } }, Cesium.ScreenSpaceEventType.LEFT_DOWN)
      handler.setInputAction(() => { if (dragging && pts.length >= 2) emit({ type: 'LineString', coordinates: pts.map(p => [p.lon, p.lat]) }); dragging = false }, Cesium.ScreenSpaceEventType.LEFT_UP)
    } else {
      handler.setInputAction(() => {
        if ((tool === 'line' || tool === 'rb') && pts.length >= 2) emit({ type: 'LineString', coordinates: pts.map(p => [p.lon, p.lat]) }, tool === 'rb' && pts.length === 2 ? (() => { const { d, brg } = geodist(pts[0], pts[1]); return { distance_m: Math.round(d), bearing_deg: Math.round(brg) } })() : {})
        else if ((tool === 'polygon' || tool === 'geofence') && pts.length >= 3) emit({ type: 'Polygon', coordinates: [closeRing(pts)] }, tool === 'geofence' ? { geofence: true } : {})
      }, Cesium.ScreenSpaceEventType.RIGHT_CLICK)
    }

    return () => {
      try { handler.destroy() } catch { /* noop */ }
      try { v.dataSources.remove(ds) } catch { /* noop */ }
      v.canvas.style.cursor = ''
      ctrl.enableRotate = true; ctrl.enableTranslate = true; ctrl.enableTilt = true; ctrl.enableLook = true
      v.scene.requestRender()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drawTool])

  // ── primary coverage ───────────────────────────────────────────────────────
  useEffect(() => {
    const v = viewerRef.current
    if (!v || v.isDestroyed()) return
    // clear prior
    if (layersRef.current.coveragePoints && !layersRef.current.coveragePoints.isDestroyed())
      v.scene.primitives.remove(layersRef.current.coveragePoints)
    if (layersRef.current.coverageRaster) v.imageryLayers.remove(layersRef.current.coverageRaster)
    layersRef.current.coveragePoints = null
    layersRef.current.coverageRaster = null

    const feats = (coverageGeoJSON?.features || []).filter(
      (f) => f?.geometry?.type === 'Point' && f.properties?.covered !== false,
    )
    if (feats.length === 0) { requestRenderRef.current(); return }

    const mode = covMode === 'auto' ? (feats.length <= POINT_MODE_THRESHOLD ? 'points' : 'raster') : covMode
    let bs = null
    if (mode === 'points') {
      const pts = new Cesium.PointPrimitiveCollection()
      const stride = Math.max(1, Math.ceil(feats.length / 60_000))
      const carts = []
      for (let i = 0; i < feats.length; i += stride) {
        const [lon, lat] = feats[i].geometry.coordinates
        if (typeof lon !== 'number' || typeof lat !== 'number') continue
        const [r, g, b, a] = signalToColor(feats[i].properties?.signal_dbm ?? minSignalDbm, minSignalDbm)
        const p = Cesium.Cartesian3.fromDegrees(lon, lat)
        carts.push(p)
        pts.add({ position: p, color: new Cesium.Color(r / 255, g / 255, b / 255, a / 255),
                  pixelSize: 6, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND })
      }
      v.scene.primitives.add(pts)
      layersRef.current.coveragePoints = pts
      try { bs = Cesium.BoundingSphere.fromPoints(carts) } catch { /* ignore */ }
    } else {
      const raster = rasterizeCoverage(feats, minSignalDbm)
      if (raster) {
        const { dataUrl, rect } = raster
        const provider = new Cesium.SingleTileImageryProvider({ url: dataUrl, rectangle: rect })
        layersRef.current.coverageRaster = v.imageryLayers.addImageryProvider(provider)
        try { bs = Cesium.BoundingSphere.fromRectangle3D(rect) } catch { /* ignore */ }
      }
    }
    if (bs && bs.radius > 0) v.camera.flyToBoundingSphere(bs, { duration: 1.0 })
    requestRenderRef.current()
  }, [coverageGeoJSON, minSignalDbm, covMode])

  // ── vector overlays: extra layers + TX/RX + LOS + Fresnel + antenna lobe ───
  useEffect(() => {
    const v = viewerRef.current
    const ds = dsRef.current
    if (!v || v.isDestroyed() || !ds) return
    ds.entities.removeAll()
    let cancelled = false   // guards async LOS-obstruction adds against a re-run

    // extra geojson layers
    for (const layer of extraGeojsonLayers || []) {
      const color = Cesium.Color.fromCssColorString(layer?.color || '#00b4d8')
      for (const f of layer?.geojson?.features || []) {
        const t = f?.geometry?.type
        if (t === 'LineString') {
          ds.entities.add({
            polyline: { positions: f.geometry.coordinates.map(([lo, la]) => Cesium.Cartesian3.fromDegrees(lo, la)),
                        width: 2.5, material: color, clampToGround: true },
          })
        } else if (t === 'Point') {
          const [lo, la] = f.geometry.coordinates
          const dbm = f.properties?.signal_dbm
          const c = typeof dbm === 'number'
            ? (() => { const [r, g, b, a] = signalToColor(dbm, minSignalDbm); return new Cesium.Color(r / 255, g / 255, b / 255, a / 255) })()
            : color
          ds.entities.add({ position: Cesium.Cartesian3.fromDegrees(lo, la),
                            point: { pixelSize: 5, color: c, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND } })
        }
      }
    }

    // imported layers (KML / KMZ / GeoJSON / GPX dragged onto the *2D* map, or the
    // 📥 globe button) — render them here too so they persist across 2D⇄3D.
    for (const layer of (ul?.layers || [])) {
      if (!layer || layer.visible === false || layer.kind !== 'geojson' || !layer.geojson) continue
      const layerC = Cesium.Color.fromCssColorString(layer.color || '#22d3ee')
      const layerFill = layerC.withAlpha(Math.max(0.08, (layer.opacity ?? 0.7) * 0.25))
      const toDegArr = (ring) => Cesium.Cartesian3.fromDegreesArray(ring.flat())
      for (const f of (layer.geojson.features || [])) {
        const g = f?.geometry; if (!g) continue
        const p = f.properties || {}
        // honour per-feature colour (UAS / Remote-ID features carry their own); label uas_glx/rid_glx points
        const ug = p.uas_glx || p.rid_glx
        const lc = (ug || p.color) ? Cesium.Color.fromCssColorString(p.color || layer.color || '#22d3ee') : layerC
        const fillc = (ug || p.color) ? lc.withAlpha(0.16) : layerFill
        const ptSize = (ug === 'drone' || ug === 'platform') ? 10 : (ug ? 7 : 7)
        const ugLabel = ug
          ? { text: String(p.serial || p.call_sign || (ug.charAt(0).toUpperCase() + ug.slice(1))),
              font: '11px sans-serif', fillColor: Cesium.Color.WHITE, showBackground: true,
              backgroundColor: Cesium.Color.fromCssColorString('#161b22').withAlpha(0.85),
              pixelOffset: new Cesium.Cartesian2(0, -14), heightReference: Cesium.HeightReference.CLAMP_TO_GROUND }
          : undefined
        try {
          if (g.type === 'Point') {
            const [lo, la, h] = g.coordinates
            ds.entities.add({ position: Cesium.Cartesian3.fromDegrees(lo, la, typeof h === 'number' ? h : 0),
              point: { pixelSize: ptSize, color: lc, outlineColor: Cesium.Color.BLACK, outlineWidth: 1,
                       heightReference: typeof h === 'number' ? Cesium.HeightReference.NONE : Cesium.HeightReference.CLAMP_TO_GROUND },
              label: ugLabel || (p.name ? { text: String(p.name), font: '11px sans-serif', fillColor: Cesium.Color.WHITE,
                pixelOffset: new Cesium.Cartesian2(0, -14), style: Cesium.LabelStyle.FILL_AND_OUTLINE, outlineWidth: 2,
                heightReference: Cesium.HeightReference.CLAMP_TO_GROUND } : undefined) })
          } else if (g.type === 'LineString') {
            ds.entities.add({ polyline: { positions: g.coordinates.map(([lo, la]) => Cesium.Cartesian3.fromDegrees(lo, la)),
              width: 2.5, material: lc, clampToGround: true } })
          } else if (g.type === 'MultiLineString') {
            for (const line of g.coordinates) ds.entities.add({ polyline: { positions: line.map(([lo, la]) => Cesium.Cartesian3.fromDegrees(lo, la)), width: 2.5, material: lc, clampToGround: true } })
          } else if (g.type === 'Polygon') {
            ds.entities.add({ polygon: { hierarchy: toDegArr(g.coordinates[0]), material: fillc, outline: true, outlineColor: lc, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND } })
          } else if (g.type === 'MultiPolygon') {
            for (const poly of g.coordinates) ds.entities.add({ polygon: { hierarchy: toDegArr(poly[0]), material: fillc, outline: true, outlineColor: lc, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND } })
          }
        } catch { /* skip a malformed imported feature */ }
      }
    }

    // user-drawn features (annotations from the 2D map's ✎ tools) — render here so
    // anything drawn in 2D shows on the globe too.
    const drawColor = mapColors?.draw || '#a855f7'
    for (const f of [...(ul?.drawnGeoJSON?.features || []), ...(ul?.globeDrawnGeoJSON?.features || [])]) {
      const t = f?.geometry?.type
      const props = f?.properties || {}
      const stroke = Cesium.Color.fromCssColorString(props.stroke || props['stroke-color'] || drawColor)
      const fill = Cesium.Color.fromCssColorString(props.fill || props['fill-color'] || props.stroke || drawColor).withAlpha(0.2)
      const toDeg = (ring) => ring.map(([lo, la]) => Cesium.Cartesian3.fromDegrees(lo, la))
      try {
        if (t === 'Point') {
          const [lo, la] = f.geometry.coordinates
          const pos = Cesium.Cartesian3.fromDegrees(lo, la)
          const MILC = { milFriend: '#3b82f6', milHostile: '#ef4444', milNeutral: '#22c55e', milUnknown: '#facc15' }
          const MILL = { milFriend: 'F', milHostile: 'H', milNeutral: 'N', milUnknown: '?' }
          if (props.mv_kind === 'nato') {
            const arm = props.natoArm
            const sidc = typeof arm === 'string' ? arm : (arm?.sidc || arm?.code || arm?.sic || null)
            if (sidc) {
              getMilsymbol().then((ms) => {
                if (!viewerRef.current || viewerRef.current.isDestroyed()) return
                try { ds.entities.add({ position: pos, billboard: { image: new ms.Symbol(String(sidc), { size: 30 }).asCanvas(), verticalOrigin: Cesium.VerticalOrigin.CENTER, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY } }); requestRenderRef.current() } catch { /* bad SIDC */ }
              }).catch(() => { /* milsymbol unavailable */ })
            }
            ds.entities.add({ position: pos, point: { pixelSize: 6, color: stroke, outlineColor: Cesium.Color.BLACK, outlineWidth: 1, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND } })
          } else if (props.mv_kind && props.mv_kind.startsWith('mil')) {
            const c = Cesium.Color.fromCssColorString(MILC[props.mv_kind] || (props.stroke || drawColor))
            ds.entities.add({ position: pos, point: { pixelSize: 12, color: c, outlineColor: Cesium.Color.WHITE, outlineWidth: 2, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND },
              label: { text: MILL[props.mv_kind] || '•', font: 'bold 11px sans-serif', fillColor: Cesium.Color.BLACK, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND } })
          } else {
            ds.entities.add({ position: pos,
              point: { pixelSize: 8, color: stroke, outlineColor: Cesium.Color.BLACK, outlineWidth: 1, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND },
              label: props.name ? { text: String(props.name), font: '11px sans-serif', fillColor: Cesium.Color.WHITE,
                pixelOffset: new Cesium.Cartesian2(0, -14), style: Cesium.LabelStyle.FILL_AND_OUTLINE, outlineWidth: 2,
                heightReference: Cesium.HeightReference.CLAMP_TO_GROUND } : undefined })
          }
        } else if (t === 'LineString') {
          ds.entities.add({ polyline: { positions: toDeg(f.geometry.coordinates), width: props['stroke-width'] || 2.5, material: stroke, clampToGround: true } })
          if (props.mv_kind === 'rb' && f.geometry.coordinates.length >= 2 && props.distance_m != null) {
            const [a, b] = f.geometry.coordinates
            const d = props.distance_m
            ds.entities.add({ position: Cesium.Cartesian3.fromDegrees((a[0] + b[0]) / 2, (a[1] + b[1]) / 2),
              label: { text: `${d >= 1000 ? (d / 1000).toFixed(2) + ' km' : d + ' m'}${props.bearing_deg != null ? `  ·  ${props.bearing_deg}°` : ''}`,
                font: '11px sans-serif', fillColor: Cesium.Color.WHITE, showBackground: true, backgroundColor: Cesium.Color.fromCssColorString('#161b22').withAlpha(0.85),
                pixelOffset: new Cesium.Cartesian2(0, -12), heightReference: Cesium.HeightReference.CLAMP_TO_GROUND } })
          }
        } else if (t === 'MultiLineString') {
          for (const line of f.geometry.coordinates) ds.entities.add({ polyline: { positions: toDeg(line), width: 2.5, material: stroke, clampToGround: true } })
        } else if (t === 'Polygon') {
          ds.entities.add({ polygon: { hierarchy: toDeg(f.geometry.coordinates[0]), material: fill, outline: true, outlineColor: stroke, perPositionHeight: false } })
        } else if (t === 'MultiPolygon') {
          for (const poly of f.geometry.coordinates) ds.entities.add({ polygon: { hierarchy: toDeg(poly[0]), material: fill, outline: true, outlineColor: stroke } })
        }
      } catch { /* skip malformed feature */ }
    }

    // extra transmitters (the 2D map shows these too)
    for (const e of extraTxList || []) {
      if (typeof e?.lat !== 'number' || typeof e?.lon !== 'number') continue
      ds.entities.add({ position: Cesium.Cartesian3.fromDegrees(e.lon, e.lat, (e.altitude_m || 0) + (e.height_m || 0)),
        point: { pixelSize: 9, color: Cesium.Color.fromCssColorString(e.color || '#00b4d8'), outlineColor: Cesium.Color.BLACK, outlineWidth: 2, heightReference: Cesium.HeightReference.NONE },
        label: { text: String(e.label || 'TX'), font: '11px sans-serif', fillColor: Cesium.Color.WHITE, pixelOffset: new Cesium.Cartesian2(0, -16),
          style: Cesium.LabelStyle.FILL_AND_OUTLINE, outlineWidth: 2, heightReference: Cesium.HeightReference.NONE } })
    }

    // DF / geolocation picture (bearing wedges, Cut/Fix centroids, CAP/CEP ellipses) — mirrors the 2D map
    const lobCutC = Cesium.Color.fromCssColorString(mapColors?.lobCut || '#06d6a0')
    const lobFixC = Cesium.Color.fromCssColorString(mapColors?.lobFix || '#ef4444')
    for (const f of geolocationGeoJSON?.features || []) {
      const p = f?.properties || {}
      const t = f?.geometry?.type
      try {
        if (p.glx === 'lob' && t === 'LineString') {
          ds.entities.add({ polyline: { positions: f.geometry.coordinates.map(([lo, la]) => Cesium.Cartesian3.fromDegrees(lo, la)),
            width: 2, material: Cesium.Color.fromCssColorString(p.color || '#f59e0b').withAlpha(0.9), clampToGround: true } })
        } else if (p.glx === 'cap' && (t === 'Polygon')) {
          const c = p.kind === 'fix' ? lobFixC : lobCutC
          ds.entities.add({ polygon: { hierarchy: f.geometry.coordinates[0].map(([lo, la]) => Cesium.Cartesian3.fromDegrees(lo, la)),
            material: c.withAlpha(0.14), outline: true, outlineColor: c } })
        } else if (p.glx === 'emitter' && t === 'Point') {
          const [lo, la] = f.geometry.coordinates
          const c = p.kind === 'fix' ? lobFixC : lobCutC
          ds.entities.add({ position: Cesium.Cartesian3.fromDegrees(lo, la),
            point: { pixelSize: 13, color: c, outlineColor: Cesium.Color.WHITE, outlineWidth: 2, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND },
            label: { text: `${(p.kind || 'fix').toUpperCase()}${p.frequency_hz ? ` · ${(p.frequency_hz / 1e6).toFixed(3)} MHz` : ''}${p.device_id ? ` · ${p.device_id}` : ''}`,
              font: '11px sans-serif', fillColor: Cesium.Color.WHITE, showBackground: true, backgroundColor: Cesium.Color.fromCssColorString('#161b22').withAlpha(0.85),
              pixelOffset: new Cesium.Cartesian2(0, -16), heightReference: Cesium.HeightReference.CLAMP_TO_GROUND } })
        }
      } catch { /* skip malformed feature */ }
    }

    // TX marker + antenna lobe
    if (tx && typeof tx.lat === 'number' && typeof tx.lon === 'number') {
      const txPos = Cesium.Cartesian3.fromDegrees(tx.lon, tx.lat, (tx.altitude_m || 0) + (tx.height_m || 0))
      ds.entities.add({
        position: txPos,
        point: { pixelSize: 10, color: Cesium.Color.CYAN, outlineColor: Cesium.Color.BLACK, outlineWidth: 2,
                 heightReference: Cesium.HeightReference.NONE },
        label: { text: 'TX', font: '11px sans-serif', fillColor: Cesium.Color.WHITE,
                 pixelOffset: new Cesium.Cartesian2(0, -18), style: Cesium.LabelStyle.FILL_AND_OUTLINE, outlineWidth: 2 },
      })
      addAntennaLobe(ds, tx, antennaAzimuthDeg, antennaTiltDeg, antennaPattern)
    }

    // RX marker + LOS line + Fresnel + obstruction check
    if (rxPoint && typeof rxPoint.lat === 'number' && typeof rxPoint.lon === 'number') {
      const rxPos = Cesium.Cartesian3.fromDegrees(rxPoint.lon, rxPoint.lat, rxPoint.height_m || 0)
      ds.entities.add({ position: rxPos, point: { pixelSize: 9, color: Cesium.Color.YELLOW, outlineColor: Cesium.Color.BLACK, outlineWidth: 2 },
                        label: { text: 'RX', font: '11px sans-serif', fillColor: Cesium.Color.WHITE,
                                 pixelOffset: new Cesium.Cartesian2(0, -18), style: Cesium.LabelStyle.FILL_AND_OUTLINE, outlineWidth: 2 } })
      if (tx && typeof tx.lat === 'number') {
        ds.entities.add({
          polyline: { positions: [Cesium.Cartesian3.fromDegrees(tx.lon, tx.lat, (tx.altitude_m || 0) + (tx.height_m || 0)), rxPos],
                      width: 2, material: new Cesium.PolylineDashMaterialProperty({ color: Cesium.Color.LIME }), arcType: Cesium.ArcType.GEODESIC },
        })
        addFresnelEllipsoid(ds, tx, rxPoint, tx.frequency_hz || 433e6)
        // LOS obstruction: pull a terrain profile and drop a red marker on the blocking ridge.
        checkLosObstruction(tx, rxPoint).then((obs) => {
          if (cancelled || !obs || !dsRef.current) return
          dsRef.current.entities.add({
            position: Cesium.Cartesian3.fromDegrees(obs.lon, obs.lat, obs.elevM),
            point: { pixelSize: 11, color: Cesium.Color.RED, outlineColor: Cesium.Color.WHITE, outlineWidth: 2,
                     heightReference: Cesium.HeightReference.CLAMP_TO_GROUND },
            label: { text: `LOS blocked · clearance ${obs.clearanceM > 0 ? '+' : ''}${obs.clearanceM.toFixed(0)} m`,
                     font: '11px sans-serif', fillColor: Cesium.Color.WHITE, showBackground: true,
                     backgroundColor: Cesium.Color.fromCssColorString('#3d1414').withAlpha(0.88),
                     pixelOffset: new Cesium.Cartesian2(0, -16), heightReference: Cesium.HeightReference.CLAMP_TO_GROUND },
          })
          dsRef.current.entities.add({ polyline: {
            positions: [Cesium.Cartesian3.fromDegrees(tx.lon, tx.lat, (tx.altitude_m || 0) + (tx.height_m || 0)),
                        Cesium.Cartesian3.fromDegrees(obs.lon, obs.lat, obs.elevM)],
            width: 3, material: new Cesium.PolylineDashMaterialProperty({ color: Cesium.Color.RED }), arcType: Cesium.ArcType.GEODESIC } })
          requestRenderRef.current()
        })
      }
    }

    // operator GPS — "you are here"
    if (gpsFix && typeof gpsFix.lat === 'number' && typeof gpsFix.lon === 'number') {
      const gp = Cesium.Cartesian3.fromDegrees(gpsFix.lon, gpsFix.lat)
      ds.entities.add({ position: gp,
        point: { pixelSize: 11, color: Cesium.Color.fromCssColorString('#22d3ee'), outlineColor: Cesium.Color.WHITE, outlineWidth: 2, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND },
        label: { text: `▲ you (${gpsFix.source || 'GPS'})`, font: '11px sans-serif', fillColor: Cesium.Color.WHITE, showBackground: true,
                 backgroundColor: Cesium.Color.fromCssColorString('#0e3a44').withAlpha(0.85), pixelOffset: new Cesium.Cartesian2(0, -16), heightReference: Cesium.HeightReference.CLAMP_TO_GROUND } })
      if (typeof gpsFix.heading_deg === 'number') {
        const [hlat, hlon] = [gpsFix.lat + 0.0009 * Math.cos(gpsFix.heading_deg * Math.PI / 180), gpsFix.lon + 0.0009 * Math.sin(gpsFix.heading_deg * Math.PI / 180) / Math.max(0.05, Math.cos(gpsFix.lat * Math.PI / 180))]
        ds.entities.add({ polyline: { positions: [gp, Cesium.Cartesian3.fromDegrees(hlon, hlat)], width: 3, material: Cesium.Color.fromCssColorString('#22d3ee'), clampToGround: true } })
      }
    }
    requestRenderRef.current()
    return () => { cancelled = true }
  }, [extraGeojsonLayers, tx, rxPoint, antennaAzimuthDeg, antennaTiltDeg, antennaPattern, minSignalDbm, mapColors, ul?.drawnGeoJSON, ul?.globeDrawnGeoJSON, ul?.layers, extraTxList, geolocationGeoJSON, gpsFix])

  const recenter = () => { const v = viewerRef.current; if (v && !v.isDestroyed()) { flyTo(v, center); requestRenderRef.current() } }
  const flyToLatLon = (lat, lon) => { const v = viewerRef.current; if (v && !v.isDestroyed()) { flyTo(v, { lat, lon }); requestRenderRef.current() } }
  const runSearch = async () => {
    const q = searchQuery.trim(); if (!q) return
    setSearching(true)
    try { setSearchResults(await geocodeNominatim(q)) } catch { setSearchResults([]) } finally { setSearching(false) }
  }
  // KMZ/KML/GeoJSON/GPX import — routed through the *shared* imported-layers store
  // (`ul`) so it renders on the globe AND persists when you switch back to 2D (and
  // vice-versa: anything dropped on the 2D map renders here too — see the `ul.layers`
  // loop in the vector-overlays effect).
  const onKmzPicked = async (ev) => {
    const files = Array.from(ev.target.files || [])
    ev.target.value = ''
    if (!files.length || !ul?.addGeoJSONLayer) return
    try {
      const { loadFiles } = await import('../../utils/fileLoaders')
      const items = await loadFiles(files)
      for (const it of items) {
        if (it?.kind === 'geojson' && it.geojson) {
          ul.addGeoJSONLayer(it.geojson, { name: it.name, sourceFormat: it.sourceFormat, fit: false })
        }
      }
      requestRenderRef.current()
    } catch (e) {
      console.warn('[GlobeView] import failed:', e)
    }
  }
  const exportCoverageAsKmz = async () => {
    if (!coverageGeoJSON) return
    try {
      const blob = await exportCoverageKmz(coverageGeoJSON, 'Ares coverage', minSignalDbm)
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `ares-coverage-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.kmz`
      a.click()
      setTimeout(() => URL.revokeObjectURL(a.href), 0)
    } catch (e) {
      console.warn('[GlobeView] KMZ export failed:', e)
    }
  }
  const popup = { marginTop: 6, background: '#161b22', border: '1px solid #30363d', borderRadius: 8,
                  padding: '12px 14px', boxShadow: '0 6px 20px rgba(0,0,0,0.7)' }
  const btn = (active) => `btn ${active ? 'btn-primary' : 'btn-ghost'}`
  const BTN_STYLE = { padding: '3px 8px', fontSize: 13 }

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%' }}>
      <div ref={containerRef} style={{ width: '100%', height: '100%' }} />

      {/* Floating toolbar — visually identical to the 2D map's toolbar.
          zIndex 700 keeps it + the (downward-expanding) draw palette above the bottom panel (zIndex 600). */}
      {!err && (
        <div style={{ position: 'absolute', top: 8, right: 8, zIndex: 700 }}>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center', justifyContent: 'flex-end', flexWrap: 'wrap' }}>
            {/* basemap (shared list with the 2D map) */}
            {Object.entries(BASEMAPS).map(([id, bm]) => (
              <button key={id} className={btn(basemapId === id)} style={{ padding: '3px 8px', fontSize: 11 }}
                title={bm.label} onClick={() => setBasemapId(id)}>{bm.label}</button>
            ))}
            <button className="btn btn-ghost" style={BTN_STYLE} title="Re-centre on the transmitter" onClick={recenter}>⊕</button>
            <button className={btn(rulerActive)} style={BTN_STYLE} title="Distance / bearing ruler — click two points on the globe" onClick={() => setRulerActive(a => !a)}>📏</button>
            <button className={btn(searchOpen)} style={BTN_STYLE} title="Search a place" onClick={() => setSearchOpen(o => !o)}>🔍</button>
            <button className={btn(drawPaletteOpen || !!drawTool)} style={BTN_STYLE}
                    title="Annotation drawing tools (point / line / polygon / rectangle — more coming)"
                    onClick={() => { if (drawTool) { setDrawTool(null) } else { setDrawPaletteOpen(o => !o) } }}>✎{drawTool ? ' •' : ''}</button>
            <button className="btn btn-ghost" style={BTN_STYLE} title="Load KMZ / KML onto the globe (ATAK / WinTAK / Google Earth)" onClick={() => kmzFileInputRef.current?.click()}>📥</button>
            <input ref={kmzFileInputRef} type="file" accept=".kmz,.kml,application/vnd.google-earth.kml+xml,application/vnd.google-earth.kmz"
                   style={{ display: 'none' }} onChange={onKmzPicked} />
            <button className="btn btn-ghost" style={BTN_STYLE} title="Export the current coverage to KMZ (ATAK image overlay / WinTAK)" onClick={exportCoverageAsKmz} disabled={!coverageGeoJSON}>💾</button>
            <button className="btn btn-ghost" style={BTN_STYLE} title="Switch to the 2D map" onClick={() => setViewMode('2d')}>2D</button>
            <MapSettingsCog
              kind="3d"
              distUnit={distUnit} setDistUnit={setDistUnit}
              coordSystem={coordSystem} setCoordSystem={setCoordSystem}
              showCompassRose={showCompassRose} setShowCompassRose={setShowCompassRose}
              mapBrightness={mapBrightness} setMapBrightness={setMapBrightness}
            />
          </div>

          {/* ✎ annotation-tools palette — same tool set as the 2D map */}
          {drawPaletteOpen && (
            <div style={{ ...popup, width: 264, maxHeight: '60vh', overflowY: 'auto' }}>
              {[
                { hdr: 'Basic', tools: [['point', 'Point', '•'], ['line', 'Line', '╱'], ['polygon', 'Polygon', '▱'], ['rectangle', 'Rect', '▭']] },
                { hdr: 'Advanced', tools: [['circle', 'Circle', '◯'], ['ellipse', 'Ellipse', '⬭'], ['freehand', 'Freehand', '✎'], ['rangeRings', 'Rings', '◎'], ['fan', 'Fan', '◔'], ['rb', 'Rng/Brg', '↗'], ['geofence', 'Geofence', '⬡']] },
                { hdr: 'Markers', tools: [['milFriend', 'Friend', '◻'], ['milHostile', 'Hostile', '◇'], ['milNeutral', 'Neutral', '□'], ['milUnknown', 'Unknown', '?']] },
              ].map(group => (
                <div key={group.hdr} style={{ marginBottom: 8 }}>
                  <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e', padding: '0 2px 6px', textTransform: 'uppercase', letterSpacing: 0.8 }}>{group.hdr}</div>
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 4 }}>
                    {group.tools.map(([id, l, i]) => (
                      <button key={id} className={`btn ${drawTool === id ? 'btn-primary' : 'btn-ghost'}`}
                        style={{ padding: '6px 3px', fontSize: 10, lineHeight: 1.2, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2 }}
                        title={l} onClick={() => { setDrawTool(id); setDrawPaletteOpen(false) }}>
                        <span style={{ fontSize: 14 }}>{i}</span><span>{l}</span>
                      </button>
                    ))}
                  </div>
                </div>
              ))}
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 2px 4px' }}>
                <span style={{ fontSize: 10, fontWeight: 700, color: '#8b949e', textTransform: 'uppercase', letterSpacing: 0.8 }}>NATO / Ranger Symbology</span>
                <button className="btn btn-ghost" style={{ padding: '2px 6px', fontSize: 10, color: natoPickerOpen ? '#06d6a0' : '#8b949e' }} onClick={() => setNatoPickerOpen(o => !o)}>{natoPickerOpen ? '▾ Hide' : '▸ Open picker'}</button>
              </div>
              {natoPickerOpen && (
                <div style={{ background: '#0d1117', border: '1px solid #30363d', borderRadius: 6, padding: '4px 6px', marginBottom: 6 }}>
                  <Suspense fallback={<div style={{ fontSize: 11, color: '#8b949e', padding: 12, textAlign: 'center' }}>Loading NATO symbology…</div>}>
                    <NatoSymbolPicker ctrl={null} onArm={(arm) => { natoArmRef.current = arm; setDrawTool('nato'); setNatoPickerOpen(false); setDrawPaletteOpen(false) }} />
                  </Suspense>
                </div>
              )}
              <div style={{ fontSize: 10, color: '#6e7681', marginTop: 4 }}>Click to place; right-click finishes lines / polygons / range-bearing; drag for freehand.</div>
              {ul?.globeDrawnGeoJSON?.features?.length ? (
                <button className="btn btn-secondary" style={{ width: '100%', fontSize: 11, marginTop: 8 }} onClick={() => ul.clearGlobeDrawings()}>Clear 3D-drawn features</button>
              ) : null}
            </div>
          )}

          {/* search panel */}
          {searchOpen && (
            <div style={{ ...popup, width: 280, padding: 10 }}>
              <div style={{ display: 'flex', gap: 6 }}>
                <input value={searchQuery} onChange={e => setSearchQuery(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') runSearch() }}
                  placeholder="Place, address…" autoFocus
                  style={{ flex: 1, background: '#0d1117', border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3', fontSize: 12, padding: '5px 7px' }} />
                <button className="btn btn-ghost" style={BTN_STYLE} onClick={runSearch} disabled={searching}>{searching ? '…' : 'Go'}</button>
              </div>
              {searchResults.map((r, i) => (
                <div key={i} onClick={() => { flyToLatLon(r.lat, r.lon); setSearchOpen(false) }}
                  style={{ padding: '5px 6px', fontSize: 11, color: '#c9d1d9', cursor: 'pointer', borderTop: '1px solid #21262d' }}
                  onMouseEnter={e => e.currentTarget.style.background = '#1c2128'} onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>{r.name}</div>
              ))}
            </div>
          )}
        </div>
      )}

      {err && (
        <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
                      background: '#0d1117', color: '#f85149', font: '13px/1.5 system-ui', padding: 24, textAlign: 'center' }}>
          3D globe failed to load: {err}<br/><span style={{ color: '#8b949e', fontSize: 11 }}>Check the browser console; likely a Cesium asset/CESIUM_BASE_URL issue — try a hard refresh, or re-run `npm install`.</span>
        </div>
      )}
      <div style={{ position: 'absolute', bottom: 8, left: 8, zIndex: 5, background: 'rgba(13,17,23,0.8)',
                    color: '#8b949e', font: '11px/1.4 system-ui', padding: '4px 8px', borderRadius: 4,
                    border: '1px solid #30363d', pointerEvents: 'none' }}>
        3D globe (CesiumJS){lite ? ' · lite' : ''} · coverage + LOS/Fresnel + obstruction + antenna pattern
        {offlinePacks.length ? <> · offline packs: {offlinePacks.map((p) => p.name).join(', ')}</> : null}
      </div>
    </div>
  )
}

// ── helpers ──────────────────────────────────────────────────────────────────
// A sensible regional 3D view: look AT the point from ~90 km out, tilted ~50°.
// (Leaflet zoom levels don't map cleanly to a 3D camera, so we don't try.)
function flyTo(viewer, center, /* animate */ _animate) {
  const haveCenter = center?.lat != null && center?.lon != null
  if (!haveCenter) {
    viewer.camera.setView({ destination: Cesium.Cartesian3.fromDegrees(DEFAULT_VIEW.lon, DEFAULT_VIEW.lat, DEFAULT_VIEW.heightM) })
    return
  }
  const target = Cesium.Cartesian3.fromDegrees(center.lon, center.lat, 0)
  viewer.camera.lookAt(target, new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-50), 90_000))
  // release the lookAt transform so the user can pan/zoom/rotate freely afterward
  viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY)
}

/** Rasterise coverage Point features to a data-URL PNG + the Cesium Rectangle it spans. */
function rasterizeCoverage(feats, minDbm) {
  let west = Infinity, south = Infinity, east = -Infinity, north = -Infinity
  for (const f of feats) {
    const [lo, la] = f.geometry.coordinates
    if (lo < west) west = lo; if (lo > east) east = lo
    if (la < south) south = la; if (la > north) north = la
  }
  if (!isFinite(west) || east <= west || north <= south) return null
  const dlon = (east - west) || 1e-6, dlat = (north - south) || 1e-6
  west -= dlon * 0.02; east += dlon * 0.02; south -= dlat * 0.02; north += dlat * 0.02
  const W = east - west, H = north - south
  const midLat = (north + south) / 2
  const aspect = (W * Math.cos(midLat * Math.PI / 180)) / H
  const cw = aspect >= 1 ? MAX_RASTER_PX : Math.max(1, Math.round(MAX_RASTER_PX * aspect))
  const ch = aspect >= 1 ? Math.max(1, Math.round(MAX_RASTER_PX / aspect)) : MAX_RASTER_PX
  const cv = document.createElement('canvas'); cv.width = cw; cv.height = ch
  const ctx = cv.getContext('2d')
  const r = 3
  for (const f of feats) {
    const [lo, la] = f.geometry.coordinates
    const px = ((lo - west) / W) * (cw - 1)
    const py = ((north - la) / H) * (ch - 1)
    const [cr, cg, cb, ca] = signalToColor(f.properties?.signal_dbm ?? minDbm, minDbm)
    ctx.fillStyle = `rgba(${cr},${cg},${cb},${ca / 255})`
    ctx.beginPath(); ctx.arc(px, py, r, 0, 2 * Math.PI); ctx.fill()
  }
  return { dataUrl: cv.toDataURL('image/png'), rect: Cesium.Rectangle.fromDegrees(west, south, east, north) }
}

/**
 * Antenna "lobe" at the TX, shaped by the selected polar pattern.
 *  - omni / unknown pattern → a translucent extruded ring (as before).
 *  - directional pattern → a ground footprint polygon whose boundary radius
 *    tracks the azimuth-plane gain (peak along `azDeg`), plus a 3D outline of
 *    the same curve at antenna height. Distances are illustrative, not metric:
 *    radius ∝ a base reach (scaled by TX power) × linear-amplitude gain.
 */
function addAntennaLobe(ds, tx, azDeg, tiltDeg, patternId) {
  const baseHeight = (tx.altitude_m || 0) + (tx.height_m || 0)
  const pos = Cesium.Cartesian3.fromDegrees(tx.lon, tx.lat, baseHeight)
  const powerScale = Math.max(0.5, Math.min(3, ((tx.power_dbm ?? 27) - 17) / 15))
  const reach = 1500 * powerScale // metres — visual only
  const pat = patternId && POLAR_PATTERNS[patternId]
  const isOmni = !pat || patternId === 'omni' || azDeg == null

  if (isOmni) {
    ds.entities.add({ position: pos, ellipse: {
      semiMajorAxis: reach, semiMinorAxis: reach, height: baseHeight, extrudedHeight: baseHeight + 60,
      material: Cesium.Color.CYAN.withAlpha(0.12), outline: true, outlineColor: Cesium.Color.CYAN.withAlpha(0.35) } })
    return
  }

  // boundary of the azimuth pattern, sampled every few degrees of true bearing
  const cosLat = Math.max(0.05, Math.cos((tx.lat * Math.PI) / 180))
  const ground = []   // [lon,lat,...] for fromDegreesArray
  const outline = []  // Cartesian3 at antenna height
  for (let bear = 0; bear <= 360; bear += 3) {
    const gDb = polarPatternGainDb(patternId, bear - azDeg)        // dB rel. peak (0)
    const r = reach * Math.max(0.04, Math.pow(10, gDb / 20))        // linear amplitude, floored
    const br = (bear * Math.PI) / 180
    const dLat = (r * Math.cos(br)) / 111320
    const dLon = (r * Math.sin(br)) / (111320 * cosLat)
    const lon = tx.lon + dLon, lat = tx.lat + dLat
    ground.push(lon, lat)
    outline.push(Cesium.Cartesian3.fromDegrees(lon, lat, baseHeight + 8))
  }
  ds.entities.add({ polygon: {
    hierarchy: Cesium.Cartesian3.fromDegreesArray(ground),
    material: Cesium.Color.CYAN.withAlpha(0.13), heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
    outline: false } })
  ds.entities.add({ polyline: {
    positions: outline, width: 2, material: Cesium.Color.CYAN.withAlpha(0.6), arcType: Cesium.ArcType.GEODESIC } })
  // a short boresight needle so the pointing direction reads at a glance
  const bb = (azDeg * Math.PI) / 180
  ds.entities.add({ polyline: { positions: [pos, Cesium.Cartesian3.fromDegrees(
    tx.lon + (reach * 1.05 * Math.sin(bb)) / (111320 * cosLat),
    tx.lat + (reach * 1.05 * Math.cos(bb)) / 111320, baseHeight + 8)],
    width: 2, material: new Cesium.PolylineDashMaterialProperty({ color: Cesium.Color.CYAN }), arcType: Cesium.ArcType.GEODESIC } })
}

// ── LOS obstruction (4/3-earth) via the server terrain profile ───────────────
// Returns {lat, lon, elevM, clearanceM} of the worst blocking point, or null if
// the path is clear / no terrain data / the request failed.
async function checkLosObstruction(tx, rx) {
  try {
    const r = await fetch(`/api/v1/terrain/profile?lat1=${tx.lat}&lon1=${tx.lon}&lat2=${rx.lat}&lon2=${rx.lon}&num_points=256`)
    if (!r.ok) return null
    const { distances_m: d, elevations_m: el, total_distance_m: D } = await r.json()
    if (!Array.isArray(d) || !Array.isArray(el) || el.length < 3 || !(D > 1)) return null
    const txGround = el[0], rxGround = el[el.length - 1]
    const txAsl = (tx.altitude_m && tx.altitude_m > 0 ? tx.altitude_m : txGround) + (tx.height_m || 0)
    const rxAsl = (rx.altitude_m && rx.altitude_m > 0 ? rx.altitude_m : rxGround) + (rx.height_m ?? 2)
    const Re = 6_371_000, k = 4 / 3
    let worst = null
    for (let i = 1; i < el.length - 1; i++) {
      const di = d[i]
      const los = txAsl + (rxAsl - txAsl) * (di / D)
      const bulge = (di * (D - di)) / (2 * k * Re)        // terrain rises toward the chord
      const clearance = los - (el[i] + bulge)
      if (clearance < 0 && (worst == null || clearance < worst.clearance)) worst = { i, clearance }
    }
    if (!worst) return null
    const carto = new Cesium.EllipsoidGeodesic(
      Cesium.Cartographic.fromDegrees(tx.lon, tx.lat), Cesium.Cartographic.fromDegrees(rx.lon, rx.lat))
      .interpolateUsingFraction(d[worst.i] / D)
    return { lat: Cesium.Math.toDegrees(carto.latitude), lon: Cesium.Math.toDegrees(carto.longitude),
             elevM: el[worst.i], clearanceM: worst.clearance }
  } catch { return null }
}

/** First-Fresnel-zone ellipsoid along TX→RX (semi-major = half path length, minor = F1 at midpoint). */
function addFresnelEllipsoid(ds, tx, rx, freqHz) {
  const txC = Cesium.Cartographic.fromDegrees(tx.lon, tx.lat, (tx.altitude_m || 0) + (tx.height_m || 0))
  const rxC = Cesium.Cartographic.fromDegrees(rx.lon, rx.lat, rx.height_m || 0)
  const geod = new Cesium.EllipsoidGeodesic(txC, rxC)
  const D = geod.surfaceDistance
  if (!isFinite(D) || D <= 1) return
  const lambda = 299_792_458 / Math.max(1, freqHz)
  const f1 = Math.sqrt(lambda * (D / 2) * (D / 2) / D) // = 0.5·sqrt(λ·D)
  const midCarto = geod.interpolateUsingFraction(0.5)
  const midHeight = ((tx.altitude_m || 0) + (tx.height_m || 0) + (rx.height_m || 0)) / 2
  const midPos = Cesium.Cartesian3.fromRadians(midCarto.longitude, midCarto.latitude, midHeight)
  const headingDeg = Cesium.Math.toDegrees(geod.startHeading)
  const orient = Cesium.Transforms.headingPitchRollQuaternion(
    midPos, new Cesium.HeadingPitchRoll(Cesium.Math.toRadians(headingDeg), 0, 0))
  // Cesium ellipsoid radii are (x,y,z) in local frame; +Y is "forward" after heading → put the long axis on Y.
  ds.entities.add({ position: midPos, orientation: orient, ellipsoid: {
    radii: new Cesium.Cartesian3(Math.max(1, f1), D / 2, Math.max(1, f1)),
    material: Cesium.Color.ORANGE.withAlpha(0.10), outline: true, outlineColor: Cesium.Color.ORANGE.withAlpha(0.35),
    slicePartitions: 24, stackPartitions: 24 } })
}
