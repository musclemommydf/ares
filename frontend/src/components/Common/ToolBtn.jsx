// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/** A compact toolbar button: a lucide icon over a small label, primary when active. */
export default function ToolBtn({ icon: Icon, label, active, onClick, style }) {
  return (
    <button
      className={`btn ${active ? 'btn-primary' : 'btn-ghost'}`}
      title={label}
      onClick={onClick}
      style={{ gap: 4, fontSize: 11, padding: '4px 8px', ...style }}
    >
      <Icon size={13} />
      <span style={{ fontSize: 10 }}>{label}</span>
    </button>
  )
}
