// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Settings screen — backend URL, GPU, units, cache management.
 */
import React, { useState } from 'react'
import {
  View, Text, StyleSheet, ScrollView,
  TextInput, TouchableOpacity, Switch, Alert
} from 'react-native'
import AsyncStorage from '@react-native-async-storage/async-storage'
import { purgeCache } from '../api/client'

const COLORS = {
  bg: '#0d1117', card: '#161b22', border: '#30363d',
  accent: '#00b4d8', green: '#06d6a0', amber: '#ffb703',
  red: '#ef4444', text: '#e6edf3', textSecondary: '#8b949e',
  tertiary: '#21262d',
}

export default function SettingsScreen() {
  const [serverUrl, setServerUrl] = useState('http://192.168.1.100:8000')
  const [gpuEnabled, setGpuEnabled] = useState(false)
  const [darkMode, setDarkMode] = useState(true)
  const [metricUnits, setMetricUnits] = useState(true)
  const [autoCache, setAutoCache] = useState(true)

  const handlePurgeCache = () => {
    Alert.alert(
      'Purge Cache',
      'Delete all cached terrain and building data?',
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Purge',
          style: 'destructive',
          onPress: async () => {
            try {
              await purgeCache(serverUrl)
              Alert.alert('Success', 'Cache purged')
            } catch {
              Alert.alert('Error', 'Cache purge failed')
            }
          },
        },
      ]
    )
  }

  const saveSettings = async () => {
    await AsyncStorage.multiSet([
      ['serverUrl', serverUrl],
      ['gpuEnabled', String(gpuEnabled)],
      ['metricUnits', String(metricUnits)],
    ])
    Alert.alert('Saved', 'Settings saved')
  }

  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      {/* Server */}
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>Backend Server</Text>
        <Text style={styles.label}>API URL</Text>
        <TextInput
          style={styles.input}
          value={serverUrl}
          onChangeText={setServerUrl}
          keyboardType="url"
          autoCapitalize="none"
          autoCorrect={false}
          selectionColor={COLORS.accent}
        />
        <Text style={styles.hint}>
          Set this to the IP/hostname where the Python backend is running.
          Default: http://localhost:8000
        </Text>
      </View>

      {/* Computation */}
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>Computation</Text>
        <SwitchRow
          label="GPU acceleration (CUDA)"
          hint="Requires NVIDIA GPU with CUDA drivers"
          value={gpuEnabled}
          onValueChange={setGpuEnabled}
        />
        <SwitchRow
          label="Auto-purge stale terrain data"
          hint="Automatically deletes terrain cache older than 30 days"
          value={autoCache}
          onValueChange={setAutoCache}
        />
      </View>

      {/* Display */}
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>Display</Text>
        <SwitchRow
          label="Metric units (km, m)"
          value={metricUnits}
          onValueChange={setMetricUnits}
        />
      </View>

      {/* Data */}
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>Data Sources</Text>
        <InfoRow label="Terrain" value="SRTM 90m (auto-download)" />
        <InfoRow label="Space Weather" value="NOAA SWPC (real-time)" />
        <InfoRow label="Buildings" value="OpenStreetMap Overpass" />
        <InfoRow label="Elevation API" value="OpenTopoData (fallback)" />
      </View>

      {/* Cache */}
      <TouchableOpacity style={styles.dangerBtn} onPress={handlePurgeCache}>
        <Text style={styles.dangerBtnText}>🗑  Purge Terrain Cache</Text>
      </TouchableOpacity>

      {/* Save */}
      <TouchableOpacity style={styles.saveBtn} onPress={saveSettings}>
        <Text style={styles.saveBtnText}>Save Settings</Text>
      </TouchableOpacity>

      {/* About */}
      <View style={styles.about}>
        <Text style={styles.aboutTitle}>RF Propagation Simulator v1.0.0</Text>
        <Text style={styles.aboutText}>
          Propagation models: ITM/Longley-Rice, Hata, COST-231, ITU-R P.452/528/1546{'\n'}
          Terrain: SRTM auto-download (30m/90m){'\n'}
          Space weather: NOAA SWPC real-time{'\n'}
          GPU: CUDA via CuPy (optional)
        </Text>
      </View>
    </ScrollView>
  )
}

function SwitchRow({ label, hint, value, onValueChange }: any) {
  return (
    <View style={styles.switchRow}>
      <View style={{ flex: 1 }}>
        <Text style={styles.switchLabel}>{label}</Text>
        {hint && <Text style={styles.hint}>{hint}</Text>}
      </View>
      <Switch
        value={value}
        onValueChange={onValueChange}
        trackColor={{ false: '#30363d', true: '#00b4d844' }}
        thumbColor={value ? '#00b4d8' : '#8b949e'}
      />
    </View>
  )
}

function InfoRow({ label, value }: any) {
  return (
    <View style={styles.infoRow}>
      <Text style={styles.infoLabel}>{label}</Text>
      <Text style={styles.infoValue}>{value}</Text>
    </View>
  )
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: COLORS.bg },
  content: { padding: 12, gap: 12 },
  section: {
    backgroundColor: COLORS.card,
    borderWidth: 1, borderColor: COLORS.border,
    borderRadius: 12, padding: 12,
  },
  sectionTitle: {
    color: COLORS.textSecondary, fontSize: 11, fontWeight: '600',
    textTransform: 'uppercase', letterSpacing: 0.8, marginBottom: 10,
  },
  label: { color: COLORS.textSecondary, fontSize: 12, marginBottom: 4 },
  input: {
    backgroundColor: COLORS.tertiary,
    borderWidth: 1, borderColor: COLORS.border,
    borderRadius: 8, color: COLORS.text,
    fontSize: 13, paddingHorizontal: 10, paddingVertical: 8,
  },
  hint: { color: COLORS.textSecondary, fontSize: 10, marginTop: 4 },
  switchRow: {
    flexDirection: 'row', alignItems: 'center',
    paddingVertical: 8, borderTopWidth: 0,
  },
  switchLabel: { color: COLORS.text, fontSize: 13 },
  infoRow: {
    flexDirection: 'row', justifyContent: 'space-between',
    paddingVertical: 6,
    borderTopWidth: 1, borderColor: COLORS.border,
  },
  infoLabel: { color: COLORS.textSecondary, fontSize: 12 },
  infoValue: { color: COLORS.text, fontSize: 12 },
  dangerBtn: {
    backgroundColor: '#ef444420',
    borderWidth: 1, borderColor: '#ef444440',
    borderRadius: 12, padding: 14,
    alignItems: 'center',
  },
  dangerBtnText: { color: COLORS.red, fontWeight: '600', fontSize: 14 },
  saveBtn: {
    backgroundColor: COLORS.accent,
    borderRadius: 12, padding: 14, alignItems: 'center',
  },
  saveBtnText: { color: '#000', fontWeight: '700', fontSize: 15 },
  about: {
    backgroundColor: COLORS.card,
    borderWidth: 1, borderColor: COLORS.border,
    borderRadius: 12, padding: 16, marginBottom: 24,
  },
  aboutTitle: { color: COLORS.accent, fontWeight: '700', marginBottom: 8 },
  aboutText: { color: COLORS.textSecondary, fontSize: 12, lineHeight: 20 },
})
