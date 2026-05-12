import { toast } from 'react-toastify'
import HelpPanel from './Common/HelpPanel'
import AtakServerPanel from './Tools/AtakServerPanel'
import SdrPanel from './Tools/SdrPanel'
import ArchivePanel from './Tools/ArchivePanel'

/** The top-level modal dialogs: Help · ATAK / Server console · SDR console · Archive. */
export default function AppModals({
  helpOpen, onCloseHelp,
  atakPanelOpen, onCloseAtak, mapCenter, packBboxFromMap, awaitingPackBboxRef, setDrawMode,
  sdrPanelOpen, onCloseSdr, onSdrFeatures, onSdrCoverage,
  archiveOpen, onCloseArchive, currentGeojson, currentParams, onArchiveLoad,
}) {
  return (
    <>
      {helpOpen && <HelpPanel onClose={onCloseHelp} />}

      {atakPanelOpen && (
        <AtakServerPanel
          onClose={onCloseAtak}
          mapCenter={mapCenter}
          incomingBbox={packBboxFromMap}
          onRequestDrawBbox={() => {
            onCloseAtak()
            awaitingPackBboxRef.current = true
            setDrawMode('bounds')
            toast.info('Draw a rectangle on the map to pick the pack area')
          }}
        />
      )}

      {sdrPanelOpen && (
        <SdrPanel
          onClose={onCloseSdr}
          mapCenter={mapCenter}
          onSdrFeatures={onSdrFeatures}
          onSdrCoverage={onSdrCoverage}
        />
      )}

      {archiveOpen && (
        <ArchivePanel
          currentGeojson={currentGeojson}
          currentParams={currentParams}
          onLoad={onArchiveLoad}
          onClose={onCloseArchive}
        />
      )}
    </>
  )
}
