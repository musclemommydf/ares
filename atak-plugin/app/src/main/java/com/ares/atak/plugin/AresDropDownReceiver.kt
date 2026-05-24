// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

package com.ares.atak.plugin

import android.content.Context
import android.content.Intent
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import com.ares.atak.plugin.net.AresApiClient
import com.ares.atak.plugin.net.CoverageRequest
import com.ares.atak.plugin.net.RadioTemplate
import com.ares.atak.plugin.net.Transmitter
import com.atak.plugins.impl.PluginLayoutInflater
import com.atakmap.android.dropdown.DropDown
import com.atakmap.android.dropdown.DropDownReceiver
import com.atakmap.android.maps.MapView
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/**
 * ARES-ATAK — right-side dropdown pane controller.
 *
 * Receives the SHOW_ARES broadcast (from the toolbar tool), inflates
 * `R.layout.ares_main` via [PluginLayoutInflater] (the plugin-aware inflater —
 * a plain LayoutInflater would resolve resources against the host classloader
 * and fail to find plugin layouts), and shows it as a 3/8-width dropdown.
 *
 * Owns: [AresApiClient] (REST/WS connection), [SettingsStore] (persisted
 * server URL/token/Co-Opt policy), [CoOptManager] (live coverage), and
 * [DfManager] (DF / geolocation).
 */
class AresDropDownReceiver(
    private val pluginContext: Context,
    mapView: MapView,
) : DropDownReceiver(mapView), DropDown.OnStateListener {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val settings = SettingsStore(pluginContext)
    private var api: AresApiClient? = null
    private var coOpt: CoOptManager? = null
    private var df: DfManager? = null
    private var templates: List<RadioTemplate> = emptyList()
    private var selectedTemplate: RadioTemplate? = null

    private var paneView: View? = null

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != AresMapComponent.SHOW_ARES) return

        if (paneView == null) {
            paneView = PluginLayoutInflater.inflate(pluginContext, R.layout.ares_main, null)
            wirePane(paneView!!)
        }
        showDropDown(paneView!!,
            THREE_EIGHTHS_WIDTH, FULL_HEIGHT,
            HALF_WIDTH, FULL_HEIGHT,
            this)

        if (api == null && !settings.token.isNullOrEmpty()) resumeSession()
    }

    // ── DropDown.OnStateListener ────────────────────────────────────────────
    override fun onDropDownSelectionRemoved() = Unit
    override fun onDropDownVisible(v: Boolean) = Unit
    override fun onDropDownSizeChanged(width: Double, height: Double) = Unit
    override fun onDropDownClose() = Unit

    // ── DropDownReceiver hooks ──────────────────────────────────────────────
    override fun disposeImpl() { /* nothing extra — dispose() handles teardown */ }
    override fun getAssociationKey(): String = "aresPreferences"

    // ── pane wiring ─────────────────────────────────────────────────────────
    private fun wirePane(v: View) {
        // Settings tab inputs (server URL / username / password / "allow self-signed").
        v.findViewById<EditText?>(R.id.ares_server_url)?.setText(settings.serverUrl)
        v.findViewById<EditText?>(R.id.ares_username)?.setText(settings.username)

        v.findViewById<Button?>(R.id.ares_login_btn)?.setOnClickListener {
            val url = v.findViewById<EditText>(R.id.ares_server_url).text.toString().trim()
            val user = v.findViewById<EditText>(R.id.ares_username).text.toString().trim()
            val pass = v.findViewById<EditText>(R.id.ares_password).text.toString()
            val selfSigned = v.findViewById<android.widget.CheckBox?>(R.id.ares_self_signed)?.isChecked ?: false
            connect(url, user, pass, selfSigned)
        }

        v.findViewById<Button?>(R.id.ares_btn_coverage)?.setOnClickListener {
            val centre = mapView.centerPoint?.get() ?: return@setOnClickListener
            runCoverageAt(centre.latitude, centre.longitude, null)
        }
    }

    private fun status(view: View?, msg: String) {
        val statusView = view?.findViewById<TextView?>(R.id.ares_status)
        if (statusView != null) statusView.text = msg
        else Toast.makeText(pluginContext, msg, Toast.LENGTH_SHORT).show()
    }

    // ── connection ──────────────────────────────────────────────────────────
    fun connect(serverUrl: String, username: String, password: String, allowSelfSigned: Boolean) {
        settings.serverUrl = serverUrl; settings.username = username; settings.allowSelfSigned = allowSelfSigned
        val client = AresApiClient(serverUrl, allowSelfSigned)
        scope.launch {
            try {
                val resp = client.login(username, password)
                settings.token = resp.token
                onConnected(client)
                status(paneView, "Connected to $serverUrl")
            } catch (e: Exception) {
                status(paneView, "Login failed: ${e.message}")
            }
        }
    }

    private fun resumeSession() {
        val client = AresApiClient(settings.serverUrl, settings.allowSelfSigned).apply { setToken(settings.token) }
        scope.launch {
            try { client.serverInfo(); onConnected(client) }
            catch (_: Exception) { settings.token = null }
        }
    }

    private fun onConnected(client: AresApiClient) {
        api = client
        coOpt = CoOptManager(client, settings, mapView)
        df = DfManager(client, mapView, pluginContext)
        loadTemplates()
    }

    private fun loadTemplates() {
        val client = api ?: return
        scope.launch {
            try {
                templates = client.listTemplates().templates
                selectedTemplate = templates.firstOrNull()
            } catch (_: Exception) {
                status(paneView, "Could not load templates")
            }
        }
    }

    // ── coverage actions (driven by the pane buttons / radial-menu items) ──
    fun runCoverageAt(lat: Double, lon: Double, mapItemUid: String?) {
        val client = api ?: return status(paneView, "Connect first").let { Unit }
        val tmpl = selectedTemplate ?: return status(paneView, "Pick a template first").let { Unit }
        scope.launch {
            try {
                val req = client.templateCoverageRequest(tmpl.id, lat, lon, null)
                val resp = client.coverage(req)
                val summary = CoverageOverlayRenderer.render(mapView, pluginContext, resp,
                    "ARES:${tmpl.id}:${mapItemUid ?: "$lat,$lon"}")
                status(paneView, "Coverage: ${summary.covered}/${summary.points} covered")
            } catch (e: Exception) {
                status(paneView, "Coverage failed: ${e.message}")
            }
        }
    }

    /** Ad-hoc coverage from the "Edit RF" radial-menu sheet. */
    fun runCoverageRaw(tx: Transmitter, radiusKm: Double, minSignalDbm: Double) {
        val client = api ?: return
        scope.launch {
            runCatching {
                client.coverage(CoverageRequest(transmitter = tx, radiusKm = radiusKm, minSignalDbm = minSignalDbm))
            }.onSuccess { CoverageOverlayRenderer.render(mapView, pluginContext, it, "ARES:adhoc") }
        }
    }

    // ── accessors for other tabs (Co-Opt / DF / templates) ──────────────────
    fun coOptManager(): CoOptManager? = coOpt
    fun dfManager(): DfManager? = df
    fun availableTemplates(): List<RadioTemplate> = templates
    fun selectTemplate(id: String) { selectedTemplate = templates.firstOrNull { it.id == id } }

    fun dispose() {
        coOpt?.dispose(); df?.dispose()
        scope.cancel()
        api = null; coOpt = null; df = null
        paneView = null
    }
}
