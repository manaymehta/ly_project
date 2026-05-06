const COLUMN_ORDER = ['API', 'Factory', 'Drug', 'Distributor', 'Hospital']
const COL_SPACING = 200  // px between columns
const NODE_H = 22
const NODE_W = 72
const NODE_GAP = 5   // vertical gap between nodes in same column

export const LAYOUT_NODE_WIDTH = NODE_W
export const LAYOUT_NODE_HEIGHT = NODE_H

export function applyColumnLayout(nodes, edges) {
  // Group nodes by type, preserving COLUMN_ORDER
  const groups = {}
  COLUMN_ORDER.forEach(g => { groups[g] = [] })
  nodes.forEach(n => {
    const g = n.data?.group
    if (groups[g]) groups[g].push(n)
  })

  // Total canvas height is driven by the tallest column
  const maxCount = Math.max(...COLUMN_ORDER.map(g => groups[g].length), 1)
  const totalH = maxCount * (NODE_H + NODE_GAP) - NODE_GAP

  const layoutedNodes = nodes.map(node => {
    const group = node.data?.group
    const colIndex = COLUMN_ORDER.indexOf(group)
    const groupNodes = groups[group] || []
    const nodeIndex = groupNodes.indexOf(node)
    const count = groupNodes.length

    const x = colIndex * COL_SPACING

    // Centre each column's nodes vertically within totalH
    const colH = count * (NODE_H + NODE_GAP) - NODE_GAP
    const startY = (totalH - colH) / 2
    const y = startY + nodeIndex * (NODE_H + NODE_GAP)

    return { ...node, position: { x, y } }
  })

  return { nodes: layoutedNodes, edges }
}
