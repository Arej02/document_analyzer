// Migration plan cards (tasks_frontend.md A2.3), one per phase, from
//   plan        GET /api/agent2/plans/{id}                   phases -> {label, summary_text, approval, units, ...}
//   phaseGraph  GET /api/agent2/plans/{id}/graph?level=phase {nodes, edges: [{from, to}]}, `from` depends on `to`
//
// Not in the data, so not on the card: a goals list (the phase report's summary is the goal text) and an
// effort estimate. Affected files need one unit-detail request per unit, so they are loaded on request.

export const APPROVAL = {
  approved: { label: 'Approved', tone: 'success' },
  rejected: { label: 'Rejected', tone: 'danger' },
  pending: { label: 'Not reviewed', tone: 'neutral' },
}

export const phaseAnchor = (phase) => `phase-${phase.phase_cluster_id}`

/** Outline subsections for the Migration plan section: one per phase, matching each card's heading id. */
export function migrationSubs(plan) {
  return (plan?.phases ?? []).map((p) => ({ id: phaseAnchor(p), title: p.label || `Phase ${p.phase_order}` }))
}

export function approvalState(phase) {
  const status = phase.approval?.status
  return { key: status in APPROVAL ? status : 'pending', note: phase.approval?.note || null }
}

export function phaseCards(plan, phaseGraph) {
  const phases = plan?.phases ?? []
  const byId = new Map(phases.map((p) => [p.phase_cluster_id, p]))
  const dependsOn = new Map()
  const dependedOnBy = new Map()
  const seen = new Set()
  for (const e of phaseGraph?.edges ?? []) {
    const key = `${e.from}>${e.to}`
    if (e.from === e.to || seen.has(key) || !byId.has(e.from) || !byId.has(e.to)) continue
    seen.add(key)
    dependsOn.set(e.from, [...(dependsOn.get(e.from) ?? []), e.to])
    dependedOnBy.set(e.to, [...(dependedOnBy.get(e.to) ?? []), e.from])
  }
  const ref = (id) => ({ id, label: byId.get(id).label || `Phase ${byId.get(id).phase_order}`, order: byId.get(id).phase_order })
  const byOrder = (a, b) => a.order - b.order
  return phases.map((p) => ({
    id: p.phase_cluster_id,
    anchor: phaseAnchor(p),
    order: p.phase_order,
    label: p.label || `Phase ${p.phase_order}`,
    riskLevel: p.risk_level ?? null,
    unverified: p.grounding_status === 'unverified',
    summary: p.summary_text || null,
    approval: approvalState(p),
    units: p.units ?? [],
    // null (not []) when there is no graph, so "unknown" never reads as "depends on nothing"
    dependsOn: phaseGraph ? (dependsOn.get(p.phase_cluster_id) ?? []).map(ref).sort(byOrder) : null,
    dependedOnBy: phaseGraph ? (dependedOnBy.get(p.phase_cluster_id) ?? []).map(ref).sort(byOrder) : null,
  }))
}

/** Distinct file paths over a phase's units, from `useRiskDetails().byId`; `pending` counts units not answered yet. */
export function phaseFiles(units, byId) {
  const files = new Set()
  let pending = 0
  let failed = 0
  for (const u of units) {
    const entry = byId[u.cluster_id]
    if (!entry) pending += 1
    else if (entry.error) failed += 1
    else for (const m of entry.detail?.members ?? []) if (m.file_path) files.add(m.file_path)
  }
  return { files: [...files].sort(), pending, failed }
}
