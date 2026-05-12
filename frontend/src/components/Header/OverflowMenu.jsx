import { Undo2, Redo2, Square, Layers, GitMerge, Satellite, Archive, Save, FolderOpen, Upload, Trash2, HelpCircle } from 'lucide-react'

const HDR = { fontSize: 10, color: '#484f58', padding: '3px 14px', letterSpacing: 0.7, fontWeight: 600 }
const SEP = { height: 1, background: '#21262d', margin: '4px 0' }

/**
 * The header hamburger dropdown — Edit (undo/redo) · Analysis Tools (propagation mode
 * only) · File. App owns the positioned wrapper, the hamburger button and the
 * outside-click close; this component is just the menu body (renders nothing when closed).
 */
export default function OverflowMenu({
  open, onClose, mainMode,
  canUndo, canRedo, undoTick, onUndo, onRedo,
  drawMode, onToggleBoundsDraw, isSimulating, onInterference, onSuperLayer,
  satToolActive, onToggleSatTool, onOpenArchive,
  onSaveState, onLoadState, onImport, onPurgeCache, onOpenHelp,
}) {
  if (!open) return null
  return (
    <div
      style={{
        position: 'absolute', top: '110%', left: 0, zIndex: 9999,
        background: '#161b22', border: '1px solid #30363d', borderRadius: 8,
        minWidth: 210, boxShadow: '0 6px 20px rgba(0,0,0,0.7)', padding: '4px 0',
      }}
      onClick={onClose}
    >
      <div style={HDR}>EDIT</div>
      <button className="overflow-menu-item" disabled={!canUndo} onClick={onUndo}>
        <Undo2 size={13} /> Undo
        <span style={{ marginLeft: 'auto', fontSize: 10, color: '#484f58' }}>Ctrl+Z</span>
      </button>
      <button className="overflow-menu-item" disabled={!canRedo} onClick={onRedo}>
        <Redo2 size={13} /> Redo
        <span style={{ marginLeft: 'auto', fontSize: 10, color: '#484f58' }}>Ctrl+R</span>
      </button>
      {/* read undoTick so the disabled state stays in sync */}
      <span style={{ display: 'none' }}>{undoTick}</span>

      {mainMode === 'propagation' && <>
        <div style={SEP} />
        <div style={HDR}>ANALYSIS TOOLS</div>
        <button className="overflow-menu-item" onClick={onToggleBoundsDraw}>
          <Square size={13} style={{ color: drawMode === 'bounds' ? '#a855f7' : undefined }} />
          Draw Bounds{drawMode === 'bounds' ? ' ✓' : ''}
        </button>
        <button className="overflow-menu-item" disabled={isSimulating} onClick={onInterference}>
          <Layers size={13} /> Interference Analysis
        </button>
        <button className="overflow-menu-item" disabled={isSimulating} onClick={onSuperLayer}>
          <GitMerge size={13} /> Super Layer
        </button>
        <button className="overflow-menu-item" onClick={onToggleSatTool}>
          <Satellite size={13} style={{ color: satToolActive ? '#06d6a0' : undefined }} />
          Satellite Visibility{satToolActive ? ' ✓' : ''}
        </button>
        <button className="overflow-menu-item" onClick={onOpenArchive}>
          <Archive size={13} /> Archive
        </button>
      </>}

      <div style={SEP} />
      <div style={HDR}>FILE</div>
      <button className="overflow-menu-item" onClick={onSaveState}><Save size={13} /> Save State</button>
      <button className="overflow-menu-item" onClick={onLoadState}><FolderOpen size={13} /> Load State</button>
      <button className="overflow-menu-item" onClick={onImport}><Upload size={13} /> Import KML / KMZ / Image…</button>
      <div style={SEP} />
      <button className="overflow-menu-item" onClick={onPurgeCache}><Trash2 size={13} /> Purge Cache</button>
      <button className="overflow-menu-item" onClick={onOpenHelp}><HelpCircle size={13} /> Help</button>
    </div>
  )
}
