import { ChevronDown } from 'lucide-react'

const SIMPLE_TABS = [
  ['results', 'Results'], ['terrain', 'Terrain Profile'], ['budget', 'Link Budget'],
  ['3d', '3D View'], ['df', 'DF'], ['chat', 'Chat'], ['dbcalc', 'dB Calc'],
]

/**
 * The bottom-panel tab bar: Results / Terrain Profile / Link Budget / 3D View / DF /
 * Chat / dB Calc / Layers / Emitter Summary / Saved Locations / (Space Wx, when space
 * weather is available), plus the hide button. App owns the active tab, the counts,
 * the spaceWeather gate and the close action.
 */
export default function BottomPanelTabs({ active, onSelect, layerCount, savedCount, spaceWeather, onClose }) {
  return (
    <div className="tabs" style={{ alignItems: 'center' }}>
      {SIMPLE_TABS.map(([id, label]) => (
        <button key={id} className={`tab ${active === id ? 'active' : ''}`} onClick={() => onSelect(id)}>{label}</button>
      ))}
      <button className={`tab ${active === 'layers' ? 'active' : ''}`} onClick={() => onSelect('layers')}>
        Layers{layerCount > 0 ? ` (${layerCount})` : ''}
      </button>
      <button className={`tab ${active === 'emitters' ? 'active' : ''}`} onClick={() => onSelect('emitters')}>
        Emitter Summary
      </button>
      <button className={`tab ${active === 'savedlocs' ? 'active' : ''}`} onClick={() => onSelect('savedlocs')}>
        Saved Locations{savedCount > 0 ? ` (${savedCount})` : ''}
      </button>
      {spaceWeather && (
        <button className={`tab ${active === 'spacewx' ? 'active' : ''}`} onClick={() => onSelect('spacewx')}
          style={{ color: active === 'spacewx' ? undefined : (spaceWeather.kp_index >= 5 ? '#ef4444' : spaceWeather.kp_index >= 3 ? '#f59e0b' : '#06d6a0') }}>
          Space Wx
        </button>
      )}
      <div style={{ flex: 1 }} />
      <button className="btn btn-ghost" style={{ padding: '2px 6px', marginRight: 4, flexShrink: 0 }} title="Hide bottom panel" onClick={onClose}>
        <ChevronDown size={13} />
      </button>
    </div>
  )
}
