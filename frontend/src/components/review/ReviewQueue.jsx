import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useApp, APP_STATE } from '../../context/AppContext'
import PackageCard from './PackageCard'
import PackageDetailDrawer from './PackageDetailDrawer'

const FILTERS = [
  { id: 'all',      label: 'All' },
  { id: 'pending',  label: 'Pending' },
  { id: 'approved', label: 'Approved' },
  { id: 'rejected', label: 'Rejected' },
]

const STATUS_MAP = {
  pending:  'pending_review',
  approved: 'approved',
  rejected: 'rejected',
}

export default function ReviewQueue() {
  const { appState } = useApp()
  const hasData = appState !== APP_STATE.IDLE

  const [filter,         setFilter]         = useState('all')
  const [openPackageId,  setOpenPackageId]  = useState(null)

  const { sessionStart } = useApp()

  const packageUrl = sessionStart
    ? `/api/packages?since=${encodeURIComponent(sessionStart)}`
    : '/api/packages'

  const { data: packages = [], isLoading, isError, refetch } = useQuery({
    queryKey: ['packages', sessionStart],
    queryFn:  () => fetch(packageUrl).then(r => r.json()),
    enabled:  hasData,
    refetchInterval: appState === APP_STATE.REVIEW_READY ? 15000 : false,
  })

  const visible = filter === 'all'
    ? packages
    : packages.filter(p => p.status === STATUS_MAP[filter])

  const counts = {
    all:      packages.length,
    pending:  packages.filter(p => p.status === 'pending_review').length,
    approved: packages.filter(p => p.status === 'approved').length,
    rejected: packages.filter(p => p.status === 'rejected').length,
  }

  if (!hasData) {
    return (
      <div className="flex items-center justify-center h-full text-muted text-sm">
        Start a session to view review packages.
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      {/* Filter bar */}
      <div className="flex items-center gap-1 mb-3 shrink-0">
        {FILTERS.map(f => (
          <button
            key={f.id}
            onClick={() => setFilter(f.id)}
            className={`flex items-center gap-1.5 px-3 py-1 rounded text-xs font-medium
                        transition-colors border
              ${filter === f.id
                ? 'bg-factory/20 border-factory text-factory'
                : 'bg-surfaceHigh border-border text-muted hover:text-slate-300 hover:border-slate-500'
              }`}
          >
            {f.label}
            <span className={`rounded-full px-1.5 py-0.5 text-xs leading-none
              ${filter === f.id ? 'bg-factory/30 text-factory' : 'bg-border text-muted'}`}>
              {counts[f.id]}
            </span>
          </button>
        ))}

        <button
          onClick={() => refetch()}
          className="ml-auto text-xs text-muted hover:text-slate-300 transition-colors px-2 py-1
                     rounded border border-border hover:border-slate-500"
          title="Refresh"
        >
          ↻
        </button>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto">
        {isLoading && (
          <div className="flex items-center justify-center h-24 text-muted text-sm">
            Loading packages…
          </div>
        )}

        {isError && (
          <div className="px-3 py-2 rounded bg-red-950 border border-red-800 text-red-300 text-sm">
            Failed to load packages.
          </div>
        )}

        {!isLoading && !isError && visible.length === 0 && (
          <div className="flex items-center justify-center h-24 text-muted text-sm">
            {filter === 'all'
              ? 'No packages yet — run a disruption to generate review items.'
              : `No ${filter} packages.`}
          </div>
        )}

        <div className="flex flex-col gap-2 pr-1">
          {visible.map(pkg => (
            <PackageCard
              key={pkg.package_id}
              pkg={pkg}
              onReview={id => setOpenPackageId(id)}
            />
          ))}
        </div>
      </div>

      {/* Detail drawer */}
      {openPackageId && (
        <PackageDetailDrawer
          packageId={openPackageId}
          onClose={() => setOpenPackageId(null)}
        />
      )}
    </div>
  )
}
