import { useState } from 'react'
import SupplyChainGraph from '../graph/SupplyChainGraph'
import VulnerabilityGraph from '../tabs/VulnerabilityGraph'
import ContextPanel from './ContextPanel'
import BottomTabs from './BottomTabs'
import { useApp, APP_STATE } from '../../context/AppContext'

export default function MainLayout() {
  const { appState } = useApp()
  const [graphView, setGraphView] = useState('supply')
  const isActive = appState !== APP_STATE.IDLE

  return (
    <div className="flex flex-col flex-1 overflow-hidden">
      {/* Main content: graph + context panel */}
      <div className="flex flex-1 overflow-hidden">
        {/* Graph area */}
        <div className="flex-1 overflow-hidden bg-bg relative">
          {/* Toggle */}
          {isActive && (
            <div className="absolute top-3 right-3 z-20 flex rounded overflow-hidden border border-border text-xs font-medium">
              <button
                onClick={() => setGraphView('supply')}
                className={`px-3 py-1.5 transition-colors ${
                  graphView === 'supply'
                    ? 'bg-factory text-white'
                    : 'bg-surface text-muted hover:text-slate-300 hover:bg-surfaceHigh'
                }`}
              >
                Supply Chain
              </button>
              <button
                onClick={() => setGraphView('vulnerability')}
                className={`px-3 py-1.5 transition-colors border-l border-border ${
                  graphView === 'vulnerability'
                    ? 'bg-risk-high text-white'
                    : 'bg-surface text-muted hover:text-slate-300 hover:bg-surfaceHigh'
                }`}
              >
                Vulnerability
              </button>
            </div>
          )}

          {graphView === 'supply' || !isActive
            ? <SupplyChainGraph />
            : <VulnerabilityGraph />
          }
        </div>

        {/* Context panel — fixed width */}
        <div className="w-80 shrink-0 overflow-hidden">
          <ContextPanel />
        </div>
      </div>

      {/* Bottom tabs */}
      <BottomTabs />
    </div>
  )
}
