// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * ViewModeToggle — a small 2D / 3D button for the map toolbar (Workstream B, P0).
 * Drop it next to the other map controls in App.jsx.
 */
import { Globe, Map as MapIcon } from 'lucide-react'
import { useViewMode } from '../../hooks/useViewMode'

export default function ViewModeToggle() {
  const mode = useViewMode((s) => s.mode)
  const toggleMode = useViewMode((s) => s.toggleMode)
  const next = mode === '2d' ? '3D globe' : '2D map'
  return (
    <button
      onClick={toggleMode}
      title={`Switch to ${next}`}
      style={{
        display: 'flex', alignItems: 'center', gap: 6,
        background: '#161b22', color: '#e6edf3',
        border: '1px solid #30363d', borderRadius: 6,
        font: '12px system-ui', padding: '5px 9px', cursor: 'pointer',
      }}
    >
      {mode === '2d' ? <Globe size={14} /> : <MapIcon size={14} />}
      {mode === '2d' ? '3D' : '2D'}
    </button>
  )
}
