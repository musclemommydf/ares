package com.ares.atak.plugin

import android.content.Context
import android.content.Intent
import com.atakmap.android.dropdown.DropDownMapComponent
import com.atakmap.android.ipc.AtakBroadcast
import com.atakmap.android.maps.MapView

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

    override fun onCreate(context: Context, intent: Intent, view: MapView) {
        // Receiver lives until onDestroyImpl — it owns the AresApiClient, settings,
        // CoOptManager, DfManager, and the overlay MapGroup.
        dropDown = AresDropDownReceiver(context, view)

        val filter = AtakBroadcast.DocumentedIntentFilter().apply {
            addAction(SHOW_ARES, "Open the ARES dropdown pane (coverage / DF / Co-Opt / settings).")
        }
        registerDropDownReceiver(dropDown, filter)

        // Make sure the parent MapGroup exists so overlay renderers don't have to.
        view.rootGroup.let { root ->
            if (root.findMapGroup(OVERLAY_GROUP) == null) root.addGroup(OVERLAY_GROUP)
        }
    }

    override fun onDestroyImpl(context: Context, view: MapView) {
        if (::dropDown.isInitialized) dropDown.dispose()
        // The MapGroup persists across reloads — leaving it makes hot-reloads cheaper.
    }
}
