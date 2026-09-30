// Open questions and decisions (tasks_frontend.md A2.3): the plan's open facade cycles as decision
// cards. Free-form decisions are not built (design_agent2.md §16 decision 3).
//
//   plan.facade_cycles     [{level: 'unit'|'phase', scc_id, cluster_ids}], still-open needs_facade SCCs only
//   plan.open_facade_flags int; approval cannot complete until it is 0 (design_agent2.md §9.1)
//
// Acknowledged cycles are not in the response, so there is no "decided" card to show, only open ones.

export function openQuestions(plan) {
  const units = new Map()
  const phases = new Map()
  for (const phase of plan?.phases ?? []) {
    phases.set(phase.phase_cluster_id, phase)
    for (const unit of phase.units ?? []) units.set(unit.cluster_id, { phase, unit })
  }
  const cards = (plan?.facade_cycles ?? []).map((cycle) => ({
    key: `${cycle.level}:${cycle.scc_id}`,
    level: cycle.level,
    sccId: cycle.scc_id,
    // an SCC with no id cannot be acknowledged by id; merging its units is the way out
    acknowledgeable: cycle.scc_id != null,
    members: (cycle.cluster_ids ?? []).map((id) => {
      const u = units.get(id)
      if (u) return { id, kind: 'unit', label: u.unit.label || `Unit ${u.phase.phase_order}.${u.unit.unit_order}` }
      const p = phases.get(id)
      if (p) return { id, kind: 'phase', label: `Phase ${p.phase_order}${p.label ? ` · ${p.label}` : ''}` }
      return { id, kind: 'unknown', label: String(id).slice(0, 8) }
    }),
  }))
  const openFlags = plan?.open_facade_flags ?? 0
  return { cards, openFlags, unlisted: Math.max(0, openFlags - cards.length) }
}
