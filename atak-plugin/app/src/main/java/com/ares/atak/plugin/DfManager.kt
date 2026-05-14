package com.ares.atak.plugin

import android.content.Context
import com.ares.atak.plugin.net.AresApiClient
import com.ares.atak.plugin.net.GeoFixOptions
import com.ares.atak.plugin.net.GeoFixRequest
import com.ares.atak.plugin.net.GeoFixResponse
import com.ares.atak.plugin.net.GeoLineOfBearing
import com.atakmap.android.maps.MapGroup
import com.atakmap.android.maps.MapView
import com.atakmap.android.maps.Marker
import com.atakmap.comms.CommsMapComponent
import com.atakmap.coremap.cot.event.CotDetail
import com.atakmap.coremap.cot.event.CotEvent
import com.atakmap.coremap.cot.event.CotPoint
import com.atakmap.coremap.maps.coords.GeoPoint
import com.atakmap.coremap.maps.time.CoordinatedTime
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.util.UUID

/**
 * ARES-ATAK — DF / geolocation. The Ares-exclusive feature SOOTHSAYER has no
 * answer to.
 *
 * Flow:
 *   1. operator adds Lines-of-Bearing (from the radial-menu "Add LoB" or from
 *      an SDR feed): azimuth, RSSI, frequency, antenna, observer height, etc;
 *   2. (optional) each bearing is terrain-capped via
 *      `POST /api/v1/lob/range_estimate` to set `estimatedDistanceM`;
 *   3. all LoBs are POSTed to `POST /api/v1/geolocate/fix` → groups, Cut/Fix,
 *      confidence-weighted centroid, CEP/CAP ellipse, and a GeoJSON
 *      FeatureCollection (wedges / ellipses / suspected emitters);
 *   4. wedges + ellipses + emitter Markers land in a child MapGroup ("ARES-DF")
 *      and the suspected emitters are also published as CoT to the team via
 *      [CommsMapComponent.sendCoTToServersByMission].
 */
class DfManager(
    private val api: AresApiClient,
    private val mapView: MapView,
    @Suppress("UNUSED_PARAMETER") pluginContext: Context,
) {
    companion object {
        const val DF_GROUP = "ARES-DF"
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val lobs = mutableListOf<GeoLineOfBearing>()
    private var rxHpbwDeg: Double? = 30.0

    fun setReceiverBeamwidth(deg: Double?) { rxHpbwDeg = deg }
    fun addLoB(lob: GeoLineOfBearing) { lobs += lob }
    fun count() = lobs.size

    fun clear() {
        lobs.clear()
        dfGroup()?.clearItems()
    }

    /** Solve fixes from the current LoB set. Renders the response to the map and
     *  publishes a CoT marker for each suspected emitter. */
    fun solve(onResult: (GeoFixResponse) -> Unit, onError: (String) -> Unit) {
        if (lobs.isEmpty()) { onError("no LoBs added"); return }
        scope.launch {
            try {
                val resp = api.geolocateFix(GeoFixRequest(lobs.toList(), GeoFixOptions(rxHpbwDeg = rxHpbwDeg)))
                renderToMap(resp)
                publishCoTForEmitters(resp)
                onResult(resp)
            } catch (e: Exception) {
                onError(e.message ?: e.toString())
            }
        }
    }

    /** Terrain-aware bearing caps via `/lob/range_estimate` before solving. Sets
     *  `estimatedDistanceM` on each LoB so the solver can clip wedges to terrain
     *  rather than running them to the radio horizon. */
    fun refineWithTerrain(onDone: () -> Unit) {
        if (lobs.isEmpty()) { onDone(); return }
        scope.launch {
            val refined = lobs.map { lob ->
                runCatching { api.lobRangeEstimate(lob) }.getOrNull()?.let { resp ->
                    lob.copy(estimatedDistanceM = resp.estimatedDistanceM ?: lob.estimatedDistanceM)
                } ?: lob
            }
            lobs.clear(); lobs.addAll(refined)
            onDone()
        }
    }

    fun dispose() {
        scope.coroutineContext[Job]?.cancel()
        dfGroup()?.clearItems()
    }

    // ── rendering / CoT ─────────────────────────────────────────────────────
    private fun dfGroup(): MapGroup? {
        val root = mapView.rootGroup.findMapGroup(AresMapComponent.OVERLAY_GROUP) ?: return null
        return root.findMapGroup(DF_GROUP) ?: root.addGroup(DF_GROUP)
    }

    private fun renderToMap(resp: GeoFixResponse) {
        val group = dfGroup() ?: return
        group.clearItems()
        val feats = resp.geojson?.get("features")?.jsonArray ?: return
        for (f in feats) {
            val obj = f.jsonObject
            val props = obj["properties"]?.jsonObject ?: continue
            val kind = props["kind"]?.jsonPrimitive?.content ?: continue
            if (kind != "suspected_emitter") continue              // wedges/ellipses are rendered as drawing shapes later
            val coords = obj["geometry"]?.jsonObject?.get("coordinates")?.jsonArray ?: continue
            if (coords.size < 2) continue
            val lon = coords[0].jsonPrimitive.content.toDoubleOrNull() ?: continue
            val lat = coords[1].jsonPrimitive.content.toDoubleOrNull() ?: continue
            val conf = props["confidence_pct"]?.jsonPrimitive?.content?.toDoubleOrNull()
            val m = Marker(GeoPoint(lat, lon), "ARES-DF-${UUID.randomUUID()}").apply {
                setType("a-h-G")                                    // hostile, ground (suspected emitter)
                setTitle("Suspected emitter${conf?.let { " (${"%.0f".format(it)}%)" } ?: ""}")
                setColor(0xFFEF4444.toInt())
                setMetaString("aresLayer", "df")
                setMetaBoolean("readiness", true)
            }
            group.addItem(m)
        }
    }

    /** For every suspected_emitter feature, publish a CoT marker so teammates
     *  see it in their own ATAK. Uses [CommsMapComponent.sendCoTToServersByMission]
     *  with a null mission key, which delivers to every connected TAK server. */
    private fun publishCoTForEmitters(resp: GeoFixResponse) {
        val comms = CommsMapComponent.getInstance() ?: return
        val feats = resp.geojson?.get("features")?.jsonArray ?: return
        for (f in feats) {
            val obj = f.jsonObject
            val props = obj["properties"]?.jsonObject ?: continue
            val kind = props["kind"]?.jsonPrimitive?.content ?: continue
            if (kind != "suspected_emitter") continue
            val coords = obj["geometry"]?.jsonObject?.get("coordinates")?.jsonArray ?: continue
            if (coords.size < 2) continue
            val lon = coords[0].jsonPrimitive.content.toDoubleOrNull() ?: continue
            val lat = coords[1].jsonPrimitive.content.toDoubleOrNull() ?: continue
            val ce = props["cep_m"]?.jsonPrimitive?.content?.toDoubleOrNull() ?: 9_999_999.0
            val cot = CotEvent().apply {
                uid = "ARES-EMITTER-${UUID.randomUUID()}"
                type = "a-h-G"
                how = "m-g"                                          // machine-generated
                time = CoordinatedTime()
                start = time
                stale = CoordinatedTime(time.milliseconds + 5 * 60_000L)
                point = CotPoint(lat, lon, 0.0, ce, ce)
                detail = CotDetail("detail").apply {
                    addChild(CotDetail("contact").apply { setAttribute("callsign", "ARES suspect") })
                    addChild(CotDetail("__group").apply { setAttribute("name", "Red"); setAttribute("role", "Sensor") })
                    addChild(CotDetail("remarks").apply { setInnerText("ARES geolocation fix (confidence ${props["confidence_pct"]?.jsonPrimitive?.content ?: "?"}%)") })
                }
            }
            try {
                comms.sendCoTToServersByMission(null, cot)            // null = all servers
            } catch (_: Throwable) {
                // CoT broadcast is best-effort; the local marker still lands.
            }
        }
    }
}
