import { useCallback, useEffect, useRef } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  useReactFlow,
  ReactFlowProvider,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useApp, APP_STATE } from '../../context/AppContext'
import SupplyNode from './SupplyNode'
import { applyColumnLayout } from './graphLayout'

const NODE_TYPES = { supplyNode: SupplyNode }

const EDGE_STYLE         = { stroke: '#2a2d3a', strokeWidth: 1 }
const CASCADE_EDGE_STYLE = { stroke: '#f59e0b', strokeWidth: 2, opacity: 0.85 }

const MINIMAP_NODE_COLOR = n => {
  const map = {
    Factory: '#e07b54', API: '#f0c040', Drug: '#6c9dc6',
    Distributor: '#82c091', Hospital: '#b39ddb',
  }
  return map[n.data?.group] || '#64748b'
}

function buildFlowElements(apiNodes, apiEdges) {
  const rfNodes = apiNodes.map(n => ({
    id:   n.id,
    type: 'supplyNode',
    data: {
      label:         n.label,
      group:         n.group,
      title:         n.title,
      disruptable:   n.disruptable,
      isSelected:    false,
      isDisrupted:   false,
      cascadeLevel:  null,
      cascadeActive: false,
    },
    position: { x: 0, y: 0 },
  }))

  const rfEdges = apiEdges.map((e, i) => {
    const flipped = e.label === 'PRODUCES_API'
    return {
      id:     `e-${i}`,
      source: flipped ? e.to   : e.from,
      target: flipped ? e.from : e.to,
      style:  EDGE_STYLE,
    }
  })

  return applyColumnLayout(rfNodes, rfEdges)
}

// ── Cascade layer computation ────────────────────────────────────────────────
function computeCascadeLayers(disruptedId, disruptedGroup, affectedDrugIds, rawEdges) {
  const affected = new Set(affectedDrugIds || [])

  if (disruptedGroup === 'Factory') {
    const apiIds = rawEdges
      .filter(e => e.label === 'PRODUCES_API' && e.from === disruptedId)
      .map(e => e.to)
    const relatedApis = apiIds.filter(api =>
      rawEdges.some(e => e.label === 'COMPONENT_OF' && e.from === api && affected.has(e.to))
    )
    const distIds = [...new Set(
      rawEdges.filter(e => e.label === 'SUPPLIED_BY' && affected.has(e.from)).map(e => e.to)
    )]
    const hospIds = [...new Set(
      rawEdges.filter(e => e.label === 'DELIVERS_TO' && distIds.includes(e.from)).map(e => e.to)
    )]
    return [
      [disruptedId],
      relatedApis,
      [...affected],
      distIds,
      hospIds,
    ]
  }

  if (disruptedGroup === 'Distributor') {
    const drugIds = [...new Set(
      rawEdges
        .filter(e => e.label === 'SUPPLIED_BY' && e.to === disruptedId && affected.has(e.from))
        .map(e => e.from)
    )]
    const hospIds = [...new Set(
      rawEdges.filter(e => e.label === 'DELIVERS_TO' && e.from === disruptedId).map(e => e.to)
    )]
    return [
      [disruptedId],
      drugIds,
      hospIds,
    ]
  }

  if (disruptedGroup === 'API') {
    const drugIds = [...new Set(
      rawEdges
        .filter(e => e.label === 'COMPONENT_OF' && e.from === disruptedId && affected.has(e.to))
        .map(e => e.to)
    )]
    const distIds = [...new Set(
      rawEdges.filter(e => e.label === 'SUPPLIED_BY' && drugIds.includes(e.from)).map(e => e.to)
    )]
    const hospIds = [...new Set(
      rawEdges.filter(e => e.label === 'DELIVERS_TO' && distIds.includes(e.from)).map(e => e.to)
    )]
    return [
      [disruptedId],
      drugIds,
      distIds,
      hospIds,
    ]
  }

  return [[disruptedId]]
}

// ── Inner component ──────────────────────────────────────────────────────────
function GraphInner() {
  const { appState, selectedNode, disruptedNode, selectNode, pipelineResult } = useApp()
  const isSessionActive = appState !== APP_STATE.IDLE
  const { fitView }  = useReactFlow()
  const didFit       = useRef(false)
  const rawEdgesRef  = useRef([])
  const cascadeTimers  = useRef([])
  const cascadePlayed  = useRef(false)
  const queryClient  = useQueryClient()

  useEffect(() => {
    if (isSessionActive) queryClient.invalidateQueries({ queryKey: ['graph-nodes'] })
  }, [isSessionActive])

  const { data: graphData, isLoading, isError, error } = useQuery({
    queryKey: ['graph-nodes'],
    queryFn:  () => fetch('/api/graph/nodes').then(r => {
      if (!r.ok) throw new Error(`Graph API error: ${r.status}`)
      return r.json()
    }),
    enabled:   isSessionActive,
    staleTime: Infinity,
    retry:     1,
  })

  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])

  useEffect(() => {
    if (!graphData) return
    rawEdgesRef.current = graphData.edges
    const { nodes: ln, edges: le } = buildFlowElements(graphData.nodes, graphData.edges)
    setNodes(ln)
    setEdges(le)
    didFit.current = false
  }, [graphData])

  useEffect(() => {
    if (nodes.length > 0 && !didFit.current) {
      setTimeout(() => { fitView({ padding: 0.1, duration: 400 }); didFit.current = true }, 50)
    }
  }, [nodes, fitView])

  useEffect(() => {
    setNodes(prev => prev.map(n => ({
      ...n,
      data: {
        ...n.data,
        isSelected:  n.id === selectedNode?.id,
        isDisrupted: n.id === disruptedNode?.id && n.id !== selectedNode?.id,
      },
    })))
  }, [selectedNode, disruptedNode])

  // ── Cascade helpers ────────────────────────────────────────────────────────
  function clearCascade() {
    cascadeTimers.current.forEach(t => clearTimeout(t))
    cascadeTimers.current = []
    setNodes(prev => prev.map(n => ({
      ...n,
      data: { ...n.data, cascadeLevel: null, cascadeActive: false },
    })))
    setEdges(prev => prev.map(e => ({ ...e, style: EDGE_STYLE })))
  }

  function runCascade(layers) {
    const LAYER_MS  = 380   // ms between wave fronts
    const ACTIVE_MS = 600   // duration of ping ring per layer

    // Pre-compute cumulative node sets so edges light up as nodes are revealed
    const cumulativeSets = layers.map((_, i) =>
      new Set(layers.slice(0, i + 1).flat())
    )

    layers.forEach((layerIds, i) => {
      if (layerIds.length === 0) return

      const nodesLit = cumulativeSets[i]

      const t = setTimeout(() => {
        // Activate this layer's nodes, deactivate previous layer's ping
        setNodes(prev => prev.map(n => {
          const inThis = layerIds.includes(n.id)
          const inPrev = i > 0 && layers[i - 1].includes(n.id)
          if (!inThis && !inPrev) return n
          return {
            ...n,
            data: {
              ...n.data,
              cascadeLevel:  inThis ? i     : n.data.cascadeLevel,
              cascadeActive: inThis ? true  : (inPrev ? false : n.data.cascadeActive),
            },
          }
        }))

        // Highlight edges where both endpoints are now lit
        setEdges(prev => prev.map(e => {
          if (!nodesLit.has(e.source) || !nodesLit.has(e.target)) return e
          return { ...e, style: CASCADE_EDGE_STYLE }
        }))
      }, i * LAYER_MS)

      cascadeTimers.current.push(t)

      // Turn off ping ring for last layer after animation finishes
      if (i === layers.length - 1) {
        const t2 = setTimeout(() => {
          setNodes(prev => prev.map(n => ({
            ...n,
            data: { ...n.data, cascadeActive: false },
          })))
        }, i * LAYER_MS + ACTIVE_MS)
        cascadeTimers.current.push(t2)
      }
    })
  }

  // ── Trigger cascade on REVIEW_READY ───────────────────────────────────────
  useEffect(() => {
    if (appState !== APP_STATE.REVIEW_READY) {
      cascadePlayed.current = false
      if (
        appState === APP_STATE.IDLE ||
        appState === APP_STATE.PIPELINE_RUNNING ||
        appState === APP_STATE.SESSION_ACTIVE
      ) {
        clearCascade()
      }
      return
    }
    if (cascadePlayed.current) return
    if (!disruptedNode || !pipelineResult?.affected_drug_ids) return

    cascadePlayed.current = true

    // No return-cleanup here — returning clearTimeout would let StrictMode's
    // effect-cleanup-rerun cancel the timer, while cascadePlayed.current stays
    // true on the re-run, permanently preventing the cascade from starting.
    const t = setTimeout(() => {
      const layers = computeCascadeLayers(
        disruptedNode.id,
        disruptedNode.group,
        pipelineResult.affected_drug_ids,
        rawEdgesRef.current,
      )
      runCascade(layers)
    }, 50)
    cascadeTimers.current.push(t)
  }, [appState, disruptedNode, pipelineResult])

  const onNodeClick = useCallback((_, node) => {
    if (!node.data.disruptable) return
    if (appState === APP_STATE.IDLE || appState === APP_STATE.PIPELINE_RUNNING) return
    selectNode({
      id:    node.id,
      label: node.data.label,
      group: node.data.group,
      title: node.data.title,
    })
  }, [appState, selectNode])

  // ── Render ─────────────────────────────────────────────────────────────────
  if (!isSessionActive) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3 text-center">
        <div className="text-5xl opacity-30">⬡</div>
        <p className="text-muted text-sm">Start a session to load the supply chain graph</p>
      </div>
    )
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full gap-3 text-muted">
        <div className="w-4 h-4 rounded-full border-2 border-t-factory border-r-transparent
                        border-b-transparent border-l-transparent animate-spin" />
        <span className="text-sm">Loading supply chain graph…</span>
      </div>
    )
  }

  if (isError) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-2 text-center p-6">
        <div className="text-3xl">⚠</div>
        <p className="text-risk-high text-sm font-medium">Failed to load graph</p>
        <p className="text-muted text-xs max-w-xs">
          {error?.message || 'Could not reach Neo4j. Make sure the database is running.'}
        </p>
      </div>
    )
  }

  return (
    <div className="h-full w-full relative">
      <div className="absolute top-3 left-3 z-10 flex items-center gap-3 px-3 py-1.5
                      bg-surface/90 backdrop-blur-sm border border-border rounded text-xs">
        {[
          { label: 'Factory',     color: '#e07b54' },
          { label: 'API',         color: '#f0c040' },
          { label: 'Drug',        color: '#6c9dc6' },
          { label: 'Distributor', color: '#82c091' },
          { label: 'Hospital',    color: '#b39ddb' },
        ].map(({ label, color }) => (
          <span key={label} className="flex items-center gap-1.5 text-muted">
            <span style={{ background: color }} className="w-2 h-2 rounded-sm inline-block" />
            {label}
          </span>
        ))}
        <span className="text-muted border-l border-border pl-2">✦ = disruptable</span>
      </div>

      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={onNodeClick}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        minZoom={0.05}
        maxZoom={2.5}
        proOptions={{ hideAttribution: true }}
        style={{ background: '#0f1117' }}
      >
        <Background color="#1e2130" gap={24} size={1.5} />
        <Controls
          style={{ background: '#1a1d27', border: '1px solid #2a2d3a', borderRadius: '6px' }}
          showInteractive={false}
        />
        <MiniMap
          nodeColor={MINIMAP_NODE_COLOR}
          nodeStrokeWidth={0}
          style={{ background: '#1a1d27', border: '1px solid #2a2d3a', borderRadius: '6px' }}
          maskColor="rgba(15,17,23,0.75)"
          position="bottom-right"
        />
      </ReactFlow>
    </div>
  )
}

export default function SupplyChainGraph() {
  return (
    <ReactFlowProvider>
      <GraphInner />
    </ReactFlowProvider>
  )
}
