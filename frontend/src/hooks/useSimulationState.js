// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { useState, useRef } from 'react'

/**
 * Shared state for the simulation runners: whether a sim is in flight, its progress %,
 * and an abort-controller ref the runners use to cancel an in-flight request. (The
 * runners themselves still live in App for now — this is the first step toward a full
 * useSimulations() hook.)
 */
export function useSimulationState() {
  const [isSimulating, setIsSimulating] = useState(false)
  const [progress, setProgress] = useState(0)
  const abortRef = useRef(null)
  return { isSimulating, setIsSimulating, progress, setProgress, abortRef }
}
