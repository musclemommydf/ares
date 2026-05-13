import { Trash2, Server, Radio, Layers, Calculator, Zap } from 'lucide-react'

const BTN = { gap: 4, fontSize: 11, flexShrink: 0 }

/**
 * The right side of the header: a flex spacer · the GPU badge · Clear · Layers · the
 * ATAK/Server and SDR console buttons · the dB/power calculator · the Run button · and
 * the simulation progress bar (absolutely positioned along the bottom of the <header>,
 * which is the nearest positioned ancestor). The per-pixel raster toggle now lives in
 * the Propagation panel in the left sidebar.
 */
export default function HeaderActions({
  gpuActive, mainMode,
  isSimulating, progress, txActive, sdrActive,
  onClear, onOpenLayers, onOpenAtak, onOpenSdr, onOpenDbCalc, onRun,
}) {
  return (
    <>
      <div className="header-spacer" />
      {gpuActive && <span className="header-badge active" style={{ flexShrink: 0 }}>GPU</span>}

      <button className="btn btn-ghost" title="Clear all map layers" style={BTN} onClick={onClear}>
        <Trash2 size={13} />
      </button>
      <button className="btn btn-ghost" title="Layers — imported KML/GeoJSON/GPX, imagery & tile sources, terrain grids, drawings; session save/load" style={BTN} onClick={onOpenLayers}>
        <Layers size={13} />
      </button>
      <button className="btn btn-ghost" title="ATAK / Server — offline data packs, radio templates, server status" style={BTN} onClick={onOpenAtak}>
        <Server size={13} />
      </button>
      <button
        className={`btn ${sdrActive ? 'btn-primary' : 'btn-ghost'}`}
        title="SDR console — connect single-channel (spectrum/audio) or multi-channel (DF) SDRs; bearings/fixes/auto-coverage stream to ATAK"
        style={BTN} onClick={onOpenSdr}
      >
        <Radio size={13} />
      </button>
      <button
        className="btn btn-ghost"
        title="Decibel / power calculator — convert between dBm · dBW · W · dBμV · V/m · …"
        style={BTN} onClick={onOpenDbCalc}
      >
        <Calculator size={13} />
      </button>

      {mainMode === 'propagation' && (
        <button
          className={`btn ${isSimulating ? 'btn-secondary' : 'btn-primary'}`}
          style={{ gap: 6, fontSize: 13, padding: '5px 14px', flexShrink: 0, opacity: (isSimulating || !txActive) ? 0.5 : 1 }}
          onClick={onRun} disabled={isSimulating || !txActive}
          title={!txActive ? 'Right-click the map to place an emitter first' : undefined}
        >
          {isSimulating
            ? (<><div className="spinner" style={{ width: 12, height: 12, borderWidth: 2 }} />Simulating…</>)
            : (<><Zap size={14} />Run</>)}
        </button>
      )}

      {isSimulating && (
        <div style={{ position: 'absolute', bottom: 0, left: 0, right: 0, height: 3, background: 'var(--bg-tertiary)' }}>
          <div style={{ height: '100%', width: `${progress}%`, background: 'var(--accent-blue)', transition: 'width 0.3s ease' }} />
        </div>
      )}
    </>
  )
}
