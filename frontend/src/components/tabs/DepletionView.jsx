import { useQuery } from '@tanstack/react-query'
import { useApp, APP_STATE } from '../../context/AppContext'

function StatCard({ label, value, sub, color = 'text-slate-200' }) {
  return (
    <div className="rounded-lg bg-surfaceHigh border border-border px-3 py-2 flex flex-col">
      <span className="text-xs text-muted mb-0.5">{label}</span>
      <span className={`text-lg font-bold leading-none ${color}`}>{value}</span>
      {sub && <span className="text-xs text-muted mt-0.5">{sub}</span>}
    </div>
  )
}

export default function DepletionView() {
  const { appState } = useApp()
  const isActive = appState !== APP_STATE.IDLE

  const { data, isLoading } = useQuery({
    queryKey: ['session-state-depletion'],
    queryFn:  () => fetch('/api/session/state').then(r => r.json()),
    enabled:  isActive,
    refetchInterval: 10000,
  })

  if (!isActive) {
    return (
      <div className="flex items-center justify-center h-full text-muted text-sm">
        Start a session to track distributor depletion.
      </div>
    )
  }

  if (isLoading) {
    return <div className="flex items-center justify-center h-full text-muted text-sm">Loading…</div>
  }

  const depletions = data?.depletions || []
  const restocks   = data?.restocks   || []

  if (depletions.length === 0 && restocks.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-muted text-sm">
        No depletions yet — approve a procurement package to see stock changes.
      </div>
    )
  }

  const totalDepleted  = depletions.reduce((s, r) => s + (r.depleted  || 0), 0)
  const totalRestocked = restocks.reduce((s, r)   => s + (r.restocked || 0), 0)

  return (
    <div className="h-full flex flex-col gap-3">
      {/* Summary cards */}
      <div className="flex gap-2 shrink-0">
        <StatCard
          label="Distributor Stock Depleted"
          value={totalDepleted.toLocaleString()}
          sub={`across ${depletions.length} route(s)`}
          color="text-coverage-zero"
        />
        <StatCard
          label="Hospital Units Restocked"
          value={totalRestocked.toLocaleString()}
          sub={`across ${restocks.length} hospital-drug pair(s)`}
          color="text-coverage-full"
        />
      </div>

      {/* Two-column tables */}
      <div className="flex-1 overflow-auto flex gap-4">

        {/* Distributor depletions */}
        {depletions.length > 0 && (
          <div className="flex-1 min-w-0">
            <p className="text-xs font-semibold text-muted uppercase tracking-wider mb-1.5">
              Distributor Stock Used
            </p>
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left text-muted font-medium pb-1 pr-2">Distributor</th>
                  <th className="text-left text-muted font-medium pb-1 pr-2">Drug</th>
                  <th className="text-right text-muted font-medium pb-1 pr-2">Depleted</th>
                  <th className="text-right text-muted font-medium pb-1">Remaining</th>
                </tr>
              </thead>
              <tbody>
                {depletions.map((r, i) => {
                  const pct = r.baseline_stock > 0
                    ? Math.round((r.depleted / r.baseline_stock) * 100)
                    : 0
                  return (
                    <tr key={i} className="border-b border-border/40 last:border-0">
                      <td className="py-1 pr-2 text-slate-300 font-medium">{r.distributor_id}</td>
                      <td className="py-1 pr-2 text-muted">{r.drug_id}</td>
                      <td className="py-1 pr-2 text-right text-coverage-zero font-medium">
                        −{(r.depleted || 0).toLocaleString()}
                        <span className="text-muted ml-1">({pct}%)</span>
                      </td>
                      <td className="py-1 text-right text-slate-300">
                        {(r.current_stock || 0).toLocaleString()}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* Hospital restocks */}
        {restocks.length > 0 && (
          <div className="flex-1 min-w-0">
            <p className="text-xs font-semibold text-muted uppercase tracking-wider mb-1.5">
              Hospital Inventory Restocked
            </p>
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left text-muted font-medium pb-1 pr-2">Hospital</th>
                  <th className="text-left text-muted font-medium pb-1 pr-2">Drug</th>
                  <th className="text-right text-muted font-medium pb-1">Added</th>
                </tr>
              </thead>
              <tbody>
                {restocks.map((r, i) => (
                  <tr key={i} className="border-b border-border/40 last:border-0">
                    <td className="py-1 pr-2 text-slate-300 font-medium">{r.hospital_id}</td>
                    <td className="py-1 pr-2 text-muted">{r.drug_id}</td>
                    <td className="py-1 text-right text-coverage-full font-medium">
                      +{(r.restocked || 0).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

      </div>
    </div>
  )
}
