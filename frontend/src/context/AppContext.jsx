import { createContext, useContext, useState, useEffect } from 'react'

// App state machine
// IDLE → SESSION_ACTIVE → NODE_SELECTED → PIPELINE_RUNNING → REVIEW_READY
// REVIEW_READY → NODE_SELECTED (run another disruption)
// any → IDLE (end session)

export const APP_STATE = {
  IDLE:             'IDLE',
  SESSION_ACTIVE:   'SESSION_ACTIVE',
  NODE_SELECTED:    'NODE_SELECTED',
  PIPELINE_RUNNING: 'PIPELINE_RUNNING',
  REVIEW_READY:     'REVIEW_READY',
}

const AppContext = createContext(null)

export function AppProvider({ children }) {
  const [appState, setAppState]             = useState(APP_STATE.IDLE)
  const [selectedNode, setSelectedNode]     = useState(null)
  const [disruptedNode, setDisruptedNode]   = useState(null)  // last successfully disrupted node
  const [activeTab, setActiveTab]           = useState('review')
  const [pipelineResult, setPipelineResult] = useState(null)
  const [sessionStart, setSessionStart]     = useState(null)  // ISO — filters review queue to current session

  // On mount: sync with backend session state so a page refresh doesn't
  // leave the frontend stuck in IDLE while the backend has an active session.
  useEffect(() => {
    fetch('/api/session/state')
      .then(r => r.json())
      .then(data => {
        if (data.active) {
          setAppState(APP_STATE.SESSION_ACTIVE)
          if (data.started_at) setSessionStart(data.started_at)
        }
      })
      .catch(() => {})
  }, [])

  function selectNode(node) {
    if (appState === APP_STATE.PIPELINE_RUNNING) return
    if (appState === APP_STATE.IDLE) return
    setSelectedNode(node)
    setAppState(APP_STATE.NODE_SELECTED)
  }

  function startSession(startedAt) {
    setAppState(APP_STATE.SESSION_ACTIVE)
    setSelectedNode(null)
    setDisruptedNode(null)
    setPipelineResult(null)
    if (startedAt) setSessionStart(startedAt)
  }

  function endSession() {
    setAppState(APP_STATE.IDLE)
    setSelectedNode(null)
    setDisruptedNode(null)
    setPipelineResult(null)
    setSessionStart(null)
  }

  function startPipeline() {
    setAppState(APP_STATE.PIPELINE_RUNNING)
  }

  function pipelineDone(result) {
    setPipelineResult(result)
    setAppState(APP_STATE.REVIEW_READY)
    setActiveTab('review')
    setDisruptedNode(selectedNode)  // freeze the disrupted node for graph highlight
  }

  function resetToActive() {
    setSelectedNode(null)
    setAppState(APP_STATE.SESSION_ACTIVE)
    // disruptedNode intentionally kept until next pipeline run
  }

  function pipelineFailed() {
    setAppState(APP_STATE.NODE_SELECTED)
  }

  return (
    <AppContext.Provider value={{
      appState, selectedNode, disruptedNode, activeTab, pipelineResult, sessionStart,
      setActiveTab,
      selectNode, startSession, endSession,
      startPipeline, pipelineDone, resetToActive, pipelineFailed,
    }}>
      {children}
    </AppContext.Provider>
  )
}

export function useApp() {
  return useContext(AppContext)
}
