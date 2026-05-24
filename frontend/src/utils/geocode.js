// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Forward geocoding via OpenStreetMap Nominatim. Shared by the 2D map and the
 * 3D globe toolbars. Returns [{ name, display_name, lat, lon, bounds }] where
 * bounds is [[south, west], [north, east]] (Leaflet-style) or null.
 */
export async function geocodeNominatim(query) {
  const url = `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(query)}&format=json&limit=5&addressdetails=0`
  const res = await fetch(url, { headers: { 'Accept-Language': 'en' } })
  if (!res.ok) throw new Error('Geocoding request failed')
  const data = await res.json()
  return data.map(r => ({
    name: r.display_name.split(',').slice(0, 3).join(',').trim(),
    display_name: r.display_name,
    lat: parseFloat(r.lat),
    lon: parseFloat(r.lon),
    // boundingbox: [south, north, west, east]
    bounds: r.boundingbox
      ? [[parseFloat(r.boundingbox[0]), parseFloat(r.boundingbox[2])],
         [parseFloat(r.boundingbox[1]), parseFloat(r.boundingbox[3])]]
      : null,
  }))
}
