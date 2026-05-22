import { Undo2, Redo2, Save, FolderOpen, Upload, Trash2, HelpCircle } from 'lucide-react'

const HDR = { fontSize: 10, color: '#484f58', padding: '3px 14px', letterSpacing: 0.7, fontWeight: 600 }
const SEP = { height: 1, background: '#21262d', margin: '4px 0' }

/**
 * The header hamburger dropdown — Edit (undo/redo) · File. App owns the
 * positioned wrapper, the hamburger button and the outside-click close; this
 * component is just the menu body (renders nothing when closed).
 *
 * Former Analysis Tools have all moved to where they're used: Draw Bounds → the
 * map's ✎ drawing-tools dropdown; Interference Analysis + Super Layer → the
 * Emitter Summary panel; saved results → the Layer Manager ("Saved results").
 */
export default function OverflowMenu({
  open, onClose,
  canUndo, canRedo, undoTick, onUndo, onRedo,
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
