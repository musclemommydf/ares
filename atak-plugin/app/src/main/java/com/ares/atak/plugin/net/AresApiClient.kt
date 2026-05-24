// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

package com.ares.atak.plugin.net

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.concurrent.TimeUnit

/**
 * ARES-ATAK — REST/WS client for an Ares server (skeleton, growing).
 *
 * - bearer-token auth ([login] → token attached to every request);
 * - field-server convenience: `allowSelfSigned` relaxes TLS (replaced by a
 *   pinning UI in P5);
 * - REST: server info, packs, radio templates, coverage, point-to-point, MANET,
 *   geolocate/fix, KMZ export;
 * - WS: [openSimulateProgress] subscribes to `/api/v1/ws/simulate` and pushes
 *   progress events to a callback.
 *
 * Real impl will likely move to Retrofit + `converter-kotlinx-serialization`;
 * the direct-OkHttp form here keeps the surface obvious.
 */
class AresApiClient(baseUrl: String, allowSelfSigned: Boolean = false) {

    val base: String = baseUrl.trimEnd('/')
    @Volatile var token: String? = null
        private set
    val json: Json = Json { ignoreUnknownKeys = true; explicitNulls = false }

    private val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(180, TimeUnit.SECONDS)            // coverage / pack jobs can be slow
        .pingInterval(20, TimeUnit.SECONDS)            // keep the WS alive
        .addInterceptor(Interceptor { chain ->
            val b = chain.request().newBuilder()
            token?.let { b.header("Authorization", "Bearer $it") }
            chain.proceed(b.build())
        })
        .apply { if (allowSelfSigned) trustAllCerts(this) }
        .build()

    fun setToken(t: String?) { token = t }

    // ── low-level ───────────────────────────────────────────────────────────
    private val JSON = "application/json".toMediaType()

    private inline fun <reified T> get(path: String): T = call(Request.Builder().url("$base$path").get().build())
    private inline fun <reified T, reified B> post(path: String, body: B): T =
        call(Request.Builder().url("$base$path")
            .post(json.encodeToString(serializer(), body).toRequestBody(JSON)).build())
    private inline fun <reified T, reified B> put(path: String, body: B): T =
        call(Request.Builder().url("$base$path")
            .put(json.encodeToString(serializer(), body).toRequestBody(JSON)).build())
    private inline fun <reified T> delete(path: String): T = call(Request.Builder().url("$base$path").delete().build())

    private inline fun <reified T> call(req: Request): T {
        http.newCall(req).execute().use { r: Response ->
            val txt = r.body?.string().orEmpty()
            check(r.isSuccessful) { "HTTP ${r.code} ${req.method} ${req.url} — ${txt.take(300)}" }
            return json.decodeFromString(serializer(), txt)
        }
    }

    // ── auth / server ───────────────────────────────────────────────────────
    suspend fun login(username: String, password: String): LoginResponse = withContext(Dispatchers.IO) {
        val r: LoginResponse = post("/api/v1/auth/login", LoginRequest(username, password))
        token = r.token
        r
    }
    suspend fun serverInfo(): ServerInfo = withContext(Dispatchers.IO) { get("/api/v1/server/info") }

    // ── packs ───────────────────────────────────────────────────────────────
    suspend fun listPacks(): JsonObject = withContext(Dispatchers.IO) { get("/api/v1/packs") }
    suspend fun downloadPack(req: PackDownloadRequest): PackJob = withContext(Dispatchers.IO) { post("/api/v1/packs/download", req) }
    suspend fun packJob(jobId: String): PackJob = withContext(Dispatchers.IO) { get("/api/v1/packs/jobs/$jobId") }

    // ── radio templates ─────────────────────────────────────────────────────
    suspend fun listTemplates(): TemplateList = withContext(Dispatchers.IO) { get("/api/v1/atak/templates") }
    suspend fun getTemplate(id: String): RadioTemplate = withContext(Dispatchers.IO) { get("/api/v1/atak/templates/$id") }
    suspend fun putTemplate(t: RadioTemplate): RadioTemplate = withContext(Dispatchers.IO) { put("/api/v1/atak/templates/${t.id}", t) }
    suspend fun deleteTemplate(id: String): JsonObject = withContext(Dispatchers.IO) { delete("/api/v1/atak/templates/$id") }
    /** Flatten a template + a placed location into a /simulate/coverage body. */
    suspend fun templateCoverageRequest(id: String, lat: Double, lon: Double, azimuthDeg: Double?): CoverageRequest =
        withContext(Dispatchers.IO) {
            val q = StringBuilder("/api/v1/atak/templates/$id/coverage_request?lat=$lat&lon=$lon")
            if (azimuthDeg != null) q.append("&azimuth_deg=$azimuthDeg")
            post(q.toString(), emptyMap<String, String>())
        }

    // ── propagation ─────────────────────────────────────────────────────────
    suspend fun coverage(req: CoverageRequest): CoverageResponse = withContext(Dispatchers.IO) { post("/api/v1/simulate/coverage", req) }
    suspend fun p2p(req: P2PRequest): JsonObject = withContext(Dispatchers.IO) { post("/api/v1/simulate/p2p", req) }
    suspend fun manet(req: ManetRequest): JsonObject = withContext(Dispatchers.IO) { post("/api/v1/simulate/manet", req) }

    // ── geolocation ─────────────────────────────────────────────────────────
    suspend fun geolocateFix(req: GeoFixRequest): GeoFixResponse = withContext(Dispatchers.IO) { post("/api/v1/geolocate/fix", req) }

    /** Terrain-aware range cap for a single LoB. Builds the request from a LoB DTO
     *  so callers don't have to repackage; returns the parsed range/confidence response. */
    suspend fun lobRangeEstimate(lob: GeoLineOfBearing): LoBRangeEstimateResponse = withContext(Dispatchers.IO) {
        post("/api/v1/lob/range_estimate", LoBRangeEstimateRequest(
            observerLat = lob.lat, observerLon = lob.lon,
            azimuthDeg = lob.azimuthDeg, frequencyHz = lob.frequencyHz,
            txPowerDbm = lob.txPowerDbm, observedRssiDbm = lob.rssiDbm,
        ))
    }

    // ── KMZ export (returns the raw .kmz bytes) ─────────────────────────────
    suspend fun exportKmz(geojson: JsonObject, name: String, minSignalDbm: Double = -120.0): ByteArray = withContext(Dispatchers.IO) {
        val payload = kotlinx.serialization.json.buildJsonObject {
            put("geojson", geojson)
            put("name", kotlinx.serialization.json.JsonPrimitive(name))
            put("min_signal_dbm", kotlinx.serialization.json.JsonPrimitive(minSignalDbm))
        }
        val body = json.encodeToString(JsonObject.serializer(), payload).toRequestBody(JSON)
        http.newCall(Request.Builder().url("$base/api/v1/atak/export/kmz").post(body).build()).execute().use { r ->
            check(r.isSuccessful) { "KMZ export HTTP ${r.code}" }
            r.body!!.bytes()
        }
    }

    // ── WS progress: /api/v1/ws/simulate ────────────────────────────────────
    fun openSimulateProgress(onEvent: (JsonObject) -> Unit, onClosed: () -> Unit = {}): WebSocket {
        val wsUrl = base.replace("https://", "wss://").replace("http://", "ws://") + "/api/v1/ws/simulate"
        val req = Request.Builder().url(wsUrl).apply { token?.let { header("Authorization", "Bearer $it") } }.build()
        return http.newWebSocket(req, object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                runCatching { json.decodeFromString<JsonObject>(text) }.getOrNull()?.let(onEvent)
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = onClosed()
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = onClosed()
        })
    }

    // ── field-server convenience: accept self-signed certs (replaced by pinning UI in P5) ──
    private fun trustAllCerts(builder: OkHttpClient.Builder) {
        try {
            val tm = object : javax.net.ssl.X509TrustManager {
                override fun checkClientTrusted(c: Array<out java.security.cert.X509Certificate>?, a: String?) {}
                override fun checkServerTrusted(c: Array<out java.security.cert.X509Certificate>?, a: String?) {}
                override fun getAcceptedIssuers(): Array<java.security.cert.X509Certificate> = arrayOf()
            }
            val ctx = javax.net.ssl.SSLContext.getInstance("TLS").apply {
                init(null, arrayOf<javax.net.ssl.TrustManager>(tm), java.security.SecureRandom())
            }
            builder.sslSocketFactory(ctx.socketFactory, tm)
            builder.hostnameVerifier { _, _ -> true }
        } catch (_: Exception) { /* keep strict TLS */ }
    }

    companion object { private inline fun <reified T> serializer() = kotlinx.serialization.serializer<T>() }
}
