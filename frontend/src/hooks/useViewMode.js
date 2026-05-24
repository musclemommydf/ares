// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * useViewMode — tiny global store for the 2D (Leaflet) ⇄ 3D (Cesium globe) toggle.
 * (Workstream B, P0.) The actual mounting of <MapView> vs lazy <GlobeView> is
 * done in App.jsx based on `mode`; this just holds the flag + the last camera
 * view so switching modes preserves location.
 */
import { create } from 'zustand'

export const useViewMode = create((set) => ({
  mode: '2d',                 // '2d' | '3d'
  /** last known map/globe center, shared across modes: { lat, lon, zoom } */
  view: { lat: 39.8, lon: -98.5, zoom: 4 },
  setMode: (mode) => set({ mode }),
  toggleMode: () => set((s) => ({ mode: s.mode === '2d' ? '3d' : '2d' })),
  setView: (view) => set((s) => ({ view: { ...s.view, ...view } })),
}))
