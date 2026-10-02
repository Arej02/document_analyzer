// Layout and facts for the System map (Architecture section), from data the plan document already loads:
//   plan        phases -> units (label, size, risk_level)
//   phaseGraph  GET /api/agent2/plans/{id}/graph?level=phase   edges `from -> to` mean `from` depends on / calls `to`
//   routes, schemaTotal, packageCount   Agent 1 counts (entry points, WordPress post types + meta keys, declared packages)
// The map reads left to right as a call flow: entry points, then the phases that call others, then the phases they
// call, then the data and packages the application uses. Edges between phases are real. The edges from the entry
// points and to the data and packages attach to the application boundary as a whole, because nothing records which
// phase serves which route or touches which definition.

export const NODE_W = 230
export const NODE_H = 96
export const SIDE_W = 200
export const COL_GAP = 96
export const ROW_GAP = 28
const PAD_X = 28
const PAD_TOP = 52
const PAD_BOTTOM = 28
const OUTER_GAP = 120
// A dependency level with more phases than this wraps into side-by-side columns. One column of 46
// single-unit phases (the sparse-graph case in design_agent2.md §15) is ~5,700px tall, and fit-to-view
// cannot shrink that below the minimum zoom, so the bottom of the map would fall outside the canvas.
export const MAX_ROWS = 8

const RISK_RANK = { low: 1, medium: 2, high: 3 }
const RISK_BY_RANK = [null, 'low', 'medium', 'high']

export function phaseRisk(phase) {
  const rank = Math.max(0, ...(phase.units ?? []).map((u) => RISK_RANK[u.risk_level] ?? 0))
  return RISK_BY_RANK[rank]
}

export function buildSystemMap({ plan, phaseGraph, routes = [], schemaTotal = null, packageCount = null }) {
  const phases = plan?.phases ?? []
  const byId = new Map(phases.map((p) => [p.phase_cluster_id, p]))
  const seen = new Set()
  const edges = []
  for (const e of phaseGraph?.edges ?? []) {
    const key = `${e.from}>${e.to}`
    if (e.from === e.to || seen.has(key) || !byId.has(e.from) || !byId.has(e.to)) continue
    seen.add(key)
    edges.push({ from: e.from, to: e.to, weight: e.weight ?? null, flag: e.flag ?? null })
  }

  // Depth by dependency. Plan order lists dependencies first, so a not-yet-placed target is a cycle's other end.
  const targets = new Map()
  edges.forEach((e) => targets.set(e.from, [...(targets.get(e.from) ?? []), e.to]))
  const depth = new Map()
  phases.forEach((p) => {
    const placed = (targets.get(p.phase_cluster_id) ?? []).filter((id) => depth.has(id)).map((id) => depth.get(id))
    depth.set(p.phase_cluster_id, placed.length ? Math.max(...placed) + 1 : 0)
  })
  const maxDepth = Math.max(0, ...depth.values())
  // Callers on the left, what they call on the right.
  const level = new Map(phases.map((p) => [p.phase_cluster_id, maxDepth - depth.get(p.phase_cluster_id)]))
  // Each level takes ceil(count / MAX_ROWS) visual columns, so `col` below is a drawn column, not a level.
  const levelCount = []
  phases.forEach((p) => { const l = level.get(p.phase_cluster_id); levelCount[l] = (levelCount[l] ?? 0) + 1 })
  const firstCol = []
  let nextCol = 0
  levelCount.forEach((n, l) => { firstCol[l] = nextCol; nextCol += Math.ceil((n ?? 0) / MAX_ROWS) })

  const seenInLevel = []
  const rowsIn = []
  const nodes = phases.map((p) => {
    const l = level.get(p.phase_cluster_id)
    const k = seenInLevel[l] ?? 0
    seenInLevel[l] = k + 1
    const col = (firstCol[l] ?? 0) + Math.floor(k / MAX_ROWS)
    const row = rowsIn[col] ?? 0
    rowsIn[col] = row + 1
    const units = p.units ?? []
    return {
      id: p.phase_cluster_id,
      col,
      row,
      title: p.label || `Phase ${p.phase_order}`,
      order: p.phase_order,
      unitCount: units.length,
      entities: units.reduce((sum, u) => sum + (u.size ?? 0), 0),
      risk: phaseRisk(p),
      units: units.map((u) => ({ id: u.cluster_id, order: u.unit_order, label: u.label || 'Unlabeled unit', risk: u.risk_level ?? null })),
    }
  })

  const cols = Math.max(1, nextCol)
  const tallest = Math.max(1, ...rowsIn.map((n) => n ?? 0))
  const innerH = tallest * NODE_H + (tallest - 1) * ROW_GAP
  const boundary = {
    x: SIDE_W + OUTER_GAP,
    y: 0,
    w: PAD_X * 2 + cols * NODE_W + (cols - 1) * COL_GAP,
    h: PAD_TOP + innerH + PAD_BOTTOM,
  }
  nodes.forEach((n) => {
    const colHeight = rowsIn[n.col] * NODE_H + (rowsIn[n.col] - 1) * ROW_GAP
    n.x = boundary.x + PAD_X + n.col * (NODE_W + COL_GAP)
    n.y = boundary.y + PAD_TOP + (innerH - colHeight) / 2 + n.row * (NODE_H + ROW_GAP)
  })

  const rightX = boundary.x + boundary.w + OUTER_GAP
  const sides = {
    entry: { x: 0, y: boundary.h / 2 - 46, count: routes.length, names: routes.slice(0, 3).map((r) => r.name) },
    data: { x: rightX, y: boundary.h / 2 - 116, count: schemaTotal },
    packages: { x: rightX, y: boundary.h / 2 + 20, count: packageCount },
  }
  return { nodes, edges, boundary, sides, width: rightX + SIDE_W, height: boundary.h }
}

/** What a phase calls and what calls it, for the detail panel. */
export function phaseLinks(map, id) {
  const node = (x) => map.nodes.find((n) => n.id === x)
  return {
    calls: map.edges.filter((e) => e.from === id).map((e) => ({ ...e, node: node(e.to) })),
    calledBy: map.edges.filter((e) => e.to === id).map((e) => ({ ...e, node: node(e.from) })),
  }
}
