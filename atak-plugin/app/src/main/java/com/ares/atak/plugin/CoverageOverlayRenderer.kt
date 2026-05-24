// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

package com.ares.atak.plugin

import android.content.Context
import com.ares.atak.plugin.net.CoverageResponse
import com.atakmap.android.maps.MapGroup
import com.atakmap.android.maps.MapView
import com.atakmap.android.maps.Marker
import com.atakmap.coremap.maps.coords.GeoPoint
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * ARES-ATAK — turn an Ares coverage GeoJSON into ATAK map items.
 *
 * Default strategy: parse the Point FeatureCollection and add coloured
 * [Marker]s to a child [MapGroup] under "ARES" — one per covered point. The
 * marker colour is graduated by signal strength so the overlay reads at a
 * glance, matching the web UI's heatmap.
 *
 * Alternative (raster): use [com.ares.atak.plugin.net.AresApiClient.exportKmz]
 * to fetch the SOOTHSAYER-style raster KMZ and drop it into `atak/ARES/KMZ/`
 * for ATAK's standard Image-Overlay importer. That's wired separately on the
 * Coverage tab.
 */
object CoverageOverlayRenderer {

    data class Summary(val points: Int, val covered: Int, val maxSignalDbm: Double?, val minSignalDbm: Double?)

    fun summarize(resp: CoverageResponse): Summary {
        val feats = resp.geojson?.get("features")?.jsonArray ?: return Summary(0, 0, null, null)
        var covered = 0; var maxS: Double? = null; var minS: Double? = null
        for (f in feats) {
            val props = f.jsonObject["properties"]?.jsonObject ?: continue
            val isCov = props["covered"]?.jsonPrimitive?.content?.toBooleanStrictOrNull() ?: true
            if (isCov) covered++
            props["signal_dbm"]?.jsonPrimitive?.content?.toDoubleOrNull()?.let { s ->
                maxS = if (maxS == null || s > maxS!!) s else maxS
                minS = if (minS == null || s < minS!!) s else minS
            }
        }
        return Summary(feats.size, covered, maxS, minS)
    }

    /** Render `resp` into the "ARES" overlay MapGroup under [layerName]. Replaces
     *  any prior layer with the same name (Co-Opt re-runs land here). */
    fun render(mapView: MapView, pluginContext: Context, resp: CoverageResponse, layerName: String): Summary {
        val s = summarize(resp)
        val root = mapView.rootGroup
        val parent = root.findMapGroup(AresMapComponent.OVERLAY_GROUP)
            ?: root.addGroup(AresMapComponent.OVERLAY_GROUP)

        // Replace prior layer with the same name (Co-Opt reruns / Edit RF re-applies).
        parent.findMapGroup(layerName)?.let { parent.removeGroup(it) }
        val layer = parent.addGroup(layerName)
        layer.setMetaBoolean("addToObjList", true)
        layer.setMetaString("aresLayer", "coverage")

        val feats = resp.geojson?.get("features")?.jsonArray ?: return s
        var idx = 0
        for (f in feats) {
            val obj = f.jsonObject
            val coords = obj["geometry"]?.jsonObject?.get("coordinates")?.jsonArray ?: continue
            if (coords.size < 2) continue
            val lon = coords[0].jsonPrimitive.content.toDoubleOrNull() ?: continue
            val lat = coords[1].jsonPrimitive.content.toDoubleOrNull() ?: continue
            val props = obj["properties"]?.jsonObject
            val isCov = props?.get("covered")?.jsonPrimitive?.content?.toBooleanStrictOrNull() ?: true
            if (!isCov) continue
            val sig = props?.get("signal_dbm")?.jsonPrimitive?.content?.toDoubleOrNull()
            val m = Marker(GeoPoint(lat, lon), "$layerName:${idx++}").apply {
                setColor(signalToColor(sig))
                setMetaBoolean("addToObjList", false)
                setMetaString("aresLayer", layerName)
                setMetaBoolean("readiness", true)
                setType("u-d-p")              // generic "point" CoT type so ATAK doesn't try to be clever
                setTitle("${sig?.let { "%.1f dBm".format(it) } ?: "covered"}")
            }
            layer.addItem(m)
        }
        return s
    }

    /** Remove an ARES coverage layer by name (used by CoOptManager.release). */
    fun remove(mapView: MapView, layerName: String) {
        val parent = mapView.rootGroup.findMapGroup(AresMapComponent.OVERLAY_GROUP) ?: return
        parent.findMapGroup(layerName)?.let { parent.removeGroup(it) }
    }

    /** Signal-strength → ARGB. Ramp roughly matches the web UI: red (strong) →
     *  yellow → green → blue (weak/edge of coverage). */
    private fun signalToColor(signalDbm: Double?): Int {
        val s = signalDbm ?: return 0xFF06D6A0.toInt()         // teal-green fallback
        return when {
            s >= -60 -> 0xFFEF4444.toInt()
            s >= -75 -> 0xFFF59E0B.toInt()
            s >= -90 -> 0xFFEAB308.toInt()
            s >= -100 -> 0xFF06D6A0.toInt()
            else -> 0xFF3B82F6.toInt()
        }
    }
}
