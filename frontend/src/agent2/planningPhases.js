// Planning progress in the shape the shared progress components take (`ProgressViz`, `PhaseTimeline`
// from agent1/, generalised over `phases`; tasks_frontend.md Phase 1 contract).
//
// The planner emits no workflow events and has no events endpoint, so all that is known is the job row:
// `status` and `progress_stage` (design_agent2.md §2 `planning_stage`). Progress is therefore whole stages
// only: a finished stage is 100%, the running one is not given a made-up fraction, and `pct` is the
// share of stages completed.
import { jobStageState } from '../jobStageState.js'

export const PLANNING_PHASES = [
  { key: 'graph', label: 'Graph' },
  { key: 'cluster', label: 'Cluster' },
  { key: 'report', label: 'Report' },
  { key: 'order', label: 'Order' },
]

const STATUS = { complete: 'completed', active: 'in_progress', failed: 'failed', pending: 'queued' }

export function planningPhases(job) {
  const phases = PLANNING_PHASES.map((p, i) => {
    const status = STATUS[jobStageState(PLANNING_PHASES, i, job?.status, job?.progress_stage)]
    return { id: p.key, label: p.label, status, progress: status === 'completed' ? 1 : 0, weight: 1 }
  })
  const completed = phases.filter((p) => p.status === 'completed').length
  return { phases, completed, total: phases.length, pct: (completed / phases.length) * 100 }
}
