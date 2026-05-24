// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Simulator settings screen for mobile.
 * Configures TX/RX parameters, runs P2P link budget.
 */
import React, { useState } from 'react'
import {
  View, Text, StyleSheet, ScrollView,
  TextInput, TouchableOpacity, Switch, Alert
} from 'react-native'
import { Ionicons } from '@expo/vector-icons'
import { useSimulatorStore } from '../store/simulatorStore'
import { simulateP2P } from '../api/client'

const COLORS = {
  bg: '#0d1117', card: '#161b22', border: '#30363d',
  accent: '#00b4d8', green: '#06d6a0', amber: '#ffb703',
  red: '#ef4444', text: '#e6edf3', textSecondary: '#8b949e',
  tertiary: '#21262d',
}

const FREQ_PRESETS = [
  { label: '433 MHz (ISM)', hz: 433e6 },
  { label: '915 MHz (ISM)', hz: 915e6 },
  { label: '2.4 GHz (WiFi)', hz: 2437e6 },
  { label: '5.8 GHz (WiFi)', hz: 5800e6 },
  { label: '144 MHz (VHF)', hz: 144e6 },
  { label: '462 MHz (GMRS)', hz: 462e6 },
  { label: '700 MHz (LTE)', hz: 700e6 },
]

function Field({ label, value, onChangeText, keyboardType = 'numeric', unit = '', hint = '' }: any) {
  return (
    <View style={styles.field}>
      <Text style={styles.fieldLabel}>{label}</Text>
      <View style={styles.inputRow}>
        <TextInput
          style={styles.input}
          value={String(value)}
          onChangeText={onChangeText}
          keyboardType={keyboardType}
          placeholderTextColor={COLORS.textSecondary}
          selectionColor={COLORS.accent}
        />
        {unit ? <Text style={styles.unit}>{unit}</Text> : null}
      </View>
      {hint ? <Text style={styles.hint}>{hint}</Text> : null}
    </View>
  )
}

export default function SimulatorScreen() {
  const store = useSimulatorStore()
  const [isRunning, setIsRunning] = useState(false)
  const [result, setResult] = useState<any>(null)

  const runP2P = async () => {
    setIsRunning(true)
    setResult(null)
    try {
      const res = await simulateP2P({
        transmitter: {
          lat: store.txLat, lon: store.txLon,
          height_m: store.txHeight,
          altitude_m: store.txAltitude,
          power_dbm: store.powerDbm,
          frequency_hz: store.frequencyHz,
          antenna: store.txAntenna,
        },
        receiver_lat: store.rxLat,
        receiver_lon: store.rxLon,
        receiver_height_m: store.rxHeight,
        receiver_altitude_m: store.rxAltitude,
        propagation_model: store.model,
        fetch_space_weather: true,
      })
      setResult(res)
    } catch (err: any) {
      Alert.alert('Error', err.message)
    } finally {
      setIsRunning(false)
    }
  }

  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      {/* Transmitter */}
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>
          <Ionicons name="radio-outline" size={14} color={COLORS.accent} /> Transmitter
        </Text>
        <View style={styles.row}>
          <Field label="Lat" value={store.txLat.toFixed(5)}
                 onChangeText={(v: string) => store.setTxLat(parseFloat(v))} />
          <Field label="Lon" value={store.txLon.toFixed(5)}
                 onChangeText={(v: string) => store.setTxLon(parseFloat(v))} />
        </View>
        <View style={styles.row}>
          <Field label="Height AGL" value={store.txHeight} unit="m"
                 onChangeText={(v: string) => store.setTxHeight(parseFloat(v))} />
          <Field label="Altitude ASL" value={store.txAltitude} unit="m"
                 hint="0–9144m (30k ft)"
                 onChangeText={(v: string) => store.setTxAltitude(parseFloat(v))} />
        </View>
        <View style={styles.row}>
          <Field label="Power" value={store.powerDbm} unit="dBm"
                 onChangeText={(v: string) => store.setPowerDbm(parseFloat(v))} />
          <Field label="Frequency" value={(store.frequencyHz / 1e6).toFixed(3)} unit="MHz"
                 onChangeText={(v: string) => store.setFrequencyHz(parseFloat(v) * 1e6)} />
        </View>

        {/* Freq presets */}
        <ScrollView horizontal showsHorizontalScrollIndicator={false} style={{ marginTop: 6 }}>
          {FREQ_PRESETS.map(p => (
            <TouchableOpacity
              key={p.hz}
              style={[styles.chip, store.frequencyHz === p.hz && styles.chipActive]}
              onPress={() => store.setFrequencyHz(p.hz)}
            >
              <Text style={[styles.chipText, store.frequencyHz === p.hz && styles.chipTextActive]}>
                {p.label}
              </Text>
            </TouchableOpacity>
          ))}
        </ScrollView>
      </View>

      {/* Receiver */}
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>Receiver</Text>
        <View style={styles.row}>
          <Field label="Lat" value={store.rxLat.toFixed(5)}
                 onChangeText={(v: string) => store.setRxLat(parseFloat(v))} />
          <Field label="Lon" value={store.rxLon.toFixed(5)}
                 onChangeText={(v: string) => store.setRxLon(parseFloat(v))} />
        </View>
        <View style={styles.row}>
          <Field label="Height AGL" value={store.rxHeight} unit="m"
                 onChangeText={(v: string) => store.setRxHeight(parseFloat(v))} />
          <Field label="Altitude ASL" value={store.rxAltitude} unit="m"
                 onChangeText={(v: string) => store.setRxAltitude(parseFloat(v))} />
        </View>
        <View style={styles.row}>
          <Field label="Sensitivity" value={store.rxSensitivity} unit="dBm"
                 onChangeText={(v: string) => store.setRxSensitivity(parseFloat(v))} />
          <Field label="Min Signal" value={store.minSignalDbm} unit="dBm"
                 onChangeText={(v: string) => store.setMinSignalDbm(parseFloat(v))} />
        </View>
      </View>

      {/* Model */}
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>Propagation Model</Text>
        {['itm', 'fspl', 'hata_urban', 'hata_rural', 'itu_p528'].map(m => (
          <TouchableOpacity
            key={m}
            style={[styles.modelBtn, store.model === m && styles.modelBtnActive]}
            onPress={() => store.setModel(m)}
          >
            <Text style={[styles.modelText, store.model === m && styles.modelTextActive]}>
              {m.toUpperCase().replace(/_/g, ' ')}
            </Text>
          </TouchableOpacity>
        ))}
      </View>

      {/* Options */}
      <View style={styles.section}>
        <View style={styles.switchRow}>
          <Text style={styles.switchLabel}>GPU acceleration (CUDA)</Text>
          <Switch
            value={store.useGpu}
            onValueChange={store.setUseGpu}
            trackColor={{ false: COLORS.border, true: COLORS.accent + '66' }}
            thumbColor={store.useGpu ? COLORS.accent : COLORS.textSecondary}
          />
        </View>
      </View>

      {/* Run button */}
      <TouchableOpacity
        style={[styles.runBtn, isRunning && styles.runBtnLoading]}
        onPress={runP2P}
        disabled={isRunning}
      >
        <Ionicons name="flash" size={18} color="#000" />
        <Text style={styles.runBtnText}>{isRunning ? 'Computing…' : 'Run P2P Link Budget'}</Text>
      </TouchableOpacity>

      {/* Results */}
      {result && (
        <View style={styles.resultCard}>
          <Text style={styles.sectionTitle}>Link Budget Results</Text>
          <View style={styles.resultGrid}>
            {[
              ['Path Loss', `${result.result?.path_loss_db?.toFixed(1)} dB`, COLORS.amber],
              ['Rx Signal', `${result.result?.received_signal_dbm?.toFixed(1)} dBm`, COLORS.green],
              ['Mode', result.result?.propagation_mode, COLORS.accent],
              ['Margin', `${result.result?.link_budget?.link_margin_db?.toFixed(1)} dB`,
               result.result?.link_budget?.is_viable ? COLORS.green : COLORS.red],
            ].map(([label, value, color]) => (
              <View key={label as string} style={styles.resultCell}>
                <Text style={styles.resultLabel}>{label}</Text>
                <Text style={[styles.resultValue, { color: color as string }]}>{value}</Text>
              </View>
            ))}
          </View>

          {result.result?.warnings?.map((w: string, i: number) => (
            <View key={i} style={styles.warning}>
              <Text style={styles.warningText}>{w}</Text>
            </View>
          ))}
        </View>
      )}
    </ScrollView>
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
    color: COLORS.textSecondary, fontSize: 11,
    fontWeight: '600', textTransform: 'uppercase',
    letterSpacing: 0.8, marginBottom: 10,
  },
  row: { flexDirection: 'row', gap: 8, marginBottom: 8 },
  field: { flex: 1 },
  fieldLabel: { color: COLORS.textSecondary, fontSize: 11, marginBottom: 4 },
  inputRow: { flexDirection: 'row', alignItems: 'center' },
  input: {
    flex: 1, backgroundColor: COLORS.tertiary,
    borderWidth: 1, borderColor: COLORS.border,
    borderRadius: 6, color: COLORS.text,
    fontSize: 13, paddingHorizontal: 8, paddingVertical: 5,
  },
  unit: { color: COLORS.textSecondary, fontSize: 11, marginLeft: 4 },
  hint: { color: COLORS.textSecondary, fontSize: 9, marginTop: 2 },
  chip: {
    paddingHorizontal: 10, paddingVertical: 4,
    borderRadius: 20, borderWidth: 1, borderColor: COLORS.border,
    marginRight: 6, backgroundColor: COLORS.tertiary,
  },
  chipActive: { borderColor: COLORS.accent, backgroundColor: COLORS.accent + '22' },
  chipText: { color: COLORS.textSecondary, fontSize: 11 },
  chipTextActive: { color: COLORS.accent },
  modelBtn: {
    paddingHorizontal: 12, paddingVertical: 7,
    borderRadius: 6, borderWidth: 1, borderColor: COLORS.border,
    marginBottom: 6, backgroundColor: COLORS.tertiary,
  },
  modelBtnActive: { borderColor: COLORS.accent, backgroundColor: COLORS.accent + '22' },
  modelText: { color: COLORS.textSecondary, fontSize: 12 },
  modelTextActive: { color: COLORS.accent },
  switchRow: {
    flexDirection: 'row', alignItems: 'center',
    justifyContent: 'space-between', paddingVertical: 4,
  },
  switchLabel: { color: COLORS.text, fontSize: 13 },
  runBtn: {
    backgroundColor: COLORS.accent,
    borderRadius: 12, padding: 14,
    flexDirection: 'row', alignItems: 'center',
    justifyContent: 'center', gap: 8,
  },
  runBtnLoading: { backgroundColor: '#374151' },
  runBtnText: { color: '#000', fontWeight: '700', fontSize: 15 },
  resultCard: {
    backgroundColor: COLORS.card,
    borderWidth: 1, borderColor: COLORS.border,
    borderRadius: 12, padding: 12,
  },
  resultGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginBottom: 8 },
  resultCell: {
    width: '47%', backgroundColor: COLORS.tertiary,
    borderRadius: 8, padding: 10,
  },
  resultLabel: { color: COLORS.textSecondary, fontSize: 10, textTransform: 'uppercase' },
  resultValue: { fontSize: 18, fontWeight: '700', marginTop: 4 },
  warning: {
    backgroundColor: COLORS.amber + '15',
    borderWidth: 1, borderColor: COLORS.amber + '44',
    borderRadius: 6, padding: 8, marginTop: 4,
  },
  warningText: { color: COLORS.amber, fontSize: 11 },
})
