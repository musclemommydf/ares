// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { useEffect } from 'react'

/** Select all text whenever a number <input> receives focus, so typing replaces the value. */
export function useNumberFieldSelectAll() {
  useEffect(() => {
    const onFocus = (e) => {
      if (e.target.tagName === 'INPUT' && e.target.type === 'number') {
        const el = e.target
        setTimeout(() => el.select(), 0)
      }
    }
    document.addEventListener('focus', onFocus, true)
    return () => document.removeEventListener('focus', onFocus, true)
  }, [])
}
