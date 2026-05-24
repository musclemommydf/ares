// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { useEffect, useRef } from 'react'

/**
 * Visibility-aware interval polling.
 *
 * Replaces the hand-rolled `tick(); const h = setInterval(tick, ms); return () =>
 * clearInterval(h)` pattern. The key win: it **pauses while the tab/window is
 * hidden** (minimized, backgrounded, other desktop) and resumes — firing once
 * immediately — when it becomes visible again. A backgrounded Ares therefore
 * stops waking the CPU (and, for SDR polls, the backend) every second.
 *
 *   usePolling(fn, intervalMs, { enabled, immediate, deps })
 *
 *   fn         called each tick (kept in a ref, so passing a fresh closure each
 *              render does NOT reset the timer)
 *   intervalMs poll period in ms; <= 0 disables
 *   enabled    when false, no polling at all — tie a poll to its panel/tab being
 *              active (e.g. the DF spectrum only while the DF tab is shown)
 *   immediate  run once on (re)start, including on visibility-resume (default true)
 *   deps       restart the loop when these change (like useEffect deps)
 */
export function usePolling(fn, intervalMs, { enabled = true, immediate = true, deps = [] } = {}) {
  const fnRef = useRef(fn)
  fnRef.current = fn

  useEffect(() => {
    if (!enabled || !intervalMs || intervalMs <= 0) return
    let timer = null
    const run = () => { try { fnRef.current && fnRef.current() } catch { /* ignore */ } }
    const start = () => {
      if (timer != null) return
      if (immediate) run()
      timer = setInterval(run, intervalMs)
    }
    const stop = () => { if (timer != null) { clearInterval(timer); timer = null } }
    const onVisibility = () => { (typeof document !== 'undefined' && document.hidden) ? stop() : start() }

    if (!(typeof document !== 'undefined' && document.hidden)) start()
    if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisibility)
    return () => {
      stop()
      if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibility)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, intervalMs, immediate, ...deps])
}
