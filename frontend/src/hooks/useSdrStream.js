// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * useSdrStream — the always-on SDR/DF live feed.
 *
 * Subscribes to `WS /api/v1/sdr/stream` once at the App level (not inside the SDR
 * console), so devices / LoBs / fixes / GPS and the derived map features +
 * auto-coverage keep flowing whether or not the SDR console is open. The console
 * (SdrPanel) and any other consumer read this shared state via props instead of
 * owning their own subscription.
 *
 * Lifts three things up to the app via the supplied callbacks:
 *   • onFeatures(features[])  — the solver FeatureCollection for the maps
 *   • onCoverage({geojson,…}) — server auto-coverage layer from a fresh fix
 *   • onFixes(fixes[])        — live Cuts/Fixes for the Emitter Summary + auto-coverage
 */
import { useEffect, useState } from 'react'
import { getSdrState, createSdrSocket, getSdrPeers } from '../api/client'

// Translate the server's solver FeatureCollection into the flat feature list the
// maps merge into `geolocationGeoJSON` (tags: lob / cep_ellipse / suspected_emitter).
function featuresFrom(fixes) {
  const last = fixes[fixes.length - 1]
  const gj = last?.geojson
  return Array.isArray(gj?.features) ? gj.features : []
}

export function useSdrStream({ onFeatures, onCoverage, onFixes } = {}) {
  const [devices, setDevices] = useState([])
  const [lobs, setLobs] = useState([])
  const [fixes, setFixes] = useState([])
  const [gps, setGps] = useState(null)
  const [mesh, setMesh] = useState(null)
  const [wsState, setWsState] = useState('connecting')
  const [wsError, setWsError] = useState(null)        // last disconnect cause: { detail, code, t }

  // Lift live Cuts/Fixes (with a centroid) up whenever they change.
  useEffect(() => {
    onFixes?.((fixes || []).filter(f => f?.centroid).map(f => ({
      frequency_hz: f.frequency_hz, centroid: f.centroid, kind: f.kind,
      n_lobs: f.n_lobs, cep: f.cep, t: f.t,
    })))
  }, [fixes, onFixes])

  // Initial snapshot + the single, always-on WebSocket subscription.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const s = await getSdrState()
        if (cancelled) return
        setDevices(s.devices || [])
        setLobs(s.lobs || [])
        setFixes(s.fixes || [])
        setGps(s.gps || null); setMesh(s.mesh || null)
        onFeatures?.(featuresFrom(s.fixes || []))
      } catch { /* backend may not be up yet — the socket will reconnect */ }
    })()
    const sock = createSdrSocket(
      (m) => {
        if (cancelled) return
        setWsState('open')
        setWsError(null)            // a message means the stream is healthy again
        if (m.type === 'snapshot') {
          setDevices(m.devices || [])
          setLobs(m.lobs || [])
          setFixes(m.fixes || [])
          setGps(m.gps || null)
          onFeatures?.(featuresFrom(m.fixes || []))
        } else if (m.type === 'gps') {
          setGps(m.fix || null)
        } else if (m.type === 'device_status') {
          setDevices(prev => prev.map(d => d.id === m.device.id ? m.device : d))
        } else if (m.type === 'lob' || m.type === 'lob_rejected') {
          if (m.type === 'lob') setLobs(prev => [...prev.slice(-127), m.lob])
          if (m.device) setDevices(prev => prev.map(d => d.id === m.device.id ? m.device : d))
        } else if (m.type === 'fix') {
          setFixes(prev => {
            const next = [...prev, m].slice(-32)
            onFeatures?.(featuresFrom(next))
            return next
          })
        } else if (m.type === 'coverage') {
          onCoverage?.({ geojson: m.geojson, frequency_hz: m.frequency_hz, centroid: m.centroid })
        }
      },
      (info) => {
        if (cancelled) return
        setWsState('error')
        const detail = (info && typeof info === 'object') ? info.detail : (info ? String(info) : 'connection failed')
        setWsError({ detail, code: info?.code ?? null, t: Date.now() })
      },
    )
    const meshTimer = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return   // skip the peer poll while the window is hidden
      getSdrPeers().then(r => { if (!cancelled) setMesh({ node_id: r.node_id, node_label: r.node_label, peers: r.status || [] }) }).catch(() => {})
    }, 5000)
    return () => { cancelled = true; sock.close(); clearInterval(meshTimer) }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return { devices, setDevices, lobs, fixes, gps, setGps, mesh, setMesh, wsState, wsError }
}
