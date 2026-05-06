import { Handle, Position } from '@xyflow/react'

function vulnColor(score) {
  if (score >= 0.8) return '#ef4444'
  if (score >= 0.6) return '#f97316'
  if (score >= 0.4) return '#f59e0b'
  if (score >= 0.2) return '#84cc16'
  return '#22c55e'
}

function vulnLabel(score) {
  if (score >= 0.8) return 'CRIT'
  if (score >= 0.6) return 'HIGH'
  if (score >= 0.4) return 'MED'
  if (score >= 0.2) return 'LOW'
  return 'MIN'
}

export default function VulnNode({ data }) {
  const score = data.vulnerability_score ?? 0
  const color  = vulnColor(score)

  return (
    <div
      title={data.title}
      style={{
        background:   `${color}12`,
        border:       `1px solid ${color}70`,
        borderRadius: '4px',
        padding:      '2px 5px',
        color,
        fontSize:     '9px',
        fontFamily:   'monospace',
        fontWeight:   600,
        cursor:       'default',
        userSelect:   'none',
        width:        '80px',
        textAlign:    'center',
        whiteSpace:   'nowrap',
        overflow:     'hidden',
        textOverflow: 'ellipsis',
        boxShadow:    score >= 0.7 ? `0 0 6px ${color}40` : 'none',
        transition:   'box-shadow 0.15s',
      }}
    >
      <Handle
        type="target"
        position={Position.Left}
        style={{ background: color, width: 4, height: 4, border: 'none', opacity: 0.6 }}
      />

      <div style={{ lineHeight: '1.3' }}>{data.label}</div>
      <div style={{ fontSize: '7px', opacity: 0.75, marginTop: '1px', letterSpacing: '0.03em' }}>
        {vulnLabel(score)} {score.toFixed(2)}
      </div>

      {/* Score bar at bottom */}
      <div style={{
        marginTop: '2px',
        height: '2px',
        background: `${color}25`,
        borderRadius: '1px',
        overflow: 'hidden',
      }}>
        <div style={{
          width:      `${score * 100}%`,
          height:     '100%',
          background: color,
          borderRadius: '1px',
        }} />
      </div>

      <Handle
        type="source"
        position={Position.Right}
        style={{ background: color, width: 4, height: 4, border: 'none', opacity: 0.6 }}
      />
    </div>
  )
}
