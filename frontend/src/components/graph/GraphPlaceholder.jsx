import { useApp, APP_STATE } from '../../context/AppContext'

const MOCK_NODES = [
  { id: 'F001', label: 'F001', group: 'Factory',     title: 'Factory 1',     disruptable: true },
  { id: 'F002', label: 'F002', group: 'Factory',     title: 'Factory 2',     disruptable: true },
  { id: 'A004', label: 'A004', group: 'API',         title: 'API Ingredient', disruptable: true },
  { id: 'D001', label: 'D001', group: 'Drug',        title: 'Drug 1',        disruptable: false },
  { id: 'S001', label: 'S001', group: 'Distributor', title: 'Distributor 1', disruptable: true },
  { id: 'H001', label: 'H001', group: 'Hospital',    title: 'Hospital 1',    disruptable: false },
]

const GROUP_COLORS = {
  Factory:     { bg: 'bg-factory/20',     border: 'border-factory',     text: 'text-factory' },
  API:         { bg: 'bg-api/20',         border: 'border-api',         text: 'text-api' },
  Drug:        { bg: 'bg-drug/20',        border: 'border-drug',        text: 'text-drug' },
  Distributor: { bg: 'bg-distributor/20', border: 'border-distributor', text: 'text-distributor' },
  Hospital:    { bg: 'bg-hospital/20',    border: 'border-hospital',    text: 'text-hospital' },
}

const LEVEL_ORDER = ['Factory', 'API', 'Drug', 'Distributor', 'Hospital']

export default function GraphPlaceholder() {
  const { appState, selectedNode, selectNode } = useApp()
  const isDisabled = appState === APP_STATE.IDLE || appState === APP_STATE.PIPELINE_RUNNING

  const grouped = LEVEL_ORDER.map(group => ({
    group,
    nodes: MOCK_NODES.filter(n => n.group === group),
  }))

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-border">
        <span className="text-xs font-medium text-muted uppercase tracking-wider">
          Supply Chain Graph
        </span>
        <span className="text-xs text-muted italic">
          Phase 2 — full vis.js graph coming next
        </span>
      </div>

      {/* Legend */}
      <div className="flex items-center gap-4 px-4 py-2 border-b border-border">
        {Object.entries(GROUP_COLORS).map(([group, colors]) => (
          <div key={group} className="flex items-center gap-1.5">
            <div className={`w-2.5 h-2.5 rounded-sm border ${colors.border} ${colors.bg}`} />
            <span className="text-xs text-muted">{group}</span>
          </div>
        ))}
        <div className="ml-auto text-xs text-muted">
          Click a <span className="text-factory">Factory</span>,{' '}
          <span className="text-distributor">Distributor</span>, or{' '}
          <span className="text-api">API</span> node to disrupt
        </div>
      </div>

      {/* Graph area */}
      <div className="flex-1 flex items-center justify-center p-6 overflow-auto">
        {appState === APP_STATE.IDLE ? (
          <div className="text-center text-muted">
            <div className="text-4xl mb-3">⬡</div>
            <p className="text-sm">Start a session to load the supply chain graph</p>
          </div>
        ) : (
          <div className="flex gap-12 items-start w-full justify-center">
            {grouped.map(({ group, nodes }) => (
              <div key={group} className="flex flex-col items-center gap-3">
                {/* Group label */}
                <span className={`text-xs font-semibold uppercase tracking-widest ${GROUP_COLORS[group].text}`}>
                  {group}
                </span>

                {/* Nodes */}
                <div className="flex flex-col gap-2">
                  {nodes.map(node => {
                    const colors  = GROUP_COLORS[node.group]
                    const isSelected = selectedNode?.id === node.id
                    const canClick   = node.disruptable && !isDisabled

                    return (
                      <button
                        key={node.id}
                        onClick={() => canClick && selectNode(node)}
                        title={node.title}
                        className={`
                          px-4 py-2 rounded border text-sm font-mono font-medium transition-all
                          ${colors.bg} ${colors.border} ${colors.text}
                          ${isSelected ? 'ring-2 ring-white/30 scale-105 shadow-lg' : ''}
                          ${canClick ? 'hover:scale-105 hover:shadow-md cursor-pointer' : 'cursor-default opacity-70'}
                        `}
                      >
                        {node.label}
                        {node.disruptable && (
                          <span className="ml-1.5 text-[10px] opacity-60">✦</span>
                        )}
                      </button>
                    )
                  })}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Bottom hint */}
      <div className="px-4 py-2 border-t border-border text-xs text-muted text-center">
        ✦ = disruptable node &nbsp;·&nbsp; Full interactive graph with all nodes loads in Phase 2
      </div>
    </div>
  )
}
