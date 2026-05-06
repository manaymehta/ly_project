import { Handle, Position } from '@xyflow/react'

const GROUP_STYLES = {
  Factory:     { bg: '#e07b5418', border: '#e07b54', text: '#e07b54', selBg: '#e07b5435' },
  API:         { bg: '#f0c04018', border: '#f0c040', text: '#f0c040', selBg: '#f0c04035' },
  Drug:        { bg: '#6c9dc618', border: '#6c9dc6', text: '#6c9dc6', selBg: '#6c9dc635' },
  Distributor: { bg: '#82c09118', border: '#82c091', text: '#82c091', selBg: '#82c09135' },
  Hospital:    { bg: '#b39ddb18', border: '#b39ddb', text: '#b39ddb', selBg: '#b39ddb35' },
}

export default function SupplyNode({ data }) {
  const s           = GROUP_STYLES[data.group] || GROUP_STYLES.Drug
  const isSelected  = data.isSelected
  const isDisrupted = data.isDisrupted
  const isCascade   = data.cascadeLevel != null   // any level ≥ 0

  // Background: cascade uses same brightness as selected so it's clearly visible
  const background = isSelected
    ? s.selBg
    : isDisrupted
    ? '#ef444420'
    : isCascade
    ? s.selBg       // match selected brightness — unmistakably highlighted
    : s.bg

  // Border: disrupted = red, cascade = full-opacity group color, normal = same
  const borderColor = isDisrupted
    ? '#ef4444cc'
    : s.border      // cascade and normal share the same border (glow distinguishes them)

  // Glow ring: cascade gets a prominent double-ring shadow
  const boxShadow = isSelected
    ? `0 0 0 2px ${s.border}90, 0 0 10px ${s.border}40`
    : isDisrupted
    ? '0 0 0 2px #ef444480, 0 0 10px #ef444440'
    : isCascade
    ? `0 0 0 2px ${s.border}70, 0 0 14px ${s.border}50`
    : 'none'

  const pingColor = isDisrupted ? '#ef4444' : s.border

  return (
    // Outer wrapper has no overflow:hidden so the ping ring can expand freely
    <div style={{ position: 'relative', width: '72px' }}>

      {/* Expanding ping ring — plays once when the cascade wave hits this node */}
      {data.cascadeActive && (
        <div style={{
          position:      'absolute',
          inset:         '-7px',
          borderRadius:  '8px',
          border:        `2.5px solid ${pingColor}`,
          animation:     'cascadePing 0.58s ease-out forwards',
          pointerEvents: 'none',
        }} />
      )}

      {/* Inner content */}
      <div
        title={data.title}
        style={{
          background,
          border:       `1px solid ${borderColor}`,
          borderRadius: '4px',
          padding:      '2px 6px',
          color:        s.text,
          fontSize:     '9px',
          fontFamily:   'monospace',
          fontWeight:   600,
          cursor:       data.disruptable ? 'pointer' : 'default',
          userSelect:   'none',
          overflow:     'hidden',
          textOverflow: 'ellipsis',
          whiteSpace:   'nowrap',
          textAlign:    'center',
          boxShadow,
          transition:   'box-shadow 0.2s, background 0.2s, border-color 0.2s',
        }}
      >
        <Handle
          type="target"
          position={Position.Left}
          style={{ background: s.border, width: 4, height: 4, border: 'none' }}
        />

        {data.label}
        {data.disruptable && (
          <span style={{ marginLeft: 3, opacity: 0.5, fontSize: 7 }}>✦</span>
        )}
        {isDisrupted && (
          <span style={{ marginLeft: 3, fontSize: 7, color: '#ef4444' }}>!</span>
        )}

        <Handle
          type="source"
          position={Position.Right}
          style={{ background: s.border, width: 4, height: 4, border: 'none' }}
        />
      </div>
    </div>
  )
}
