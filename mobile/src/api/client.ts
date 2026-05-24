// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Mobile API client — connects to the RF Simulator Python backend.
 */
import AsyncStorage from '@react-native-async-storage/async-storage'

const DEFAULT_URL = 'http://10.0.2.2:8000'  // Android emulator → host machine

async function getBaseUrl(): Promise<string> {
  try {
    const saved = await AsyncStorage.getItem('serverUrl')
    return saved || DEFAULT_URL
  } catch {
    return DEFAULT_URL
  }
}

async function apiFetch(path: string, options: RequestInit = {}) {
  const base = await getBaseUrl()
  const url = `${base}/api/v1${path}`
  const res = await fetch(url, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers },
  })
  if (!res.ok) {
    const err = await res.text()
    throw new Error(`API error ${res.status}: ${err}`)
  }
  return res.json()
}

export async function simulateCoverage(params: any) {
  return apiFetch('/simulate/coverage', { method: 'POST', body: JSON.stringify(params) })
}

export async function simulateP2P(params: any) {
  return apiFetch('/simulate/p2p', { method: 'POST', body: JSON.stringify(params) })
}

export async function getTerrainProfile(lat1: number, lon1: number, lat2: number, lon2: number) {
  return apiFetch(`/terrain/profile?lat1=${lat1}&lon1=${lon1}&lat2=${lat2}&lon2=${lon2}`)
}

export async function getSpaceWeather() {
  return apiFetch('/space_weather')
}

export async function purgeCache(baseUrl?: string) {
  if (baseUrl) {
    const res = await fetch(`${baseUrl}/api/v1/cache/purge`, { method: 'DELETE' })
    return res.json()
  }
  return apiFetch('/cache/purge', { method: 'DELETE' })
}
