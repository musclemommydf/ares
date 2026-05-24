// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

package com.ares.atak.plugin

import android.content.Context
import com.atak.plugins.impl.AbstractPlugin
import com.atak.plugins.impl.PluginContextProvider
import gov.tak.api.plugin.IServiceController

/**
 * ARES-ATAK — plugin entry point. ATAK's plugin registry instantiates this once
 * via the IPlugin extension declared in `assets/plugin.xml`. Construction wires:
 *   - the toolbar tool ([AresPluginTool]) that broadcasts `SHOW_ARES`, and
 *   - the map component ([AresMapComponent]) that registers the dropdown
 *     receiver and overlays.
 *
 * Lifecycle (`onStart` / `onStop`) is inherited from [AbstractPlugin]; it
 * forwards through to the contained MapComponent.
 */
class AresPlugin(
    serviceController: IServiceController,
) : AbstractPlugin(
    serviceController,
    AresPluginTool(pluginContext(serviceController)),
    AresMapComponent(),
) {
    companion object {
        /** Resolve the plugin's own Context from the IServiceController. Every
         *  modern ATAK plugin pulls its plugin-side Context this way; the host
         *  Context has the wrong ClassLoader/Resources for plugin assets. */
        private fun pluginContext(sc: IServiceController): Context {
            val provider = sc.getService(PluginContextProvider::class.java)
                ?: throw IllegalStateException("PluginContextProvider unavailable — host ATAK is too old (need 5.x)")
            return provider.pluginContext
        }
    }
}
