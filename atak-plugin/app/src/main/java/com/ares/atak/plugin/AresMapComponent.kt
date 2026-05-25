// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

package com.ares.atak.plugin

import android.content.Context
import android.content.Intent
import android.os.Bundle
import com.atakmap.android.dropdown.DropDownMapComponent
import com.atakmap.android.ipc.AtakBroadcast
import com.atakmap.android.maps.MapView
import com.atakmap.comms.CotServiceRemote
import com.atakmap.coremaps.log.Log

/**
 * ARES-ATAK — map component. ATAK calls [onCreate] when the plugin loads; we
 * build the [AresDropDownReceiver] (which owns the right-side pane and its
 * network state) and register it for the SHOW_ARES intent so the toolbar
 * button can open it.
 */
class AresMapComponent : DropDownMapComponent() {

    companion object {
        /** Broadcast intent fired by the toolbar tool; opens the ARES dropdown. */
        const val SHOW_ARES = "com.ares.atak.plugin.SHOW_ARES"
        /** Friendly name of the MapGroup we drop coverage / DF overlays into. */
        const val OVERLAY_GROUP = "ARES"
    }

    private lateinit var dropDown: AresDropDownReceiver
    private var menuReceiver: AresMenuReceiver? = null      // D1.3 radial-menu actions
    private var cotRemote: CotServiceRemote? = null         // D1.4a sensor-pushed CoT

    override fun onCreate(context: Context, intent: Intent, view: MapView) {
        // Receiver lives until onDestroyImpl — it owns the AresApiClient, settings,
        // CoOptManager, DfManager, and the overlay MapGroup.
        dropDown = AresDropDownReceiver(context, view)

        val filter = AtakBroadcast.DocumentedIntentFilter().apply {
            addAction(SHOW_ARES, "Open the ARES dropdown pane (coverage / DF / Co-Opt / settings).")
        }
        registerDropDownReceiver(dropDown, filter)

        // D1.3 — radial-menu actions ("Edit RF" / "Add LoB from here"). The buttons
        // live in assets/menus/menu_ares_point.xml and fire EDIT_RF / ADD_LOB.
        menuReceiver = AresMenuReceiver(view).also {
            AtakBroadcast.getInstance().registerReceiver(it, AresMenuReceiver.filter())
        }
        // TODO(D1.3): register menu_ares_point.xml on a point's radial via the
        // SDK's MenuMapAdapter / MapMenuReceiver (exact call is ATAK-line specific).

        // D1.4a — sensor-pushed inbound CoT, so Co-Opt doesn't have to poll. We
        // forward foreign emitter fixes (a-u-G…, non-"ares-" uid) into the pane;
        // the substantive parse/fuse also happens server-side (backend D1.4b).
        cotRemote = CotServiceRemote().apply {
            setCotEventListener { event, _ -> onInboundCot(event) }
            connect(object : CotServiceRemote.ConnectionListener {
                override fun onCotServiceConnected(bundle: Bundle?) {}
                override fun onCotServiceDisconnected() {}
            })
        }

        // Make sure the parent MapGroup exists so overlay renderers don't have to.
        view.rootGroup.let { root ->
            if (root.findMapGroup(OVERLAY_GROUP) == null) root.addGroup(OVERLAY_GROUP)
        }
    }

    /** Inbound CoT from the TAK bus: surface foreign emitter fixes in the pane. */
    private fun onInboundCot(event: com.atakmap.cot.event.CotEvent) {
        val type = event.type ?: return
        val uid = event.uid ?: ""
        if (uid.startsWith("ares-")) return                 // our own — skip
        if (!(type.startsWith("a-u-G") || type.startsWith("a-h-G"))) return
        Log.d("AresMapComponent", "inbound emitter CoT $uid ($type)")
        AtakBroadcast.getInstance().sendBroadcast(
            Intent(SHOW_ARES).apply { putExtra("inbound_cot_uid", uid) })
    }

    override fun onDestroyImpl(context: Context, view: MapView) {
        if (::dropDown.isInitialized) dropDown.dispose()
        menuReceiver?.let { AtakBroadcast.getInstance().unregisterReceiver(it) }
        cotRemote?.disconnect()
        // The MapGroup persists across reloads — leaving it makes hot-reloads cheaper.
    }
}
