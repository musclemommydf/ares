// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { useState, useRef } from 'react'

/** A span that turns into an inline text input when clicked — used for renaming TX layers. */
export default function EditableLabel({ value, onChange, style }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value)
  const inputRef = useRef(null)

  const commit = () => {
    const trimmed = draft.trim()
    if (trimmed) onChange(trimmed)
    else setDraft(value)
    setEditing(false)
  }

  if (editing) {
    return (
      <input
        ref={inputRef}
        value={draft}
        onChange={e => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={e => {
          if (e.key === 'Enter') commit()
          if (e.key === 'Escape') { setDraft(value); setEditing(false) }
        }}
        style={{
          fontSize: 11, fontWeight: 600, color: '#e6edf3',
          background: 'transparent', border: 'none', borderBottom: '1px solid #58a6ff',
          outline: 'none', padding: '0 2px', width: '100%', minWidth: 0,
          ...style,
        }}
        autoFocus
      />
    )
  }
  return (
    <span
      title="Click to rename"
      onClick={() => { setDraft(value); setEditing(true) }}
      style={{
        fontSize: 11, fontWeight: 600, color: '#8b949e', flex: 1,
        cursor: 'text', borderBottom: '1px solid transparent',
        ...style,
      }}
    >
      {value}
    </span>
  )
}
