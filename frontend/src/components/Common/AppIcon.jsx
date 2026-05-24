// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * App icon — Ares logo (dagger + RF arcs).
 */
export default function AppIcon({ size = 20 }) {
  return (
    <img
      src="/icon.png"
      alt="Ares"
      width={size}
      height={size}
      style={{ borderRadius: Math.round(size * 0.2), display: 'block', flexShrink: 0 }}
    />
  )
}
