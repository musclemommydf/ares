import ResultsPanel from '../Results/ResultsPanel'
import AnalysisResults from '../Results/AnalysisResults'
import DfPanel from './DfPanel'
import ChatPanel from './ChatPanel'
import TerrainTab from './TerrainTab'
import UasVideoPanel from '../Tools/UasVideoPanel'
import ThreeDView from '../Charts/ThreeDView'
import EmitterSummary from './EmitterSummary'
import SavedLocations from './SavedLocations'
import SpaceWxPanel from './SpaceWxPanel'
import ErrorBoundary from '../Common/ErrorBoundary'
import TrackHistoryPanel from '../Map/TrackHistoryPanel'
import PassiveRadarPanel from '../Tools/PassiveRadarPanel'
import EmitterAnalyticsPanel from './EmitterAnalyticsPanel'
import AlgorithmsPanel from './AlgorithmsPanel'
import TargetsPanel from './TargetsPanel'

const COL = { flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }
const HIDDEN = { flex: 1, minHeight: 0, overflow: 'hidden' }
const SCROLL = { flex: 1, minHeight: 0, overflowY: 'auto' }

/**
 * The bottom-panel content area — dispatches on the active tab and renders it
 * (most tabs are their own components; this is the dispatch + the wrapper divs).
 * `terrain` bundles the useStandaloneTerrainProfile outputs + the P2P-sim profile.
 */
export default function BottomPanelContent({
  active,
  metadata, p2pResult, warnings, activeTab,            // results / budget
  analysisResults,                                     // { bestSiteResult, routeResult, multipointResult, manetResult, bestServerResult, bsaPolygonResult, bestSiteCandidates }
  onChatLocate,                                        // chat / video "fly to"
  terrain,                                             // terrain tab
  ul,                                                  // video → "add to map"
  terrainGrid, terrainGridLoading, coverageGeoJSON, buildingGeoJSON,   // 3-D view
  txActive, txLabel, extraTxList, lobs, lobGroups, onRemoveLoB, onEditLoB, onSimulatePropagationFromFix,   // emitter summary
  onSendAlgorithmFixToMap,                                                                              // algorithms tab
  savedLocations, onSavedFlyTo, onSavedRemove,         // saved locations
  tx, rx, propagation, spaceWeather,                   // shared
}) {
  const COVERAGE_OR_P2P = activeTab === 'coverage' || activeTab === 'radar' || activeTab === 'p2p'
  return (
    <ErrorBoundary label="This panel" resetKey={active}>
    <div style={{ flex: 1, minHeight: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
      {active === 'results' && (
        COVERAGE_OR_P2P
          ? <div style={SCROLL}><ResultsPanel metadata={metadata} p2pResult={p2pResult} warnings={warnings} spaceWeather={spaceWeather} activeTab={activeTab} /></div>
          : <div style={SCROLL}><AnalysisResults activeTab={activeTab} {...(analysisResults || {})} /></div>
      )}
      {active === '3d' && (
        <div style={COL}>
          <ThreeDView terrainGrid={terrainGrid} loading={terrainGridLoading} coverageGeoJSON={coverageGeoJSON} buildingGeoJSON={buildingGeoJSON} tx={tx} minSignalDbm={propagation.min_signal_dbm} />
        </div>
      )}
      {active === 'terrain' && (
        <TerrainTab
          terrainLineMode={terrain.terrainLineMode}
          standaloneProfile={terrain.standaloneProfile}
          standaloneProfileLoading={terrain.standaloneProfileLoading}
          standaloneProfileError={terrain.standaloneProfileError}
          onToggleLineMode={terrain.onToggleLineMode}
          onClearStandalone={terrain.onClearStandalone}
          terrainProfile={terrain.terrainProfile}
          tx={tx}
          rx={rx}
          propagationModel={propagation.model}
          waveType={propagation.wave_type}
        />
      )}
      {active === 'df' && <div style={HIDDEN}><DfPanel onSendAlgorithmFixToMap={onSendAlgorithmFixToMap} /></div>}
      {active === 'algorithms' && (
        <div style={HIDDEN}>
          <AlgorithmsPanel onSendToMap={onSendAlgorithmFixToMap} />
        </div>
      )}
      {active === 'targets' && (
        <div style={HIDDEN}>
          <TargetsPanel onSendToMap={onSendAlgorithmFixToMap} />
        </div>
      )}
      {active === 'tracks' && <div style={SCROLL}><TrackHistoryPanel /></div>}
      {active === 'passive_radar' && <div style={HIDDEN}><PassiveRadarPanel /></div>}
      {active === 'activity' && <div style={HIDDEN}><EmitterAnalyticsPanel /></div>}
      {active === 'emitters' && (
        <EmitterSummary txActive={txActive} txLabel={txLabel} tx={tx} extraTxList={extraTxList} lobs={lobs} lobGroups={lobGroups} onRemoveLoB={onRemoveLoB} onEditLoB={onEditLoB} onSimulatePropagationFromFix={onSimulatePropagationFromFix} />
      )}
      {active === 'video' && (
        <div style={HIDDEN}>
          <UasVideoPanel
            embedded
            mapCenter={{ lat: tx.lat, lon: tx.lon }}
            onLoadGeoJSON={(name, fc) => ul.addGeoJSONLayer(fc, { name })}
            onLocate={onChatLocate}
          />
        </div>
      )}
      {active === 'chat' && <div style={HIDDEN}><ChatPanel onLocate={onChatLocate} /></div>}
      {active === 'savedlocs' && <SavedLocations locations={savedLocations} onFlyTo={onSavedFlyTo} onRemove={onSavedRemove} />}
      {active === 'spacewx' && spaceWeather && <SpaceWxPanel spaceWeather={spaceWeather} />}
    </div>
    </ErrorBoundary>
  )
}
