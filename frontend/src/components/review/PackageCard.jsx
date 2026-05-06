const RISK_STYLES = {
  HIGH_RISK:   { badge: 'bg-risk-high/20 text-risk-high border-risk-high',     dot: 'bg-risk-high',   label: 'High Risk' },
  MEDIUM_RISK: { badge: 'bg-risk-medium/20 text-risk-medium border-risk-medium', dot: 'bg-risk-medium', label: 'Med Risk' },
  LOW_RISK:    { badge: 'bg-risk-low/20 text-risk-low border-risk-low',         dot: 'bg-risk-low',   label: 'Low Risk' },
  NO_RISK:     { badge: 'bg-surfaceHigh text-muted border-border',              dot: 'bg-muted',      label: 'No Risk' },
}

const STATUS_STYLES = {
  pending_review: 'text-yellow-400',
  approved:       'text-emerald-400',
  rejected:       'text-red-400',
}

const STATUS_LABEL = {
  pending_review: 'Pending',
  approved:       'Approved',
  rejected:       'Rejected',
}

export default function PackageCard({ pkg, onReview }) {
  const risk   = RISK_STYLES[pkg.risk_level] || RISK_STYLES.NO_RISK
  const status = STATUS_LABEL[pkg.status] || pkg.status
  const sc     = STATUS_STYLES[pkg.status] || 'text-muted'

  const { full = 0, partial = 0, zero = 0 } = pkg.coverage || {}

  return (
    <div className="bg-surfaceHigh border border-border rounded-lg p-3 flex flex-col gap-2
                    hover:border-slate-500 transition-colors">
      {/* Top row: drug name + risk + status */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <p className="text-sm font-semibold text-slate-200 truncate">{pkg.drug_name}</p>
          <p className="text-xs text-muted truncate">{pkg.drug_id}</p>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {pkg.is_dicey && (
            <span className="text-xs px-1.5 py-0.5 rounded bg-yellow-900/40 border border-yellow-700
                             text-yellow-300">⚠ Dicey</span>
          )}
          <span className={`text-xs px-2 py-0.5 rounded border ${risk.badge}`}>
            {risk.label}
          </span>
        </div>
      </div>

      {/* Coverage pills */}
      <div className="flex items-center gap-1.5">
        <span className="text-xs text-muted">Coverage:</span>
        {full > 0 && (
          <span className="text-xs px-1.5 py-0.5 rounded bg-coverage-full/20 text-coverage-full border border-coverage-full/40">
            {full} Full
          </span>
        )}
        {partial > 0 && (
          <span className="text-xs px-1.5 py-0.5 rounded bg-coverage-partial/20 text-coverage-partial border border-coverage-partial/40">
            {partial} Partial
          </span>
        )}
        {zero > 0 && (
          <span className="text-xs px-1.5 py-0.5 rounded bg-coverage-zero/20 text-coverage-zero border border-coverage-zero/40">
            {zero} Zero
          </span>
        )}
        {full === 0 && partial === 0 && zero === 0 && (
          <span className="text-xs text-muted">—</span>
        )}
      </div>

      {/* Shortage days */}
      {(pkg.max_shortage_days > 0 || pkg.affected_hospitals > 0) && (
        <div className="flex items-center gap-2 text-xs">
          <span className="text-muted">Shortage:</span>
          <span className="font-medium text-coverage-zero">
            {pkg.max_shortage_days > 0 ? `up to ${pkg.max_shortage_days}d` : '—'}
          </span>
          {pkg.affected_hospitals > 0 && (
            <span className="text-muted">
              across {pkg.affected_hospitals} hospital{pkg.affected_hospitals !== 1 ? 's' : ''}
            </span>
          )}
        </div>
      )}

      {/* Action summary */}
      {pkg.action_summary && (
        <p className="text-xs text-slate-400 line-clamp-2 leading-relaxed">
          {pkg.action_summary}
        </p>
      )}

      {/* Bottom row: status + review button */}
      <div className="flex items-center justify-between pt-1">
        <span className={`text-xs font-medium ${sc}`}>{status}</span>
        <button
          onClick={() => onReview(pkg.package_id)}
          className="text-xs px-3 py-1 rounded bg-factory/10 hover:bg-factory/20
                     border border-factory/40 hover:border-factory text-factory
                     transition-colors font-medium"
        >
          Review →
        </button>
      </div>
    </div>
  )
}
