/**
 * RF Propagation Simulator — Main App Component
 * Layout: Header → Sidebar (controls) | Map | Bottom (charts/results)
 * Includes all CloudRF-equivalent features + 3D ray tracing.
 */
import { useState, useEffect, useCallback, useRef, useMemo, lazy, Suspense } from 'react'
import {
  Zap, Trash2, Menu, X, HelpCircle, Plus, Save, FolderOpen,
  Route, MapPin, Square, Hexagon, Radio, Layers, GitMerge, Network,
  Satellite, Archive, Scan, RefreshCw, Crosshair, Upload, Server, Globe,
  ChevronLeft, ChevronRight, ChevronDown, ChevronUp,
  Undo2, Redo2,
} from 'lucide-react'
import AppIcon from './components/Common/AppIcon'
import { ToastContainer, toast } from 'react-toastify'
import 'react-toastify/dist/ReactToastify.css'
import { coordSystemLabel } from './utils/units'
import { makeResolveModelFast } from './utils/autoPropagationModel'

import MapView from './components/Map/MapView'
import LayerManagerPanel from './components/Map/LayerManagerPanel'
import { useViewMode } from './hooks/useViewMode'
import AtakServerPanel from './components/Tools/AtakServerPanel'
import AppModals from './components/AppModals'
import OverflowMenu from './components/Header/OverflowMenu'
import HeaderTabs from './components/Header/HeaderTabs'
import HeaderActions from './components/Header/HeaderActions'
import SdrPanel from './components/Tools/SdrPanel'
import { useUserLayers } from './hooks/useUserLayers'
import { useStandaloneTerrainProfile } from './hooks/useStandaloneTerrainProfile'
import { getGpsFix } from './api/client'
import TransmitterPanel from './components/Controls/TransmitterPanel'
import PropagationPanel from './components/Controls/PropagationPanel'
import AntennaPanel from './components/Controls/AntennaPanel'
import AtmospherePanel from './components/Controls/AtmospherePanel'
import ResultsPanel from './components/Results/ResultsPanel'
import TerrainProfile from './components/Charts/TerrainProfile'
import DfPanel from './components/Panels/DfPanel'
import ChatPanel from './components/Panels/ChatPanel'
import EmitterSummary from './components/Panels/EmitterSummary'
import SavedLocations from './components/Panels/SavedLocations'
import SpaceWxPanel from './components/Panels/SpaceWxPanel'
import BottomPanelTabs from './components/Panels/BottomPanelTabs'
import TerrainTab from './components/Panels/TerrainTab'
import BottomPanelContent from './components/Panels/BottomPanelContent'
import HelpPanel from './components/Common/HelpPanel'
import DecibelCalculator from './components/Tools/DecibelCalculator'
import RemoteAccessPanel from './components/Tools/RemoteAccessPanel'
import ArchivePanel from './components/Tools/ArchivePanel'
import ManetPanel from './components/Tools/ManetPanel'
import SatellitePanel from './components/Tools/SatellitePanel'
import GeoLocationPanel from './components/Geolocation/GeoLocationPanel'
import LoBList from './components/Geolocation/LoBList'
import { groupLoBsByFrequency, lobGroupKey, computeGroupIntersections, computeCentroid, computeCAPEllipse, computeLoBRenderDistance, destinationPoint, DEFAULT_LOB_ALGORITHM } from './components/Geolocation/LoBUtils'
import { useGeolocation } from './hooks/useGeolocation'
import { useSdrStream } from './hooks/useSdrStream'
import { useBottomPanelResize } from './hooks/useBottomPanelResize'
import { useNumberFieldSelectAll } from './hooks/useNumberFieldSelectAll'
import { useTerrainGrid } from './hooks/useTerrainGrid'
import { DEFAULT_TX, DEFAULT_RX, DEFAULT_PROPAGATION, DEFAULT_ATMOSPHERE, RADAR_TARGETS, TX_COLORS } from './appDefaults'
import { SESSION_KEY, loadSession } from './session'
import { useSessionAutosave } from './hooks/useSessionAutosave'
import { useSimulationState } from './hooks/useSimulationState'
import EditableLabel from './components/Common/EditableLabel'
import ExtraTransmitters from './components/Controls/ExtraTransmitters'
import RadarTargetPicker from './components/Controls/RadarTargetPicker'
import BestSiteSidebar from './components/Controls/BestSiteSidebar'
import RouteSidebar from './components/Controls/RouteSidebar'
import MultipointSidebar from './components/Controls/MultipointSidebar'
import BestServerSidebar from './components/Controls/BestServerSidebar'
import BsaPolygonSidebar from './components/Controls/BsaPolygonSidebar'
import RayTraceSidebar from './components/Controls/RayTraceSidebar'
import ToolBtn from './components/Common/ToolBtn'
import PromptDialogProvider from './components/Common/PromptDialog'
import SaveStateDialog from './components/Common/SaveStateDialog'

import {
  simulateCoverage, simulateCoverageRaster, simulateP2P, simulateBestSite, getSpaceWeather, purgeCache,
  simulateRoute, simulateMultipoint, simulateManet, simulateBestServer,
  simulateInterference, simulateSuperLayer, simulateBestSitePolygon,
  simulateRayTrace, simulateSatelliteVisibility,
  getBuildings, regionAtPoint,
  getViewshed, getTerrainContours,
} from './api/client'
import ThreeDView from './components/Charts/ThreeDView'

// Cesium globe is ~30 MB — load it only when the user switches to the 3D view.
const GlobeView = lazy(() => import('./components/Map/GlobeView'))

// Session restored once at module load — used to hydrate useState initial values.
const _s = loadSession()

// Turn an axios error into a readable string — flattens FastAPI 422 validation arrays
// (`[{loc, msg, type}, ...]`) so the user sees "radius_km — Input should be ≤ 500" instead of
// "[object Object]".
function errDetail(err) {
  const raw = err?.response?.data?.detail
  if (Array.isArray(raw)) return raw.map(e => `${(e.loc || []).slice(1).join('.') || 'field'} — ${e.msg || e}`).join('; ')
  if (raw && typeof raw === 'object') return JSON.stringify(raw)
  return raw || err?.message || String(err)
}

// ── Main App ──────────────────────────────────────────────────────────────────
export default function App() {
  // ── Core state ───────────────────────────────────────────────────────────────
  const viewMode = useViewMode((s) => s.mode)            // '2d' (Leaflet) | '3d' (Cesium globe)
  const setViewMode = useViewMode((s) => s.setMode)
  const [tx, setTx] = useState(() => _s?.primaryTransmitter ? { ...DEFAULT_TX, ..._s.primaryTransmitter } : DEFAULT_TX)
  const [rx, setRx] = useState(() => _s?.receiver ? { ...DEFAULT_RX, ..._s.receiver } : DEFAULT_RX)
  const [propagation, setPropagation] = useState(() => _s?.propagation ? { ...DEFAULT_PROPAGATION, ..._s.propagation } : DEFAULT_PROPAGATION)
  const [atmosphere, setAtmosphere] = useState(() => _s?.atmosphere ? { ...DEFAULT_ATMOSPHERE, ..._s.atmosphere } : DEFAULT_ATMOSPHERE)

  const [txLabel, setTxLabel] = useState(() => _s?.ui?.txLabel ?? 'TX 1')
  const [extraTxList, setExtraTxList] = useState(() => _s?.extraTransmitters ?? [])
  const [helpOpen, setHelpOpen] = useState(false)
  const [atakPanelOpen, setAtakPanelOpen] = useState(false)
  const [sdrPanelOpen, setSdrPanelOpen] = useState(false)
  const [sdrPicking, setSdrPicking] = useState(false)        // SDR device-location map pick in progress
  const sdrPickCbRef = useRef(null)                          // callback fed the clicked lat/lon
  const [layersOpen, setLayersOpen] = useState(false)
  const [layersRegionPreselect, setLayersRegionPreselect] = useState(null)   // region pre-picked from a right-click on the map
  const [dbCalcOpen, setDbCalcOpen] = useState(false)
  const [remoteOpen, setRemoteOpen] = useState(false)
  // SDR / DF live state: features from the server-side solver, and the latest
  // auto-coverage GeoJSON from a confirmed fix (rendered as a faint extra layer).
  const [sdrFeatures, setSdrFeatures] = useState([])
  const [sdrCoverage, setSdrCoverage] = useState(null)   // { geojson, frequency_hz, centroid }
  const [sdrFixes, setSdrFixes] = useState([])           // live SDR Cuts/Fixes → Emitter Summary + auto-coverage
  // Always-on SDR/DF feed: one WS subscription at the app level (not inside the
  // SDR console), so devices/LoBs/fixes/GPS + map features + auto-coverage flow
  // whether or not the console is open. SdrPanel consumes this via props.
  const sdrStream = useSdrStream({ onFeatures: setSdrFeatures, onCoverage: setSdrCoverage, onFixes: setSdrFixes })
  const [coverageRaster, setCoverageRaster] = useState(() => _s?.ui?.coverageRaster ?? false)   // per-pixel raster coverage instead of the radial sweep
  // live operator GPS fix (shown as a "you are here" marker on the 2D/3D map)
  const [gpsFix, setGpsFix] = useState(null)
  useEffect(() => {
    let stop = false
    const tick = async () => { try { const r = await getGpsFix(); if (!stop) setGpsFix(r?.fix || null) } catch { /* ignore */ } }
    tick(); const h = setInterval(tick, 4000)
    return () => { stop = true; clearInterval(h) }
  }, [])
  // SDRs whose position is the live GPS fix (use_gps + enabled) — surfaced on the
  // "you are here" marker's hover so the operator sees what's pinned to that fix.
  const gpsTrackers = useMemo(() => (sdrStream.devices || [])
    .filter(d => d.use_gps && d.enabled)
    .map(d => ({ id: d.id, name: d.name, type: d.type, position_source: d.position_source, status: d.status })),
  [sdrStream.devices])
  const [packBboxFromMap, setPackBboxFromMap] = useState(null)   // [w,s,e,n] picked by drawing a box for a pack download
  const awaitingPackBboxRef = useRef(false)

  const [coverageGeoJSON, setCoverageGeoJSON] = useState(null)
  const [p2pResult, setP2pResult] = useState(null)
  const [terrainProfile, setTerrainProfile] = useState(null)
  const [metadata, setMetadata] = useState(null)
  const [spaceWeather, setSpaceWeather] = useState(null)
  const [warnings, setWarnings] = useState([])

  const { isSimulating, setIsSimulating, progress, setProgress, abortRef } = useSimulationState()
  const [activeTab, setActiveTab] = useState(() => _s?.ui?.activeTab ?? 'coverage')
  const [bottomTab, setBottomTab] = useState(() => {
    const t = _s?.ui?.bottomTab
    // 'mapopts' tab removed → its options moved to the map ⚙; 'dbcalc'/'layers' moved to the header
    return (!t || t === 'mapopts' || t === 'dbcalc' || t === 'layers') ? 'results' : t
  })

  // ── Best site (candidates) ────────────────────────────────────────────────
  const [bestSiteResult, setBestSiteResult] = useState(null)
  const [bestSiteCandidates, setBestSiteCandidates] = useState([])

  // ── Radar ──────────────────────────────────────────────────────────────────
  const [radarResult, setRadarResult] = useState(null)

  // ── UI ────────────────────────────────────────────────────────────────────
  const [sidebarOpen, setSidebarOpen] = useState(() => _s?.ui?.sidebarOpen ?? true)
  const [bottomOpen, setBottomOpen] = useState(() => _s?.ui?.bottomOpen ?? true)
  // {id, ts}: when ts changes, the matching TransmitterPanel in the sidebar
  // expands its accordion + scrolls into view. Driven by the Emitter Summary
  // tab's "Edit" button.
  const [txEditFocus, setTxEditFocus] = useState({ id: null, ts: 0 })
  // Save State selector dialog (opened from the Layer Manager).
  const [saveStateDialogOpen, setSaveStateDialogOpen] = useState(false)
  const [savedLocations, setSavedLocations] = useState(() => _s?.savedLocations ?? [])
  const [flyToTarget, setFlyToTarget] = useState(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const [distUnit, setDistUnit] = useState(() => _s?.ui?.distUnit ?? 'imperial')
  const [coordSystem, setCoordSystem] = useState(() => _s?.ui?.coordSystem ?? 'mgrs')
  const [showCompassRose, setShowCompassRose] = useState(() => _s?.ui?.showCompassRose ?? true)
  const [mapBrightness, setMapBrightness] = useState(() => _s?.ui?.mapBrightness ?? 100)

  // ── P2P receiver ──────────────────────────────────────────────────────────
  const [rxPoint, setRxPoint] = useState(null)

  // ── Draw / tool mode ──────────────────────────────────────────────────────
  // drawMode: null | 'bounds' | 'polygon' | 'route' | 'multipoint' | 'manet'
  const [drawMode, setDrawMode] = useState(null)
  const [drawBounds, setDrawBounds] = useState(null)    // { north, south, east, west }
  const [routeWaypoints, setRouteWaypoints] = useState([])
  const [routeResult, setRouteResult] = useState(null)
  const [multipointTxs, setMultipointTxs] = useState([])
  const [multipointResult, setMultipointResult] = useState(null)
  const [polygonCoords, setPolygonCoords] = useState([])

  // ── New tool results (stored as extra GeoJSON layers) ─────────────────────
  // Each layer: { id, geojson, color? }
  const [extraGeojsonLayers, setExtraGeojsonLayers] = useState([])

  // ── Route / multipoint receiver (fixed RX for route analysis) ────────────
  const [routeReceiverPoint, setRouteReceiverPoint] = useState(null)

  // ── MANET nodes ───────────────────────────────────────────────────────────
  const [manetNodes, setManetNodes] = useState([])
  const [manetResult, setManetResult] = useState(null)
  const [manetAddingNode, setManetAddingNode] = useState(false)

  // Last-clicked map feature (kind + id/index), used by the Delete key and the trash-can header
  // button to remove the specific feature the user is interacting with. Covers the primary TX,
  // the receiver, every extra TX, multipoint TXs, MANET nodes, and P2P route waypoints. Set by
  // MapView's marker click handlers; cleared on removal or on map background click.
  const [mapSel, setMapSel] = useState(null)   // null | { kind, id? }

  // ── Best server TX sites ──────────────────────────────────────────────────
  const [bestServerSites, setBestServerSites] = useState([])
  const [bestServerResult, setBestServerResult] = useState(null)
  const [bestServerQuery, setBestServerQuery] = useState(null) // {lat,lon}

  // ── OSM Buildings layer ────────────────────────────────────────────────────
  const [buildingGeoJSON, setBuildingGeoJSON] = useState(null)

  // ── 3D terrain grid ───────────────────────────────────────────────────────
  const { terrainGrid, terrainGridLoading } = useTerrainGrid(tx, propagation, bottomTab)

  // ── Bottom panel resize ───────────────────────────────────────────────────
  const { bottomPanelHeight, setBottomPanelHeight, handleResizeMouseDown } =
    useBottomPanelResize(_s?.ui?.bottomPanelHeight ?? 240, bottomTab)

  // ── Archive ───────────────────────────────────────────────────────────────
  const [archiveOpen, setArchiveOpen] = useState(false)

  // ── Emitter placement ────────────────────────────────────────────────────
  const [txActive, setTxActive] = useState(false)  // true once user places an emitter via right-click

  // ── Geolocation / LoB ────────────────────────────────────────────────────
  const [mainMode, setMainMode] = useState(() => _s?.ui?.mainMode ?? 'propagation') // 'propagation' | 'geolocation'
  const {
    lobs, setLobs, capGroups, setCapGroups, lobAlgorithm, setLobAlgorithm,
    lobPickingMode, setLobPickingMode, pendingLobLocation, setPendingLobLocation,
    lobAzimuthPickingMode, setLobAzimuthPickingMode, pendingLobAzimuthTarget, setPendingLobAzimuthTarget,
    editLobRequestId, setEditLobRequestId, lobGroups, lobFeatures,
    handleAddLoB, handleRemoveLoB, handleUpdateLoB, handleToggleCAP,
    handleAddLoBObserver, handleAddLoBAzimuthTarget,
  } = useGeolocation(_s, useCallback(() => setMainMode('geolocation'), []))

  // GeoJSON of the DF picture (bearing wedges, Cut/Fix centroids, CAP/CEP ellipses)
  // — fed to the 3D globe so geolocation shows there too, mirroring the 2D map.
  const geolocationGeoJSON = useMemo(() => {
    const features = [...lobFeatures]
    // Live SDR/DF features (KrakenSDR / Matchstiq X40 / generic stream) — the
    // server already groups them, computes Cut/Fix/CEP and emits a FeatureCollection;
    // we just translate `properties.type` -> the `glx` tag the map renderers use.
    for (const f of (sdrFeatures || [])) {
      const t = f?.properties?.type
      const glx = t === 'lob' ? 'lob' : t === 'cep_ellipse' ? 'cap' : t === 'suspected_emitter' ? 'emitter' : null
      if (!glx) continue
      features.push({ ...f, properties: { ...f.properties, glx, source: 'sdr',
        color: f.properties.color || '#06d6a0' } })
    }
    return { type: 'FeatureCollection', features }
  }, [lobFeatures, sdrFeatures])

  // Composite layer list passed to the maps: user-drawn / API extra layers,
  // plus the live SDR auto-coverage (rendered on top of the primary coverage).
  const extraGeojsonLayersWithSdr = useMemo(() => {
    if (!sdrCoverage?.geojson) return extraGeojsonLayers
    return [...extraGeojsonLayers, {
      id: 'sdr-auto-coverage',
      name: `SDR auto-coverage · ${(sdrCoverage.frequency_hz / 1e6).toFixed(3)} MHz`,
      geojson: sdrCoverage.geojson,
      color: '#06d6a0',
    }]
  }, [extraGeojsonLayers, sdrCoverage])

  // ── Polygon BSA ───────────────────────────────────────────────────────────
  const [polygonBsaResult, setPolygonBsaResult] = useState(null)
  const [polygonBsaCoveragePct, setPolygonBsaCoveragePct] = useState(50)

  // ── Ray trace ────────────────────────────────────────────────────────────
  const [rayTraceActive, setRayTraceActive] = useState(false)

  // ── Satellite ────────────────────────────────────────────────────────────
  const [satToolActive, setSatToolActive] = useState(false)

  const menuRef = useRef(null)
  const mapImportApiRef = useRef(null)

  // Unified user-layer manager
  const ul = useUserLayers()
  const {
    terrainLineMode, setTerrainLineMode,
    standaloneProfile, setStandaloneProfile, standaloneProfileLoading, standaloneProfileError,
    handleTerrainLineComplete,
  } = useStandaloneTerrainProfile(ul, useCallback(() => setBottomTab('terrain'), []))

  // ── Restore user layers and drawn features on first mount ────────────────
  // The map and draw controller are mounted by MapView shortly after this
  // runs; restoreSnapshot defers until both are bound, so the call order is
  // safe even though the user-layer hook hasn't received the map yet.
  useEffect(() => {
    if (_s?.userLayers) {
      try { ul.restoreSnapshot(_s.userLayers) } catch {}
    }
  }, [])  // first mount only — uses session loaded at module load

  // ── Auto-save session to localStorage (debounced 1 s) ────────────────────
  const sessionJson = useMemo(() => JSON.stringify({
    version: '2.0', savedAt: new Date().toISOString(),
    primaryTransmitter: tx, extraTransmitters: extraTxList,
    receiver: rx, propagation, atmosphere,
    savedLocations, lobs, capGroups, lobAlgorithm,
    userLayers: ul.exportSession(),
    ui: {
      txLabel, distUnit, coordSystem, showCompassRose, mapBrightness,
      sidebarOpen, bottomOpen, bottomTab, activeTab, mainMode, bottomPanelHeight, coverageRaster,
    },
  }), [
    tx, extraTxList, rx, propagation, atmosphere, savedLocations, lobs, capGroups, lobAlgorithm,
    txLabel, distUnit, coordSystem, showCompassRose, mapBrightness,
    sidebarOpen, bottomOpen, bottomTab, activeTab, mainMode, bottomPanelHeight, coverageRaster,
    ul.layers, ul.drawnFeatures,
  ])
  useSessionAutosave(SESSION_KEY, sessionJson)

  // Close overflow menu on outside click
  useEffect(() => {
    if (!menuOpen) return
    const handler = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [menuOpen])

  // ── Select-all on number focus ────────────────────────────────────────────
  useNumberFieldSelectAll()

  // ── Fetch space weather on mount ──────────────────────────────────────────
  useEffect(() => {
    getSpaceWeather()
      .then(d => setSpaceWeather(d.data))
      .catch(() => {})
  }, [])

  // ── Auto-select propagation model (model = "auto") ───────────────────────
  const resolveModelFast = makeResolveModelFast(rx, activeTab)

  // ── Map click handler ────────────────────────────────────────────────────
  const handleMapClick = useCallback((lat, lon, isRx = false) => {
    // SDR device-location picking (the SDR console is hidden while we wait for the click)
    if (sdrPickCbRef.current) {
      const cb = sdrPickCbRef.current
      sdrPickCbRef.current = null
      setSdrPicking(false)
      cb(lat, lon)
      return
    }
    // LoB observer location picking
    if (lobPickingMode) {
      setPendingLobLocation({ lat, lon })
      setLobPickingMode(false)
      return
    }
    // LoB azimuth target picking
    if (lobAzimuthPickingMode) {
      setPendingLobAzimuthTarget({ lat, lon })
      setLobAzimuthPickingMode(false)
      return
    }
    // Route / multipoint: set the fixed receiver
    if ((activeTab === 'route' || activeTab === 'multipoint') && isRx) {
      setRouteReceiverPoint({ lat, lon })
      return
    }
    // Best server: query point
    if (activeTab === 'best_server' && isRx) {
      setBestServerQuery({ lat, lon })
      return
    }
    if (activeTab === 'p2p' && isRx) {
      setRxPoint({ lat, lon })
      return
    }
    if (activeTab === 'best_site') {
      setBestSiteCandidates(prev => [
        ...prev,
        { lat, lon, height_m: tx.height_m, label: `Site ${prev.length + 1}` },
      ])
      return
    }
    if (manetAddingNode) {
      const id = Date.now()
      setManetNodes(prev => [...prev, {
        id, lat, lon, height_m: tx.height_m,
        label: `Node ${prev.length + 1}`,
      }])
      setManetAddingNode(false)
      return
    }
    setTx(prev => ({ ...prev, lat, lon }))
  }, [activeTab, tx.height_m, manetAddingNode, lobPickingMode, lobAzimuthPickingMode])

  // SDR console asks to set a device position from the map: stash the callback,
  // hide the console (it stays mounted, so the in-progress edit survives), and the
  // next map click feeds the coords back (see handleMapClick).
  const requestSdrLocationPick = useCallback((cb) => {
    sdrPickCbRef.current = cb
    setSdrPicking(true)
    toast.info('Click the map to set the device location (Esc to cancel)')
  }, [])
  const cancelSdrPick = useCallback(() => { sdrPickCbRef.current = null; setSdrPicking(false) }, [])
  useEffect(() => {
    if (!sdrPicking) return
    const onKey = (e) => { if (e.key === 'Escape') cancelSdrPick() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [sdrPicking, cancelSdrPick])

  // Existing map points an SDR device position can attach to (primary TX, extra
  // emitters, drawn point features).
  const sdrMapFeatures = useMemo(() => {
    const out = []
    if (txActive && tx?.lat != null) out.push({ label: txLabel || 'Primary TX', lat: tx.lat, lon: tx.lon })
    for (const e of (extraTxList || [])) {
      const la = e.tx?.lat ?? e.lat, lo = e.tx?.lon ?? e.lon
      if (la != null && lo != null) out.push({ label: e.label || 'Emitter', lat: la, lon: lo })
    }
    for (const f of (ul?.drawnGeoJSON?.features || [])) {
      if (f?.geometry?.type === 'Point') {
        const c = f.geometry.coordinates
        out.push({ label: f.properties?.name || 'Drawn point', lat: c[1], lon: c[0] })
      }
    }
    return out
  }, [txActive, txLabel, tx?.lat, tx?.lon, extraTxList, ul?.drawnGeoJSON])

  // ── Draw complete callback (from MapView) ─────────────────────────────────
  const handleDrawComplete = useCallback((type, data) => {
    if (type === 'bounds' && awaitingPackBboxRef.current) {
      // a box drawn to pick the area for an offline-pack download (Layer Manager →
      // RegionDownloadPanel). `awaitingPackBboxRef.current` is the panel id that asked
      // for the bbox; we re-open it once the rectangle is in.
      awaitingPackBboxRef.current = false
      setPackBboxFromMap([data.west, data.south, data.east, data.north])
      setDrawMode(null)
      setLayersOpen(true)
      toast.info(`Region selected: ${data.west.toFixed(3)},${data.south.toFixed(3)} → ${data.east.toFixed(3)},${data.north.toFixed(3)}`)
      return
    }
    if (type === 'bounds') {
      setDrawBounds(data)
      setDrawMode(null)
      toast.info(`Bounds set: ${data.north.toFixed(3)}N, ${data.south.toFixed(3)}S, ${data.east.toFixed(3)}E, ${data.west.toFixed(3)}W`)
    } else if (type === 'polygon') {
      setPolygonCoords(data)
      setDrawMode(null)
      toast.info(`Polygon drawn: ${data.length} vertices — click Run to compute best site`)
    } else if (type === 'route') {
      setRouteWaypoints(data)
    } else if (type === 'route_finish') {
      setRouteWaypoints(data)
      setDrawMode(null)
      toast.info(`Route set: ${data.length} waypoints`)
    } else if (type === 'multipoint') {
      setMultipointTxs(data)
    } else if (type === 'multipoint_finish') {
      setMultipointTxs(data)
      setDrawMode(null)
      toast.info(`${data.length} TX points added`)
    } else if (type === 'manet') {
      const id = Date.now()
      setManetNodes(prev => [...prev, {
        id, lat: data.lat, lon: data.lon, height_m: tx.height_m,
        label: `Node ${prev.length + 1}`,
      }])
    }
  }, [tx.height_m])

  // ── Saved-location handlers ──────────────────────────────────
  const handleSaveLocation = useCallback((loc) => {
    setSavedLocations(prev => [...prev, { ...loc, id: Date.now() }])
  }, [])

  const handleRemoveSavedLocation = useCallback((id) => {
    setSavedLocations(prev => prev.filter(l => l.id !== id))
  }, [])

  // ── Right-click map placement ─────────────────────────────────────────────
  const handleAddEmitter = useCallback((lat, lon) => {
    setTx(prev => ({ ...prev, lat, lon }))
    setTxActive(true)
    setMainMode('propagation')
  }, [])

  // ── upsertLayer helper ────────────────────────────────────────────────────
  const upsertLayer = useCallback((id, geojson, color) => {
    setExtraGeojsonLayers(prev => {
      const filtered = prev.filter(l => l.id !== id)
      if (!geojson) return filtered
      return [...filtered, { id, geojson, color }]
    })
  }, [])

  // ── Undo / Redo ───────────────────────────────────────────────────────────
  // Snapshot a curated subset of state. Stack-based, 400 ms debounce so a
  // burst of small changes collapses into one undo step. Cap at 50 entries.
  const undoStackRef = useRef([])
  const redoStackRef = useRef([])
  const lastSnapRef = useRef(null)
  const skipRecordRef = useRef(false)
  const undoDebounceRef = useRef(null)
  const [undoTick, setUndoTick] = useState(0)  // bump to re-render menu's disabled state

  const undoState = useMemo(() => ({
    tx, extraTxList, rx, propagation, atmosphere,
    savedLocations, lobs, capGroups, lobAlgorithm,
  }), [tx, extraTxList, rx, propagation, atmosphere, savedLocations, lobs, capGroups, lobAlgorithm])

  const restoreUndoSnapshot = useCallback((s) => {
    if (s.tx) setTx(s.tx)
    if (s.extraTxList) setExtraTxList(s.extraTxList)
    if (s.rx !== undefined) setRx(s.rx)
    if (s.propagation) setPropagation(s.propagation)
    if (s.atmosphere) setAtmosphere(s.atmosphere)
    if (s.savedLocations) setSavedLocations(s.savedLocations)
    if (s.lobs) setLobs(s.lobs)
    if (s.capGroups) setCapGroups(s.capGroups)
    if (s.lobAlgorithm) setLobAlgorithm(s.lobAlgorithm)
  }, [])

  useEffect(() => {
    const ser = JSON.stringify(undoState)
    if (lastSnapRef.current === null) { lastSnapRef.current = ser; return }
    if (skipRecordRef.current) { skipRecordRef.current = false; lastSnapRef.current = ser; return }
    clearTimeout(undoDebounceRef.current)
    undoDebounceRef.current = setTimeout(() => {
      if (ser === lastSnapRef.current) return
      undoStackRef.current.push(lastSnapRef.current)
      if (undoStackRef.current.length > 50) undoStackRef.current.shift()
      redoStackRef.current = []
      lastSnapRef.current = ser
      setUndoTick(t => t + 1)
    }, 400)
  }, [undoState])

  const flushPendingUndo = useCallback(() => {
    if (!undoDebounceRef.current) return
    clearTimeout(undoDebounceRef.current)
    undoDebounceRef.current = null
    const ser = JSON.stringify(undoState)
    if (ser !== lastSnapRef.current) {
      undoStackRef.current.push(lastSnapRef.current)
      if (undoStackRef.current.length > 50) undoStackRef.current.shift()
      redoStackRef.current = []
      lastSnapRef.current = ser
    }
  }, [undoState])

  const undo = useCallback(() => {
    flushPendingUndo()
    if (undoStackRef.current.length === 0) return
    const prev = undoStackRef.current.pop()
    redoStackRef.current.push(lastSnapRef.current)
    skipRecordRef.current = true
    lastSnapRef.current = prev
    restoreUndoSnapshot(JSON.parse(prev))
    setUndoTick(t => t + 1)
  }, [flushPendingUndo, restoreUndoSnapshot])

  const redo = useCallback(() => {
    flushPendingUndo()
    if (redoStackRef.current.length === 0) return
    const next = redoStackRef.current.pop()
    undoStackRef.current.push(lastSnapRef.current)
    skipRecordRef.current = true
    lastSnapRef.current = next
    restoreUndoSnapshot(JSON.parse(next))
    setUndoTick(t => t + 1)
  }, [flushPendingUndo, restoreUndoSnapshot])

  useEffect(() => {
    const onKey = (e) => {
      const mod = e.ctrlKey || e.metaKey
      if (!mod) return
      // Ignore when typing in an input/textarea/contentEditable
      const tag = (e.target?.tagName || '').toLowerCase()
      const editable = tag === 'input' || tag === 'textarea' || e.target?.isContentEditable
      if (e.key === 'z' || e.key === 'Z') {
        if (editable) return
        e.preventDefault(); undo()
      } else if (e.key === 'r' || e.key === 'R') {
        // Note: this overrides the browser refresh shortcut. F5 still refreshes.
        e.preventDefault(); redo()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [undo, redo])

  // Delete / Backspace removes the last-clicked map feature. Priority is the map-feature selection
  // (primary TX / RX / extra TX / multipoint TX / MANET node / P2P route waypoint), falling back
  // to the useUserLayers selection (imported KML/GeoJSON layers and drawn features).
  const removeSelectedMapFeature = useCallback(() => {
    if (!mapSel) return null
    switch (mapSel.kind) {
      case 'primary_tx': setTxActive(false); setMapSel(null); return 'Primary TX'
      case 'rx':         setRxPoint(null); setMapSel(null); return 'Receiver'
      case 'extra_tx':   setExtraTxList(prev => prev.filter(e => e.id !== mapSel.id)); setMapSel(null); return 'Extra TX'
      case 'multipoint_tx':
        setMultipointTxs(prev => prev.filter((_, i) => i !== mapSel.id)); setMapSel(null); return 'Multipoint TX'
      case 'manet_node': setManetNodes(prev => prev.filter(n => n.id !== mapSel.id)); setMapSel(null); return 'MANET node'
      case 'route_waypoint':
        setRouteWaypoints(prev => prev.filter((_, i) => i !== mapSel.id)); setMapSel(null); return 'Route waypoint'
      default: return null
    }
  }, [mapSel])

  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return
      const tag = (e.target?.tagName || '').toLowerCase()
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target?.isContentEditable) return
      // 1) try the map-feature selection (TX / RX / multipoint / MANET / waypoint)
      const removed = removeSelectedMapFeature()
      if (removed) { e.preventDefault(); toast.info(`Removed ${removed}`); return }
      // 2) fall back to useUserLayers (imported KML/GeoJSON layers + drawn features)
      const sel = ul.getSelectedFeature?.()
      if (!sel) return
      e.preventDefault()
      const kind = ul.removeSelected?.()
      if (kind) toast.info(
        kind === 'drawn'   ? 'Removed the drawn feature'
        : kind === 'feature' ? 'Removed the feature'
        : 'Removed the map layer')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [ul.getSelectedFeature, ul.removeSelected, removeSelectedMapFeature])

  // Drop a stale mapSel when its target no longer exists (sidebar Remove, draw re-run, etc.).
  useEffect(() => {
    if (!mapSel) return
    const exists = (() => {
      switch (mapSel.kind) {
        case 'primary_tx':     return txActive
        case 'rx':             return !!rxPoint
        case 'extra_tx':       return extraTxList.some(e => e.id === mapSel.id)
        case 'multipoint_tx':  return mapSel.id < multipointTxs.length
        case 'manet_node':     return manetNodes.some(n => n.id === mapSel.id)
        case 'route_waypoint': return mapSel.id < routeWaypoints.length
        default: return false
      }
    })()
    if (!exists) setMapSel(null)
  }, [mapSel, txActive, rxPoint, extraTxList, multipointTxs, manetNodes, routeWaypoints])

  // ── Save / Load state ─────────────────────────────────────────────────────
  // The downloaded .json carries the *whole* scene — literally everything on the map. Beyond
  // emitters/receiver/propagation/atmosphere, this covers: imported KMZ/KML/GeoJSON/imagery/
  // tile-source/terrain-grid layers + drawings (via ul.exportSession), the propagation coverage
  // + building footprints, every analysis input/result (best-site, route, multipoint, MANET,
  // best-server, BSA polygon, P2P, radar, terrain profile, standalone terrain profile), the SDR
  // live overlay (features + auto-coverage), LoBs/DF, saved locations, the map view (centre +
  // zoom), and the UI prefs that affect what's visible (brightness, compass, units, coord
  // system, sidebar/bottom-panel collapse).
  // Build the full session blob, optionally filtered by `sel` (a partial flag map).
  // Pass sel=null/undefined to include everything (the default — preserves the
  // existing one-shot "Save State" behavior from the File menu). Each flag drops
  // a *whole* slice; per-emitter / per-LoB granularity isn't on offer because
  // the slices are entangled (propagation references the emitter that produced
  // it, etc.) and "save subset" is most useful for sharing a clean scenario.
  const handleSaveState = useCallback((sel) => {
    const include = (k) => sel == null || sel[k] !== false
    let userLayers = null
    try { userLayers = include('layers') ? ul.exportSession() : null } catch { userLayers = null }
    let mapView = null
    try { mapView = include('mapView') ? (mapImportApiRef.current?.getView?.() ?? null) : null } catch { mapView = null }
    const state = {
      version: '2.2', savedAt: new Date().toISOString(),
      ...(include('emitters') && {
        primaryTransmitter: tx, extraTransmitters: extraTxList,
        receiver: rx, propagation, atmosphere,
      }),
      ...(include('savedLocations') && { savedLocations }),
      ...(include('lobs') && { lobs, capGroups, lobAlgorithm }),
      ...(userLayers != null && { userLayers }),
      ...(include('coverage') && {
        coverage: { geojson: coverageGeoJSON, metadata, buildings: buildingGeoJSON, warnings },
      }),
      ...(include('analyses') && {
        analyses: {
          p2pResult, terrainProfile, radarResult,
          bestSiteCandidates, bestSiteResult,
          routeWaypoints, routeReceiverPoint, routeResult,
          multipointTxs, multipointResult,
          manetNodes, manetResult,
          bestServerSites, bestServerQuery, bestServerResult,
          polygonCoords, polygonBsaCoveragePct, polygonBsaResult,
          drawBounds,
          standaloneProfile,
        },
      }),
      ...(include('sdr') && { sdr: { features: sdrFeatures, coverage: sdrCoverage } }),
      ...(mapView != null && { mapView }),
      ...(include('ui') && {
        ui: { txLabel, activeTab, mainMode, bottomTab, coverageRaster,
              mapBrightness, showCompassRose, distUnit, coordSystem,
              sidebarOpen, bottomOpen },
      }),
    }
    const blob = new Blob([JSON.stringify(state, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `ares-state-${new Date().toISOString().slice(0, 10)}.json`
    a.click()
    URL.revokeObjectURL(url)
    if (sel == null) {
      toast.success('State saved (everything on the map + propagation/geolocation analyses)')
    } else {
      const kept = Object.entries(sel).filter(([, v]) => v !== false).length
      toast.success(`State saved (${kept} section${kept === 1 ? '' : 's'} included)`)
    }
  }, [tx, extraTxList, rx, propagation, atmosphere, savedLocations, lobs, capGroups, lobAlgorithm,
      ul, coverageGeoJSON, metadata, buildingGeoJSON, warnings, p2pResult, terrainProfile, radarResult,
      bestSiteCandidates, bestSiteResult, routeWaypoints, routeReceiverPoint, routeResult,
      multipointTxs, multipointResult, manetNodes, manetResult, bestServerSites, bestServerQuery,
      bestServerResult, polygonCoords, polygonBsaCoveragePct, polygonBsaResult, drawBounds,
      standaloneProfile, sdrFeatures, sdrCoverage,
      txLabel, activeTab, mainMode, bottomTab, coverageRaster,
      mapBrightness, showCompassRose, distUnit, coordSystem, sidebarOpen, bottomOpen])

  const handleLoadState = useCallback(() => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = '.json'
    input.onchange = e => {
      const file = e.target.files[0]
      if (!file) return
      const reader = new FileReader()
      reader.onload = ev => {
        try {
          const state = JSON.parse(ev.target.result)
          if (state.primaryTransmitter) setTx(p => ({ ...p, ...state.primaryTransmitter }))
          if (state.extraTransmitters) setExtraTxList(state.extraTransmitters)
          if (state.receiver) setRx(p => ({ ...p, ...state.receiver }))
          if (state.propagation) setPropagation(p => ({ ...p, ...state.propagation }))
          if (state.atmosphere) setAtmosphere(p => ({ ...p, ...state.atmosphere }))
          if (state.savedLocations) setSavedLocations(state.savedLocations)
          // Geolocation state — v2.0+
          if (state.lobs) setLobs(state.lobs.map(l => ({
            device_type: '', device_id: '', environment: 'suburban', clutter_height_m: 0, ...l  // fill any missing fields
          })))
          if (state.capGroups) setCapGroups(state.capGroups)
          if (state.lobAlgorithm) setLobAlgorithm(prev => ({
            ...prev,
            ...state.lobAlgorithm,
            step: { ...prev.step, ...(state.lobAlgorithm.step || {}) },
            fixed: { ...prev.fixed, ...(state.lobAlgorithm.fixed || {}) },
          }))
          // Imported KMZ/KML/GeoJSON/imagery/tiles/terrain-grid layers + drawings — v2.1+
          if (state.userLayers) { try { ul.restoreSnapshot(state.userLayers) } catch { /* noop */ } }
          // Propagation coverage + buildings — v2.1+
          if (state.coverage) {
            setCoverageGeoJSON(state.coverage.geojson ?? null)
            setMetadata(state.coverage.metadata ?? null)
            setBuildingGeoJSON(state.coverage.buildings ?? null)
            setWarnings(state.coverage.warnings ?? [])
          }
          // Analysis inputs + results — v2.1+
          const a = state.analyses || {}
          if ('p2pResult' in a) setP2pResult(a.p2pResult ?? null)
          if ('terrainProfile' in a) setTerrainProfile(a.terrainProfile ?? null)
          if ('radarResult' in a) setRadarResult(a.radarResult ?? null)
          if (a.bestSiteCandidates) setBestSiteCandidates(a.bestSiteCandidates)
          if ('bestSiteResult' in a) setBestSiteResult(a.bestSiteResult ?? null)
          if (a.routeWaypoints) setRouteWaypoints(a.routeWaypoints)
          if ('routeReceiverPoint' in a) setRouteReceiverPoint(a.routeReceiverPoint ?? null)
          if ('routeResult' in a) setRouteResult(a.routeResult ?? null)
          if (a.multipointTxs) setMultipointTxs(a.multipointTxs)
          if ('multipointResult' in a) setMultipointResult(a.multipointResult ?? null)
          if (a.manetNodes) setManetNodes(a.manetNodes)
          if ('manetResult' in a) setManetResult(a.manetResult ?? null)
          if (a.bestServerSites) setBestServerSites(a.bestServerSites)
          if ('bestServerQuery' in a) setBestServerQuery(a.bestServerQuery ?? null)
          if ('bestServerResult' in a) setBestServerResult(a.bestServerResult ?? null)
          if (a.polygonCoords) setPolygonCoords(a.polygonCoords)
          if (a.polygonBsaCoveragePct != null) setPolygonBsaCoveragePct(a.polygonBsaCoveragePct)
          if ('polygonBsaResult' in a) setPolygonBsaResult(a.polygonBsaResult ?? null)
          if ('drawBounds' in a) setDrawBounds(a.drawBounds ?? null)
          if ('standaloneProfile' in a) setStandaloneProfile(a.standaloneProfile ?? null)
          // SDR live overlay (features + auto-coverage) — v2.2+
          if (state.sdr) {
            if (Array.isArray(state.sdr.features)) setSdrFeatures(state.sdr.features)
            if ('coverage' in state.sdr) setSdrCoverage(state.sdr.coverage ?? null)
          }
          // Map view (centre + zoom) — v2.2+. Defer one tick so the leaflet instance exists.
          if (state.mapView) {
            const v = state.mapView
            setTimeout(() => { try { mapImportApiRef.current?.setView?.(v) } catch { /* noop */ } }, 0)
          }
          if (state.ui) {
            if (state.ui.txLabel) setTxLabel(state.ui.txLabel)
            if (state.ui.activeTab) setActiveTab(state.ui.activeTab)
            if (state.ui.mainMode) setMainMode(state.ui.mainMode)
            if (state.ui.bottomTab) setBottomTab(state.ui.bottomTab)
            if (typeof state.ui.coverageRaster === 'boolean') setCoverageRaster(state.ui.coverageRaster)
            // v2.2+: visible-on-map prefs
            if (typeof state.ui.mapBrightness === 'number') setMapBrightness(state.ui.mapBrightness)
            if (typeof state.ui.showCompassRose === 'boolean') setShowCompassRose(state.ui.showCompassRose)
            if (typeof state.ui.distUnit === 'string') setDistUnit(state.ui.distUnit)
            if (typeof state.ui.coordSystem === 'string') setCoordSystem(state.ui.coordSystem)
            if (typeof state.ui.sidebarOpen === 'boolean') setSidebarOpen(state.ui.sidebarOpen)
            if (typeof state.ui.bottomOpen === 'boolean') setBottomOpen(state.ui.bottomOpen)
          }
          toast.success('State loaded')
        } catch (err) {
          toast.error('Invalid state file: ' + (err?.message || err))
        }
      }
      reader.readAsText(file)
    }
    input.click()
  }, [ul])

  // ── Simulate Propagation from a fix/cut ───────────────────────────────────
  // Operator right-clicks a fix/cut centroid (or hits the button in the
  // Emitter Summary table) → we attach a "tracking" extra TX whose location
  // mirrors the centroid as new LoBs refine it. Identified by a stable key
  // derived from frequency + device_id so re-invoking with the same group
  // doesn't spawn duplicates.
  const fixKey = useCallback((g) => `fix:${Math.round((g?.frequency_hz || 0))}:${g?.device_id || ''}`, [])

  const handleSimulatePropagationFromFix = useCallback((groupSummary, lat, lon, opts = {}) => {
    if (!groupSummary || lat == null || lon == null) return
    const key = fixKey(groupSummary)
    let created = false
    setExtraTxList((prev) => {
      // Already tracking this group → reuse, just nudge its lat/lon (lobGroups
      // effect below would handle this on the next tick anyway).
      const existing = prev.find((x) => x.trackingFixKey === key)
      if (existing) {
        return prev.map((x) => x.id === existing.id
          ? { ...x, tx: { ...x.tx, lat, lon } }
          : x)
      }
      created = true
      const idx = prev.length
      const color = TX_COLORS[idx % TX_COLORS.length]
      const id = Date.now() + idx
      return [...prev, {
        id, color,
        label: `${groupSummary.kind?.toUpperCase() || 'FIX'} · ${(groupSummary.frequency_hz / 1e6).toFixed(3)} MHz`,
        trackingFixKey: key,
        origin: 'df_head',                       // distinguishes from algorithm-tab fixes on the map
        tx: { ...tx, lat, lon, frequency_hz: groupSummary.frequency_hz || tx.frequency_hz },
        propagation: { ...propagation },
        atmosphere: { ...atmosphere },
      }]
    })
    // `silent` (auto-coverage path) doesn't yank the operator into propagation
    // mode or toast on every fix update — it just keeps the tracking emitter live.
    if (opts.silent) return
    setMainMode('propagation')
    toast.success(`Tracking propagation from ${groupSummary.kind?.toUpperCase() || 'FIX'} @ ${(groupSummary.frequency_hz / 1e6).toFixed(3)} MHz`)
  }, [tx, propagation, atmosphere, fixKey])

  // Auto-coverage: when enabled (toggle lives in the Emitter Summary table),
  // spin up a tracking propagation emitter for every geolocated emitter (≥2 LoBs
  // with an intersection) so coverage follows each fix automatically. The handler
  // is idempotent — existing trackers are just nudged — so this is safe to re-run
  // on each lobGroups change, and `silent` keeps it from hijacking the UI.
  const [autoCoverage, setAutoCoverage] = useState(false)
  useEffect(() => {
    if (!autoCoverage) return
    // geolocation-tool emitters (manual / picked LoBs)
    for (const grp of lobGroups) {
      if (!grp || grp.lobs.length < 2) continue
      const c = computeCentroid(computeGroupIntersections(grp))
      if (!c) continue
      handleSimulatePropagationFromFix({
        frequency_hz: grp.frequency_hz, device_id: grp.device_id || '',
        device_type: grp.device_type || '', n_lobs: grp.lobs.length,
        kind: grp.lobs.length >= 3 ? 'fix' : 'cut',
      }, c.lat, c.lon, { silent: true })
    }
    // live SDR Cuts/Fixes streaming from the DF hardware (via the SDR console)
    for (const fx of sdrFixes) {
      if (!fx?.centroid) continue
      handleSimulatePropagationFromFix({
        frequency_hz: fx.frequency_hz, device_id: '', device_type: 'sdr',
        n_lobs: fx.n_lobs, kind: fx.kind || 'fix',
      }, fx.centroid.lat, fx.centroid.lon, { silent: true })
    }
  }, [autoCoverage, lobGroups, sdrFixes, handleSimulatePropagationFromFix])

  // Algorithms tab → "Send fix to map". Drops a distinct algorithm-origin
  // emitter so the map can visually differentiate it from DF-head fixes.
  const handleSendAlgorithmFixToMap = useCallback((fix) => {
    if (!fix || fix.lat == null || fix.lon == null) return
    setExtraTxList((prev) => {
      const idx = prev.length
      const color = TX_COLORS[idx % TX_COLORS.length]
      const id = Date.now() + idx
      return [...prev, {
        id, color,
        label: fix.label || `Algo: ${fix.method_name || fix.method_id || 'fix'}`,
        origin: 'algorithm',                     // distinguishes from DF-head fixes on the map
        algorithm_method_id: fix.method_id,
        algorithm_cep_m: fix.cep_m,
        tx: { ...tx, lat: fix.lat, lon: fix.lon },
        propagation: { ...propagation },
        atmosphere: { ...atmosphere },
      }]
    })
    toast.success(`Algorithm fix → map: ${fix.method_name || fix.method_id}${fix.cep_m ? ` (CEP ${Math.round(fix.cep_m)} m)` : ''}`)
  }, [tx, propagation, atmosphere])

  // Keep every tracking extra-TX glued to its current centroid as lobGroups
  // updates. Cheap diff — only touches TXs whose position actually changed.
  useEffect(() => {
    if (!extraTxList.some((x) => x.trackingFixKey)) return
    setExtraTxList((prev) => {
      let changed = false
      const next = prev.map((x) => {
        if (!x.trackingFixKey) return x
        const grp = lobGroups.find((g) => fixKey(g) === x.trackingFixKey)
        if (!grp) return x
        const ints = computeGroupIntersections(grp)
        const c = computeCentroid(ints)
        if (!c) return x
        if (Math.abs((x.tx.lat ?? 0) - c.lat) < 1e-7 && Math.abs((x.tx.lon ?? 0) - c.lon) < 1e-7) return x
        changed = true
        return { ...x, tx: { ...x.tx, lat: c.lat, lon: c.lon } }
      })
      return changed ? next : prev
    })
  }, [lobGroups, extraTxList.length, fixKey])

  // ── Multi-TX management ───────────────────────────────────────────────────
  const addTransmitter = useCallback(() => {
    const id = Date.now()
    const color = TX_COLORS[extraTxList.length % TX_COLORS.length]
    setExtraTxList(prev => [...prev, {
      id, color, label: `TX ${prev.length + 2}`,
      tx: { ...tx, lat: tx.lat + 0.01, lon: tx.lon + 0.01 },
      propagation: { ...propagation },
      atmosphere: { ...atmosphere },
    }])
  }, [tx, propagation, atmosphere, extraTxList.length])

  const removeTransmitter = useCallback((id) => {
    setExtraTxList(prev => prev.filter(x => x.id !== id))
  }, [])

  const updateExtraTx = useCallback((id, updater) => {
    setExtraTxList(prev => prev.map(x => {
      if (x.id !== id) return x
      return { ...x, tx: typeof updater === 'function' ? updater(x.tx) : updater }
    }))
  }, [])

  // "Edit this emitter" signal from the Emitter Summary tab: open the sidebar
  // (if collapsed) and tell the matching TransmitterPanel to expand + scroll
  // into view. The TransmitterPanel watches `expandSignal` (a timestamp) — any
  // change triggers the open+scroll. Bumping ts re-fires even if the same
  // emitter is requested twice in a row.
  const handleEditEmitter = useCallback((emitterId) => {
    if (!emitterId) return
    setSidebarOpen(true)
    setTxEditFocus({ id: emitterId, ts: Date.now() })
  }, [])

  const updateExtraPropagation = useCallback((id, updater) => {
    setExtraTxList(prev => prev.map(x => {
      if (x.id !== id) return x
      return { ...x, propagation: typeof updater === 'function' ? updater(x.propagation) : updater }
    }))
  }, [])

  const updateExtraAtmosphere = useCallback((id, updater) => {
    setExtraTxList(prev => prev.map(x => {
      if (x.id !== id) return x
      return { ...x, atmosphere: typeof updater === 'function' ? updater(x.atmosphere) : updater }
    }))
  }, [])

  const renameExtraTx = useCallback((id, newLabel) => {
    setExtraTxList(prev => prev.map(x => x.id === id ? { ...x, label: newLabel } : x))
  }, [])

  // ── Clear all layers ──────────────────────────────────────────────────────
  // Trash-can in the header — wipes everything visible on the map: imported KMZ/KML/GeoJSON/
  // imagery/tile overlays, drawings, propagation coverage + every analysis result/input array,
  // the SDR live overlay, buildings, LoBs / DF, AND the primary TX / receiver / extra TXs.
  // The persistent offline data-pack library is left alone (it survives sessions on disk).
  // For surgical single-feature removal, click the feature on the map and press Delete.
  const handleClearAll = useCallback(() => {
    try { ul.clearAll(); ul.clearDrawn() } catch { /* noop */ }
    setCoverageGeoJSON(null); setMetadata(null); setP2pResult(null)
    setTerrainProfile(null); setStandaloneProfile(null); setWarnings([])
    setBestSiteResult(null); setRadarResult(null); setManetResult(null)
    setPolygonBsaResult(null); setBestServerResult(null); setRouteResult(null); setMultipointResult(null)
    setBuildingGeoJSON(null)
    setExtraGeojsonLayers([])
    setExtraTxList([])
    setTxActive(false); setRxPoint(null)
    setSdrFeatures([]); setSdrCoverage(null)
    setLobs([]); setCapGroups({})
    setBestSiteCandidates([]); setRouteWaypoints([]); setMultipointTxs([]); setManetNodes([]); setBestServerSites([]); setPolygonCoords([]); setDrawBounds(null)
    setDrawMode(null); setMapSel(null)
    if (ul.getSelectedFeature?.()) ul.selectFeature?.(null)
    toast.info('Cleared all layers, drawings, results, emitters & LoBs')
  }, [ul])

  // ── Coverage simulation ───────────────────────────────────────────────────
  const runCoverage = useCallback(async () => {
    setIsSimulating(true)
    setProgress(10)
    setCoverageGeoJSON(null)
    setMetadata(null)
    setWarnings([])

    const resolvedModel = resolveModelFast(tx, propagation)
    const baseParams = {
      receiver: rx,
      propagation_model: resolvedModel,
      wave_type: propagation.wave_type,
      radius_km: propagation.radius_km,
      num_radials: propagation.num_radials,
      points_per_radial: propagation.points_per_radial,
      min_signal_dbm: propagation.min_signal_dbm,
      atmosphere,
      use_gpu: propagation.use_gpu,
      terrain_resolution: propagation.terrain_resolution,
      include_buildings: propagation.include_buildings,
      fetch_space_weather: propagation.fetch_space_weather,
      context: propagation.context ?? 2,
      diffraction_model: propagation.diffraction_model ?? 'none',
      rcs_m2: propagation.rcs_m2 ?? 1.0,
      clutter_height_m: propagation.clutter_height_m ?? 0,
      polar_pattern: tx.antenna.polar_pattern ?? 'omni',
      polar_peak_gain_dbi: tx.antenna.polar_peak_gain_dbi ?? null,
      sweep_deg: tx.antenna.sweep_deg ?? 0,
      buildings_radius_m: propagation.buildings_radius_m ?? 500,
    }

    try {
      setProgress(20)
      const result = await (coverageRaster
        ? simulateCoverageRaster({ transmitter: { ...tx, frequency_hz: Number(tx.frequency_hz) }, ...baseParams }, 72)
        : simulateCoverage({ transmitter: { ...tx, frequency_hz: Number(tx.frequency_hz) }, ...baseParams }))
      setCoverageGeoJSON(result.geojson)
      setMetadata(result.metadata)
      if (result.metadata?.space_weather) setSpaceWeather(result.metadata.space_weather)
      if (result.metadata?.warnings?.length > 0) {
        setWarnings(result.metadata.warnings)
        result.metadata.warnings.forEach(w => toast.warn(w, { autoClose: 8000 }))
      }

      // Fetch OSM building footprints for map overlay if requested
      if (propagation.include_buildings && propagation.show_buildings_layer) {
        try {
          const bldg = await getBuildings(tx.lat, tx.lon, propagation.buildings_radius_m ?? 500)
          setBuildingGeoJSON(bldg.geojson)
        } catch (_) { /* non-fatal */ }
      } else {
        setBuildingGeoJSON(null)
      }

      if (extraTxList.length > 0) {
        setProgress(60)
        const extraResults = await Promise.allSettled(
          extraTxList.map(e => {
            const ep = e.propagation ?? propagation
            return simulateCoverage({
              transmitter: { ...e.tx, frequency_hz: Number(e.tx.frequency_hz) },
              receiver: rx,
              propagation_model: resolveModelFast(e.tx, ep),
              wave_type: ep.wave_type,
              radius_km: ep.radius_km,
              num_radials: ep.num_radials,
              points_per_radial: ep.points_per_radial,
              min_signal_dbm: ep.min_signal_dbm,
              atmosphere: e.atmosphere ?? atmosphere,
              use_gpu: ep.use_gpu,
              terrain_resolution: ep.terrain_resolution,
              include_buildings: ep.include_buildings,
              fetch_space_weather: false,
            })
          })
        )
        setExtraTxList(prev => prev.map((e, i) => ({
          ...e,
          geojson: extraResults[i].status === 'fulfilled' ? extraResults[i].value.geojson : null,
        })))
      }

      setProgress(100)
      const modelLabel = resolvedModel === propagation.model ? resolvedModel : `${resolvedModel} (auto)`
      toast.success(`Coverage computed in ${result.metadata?.computation_time_s?.toFixed(1)}s — model: ${modelLabel}`)
    } catch (err) {
      const detail = errDetail(err)
      toast.error(`Simulation failed: ${detail}`, { autoClose: 10000 })
    } finally {
      setIsSimulating(false)
      setTimeout(() => setProgress(0), 1000)
    }
  }, [tx, rx, propagation, atmosphere, extraTxList, coverageRaster])

  // ── P2P simulation ────────────────────────────────────────────────────────
  const runP2P = useCallback(async () => {
    if (!rxPoint) {
      toast.info('Click on the map to set the receiver location')
      return
    }
    setIsSimulating(true)
    setProgress(20)
    setP2pResult(null)
    setTerrainProfile(null)

    try {
      const result = await simulateP2P({
        transmitter: { ...tx, frequency_hz: Number(tx.frequency_hz) },
        receiver_lat: rxPoint.lat,
        receiver_lon: rxPoint.lon,
        receiver_height_m: rx.height_m,
        receiver_altitude_m: rx.altitude_m,
        propagation_model: resolveModelFast(tx, propagation),
        wave_type: propagation.wave_type,
        atmosphere,
        use_gpu: propagation.use_gpu,
        fetch_space_weather: propagation.fetch_space_weather,
        num_profile_points: 512,
        context: propagation.context ?? 2,
        diffraction_model: propagation.diffraction_model ?? 'none',
        rcs_m2: propagation.rcs_m2 ?? 1.0,
        clutter_height_m: propagation.clutter_height_m ?? 0,
      })
      setProgress(90)
      setP2pResult(result.result)
      setTerrainProfile(result.profile)
      if (result.result?.warnings?.length > 0) setWarnings(result.result.warnings)
      setProgress(100)
      setBottomTab('terrain')
    } catch (err) {
      const detail = errDetail(err)
      toast.error(`P2P failed: ${detail}`, { autoClose: 10000 })
    } finally {
      setIsSimulating(false)
      setTimeout(() => setProgress(0), 1000)
    }
  }, [tx, rx, rxPoint, propagation, atmosphere])

  // ── Best site (candidates) ────────────────────────────────────────────────
  const runBestSite = useCallback(async () => {
    if (bestSiteCandidates.length < 2) {
      toast.info('Add at least 2 candidate sites')
      return
    }
    setIsSimulating(true)
    setProgress(10)
    setBestSiteResult(null)

    try {
      const result = await simulateBestSite({
        candidates: bestSiteCandidates,
        base_transmitter: { ...tx, frequency_hz: Number(tx.frequency_hz) },
        receiver: rx,
        propagation_model: resolveModelFast(tx, propagation),
        wave_type: propagation.wave_type,
        radius_km: Math.min(propagation.radius_km || 30, 500),   // best-site endpoints cap radius at 500 km
        num_radials: Math.min(propagation.num_radials, 180),
        points_per_radial: Math.min(propagation.points_per_radial, 150),
        min_signal_dbm: Math.min(0, propagation.min_signal_dbm ?? -100),
        atmosphere,
        use_gpu: propagation.use_gpu,
        terrain_resolution: propagation.terrain_resolution,
        context: propagation.context ?? 2,
        diffraction_model: propagation.diffraction_model ?? 'none',
      })
      setBestSiteResult(result)
      if (result.best_geojson) setCoverageGeoJSON(result.best_geojson)
      setProgress(100)
      setBottomOpen(true); setBottomTab('results')
      const best = result.sites?.[0]
      if (best) toast.success(`Best site: ${best.label} — ${best.covered_area_km2} km² covered`)
    } catch (err) {
      const detail = errDetail(err)
      toast.error(`Best site failed: ${detail}`, { autoClose: 10000 })
    } finally {
      setIsSimulating(false)
      setTimeout(() => setProgress(0), 1000)
    }
  }, [tx, rx, propagation, atmosphere, bestSiteCandidates])

  // ── Route analysis ────────────────────────────────────────────────────────
  const runRoute = useCallback(async () => {
    if (routeWaypoints.length < 2) {
      toast.info('Draw at least 2 waypoints on the map first (use the Route draw tool)')
      return
    }
    const recvLat = routeReceiverPoint?.lat ?? tx.lat
    const recvLon = routeReceiverPoint?.lon ?? tx.lon
    setIsSimulating(true)
    setProgress(20)
    try {
      const result = await simulateRoute({
        waypoints: routeWaypoints,
        receiver_lat: recvLat,
        receiver_lon: recvLon,
        transmitter: { ...tx, frequency_hz: Number(tx.frequency_hz) },
        receiver: rx,
        propagation_model: resolveModelFast(tx, propagation),
        wave_type: propagation.wave_type,
        atmosphere,
        context: propagation.context ?? 2,
        diffraction_model: propagation.diffraction_model ?? 'none',
        clutter_height_m: propagation.clutter_height_m ?? 0,
      })
      upsertLayer('route', result.geojson, '#00b4d8')
      setRouteResult(result)
      setProgress(100)
      setBottomOpen(true); setBottomTab('results')
      toast.success(`Route analysis: ${result.geojson?.features?.filter(f => f.geometry.type === 'Point').length} waypoints processed`)
    } catch (err) {
      toast.error('Route analysis failed: ' + errDetail(err))
    } finally {
      setIsSimulating(false)
      setTimeout(() => setProgress(0), 1000)
    }
  }, [tx, rx, propagation, atmosphere, routeWaypoints, routeReceiverPoint, upsertLayer])

  // ── Multipoint analysis ───────────────────────────────────────────────────
  const runMultipoint = useCallback(async () => {
    if (multipointTxs.length < 1) {
      toast.info('Click TX points on the map first (use the Multipoint draw tool)')
      return
    }
    const recvLat = routeReceiverPoint?.lat ?? tx.lat
    const recvLon = routeReceiverPoint?.lon ?? tx.lon
    setIsSimulating(true)
    setProgress(20)
    try {
      const result = await simulateMultipoint({
        tx_points: multipointTxs,
        receiver_lat: recvLat,
        receiver_lon: recvLon,
        transmitter: { ...tx, frequency_hz: Number(tx.frequency_hz) },
        receiver: rx,
        propagation_model: resolveModelFast(tx, propagation),
        wave_type: propagation.wave_type,
        atmosphere,
        context: propagation.context ?? 2,
        diffraction_model: propagation.diffraction_model ?? 'none',
        clutter_height_m: propagation.clutter_height_m ?? 0,
      })
      upsertLayer('multipoint', result.geojson, '#f59e0b')
      setMultipointResult(result)
      setProgress(100)
      setBottomOpen(true); setBottomTab('results')
      toast.success(`Multipoint: ${multipointTxs.length} TX points analysed`)
    } catch (err) {
      toast.error('Multipoint failed: ' + errDetail(err))
    } finally {
      setIsSimulating(false)
      setTimeout(() => setProgress(0), 1000)
    }
  }, [tx, rx, propagation, atmosphere, multipointTxs, routeReceiverPoint, upsertLayer])

  // ── MANET planning ────────────────────────────────────────────────────────
  const runManet = useCallback(async () => {
    if (manetNodes.length < 2) {
      toast.info('Place at least 2 nodes on the map')
      return
    }
    setIsSimulating(true)
    setProgress(15)
    try {
      const result = await simulateManet({
        nodes: manetNodes.map(n => ({ lat: n.lat, lon: n.lon, height_m: n.height_m, label: n.label })),
        transmitter: { ...tx, frequency_hz: Number(tx.frequency_hz) },
        receiver: rx,
        propagation_model: resolveModelFast(tx, propagation),
        wave_type: propagation.wave_type,
        atmosphere,
        context: propagation.context ?? 2,
        diffraction_model: propagation.diffraction_model ?? 'none',
        clutter_height_m: propagation.clutter_height_m ?? 0,
        sensitivity_dbm: rx.sensitivity_dbm,
      })
      setManetResult(result.geojson)
      upsertLayer('manet', result.geojson, '#06d6a0')
      setProgress(100)
      setBottomOpen(true); setBottomTab('results')
      const links = result.geojson?.features?.filter(f => f.geometry.type === 'LineString') || []
      const connected = links.filter(f => f.properties?.connected).length
      toast.success(`MANET: ${connected}/${links.length} links connected`)
    } catch (err) {
      toast.error('MANET planning failed: ' + errDetail(err))
    } finally {
      setIsSimulating(false)
      setTimeout(() => setProgress(0), 1000)
    }
  }, [tx, rx, propagation, atmosphere, manetNodes, upsertLayer])

  // ── Best server ───────────────────────────────────────────────────────────
  const runBestServer = useCallback(async () => {
    if (!bestServerQuery) {
      toast.info('Click a query point on the map first (P2P mode: right-click)')
      return
    }
    if (bestServerSites.length < 1) {
      // Use extra TX list as sites
      if (extraTxList.length < 1) {
        toast.info('Add at least one TX site (use Add Transmitter) or add Best Server sites below')
        return
      }
    }
    const sites = bestServerSites.length > 0
      ? bestServerSites
      : extraTxList.map(e => ({ lat: e.tx.lat, lon: e.tx.lon, height_m: e.tx.height_m, label: e.label }))

    setIsSimulating(true)
    setProgress(20)
    setBestServerResult(null)
    try {
      const result = await simulateBestServer({
        query_lat: bestServerQuery.lat,
        query_lon: bestServerQuery.lon,
        tx_sites: sites,
        transmitter: { ...tx, frequency_hz: Number(tx.frequency_hz) },
        receiver: rx,
        propagation_model: resolveModelFast(tx, propagation),
        wave_type: propagation.wave_type,
        atmosphere,
        context: propagation.context ?? 2,
        clutter_height_m: propagation.clutter_height_m ?? 0,
      })
      setBestServerResult(result)
      setProgress(100)
      setBottomOpen(true); setBottomTab('results')
      toast.success(`Best server: ${result.best_server?.label} at ${result.best_server?.signal_dbm} dBm`)
    } catch (err) {
      toast.error('Best server failed: ' + errDetail(err))
    } finally {
      setIsSimulating(false)
      setTimeout(() => setProgress(0), 1000)
    }
  }, [tx, rx, propagation, atmosphere, bestServerQuery, bestServerSites, extraTxList])

  // ── Interference analysis ─────────────────────────────────────────────────
  const runInterference = useCallback(async () => {
    const layers = extraTxList.filter(e => e.geojson)
    if (layers.length < 2) {
      toast.info('Run coverage for at least 2 TX layers first, then use Interference')
      return
    }
    const [signalLayer, noiseLayer] = [layers[0].geojson, layers[1].geojson]
    setIsSimulating(true)
    setProgress(30)
    try {
      const result = await simulateInterference(signalLayer, noiseLayer)
      upsertLayer('interference', result.geojson, '#a855f7')
      setProgress(100)
      toast.success(`Interference analysis: ${result.geojson?.features?.length} SNR points`)
    } catch (err) {
      toast.error('Interference analysis failed: ' + errDetail(err))
    } finally {
      setIsSimulating(false)
      setTimeout(() => setProgress(0), 1000)
    }
  }, [extraTxList, upsertLayer])

  // ── Super layer ───────────────────────────────────────────────────────────
  const runSuperLayer = useCallback(async () => {
    const layers = [
      ...(coverageGeoJSON ? [coverageGeoJSON] : []),
      ...extraTxList.filter(e => e.geojson).map(e => e.geojson),
    ]
    if (layers.length < 2) {
      toast.info('Need at least 2 coverage layers for Super Layer merge')
      return
    }
    setIsSimulating(true)
    setProgress(30)
    try {
      const result = await simulateSuperLayer(layers, 0.001)
      upsertLayer('super_layer', result.geojson, '#06d6a0')
      setCoverageGeoJSON(result.geojson)
      setProgress(100)
      toast.success(`Super Layer: ${result.geojson?.features?.length} merged points`)
    } catch (err) {
      toast.error('Super Layer failed: ' + errDetail(err))
    } finally {
      setIsSimulating(false)
      setTimeout(() => setProgress(0), 1000)
    }
  }, [coverageGeoJSON, extraTxList, upsertLayer])

  // ── Best site polygon ─────────────────────────────────────────────────────
  const runBestSitePolygon = useCallback(async () => {
    if (polygonCoords.length < 3) {
      toast.info('Draw a polygon on the map first (use the Polygon draw tool)')
      return
    }
    setIsSimulating(true)
    setProgress(10)
    setPolygonBsaResult(null)
    try {
      const result = await simulateBestSitePolygon({
        polygon: polygonCoords,
        coverage_pct: polygonBsaCoveragePct,
        base_transmitter: { ...tx, frequency_hz: Number(tx.frequency_hz) },
        receiver: rx,
        propagation_model: resolveModelFast(tx, propagation),
        wave_type: propagation.wave_type,
        radius_km: Math.min(propagation.radius_km || 30, 500),   // best-site endpoints cap radius at 500 km
        num_radials: Math.min(propagation.num_radials, 180),
        points_per_radial: Math.min(propagation.points_per_radial, 150),
        min_signal_dbm: Math.min(0, propagation.min_signal_dbm ?? -100),
        atmosphere,
        terrain_resolution: propagation.terrain_resolution,
        context: propagation.context ?? 2,
        diffraction_model: propagation.diffraction_model ?? 'none',
      })
      setPolygonBsaResult(result)
      if (result.best_geojson) setCoverageGeoJSON(result.best_geojson)
      setProgress(100)
      setBottomOpen(true); setBottomTab('results')
      const best = result.sites?.[0]
      if (best) toast.success(`Best polygon site: ${best.lat?.toFixed(4)}, ${best.lon?.toFixed(4)} — ${best.covered_area_km2} km²`)
    } catch (err) {
      const detail = errDetail(err)
      toast.error(`Best site polygon failed: ${detail}`, { autoClose: 10000 })
    } finally {
      setIsSimulating(false)
      setTimeout(() => setProgress(0), 1000)
    }
  }, [tx, rx, propagation, atmosphere, polygonCoords, polygonBsaCoveragePct])

  // ── Ray trace ────────────────────────────────────────────────────────────
  const runRayTrace = useCallback(async () => {
    setIsSimulating(true)
    setProgress(20)
    try {
      const result = await simulateRayTrace({
        tx_lat: tx.lat,
        tx_lon: tx.lon,
        tx_height_m: Math.max(0, tx.height_m),
        tx_power_dbm: Math.max(-30, Math.min(100, tx.power_dbm)),
        frequency_hz: Number(tx.frequency_hz),
        num_azimuths: 36,
        num_elevations: 5,
        max_range_m: Math.max(100, Math.min(200000, (propagation.radius_km || 10) * 1000)),   // backend caps at 200 km
        num_points: 200,
        ground_material: 'average_ground',
        vegetation_height_m: Math.max(0, propagation.clutter_height_m ?? 0),
        building_height_m: 0,
        enable_reflections: true,
        min_signal_dbm: Math.min(0, propagation.min_signal_dbm ?? -120),
      })
      upsertLayer('ray_trace', result.geojson, '#f59e0b')
      setProgress(100)
      toast.success(`Ray trace: ${result.metadata?.num_paths} paths in ${result.metadata?.computation_s}s`)
    } catch (err) {
      toast.error('Ray trace failed: ' + errDetail(err))
    } finally {
      setIsSimulating(false)
      setTimeout(() => setProgress(0), 1000)
    }
  }, [tx, propagation, upsertLayer])

  // ── Archive load ──────────────────────────────────────────────────────────
  const handleArchiveLoad = useCallback((entry) => {
    if (entry.geojson) {
      upsertLayer(`archive_${entry.id}`, entry.geojson, '#a855f7')
      toast.success(`Loaded "${entry.name}" from archive`)
    }
    setArchiveOpen(false)
  }, [upsertLayer])

  // ── Cache purge ───────────────────────────────────────────────────────────
  const handlePurgeCache = async () => {
    try {
      await purgeCache()
      toast.success('Cache purged successfully')
    } catch (err) {
      toast.error('Cache purge failed')
    }
  }

  // ── Run simulation dispatch ───────────────────────────────────────────────
  const runSimulation = useCallback(() => {
    if (activeTab === 'coverage') return runCoverage()
    if (activeTab === 'p2p') return runP2P()
    if (activeTab === 'best_site') return runBestSite()
    if (activeTab === 'radar') return runCoverage()
    if (activeTab === 'route') return runRoute()
    if (activeTab === 'multipoint') return runMultipoint()
    if (activeTab === 'manet') return runManet()
    if (activeTab === 'best_server') return runBestServer()
    if (activeTab === 'best_site_polygon') return runBestSitePolygon()
    if (activeTab === 'ray_trace') return runRayTrace()
    return runCoverage()
  }, [
    activeTab, runCoverage, runP2P, runBestSite, runRoute, runMultipoint,
    runManet, runBestServer, runBestSitePolygon, runRayTrace,
  ])

  // ── Effective drawMode from activeTab ─────────────────────────────────────
  const effectiveDrawMode = drawMode || (
    activeTab === 'route' && routeWaypoints.length === 0 ? null :
    activeTab === 'multipoint' && multipointTxs.length === 0 ? null :
    null
  )

  // ── Current geojson for archive ───────────────────────────────────────────
  const currentGeojsonForArchive = coverageGeoJSON ||
    extraGeojsonLayers.slice(-1)[0]?.geojson || null
  const currentParamsForArchive = { type: activeTab, tx, propagation }

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="app" style={{
      gridTemplateColumns: sidebarOpen ? 'var(--panel-width) 1fr' : '0 1fr',
      gridTemplateRows: `var(--header-height) 1fr ${bottomOpen ? bottomPanelHeight : 0}px`,
    }}>
      {/* ── Header ──────────────────────────────────────────────────────── */}
      <header className="app-header">

        {/* ── Hamburger — overflow menu ─────────────────────────────────── */}
        <div ref={menuRef} style={{ position: 'relative', flexShrink: 0 }}>
          <button
            className={`btn ${menuOpen ? 'btn-primary' : 'btn-ghost'}`}
            style={{ padding: '4px 8px' }}
            title="Menu"
            onClick={() => setMenuOpen(o => !o)}
          >
            <Menu size={16} />
          </button>

          <OverflowMenu
            open={menuOpen}
            onClose={() => setMenuOpen(false)}
            mainMode={mainMode}
            canUndo={undoStackRef.current.length > 0}
            canRedo={redoStackRef.current.length > 0}
            undoTick={undoTick}
            onUndo={undo}
            onRedo={redo}
            drawMode={drawMode}
            onToggleBoundsDraw={() => setDrawMode(m => m === 'bounds' ? null : 'bounds')}
            isSimulating={isSimulating}
            onInterference={runInterference}
            onSuperLayer={runSuperLayer}
            satToolActive={satToolActive}
            onToggleSatTool={() => setSatToolActive(s => !s)}
            onOpenArchive={() => setArchiveOpen(true)}
            onSaveState={handleSaveState}
            onLoadState={handleLoadState}
            onImport={() => mapImportApiRef.current?.openFileDialog?.()}
            onPurgeCache={handlePurgeCache}
            onOpenHelp={() => setHelpOpen(true)}
            onOpenRemote={() => setRemoteOpen(true)}
          />
        </div>

        <HeaderTabs
          mainMode={mainMode}
          activeTab={activeTab}
          lobCount={lobs.length}
          lobGroupCount={lobGroups.filter(g => g.lobs.length >= 2).length}
          onSelectMode={(m) => { setMainMode(m); if (m === 'propagation') setLobPickingMode(false); else setDrawMode(null) }}
          onSelectTab={(id) => { setActiveTab(id); setDrawMode(null); if (id === 'radar') setPropagation(prev => ({ ...prev, model: 'radar' })) }}
        />

        <HeaderActions
          gpuActive={propagation.use_gpu}
          mainMode={mainMode}
          isSimulating={isSimulating}
          progress={progress}
          txActive={txActive}
          sdrActive={!!(sdrFeatures.length || sdrCoverage)}
          onClear={handleClearAll}
          onOpenLayers={() => setLayersOpen(true)}
          onOpenAtak={() => setAtakPanelOpen(true)}
          onOpenSdr={() => setSdrPanelOpen(true)}
          onOpenDbCalc={() => setDbCalcOpen(true)}
          onRun={runSimulation}
        />
      </header>

      {/* ── Modals ──────────────────────────────────────────────────────── */}
      <AppModals
        helpOpen={helpOpen} onCloseHelp={() => setHelpOpen(false)}
        atakPanelOpen={atakPanelOpen} onCloseAtak={() => setAtakPanelOpen(false)}
        mapCenter={{ lat: tx.lat, lon: tx.lon }}
        sdrPanelOpen={sdrPanelOpen} onCloseSdr={() => setSdrPanelOpen(false)} sdr={sdrStream}
        sdrHidden={sdrPicking} onSdrPickLocation={requestSdrLocationPick} sdrMapFeatures={sdrMapFeatures}
        archiveOpen={archiveOpen} onCloseArchive={() => setArchiveOpen(false)}
        currentGeojson={currentGeojsonForArchive} currentParams={currentParamsForArchive} onArchiveLoad={handleArchiveLoad}
      />
      {sdrPicking && (
        <div style={{ position: 'fixed', top: 64, left: '50%', transform: 'translateX(-50%)', zIndex: 3000,
                      background: '#0d1117', border: '1px solid #1f6feb', borderRadius: 6, padding: '6px 12px',
                      color: '#e6edf3', fontSize: 12, boxShadow: '0 6px 20px rgba(0,0,0,0.6)',
                      display: 'flex', alignItems: 'center', gap: 10 }}>
          📍 Click the map to set the device location
          <button className="btn btn-ghost" style={{ fontSize: 11, padding: '2px 8px' }} onClick={cancelSdrPick}>Cancel</button>
        </div>
      )}
      {dbCalcOpen && <DecibelCalculator onClose={() => setDbCalcOpen(false)} />}
      {remoteOpen && <RemoteAccessPanel onClose={() => setRemoteOpen(false)} />}
      {layersOpen && (
        <div onClick={() => setLayersOpen(false)}
             style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)', zIndex: 2000,
                      display: 'flex', alignItems: 'flex-start', justifyContent: 'center', padding: '4vh 20px' }}>
          <div onClick={e => e.stopPropagation()}
               style={{ background: '#0d1117', border: '1px solid #30363d', borderRadius: 8, width: 760, maxWidth: '100%',
                        height: 'min(86vh, 820px)', color: '#e6edf3', boxShadow: '0 20px 60px rgba(0,0,0,0.7)',
                        display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 12px', borderBottom: '1px solid #21262d', flexShrink: 0 }}>
              <h3 style={{ margin: 0, fontSize: 14, display: 'flex', alignItems: 'center', gap: 8 }}><Layers size={16} /> Layers</h3>
              <button className="btn btn-ghost" style={{ padding: '2px 6px' }} onClick={() => setLayersOpen(false)}><X size={14} /></button>
            </div>
            <div style={{ flex: 1, minHeight: 0 }}>
              <LayerManagerPanel ul={ul} openFileDialog={() => mapImportApiRef.current?.openFileDialog?.()}
                regionPreselect={layersRegionPreselect} onConsumeRegionPreselect={() => setLayersRegionPreselect(null)}
                incomingBbox={packBboxFromMap} onConsumeBbox={() => setPackBboxFromMap(null)}
                onRequestDrawBbox={() => {
                  setLayersOpen(false)
                  awaitingPackBboxRef.current = true
                  setDrawMode('bounds')
                  toast.info('Draw a rectangle on the map to pick the download area')
                }}
                onOpenSaveStateDialog={() => setSaveStateDialogOpen(true)}
                onLoadFullState={handleLoadState}
              />
            </div>
          </div>
        </div>
      )}


      {/* ── Sidebar ────────────────────────────────────────────────────── */}
      <aside className={`app-sidebar ${sidebarOpen ? '' : 'collapsed'}`}
             style={{ display: sidebarOpen ? 'flex' : 'none' }}>

        {/* Collapse button */}
        <div style={{ display: 'flex', justifyContent: 'flex-end', padding: '3px 5px', borderBottom: '1px solid #21262d', flexShrink: 0 }}>
          <button
            className="btn btn-ghost"
            style={{ padding: '2px 5px' }}
            title="Hide sidebar"
            onClick={() => setSidebarOpen(false)}
          >
            <ChevronLeft size={13} />
          </button>
        </div>

        {/* ── GEOLOCATION MODE ─────────────────────────────────────────── */}
        {mainMode === 'geolocation' && (
          <GeoLocationPanel
            lobs={lobs}
            onAddLoB={handleAddLoB}
            onRemoveLoB={handleRemoveLoB}
            onUpdateLoB={handleUpdateLoB}
            capGroups={capGroups}
            onToggleCAP={handleToggleCAP}
            lobGroups={lobGroups}
            pickedLocation={pendingLobLocation}
            onClearPickedLocation={() => setPendingLobLocation(null)}
            onStartPickLocation={() => setLobPickingMode(true)}
            isPickingLocation={lobPickingMode}
            pickedAzimuthTarget={pendingLobAzimuthTarget}
            onClearPickedAzimuthTarget={() => setPendingLobAzimuthTarget(null)}
            onStartPickAzimuth={() => setLobAzimuthPickingMode(true)}
            isPickingAzimuth={lobAzimuthPickingMode}
            editLobRequestId={editLobRequestId}
            onClearEditLobRequest={() => setEditLobRequestId(null)}
            lobAlgorithm={lobAlgorithm}
            onChangeLobAlgorithm={setLobAlgorithm}
          />
        )}

        {/* ── PROPAGATION MODE ─────────────────────────────────────────── */}
        {mainMode === 'propagation' && <>

        {/* No-emitter hint */}
        {!txActive && (
          <div style={{
            margin: '8px 12px', padding: '10px 12px',
            background: '#0d1117', border: '1px dashed #30363d', borderRadius: 6,
            fontSize: 11, color: '#8b949e', lineHeight: 1.5, textAlign: 'center',
          }}>
            <div style={{ color: '#00b4d8', fontWeight: 600, marginBottom: 4 }}>No emitter placed</div>
            Right-click anywhere on the map to add an emitter or a LoB observer.
          </div>
        )}

        {/* Primary TX panel */}
        <div style={{ display: 'flex', alignItems: 'center', padding: '6px 12px 0', gap: 6 }}>
          <div style={{ width: 10, height: 10, borderRadius: '50%', background: '#00b4d8', flexShrink: 0 }} />
          <EditableLabel value={txLabel} onChange={setTxLabel} />
          <span style={{ fontSize: 10, color: '#444d56', flexShrink: 0 }}>Primary</span>
        </div>
        <TransmitterPanel
          tx={tx} setTx={setTx} coordSystem={coordSystem} distUnit={distUnit} setRx={setRx}
          expandSignal={txEditFocus.id === 'primary' ? txEditFocus.ts : 0}
        />
        <PropagationPanel
          propagation={propagation}
          setPropagation={setPropagation}
          resolvedModel={resolveModelFast(tx, propagation)}
          distUnit={distUnit}
          activeTab={activeTab}
          coverageRaster={coverageRaster}
          onSetRaster={setCoverageRaster}
        />
        <AntennaPanel tx={tx} setTx={setTx} rx={rx} setRx={setRx} txFrequencyHz={tx.frequency_hz} />
        <AtmospherePanel atmosphere={atmosphere} setAtmosphere={setAtmosphere} txLat={tx.lat} txLon={tx.lon} />

        {/* Extra transmitters + "Add Transmitter" */}
        <ExtraTransmitters
          extraTxList={extraTxList}
          coordSystem={coordSystem}
          distUnit={distUnit}
          rx={rx}
          setRx={setRx}
          defaultPropagation={propagation}
          defaultAtmosphere={atmosphere}
          resolveModelFast={resolveModelFast}
          onRename={renameExtraTx}
          onRemove={removeTransmitter}
          onUpdateTx={updateExtraTx}
          onUpdatePropagation={updateExtraPropagation}
          onUpdateAtmosphere={updateExtraAtmosphere}
          onAdd={addTransmitter}
          expandSignalForId={txEditFocus.id !== 'primary' ? txEditFocus : null}
        />

        {/* ── Tab-specific panels ─────────────────────────────────────── */}

        {/* Best Site (candidates) */}
        {activeTab === 'best_site' && (
          <BestSiteSidebar
            candidates={bestSiteCandidates}
            onRemove={(i) => setBestSiteCandidates(prev => prev.filter((_, j) => j !== i))}
            onAddFromTx={() => setBestSiteCandidates(prev => [
              ...prev,
              { lat: tx.lat + (prev.length % 2 === 0 ? 0.05 : -0.05), lon: tx.lon + (prev.length % 2 === 0 ? 0.05 : -0.05), height_m: tx.height_m, label: `Site ${prev.length + 1}` },
            ])}
          />
        )}

        {/* Radar target */}
        {activeTab === 'radar' && (
          <RadarTargetPicker rcsM2={propagation.rcs_m2} onSelectRcs={(rcs) => setPropagation(p => ({ ...p, rcs_m2: rcs }))} />
        )}

        {/* Route analysis */}
        {activeTab === 'route' && (
          <RouteSidebar
            drawMode={drawMode}
            waypoints={routeWaypoints}
            receiverPoint={routeReceiverPoint}
            onToggleDraw={() => setDrawMode(m => m === 'route' ? null : 'route')}
            onClearWaypoints={() => { setRouteWaypoints([]); setDrawMode(null) }}
            onClearReceiver={() => setRouteReceiverPoint(null)}
          />
        )}

        {/* Multipoint */}
        {activeTab === 'multipoint' && (
          <MultipointSidebar
            drawMode={drawMode}
            txPoints={multipointTxs}
            onToggleDraw={() => setDrawMode(m => m === 'multipoint' ? null : 'multipoint')}
            onClear={() => { setMultipointTxs([]); setDrawMode(null) }}
          />
        )}

        {/* MANET planning */}
        {activeTab === 'manet' && (
          <ManetPanel
            nodes={manetNodes}
            onAddNode={() => {
              setDrawMode('manet')
              setManetAddingNode(true)
              toast.info('Click the map to place a MANET node')
            }}
            onRemoveNode={(nodeId) => setManetNodes(prev => prev.filter(n => n.id !== nodeId))}
            onUpdateNode={(nodeId, updates) => setManetNodes(prev =>
              prev.map(n => n.id === nodeId ? { ...n, ...updates } : n)
            )}
            isSimulating={isSimulating}
          />
        )}

        {/* Best server */}
        {activeTab === 'best_server' && (
          <BestServerSidebar query={bestServerQuery} onClearQuery={() => setBestServerQuery(null)} />
        )}

        {/* Best Site Polygon */}
        {activeTab === 'best_site_polygon' && (
          <BsaPolygonSidebar
            drawMode={drawMode}
            polygonCoords={polygonCoords}
            coveragePct={polygonBsaCoveragePct}
            onToggleDraw={() => setDrawMode(m => m === 'polygon' ? null : 'polygon')}
            onClearPolygon={() => { setPolygonCoords([]); setDrawMode(null) }}
            onSetCoveragePct={setPolygonBsaCoveragePct}
          />
        )}

        {/* Ray trace */}
        {activeTab === 'ray_trace' && <RayTraceSidebar tx={tx} />}

        {/* Satellite tool */}
        {satToolActive && (
          <SatellitePanel
            txLat={tx.lat}
            txLon={tx.lon}
            onResult={(geojson) => upsertLayer('satellite', geojson, '#06d6a0')}
          />
        )}

        {/* Draw bounds indicator */}
        {drawBounds && (
          <div style={{ borderTop: '1px solid #21262d', padding: '8px 12px' }}>
            <div style={{ fontSize: 10, fontWeight: 600, color: '#a855f7', marginBottom: 4 }}>COVERAGE BOUNDS ACTIVE</div>
            <div style={{ fontSize: 10, color: '#8b949e' }}>
              N:{drawBounds.north.toFixed(3)} S:{drawBounds.south.toFixed(3)}<br />
              E:{drawBounds.east.toFixed(3)} W:{drawBounds.west.toFixed(3)}
            </div>
            <button
              className="btn btn-ghost"
              style={{ fontSize: 10, marginTop: 4, color: '#ef4444', padding: '2px 6px' }}
              onClick={() => setDrawBounds(null)}
            >
              Clear Bounds
            </button>
          </div>
        )}

        </>}  {/* end mainMode === 'propagation' */}

      </aside>

      {/* ── Map ──────────────────────────────────────────────────────────── */}
      <div className="map-container">
        {/* Sidebar re-open tab */}
        {!sidebarOpen && (
          <button
            onClick={() => setSidebarOpen(true)}
            style={{
              position: 'absolute', left: 0, top: '50%', transform: 'translateY(-50%)',
              zIndex: 600, padding: '8px 3px', background: '#161b22',
              border: '1px solid #30363d', borderLeft: 'none', borderRadius: '0 4px 4px 0',
              color: '#8b949e', cursor: 'pointer',
            }}
            title="Show sidebar"
          >
            <ChevronRight size={12} />
          </button>
        )}
        {/* Bottom panel re-open tab */}
        {!bottomOpen && (
          <button
            onClick={() => setBottomOpen(true)}
            style={{
              position: 'absolute', bottom: 0, left: '50%', transform: 'translateX(-50%)',
              zIndex: 600, padding: '2px 14px', background: '#161b22',
              border: '1px solid #30363d', borderBottom: 'none', borderRadius: '4px 4px 0 0',
              color: '#8b949e', cursor: 'pointer',
            }}
            title="Show bottom panel"
          >
            <ChevronUp size={12} />
          </button>
        )}
        {viewMode === '3d' ? (
          <Suspense fallback={
            <div style={{ width: '100%', height: '100%', display: 'flex', alignItems: 'center',
                          justifyContent: 'center', color: '#8b949e', fontSize: 13 }}>Loading 3D globe…</div>
          }>
            <GlobeView
              center={{ lat: tx.lat, lon: tx.lon, zoom: 11 }}
              coverageGeoJSON={coverageGeoJSON}
              extraGeojsonLayers={extraGeojsonLayersWithSdr}
              tx={tx}
              rxPoint={rxPoint}
              antennaAzimuthDeg={tx.antenna?.azimuth_deg ?? null}
              antennaTiltDeg={tx.antenna?.tilt_deg ?? 0}
              antennaPattern={tx.antenna?.polar_pattern || 'omni'}
              minSignalDbm={propagation.min_signal_dbm}
              distUnit={distUnit} setDistUnit={setDistUnit}
              coordSystem={coordSystem} setCoordSystem={setCoordSystem}
              showCompassRose={showCompassRose} setShowCompassRose={setShowCompassRose}
              mapBrightness={mapBrightness} setMapBrightness={setMapBrightness}
              ul={ul}
              drawMode={drawMode}
              onDrawComplete={handleDrawComplete}
              extraTxList={extraTxList}
              geolocationGeoJSON={geolocationGeoJSON}
              gpsFix={gpsFix}
              gpsTrackers={gpsTrackers}
            />
          </Suspense>
        ) : (
        <MapView
          tx={tx}
          txLabel={txLabel}
          rxPoint={rxPoint}
          coverageGeoJSON={coverageGeoJSON}
          buildingGeoJSON={buildingGeoJSON}
          extraTxList={extraTxList}
          gpsFix={gpsFix}
          gpsTrackers={gpsTrackers}
          p2pProfile={terrainProfile}
          activeTab={activeTab}
          minSignalDbm={propagation.min_signal_dbm}
          onMapClick={handleMapClick}
          onTxDrag={(lat, lon) => setTx(prev => ({ ...prev, lat, lon }))}
          onRxDrag={(lat, lon) => setRxPoint({ lat, lon })}
          onExtraTxDrag={(id, lat, lon) => updateExtraTx(id, prev => ({ ...prev, lat, lon }))}
          distUnit={distUnit} setDistUnit={setDistUnit}
          coordSystem={coordSystem} setCoordSystem={setCoordSystem}
          drawMode={drawMode}
          onDrawComplete={handleDrawComplete}
          extraGeojsonLayers={extraGeojsonLayersWithSdr}
          bestSiteCandidates={activeTab === 'best_site' ? bestSiteCandidates : []}
          bestSiteResult={activeTab === 'best_site' ? bestSiteResult : null}
          lobs={lobs}
          lobGroups={lobGroups}
          capGroups={capGroups}
          lobAlgorithm={lobAlgorithm}
          lobPickingMode={lobPickingMode}
          lobAzimuthPickingMode={lobAzimuthPickingMode}
          txActive={txActive}
          onAddEmitter={handleAddEmitter}
          onAddLoBObserver={handleAddLoBObserver}
          onAddLoBAzimuthTarget={handleAddLoBAzimuthTarget}
          onSimulatePropagationFromFix={handleSimulatePropagationFromFix}
          onDownloadRegionAt={async (lat, lon) => {
            try {
              const r = await regionAtPoint(lat, lon)
              setLayersRegionPreselect(r); setLayersOpen(true)
            } catch {
              toast.info('No catalogued region contains that point — open the Layer Manager and search by name instead')
            }
          }}
          onViewshedAt={async (lat, lon) => {
            // Defaults — Electron's BrowserWindow blocks window.prompt(), so we
            // don't ask up-front. Operator can delete the layer and re-run, or
            // tweak via the dedicated Viewshed panel (next pass).
            const radius = 10, height = 2
            console.info('[viewshed] requesting', { lat, lon, radius, height })
            const id = toast.info(`Computing viewshed at ${lat.toFixed(4)},${lon.toFixed(4)} (${radius} km)…`, { autoClose: false })
            try {
              const fc = await getViewshed({ lat, lon, radius_km: radius, observer_height_m: height })
              console.info('[viewshed] result', fc)
              if (!fc?.features?.length) {
                toast.dismiss(id); toast.warning('Viewshed returned no visible region — no terrain data for this point?')
                return
              }
              ul?.addGeoJSONLayer?.(fc, { name: `Viewshed @ ${lat.toFixed(4)},${lon.toFixed(4)} (${radius} km)`,
                                          sourceFormat: 'viewshed', color: '#f59e0b', opacity: 0.45 })
              toast.dismiss(id); toast.success('Viewshed added — visible from observer in orange')
            } catch (e) {
              console.error('[viewshed] failed', e)
              toast.dismiss(id)
              const msg = e?.response?.data?.detail || e?.message || String(e)
              toast.error(`Viewshed failed: ${msg}`, { autoClose: 8000 })
            }
          }}
          onContoursAt={async (lat, lon) => {
            const radius = 10, interval = 50
            console.info('[contours] requesting', { lat, lon, radius, interval })
            const id = toast.info(`Generating ${interval} m contours at ${lat.toFixed(4)},${lon.toFixed(4)} (${radius} km)…`, { autoClose: false })
            try {
              const fc = await getTerrainContours(lat, lon, radius, interval)
              console.info('[contours] result', fc)
              if (!fc?.features?.length) {
                toast.dismiss(id); toast.warning(fc?.note || 'No contour data — area effectively flat or terrain pack missing')
                return
              }
              ul?.addGeoJSONLayer?.(fc, { name: `Contours @ ${lat.toFixed(4)},${lon.toFixed(4)} (${interval} m)`,
                                          sourceFormat: 'contours', color: '#a78bfa', opacity: 0.85 })
              toast.dismiss(id); toast.success(`Added ${fc.features.length} contour line${fc.features.length > 1 ? 's' : ''}`)
            } catch (e) {
              console.error('[contours] failed', e)
              toast.dismiss(id)
              const msg = e?.response?.data?.detail || e?.message || String(e)
              toast.error(`Contours failed: ${msg}`, { autoClose: 8000 })
            }
          }}
          // Clear-all-of-a-kind entries for the right-click menu. The
          // hasViewsheds / hasContours flags drive whether the buttons are shown.
          hasViewsheds={ul.layers.some(l => l.sourceFormat === 'viewshed')}
          hasContours={ul.layers.some(l => l.sourceFormat === 'contours')}
          onClearViewsheds={() => {
            const ids = ul.layers.filter(l => l.sourceFormat === 'viewshed').map(l => l.id)
            ids.forEach(id => ul.removeLayer(id))
            if (ids.length > 0) toast.info(`Cleared ${ids.length} viewshed${ids.length > 1 ? 's' : ''}`)
          }}
          onClearContours={() => {
            const ids = ul.layers.filter(l => l.sourceFormat === 'contours').map(l => l.id)
            ids.forEach(id => ul.removeLayer(id))
            if (ids.length > 0) toast.info(`Cleared ${ids.length} contour layer${ids.length > 1 ? 's' : ''}`)
          }}
          showCompassRose={showCompassRose} setShowCompassRose={setShowCompassRose}
          mapBrightness={mapBrightness} setMapBrightness={setMapBrightness}
          flyToTarget={flyToTarget}
          onSaveLocation={handleSaveLocation}
          onImportApi={(api) => { mapImportApiRef.current = api }}
          ul={ul}
          terrainLineMode={terrainLineMode}
          onTerrainLineComplete={handleTerrainLineComplete}
          multipointTxs={multipointTxs}
          manetNodes={manetNodes}
          routeWaypoints={routeWaypoints}
          mapSel={mapSel}
          onSelectFeature={setMapSel}
        />
        )}
      </div>

      {/* ── Bottom panel ─────────────────────────────────────────────────── */}
      <div className="bottom-panel" style={{ display: bottomOpen ? undefined : 'none' }}>
        {/* Drag handle */}
        <div
          onMouseDown={handleResizeMouseDown}
          style={{
            height: 5,
            cursor: 'row-resize',
            background: 'transparent',
            borderTop: '2px solid #30363d',
            flexShrink: 0,
          }}
          title="Drag to resize"
        />
        <BottomPanelTabs
          active={bottomTab}
          onSelect={setBottomTab}
          savedCount={savedLocations.length}
          spaceWeather={spaceWeather}
          onClose={() => setBottomOpen(false)}
        />

        <BottomPanelContent
          active={bottomTab}
          metadata={metadata} p2pResult={p2pResult} warnings={warnings} activeTab={activeTab}
          analysisResults={{ bestSiteResult, bestSiteCandidates, routeResult, multipointResult, manetResult, bestServerResult, bsaPolygonResult: polygonBsaResult }}
          onChatLocate={(lat, lon) => setRxPoint({ lat, lon })}
          terrain={{
            terrainLineMode, standaloneProfile, standaloneProfileLoading, standaloneProfileError, terrainProfile,
            onToggleLineMode: () => setTerrainLineMode(m => !m),
            onClearStandalone: () => setStandaloneProfile(null),
          }}
          ul={ul}
          terrainGrid={terrainGrid} terrainGridLoading={terrainGridLoading} coverageGeoJSON={coverageGeoJSON} buildingGeoJSON={buildingGeoJSON}
          txActive={txActive} txLabel={txLabel} extraTxList={extraTxList} lobs={lobs} lobGroups={lobGroups}
          onRemoveLoB={handleRemoveLoB} onEditLoB={(lob) => { setMainMode('geolocation'); setEditLobRequestId(lob.id) }}
          onEditEmitter={handleEditEmitter}
          onSimulatePropagationFromFix={handleSimulatePropagationFromFix}
          autoCoverage={autoCoverage} onToggleAutoCoverage={setAutoCoverage} sdrFixes={sdrFixes}
          onSendAlgorithmFixToMap={handleSendAlgorithmFixToMap}
          savedLocations={savedLocations} onSavedFlyTo={(lat, lon) => setFlyToTarget({ lat, lon, zoom: 12, _t: Date.now() })} onSavedRemove={handleRemoveSavedLocation}
          tx={tx} rx={rx} propagation={propagation} spaceWeather={spaceWeather}
        />
      </div>

      <ToastContainer
        position="bottom-right"
        theme="dark"
        toastStyle={{ background: '#161b22', borderColor: '#30363d', color: '#e6edf3' }}
      />
      {/* Electron-safe replacement for window.prompt — see PromptDialog.jsx.
          One provider, called via the `promptUser()` helper from anywhere. */}
      <PromptDialogProvider />
      {/* Save State selector modal (opened from the Layer Manager). */}
      <SaveStateDialog
        open={saveStateDialogOpen}
        onCancel={() => setSaveStateDialogOpen(false)}
        onSave={(sel) => { setSaveStateDialogOpen(false); handleSaveState(sel) }}
      />
    </div>
  )
}
