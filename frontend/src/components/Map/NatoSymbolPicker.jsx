// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Picker UI for placing NATO MIL-STD-2525 / APP-6 symbols on the map.
 * Lists curated categories (Units, Equipment, Tactical, Combat Service, CBRN,
 * Ranger Handbook, IADS, Special Operations), supports text search and an
 * affiliation toggle, and renders each preset using milsymbol so the user
 * previews the actual icon.
 */
import { useMemo, useState } from 'react'
import {
  AFFILIATIONS,
  SYMBOL_CATALOGUE,
  SYMBOL_INDEX,
  applyAffiliation,
  renderSymbolPreview,
  makeSidcIcon,
} from './NatoSymbols'

export default function NatoSymbolPicker({ ctrl, onArm }) {
  const [aff, setAff] = useState('F')
  const [query, setQuery] = useState('')
  const [label, setLabel] = useState('')
  const [activeSidc, setActiveSidc] = useState(null)

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return SYMBOL_CATALOGUE
    const out = {}
    SYMBOL_INDEX.forEach(item => {
      if (item.name.toLowerCase().includes(q) ||
          item.sidc.toLowerCase().includes(q) ||
          item.category.toLowerCase().includes(q)) {
        if (!out[item.category]) out[item.category] = []
        out[item.category].push(item)
      }
    })
    return out
  }, [query])

  const arm = (baseSidc, name, affiliation = aff, lbl = label) => {
    setActiveSidc(baseSidc)
    const fullSidc = applyAffiliation(baseSidc, affiliation)
    const icon = makeSidcIcon(fullSidc, { size: 36, uniqueDesignation: lbl })
    onArm({
      sidc: fullSidc,
      baseSidc,
      affiliation,
      label: lbl,
      name,
      icon,
    })
  }

  const totalCount = Object.values(filtered).reduce((n, list) => n + list.length, 0)

  return (
    <div style={{ padding: '6px 4px' }}>
      <div style={{
        display: 'flex', gap: 4, marginBottom: 6, alignItems: 'center',
      }}>
        {AFFILIATIONS.map(a => (
          <button key={a.id}
            className={`btn ${aff === a.id ? 'btn-primary' : 'btn-ghost'}`}
            style={{
              flex: 1, padding: '3px 4px', fontSize: 10,
              borderColor: aff === a.id ? a.color : '#30363d',
              color: aff === a.id ? '#fff' : a.color,
            }}
            onClick={() => {
              setAff(a.id)
              if (activeSidc) arm(activeSidc, '', a.id, label)
            }}>
            {a.label}
          </button>
        ))}
      </div>

      <input
        value={query}
        onChange={e => setQuery(e.target.value)}
        placeholder="Search symbols (e.g. infantry, tank, OBJ)…"
        style={{
          width: '100%', background: '#0d1117', border: '1px solid #30363d',
          borderRadius: 4, color: '#e6edf3', fontSize: 11, padding: '4px 7px',
          outline: 'none', boxSizing: 'border-box', marginBottom: 6,
        }}
      />

      <input
        value={label}
        onChange={e => {
          setLabel(e.target.value)
          if (activeSidc) arm(activeSidc, '', aff, e.target.value)
        }}
        placeholder="Optional designation / callsign…"
        style={{
          width: '100%', background: '#0d1117', border: '1px solid #30363d',
          borderRadius: 4, color: '#e6edf3', fontSize: 10, padding: '3px 7px',
          outline: 'none', boxSizing: 'border-box', marginBottom: 6,
        }}
      />

      <div style={{ maxHeight: 320, overflowY: 'auto' }}>
        {totalCount === 0 && (
          <div style={{ fontSize: 10, color: '#484f58', textAlign: 'center', padding: 12 }}>
            No symbols match "{query}"
          </div>
        )}
        {Object.entries(filtered).map(([cat, items]) => (
          <div key={cat} style={{ marginBottom: 6 }}>
            <div style={{
              fontSize: 9, fontWeight: 700, color: '#8b949e',
              padding: '2px 4px', textTransform: 'uppercase', letterSpacing: 0.6,
            }}>{cat}</div>
            <div style={{
              display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 2,
            }}>
              {items.map(item => {
                const sidc = applyAffiliation(item.sidc, aff)
                const preview = renderSymbolPreview(sidc, 26)
                const active = activeSidc === item.sidc
                return (
                  <button key={item.sidc + item.name}
                    className={`btn ${active ? 'btn-primary' : 'btn-ghost'}`}
                    style={{
                      padding: '4px 2px', fontSize: 9,
                      display: 'flex', flexDirection: 'column', alignItems: 'center',
                      gap: 2, lineHeight: 1.1, minHeight: 56,
                    }}
                    title={`${item.name} · ${sidc}`}
                    onClick={() => arm(item.sidc, item.name)}>
                    <span style={{
                      width: 30, height: 30, display: 'flex',
                      alignItems: 'center', justifyContent: 'center',
                    }}
                    dangerouslySetInnerHTML={{ __html: preview }} />
                    <span style={{ textAlign: 'center', wordBreak: 'break-word' }}>
                      {item.name}
                    </span>
                  </button>
                )
              })}
            </div>
          </div>
        ))}
      </div>

      {activeSidc && (
        <div style={{
          marginTop: 6, padding: '4px 6px', fontSize: 10,
          background: '#0d1117', border: '1px solid #30363d', borderRadius: 4,
          color: '#06d6a0',
        }}>
          ✓ Click on the map to place. SIDC: <code>{applyAffiliation(activeSidc, aff)}</code>
        </div>
      )}
    </div>
  )
}
