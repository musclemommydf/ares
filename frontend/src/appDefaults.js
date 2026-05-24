// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Module-level defaults for App.jsx's core state (transmitter, receiver,
 * propagation, atmosphere) plus a couple of small lookup tables.
 */

export const DEFAULT_TX = {
  lat: 52.3619,
  lon: 0.4860,
  height_m: 1.8288,  // 6ft AGL default
  altitude_m: 0,
  power_dbm: 27,
  frequency_hz: 433e6,
  antenna: {
    type: 'dipole_half_wave',
    tilt_deg: 0,
    azimuth_deg: 0,
    height_m: 1.8288,  // 6ft AGL default
    diameter_m: 1.2,
    efficiency: 0.55,
    elements: 9,
    array_elements: 64,
    polarization: 'vertical',
    gain_dbi: null,
    polar_pattern: 'omni',
    polar_peak_gain_dbi: null,
    sweep_deg: 0,  // 0 = no sweep (focused pattern). 360 = omni-equivalent. Otherwise the radar scans this arc centered on azimuth_deg.
    beam_height_min_m: 2,
    beam_height_max_m: 50,
  },
}

export const DEFAULT_RX = {
  height_m: 1.5,
  altitude_m: 0,
  sensitivity_dbm: -100,
  noise_figure_db: 3,
  required_snr_db: 10,
  antenna: {
    type: 'dipole_half_wave', gain_dbi: null, tilt_deg: 0, azimuth_deg: 0,
    height_m: 1.5, diameter_m: 1.2, efficiency: 0.55,
    elements: 3, array_elements: 8, polarization: 'vertical',
  },
}

export const DEFAULT_PROPAGATION = {
  model: 'auto',
  wave_type: 'auto',
  radius_km: 50,
  num_radials: 360,
  points_per_radial: 300,
  min_signal_dbm: -100,
  use_gpu: true,
  terrain_resolution: 'srtm1',
  include_buildings: true,
  buildings_radius_m: 500,
  show_buildings_layer: false,
  fetch_space_weather: true,
  context: 2,
  diffraction_model: 'deygout',
  rcs_m2: 1.0,
  clutter_height_m: 0,
}

export const DEFAULT_ATMOSPHERE = {
  temperature_c: 15,
  pressure_hpa: 1013.25,
  humidity_percent: 60,
  rain_rate_mm_per_hr: 0,
  visibility_km: 10,
  refractivity_gradient: -40,
}

export const RADAR_TARGETS = [
  { label: 'Person (0.5 m²)',         rcs: 0.5 },
  { label: 'Car (10 m²)',             rcs: 10 },
  { label: 'Fighter aircraft (5 m²)', rcs: 5 },
  { label: 'Large aircraft (30 m²)',  rcs: 30 },
  { label: 'Small UAV (0.005 m²)',    rcs: 0.005 },
  { label: 'Stealth (0.001 m²)',      rcs: 0.001 },
  { label: 'Frigate (5000 m²)',       rcs: 5000 },
]

export const TX_COLORS = ['#00b4d8', '#f59e0b', '#06d6a0', '#ef4444', '#a78bfa', '#f97316']
