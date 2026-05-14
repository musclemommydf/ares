package com.ares.atak.plugin.net

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

/**
 * ARES-ATAK — DTOs for the Ares REST API (skeleton, growing).
 *
 * Mirrors slices of the backend (`backend/app/api/{routes,auth_routes,system_routes,
 * atak_routes,geo_routes}.py`). GeoJSON payloads are kept as opaque `JsonObject`
 * until the ATAK overlay renderer is built. Field names use `@SerialName` so the
 * Kotlin side stays idiomatic.
 */

// ── auth / server ────────────────────────────────────────────────────────────
@Serializable
data class LoginRequest(val username: String, val password: String)

@Serializable
data class LoginResponse(
    val token: String,
    @SerialName("expires_at") val expiresAt: Long,
    val user: Map<String, JsonElement> = emptyMap(),
)

@Serializable
data class ServerInfo(
    val name: String,
    val version: String,
    @SerialName("auth_enabled") val authEnabled: Boolean = false,
    @SerialName("network_policy") val networkPolicy: String? = null,
    val online: Boolean? = null,
    val mode: String? = null,
    val gpu: JsonObject? = null,
    val packs: JsonObject? = null,
)

// ── packs ────────────────────────────────────────────────────────────────────
@Serializable
data class PackDownloadRequest(
    val layers: List<String>,                       // e.g. ["terrain"], ["osm"]
    val bbox: List<Double>? = null,                 // [minlon,minlat,maxlon,maxlat]; null ⇒ "full planet"
    val fidelity: String = "auto",
    @SerialName("max_zoom") val maxZoom: Int? = null,
    val source: String? = null,                     // own tile server for large osm jobs
)

@Serializable
data class PackJob(
    @SerialName("job_id") val jobId: String,
    val status: String,                             // queued|running|done|error|not_implemented
    val progress: Double = 0.0,
    val detail: String? = null,
)

// ── transmitter / antenna / receiver (subset of the backend CoverageRequest) ──
@Serializable
data class Antenna(
    val type: String = "dipole_half_wave",
    @SerialName("gain_dbi") val gainDbi: Double? = null,
    @SerialName("azimuth_deg") val azimuthDeg: Double = 0.0,
    @SerialName("tilt_deg") val tiltDeg: Double = 0.0,
    val polarization: String = "vertical",
    @SerialName("frequency_hz") val frequencyHz: Double = 433e6,
)

@Serializable
data class Transmitter(
    val lat: Double, val lon: Double,
    @SerialName("height_m") val heightM: Double = 1.83,
    @SerialName("altitude_m") val altitudeM: Double = 0.0,
    @SerialName("power_dbm") val powerDbm: Double = 27.0,
    @SerialName("frequency_hz") val frequencyHz: Double = 433e6,
    val antenna: Antenna = Antenna(),
)

@Serializable
data class Receiver(
    @SerialName("height_m") val heightM: Double = 1.83,
    @SerialName("gain_dbi") val gainDbi: Double = 0.0,
    @SerialName("sensitivity_dbm") val sensitivityDbm: Double = -110.0,
)

// ── coverage / p2p / manet / best-site ───────────────────────────────────────
@Serializable
data class CoverageRequest(
    val transmitter: Transmitter,
    val receiver: Receiver = Receiver(),
    @SerialName("propagation_model") val propagationModel: String = "itm",
    @SerialName("diffraction_model") val diffractionModel: String = "deygout",
    @SerialName("radius_km") val radiusKm: Double = 10.0,
    @SerialName("min_signal_dbm") val minSignalDbm: Double = -110.0,
    @SerialName("include_buildings") val includeBuildings: Boolean = false,
    @SerialName("clutter_height_m") val clutterHeightM: Double = 0.0,
)

@Serializable
data class CoverageResponse(
    val status: String? = null,
    val geojson: JsonObject? = null,
    val summary: JsonObject? = null,
    val metadata: JsonObject? = null,
)

@Serializable
data class P2PRequest(
    val transmitter: Transmitter,
    @SerialName("rx_lat") val rxLat: Double,
    @SerialName("rx_lon") val rxLon: Double,
    @SerialName("rx_height_m") val rxHeightM: Double = 1.83,
    val receiver: Receiver = Receiver(),
    @SerialName("propagation_model") val propagationModel: String = "itm",
)

@Serializable
data class ManetRequest(
    val nodes: List<Transmitter>,
    @SerialName("propagation_model") val propagationModel: String = "itm",
)

@Serializable
data class GeoLineOfBearing(
    val lat: Double, val lon: Double,
    @SerialName("azimuth_deg") val azimuthDeg: Double,
    @SerialName("frequency_hz") val frequencyHz: Double,
    @SerialName("rssi_dbm") val rssiDbm: Double = -80.0,
    @SerialName("tx_power_dbm") val txPowerDbm: Double = 30.0,
    @SerialName("confidence_pct") val confidencePct: Double = 80.0,
    @SerialName("device_type") val deviceType: String = "",
    @SerialName("device_id") val deviceId: String = "",
    @SerialName("estimated_distance_m") val estimatedDistanceM: Double = 0.0,
    val time: String = "",
)

@Serializable
data class GeoFixOptions(
    @SerialName("rx_hpbw_deg") val rxHpbwDeg: Double? = null,
    @SerialName("lob_length_m") val lobLengthM: Double? = null,
)

@Serializable
data class GeoFixRequest(val observations: List<GeoLineOfBearing>, val options: GeoFixOptions = GeoFixOptions())

@Serializable
data class GeoFixResponse(
    val status: String? = null,
    val groups: List<JsonObject> = emptyList(),
    val geojson: JsonObject? = null,
)

// ── /lob/range_estimate (terrain-aware LoB capping) ──────────────────────────
@Serializable
data class LoBRangeEstimateRequest(
    @SerialName("observer_lat") val observerLat: Double,
    @SerialName("observer_lon") val observerLon: Double,
    @SerialName("observer_height_m") val observerHeightM: Double = 1.5,
    @SerialName("azimuth_deg") val azimuthDeg: Double,
    @SerialName("frequency_hz") val frequencyHz: Double,
    @SerialName("tx_power_dbm") val txPowerDbm: Double,
    @SerialName("observed_rssi_dbm") val observedRssiDbm: Double,
    @SerialName("propagation_model") val propagationModel: String = "itm",
    @SerialName("diffraction_model") val diffractionModel: String = "deygout",
    @SerialName("clutter_height_m") val clutterHeightM: Double = 0.0,
    @SerialName("terrain_resolution") val terrainResolution: String = "srtm1",
    val context: Int = 2,
    @SerialName("max_range_km") val maxRangeKm: Double = 150.0,
    @SerialName("num_points") val numPoints: Int = 300,
)

@Serializable
data class LoBRangeEstimateResponse(
    val status: String? = null,
    @SerialName("estimated_distance_m") val estimatedDistanceM: Double? = null,
    val confidence: String? = null,
    @SerialName("propagation_mode") val propagationMode: String? = null,
    val profile: List<JsonObject> = emptyList(),
)

// ── ATAK radio templates (Ares-native schema) ────────────────────────────────
@Serializable
data class RadioTemplate(
    val id: String,
    val name: String = "",
    @SerialName("icon_b64") val iconB64: String? = null,
    val transmitter: JsonObject? = null,   // kept loose — see backend app/core/templates.py schema
    val antenna: JsonObject? = null,
    val receiver: JsonObject? = null,
    val model: JsonObject? = null,
    val environment: JsonObject? = null,
)

@Serializable
data class TemplateList(val templates: List<RadioTemplate> = emptyList())
