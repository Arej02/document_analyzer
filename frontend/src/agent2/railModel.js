// Numbers for the plan rail, derived only from `GET /api/agent2/plans/{id}` (design_agent2.md §11).
// There is no effort estimate in the plan data, so there is no effort card (tasks_frontend.md A2.2).
const RISK_ORDER = { low: 0, medium: 1, high: 2 }

/** Same rule as the backend's `rollup_risk`: the highest child level, and the phases that carry it. */
export function overallRisk(phases) {
  if (!phases.length) return { level: 'low', carriers: [] }
  const known = phases.filter((p) => p.risk_level in RISK_ORDER)
  if (!known.length) return { level: null, carriers: [] }
  const level = known.reduce((top, p) => (RISK_ORDER[p.risk_level] > RISK_ORDER[top] ? p.risk_level : top), 'low')
  return { level, carriers: known.filter((p) => p.risk_level === level).map((p) => p.label) }
}

/** Phase approvals are the reviewable items (`phase_approvals`): approved, rejected, or not yet reviewed. */
export function reviewProgress(phases) {
  const approved = phases.filter((p) => p.approval?.status === 'approved').length
  const rejected = phases.filter((p) => p.approval?.status === 'rejected').length
  return { approved, rejected, pending: phases.length - approved - rejected, total: phases.length }
}

export function planMetrics(plan) {
  const phases = plan.phases ?? []
  const units = phases.flatMap((p) => p.units ?? [])
  return {
    phases: phases.length,
    units: units.length,
    entities: units.reduce((n, u) => n + (u.size ?? 0), 0),
    openFacadeFlags: plan.open_facade_flags ?? 0,
  }
}

export function railModel(plan) {
  const phases = plan.phases ?? []
  return { risk: overallRisk(phases), review: reviewProgress(phases), metrics: planMetrics(plan) }
}
