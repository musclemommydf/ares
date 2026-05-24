// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Global state store using React hooks (simple useState-based store pattern).
 * For larger apps this would use Zustand or Redux Toolkit.
 */
import { useState, createContext, useContext } from 'react'

interface SimulatorState {
  txLat: number; txLon: number
  txHeight: number; txAltitude: number
  powerDbm: number; frequencyHz: number
  rxLat: number; rxLon: number
  rxHeight: number; rxAltitude: number
  rxSensitivity: number
  model: string
  radiusKm: number
  minSignalDbm: number
  useGpu: boolean
  txAntenna: any; rxAntenna: any
  setTxLat: (v: number) => void; setTxLon: (v: number) => void
  setTxHeight: (v: number) => void; setTxAltitude: (v: number) => void
  setPowerDbm: (v: number) => void; setFrequencyHz: (v: number) => void
  setRxLat: (v: number) => void; setRxLon: (v: number) => void
  setRxHeight: (v: number) => void; setRxAltitude: (v: number) => void
  setRxSensitivity: (v: number) => void
  setModel: (v: string) => void
  setRadiusKm: (v: number) => void
  setMinSignalDbm: (v: number) => void
  setUseGpu: (v: boolean) => void
}

// Simple module-level state (no context needed for small app)
let _state = {
  txLat: 37.7749, txLon: -122.4194,
  txHeight: 1.8288, txAltitude: 0,  // 6ft AGL default
  powerDbm: 27, frequencyHz: 433e6,
  rxLat: 37.9, rxLon: -122.0,
  rxHeight: 1.5, rxAltitude: 0,
  rxSensitivity: -100,
  model: 'itm',
  radiusKm: 50,
  minSignalDbm: -100,
  useGpu: false,
  txAntenna: { type: 'dipole_half_wave', gain_dbi: null, tilt_deg: 0, azimuth_deg: 0,
               height_m: 1.8288, diameter_m: 1.2, efficiency: 0.55,  // 6ft AGL default
               elements: 3, array_elements: 8, polarization: 'vertical' },
  rxAntenna: { type: 'dipole_half_wave', gain_dbi: null, tilt_deg: 0, azimuth_deg: 0,
               height_m: 1.5, diameter_m: 1.2, efficiency: 0.55,
               elements: 3, array_elements: 8, polarization: 'vertical' },
}
const _listeners: Set<() => void> = new Set()

function setState(partial: Partial<typeof _state>) {
  _state = { ..._state, ...partial }
  _listeners.forEach(l => l())
}

import { useState as useStateHook, useEffect } from 'react'

export function useSimulatorStore() {
  const [, rerender] = useStateHook(0)
  useEffect(() => {
    const listener = () => rerender(n => n + 1)
    _listeners.add(listener)
    return () => { _listeners.delete(listener) }
  }, [])

  return {
    ..._state,
    setTxLat: (v: number) => setState({ txLat: v }),
    setTxLon: (v: number) => setState({ txLon: v }),
    setTxHeight: (v: number) => setState({ txHeight: v }),
    setTxAltitude: (v: number) => setState({ txAltitude: v }),
    setPowerDbm: (v: number) => setState({ powerDbm: v }),
    setFrequencyHz: (v: number) => setState({ frequencyHz: v }),
    setRxLat: (v: number) => setState({ rxLat: v }),
    setRxLon: (v: number) => setState({ rxLon: v }),
    setRxHeight: (v: number) => setState({ rxHeight: v }),
    setRxAltitude: (v: number) => setState({ rxAltitude: v }),
    setRxSensitivity: (v: number) => setState({ rxSensitivity: v }),
    setModel: (v: string) => setState({ model: v }),
    setRadiusKm: (v: number) => setState({ radiusKm: v }),
    setMinSignalDbm: (v: number) => setState({ minSignalDbm: v }),
    setUseGpu: (v: boolean) => setState({ useGpu: v }),
  }
}
