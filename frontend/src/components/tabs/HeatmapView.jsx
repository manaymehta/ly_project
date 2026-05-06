import { useQuery } from '@tanstack/react-query'
import { useApp, APP_STATE } from '../../context/AppContext'

const COV_CELL = {
  FULL:               'bg-coverage-full/30 text-coverage-full',
  PARTIAL:            'bg-coverage-partial/30 text-coverage-partial',
  ZERO:               'bg-coverage-zero/30 text-coverage-zero',
  NONE:               'bg-coverage-zero/30 text-coverage-zero',
  COVERED_BY_FACTORY: 'bg-drug/20 text-drug',
}

const COV_LABEL = {
  FULL:               'F',
  PARTIAL:            'P',
  ZERO:               '0',
  NONE:               '0',
  COVERED_BY_FACTORY: 'C',
}

const RISK_DOT = {
  HIGH_RISK:   'bg-risk-high',
  MEDIUM_RISK: 'bg-risk-medium',
  LOW_RISK:    'bg-risk-low',
  NO_RISK:     'bg-muted',
}

export default function HeatmapView() {
  const { appState, sessionStart } = useApp()
  const hasData = appState !== APP_STATE.IDLE

  const pkgUrl = sessionStart
    ? `/api/packages?since=${encodeURIComponent(sessionStart)}`
    : '/api/packages'

  // Heatmap is built from the packages list — no separate endpoint needed
  const { data, isLoading, isError } = useQuery({
    queryKey: ['heatmap-packages', sessionStart],
    queryFn:  () => fetch(pkgUrl).then(r => r.json()),
    enabled:  hasData,
    refetchInterval: 20000,
  })

  if (!hasData) {
    return (
      <div className="flex items-center justify-center h-full text-muted text-sm">
        Start a session to view the coverage heatmap.
      </div>
    )
  }

  if (isLoading) {
    return <div className="flex items-center justify-center h-full text-muted text-sm">Loading…</div>
  }

  if (isError) {
    return <div className="flex items-center justify-center h-full text-risk-high text-sm">Failed to load heatmap.</div>
  }

  if (!data || data.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-muted text-sm">
        Run a disruption to populate the heatmap.
      </div>
    )
  }

  // Build matrix from packages — rows = drugs, columns = hospitals
  // coverage is per-drug (aggregated across hospitals from the package)
  // We use the coverage counts from each package card
  const drugs = data.map(pkg => ({
    id:       pkg.drug_id,
    name:     pkg.drug_name,
    risk:     pkg.risk_level,
    status:   pkg.status,
    full:     pkg.coverage?.full    ?? 0,
    partial:  pkg.coverage?.partial ?? 0,
    zero:     pkg.coverage?.zero    ?? 0,
    total:    pkg.total_hospitals   ?? 0,
  }))

  return (
    <div className="h-full flex flex-col gap-2">
      {/* Legend */}
      <div className="flex items-center gap-4 shrink-0 text-xs text-muted">
        <span className="font-medium text-slate-400">Coverage:</span>
        {[
          { cls: 'bg-coverage-full/30 text-coverage-full',     label: 'F = Full' },
          { cls: 'bg-coverage-partial/30 text-coverage-partial', label: 'P = Partial' },
          { cls: 'bg-coverage-zero/30 text-coverage-zero',     label: '0 = Zero/None' },
        ].map(({ cls, label }) => (
          <span key={label} className={`px-1.5 py-0.5 rounded text-xs ${cls}`}>{label}</span>
        ))}
        <span className="ml-auto text-xs text-muted">Current session only</span>
      </div>

      {/* Table */}
      <div className="flex-1 overflow-auto">
        <table className="text-xs border-collapse w-full">
          <thead className="sticky top-0 bg-surface z-10">
            <tr>
              <th className="text-left text-muted font-medium pb-1.5 pr-4 whitespace-nowrap">Drug</th>
              <th className="text-center text-muted font-medium pb-1.5 px-2">Risk</th>
              <th className="text-center text-muted font-medium pb-1.5 px-2">Status</th>
              <th className="text-center text-muted font-medium pb-1.5 px-2">Full</th>
              <th className="text-center text-muted font-medium pb-1.5 px-2">Partial</th>
              <th className="text-center text-muted font-medium pb-1.5 px-2">Zero</th>
              <th className="text-center text-muted font-medium pb-1.5 px-2">Hospitals</th>
              <th className="text-left text-muted font-medium pb-1.5 px-4">Coverage Bar</th>
            </tr>
          </thead>
          <tbody>
            {drugs.map(drug => {
              const covered   = drug.full
              const partial   = drug.partial
              const uncovered = drug.zero
              const total     = drug.total || 1
              const fullPct    = (covered  / total) * 100
              const partialPct = (partial  / total) * 100
              const zeroPct    = (uncovered / total) * 100

              return (
                <tr key={drug.id} className="border-b border-border/40 last:border-0">
                  <td className="py-1.5 pr-4 whitespace-nowrap">
                    <span className="text-slate-200 font-medium">{drug.name}</span>
                    <span className="text-muted ml-1.5">{drug.id}</span>
                  </td>
                  <td className="text-center px-2 py-1.5">
                    <span className={`inline-block w-2 h-2 rounded-full ${RISK_DOT[drug.risk] || 'bg-muted'}`} />
                  </td>
                  <td className="text-center px-2 py-1.5">
                    <span className={`text-xs font-medium
                      ${drug.status === 'approved' ? 'text-emerald-400'
                      : drug.status === 'rejected' ? 'text-red-400'
                      : 'text-yellow-400'}`}>
                      {drug.status === 'pending_review' ? 'Pending'
                       : drug.status === 'approved' ? 'Approved'
                       : drug.status === 'rejected' ? 'Rejected'
                       : '—'}
                    </span>
                  </td>
                  <td className="text-center px-2 py-1.5 text-coverage-full font-medium">{covered}</td>
                  <td className="text-center px-2 py-1.5 text-coverage-partial font-medium">{partial}</td>
                  <td className="text-center px-2 py-1.5 text-coverage-zero font-medium">{uncovered}</td>
                  <td className="text-center px-2 py-1.5 text-slate-300">{total}</td>
                  <td className="px-4 py-1.5 min-w-32">
                    <div className="flex h-3 rounded overflow-hidden bg-surfaceHigh w-full">
                      {fullPct    > 0 && <div className="bg-coverage-full"    style={{ width: `${fullPct}%` }} />}
                      {partialPct > 0 && <div className="bg-coverage-partial" style={{ width: `${partialPct}%` }} />}
                      {zeroPct    > 0 && <div className="bg-coverage-zero"    style={{ width: `${zeroPct}%` }} />}
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
