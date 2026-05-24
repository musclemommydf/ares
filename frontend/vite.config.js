// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
// vite-plugin-cesium copies Cesium's static assets (Workers, Assets, Widgets CSS)
// into the build and sets CESIUM_BASE_URL. Required by src/components/Map/GlobeView.jsx.
// (Run `npm install` after pulling this branch — `cesium` + `vite-plugin-cesium` are new deps.)
import cesium from 'vite-plugin-cesium'

export default defineConfig({
  plugins: [react(), cesium()],
  server: {
    port: 3000,
    host: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 5000,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('/node_modules/plotly.js') || id.includes('/node_modules/@plotly/')) {
            return 'vendor-plotly'
          }
          if (id.includes('/node_modules/leaflet') || id.includes('/node_modules/react-leaflet')) {
            return 'vendor-leaflet'
          }
          // Cesium is large (~30 MB) — keep it in its own chunk so it only loads
          // when the user switches to the 3D globe view.
          if (id.includes('/node_modules/cesium/')) {
            return 'vendor-cesium'
          }
          if (id.includes('/node_modules/react/') || id.includes('/node_modules/react-dom/') ||
              id.includes('/node_modules/scheduler/')) {
            return 'vendor-react'
          }
          // milsymbol is imported only via dynamic import — let Rollup split
          // it into its own chunk that loads on demand.
          if (id.includes('/node_modules/milsymbol/')) return undefined
          if (id.includes('/node_modules/')) {
            return 'vendor-deps'
          }
        },
      },
    },
  },
  optimizeDeps: {
    include: ['leaflet', 'leaflet.heat'],
  },
})
