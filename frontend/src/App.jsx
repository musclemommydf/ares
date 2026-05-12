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
  Video,
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
import UasVideoPanel from './components/Tools/UasVideoPanel'
import HelpPanel from './components/Common/HelpPanel'
import DecibelCalculator from './components/Tools/DecibelCalculator'
import ArchivePanel from './components/Tools/ArchivePanel'
import ManetPanel from './components/Tools/ManetPanel'
import SatellitePanel from './components/Tools/SatellitePanel'
import GeoLocationPanel from './components/Geolocation/GeoLocationPanel'
import LoBList from './components/Geolocation/LoBList'
import { groupLoBsByFrequency, lobGroupKey, computeGroupIntersections, computeCentroid, computeCAPEllipse, computeLoBRenderDistance, destinationPoint, DEFAULT_LOB_ALGORITHM } from './components/Geolocation/LoBUtils'
import { useGeolocation } from './hooks/useGeolocation'
import { useBottomPanelResize } from './hooks/useBottomPanelResize'
import { useNumberFieldSelectAll } from './hooks/useNumberFieldSelectAll'
import { useTerrainGrid } from './hooks/useTerrainGrid'
import { DEFAULT_TX, DEFAULT_RX, DEFAULT_PROPAGATION, DEFAULT_ATMOSPHERE, RADAR_TARGETS, TX_COLORS } from './appDefaults'
import { SESSION_KEY, loadSession } from './session'
import { useSessionAutosave } from './hooks/useSessionAutosave'
import EditableLabel from './components/Common/EditableLabel'
import ToolBtn from './components/Common/ToolBtn'

import {
  simulateCoverage, simulateCoverageRaster, simulateP2P, simulateBestSite, getSpaceWeather, purgeCache,
  simulateRoute, simulateMultipoint, simulateManet, simulateBestServer,
  simulateInterference, simulateSuperLayer, simulateBestSitePolygon,
  simulateRayTrace, simulateSatelliteVisibility,
  getBuildings,
} from './api/client'
import ThreeDView from './components/Charts/ThreeDView'

// Cesium globe is ~30 MB — load it only when the user switches to the 3D view.
const GlobeView = lazy(() => import('./components/Map/GlobeView'))

// Session restored once at module load — used to hydrate useState initial values.
const _s = loadSession()

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
  const [uasPanelOpen, setUasPanelOpen] = useState(false)
  // SDR / DF live state: features from the server-side solver, and the latest
  // auto-coverage GeoJSON from a confirmed fix (rendered as a faint extra layer).
  const [sdrFeatures, setSdrFeatures] = useState([])
  const [sdrCoverage, setSdrCoverage] = useState(null)   // { geojson, frequency_hz, centroid }
  const [coverageRaster, setCoverageRaster] = useState(() => _s?.ui?.coverageRaster ?? false)   // per-pixel raster coverage instead of the radial sweep
  // live operator GPS fix (shown as a "you are here" marker on the 2D/3D map)
  const [gpsFix, setGpsFix] = useState(null)
  useEffect(() => {
    let stop = false
    const tick = async () => { try { const r = await getGpsFix(); if (!stop) setGpsFix(r?.fix || null) } catch { /* ignore */ } }
    tick(); const h = setInterval(tick, 4000)
    return () => { stop = true; clearInterval(h) }
  }, [])
  const [packBboxFromMap, setPackBboxFromMap] = useState(null)   // [w,s,e,n] picked by drawing a box for a pack download
  const awaitingPackBboxRef = useRef(false)

  const [coverageGeoJSON, setCoverageGeoJSON] = useState(null)
  const [p2pResult, setP2pResult] = useState(null)
  const [terrainProfile, setTerrainProfile] = useState(null)
  const [metadata, setMetadata] = useState(null)
  const [spaceWeather, setSpaceWeather] = useState(null)
  const [warnings, setWarnings] = useState([])

  const [isSimulating, setIsSimulating] = useState(false)
  const [progress, setProgress] = useState(0)
  const [activeTab, setActiveTab] = useState(() => _s?.ui?.activeTab ?? 'coverage')
  const [bottomTab, setBottomTab] = useState(() => {
    const t = _s?.ui?.bottomTab
    return (!t || t === 'mapopts') ? 'results' : t   // 'mapopts' tab removed → its options moved to the map ⚙
  })

  // ── Best site (candidates) ────────────────────────────────────────────────
  const [bestSiteResult, setBestSiteResult] = useState(null)
  const [bestSiteCandidates, setBestSiteCandidates] = useState([])

  // ── Radar ──────────────────────────────────────────────────────────────────
  const [radarResult, setRadarResult] = useState(null)

  // ── UI ────────────────────────────────────────────────────────────────────
  const [sidebarOpen, setSidebarOpen] = useState(() => _s?.ui?.sidebarOpen ?? true)
  const [bottomOpen, setBottomOpen] = useState(() => _s?.ui?.bottomOpen ?? true)
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
  const [multipointTxs, setMultipointTxs] = useState([])
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

  const abortRef = useRef(null)
  const menuRef = useRef(null)
  const mapImportApiRef = useRef(null)

  // Unified user-layer manager
  const ul = useUserLayers()
  const {
    terrainLineMode, setTerrainLineMode,
    standaloneProfile, standaloneProfileLoading, standaloneProfileError,
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

  // ── Draw complete callback (from MapView) ─────────────────────────────────
  const handleDrawComplete = useCallback((type, data) => {
    if (type === 'bounds' && awaitingPackBboxRef.current) {
      // a box drawn to pick the area for an offline-pack download (ATAK/Server panel)
      awaitingPackBboxRef.current = false
      setPackBboxFromMap([data.west, data.south, data.east, data.north])
      setDrawMode(null)
      setAtakPanelOpen(true)
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

  // ── Save / Load state ─────────────────────────────────────────────────────
  const handleSaveState = useCallback(() => {
    const state = {
      version: '2.0', savedAt: new Date().toISOString(),
      primaryTransmitter: tx, extraTransmitters: extraTxList,
      receiver: rx, propagation, atmosphere,
      savedLocations,
      lobs, capGroups, lobAlgorithm,
    }
    const blob = new Blob([JSON.stringify(state, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `ares-state-${new Date().toISOString().slice(0, 10)}.json`
    a.click()
    URL.revokeObjectURL(url)
    toast.success('State saved')
  }, [tx, extraTxList, rx, propagation, atmosphere, savedLocations, lobs, capGroups, lobAlgorithm])

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
          toast.success('State loaded')
        } catch {
          toast.error('Invalid state file')
        }
      }
      reader.readAsText(file)
    }
    input.click()
  }, [])

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
  const handleClearLayers = useCallback(() => {
    setCoverageGeoJSON(null)
    setMetadata(null)
    setP2pResult(null)
    setTerrainProfile(null)
    setWarnings([])
    setBestSiteResult(null)
    setRadarResult(null)
    setManetResult(null)
    setPolygonBsaResult(null)
    setBestServerResult(null)
    setExtraGeojsonLayers([])
    setExtraTxList(prev => prev.map(e => ({ ...e, geojson: null })))
    toast.info('All layers cleared')
  }, [])

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
        ? simulateCoverageRaster({ transmitter: { ...tx, frequency_hz: Number(tx.frequency_hz) }, ...baseParams }, 56)
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
      const raw = err.response?.data?.detail
      const detail = Array.isArray(raw)
        ? raw.map(e => `${e.loc?.slice(1).join('.')} — ${e.msg}`).join('\n')
        : (raw || err.message)
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
      const raw = err.response?.data?.detail
      const detail = Array.isArray(raw)
        ? raw.map(e => `${e.loc?.slice(1).join('.')} — ${e.msg}`).join('\n')
        : (raw || err.message)
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
        radius_km: propagation.radius_km,
        num_radials: Math.min(propagation.num_radials, 180),
        points_per_radial: Math.min(propagation.points_per_radial, 150),
        min_signal_dbm: propagation.min_signal_dbm,
        atmosphere,
        use_gpu: propagation.use_gpu,
        terrain_resolution: propagation.terrain_resolution,
        context: propagation.context ?? 2,
        diffraction_model: propagation.diffraction_model ?? 'none',
      })
      setBestSiteResult(result)
      if (result.best_geojson) setCoverageGeoJSON(result.best_geojson)
      setProgress(100)
      const best = result.sites?.[0]
      if (best) toast.success(`Best site: ${best.label} — ${best.covered_area_km2} km² covered`)
    } catch (err) {
      const raw = err.response?.data?.detail
      const detail = Array.isArray(raw)
        ? raw.map(e => `${e.loc?.slice(1).join('.')} — ${e.msg}`).join('\n')
        : (raw || err.message)
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
      setProgress(100)
      toast.success(`Route analysis: ${result.geojson?.features?.filter(f => f.geometry.type === 'Point').length} waypoints processed`)
    } catch (err) {
      toast.error('Route analysis failed: ' + (err.response?.data?.detail || err.message))
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
      setProgress(100)
      toast.success(`Multipoint: ${multipointTxs.length} TX points analysed`)
    } catch (err) {
      toast.error('Multipoint failed: ' + (err.response?.data?.detail || err.message))
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
      const links = result.geojson?.features?.filter(f => f.geometry.type === 'LineString') || []
      const connected = links.filter(f => f.properties?.connected).length
      toast.success(`MANET: ${connected}/${links.length} links connected`)
    } catch (err) {
      toast.error('MANET planning failed: ' + (err.response?.data?.detail || err.message))
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
      toast.success(`Best server: ${result.best_server?.label} at ${result.best_server?.signal_dbm} dBm`)
    } catch (err) {
      toast.error('Best server failed: ' + (err.response?.data?.detail || err.message))
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
      toast.error('Interference analysis failed: ' + (err.response?.data?.detail || err.message))
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
      toast.error('Super Layer failed: ' + (err.response?.data?.detail || err.message))
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
        radius_km: propagation.radius_km,
        num_radials: Math.min(propagation.num_radials, 180),
        points_per_radial: Math.min(propagation.points_per_radial, 150),
        min_signal_dbm: propagation.min_signal_dbm,
        atmosphere,
        terrain_resolution: propagation.terrain_resolution,
        context: propagation.context ?? 2,
        diffraction_model: propagation.diffraction_model ?? 'none',
      })
      setPolygonBsaResult(result)
      if (result.best_geojson) setCoverageGeoJSON(result.best_geojson)
      setProgress(100)
      const best = result.sites?.[0]
      if (best) toast.success(`Best polygon site: ${best.lat?.toFixed(4)}, ${best.lon?.toFixed(4)} — ${best.covered_area_km2} km²`)
    } catch (err) {
      const raw = err.response?.data?.detail
      const detail = Array.isArray(raw)
        ? raw.map(e => `${e.loc?.slice(1).join('.')} — ${e.msg}`).join('\n')
        : (raw || err.message)
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
        tx_height_m: tx.height_m,
        tx_power_dbm: tx.power_dbm,
        frequency_hz: Number(tx.frequency_hz),
        num_azimuths: 36,
        num_elevations: 5,
        max_range_m: propagation.radius_km * 1000,
        num_points: 200,
        ground_material: 'average_ground',
        vegetation_height_m: propagation.clutter_height_m ?? 0,
        building_height_m: 0,
        enable_reflections: true,
        min_signal_dbm: propagation.min_signal_dbm,
      })
      upsertLayer('ray_trace', result.geojson, '#f59e0b')
      setProgress(100)
      toast.success(`Ray trace: ${result.metadata?.num_paths} paths in ${result.metadata?.computation_s}s`)
    } catch (err) {
      toast.error('Ray trace failed: ' + (err.response?.data?.detail || err.message))
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
          activeTab={activeTab}
          coverageRaster={coverageRaster}
          onSetRaster={setCoverageRaster}
          isSimulating={isSimulating}
          progress={progress}
          txActive={txActive}
          sdrActive={!!(sdrFeatures.length || sdrCoverage)}
          onClear={() => { if (mainMode === 'geolocation') { setLobs([]); setCapGroups({}) } else { handleClearLayers() } }}
          onOpenAtak={() => setAtakPanelOpen(true)}
          onOpenSdr={() => setSdrPanelOpen(true)}
          onOpenUas={() => setUasPanelOpen(true)}
          onRun={runSimulation}
        />
      </header>

      {/* ── Modals ──────────────────────────────────────────────────────── */}
      <AppModals
        helpOpen={helpOpen} onCloseHelp={() => setHelpOpen(false)}
        atakPanelOpen={atakPanelOpen} onCloseAtak={() => setAtakPanelOpen(false)}
        mapCenter={{ lat: tx.lat, lon: tx.lon }}
        packBboxFromMap={packBboxFromMap} awaitingPackBboxRef={awaitingPackBboxRef} setDrawMode={setDrawMode}
        sdrPanelOpen={sdrPanelOpen} onCloseSdr={() => setSdrPanelOpen(false)}
        onSdrFeatures={setSdrFeatures} onSdrCoverage={setSdrCoverage}
        archiveOpen={archiveOpen} onCloseArchive={() => setArchiveOpen(false)}
        currentGeojson={currentGeojsonForArchive} currentParams={currentParamsForArchive} onArchiveLoad={handleArchiveLoad}
      />
      {uasPanelOpen && (
        <UasVideoPanel
          onClose={() => setUasPanelOpen(false)}
          mapCenter={{ lat: tx.lat, lon: tx.lon }}
          onLoadGeoJSON={(name, fc) => ul.addGeoJSONLayer(fc, { name })}
          onLocate={(lat, lon) => setRxPoint({ lat, lon })}
        />
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
        <TransmitterPanel tx={tx} setTx={setTx} coordSystem={coordSystem} distUnit={distUnit} setRx={setRx} />
        <PropagationPanel
          propagation={propagation}
          setPropagation={setPropagation}
          resolvedModel={resolveModelFast(tx, propagation)}
          distUnit={distUnit}
        />
        <AntennaPanel tx={tx} setTx={setTx} rx={rx} setRx={setRx} txFrequencyHz={tx.frequency_hz} />
        <AtmospherePanel atmosphere={atmosphere} setAtmosphere={setAtmosphere} txLat={tx.lat} txLon={tx.lon} />

        {/* Extra TXs */}
        {extraTxList.map((entry) => (
          <div key={entry.id} style={{ borderTop: '1px solid #21262d', marginTop: 4 }}>
            <div style={{ display: 'flex', alignItems: 'center', padding: '6px 12px 0', gap: 6 }}>
              <div style={{ width: 10, height: 10, borderRadius: '50%', background: entry.color, flexShrink: 0 }} />
              <EditableLabel value={entry.label} onChange={label => renameExtraTx(entry.id, label)} />
              <button
                className="btn btn-ghost"
                style={{ padding: '2px 6px', color: '#ef4444' }}
                onClick={() => removeTransmitter(entry.id)}
              >
                <X size={12} />
              </button>
            </div>
            <TransmitterPanel
              tx={entry.tx}
              setTx={(newTx) => updateExtraTx(entry.id, newTx)}
              coordSystem={coordSystem}
              distUnit={distUnit}
            />
            <PropagationPanel
              propagation={entry.propagation ?? propagation}
              setPropagation={(upd) => updateExtraPropagation(entry.id, upd)}
              resolvedModel={resolveModelFast(entry.tx, entry.propagation ?? propagation)}
              distUnit={distUnit}
            />
            <AntennaPanel
              tx={entry.tx}
              setTx={(newTx) => updateExtraTx(entry.id, newTx)}
              rx={rx}
              setRx={setRx}
              txFrequencyHz={entry.tx.frequency_hz}
            />
            <AtmospherePanel
              atmosphere={entry.atmosphere ?? atmosphere}
              setAtmosphere={(upd) => updateExtraAtmosphere(entry.id, upd)}
              txLat={entry.tx.lat}
              txLon={entry.tx.lon}
            />
          </div>
        ))}

        <div style={{ padding: '4px 12px' }}>
          <button
            className="btn btn-secondary"
            style={{ width: '100%', gap: 6, fontSize: 12 }}
            onClick={addTransmitter}
          >
            <Plus size={13} /> Add Transmitter
          </button>
        </div>

        {/* ── Tab-specific panels ─────────────────────────────────────── */}

        {/* Best Site (candidates) */}
        {activeTab === 'best_site' && (
          <div style={{ borderTop: '1px solid #21262d', padding: '8px 12px' }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 8 }}>
              CANDIDATE SITES
            </div>
            {bestSiteCandidates.length === 0 && (
              <div style={{ fontSize: 11, color: '#444d56', marginBottom: 8 }}>
                Click the map to add candidate sites. At least 2 required.
              </div>
            )}
            {bestSiteCandidates.map((c, i) => (
              <div key={i} style={{
                display: 'flex', alignItems: 'center', gap: 6,
                marginBottom: 4, padding: '4px 6px',
                background: '#0d1117', borderRadius: 4, border: '1px solid #21262d',
              }}>
                <div style={{ width: 8, height: 8, borderRadius: '50%', background: '#f59e0b', flexShrink: 0 }} />
                <div style={{ flex: 1, fontSize: 11, color: '#c9d1d9' }}>
                  {c.label || `Site ${i + 1}`}
                  <span style={{ color: '#444d56', marginLeft: 4 }}>
                    {c.lat.toFixed(4)}, {c.lon.toFixed(4)}
                  </span>
                </div>
                {bestSiteResult?.sites && (() => {
                  const s = bestSiteResult.sites.find(s => Math.abs(s.lat - c.lat) < 0.0001)
                  return s ? <span style={{ fontSize: 10, color: '#06d6a0' }}>{s.covered_area_km2} km²</span> : null
                })()}
                <button
                  className="btn btn-ghost"
                  style={{ padding: '1px 4px', color: '#ef4444' }}
                  onClick={() => setBestSiteCandidates(prev => prev.filter((_, j) => j !== i))}
                >
                  <X size={11} />
                </button>
              </div>
            ))}
            <button
              className="btn btn-secondary"
              style={{ width: '100%', gap: 6, fontSize: 11, marginTop: 4 }}
              onClick={() => setBestSiteCandidates(prev => [
                ...prev,
                { lat: tx.lat + (prev.length % 2 === 0 ? 0.05 : -0.05), lon: tx.lon + (prev.length % 2 === 0 ? 0.05 : -0.05), height_m: tx.height_m, label: `Site ${prev.length + 1}` },
              ])}
            >
              <Plus size={12} /> Add from TX
            </button>
            {bestSiteResult?.sites && (
              <div style={{ marginTop: 8 }}>
                <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 4 }}>RANKING</div>
                {bestSiteResult.sites.map((s, i) => (
                  <div key={i} style={{
                    display: 'flex', justifyContent: 'space-between',
                    fontSize: 11, padding: '2px 0', borderBottom: '1px solid #21262d',
                  }}>
                    <span style={{ color: i === 0 ? '#06d6a0' : '#c9d1d9' }}>{i + 1}. {s.label}</span>
                    <span style={{ color: '#8b949e' }}>{s.covered_area_km2} km² · {s.avg_signal_dbm} dBm</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Radar target */}
        {activeTab === 'radar' && (
          <div style={{ borderTop: '1px solid #21262d', padding: '8px 12px' }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 8 }}>RADAR TARGET</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {RADAR_TARGETS.map(t => (
                <button
                  key={t.rcs}
                  className={`btn ${(propagation.rcs_m2 ?? 1) === t.rcs ? 'btn-primary' : 'btn-secondary'}`}
                  style={{ fontSize: 11, textAlign: 'left', justifyContent: 'flex-start' }}
                  onClick={() => setPropagation(p => ({ ...p, rcs_m2: t.rcs }))}
                >
                  {t.label}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Route analysis */}
        {activeTab === 'route' && (
          <div style={{ borderTop: '1px solid #21262d', padding: '8px 12px' }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 6 }}>ROUTE ANALYSIS</div>
            <div style={{ fontSize: 11, color: '#444d56', marginBottom: 8 }}>
              Draw a polyline on the map. Each waypoint is tested against a fixed receiver.
            </div>
            <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
              <button
                className={`btn ${drawMode === 'route' ? 'btn-primary' : 'btn-secondary'}`}
                style={{ flex: 1, fontSize: 11, gap: 4 }}
                onClick={() => setDrawMode(m => m === 'route' ? null : 'route')}
              >
                <Route size={11} />
                {drawMode === 'route' ? 'Drawing… (right-click to finish)' : 'Draw Route'}
              </button>
              {routeWaypoints.length > 0 && (
                <button
                  className="btn btn-ghost"
                  style={{ fontSize: 11, color: '#ef4444' }}
                  onClick={() => { setRouteWaypoints([]); setDrawMode(null) }}
                >
                  Clear
                </button>
              )}
            </div>
            {routeWaypoints.length > 0 && (
              <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 6 }}>
                {routeWaypoints.length} waypoints drawn
              </div>
            )}
            <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 4 }}>
              Fixed receiver (P2P target):
            </div>
            {routeReceiverPoint ? (
              <div style={{ fontSize: 11, color: '#06d6a0', marginBottom: 6 }}>
                {routeReceiverPoint.lat.toFixed(4)}, {routeReceiverPoint.lon.toFixed(4)}
                <button
                  className="btn btn-ghost"
                  style={{ marginLeft: 8, fontSize: 10, padding: '1px 4px', color: '#ef4444' }}
                  onClick={() => setRouteReceiverPoint(null)}
                >×</button>
              </div>
            ) : (
              <div style={{ fontSize: 11, color: '#444d56', marginBottom: 6 }}>
                Click map in P2P mode to set receiver, or defaults to TX position.
              </div>
            )}
          </div>
        )}

        {/* Multipoint */}
        {activeTab === 'multipoint' && (
          <div style={{ borderTop: '1px solid #21262d', padding: '8px 12px' }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 6 }}>MULTIPOINT ANALYSIS</div>
            <div style={{ fontSize: 11, color: '#444d56', marginBottom: 8 }}>
              Click multiple TX candidate locations. Each is tested against a fixed receiver.
            </div>
            <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
              <button
                className={`btn ${drawMode === 'multipoint' ? 'btn-primary' : 'btn-secondary'}`}
                style={{ flex: 1, fontSize: 11, gap: 4 }}
                onClick={() => setDrawMode(m => m === 'multipoint' ? null : 'multipoint')}
              >
                <MapPin size={11} />
                {drawMode === 'multipoint' ? 'Clicking… (right-click to finish)' : 'Click TX Points'}
              </button>
              {multipointTxs.length > 0 && (
                <button
                  className="btn btn-ghost"
                  style={{ fontSize: 11, color: '#ef4444' }}
                  onClick={() => { setMultipointTxs([]); setDrawMode(null) }}
                >
                  Clear
                </button>
              )}
            </div>
            {multipointTxs.length > 0 && (
              <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 6 }}>
                {multipointTxs.length} TX points
              </div>
            )}
          </div>
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
            result={manetResult}
            isSimulating={isSimulating}
          />
        )}

        {/* Best server */}
        {activeTab === 'best_server' && (
          <div style={{ borderTop: '1px solid #21262d', padding: '8px 12px' }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 6 }}>BEST SERVER TOOL</div>
            <div style={{ fontSize: 11, color: '#444d56', marginBottom: 8 }}>
              Click a query point — the tool finds which of your TX sites serves it best.
              Uses the extra TX list as candidate sites, or add specific sites below.
            </div>
            {bestServerQuery ? (
              <div style={{ fontSize: 11, color: '#06d6a0', marginBottom: 8 }}>
                Query: {bestServerQuery.lat.toFixed(4)}, {bestServerQuery.lon.toFixed(4)}
                <button
                  className="btn btn-ghost"
                  style={{ marginLeft: 8, fontSize: 10, padding: '1px 4px', color: '#ef4444' }}
                  onClick={() => setBestServerQuery(null)}
                >×</button>
              </div>
            ) : (
              <div style={{ fontSize: 11, color: '#444d56', marginBottom: 8 }}>
                Click the map to set the query point.
              </div>
            )}
            {bestServerResult && (
              <div style={{ padding: 8, background: '#0d1117', borderRadius: 4, border: '1px solid #21262d', marginTop: 4 }}>
                <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 4 }}>RANKED SERVERS</div>
                {bestServerResult.sites?.map((s, i) => (
                  <div key={i} style={{
                    display: 'flex', justifyContent: 'space-between',
                    fontSize: 11, padding: '2px 0', borderBottom: '1px solid #21262d',
                  }}>
                    <span style={{ color: i === 0 ? '#06d6a0' : '#c9d1d9' }}>
                      {i === 0 ? '★ ' : ''}{s.label || `Site ${i + 1}`}
                    </span>
                    <span style={{ color: '#8b949e' }}>
                      {s.signal_dbm} dBm · {(s.distance_m / 1000).toFixed(1)} km
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Best Site Polygon */}
        {activeTab === 'best_site_polygon' && (
          <div style={{ borderTop: '1px solid #21262d', padding: '8px 12px' }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 6 }}>
              BEST SITE — POLYGON
            </div>
            <div style={{ fontSize: 11, color: '#444d56', marginBottom: 8 }}>
              Draw a polygon. Grid-sample TX locations within it and find the best.
            </div>
            <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
              <button
                className={`btn ${drawMode === 'polygon' ? 'btn-primary' : 'btn-secondary'}`}
                style={{ flex: 1, fontSize: 11, gap: 4 }}
                onClick={() => setDrawMode(m => m === 'polygon' ? null : 'polygon')}
              >
                <Hexagon size={11} />
                {drawMode === 'polygon' ? 'Click to close polygon' : 'Draw Polygon'}
              </button>
              {polygonCoords.length > 0 && (
                <button
                  className="btn btn-ghost"
                  style={{ fontSize: 11, color: '#ef4444' }}
                  onClick={() => { setPolygonCoords([]); setDrawMode(null) }}
                >
                  Clear
                </button>
              )}
            </div>
            {polygonCoords.length > 0 && (
              <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 8 }}>
                {polygonCoords.length} vertices
              </div>
            )}
            <div style={{ marginBottom: 8 }}>
              <label style={{ fontSize: 11, color: '#8b949e', display: 'block', marginBottom: 4 }}>
                Sample Density: {polygonBsaCoveragePct}%
              </label>
              <input
                type="range" min={5} max={100} step={5}
                value={polygonBsaCoveragePct}
                onChange={e => setPolygonBsaCoveragePct(Number(e.target.value))}
                style={{ width: '100%' }}
              />
            </div>
            {polygonBsaResult?.sites && (
              <div style={{ padding: 8, background: '#0d1117', borderRadius: 4, border: '1px solid #21262d' }}>
                <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 4 }}>
                  Best of {polygonBsaResult.num_candidates} candidates:
                </div>
                {polygonBsaResult.sites.slice(0, 3).map((s, i) => (
                  <div key={i} style={{
                    fontSize: 11, padding: '2px 0', borderBottom: '1px solid #21262d',
                    display: 'flex', justifyContent: 'space-between',
                  }}>
                    <span style={{ color: i === 0 ? '#06d6a0' : '#c9d1d9' }}>
                      {i + 1}. {s.lat?.toFixed(4)}, {s.lon?.toFixed(4)}
                    </span>
                    <span style={{ color: '#8b949e' }}>{s.covered_area_km2} km²</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Ray trace */}
        {activeTab === 'ray_trace' && (
          <div style={{ borderTop: '1px solid #21262d', padding: '8px 12px' }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 6 }}>3D RAY TRACING</div>
            <div style={{ fontSize: 11, color: '#444d56', marginBottom: 8 }}>
              Traces rays from TX, finds terrain intersections, computes Fresnel reflection and
              single-bounce contributions. Uses current TX position and frequency.
            </div>
            <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 6 }}>
              TX: {tx.lat.toFixed(4)}, {tx.lon.toFixed(4)} · f: {(tx.frequency_hz / 1e6).toFixed(1)} MHz
            </div>
          </div>
        )}

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
          showCompassRose={showCompassRose} setShowCompassRose={setShowCompassRose}
          mapBrightness={mapBrightness} setMapBrightness={setMapBrightness}
          flyToTarget={flyToTarget}
          onSaveLocation={handleSaveLocation}
          onImportApi={(api) => { mapImportApiRef.current = api }}
          ul={ul}
          terrainLineMode={terrainLineMode}
          onTerrainLineComplete={handleTerrainLineComplete}
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
        <div className="tabs" style={{ alignItems: 'center' }}>
          <button className={`tab ${bottomTab === 'results' ? 'active' : ''}`} onClick={() => setBottomTab('results')}>Results</button>
          <button className={`tab ${bottomTab === 'terrain' ? 'active' : ''}`} onClick={() => setBottomTab('terrain')}>Terrain Profile</button>
          <button className={`tab ${bottomTab === 'budget' ? 'active' : ''}`} onClick={() => setBottomTab('budget')}>Link Budget</button>
          <button className={`tab ${bottomTab === '3d' ? 'active' : ''}`} onClick={() => setBottomTab('3d')}>3D View</button>
          <button className={`tab ${bottomTab === 'df' ? 'active' : ''}`} onClick={() => setBottomTab('df')}>DF</button>
          <button className={`tab ${bottomTab === 'chat' ? 'active' : ''}`} onClick={() => setBottomTab('chat')}>Chat</button>
          <button className={`tab ${bottomTab === 'dbcalc' ? 'active' : ''}`} onClick={() => setBottomTab('dbcalc')}>dB Calc</button>
          <button className={`tab ${bottomTab === 'layers' ? 'active' : ''}`} onClick={() => setBottomTab('layers')}>
            Layers{(ul.layers.length + ul.drawnFeatures.length) > 0 ? ` (${ul.layers.length + ul.drawnFeatures.length})` : ''}
          </button>
          <button className={`tab ${bottomTab === 'emitters' ? 'active' : ''}`} onClick={() => setBottomTab('emitters')}>
            Emitter Summary
          </button>
          <button className={`tab ${bottomTab === 'savedlocs' ? 'active' : ''}`} onClick={() => setBottomTab('savedlocs')}>
            Saved Locations{savedLocations.length > 0 ? ` (${savedLocations.length})` : ''}
          </button>
          {spaceWeather && (
            <button className={`tab ${bottomTab === 'spacewx' ? 'active' : ''}`} onClick={() => setBottomTab('spacewx')}
              style={{ color: bottomTab === 'spacewx' ? undefined : (spaceWeather.kp_index >= 5 ? '#ef4444' : spaceWeather.kp_index >= 3 ? '#f59e0b' : '#06d6a0') }}>
              Space Wx
            </button>
          )}
          <div style={{ flex: 1 }} />
          <button
            className="btn btn-ghost"
            style={{ padding: '2px 6px', marginRight: 4, flexShrink: 0 }}
            title="Hide bottom panel"
            onClick={() => setBottomOpen(false)}
          >
            <ChevronDown size={13} />
          </button>
        </div>

        <div style={{ flex: 1, minHeight: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
          {bottomTab === 'results' && (
            <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
              <ResultsPanel
                metadata={metadata}
                p2pResult={p2pResult}
                warnings={warnings}
                spaceWeather={spaceWeather}
                activeTab={activeTab}
              />
            </div>
          )}
          {bottomTab === 'df' && (
            <div style={{ flex: 1, minHeight: 0, overflow: 'hidden' }}>
              <DfPanel />
            </div>
          )}
          {bottomTab === 'chat' && (
            <div style={{ flex: 1, minHeight: 0, overflow: 'hidden' }}>
              <ChatPanel onLocate={(lat, lon) => setRxPoint({ lat, lon })} />
            </div>
          )}
          {bottomTab === 'terrain' && (
            <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
              {/* Standalone terrain profile controls */}
              <div style={{
                display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
                padding: '6px 12px', borderBottom: '1px solid #21262d',
                background: '#0d1117', flexShrink: 0,
              }}>
                <span style={{ fontSize: 11, color: '#8b949e', fontWeight: 600 }}>Standalone profile:</span>
                <button
                  className={`btn ${terrainLineMode ? 'btn-primary' : 'btn-ghost'}`}
                  style={{ fontSize: 11, padding: '3px 10px' }}
                  onClick={() => setTerrainLineMode(m => !m)}>
                  {terrainLineMode ? '✏ Drawing… (right-click to finish)' : '✏ Draw line on map'}
                </button>
                {standaloneProfileLoading && (
                  <span style={{ fontSize: 10, color: '#06d6a0' }}>Sampling terrain…</span>
                )}
                {standaloneProfileError && (
                  <span style={{ fontSize: 10, color: '#fca5a5' }}>{standaloneProfileError}</span>
                )}
                {standaloneProfile && (
                  <>
                    <span style={{ fontSize: 10, color: '#8b949e' }}>
                      {standaloneProfile.path.length} pts · {(standaloneProfile.totalM/1000).toFixed(2)} km · src: {standaloneProfile.source}
                    </span>
                    <button className="btn btn-ghost"
                      style={{ fontSize: 11, padding: '3px 8px', color: '#fca5a5' }}
                      onClick={() => setStandaloneProfile(null)}>Clear</button>
                  </>
                )}
                <div style={{ flex: 1 }} />
                {terrainProfile && (
                  <span style={{ fontSize: 10, color: '#06d6a0' }}>● P2P sim profile loaded</span>
                )}
              </div>
              <div style={{ flex: 1, minHeight: 0 }}>
                {standaloneProfile ? (
                  <TerrainProfile
                    profile={{
                      distances_m: standaloneProfile.distances_m,
                      elevations_m: standaloneProfile.elevations_m,
                    }}
                    standalone
                    frequencyHz={0}
                  />
                ) : (
                  <TerrainProfile
                    profile={terrainProfile}
                    txHeight={tx.height_m}
                    rxHeight={rx.height_m}
                    frequencyHz={tx.frequency_hz}
                    propagationModel={propagation.model}
                    waveType={propagation.wave_type}
                    txLat={tx.lat}
                    txLon={tx.lon}
                  />
                )}
              </div>
            </div>
          )}
          {bottomTab === 'layers' && (
            <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
              <LayerManagerPanel
                ul={ul}
                openFileDialog={() => mapImportApiRef.current?.openFileDialog?.()}
              />
            </div>
          )}
          {bottomTab === 'budget' && (
            <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
              <ResultsPanel
                metadata={metadata}
                p2pResult={p2pResult}
                warnings={warnings}
                spaceWeather={spaceWeather}
                activeTab={activeTab}
                showBudget
              />
            </div>
          )}
          {bottomTab === 'dbcalc' && (
            <div style={{ flex: 1, minHeight: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
              <DecibelCalculator embedded />
            </div>
          )}

          {bottomTab === '3d' && (
            <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
              <ThreeDView
                terrainGrid={terrainGrid}
                loading={terrainGridLoading}
                coverageGeoJSON={coverageGeoJSON}
                buildingGeoJSON={buildingGeoJSON}
                tx={tx}
                minSignalDbm={propagation.min_signal_dbm}
              />
            </div>
          )}

          {bottomTab === 'emitters' && (() => {
            const rmsM = (inters, centroid) => {
              if (!centroid || inters.length === 0) return null
              const mpdLon = 111320 * Math.cos(centroid.lat * Math.PI / 180)
              const dists = inters.map(p => Math.sqrt(((p.lat - centroid.lat) * 111320) ** 2 + ((p.lon - centroid.lon) * mpdLon) ** 2))
              return Math.sqrt(dists.reduce((s, d) => s + d * d, 0) / dists.length)
            }
            const fmtM = m => m >= 1000 ? `~${(m / 1000).toFixed(1)} km` : `~${Math.round(m)} m`
            const DEVICE_LABELS = { dmr: 'DMR', imei: 'IMEI', imsi: 'IMSI', mac: 'MAC', callsign: 'Callsign', other: 'ID' }
            const propEmitters = [txActive ? { id: 'primary', label: txLabel, lat: tx.lat, lon: tx.lon, freq: tx.frequency_hz, type: 'propagation' } : null,
              ...extraTxList.map(e => ({ id: e.id, label: e.label, lat: e.tx?.lat ?? e.lat, lon: e.tx?.lon ?? e.lon, freq: e.tx?.frequency_hz ?? e.frequency_hz, type: 'propagation' }))
            ].filter(Boolean)
            const geoEmitters = lobGroups.filter(g => g.lobs.length >= 2).map(grp => {
              const inters = computeGroupIntersections(grp)
              const centroid = computeCentroid(inters)
              const rms = rmsM(inters, centroid)
              return { grp, inters, centroid, rms }
            })
            return (
              <div style={{
                padding: '12px 16px',
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
                gap: 16,
                overflowY: 'auto',
                flex: 1,
                minHeight: 0,
                alignContent: 'start',
              }}>
                {/* Propagation emitters */}
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 10, fontWeight: 700, color: '#00b4d8', marginBottom: 8, textTransform: 'uppercase', letterSpacing: 0.8 }}>
                    Propagation Emitters ({propEmitters.length})
                  </div>
                  {propEmitters.length === 0 && <div style={{ fontSize: 11, color: '#484f58' }}>No emitter placed</div>}
                  {propEmitters.map(e => (
                    <div key={e.id} style={{ background: '#0d1117', border: '1px solid #21262d', borderLeft: '3px solid #00b4d8', borderRadius: 4, padding: '7px 10px', marginBottom: 6 }}>
                      <div style={{ fontSize: 12, fontWeight: 700, color: '#00b4d8' }}>{e.label}</div>
                      <div style={{ fontSize: 10, color: '#8b949e' }}>{e.lat?.toFixed(5)}, {e.lon?.toFixed(5)}</div>
                      {e.freq && <div style={{ fontSize: 10, color: '#484f58' }}>{(e.freq / 1e6).toFixed(3)} MHz</div>}
                    </div>
                  ))}
                </div>
                {/* Lines of bearing */}
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 10, fontWeight: 700, color: '#a78bfa', marginBottom: 8, textTransform: 'uppercase', letterSpacing: 0.8 }}>
                    Lines of Bearing ({lobs.length})
                  </div>
                  <LoBList
                    lobs={lobs}
                    onRemoveLoB={handleRemoveLoB}
                    onEditLoB={(lob) => {
                      setMainMode('geolocation')
                      setEditLobRequestId(lob.id)
                    }}
                    emptyHint="No bearings recorded yet"
                  />
                </div>
                {/* Geolocated emitters */}
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 10, fontWeight: 700, color: '#a78bfa', marginBottom: 8, textTransform: 'uppercase', letterSpacing: 0.8 }}>
                    Geolocated Emitters ({geoEmitters.length})
                  </div>
                  {geoEmitters.length === 0 && <div style={{ fontSize: 11, color: '#484f58' }}>No cuts or fixes yet (need ≥2 LoBs)</div>}
                  {geoEmitters.map(({ grp, centroid, rms }, i) => {
                    const isFix = grp.lobs.length >= 3
                    const color = isFix ? '#ef4444' : '#06d6a0'
                    const avgConf = Math.round(grp.lobs.reduce((s, l) => s + l.confidence_pct, 0) / grp.lobs.length)
                    return (
                      <div key={i} style={{ background: '#0d1117', border: `1px solid ${color}30`, borderLeft: `3px solid ${color}`, borderRadius: 4, padding: '7px 10px', marginBottom: 6 }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                          <span style={{ fontSize: 12, fontWeight: 700, color }}>{isFix ? 'FIX' : 'CUT'} · {(grp.frequency_hz / 1e6).toFixed(3)} MHz</span>
                          <span style={{ fontSize: 10, color: '#8b949e' }}>{grp.lobs.length} LoBs</span>
                        </div>
                        {grp.device_id && (
                          <div style={{ fontSize: 10, color: '#a78bfa', marginTop: 2 }}>
                            {DEVICE_LABELS[grp.device_type] || 'ID'}: {grp.device_id}
                          </div>
                        )}
                        {centroid
                          ? <div style={{ fontSize: 10, color: '#8b949e', marginTop: 2 }}>Location: {centroid.lat.toFixed(5)}, {centroid.lon.toFixed(5)}</div>
                          : <div style={{ fontSize: 10, color: '#ef4444', marginTop: 2 }}>No intersection (parallel bearings?)</div>}
                        {rms != null && <div style={{ fontSize: 10, color: '#484f58' }}>Location accuracy: {fmtM(rms)} RMS</div>}
                        <div style={{ fontSize: 10, color: '#484f58' }}>Mean confidence: {avgConf}%</div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )
          })()}

          {bottomTab === 'savedlocs' && (
            <div style={{ padding: '12px 16px', flex: 1, minHeight: 0, overflowY: 'auto' }}>
              {savedLocations.length === 0 ? (
                <div style={{ fontSize: 12, color: '#484f58', textAlign: 'center', marginTop: 24 }}>
                  No saved locations yet. Search for a place on the map and click ★ to save it.
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  {savedLocations.map(loc => (
                    <div key={loc.id} style={{
                      display: 'flex', alignItems: 'center', gap: 8,
                      background: '#0d1117', border: '1px solid #21262d', borderRadius: 6,
                      padding: '7px 10px',
                    }}>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 12, color: '#e6edf3', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {loc.name}
                        </div>
                        <div style={{ fontSize: 10, color: '#484f58' }}>
                          {loc.lat.toFixed(5)}, {loc.lon.toFixed(5)}
                        </div>
                      </div>
                      <button
                        className="btn btn-ghost"
                        style={{ padding: '3px 8px', fontSize: 11, flexShrink: 0 }}
                        title="Fly to this location"
                        onClick={() => setFlyToTarget({ lat: loc.lat, lon: loc.lon, zoom: 12, _t: Date.now() })}
                      >
                        ⊕
                      </button>
                      <button
                        className="btn btn-ghost"
                        style={{ padding: '3px 6px', color: '#ef4444', flexShrink: 0 }}
                        title="Remove"
                        onClick={() => handleRemoveSavedLocation(loc.id)}
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {bottomTab === 'spacewx' && spaceWeather && (() => {
            const sw = spaceWeather
            const kpColor = sw.kp_index >= 5 ? '#ef4444' : sw.kp_index >= 3 ? '#f59e0b' : '#06d6a0'
            const fetchedAt = sw.timestamp_utc
              ? new Date(sw.timestamp_utc).toLocaleString(undefined, {
                  year: 'numeric', month: 'short', day: 'numeric',
                  hour: '2-digit', minute: '2-digit', second: '2-digit',
                  timeZoneName: 'short',
                })
              : null
            return (
              <div style={{ padding: '18px 24px', display: 'flex', flexWrap: 'wrap', gap: 24, flex: 1, minHeight: 0, overflowY: 'auto', alignContent: 'flex-start' }}>
                {fetchedAt && (
                  <div style={{ width: '100%', fontSize: 10, color: '#484f58', marginBottom: -12 }}>
                    Current as of {fetchedAt} · Source: NOAA SWPC
                  </div>
                )}
                <div>
                  <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e', marginBottom: 8, textTransform: 'uppercase', letterSpacing: 0.8 }}>Geomagnetic</div>
                  <div style={{ display: 'flex', gap: 4, alignItems: 'center', marginBottom: 6 }}>
                    <div style={{ width: 10, height: 10, borderRadius: '50%', background: kpColor, flexShrink: 0 }} />
                    <span style={{ fontSize: 13, color: '#e6edf3', fontWeight: 700 }}>Kp {sw.kp_index?.toFixed(1)}</span>
                    {sw.storm_class !== 'None' && <span style={{ fontSize: 11, color: '#f59e0b', marginLeft: 6 }}>Storm {sw.storm_class}</span>}
                  </div>
                  <div style={{ fontSize: 11, color: '#8b949e' }}>F10.7 solar flux: <strong style={{ color: '#e6edf3' }}>{sw.solar_flux_f107?.toFixed(0)} sfu</strong></div>
                </div>
                <div>
                  <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e', marginBottom: 8, textTransform: 'uppercase', letterSpacing: 0.8 }}>HF Propagation</div>
                  <div style={{ fontSize: 11, color: sw.radio_blackout !== 'None' ? '#ef4444' : '#8b949e', marginBottom: 4 }}>
                    Radio blackout: <strong style={{ color: '#e6edf3' }}>{sw.radio_blackout}</strong>
                  </div>
                  <div style={{ fontSize: 11, color: '#8b949e', maxWidth: 320, lineHeight: 1.5 }}>{sw.hf_propagation}</div>
                </div>
                <div>
                  <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e', marginBottom: 8, textTransform: 'uppercase', letterSpacing: 0.8 }}>VHF / Sporadic-E</div>
                  <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                    <div style={{ width: 10, height: 10, borderRadius: '50%', background: sw.vhf_sporadic_e_likely ? '#06d6a0' : '#30363d', flexShrink: 0 }} />
                    <span style={{ fontSize: 11, color: sw.vhf_sporadic_e_likely ? '#06d6a0' : '#8b949e' }}>
                      {sw.vhf_sporadic_e_likely ? 'Sporadic-E possible' : 'No Sporadic-E expected'}
                    </span>
                  </div>
                </div>
              </div>
            )
          })()}
        </div>
      </div>

      <ToastContainer
        position="bottom-right"
        theme="dark"
        toastStyle={{ background: '#161b22', borderColor: '#30363d', color: '#e6edf3' }}
      />
    </div>
  )
}
