// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Map Screen — Coverage map with transmitter placement and coverage overlay.
 */
import React, { useState, useRef, useCallback } from 'react'
import {
  View, StyleSheet, Text, TouchableOpacity,
  ActivityIndicator, Alert, ScrollView,
} from 'react-native'
import MapView, { Marker, Circle, Polyline, PROVIDER_GOOGLE } from 'react-native-maps'
import * as Location from 'expo-location'
import { Ionicons } from '@expo/vector-icons'
import { useSimulatorStore } from '../store/simulatorStore'
import { simulateCoverage } from '../api/client'
import { dbmToQuality } from '../api/helpers'

const COLORS = {
  bg: '#0d1117', card: '#161b22', border: '#30363d',
  accent: '#00b4d8', green: '#06d6a0', amber: '#ffb703',
  red: '#ef4444', text: '#e6edf3', textSecondary: '#8b949e',
}

function SignalBadge({ dbm }: { dbm: number }) {
  const q = dbmToQuality(dbm)
  return (
    <View style={[styles.badge, { backgroundColor: q.color + '22', borderColor: q.color + '44' }]}>
      <Text style={[styles.badgeText, { color: q.color }]}>{q.label}</Text>
    </View>
  )
}

export default function MapScreen() {
  const [txPos, setTxPos] = useState({ lat: 37.7749, lon: -122.4194 })
  const [coveragePoints, setCoveragePoints] = useState<any[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [metadata, setMetadata] = useState<any>(null)
  const mapRef = useRef<MapView>(null)
  const store = useSimulatorStore()

  const useGPSLocation = async () => {
    const { status } = await Location.requestForegroundPermissionsAsync()
    if (status !== 'granted') {
      Alert.alert('Permission denied', 'Location permission is required to use GPS.')
      return
    }
    const loc = await Location.getCurrentPositionAsync({})
    const pos = { lat: loc.coords.latitude, lon: loc.coords.longitude }
    setTxPos(pos)
    mapRef.current?.animateToRegion({
      latitude: pos.lat, longitude: pos.lon,
      latitudeDelta: 0.5, longitudeDelta: 0.5,
    })
  }

  const runSimulation = async () => {
    setIsLoading(true)
    setCoveragePoints([])
    try {
      const result = await simulateCoverage({
        transmitter: {
          lat: txPos.lat, lon: txPos.lon,
          height_m: store.txHeight,
          altitude_m: store.txAltitude,
          power_dbm: store.powerDbm,
          frequency_hz: store.frequencyHz,
          antenna: store.txAntenna,
        },
        receiver: {
          height_m: store.rxHeight,
          sensitivity_dbm: store.rxSensitivity,
          antenna: store.rxAntenna,
        },
        propagation_model: store.model,
        radius_km: store.radiusKm,
        num_radials: 180,         // fewer radials for mobile performance
        points_per_radial: 100,
        min_signal_dbm: store.minSignalDbm,
        fetch_space_weather: true,
      })
      setMetadata(result.metadata)
      // Extract coverage circles from GeoJSON
      const pts = (result.geojson?.features || [])
        .filter((f: any) => f.properties.covered)
        .map((f: any) => ({
          lat: f.geometry.coordinates[1],
          lon: f.geometry.coordinates[0],
          dbm: f.properties.signal_dbm,
        }))
      setCoveragePoints(pts)
    } catch (err: any) {
      Alert.alert('Simulation error', err.message || 'Unknown error')
    } finally {
      setIsLoading(false)
    }
  }

  const handleMapPress = (e: any) => {
    const { latitude, longitude } = e.nativeEvent.coordinate
    setTxPos({ lat: latitude, lon: longitude })
    store.setTxLat(latitude)
    store.setTxLon(longitude)
  }

  const getCircleColor = (dbm: number) => {
    if (dbm >= -60) return '#06d6a0'
    if (dbm >= -75) return '#84cc16'
    if (dbm >= -90) return '#f59e0b'
    return '#ef4444'
  }

  return (
    <View style={styles.container}>
      <MapView
        ref={mapRef}
        style={styles.map}
        provider={PROVIDER_GOOGLE}
        customMapStyle={darkMapStyle}
        initialRegion={{
          latitude: txPos.lat, longitude: txPos.lon,
          latitudeDelta: 1.0, longitudeDelta: 1.0,
        }}
        onPress={handleMapPress}
        showsUserLocation
        showsCompass
      >
        {/* TX Marker */}
        <Marker
          coordinate={{ latitude: txPos.lat, longitude: txPos.lon }}
          draggable
          onDragEnd={e => {
            const { latitude, longitude } = e.nativeEvent.coordinate
            setTxPos({ lat: latitude, lon: longitude })
          }}
          title="Transmitter"
          description={`${store.powerDbm} dBm | ${(store.frequencyHz / 1e6).toFixed(1)} MHz`}
          pinColor="#00b4d8"
        />

        {/* Coverage circles */}
        {coveragePoints.slice(0, 2000).map((pt, i) => (
          <Circle
            key={i}
            center={{ latitude: pt.lat, longitude: pt.lon }}
            radius={500}
            strokeWidth={0}
            fillColor={getCircleColor(pt.dbm) + '66'}
          />
        ))}
      </MapView>

      {/* Controls overlay */}
      <View style={styles.topBar}>
        <TouchableOpacity style={styles.iconBtn} onPress={useGPSLocation}>
          <Ionicons name="locate" size={20} color={COLORS.accent} />
        </TouchableOpacity>
        <View style={styles.coordBadge}>
          <Text style={styles.coordText}>
            {txPos.lat.toFixed(4)}, {txPos.lon.toFixed(4)}
          </Text>
        </View>
      </View>

      {/* Result card */}
      {metadata && (
        <View style={styles.resultCard}>
          <View style={styles.resultRow}>
            <View style={styles.resultItem}>
              <Text style={styles.resultLabel}>Max Range</Text>
              <Text style={styles.resultValue}>{metadata.max_range_km?.toFixed(1)} km</Text>
            </View>
            <View style={styles.resultItem}>
              <Text style={styles.resultLabel}>Avg Signal</Text>
              <Text style={styles.resultValue}>{metadata.avg_signal_dbm?.toFixed(0)} dBm</Text>
            </View>
            <View style={styles.resultItem}>
              <Text style={styles.resultLabel}>Area</Text>
              <Text style={styles.resultValue}>{metadata.covered_area_km2?.toFixed(0)} km²</Text>
            </View>
          </View>
        </View>
      )}

      {/* Simulate FAB */}
      <TouchableOpacity
        style={[styles.fab, isLoading && styles.fabLoading]}
        onPress={runSimulation}
        disabled={isLoading}
      >
        {isLoading
          ? <ActivityIndicator color="#000" />
          : <Ionicons name="flash" size={24} color="#000" />
        }
        <Text style={styles.fabText}>{isLoading ? 'Simulating…' : 'Simulate'}</Text>
      </TouchableOpacity>
    </View>
  )
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: COLORS.bg },
  map: { flex: 1 },
  topBar: {
    position: 'absolute', top: 8, left: 8, right: 8,
    flexDirection: 'row', alignItems: 'center', gap: 8,
  },
  iconBtn: {
    width: 40, height: 40, borderRadius: 20,
    backgroundColor: COLORS.card,
    borderWidth: 1, borderColor: COLORS.border,
    alignItems: 'center', justifyContent: 'center',
  },
  coordBadge: {
    flex: 1, backgroundColor: COLORS.card,
    borderWidth: 1, borderColor: COLORS.border,
    borderRadius: 20, paddingHorizontal: 12, paddingVertical: 8,
  },
  coordText: { color: COLORS.textSecondary, fontSize: 12, fontFamily: 'monospace' },
  resultCard: {
    position: 'absolute', bottom: 80, left: 12, right: 12,
    backgroundColor: COLORS.card,
    borderWidth: 1, borderColor: COLORS.border,
    borderRadius: 12, padding: 12,
  },
  resultRow: { flexDirection: 'row' },
  resultItem: { flex: 1, alignItems: 'center' },
  resultLabel: { color: COLORS.textSecondary, fontSize: 10, textTransform: 'uppercase' },
  resultValue: { color: COLORS.accent, fontSize: 16, fontWeight: '700', marginTop: 2 },
  fab: {
    position: 'absolute', bottom: 16, left: '50%', transform: [{ translateX: -75 }],
    width: 150, backgroundColor: COLORS.accent,
    borderRadius: 28, height: 52,
    flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 6,
    shadowColor: COLORS.accent, shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.4, shadowRadius: 8, elevation: 8,
  },
  fabLoading: { backgroundColor: '#374151' },
  fabText: { color: '#000', fontWeight: '700', fontSize: 15 },
  badge: {
    paddingHorizontal: 8, paddingVertical: 3,
    borderRadius: 12, borderWidth: 1,
  },
  badgeText: { fontSize: 11, fontWeight: '600' },
})

// Dark map style for Google Maps
const darkMapStyle = [
  { elementType: 'geometry', stylers: [{ color: '#1c1f26' }] },
  { elementType: 'labels.text.fill', stylers: [{ color: '#8b949e' }] },
  { elementType: 'labels.text.stroke', stylers: [{ color: '#0d1117' }] },
  { featureType: 'road', elementType: 'geometry', stylers: [{ color: '#21262d' }] },
  { featureType: 'water', elementType: 'geometry', stylers: [{ color: '#0a0e1a' }] },
  { featureType: 'landscape', elementType: 'geometry', stylers: [{ color: '#161b22' }] },
]
