// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { useEffect } from 'react'

/**
 * Debounced auto-save of the UI session to localStorage. App builds the session as a
 * JSON string (via a useMemo keyed on the bits that should trigger a save); this hook
 * just writes it `delayMs` after it last changed, and is a no-op (swallowed) if
 * localStorage is full or unavailable.
 *
 * @param {string} key      the localStorage key (SESSION_KEY)
 * @param {string} json     the serialised session (only re-saved when this string changes)
 * @param {number} delayMs  debounce delay; default 1000
 */
export function useSessionAutosave(key, json, delayMs = 1000) {
  useEffect(() => {
    const timer = setTimeout(() => {
      try { localStorage.setItem(key, json) } catch { /* localStorage full or unavailable — ignore */ }
    }, delayMs)
    return () => clearTimeout(timer)
  }, [key, json, delayMs])
}
