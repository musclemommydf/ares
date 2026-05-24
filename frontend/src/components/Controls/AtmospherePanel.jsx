// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Atmospheric conditions panel.
 * Temperature, humidity, rain, fog, ducting, refractivity gradient.
 * Supports auto-fill from real-time or historical weather (Open-Meteo).
 */
import { useState } from 'react'
import { Cloud, ChevronDown, ChevronUp, RefreshCw } from 'lucide-react'
import { getWeather } from '../../api/client'
import { toast } from 'react-toastify'

const RAIN_PRESETS = [
  { label: 'Clear (0 mm/hr)', value: 0 },
  { label: 'Drizzle (0.5 mm/hr)', value: 0.5 },
  { label: 'Light rain (2 mm/hr)', value: 2 },
  { label: 'Moderate rain (12 mm/hr)', value: 12 },
  { label: 'Heavy rain (25 mm/hr)', value: 25 },
  { label: 'Very heavy (50 mm/hr)', value: 50 },
  { label: 'Tropical downpour (100 mm/hr)', value: 100 },
]

const CLIMATE_PRESETS = [
  { label: 'Standard atmosphere (N=301)', gradient: -40, refractivity: 301 },
  { label: 'Tropical (N=370)', gradient: -50, refractivity: 370 },
  { label: 'Desert (N=260)', gradient: -30, refractivity: 260 },
  { label: 'Maritime/coastal (N=330)', gradient: -45, refractivity: 330 },
  { label: 'Super-refraction (ducting)', gradient: -80, refractivity: 320 },
  { label: 'Trapping duct (strong)', gradient: -200, refractivity: 300 },
  { label: 'Sub-refraction (mountains)', gradient: -20, refractivity: 280 },
]

export default function AtmospherePanel({ atmosphere, setAtmosphere, txLat, txLon }) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [weatherTime, setWeatherTime] = useState('')   // ISO local datetime for picker

  const update = (field, value) =>
    setAtmosphere(prev => ({ ...prev, [field]: value }))

  const applyClimatePreset = (preset) => {
    setAtmosphere(prev => ({
      ...prev,
      refractivity_gradient: preset.gradient,
    }))
  }

  const handleAutoWeather = async () => {
    setLoading(true)
    try {
      // Convert local picker value to UTC ISO string
      let datetimeUtc = null
      if (weatherTime) {
        const localDate = new Date(weatherTime)
        datetimeUtc = localDate.toISOString()
      }
      const result = await getWeather(txLat, txLon, datetimeUtc)
      const atm = result.atmosphere
      setAtmosphere(prev => ({
        ...prev,
        temperature_c:        atm.temperature_c,
        pressure_hpa:         atm.pressure_hpa,
        humidity_percent:     atm.humidity_percent,
        rain_rate_mm_per_hr:  atm.rain_rate_mm_per_hr,
        visibility_km:        atm.visibility_km,
        refractivity_gradient: atm.refractivity_gradient,
      }))
      const timeLabel = datetimeUtc
        ? new Date(datetimeUtc).toLocaleString()
        : 'current time'
      toast.success(`Atmosphere loaded from ${result.source} (${timeLabel})`)
    } catch (err) {
      toast.error(`Weather fetch failed: ${err.response?.data?.detail || err.message}`)
    } finally {
      setLoading(false)
    }
  }

  const isDucting = atmosphere.refractivity_gradient < -157

  return (
    <div>
      <button
        className={`accordion-trigger ${open ? 'open' : ''}`}
        onClick={() => setOpen(o => !o)}
      >
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <Cloud size={14} color="var(--accent-blue)" />
          Atmosphere &amp; Weather
        </span>
        {open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </button>

      {open && (
        <div className="panel-section">
          {/* Auto-weather from real-time or historical data */}
          <div className="panel-title">Auto-fill from Real Weather</div>
          <div className="field-row single">
            <div className="field">
              <label>Date &amp; time (leave blank for current)</label>
              <input
                type="datetime-local"
                value={weatherTime}
                onChange={e => setWeatherTime(e.target.value)}
                style={{ flex: 1 }}
              />
            </div>
          </div>
          <div style={{ marginBottom: 10 }}>
            <button
              className="btn btn-secondary"
              onClick={handleAutoWeather}
              disabled={loading}
              style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6 }}
            >
              <RefreshCw size={13} className={loading ? 'spin' : ''} />
              {loading ? 'Fetching weather…' : 'Auto-fill from weather at TX location'}
            </button>
          </div>

          {/* Climate preset */}
          <div className="panel-title">Manual / Climate Preset</div>
          <div className="field-row single">
            <div className="field">
              <label>Climate preset</label>
              <select onChange={e => applyClimatePreset(CLIMATE_PRESETS[parseInt(e.target.value)])}
                      defaultValue="">
                <option value="" disabled>— Select climate —</option>
                {CLIMATE_PRESETS.map((p, i) => (
                  <option key={i} value={i}>{p.label}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="field-row">
            <div className="field">
              <label>Temperature (°C)</label>
              <input
                type="number" min="-60" max="60" step="0.5"
                value={atmosphere.temperature_c}
                onChange={e => update('temperature_c', parseFloat(e.target.value))}
              />
            </div>
            <div className="field">
              <label>Humidity (%)</label>
              <input
                type="number" min="0" max="100" step="1"
                value={atmosphere.humidity_percent}
                onChange={e => update('humidity_percent', parseFloat(e.target.value))}
              />
            </div>
          </div>

          <div className="field-row">
            <div className="field">
              <label>Pressure (hPa)</label>
              <input
                type="number" min="300" max="1100" step="0.5"
                value={atmosphere.pressure_hpa}
                onChange={e => update('pressure_hpa', parseFloat(e.target.value))}
              />
            </div>
            <div className="field">
              <label>Visibility (km)</label>
              <input
                type="number" min="0.01" max="100" step="0.1"
                value={atmosphere.visibility_km}
                onChange={e => update('visibility_km', parseFloat(e.target.value))}
              />
            </div>
          </div>

          {/* Rain */}
          <div className="field-row single">
            <div className="field">
              <label>Rain rate (mm/hr)</label>
              <select
                value={atmosphere.rain_rate_mm_per_hr}
                onChange={e => update('rain_rate_mm_per_hr', parseFloat(e.target.value))}
              >
                {RAIN_PRESETS.map(p => (
                  <option key={p.value} value={p.value}>{p.label}</option>
                ))}
              </select>
            </div>
          </div>

          {/* Refractivity gradient */}
          <div className="field-row single">
            <div className="field">
              <label>
                Refractivity gradient (dN/dh, N-units/km)
                {isDucting && (
                  <span className="tag tag-amber" style={{ marginLeft: 6 }}>
                    DUCTING
                  </span>
                )}
              </label>
              <input
                type="number" min="-300" max="50" step="1"
                value={atmosphere.refractivity_gradient}
                onChange={e => update('refractivity_gradient', parseFloat(e.target.value))}
                title="Standard = -40 N/km. Ducting occurs below -157 N/km."
              />
            </div>
          </div>

          {isDucting && (
            <div className="alert alert-warning">
              ⚡ Tropospheric ducting conditions — signal may propagate far beyond normal horizon
            </div>
          )}
        </div>
      )}
    </div>
  )
}
