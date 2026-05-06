import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useToast } from '../../context/ToastContext'

// ── Risk / status helpers ──────────────────────────────────────────────────────

const RISK_STYLES = {
  HIGH_RISK:   'bg-risk-high/20 text-risk-high border-risk-high',
  MEDIUM_RISK: 'bg-risk-medium/20 text-risk-medium border-risk-medium',
  LOW_RISK:    'bg-risk-low/20 text-risk-low border-risk-low',
  NO_RISK:     'bg-surfaceHigh text-muted border-border',
}

const RISK_LABELS = {
  HIGH_RISK: 'High Risk', MEDIUM_RISK: 'Med Risk',
  LOW_RISK: 'Low Risk',   NO_RISK: 'No Risk',
}

const COV_STYLES = {
  FULL:    'bg-coverage-full/20 text-coverage-full',
  PARTIAL: 'bg-coverage-partial/20 text-coverage-partial',
  ZERO:    'bg-coverage-zero/20 text-coverage-zero',
  NONE:    'bg-surfaceHigh text-muted',
}

// ── Hospital coverage table ────────────────────────────────────────────────────

// Build a lookup: hospital_id → fastest delivery_days across all option_a allocations
function buildDeliveryLookup(proc) {
  const lookup = {}
  const options = [...(proc?.option_a || []), ...(proc?.option_b || [])]
  for (const order of options) {
    for (const alloc of order.hospital_allocations || []) {
      const hid  = alloc.hospital_id
      const days = alloc.delivery_days
      if (days != null && (lookup[hid] == null || days < lookup[hid])) {
        lookup[hid] = days
      }
    }
  }
  return lookup
}

function HospitalTable({ rows, proc, recoveryDays }) {
  if (!rows || rows.length === 0) return <p className="text-xs text-muted">No hospital data.</p>

  const deliveryLookup = buildDeliveryLookup(proc)

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr className="border-b border-border">
            <th className="text-left text-muted font-medium pb-1.5 pr-3">Hospital</th>
            <th className="text-right text-muted font-medium pb-1.5 px-2">Stock Lasts</th>
            <th className="text-right text-muted font-medium pb-1.5 px-2">Exposed</th>
            <th className="text-right text-muted font-medium pb-1.5 px-2">Arrives In</th>
            <th className="text-center text-muted font-medium pb-1.5 pl-2">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(h => {
            const stockLasts   = h.days_until_stockout
            const exposed      = recoveryDays != null && stockLasts != null
              ? Math.max(0, recoveryDays - stockLasts)
              : null
            const deliveryDays = deliveryLookup[h.hospital_id]
            const arrivesLate  = deliveryDays != null && stockLasts != null && deliveryDays > stockLasts

            return (
              <tr key={h.hospital_id} className="border-b border-border/50 last:border-0">
                <td className="py-1.5 pr-3">
                  <p className="text-slate-300 font-medium">{h.hospital_name || h.hospital_id}</p>
                  <p className="text-muted">{h.hospital_id}</p>
                </td>
                {/* Days until hospital runs out of stock */}
                <td className="text-right px-2 py-1.5">
                  <span className={stockLasts != null && stockLasts < 14 ? 'text-risk-high' : 'text-slate-300'}>
                    {stockLasts != null ? `${stockLasts}d` : '—'}
                  </span>
                </td>
                {/* Days without supply after stockout before factory recovers */}
                <td className="text-right px-2 py-1.5">
                  {exposed == null ? (
                    <span className="text-muted">—</span>
                  ) : exposed === 0 ? (
                    <span className="text-coverage-full">0d</span>
                  ) : (
                    <span className="text-risk-high font-medium">{exposed}d</span>
                  )}
                </td>
                {/* How fast the assigned distributor can deliver */}
                <td className="text-right px-2 py-1.5">
                  <span className={arrivesLate ? 'text-risk-high' : 'text-coverage-full'}>
                    {deliveryDays != null ? `${deliveryDays}d` : '—'}
                  </span>
                </td>
                <td className="text-center pl-2 py-1.5">
                  <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${COV_STYLES[h.coverage_status] || COV_STYLES.NONE}`}>
                    {h.coverage_status || '—'}
                  </span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// ── Single procurement option block ───────────────────────────────────────────

function OptionBlock({ label, entries, highlighted }) {
  if (!entries || entries.length === 0) return null

  return (
    <div className={`rounded-lg border p-3 flex flex-col gap-2
      ${highlighted ? 'border-factory/60 bg-factory/5' : 'border-border bg-surfaceHigh'}`}>
      <p className={`text-xs font-semibold uppercase tracking-wider
        ${highlighted ? 'text-factory' : 'text-slate-400'}`}>
        {label} {highlighted && '★'}
      </p>

      {entries.map((e, i) => (
        <div key={i} className="flex flex-col gap-1.5 pt-1.5 border-t border-border/60 first:border-0 first:pt-0">
          <div className="flex items-start justify-between gap-2">
            <div>
              <p className="text-sm text-slate-200 font-medium">{e.distributor_name || e.distributor_id}</p>
              <p className="text-xs text-muted">{e.distributor_id} — {e.distributor_city}</p>
            </div>
            <div className="text-right shrink-0">
              <p className="text-sm text-slate-200 font-medium">{(e.total_quantity || 0).toLocaleString()} units</p>
              {e.distributor_caveat && (
                <p className="text-xs text-yellow-400">{e.distributor_caveat}</p>
              )}
            </div>
          </div>

          {e.hospital_allocations && e.hospital_allocations.length > 0 && (
            <div className="pl-2 border-l-2 border-border space-y-0.5">
              {e.hospital_allocations.map((a, j) => (
                <div key={j} className="flex justify-between text-xs text-muted">
                  <span>{a.hospital_name || a.hospital_id}</span>
                  <span className="text-slate-400">{(a.units_allocated || 0).toLocaleString()} units</span>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

// ── Clinical block ─────────────────────────────────────────────────────────────

function ClinicalBlock({ clinical }) {
  if (!clinical || !Object.keys(clinical).length) return null

  return (
    <div className="rounded-lg border border-drug/40 bg-drug/5 p-3 flex flex-col gap-2">
      <p className="text-xs font-semibold text-drug uppercase tracking-wider">Clinical Guidance</p>

      {clinical.recommendation && (
        <p className="text-sm text-slate-300 leading-relaxed">{clinical.recommendation}</p>
      )}

      {clinical.recommended_alt_name && (
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted">Substitute:</span>
          <span className="text-xs font-medium text-slate-200">{clinical.recommended_alt_name}</span>
          {clinical.requires_physician_approval && (
            <span className="text-xs px-1.5 py-0.5 rounded bg-yellow-900/40 border border-yellow-700 text-yellow-300">
              Physician sign-off required
            </span>
          )}
        </div>
      )}

      {clinical.rationale && (
        <p className="text-xs text-muted leading-relaxed border-t border-border/60 pt-2">
          {clinical.rationale}
        </p>
      )}

      {clinical.clinical_notes && (
        <p className="text-xs text-muted leading-relaxed">{clinical.clinical_notes}</p>
      )}
    </div>
  )
}

// ── Section header ─────────────────────────────────────────────────────────────

function Section({ title, children }) {
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs font-semibold uppercase tracking-wider text-muted border-b border-border pb-1">
        {title}
      </p>
      {children}
    </div>
  )
}

// ── Main drawer ───────────────────────────────────────────────────────────────

export default function PackageDetailDrawer({ packageId, onClose }) {
  const queryClient = useQueryClient()
  const { addToast } = useToast()

  const { data: detail, isLoading, isError } = useQuery({
    queryKey: ['package-detail', packageId],
    queryFn:  () => fetch(`/api/packages/${packageId}`).then(r => r.json()),
    enabled:  !!packageId,
    staleTime: 0,
  })

  const mutation = useMutation({
    mutationFn: (action) =>
      fetch(`/api/packages/${packageId}/action`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ action }),
      }).then(r => {
        if (!r.ok) throw new Error('Action failed')
        return r.json()
      }),
    onSuccess: (_, action) => {
      queryClient.invalidateQueries({ queryKey: ['packages'] })
      queryClient.invalidateQueries({ queryKey: ['package-detail', packageId] })
      queryClient.invalidateQueries({ queryKey: ['stats'] })
      const label = action === 'reject' ? 'Rejected' : 'Approved'
      addToast(`${label}: ${detail?.drug_name || packageId}`, action === 'reject' ? 'error' : 'success')
    },
    onError: () => {
      addToast('Action failed — please try again.', 'error')
    },
  })

  const status    = detail?.status
  const isDone    = status === 'approved' || status === 'rejected'
  const riskStyle = RISK_STYLES[detail?.overall_risk_level] || RISK_STYLES.NO_RISK
  const riskLabel = RISK_LABELS[detail?.overall_risk_level] || '—'

  const proc    = detail?.procurement || {}
  const hasOptB = proc.option_b && proc.option_b.length > 0

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/60 z-40"
        onClick={onClose}
      />

      {/* Drawer */}
      <div className="fixed right-0 top-0 h-full w-[560px] max-w-full bg-surface border-l border-border
                      z-50 flex flex-col shadow-2xl">

        {/* Header */}
        <div className="flex items-start justify-between px-5 py-4 border-b border-border shrink-0">
          <div className="flex-1 min-w-0 pr-4">
            <div className="flex items-center gap-2 flex-wrap">
              <p className="text-base font-semibold text-slate-200">{detail?.drug_name || '…'}</p>
              {detail && (
                <span className={`text-xs px-2 py-0.5 rounded border ${riskStyle}`}>
                  {riskLabel}
                </span>
              )}
              {proc.is_dicey_case && (
                <span className="text-xs px-1.5 py-0.5 rounded bg-yellow-900/40 border border-yellow-700 text-yellow-300">
                  ⚠ Dicey
                </span>
              )}
            </div>
            <p className="text-xs text-muted mt-0.5">{detail?.drug_id}</p>
          </div>
          <button
            onClick={onClose}
            className="text-muted hover:text-slate-300 text-xl leading-none mt-0.5 shrink-0"
          >
            ✕
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-5 flex flex-col gap-5">
          {isLoading && (
            <div className="flex items-center justify-center h-40 text-muted text-sm">
              Loading…
            </div>
          )}

          {isError && (
            <div className="px-3 py-2 rounded bg-red-950 border border-red-800 text-red-300 text-sm">
              Failed to load package details.
            </div>
          )}

          {detail && (
            <>
              {/* Summary row */}
              <div className="grid grid-cols-4 gap-3">
                <div className="rounded-lg bg-surfaceHigh border border-border p-2.5 text-center">
                  <p className="text-xs text-muted mb-0.5">Status</p>
                  <p className={`text-sm font-semibold
                    ${status === 'approved' ? 'text-emerald-400'
                    : status === 'rejected' ? 'text-red-400'
                    : 'text-yellow-400'}`}>
                    {status === 'pending_review' ? 'Pending' : status === 'approved' ? 'Approved' : 'Rejected'}
                  </p>
                </div>
                <div className="rounded-lg bg-surfaceHigh border border-border p-2.5 text-center">
                  <p className="text-xs text-muted mb-0.5">Disruption</p>
                  <p className="text-sm font-semibold text-risk-high">
                    {detail.recovery_days != null ? `~${detail.recovery_days}d` : '—'}
                  </p>
                </div>
                <div className="rounded-lg bg-surfaceHigh border border-border p-2.5 text-center">
                  <p className="text-xs text-muted mb-0.5">Stock Gap</p>
                  <p className="text-sm font-semibold text-slate-200">
                    {proc.total_stock_gap != null ? proc.total_stock_gap.toLocaleString() : '—'}
                  </p>
                </div>
                <div className="rounded-lg bg-surfaceHigh border border-border p-2.5 text-center">
                  <p className="text-xs text-muted mb-0.5">Hospitals</p>
                  <p className="text-sm font-semibold text-slate-200">
                    {(detail.hospital_coverage || []).length}
                  </p>
                </div>
              </div>

              {/* Action summary */}
              {detail.action_summary && (
                <Section title="Action Summary">
                  <p className="text-sm text-slate-300 leading-relaxed">{detail.action_summary}</p>
                </Section>
              )}

              {/* Hospital coverage */}
              <Section title="Hospital Coverage">
                <HospitalTable rows={detail.hospital_coverage} proc={proc} recoveryDays={detail.recovery_days} />
              </Section>

              {/* Procurement options */}
              {(proc.option_a || proc.option_b) && (
                <Section title="Procurement Options">
                  <OptionBlock label="Option A" entries={proc.option_a} highlighted={!hasOptB} />
                  {hasOptB && (
                    <OptionBlock label="Option B" entries={proc.option_b} highlighted={false} />
                  )}
                  {proc.procurement_notes && (
                    <p className="text-xs text-muted leading-relaxed border-t border-border pt-2">
                      {proc.procurement_notes}
                    </p>
                  )}
                </Section>
              )}

              {/* Clinical */}
              {detail.clinical && Object.keys(detail.clinical).length > 0 && (
                <Section title="Clinical Guidance">
                  <ClinicalBlock clinical={detail.clinical} />
                </Section>
              )}

              {/* Mutation error */}
              {mutation.isError && (
                <div className="px-3 py-2 rounded bg-red-950 border border-red-800 text-red-300 text-sm">
                  Action failed. Please try again.
                </div>
              )}
            </>
          )}
        </div>

        {/* Sticky footer */}
        {detail && !isDone && (
          <div className="shrink-0 px-5 py-4 border-t border-border bg-surface flex gap-2">
            <button
              onClick={() => mutation.mutate('approve_a')}
              disabled={mutation.isPending}
              className="flex-1 py-2.5 text-sm font-semibold rounded bg-emerald-700 hover:bg-emerald-600
                         text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {mutation.isPending ? '…' : 'Approve A'}
            </button>

            {hasOptB && (
              <button
                onClick={() => mutation.mutate('approve_b')}
                disabled={mutation.isPending}
                className="flex-1 py-2.5 text-sm font-semibold rounded bg-teal-700 hover:bg-teal-600
                           text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {mutation.isPending ? '…' : 'Approve B'}
              </button>
            )}

            <button
              onClick={() => mutation.mutate('reject')}
              disabled={mutation.isPending}
              className="flex-1 py-2.5 text-sm font-semibold rounded bg-red-900 hover:bg-red-800
                         text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {mutation.isPending ? '…' : 'Reject'}
            </button>
          </div>
        )}

        {detail && isDone && (
          <div className={`shrink-0 px-5 py-4 border-t border-border text-center text-sm font-medium
            ${status === 'approved' ? 'text-emerald-400' : 'text-red-400'}`}>
            Package {status === 'approved' ? 'approved ✓' : 'rejected ✕'} — no further action.
          </div>
        )}
      </div>
    </>
  )
}
