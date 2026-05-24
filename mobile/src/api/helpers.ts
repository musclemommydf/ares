// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

export function dbmToQuality(dbm: number) {
  if (dbm >= -60) return { label: 'Excellent', color: '#06d6a0', bars: 5 }
  if (dbm >= -75) return { label: 'Good', color: '#84cc16', bars: 4 }
  if (dbm >= -90) return { label: 'Fair', color: '#f59e0b', bars: 3 }
  if (dbm >= -100) return { label: 'Poor', color: '#ef4444', bars: 2 }
  return { label: 'None', color: '#6b7280', bars: 0 }
}

export function formatFreq(hz: number): string {
  if (hz >= 1e9) return `${(hz / 1e9).toFixed(3)} GHz`
  if (hz >= 1e6) return `${(hz / 1e6).toFixed(3)} MHz`
  if (hz >= 1e3) return `${(hz / 1e3).toFixed(1)} kHz`
  return `${hz.toFixed(0)} Hz`
}
