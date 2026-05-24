// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
  onExportGeoJSON: (cb) => ipcRenderer.on('export-geojson', cb),
  onExportPDF:     (cb) => ipcRenderer.on('export-pdf', cb),
  onPurgeCache:    (cb) => ipcRenderer.on('purge-cache', cb),
})

// Desktop-only bridge for the in-app Remote Access panel. Its presence is also how
// the UI detects it's running inside the Electron app (vs a plain browser).
contextBridge.exposeInMainWorld('aresDesktop', {
  isDesktop: true,
  getRemote: () => ipcRenderer.invoke('remote:get'),
  setRemote: (cfg) => ipcRenderer.invoke('remote:set', cfg),
})
