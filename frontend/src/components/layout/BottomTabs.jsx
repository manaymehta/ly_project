import { useApp, APP_STATE } from '../../context/AppContext'
import ReviewQueue from '../review/ReviewQueue'
import HeatmapView from '../tabs/HeatmapView'
import DepletionView from '../tabs/DepletionView'

const TABS = [
  { id: 'review',    label: 'Review Queue', icon: '📋' },
  { id: 'heatmap',   label: 'Heatmap',      icon: '🗺' },
  { id: 'depletion', label: 'Depletion',    icon: '📉' },
]


export default function BottomTabs() {
  const { appState, activeTab, setActiveTab } = useApp()
  const isDisabled = appState === APP_STATE.IDLE

  return (
    <div className="flex flex-col border-t border-border bg-surface" style={{ height: '280px' }}>
      {/* Tab bar */}
      <div className="flex border-b border-border shrink-0">
        {TABS.map(tab => (
          <button
            key={tab.id}
            onClick={() => !isDisabled && setActiveTab(tab.id)}
            disabled={isDisabled}
            className={`
              flex items-center gap-2 px-5 py-2.5 text-sm font-medium border-r border-border
              transition-colors last:border-r-0
              ${isDisabled ? 'text-muted cursor-not-allowed opacity-50' : 'cursor-pointer'}
              ${!isDisabled && activeTab === tab.id
                ? 'text-slate-200 border-b-2 border-b-factory bg-surfaceHigh'
                : !isDisabled
                ? 'text-muted hover:text-slate-300 hover:bg-surfaceHigh'
                : ''
              }
            `}
            style={activeTab === tab.id && !isDisabled ? { marginBottom: '-1px' } : {}}
          >
            <span>{tab.icon}</span>
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-auto p-4">
        {isDisabled ? (
          <div className="flex items-center justify-center h-full text-muted text-sm">
            Start a session to access dashboard panels.
          </div>
        ) : activeTab === 'review' ? (
          <ReviewQueue />
        ) : activeTab === 'heatmap' ? (
          <HeatmapView />
        ) : (
          <DepletionView />
        )}
      </div>
    </div>
  )
}
