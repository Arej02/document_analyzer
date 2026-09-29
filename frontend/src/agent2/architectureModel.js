// Module table for the Architecture section (tasks_frontend.md A2.3), from
//   plan   GET /api/agent2/plans/{id}                      (phases -> units)
//   graph  GET /api/agent2/plans/{id}/graph?level=unit     ({nodes, edges: [{from, to, weight, flag}]})
//
// An edge `from -> to` means `from` depends on `to` (design_agent2.md §7), so a unit's fan-in is how many
// units depend on it and its fan-out is how many it depends on. The plan has no files, LOC or migration
// target per unit, so those columns do not exist (design_agent2.md §16); `size` is entities.

export function buildModuleRows(plan, graph) {
  const fanIn = new Map()
  const fanOut = new Map()
  const facade = new Set()
  const seen = new Set()
  for (const e of graph?.edges ?? []) {
    if (e.from === e.to) continue
    const key = `${e.from}>${e.to}` // one dependency counts once even if the graph lists it twice
    if (seen.has(key)) continue
    seen.add(key)
    fanOut.set(e.from, (fanOut.get(e.from) ?? 0) + 1)
    fanIn.set(e.to, (fanIn.get(e.to) ?? 0) + 1)
    if (e.flag === 'needs_facade') { facade.add(e.from); facade.add(e.to) }
  }
  const haveGraph = graph != null
  return (plan?.phases ?? []).flatMap((phase) =>
    (phase.units ?? []).map((unit) => ({
      clusterId: unit.cluster_id,
      label: unit.label || 'Unlabeled unit',
      phaseLabel: phase.label || `Phase ${phase.phase_order}`,
      phaseOrder: phase.phase_order,
      unitOrder: unit.unit_order,
      size: unit.size ?? null,
      riskLevel: unit.risk_level ?? null,
      unverified: unit.grounding_status === 'unverified',
      fanIn: haveGraph ? fanIn.get(unit.cluster_id) ?? 0 : null,
      fanOut: haveGraph ? fanOut.get(unit.cluster_id) ?? 0 : null,
      needsFacade: facade.has(unit.cluster_id),
    })),
  )
}

/** The most depended-on units: what breaks the most others if it moves late or wrong. Ties keep plan order. */
export function couplingHotspots(rows, limit = 5) {
  return rows
    .map((row, index) => ({ row, index }))
    .filter(({ row }) => (row.fanIn ?? 0) > 0)
    .sort((a, b) => b.row.fanIn - a.row.fanIn || a.index - b.index)
    .slice(0, limit)
    .map(({ row }) => row)
}
