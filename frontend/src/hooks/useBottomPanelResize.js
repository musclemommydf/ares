// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { useState, useRef, useCallback, useEffect } from 'react'

/**
 * The draggable bottom panel (charts / results): its height (persisted in the
 * session), the mouse-drag resize handler, and the "auto-grow when the 3-D tab is
 * selected" behaviour. Plotly only listens to window resize — not parent resize —
 * so we dispatch a window resize event while dragging so charts rescale live.
 *
 * @param {number} initialHeight  starting panel height in px
 * @param {string} bottomTab      the active bottom-panel tab (drives the 3-D auto-grow)
 */
export function useBottomPanelResize(initialHeight, bottomTab) {
  const [bottomPanelHeight, setBottomPanelHeight] = useState(initialHeight ?? 240)
  const resizingRef = useRef(false)
  const dragStartY = useRef(0)
  const dragStartH = useRef(0)

  // Auto-expand when the 3-D tab becomes active
  useEffect(() => {
    if (bottomTab === '3d' && bottomPanelHeight < 420) setBottomPanelHeight(520)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bottomTab])

  const handleResizeMouseDown = useCallback((e) => {
    e.preventDefault()
    resizingRef.current = true
    dragStartY.current = e.clientY
    dragStartH.current = bottomPanelHeight

    const onMove = (ev) => {
      if (!resizingRef.current) return
      const delta = dragStartY.current - ev.clientY
      const newH = Math.max(140, Math.min(window.innerHeight - 160, dragStartH.current + delta))
      setBottomPanelHeight(newH)
      window.dispatchEvent(new Event('resize'))   // Plotly listens to window resize, not parent resize
    }
    const onUp = () => {
      resizingRef.current = false
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      window.dispatchEvent(new Event('resize'))
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [bottomPanelHeight])

  return { bottomPanelHeight, setBottomPanelHeight, handleResizeMouseDown }
}
