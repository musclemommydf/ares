// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

package com.ares.atak.plugin

import android.content.Context
import com.atak.plugins.impl.AbstractPluginTool

/**
 * ARES-ATAK — toolbar entry. Renders an icon in ATAK's toolbar; tapping it
 * broadcasts [AresMapComponent.SHOW_ARES], which [AresDropDownReceiver]
 * listens for and uses to inflate the ARES dropdown pane.
 *
 * The constructor parameters land on [AbstractPluginTool] and drive the
 * tool-grid presentation (label, long-press tooltip, intent action).
 */
class AresPluginTool(pluginContext: Context) : AbstractPluginTool(
    pluginContext,
    pluginContext.getString(R.string.app_name),                  // short label
    pluginContext.getString(R.string.app_desc),                  // long description
    AresMapComponent.SHOW_ARES,                                  // intent fired on tap
    pluginContext.resources.getDrawable(R.drawable.ic_ares, pluginContext.theme),
)
