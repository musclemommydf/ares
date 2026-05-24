// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

package com.ares.atak.plugin

import android.content.Context
import android.content.SharedPreferences

/**
 * ARES-ATAK — persisted settings (skeleton).
 *
 * Server URL + last token + Co-Opt refresh policy + layer/link toggles. Backed
 * by SharedPreferences; survives ATAK restarts. (Token persistence is a
 * convenience for field use — clear it from the Settings tab to force re-login.)
 */
class SettingsStore(context: Context) {
    private val sp: SharedPreferences = context.getSharedPreferences("ares_atak", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = sp.getString(K_URL, "http://ares-box.lan:8000") ?: ""
        set(v) = sp.edit().putString(K_URL, v).apply()

    var username: String
        get() = sp.getString(K_USER, "") ?: ""
        set(v) = sp.edit().putString(K_USER, v).apply()

    var token: String?
        get() = sp.getString(K_TOKEN, null)
        set(v) = sp.edit().putString(K_TOKEN, v).apply()

    var allowSelfSigned: Boolean
        get() = sp.getBoolean(K_SELF_SIGNED, false)
        set(v) = sp.edit().putBoolean(K_SELF_SIGNED, v).apply()

    /** "single" = one layer per radio (Area API); "multisite" = one fused layer (GPU). */
    var coverageLayerType: String
        get() = sp.getString(K_LAYER_TYPE, "single") ?: "single"
        set(v) = sp.edit().putString(K_LAYER_TYPE, v).apply()

    var showCoverage: Boolean
        get() = sp.getBoolean(K_SHOW_COVERAGE, true)
        set(v) = sp.edit().putBoolean(K_SHOW_COVERAGE, v).apply()

    var showLinks: Boolean
        get() = sp.getBoolean(K_SHOW_LINKS, false)
        set(v) = sp.edit().putBoolean(K_SHOW_LINKS, v).apply()

    /** Co-Opt refresh: by time (seconds) and/or by distance moved (metres); 0 disables that trigger. */
    var cooptIntervalSec: Int
        get() = sp.getInt(K_COOPT_SEC, 30)
        set(v) = sp.edit().putInt(K_COOPT_SEC, v).apply()

    var cooptDistanceM: Int
        get() = sp.getInt(K_COOPT_DIST, 250)
        set(v) = sp.edit().putInt(K_COOPT_DIST, v).apply()

    companion object {
        private const val K_URL = "server_url"
        private const val K_USER = "username"
        private const val K_TOKEN = "token"
        private const val K_SELF_SIGNED = "allow_self_signed"
        private const val K_LAYER_TYPE = "coverage_layer_type"
        private const val K_SHOW_COVERAGE = "show_coverage"
        private const val K_SHOW_LINKS = "show_links"
        private const val K_COOPT_SEC = "coopt_interval_sec"
        private const val K_COOPT_DIST = "coopt_distance_m"
    }
}
