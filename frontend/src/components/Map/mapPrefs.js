/**
 * Shared map display preferences (basemap, feature colours, coverage render mode).
 * Both the 2D Leaflet map and the 3D Cesium globe read from here so their toolbars
 * stay in sync (a colour or basemap chosen in one view applies to the other).
 */
import { create } from 'zustand'

// Raster basemaps usable by BOTH engines. Leaflet uses {s}/{r}; Cesium's
// UrlTemplateImageryProvider uses {s} with `subdomains` (and ignores {r}).
export const BASEMAPS = {
  dark: {
    label: 'Dark',
    url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
    subdomains: 'abcd',
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
  },
  satellite: {
    label: 'Satellite',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    attribution: 'Tiles &copy; Esri',
  },
  topo: {
    label: 'Topo',
    url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
    subdomains: 'abc',
    attribution: '&copy; OpenTopoMap contributors',
  },
  osm: {
    label: 'OSM',
    url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    attribution: '&copy; OpenStreetMap contributors',
  },
}

export const DEFAULT_MAP_COLORS = {
  ruler:           '#f59e0b',
  emitter:         '#00b4d8',
  lobCut:          '#06d6a0',
  lobFix:          '#ef4444',
  draw:            '#a855f7',
  lobLineOverride: null,
}

export const useMapPrefs = create((set) => ({
  basemapId: 'dark',                          // key into BASEMAPS
  mapColors: DEFAULT_MAP_COLORS,
  coverageMode: 'auto',                       // 'auto' | 'raster' | 'points'  (globe coverage rendering)
  coverageHeight: 'beam',                     // 'ground' (clamp to terrain) | 'beam' (extrude at the antenna beam-height midpoint)
  setBasemapId: (id) => set({ basemapId: id }),
  setMapColors: (mc) => set((s) => ({ mapColors: typeof mc === 'function' ? mc(s.mapColors) : mc })),
  resetMapColors: () => set({ mapColors: DEFAULT_MAP_COLORS }),
  setCoverageMode: (m) => set({ coverageMode: m }),
  setCoverageHeight: (h) => set({ coverageHeight: h }),
}))
