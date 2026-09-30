// Risk assessment items (tasks_frontend.md A2.3). Risk values are the backend's `low|medium|high`,
// rolled up deterministically (`rollup_risk`); nothing here judges or re-scores risk.
//
//   plan    GET /api/agent2/plans/{id}                 phases -> units {cluster_id, label, risk_level, ...}
//   detail  GET /api/agent2/plans/{id}/units/{cid}     {report: {risk_factors}, constituent_reports, members: [{name, file_path}]}
//
// An item is a unit rated medium or high. "The phase that addresses it" is the phase the unit is migrated in.
// `risk_factors` are the names of the children that carried the rating. Files come from the unit's members.

export const MAX_DETAILED = 30 // unit-detail requests are per unit, so cap them; the rest still list, without files
export const MAX_FILE_CHIPS = 6
const ORDER = { high: 0, medium: 1 }

export function riskItems(plan) {
  const items = []
  for (const phase of plan?.phases ?? []) {
    for (const unit of phase.units ?? []) {
      if (!(unit.risk_level in ORDER)) continue
      items.push({
        clusterId: unit.cluster_id,
        label: unit.label || 'Unlabeled unit',
        level: unit.risk_level,
        unverified: unit.grounding_status === 'unverified',
        phaseLabel: phase.label || `Phase ${phase.phase_order}`,
        phaseOrder: phase.phase_order,
        unitOrder: unit.unit_order,
        index: items.length,
      })
    }
  }
  return items.sort((a, b) => ORDER[a.level] - ORDER[b.level] || a.index - b.index)
}

export function severityCounts(plan) {
  const counts = { high: 0, medium: 0, low: 0 }
  for (const phase of plan?.phases ?? []) for (const unit of phase.units ?? []) if (unit.risk_level in counts) counts[unit.risk_level] += 1
  return counts
}

/** Distinct factor names and file paths from a unit-detail response (merged units use their constituent reports). */
export function riskDetail(detail) {
  const reports = [detail?.report, ...(detail?.constituent_reports ?? [])].filter(Boolean)
  const factors = [...new Set(reports.flatMap((r) => r.risk_factors ?? []).map(String).filter(Boolean))]
  const files = [...new Set((detail?.members ?? []).map((m) => m.file_path).filter(Boolean))].sort()
  return { factors, files, visibleFiles: files.slice(0, MAX_FILE_CHIPS), hiddenFiles: Math.max(0, files.length - MAX_FILE_CHIPS) }
}
