import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useApp, APP_STATE } from '../../context/AppContext'

const GROUP_COLOR = {
  Factory:     'text-factory',
  Distributor: 'text-distributor',
  API:         'text-api',
  Drug:        'text-drug',
  Hospital:    'text-hospital',
}

function StatPill({ label, value, color = 'text-slate-300' }) {
  return (
    <div className="flex flex-col items-center px-3 border-r border-border last:border-r-0">
      <span className={`text-lg font-bold leading-none ${color}`}>{value}</span>
      <span className="text-xs text-muted mt-0.5">{label}</span>
    </div>
  )
}

export default function SessionBar() {
  const { appState, sessionStart, startSession, endSession } = useApp()
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState(null)

  const isActive = appState !== APP_STATE.IDLE

  const statsUrl = sessionStart
    ? `/api/stats?since=${encodeURIComponent(sessionStart)}`
    : '/api/stats'

  const { data: stats } = useQuery({
    queryKey: ['stats', sessionStart],
    queryFn: () => fetch(statsUrl).then(r => r.json()),
    enabled:  isActive,
    refetchInterval: 15000,
  })

  async function handleStart() {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/session/start', { method: 'POST' })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Failed to start session')
      // Fetch started_at so ReviewQueue filters to this session only
      const state = await fetch('/api/session/state').then(r => r.json())
      startSession(state.started_at || null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  async function handleEnd() {
    setLoading(true)
    try {
      await fetch('/api/session/end', { method: 'POST' })
      endSession()
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <header className="flex items-center justify-between px-5 py-2.5 bg-surface border-b border-border shrink-0">
      {/* Brand */}
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-full bg-factory" />
          <div className="w-2 h-2 rounded-full bg-drug" />
          <div className="w-2 h-2 rounded-full bg-distributor" />
        </div>
        <span className="font-semibold text-slate-200 tracking-wide text-sm">
          LY Project
        </span>
        <span className="text-muted text-xs">Pharma Supply Chain Monitor</span>
      </div>

      {/* Stats (only when session active and data available) */}
      {isActive && stats && (
        <div className="flex items-center">
          <StatPill label="Total"    value={stats.total}      />
          <StatPill label="High Risk" value={stats.high_risk}  color="text-risk-high" />
          <StatPill label="Pending"  value={stats.pending}    color="text-yellow-400" />
          <StatPill label="Dicey"    value={stats.dicey}      color="text-orange-400" />
          {stats.disruption_node && (
            <div className="ml-4 px-3 py-1 rounded bg-surfaceHigh border border-border text-xs text-muted">
              Last disruption:{' '}
              <span className="text-slate-300 font-medium">{stats.disruption_node}</span>
              {' · '}
              <span>{stats.disruption_event}</span>
              {' · '}
              <span className={
                stats.disruption_severity === 'High' ? 'text-risk-high' :
                stats.disruption_severity === 'Medium' ? 'text-risk-medium' : 'text-risk-low'
              }>{stats.disruption_severity}</span>
            </div>
          )}
        </div>
      )}

      {/* Session Control */}
      <div className="flex items-center gap-3">
        {error && (
          <span className="text-xs text-risk-high">{error}</span>
        )}

        {/* Status badge */}
        <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-xs font-medium
          ${isActive
            ? 'bg-emerald-950 border-emerald-700 text-emerald-400'
            : 'bg-slate-900 border-border text-muted'
          }`}>
          <div className={`w-1.5 h-1.5 rounded-full ${isActive ? 'bg-emerald-400 animate-pulse' : 'bg-muted'}`} />
          {isActive ? 'Session Active' : 'No Session'}
        </div>

        {!isActive ? (
          <button
            onClick={handleStart}
            disabled={loading}
            className="px-4 py-1.5 text-sm font-medium rounded bg-emerald-600 hover:bg-emerald-500
                       text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? 'Starting…' : 'Start Session'}
          </button>
        ) : (
          <button
            onClick={handleEnd}
            disabled={loading || appState === APP_STATE.PIPELINE_RUNNING}
            className="px-4 py-1.5 text-sm font-medium rounded bg-slate-700 hover:bg-slate-600
                       text-slate-200 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? 'Ending…' : 'End Session'}
          </button>
        )}
      </div>
    </header>
  )
}
