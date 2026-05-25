// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

package com.ares.atak.plugin

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.atakmap.android.ipc.AtakBroadcast
import com.atakmap.android.maps.MapView
import com.atakmap.coremaps.log.Log

/**
 * Radial-menu actions (Track D, D1.3).
 *
 * The radial buttons defined in `assets/menus/menu_ares_point.xml` fire these
 * broadcasts with the tapped point's `uid`. We route the operator into the ARES
 * pane carrying that context; the pane already owns `runCoverageRaw` (Edit RF)
 * and `DfManager.addLoB` (Add LoB), so the menu only has to hand off the point.
 *
 * Build note: compiles against the tak.gov ATAK-CIV SDK (not built in CI yet —
 * see D1.1/D1.2). The radial buttons are registered in
 * [AresMapComponent.onCreate] via the menu factory.
 */
class AresMenuReceiver(private val mapView: MapView) : BroadcastReceiver() {

    companion object {
        const val EDIT_RF = "com.ares.atak.plugin.EDIT_RF"
        const val ADD_LOB = "com.ares.atak.plugin.ADD_LOB"

        fun filter() = AtakBroadcast.DocumentedIntentFilter().apply {
            addAction(EDIT_RF, "ARES: model RF coverage from the tapped point.")
            addAction(ADD_LOB, "ARES: add a line of bearing from the tapped point.")
        }
    }

    override fun onReceive(context: Context, intent: Intent) {
        val uid = intent.getStringExtra("uid")
        // Open the ARES pane on the right tab, seeded with the selected point's uid.
        val show = Intent(AresMapComponent.SHOW_ARES).apply {
            putExtra("focus", if (intent.action == ADD_LOB) "df" else "coverage")
            uid?.let { putExtra("uid", it) }
        }
        AtakBroadcast.getInstance().sendBroadcast(show)
        Log.d("AresMenuReceiver", "radial ${intent.action} uid=$uid")
    }
}
