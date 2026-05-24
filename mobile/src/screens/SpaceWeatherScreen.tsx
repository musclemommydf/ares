// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Space Weather Screen — real-time NOAA SWPC data.
 */
import React, { useState, useEffect } from 'react'
import { View, Text, StyleSheet, ScrollView, TouchableOpacity, RefreshControl } from 'react-native'
import { getSpaceWeather } from '../api/client'

const COLORS = {
  bg: '#0d1117', card: '#161b22', border: '#30363d',
  accent: '#00b4d8', green: '#06d6a0', amber: '#ffb703',
  red: '#ef4444', text: '#e6edf3', textSecondary: '#8b949e',
}

function StatusCard({ title, value, status, description }: any) {
  const color = status === 'ok' ? COLORS.green
    : status === 'warn' ? COLORS.amber
    : status === 'bad' ? COLORS.red
    : COLORS.textSecondary

  return (
    <View style={[styles.statusCard, { borderColor: color + '44' }]}>
      <View style={[styles.statusDot, { backgroundColor: color }]} />
      <View style={{ flex: 1 }}>
        <Text style={styles.statusTitle}>{title}</Text>
        <Text style={[styles.statusValue, { color }]}>{value}</Text>
        {description && <Text style={styles.statusDesc}>{description}</Text>}
      </View>
    </View>
  )
}

export default function SpaceWeatherScreen() {
  const [data, setData] = useState<any>(null)
  const [raw, setRaw] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<string>('')

  const fetch = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await getSpaceWeather()
      setData(res.data)
      setRaw(res.raw)
      setLastUpdated(new Date().toLocaleTimeString())
    } catch (e: any) {
      setError('Failed to fetch space weather data from NOAA SWPC')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetch() }, [])

  const kp = raw?.kp_index || 0
  const kpStatus = kp >= 7 ? 'bad' : kp >= 5 ? 'warn' : 'ok'

  return (
    <ScrollView
      style={styles.container}
      contentContainerStyle={styles.content}
      refreshControl={<RefreshControl refreshing={loading} onRefresh={fetch}
                         tintColor={COLORS.accent} />}
    >
      <View style={styles.header}>
        <Text style={styles.headerTitle}>NOAA SWPC Space Weather</Text>
        {lastUpdated && <Text style={styles.headerSub}>Updated: {lastUpdated}</Text>}
      </View>

      {error && (
        <View style={styles.error}>
          <Text style={styles.errorText}>{error}</Text>
        </View>
      )}

      {data && raw && (
        <>
          {/* HF propagation condition */}
          <View style={[styles.hfCard, {
            borderColor: raw.hf_blackout ? COLORS.red + '66'
              : raw.ionospheric_storm ? COLORS.amber + '66'
              : COLORS.green + '66'
          }]}>
            <Text style={styles.hfTitle}>HF Propagation</Text>
            <Text style={[styles.hfStatus, {
              color: raw.hf_blackout ? COLORS.red
                : raw.ionospheric_storm ? COLORS.amber : COLORS.green
            }]}>
              {data.hf_propagation}
            </Text>
          </View>

          {/* Index cards */}
          <StatusCard
            title="Solar Flux (F10.7)"
            value={`${raw.f10_7?.toFixed(0)} sfu`}
            status={raw.f10_7 > 180 ? 'ok' : raw.f10_7 > 80 ? 'warn' : 'bad'}
            description={raw.f10_7 > 180 ? 'High solar activity — excellent HF'
              : raw.f10_7 > 100 ? 'Moderate activity'
              : 'Low activity — solar minimum'}
          />
          <StatusCard
            title="Kp Index (Geomagnetic)"
            value={`Kp ${raw.kp_index?.toFixed(1)} — Storm: ${raw.storm_class}`}
            status={kpStatus}
            description={kp >= 5 ? 'Geomagnetic storm — HF/VHF disruption possible'
              : kp >= 3 ? 'Active conditions'
              : 'Quiet conditions'}
          />
          <StatusCard
            title="X-Ray / Radio Blackout"
            value={raw.radio_blackout_class === 'None'
              ? 'No blackout'
              : `Class ${raw.radio_blackout_class} — Solar flare!`}
            status={raw.radio_blackout_class !== 'None' ? 'bad' : 'ok'}
            description={raw.hf_blackout ? 'HF communication severely impaired' : undefined}
          />
          <StatusCard
            title="Aurora / PCA"
            value={`Aurora: ${raw.aurora_activity}`}
            status={raw.polar_cap_absorption ? 'bad'
              : raw.aurora_activity === 'High' ? 'warn' : 'ok'}
            description={raw.polar_cap_absorption ? 'Polar cap absorption — high-latitude paths impacted' : undefined}
          />
          {data.vhf_sporadic_e_likely && (
            <StatusCard
              title="Sporadic-E (VHF)"
              value="Conditions possible"
              status="ok"
              description="Enhanced VHF propagation likely (30–200 MHz)"
            />
          )}

          {/* Propagation guide */}
          <View style={styles.guide}>
            <Text style={styles.guideTitle}>Quick Reference</Text>
            <Text style={styles.guideText}>
              {'• F10.7 > 150 → High MUF, excellent HF long-path\n'}
              {'• Kp ≥ 5 (G1) → HF disruption, VHF scintillation\n'}
              {'• Kp ≥ 7 (G3) → Polar cap absorption\n'}
              {'• X-class flare → HF blackout on sunlit hemisphere\n'}
              {'• May–Aug, 1400–2000 local → Sporadic-E possible'}
            </Text>
          </View>
        </>
      )}
    </ScrollView>
  )
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: COLORS.bg },
  content: { padding: 12, gap: 10, paddingBottom: 24 },
  header: { marginBottom: 4 },
  headerTitle: { color: COLORS.text, fontSize: 16, fontWeight: '700' },
  headerSub: { color: COLORS.textSecondary, fontSize: 11, marginTop: 2 },
  error: {
    backgroundColor: COLORS.red + '20',
    borderWidth: 1, borderColor: COLORS.red + '40',
    borderRadius: 8, padding: 10,
  },
  errorText: { color: COLORS.red, fontSize: 12 },
  hfCard: {
    backgroundColor: COLORS.card,
    borderWidth: 1, borderRadius: 12, padding: 14,
  },
  hfTitle: { color: COLORS.textSecondary, fontSize: 11, textTransform: 'uppercase', marginBottom: 6 },
  hfStatus: { fontSize: 14, fontWeight: '600', lineHeight: 20 },
  statusCard: {
    flexDirection: 'row', alignItems: 'flex-start', gap: 12,
    backgroundColor: COLORS.card,
    borderWidth: 1, borderRadius: 10, padding: 12,
  },
  statusDot: { width: 8, height: 8, borderRadius: 4, marginTop: 4 },
  statusTitle: { color: COLORS.textSecondary, fontSize: 11, textTransform: 'uppercase', marginBottom: 2 },
  statusValue: { fontSize: 14, fontWeight: '600' },
  statusDesc: { color: COLORS.textSecondary, fontSize: 11, marginTop: 3, lineHeight: 16 },
  guide: {
    backgroundColor: COLORS.card,
    borderWidth: 1, borderColor: COLORS.border,
    borderRadius: 10, padding: 12, marginTop: 4,
  },
  guideTitle: { color: COLORS.accent, fontWeight: '600', marginBottom: 8 },
  guideText: { color: COLORS.textSecondary, fontSize: 12, lineHeight: 20 },
})
