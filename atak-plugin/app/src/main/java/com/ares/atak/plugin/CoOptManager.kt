package com.ares.atak.plugin

import com.ares.atak.plugin.net.AresApiClient
import com.ares.atak.plugin.net.RadioTemplate
import com.atakmap.android.maps.MapView
import com.atakmap.android.maps.Marker
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * ARES-ATAK — "Co-Opt" live coverage. Adopt any marker / vehicle / person on
 * the ATAK map, assign it a radio template, and re-run coverage from its live
 * GPS position. Refreshes on a time interval and/or after the item has moved a
 * set distance, replacing its coverage layer in place. (Same headline feature
 * as SOOTHSAYER's Co-Opt.)
 *
 * Position source: the adopted item's [Marker.getPoint] — kept in sync by
 * ATAK's CoT infrastructure (own-position via Self marker; teammates via
 * CommsService; sensors via their own CoT publishers). We poll rather than
 * subscribe because the trigger is time AND distance — a CoT-event-driven loop
 * would have to dedupe back to the same conditions.
 */
class CoOptManager(
    private val api: AresApiClient,
    private val settings: SettingsStore,
    private val mapView: MapView,
) {
    data class Adopted(
        val uid: String,
        val template: RadioTemplate,
        var lastLat: Double = Double.NaN,
        var lastLon: Double = Double.NaN,
        var lastRunMs: Long = 0L,
        var job: Job? = null,
    )

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val adopted = LinkedHashMap<String, Adopted>()

    fun adopt(uid: String, template: RadioTemplate) {
        stop(uid)
        val a = Adopted(uid, template)
        adopted[uid] = a
        a.job = scope.launch { loop(a) }
    }

    fun release(uid: String) {
        stop(uid); adopted.remove(uid)
        CoverageOverlayRenderer.remove(mapView, "ARES:coopt:$uid")
    }
    fun releaseAll() { adopted.keys.toList().forEach(::release) }
    fun adoptedUids(): List<String> = adopted.keys.toList()

    private fun stop(uid: String) { adopted[uid]?.job?.cancel(); adopted[uid]?.job = null }

    private suspend fun loop(a: Adopted) {
        while (scope.isActive) {
            val pos = currentPosition(a.uid)
            if (pos != null && shouldRefresh(a, pos.first, pos.second)) {
                runCoverage(a, pos.first, pos.second)
                a.lastLat = pos.first; a.lastLon = pos.second; a.lastRunMs = System.currentTimeMillis()
            }
            val sec = if (settings.cooptIntervalSec > 0) settings.cooptIntervalSec else 30
            delay(sec * 1000L)
        }
    }

    private fun shouldRefresh(a: Adopted, lat: Double, lon: Double): Boolean {
        if (a.lastRunMs == 0L) return true
        val byTime = settings.cooptIntervalSec > 0 &&
            System.currentTimeMillis() - a.lastRunMs >= settings.cooptIntervalSec * 1000L
        val byDist = settings.cooptDistanceM > 0 && !a.lastLat.isNaN() &&
            haversineM(a.lastLat, a.lastLon, lat, lon) >= settings.cooptDistanceM
        return byTime || byDist
    }

    private suspend fun runCoverage(a: Adopted, lat: Double, lon: Double) {
        try {
            val req = api.templateCoverageRequest(a.template.id, lat, lon, null)
            val resp = api.coverage(req)
            CoverageOverlayRenderer.render(mapView, mapView.context, resp, "ARES:coopt:${a.uid}")
        } catch (_: Exception) {
            // Silent failure here — caller (the UI tab) can surface status via
            // the AresApiClient log; an opaque error toast every 30s would spam
            // the user when offline.
        }
    }

    /** Resolve the adopted item's current position via ATAK's MapView lookup.
     *  Returns null when the marker has been deleted or has no fix. */
    private fun currentPosition(uid: String): Pair<Double, Double>? {
        val item = mapView.rootGroup.deepFindUID(uid) as? Marker ?: return null
        val gp = item.point ?: return null
        if (!gp.isValid) return null
        return gp.latitude to gp.longitude
    }

    private fun haversineM(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val r = 6_371_000.0
        val dLat = Math.toRadians(lat2 - lat1); val dLon = Math.toRadians(lon2 - lon1)
        val s = Math.sin(dLat / 2).let { it * it } +
            Math.cos(Math.toRadians(lat1)) * Math.cos(Math.toRadians(lat2)) * Math.sin(dLon / 2).let { it * it }
        return 2 * r * Math.asin(Math.min(1.0, Math.sqrt(s)))
    }

    fun dispose() {
        releaseAll()
        scope.coroutineContext[Job]?.cancel()
    }
}
