import { Trash2, Server, Radio, Video, Zap } from 'lucide-react'

const BTN = { gap: 4, fontSize: 11, flexShrink: 0 }

/**
 * The right side of the header: a flex spacer · the GPU badge · Clear · the ATAK/Server,
 * SDR and UAS-Video console buttons · (on the coverage tab) the raster checkbox · the Run
 * button · and the simulation progress bar (absolutely positioned along the bottom of the
 * <header>, which is the nearest positioned ancestor).
 */
export default function HeaderActions({
  gpuActive, mainMode, activeTab, coverageRaster, onSetRaster,
  isSimulating, progress, txActive, sdrActive,
  onClear, onOpenAtak, onOpenSdr, onOpenUas, onRun,
}) {
  return (
    <>
      <div className="header-spacer" />
      {gpuActive && <span className="header-badge active" style={{ flexShrink: 0 }}>GPU</span>}

      <button className="btn btn-ghost" title="Clear all map layers" style={BTN} onClick={onClear}>
        <Trash2 size={13} />
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
        title="UAS Video — scan a band for drone video downlinks (analog · DVB-T/T2/S/S2 · COFDM · …), decode/characterise, exploit the MPEG-TS → MISB metadata → footprint → ATAK"
        style={BTN} onClick={onOpenUas}
      >
        <Video size={13} />
      </button>

      {mainMode === 'propagation' && activeTab === 'coverage' && (
        <label title="Per-pixel raster coverage — one ITM path per grid cell (even coverage everywhere, no thinning at range; heavier than the radial sweep)"
               style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, color: coverageRaster ? '#06d6a0' : '#8b949e', flexShrink: 0, cursor: 'pointer' }}>
          <input type="checkbox" checked={coverageRaster} onChange={e => onSetRaster(e.target.checked)} /> raster
        </label>
      )}
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
