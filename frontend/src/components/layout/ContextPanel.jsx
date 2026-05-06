import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useApp, APP_STATE } from '../../context/AppContext'

const GROUP_COLORS = {
  Factory:     { dot: 'bg-factory',     text: 'text-factory',     label: 'Factory' },
  API:         { dot: 'bg-api',         text: 'text-api',         label: 'API Ingredient' },
  Distributor: { dot: 'bg-distributor', text: 'text-distributor', label: 'Distributor' },
}

const EVENT_TYPES = {
  Factory:     ['Disaster', 'Strike', 'Supply Chain Failure'],
  Distributor: ['Logistics Failure', 'Supply Chain Failure'],
  API:         ['Raw Material Shortage', 'Supply Chain Failure', 'Disaster'],
}

const SEVERITIES = ['High', 'Medium', 'Low']

const MONTHS = [
  'January','February','March','April','May','June',
  'July','August','September','October','November','December',
]

// ── Idle ─────────────────────────────────────────────────────────────────────

function IdlePrompt() {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-4 p-6 text-center">
      <div className="w-16 h-16 rounded-full bg-surfaceHigh border border-border flex items-center justify-center">
        <span className="text-2xl">⬡</span>
      </div>
      <div>
        <p className="text-slate-300 font-medium mb-1">No Active Session</p>
        <p className="text-muted text-sm">Start a session using the button above to begin monitoring the supply chain.</p>
      </div>
    </div>
  )
}

// ── Session active, no node selected ─────────────────────────────────────────

function ReadyPrompt() {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-4 p-6 text-center">
      <div className="w-16 h-16 rounded-full bg-emerald-950 border border-emerald-700 flex items-center justify-center">
        <span className="text-2xl">⬡</span>
      </div>
      <div>
        <p className="text-slate-300 font-medium mb-1">Session Active</p>
        <p className="text-muted text-sm">
          Click a <span className="text-factory font-medium">Factory</span>,{' '}
          <span className="text-distributor font-medium">Distributor</span>, or{' '}
          <span className="text-api font-medium">API</span> node on the graph to trigger a disruption.
        </p>
      </div>
    </div>
  )
}

// ── Pipeline running ──────────────────────────────────────────────────────────

function PipelineRunning() {
  const [elapsed, setElapsed] = useState(0)

  // Correct timer using useEffect
  useEffect(() => {
    const id = setInterval(() => setElapsed(s => s + 1), 1000)
    return () => clearInterval(id)
  }, [])

  // Poll session state every 5s — shows the backend is alive and tracks depletions
  const { data: sessionState, dataUpdatedAt } = useQuery({
    queryKey: ['session-state-pipeline'],
    queryFn:  () => fetch('/api/session/state').then(r => r.json()),
    refetchInterval: 5000,
  })

  const mins = String(Math.floor(elapsed / 60)).padStart(2, '0')
  const secs = String(elapsed % 60).padStart(2, '0')
  const lastPoll = dataUpdatedAt
    ? new Date(dataUpdatedAt).toLocaleTimeString()
    : '—'

  return (
    <div className="flex flex-col items-center justify-center h-full gap-5 p-6 text-center">
      {/* Spinner */}
      <div className="relative w-14 h-14">
        <div className="absolute inset-0 rounded-full border-2 border-border" />
        <div className="absolute inset-0 rounded-full border-2 border-t-factory border-r-transparent
                        border-b-transparent border-l-transparent animate-spin" />
      </div>

      <div>
        <p className="text-slate-200 font-semibold mb-1">Pipeline Running</p>
        <p className="text-muted text-sm">
          LLM agents generating procurement recommendations.
          <br />Expect 3–10 minutes.
        </p>
      </div>

      {/* Elapsed timer */}
      <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded bg-surfaceHigh
                      border border-border text-sm font-mono text-slate-300">
        <span className="text-muted">Elapsed</span>
        {mins}:{secs}
      </div>

      {/* Poll indicator */}
      <div className="w-full space-y-2">
        <div className="flex items-center justify-between text-xs text-muted">
          <span>Polling backend every 5s</span>
          <span>{lastPoll}</span>
        </div>
        <div className="h-1 w-full bg-surfaceHigh rounded-full overflow-hidden">
          <div className="h-full bg-factory rounded-full animate-pulse" style={{ width: '60%' }} />
        </div>
        {sessionState?.active && (
          <p className="text-xs text-emerald-500">✓ Backend alive</p>
        )}
      </div>

      <p className="text-xs text-muted">Do not close this tab.</p>
    </div>
  )
}

// ── Pipeline complete ─────────────────────────────────────────────────────────

function ReviewReadyPrompt() {
  const { pipelineResult, resetToActive, setActiveTab } = useApp()

  return (
    <div className="flex flex-col items-center justify-center h-full gap-4 p-6 text-center">
      <div className="w-16 h-16 rounded-full bg-emerald-950 border border-emerald-600
                      flex items-center justify-center">
        <span className="text-2xl text-emerald-400">✓</span>
      </div>
      <div>
        <p className="text-emerald-400 font-semibold mb-1">Pipeline Complete</p>
        {pipelineResult && (
          <div className="text-sm text-muted mb-3 space-y-1">
            <p>
              <span className="text-slate-300">{pipelineResult.total_packages}</span> drug packages generated
            </p>
            <p>
              <span className="text-slate-300">{pipelineResult.actionable}</span> actionable (LLM processed)
            </p>
          </div>
        )}
        <p className="text-muted text-sm">
          Review and approve or reject each recommendation below.
        </p>
      </div>
      <div className="flex flex-col gap-2 w-full">
        <button
          onClick={() => setActiveTab('review')}
          className="w-full py-2 text-sm font-medium rounded bg-emerald-700 hover:bg-emerald-600
                     text-white transition-colors"
        >
          Open Review Queue
        </button>
        <button
          onClick={resetToActive}
          className="w-full py-2 text-sm font-medium rounded bg-surfaceHigh hover:bg-border
                     text-slate-300 transition-colors"
        >
          Run Another Disruption
        </button>
      </div>
    </div>
  )
}

// ── Disruption form ───────────────────────────────────────────────────────────

function DisruptionForm() {
  const { selectedNode, startPipeline, pipelineDone, pipelineFailed } = useApp()
  const colors      = GROUP_COLORS[selectedNode?.group] || {}
  const eventOptions = EVENT_TYPES[selectedNode?.group] || []

  const [eventType,  setEventType]  = useState(eventOptions[0] || '')
  const [severity,   setSeverity]   = useState('High')
  const [month,      setMonth]      = useState(new Date().getMonth() + 1)
  const [day,        setDay]        = useState(new Date().getDate())
  const [submitting, setSubmitting] = useState(false)
  const [error,      setError]      = useState(null)

  async function handleSubmit(e) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    startPipeline()   // switch UI to pipeline-running state

    // 15-minute abort timeout — covers worst-case pipeline duration
    const controller = new AbortController()
    const timeout    = setTimeout(() => controller.abort(), 15 * 60 * 1000)

    try {
      const res = await fetch('/api/session/run-disruption', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        signal:  controller.signal,
        body: JSON.stringify({
          node_type:  selectedNode.group,
          node_id:    selectedNode.id,
          event_type: eventType,
          severity,
          month: Number(month),
          day:   Number(day),
        }),
      })
      clearTimeout(timeout)
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Pipeline failed')
      pipelineDone(data)
    } catch (err) {
      clearTimeout(timeout)
      const msg = err.name === 'AbortError'
        ? 'Request timed out after 15 minutes.'
        : err.message
      setError(msg)
      pipelineFailed()   // revert to NODE_SELECTED so user can retry
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex flex-col h-full">
      {/* Node header */}
      <div className="px-4 py-3 border-b border-border flex items-center gap-3">
        <div className={`w-3 h-3 rounded-full ${colors.dot}`} />
        <div>
          <p className={`font-semibold text-sm ${colors.text}`}>{selectedNode?.id}</p>
          <p className="text-xs text-muted">{selectedNode?.title}</p>
        </div>
        <span className={`ml-auto text-xs px-2 py-0.5 rounded border bg-surfaceHigh ${colors.text}`}
              style={{ borderColor: 'currentColor' }}>
          {colors.label}
        </span>
      </div>

      {/* Form */}
      <form onSubmit={handleSubmit} className="flex flex-col gap-4 p-4 flex-1 overflow-y-auto">
        <p className="text-xs text-muted font-medium uppercase tracking-wider">Configure Disruption</p>

        {/* Event Type */}
        <div className="flex flex-col gap-1.5">
          <label className="text-xs text-slate-400">Event Type</label>
          <select
            value={eventType}
            onChange={e => setEventType(e.target.value)}
            className="w-full bg-surfaceHigh border border-border rounded px-3 py-2 text-sm
                       text-slate-200 focus:outline-none focus:border-slate-500"
          >
            {eventOptions.map(opt => (
              <option key={opt} value={opt}>{opt}</option>
            ))}
          </select>
        </div>

        {/* Severity */}
        <div className="flex flex-col gap-1.5">
          <label className="text-xs text-slate-400">Severity</label>
          <div className="flex gap-2">
            {SEVERITIES.map(s => (
              <button
                key={s}
                type="button"
                onClick={() => setSeverity(s)}
                className={`flex-1 py-2 text-sm rounded border transition-colors
                  ${severity === s
                    ? s === 'High'   ? 'bg-risk-high/20 border-risk-high text-risk-high'
                    : s === 'Medium' ? 'bg-risk-medium/20 border-risk-medium text-risk-medium'
                    :                  'bg-risk-low/20 border-risk-low text-risk-low'
                    : 'bg-surfaceHigh border-border text-muted hover:border-slate-500'
                  }`}
              >
                {s}
              </button>
            ))}
          </div>
        </div>

        {/* Date */}
        <div className="flex flex-col gap-1.5">
          <label className="text-xs text-slate-400">Disruption Date</label>
          <div className="flex gap-2">
            <select
              value={month}
              onChange={e => setMonth(e.target.value)}
              className="flex-1 bg-surfaceHigh border border-border rounded px-3 py-2 text-sm
                         text-slate-200 focus:outline-none focus:border-slate-500"
            >
              {MONTHS.map((m, i) => (
                <option key={m} value={i + 1}>{m}</option>
              ))}
            </select>
            <input
              type="number"
              min={1} max={31}
              value={day}
              onChange={e => setDay(e.target.value)}
              className="w-20 bg-surfaceHigh border border-border rounded px-3 py-2 text-sm
                         text-slate-200 focus:outline-none focus:border-slate-500 text-center"
            />
          </div>
          <p className="text-xs text-muted">Year is auto-set — Prophet uses month/day for seasonality.</p>
        </div>

        {error && (
          <div className="px-3 py-2 rounded bg-red-950 border border-red-800 text-red-300 text-sm">
            {error}
          </div>
        )}

        <div className="px-3 py-2 rounded bg-yellow-950 border border-yellow-800 text-yellow-300 text-xs">
          ⚠ Expect 3–10 minutes. Do not close the browser tab.
        </div>

        <button
          type="submit"
          disabled={submitting}
          className="mt-auto w-full py-2.5 text-sm font-semibold rounded bg-factory
                     hover:bg-orange-500 text-white transition-colors
                     disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {submitting ? 'Submitting…' : 'Run Disruption'}
        </button>
      </form>
    </div>
  )
}

// ── Root ──────────────────────────────────────────────────────────────────────

export default function ContextPanel() {
  const { appState } = useApp()

  return (
    <div className="h-full flex flex-col bg-surface border-l border-border">
      <div className="px-4 py-2.5 border-b border-border shrink-0">
        <span className="text-xs font-medium text-muted uppercase tracking-wider">
          {appState === APP_STATE.NODE_SELECTED    ? 'Disruption Config'
         : appState === APP_STATE.PIPELINE_RUNNING ? 'Pipeline Status'
         : appState === APP_STATE.REVIEW_READY     ? 'Pipeline Complete'
         : 'Control Panel'}
        </span>
      </div>

      <div className="flex-1 overflow-hidden">
        {appState === APP_STATE.IDLE             && <IdlePrompt />}
        {appState === APP_STATE.SESSION_ACTIVE   && <ReadyPrompt />}
        {appState === APP_STATE.NODE_SELECTED    && <DisruptionForm />}
        {appState === APP_STATE.PIPELINE_RUNNING && <PipelineRunning />}
        {appState === APP_STATE.REVIEW_READY     && <ReviewReadyPrompt />}
      </div>
    </div>
  )
}
