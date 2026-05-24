// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { Plus, X } from 'lucide-react'
import EditableLabel from '../Common/EditableLabel'
import TransmitterPanel from './TransmitterPanel'
import PropagationPanel from './PropagationPanel'
import AntennaPanel from './AntennaPanel'
import AtmospherePanel from './AtmospherePanel'

/**
 * The left-panel emitter editor. Instead of stacking a full parameter form per
 * emitter (which produced duplicate inputs at the bottom), this shows ONE emitter
 * at a time: a compact selector of all emitters at the top, a header labelling the
 * one being edited, then that emitter's TX / propagation / antenna / atmosphere
 * panels. Selecting another emitter (here or via the Emitter Summary "Edit" button)
 * swaps the panel to it.
 */
export default function EmitterEditor({
  emitters, editingId, onSelect, onAdd, onRemove,
  selected, setTx, setPropagation, setAtmosphere, onRename,
  coordSystem, distUnit, rx, setRx, resolveModelFast,
  activeTab, coverageRaster, onSetRaster,
}) {
  if (!selected) return null
  const chip = (active, color) => ({
    display: 'inline-flex', alignItems: 'center', gap: 5, padding: '3px 8px', fontSize: 11,
    background: active ? '#1a2f4a' : '#0d1117', color: active ? '#e6edf3' : '#8b949e',
    border: `1px solid ${active ? '#1f6feb' : '#21262d'}`, borderRadius: 12, cursor: 'pointer',
    maxWidth: 150, overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis',
  })
  return (
    <>
      {/* Emitter selector */}
      <div style={{ padding: '8px 12px', display: 'flex', flexWrap: 'wrap', gap: 4, borderBottom: '1px solid #21262d' }}>
        {emitters.map((em) => (
          <button key={em.id} type="button" onClick={() => onSelect(em.id)} style={chip(em.id === editingId, em.color)} title={em.label}>
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: em.color, flexShrink: 0 }} />
            {em.label}
          </button>
        ))}
        <button type="button" onClick={onAdd} title="Add transmitter"
          style={{ padding: '3px 8px', fontSize: 11, background: '#0d1117', color: '#06d6a0',
            border: '1px dashed #30363d', borderRadius: 12, cursor: 'pointer',
            display: 'inline-flex', alignItems: 'center', gap: 4 }}>
          <Plus size={12} /> Add
        </button>
      </div>

      {/* Which emitter is being edited */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px 2px' }}>
        <span style={{ fontSize: 9, fontWeight: 700, color: '#6e7681', textTransform: 'uppercase', letterSpacing: 0.7, flexShrink: 0 }}>
          Editing
        </span>
        <span style={{ width: 10, height: 10, borderRadius: '50%', background: selected.color, flexShrink: 0 }} />
        <EditableLabel value={selected.label} onChange={onRename} />
        <span style={{ fontSize: 10, color: '#444d56', flexShrink: 0 }}>{selected.isPrimary ? 'Primary' : 'Extra'}</span>
        {!selected.isPrimary && (
          <button className="btn btn-ghost" style={{ marginLeft: 'auto', padding: '2px 6px', color: '#ef4444' }}
            onClick={() => onRemove(selected.id)} title="Remove this transmitter">
            <X size={12} />
          </button>
        )}
      </div>

      <TransmitterPanel tx={selected.tx} setTx={setTx} coordSystem={coordSystem} distUnit={distUnit} setRx={setRx} />
      <PropagationPanel
        propagation={selected.propagation}
        setPropagation={setPropagation}
        resolvedModel={resolveModelFast(selected.tx, selected.propagation)}
        distUnit={distUnit}
        activeTab={selected.isPrimary ? activeTab : undefined}
        coverageRaster={selected.isPrimary ? coverageRaster : undefined}
        onSetRaster={selected.isPrimary ? onSetRaster : undefined}
      />
      <AntennaPanel tx={selected.tx} setTx={setTx} rx={rx} setRx={setRx} txFrequencyHz={selected.tx.frequency_hz} />
      <AtmospherePanel atmosphere={selected.atmosphere} setAtmosphere={setAtmosphere} txLat={selected.tx.lat} txLon={selected.tx.lon} />
    </>
  )
}
