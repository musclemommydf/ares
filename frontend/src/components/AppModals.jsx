// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import HelpPanel from './Common/HelpPanel'
import AtakServerPanel from './Tools/AtakServerPanel'
import SdrPanel from './Tools/SdrPanel'

/** The top-level modal dialogs: Help · ATAK / Server console · SDR console. */
export default function AppModals({
  helpOpen, onCloseHelp,
  atakPanelOpen, onCloseAtak, mapCenter,
  sdrPanelOpen, onCloseSdr, sdr, sdrHidden, onSdrPickLocation, sdrMapFeatures,
}) {
  return (
    <>
      {helpOpen && <HelpPanel onClose={onCloseHelp} />}

      {atakPanelOpen && (
        <AtakServerPanel onClose={onCloseAtak} mapCenter={mapCenter} />
      )}

      {sdrPanelOpen && (
        <SdrPanel
          onClose={onCloseSdr}
          mapCenter={mapCenter}
          sdr={sdr}
          hidden={sdrHidden}
          onPickLocation={onSdrPickLocation}
          mapFeatures={sdrMapFeatures}
        />
      )}
    </>
  )
}
