import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import {
  ResponsiveContainer, LineChart, Line,
  XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine, Legend,
} from 'recharts'
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
  ALLOCATED: 'bg-coverage-full/20 text-coverage-full',
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
            <th className="text-right text-muted font-medium pb-1.5 px-2">Need</th>
            <th className="text-right text-muted font-medium pb-1.5 px-2">Get</th>
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
                {/* Units required (bridge need) */}
                <td className="text-right px-2 py-1.5">
                  <span className="text-slate-400">
                    {h.units_required != null ? h.units_required.toLocaleString() : '—'}
                  </span>
                </td>
                {/* Units acquired from procurement */}
                <td className="text-right px-2 py-1.5">
                  <span className={
                    h.units_acquired == null ? 'text-muted'
                    : h.units_acquired >= (h.units_required || 0) ? 'text-coverage-full'
                    : h.units_acquired > 0 ? 'text-coverage-partial'
                    : 'text-coverage-zero'
                  }>
                    {h.units_acquired != null ? h.units_acquired.toLocaleString() : '—'}
                  </span>
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

// ── Agent reasoning collapsible ───────────────────────────────────────────────

function AgentReasoning({ summary }) {
  const [open, setOpen] = useState(false)
  if (!summary) return null

  return (
    <div className="rounded-lg border border-border bg-surfaceHigh">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-3 py-2 text-xs font-semibold
                   uppercase tracking-wider text-muted hover:text-slate-300 transition-colors"
      >
        <span>Agent Reasoning</span>
        <span className="text-base leading-none">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="px-3 pb-3 border-t border-border/60 pt-2">
          <p className="text-sm text-slate-300 leading-relaxed whitespace-pre-wrap">{summary}</p>
        </div>
      )}
    </div>
  )
}

// ── Single procurement option block ───────────────────────────────────────────

function OptionBlock({ label, entries, highlighted, caveats }) {
  const [caveatsOpen, setCaveatsOpen] = useState(false)
  const hasCaveats = Array.isArray(caveats) && caveats.length > 0

  if (!entries || entries.length === 0) return null

  return (
    <div className={`rounded-lg border p-3 flex flex-col gap-2
      ${highlighted ? 'border-factory/60 bg-factory/5' : 'border-border bg-surfaceHigh'}`}>
      <div className="flex items-center gap-2">
        <p className={`text-xs font-semibold uppercase tracking-wider flex-1
          ${highlighted ? 'text-factory' : 'text-slate-400'}`}>
          {label} {highlighted && '★'}
        </p>
        {hasCaveats && (
          <button
            onClick={() => setCaveatsOpen(o => !o)}
            className={`flex items-center gap-1 text-xs font-medium px-1.5 py-0.5 rounded border
                        transition-colors
                        ${caveatsOpen
                          ? 'bg-yellow-900/40 border-yellow-700/80 text-yellow-300'
                          : 'bg-yellow-900/20 border-yellow-700/50 text-yellow-400 hover:bg-yellow-900/40 hover:border-yellow-700/80'}`}
          >
            <span>⚠</span>
            <span>{caveats.length} caveat{caveats.length !== 1 ? 's' : ''}</span>
          </button>
        )}
      </div>

      {hasCaveats && caveatsOpen && (
        <ul className="flex flex-col gap-1 rounded border border-yellow-700/40 bg-yellow-900/10 px-2.5 py-2">
          {caveats.map((c, i) => (
            <li key={i} className="flex items-start gap-2 text-xs text-yellow-200/80">
              <span className="shrink-0 mt-0.5 text-yellow-500">•</span>
              <span>{c}</span>
            </li>
          ))}
        </ul>
      )}

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

// ── Outcome simulation chart ──────────────────────────────────────────────────

const H_COLORS = ['#60a5fa', '#34d399', '#fbbf24', '#a78bfa', '#fb7185']

// Tooltip shows only hospitals that are actually changing near this day
function OutcomeTooltip({ active, payload, label, isReject }) {
  if (!active || !payload?.length) return null

  // Group by hospital — pair up approved + rejected per hospital
  const byHospital = {}
  for (const p of payload) {
    const key  = p.dataKey   // e.g. "H008_a" or "H008_r"
    const hid  = key.slice(0, -2)
    const kind = key.slice(-1)   // 'a' = approved, 'r' = rejected
    byHospital[hid] = byHospital[hid] || { color: p.color, a: null, r: null }
    byHospital[hid][kind] = p.value ?? 0
  }

  const entries = Object.entries(byHospital)
    .filter(([, v]) => v.a !== v.r || v.a === 0)   // only show where lines differ or both zero

  if (!entries.length) return null

  return (
    <div className="rounded border border-border bg-surface px-2.5 py-2 text-xs shadow-lg min-w-[160px]">
      <p className="text-muted mb-1.5 font-medium">Day {label}</p>
      {entries.map(([hid, v]) => (
        <div key={hid} className="flex items-center justify-between gap-3 mb-0.5">
          <div className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full" style={{ background: v.color }} />
            <span style={{ color: v.color }}>{hid}</span>
          </div>
          <div className="flex gap-2 text-right">
            <span className="text-slate-300">
              {isReject ? v.r?.toLocaleString() : v.a?.toLocaleString()}
            </span>
            {v.a !== v.r && (
              <span className={v.a > v.r ? 'text-emerald-400' : 'text-slate-500'}>
                {isReject
                  ? `(+${(v.a - v.r)?.toLocaleString()} if ordered)`
                  : `(−${(v.r - v.a === 0 ? 0 : v.r - v.a)?.toLocaleString()} w/o)`}
              </span>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

function OutcomeChart({ packageId, proc, recoveryDays, resolvedAction }) {
  const hasOptB = proc?.option_b && proc.option_b.length > 0

  // Pre-select the actual resolved action if the package is already decided
  const defaultAction = resolvedAction ?? 'approve_a'
  const [action, setAction] = useState(defaultAction)

  const isReject = action === 'reject'

  const { data, isLoading, isError } = useQuery({
    queryKey: ['outcome', packageId, action],
    queryFn:  () =>
      fetch(`/api/packages/${packageId}/outcome?action=${action}`)
        .then(r => { if (!r.ok) throw new Error(); return r.json() }),
    enabled:  !!packageId,
    staleTime: 60_000,
  })

  const hospitals = (data?.hospitals || []).slice(0, 5)
  const summary   = data?.summary   || {}

  // Build flat Recharts dataset: [{day, H001_a, H001_r, H002_a, H002_r, ...}]
  const chartData = []
  if (hospitals.length > 0) {
    const len = hospitals[0].trajectory_approved.length
    for (let i = 0; i < len; i++) {
      const pt = { day: i }
      hospitals.forEach(h => {
        pt[`${h.hospital_id}_a`] = h.trajectory_approved[i]?.stock ?? 0
        pt[`${h.hospital_id}_r`] = h.trajectory_rejected[i]?.stock ?? 0
      })
      chartData.push(pt)
    }
  }

  const saved      = summary.total_hospital_days_saved || 0
  const protected_ = summary.hospitals_protected       || 0
  const atRisk     = summary.hospitals_still_at_risk   || 0
  const showDots   = recoveryDays <= 10   // short window — show dots so points are visible

  return (
    <div className="flex flex-col gap-3">

      {/* Toggle bar */}
      <div className="flex items-center gap-2 flex-wrap">
        {['approve_a', hasOptB && 'approve_b', 'reject'].filter(Boolean).map(a => (
          <button
            key={a}
            onClick={() => setAction(a)}
            className={`px-2.5 py-1 rounded text-xs font-medium border transition-colors
              ${action === a
                ? a === 'reject'
                  ? 'bg-red-900/60 border-red-700 text-red-300'
                  : 'bg-emerald-900/60 border-emerald-700 text-emerald-300'
                : 'bg-surfaceHigh border-border text-muted hover:text-slate-300'}`}
          >
            {a === 'approve_a' ? 'Option A' : a === 'approve_b' ? 'Option B' : 'If Rejected'}
          </button>
        ))}

        {/* Legend key */}
        <div className="flex items-center gap-3 ml-2 text-xs text-muted">
          <span className="flex items-center gap-1">
            <svg width="18" height="6"><line x1="0" y1="3" x2="18" y2="3" stroke="#94a3b8" strokeWidth="1.5"/></svg>
            {isReject ? 'no order' : 'with order'}
          </span>
          <span className="flex items-center gap-1">
            <svg width="18" height="6">
              <line x1="0" y1="3" x2="5" y2="3" stroke="#94a3b8" strokeWidth="1" strokeOpacity="0.5"/>
              <line x1="8" y1="3" x2="13" y2="3" stroke="#94a3b8" strokeWidth="1" strokeOpacity="0.5"/>
            </svg>
            {isReject ? 'if ordered' : 'no order'}
          </span>
        </div>

        {/* Summary pill */}
        {!isLoading && (
          <span className={`ml-auto text-xs font-medium rounded px-2 py-0.5
            ${saved > 0 ? 'text-emerald-400' : atRisk > 0 ? 'text-yellow-400' : 'text-coverage-full'}`}>
            {saved > 0
              ? `${saved}d saved · ${protected_} hospital${protected_ !== 1 ? 's' : ''} protected`
              : atRisk > 0
                ? `${atRisk} hospital${atRisk !== 1 ? 's' : ''} still exposed`
                : 'All hospitals safe'}
            {saved > 0 && atRisk > 0 && ` · ${atRisk} still exposed`}
          </span>
        )}
      </div>

      {isLoading && (
        <div className="h-44 flex items-center justify-center text-xs text-muted animate-pulse">
          Running simulation…
        </div>
      )}
      {isError && (
        <div className="text-xs text-muted italic">Simulation unavailable for this package.</div>
      )}

      {!isLoading && !isError && hospitals.length > 0 && (
        <>
          <div className="rounded-lg bg-surfaceHigh border border-border overflow-hidden">
            <ResponsiveContainer width="100%" height={196}>
              <LineChart data={chartData} margin={{ top: 12, right: 16, bottom: 8, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                <XAxis
                  dataKey="day"
                  tick={{ fontSize: 10, fill: '#64748b' }}
                  tickLine={false}
                  axisLine={{ stroke: '#334155' }}
                  label={{ value: 'days since disruption', position: 'insideBottomRight', offset: -4, fontSize: 10, fill: '#475569' }}
                />
                <YAxis
                  tick={{ fontSize: 10, fill: '#64748b' }}
                  tickLine={false}
                  axisLine={false}
                  width={46}
                  tickFormatter={v => v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v}
                />
                <Tooltip content={<OutcomeTooltip isReject={isReject} />} />

                {/* Recovery marker */}
                <ReferenceLine
                  x={recoveryDays}
                  stroke="#475569"
                  strokeDasharray="4 2"
                  label={{ value: 'factory recovers', position: 'insideTopRight', fontSize: 9, fill: '#64748b', dy: -2 }}
                />

                {hospitals.map((h, i) => {
                  const col = H_COLORS[i % H_COLORS.length]

                  // In "reject" view: the rejected path (no order) is the primary solid line
                  const primaryKey   = isReject ? `${h.hospital_id}_r` : `${h.hospital_id}_a`
                  const secondaryKey = isReject ? `${h.hospital_id}_a` : `${h.hospital_id}_r`

                  return [
                    <Line
                      key={primaryKey}
                      dataKey={primaryKey}
                      stroke={col}
                      strokeWidth={2}
                      dot={showDots ? { r: 2, fill: col, strokeWidth: 0 } : false}
                      activeDot={{ r: 3, strokeWidth: 0 }}
                      isAnimationActive={false}
                    />,
                    <Line
                      key={secondaryKey}
                      dataKey={secondaryKey}
                      stroke={col}
                      strokeWidth={1}
                      strokeDasharray="5 3"
                      strokeOpacity={0.35}
                      dot={false}
                      activeDot={false}
                      isAnimationActive={false}
                    />,
                  ]
                })}
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* Per-hospital impact rows */}
          <div className="flex flex-col gap-1">
            {hospitals.map((h, i) => {
              const col        = H_COLORS[i % H_COLORS.length]
              const daysSaved  = h.hospital_days_saved
              const soApproved = h.stockout_days_approved
              const soRejected = h.stockout_days_rejected

              return (
                <div key={h.hospital_id}
                  className="flex items-center gap-2 text-xs px-2.5 py-1.5 rounded bg-surfaceHigh border border-border/50">
                  <span className="w-2 h-2 rounded-full shrink-0" style={{ background: col }} />
                  <span className="text-slate-300 font-medium w-10 shrink-0">{h.hospital_id}</span>
                  <span className="text-muted truncate flex-1">{h.hospital_name}</span>
                  <div className="flex items-center gap-2 shrink-0 text-right">
                    {daysSaved > 0 && (
                      <span className="text-emerald-400 font-medium">+{daysSaved}d saved</span>
                    )}
                    {soApproved > 0 && (
                      <span className="text-yellow-400">{soApproved}d exposed</span>
                    )}
                    {daysSaved === 0 && soApproved === 0 && (
                      <span className="text-coverage-full">safe</span>
                    )}
                    {soRejected > 0 && soRejected !== soApproved && (
                      <span className="text-muted">({soRejected}d w/o order)</span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </>
      )}
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

  const retryMutation = useMutation({
    mutationFn: () =>
      fetch(`/api/packages/${packageId}/retry-procurement`, { method: 'POST' })
        .then(r => {
          if (!r.ok) return r.json().then(e => { throw new Error(e.detail || 'Retry failed') })
          return r.json()
        }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['packages'] })
      queryClient.invalidateQueries({ queryKey: ['package-detail', packageId] })
      queryClient.invalidateQueries({ queryKey: ['outcome', packageId] })
      queryClient.invalidateQueries({ queryKey: ['stats'] })
      addToast(`Procurement re-run for ${detail?.drug_name || packageId}`, 'success')
    },
    onError: (e) => {
      addToast(`Retry failed: ${e.message}`, 'error')
    },
  })

  const status    = detail?.status
  const isDone    = status === 'approved' || status === 'rejected'
  const riskStyle = RISK_STYLES[detail?.overall_risk_level] || RISK_STYLES.NO_RISK
  const riskLabel = RISK_LABELS[detail?.overall_risk_level] || '—'

  const proc    = detail?.procurement || {}
  const hasOptB = proc.option_b && proc.option_b.length > 0

  // Pre-select the chart view to match what was actually decided
  const resolvedAction = (() => {
    if (!isDone) return null
    if (status === 'rejected') return 'reject'
    try {
      const pa = JSON.parse(detail?.procurement_action || '{}')
      return pa.approved_order === 'option_b' ? 'approve_b' : 'approve_a'
    } catch { return 'approve_a' }
  })()

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

              {/* System-level totals row */}
              {(() => {
                const cov = detail.hospital_coverage || []
                if (cov.length === 0) return null
                const totalRequired = cov.reduce((s, h) => s + (h.units_required || 0), 0)
                const totalAcquired = cov.reduce((s, h) => s + (h.units_acquired || 0), 0)
                const coverageGap   = Math.max(0, totalRequired - totalAcquired)
                const coveragePct   = totalRequired > 0
                  ? Math.round(totalAcquired / totalRequired * 100)
                  : null
                return (
                  <div className="grid grid-cols-4 gap-2">
                    <div className="rounded-lg bg-surfaceHigh border border-border p-2.5 text-center">
                      <p className="text-xs text-muted mb-0.5">Units Required</p>
                      <p className="text-sm font-semibold text-slate-200">{totalRequired.toLocaleString()}</p>
                    </div>
                    <div className="rounded-lg bg-surfaceHigh border border-border p-2.5 text-center">
                      <p className="text-xs text-muted mb-0.5">Units Acquired</p>
                      <p className="text-sm font-semibold text-coverage-full">{totalAcquired.toLocaleString()}</p>
                    </div>
                    <div className="rounded-lg bg-surfaceHigh border border-border p-2.5 text-center">
                      <p className="text-xs text-muted mb-0.5">Coverage Gap</p>
                      <p className={`text-sm font-semibold ${coverageGap > 0 ? 'text-risk-high' : 'text-coverage-full'}`}>
                        {coverageGap.toLocaleString()}
                      </p>
                    </div>
                    <div className="rounded-lg bg-surfaceHigh border border-border p-2.5 text-center">
                      <p className="text-xs text-muted mb-0.5">System Coverage</p>
                      <p className={`text-sm font-semibold ${
                        coveragePct == null ? 'text-muted'
                        : coveragePct >= 100 ? 'text-coverage-full'
                        : coveragePct >= 50  ? 'text-coverage-partial'
                        : 'text-coverage-zero'
                      }`}>
                        {coveragePct != null ? `${coveragePct}%` : '—'}
                      </p>
                    </div>
                  </div>
                )
              })()}

              {/* Outcome simulation — first thing visible after summary cards */}
              {detail.recovery_days != null && (
                <Section title="Outcome Simulation">
                  <OutcomeChart
                    packageId={packageId}
                    proc={proc}
                    recoveryDays={detail.recovery_days}
                    resolvedAction={resolvedAction}
                  />
                </Section>
              )}

              {/* API error banner */}
              {detail.procurement?.api_error && !isDone && (
                <div className="flex items-center justify-between gap-3 px-3 py-2.5 rounded-lg
                                bg-red-950 border border-red-800">
                  <div>
                    <p className="text-sm font-medium text-red-300">Procurement agent failed — LLM API error</p>
                    <p className="text-xs text-red-400 mt-0.5">No recommendation was generated. Retry to re-run the LLM call.</p>
                  </div>
                  <button
                    onClick={() => retryMutation.mutate()}
                    disabled={retryMutation.isPending}
                    className="shrink-0 px-3 py-1.5 rounded text-xs font-semibold
                               bg-red-800 hover:bg-red-700 text-white transition-colors
                               disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {retryMutation.isPending ? 'Retrying…' : 'Retry'}
                  </button>
                </div>
              )}

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

              {/* Agent reasoning — collapsed by default */}
              {proc.recommendation_summary && (
                <AgentReasoning summary={proc.recommendation_summary} />
              )}

              {/* Procurement options */}
              {(proc.option_a || proc.option_b) && (
                <Section title="Procurement Options">
                  <OptionBlock label="Option A" entries={proc.option_a} highlighted={!hasOptB} caveats={proc.caveats} />
                  {hasOptB && (
                    <OptionBlock label="Option B" entries={proc.option_b} highlighted={false} caveats={proc.caveats} />
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
