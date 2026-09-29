// Planning job progress for `PlanningJobStatusPage`: the shared `ProgressViz` and `PhaseTimeline`
// (agent1/, generalised over `phases`) instead of the old dot stepper. Driven by the job row only,
// see planningPhases.js.
import { PhaseTimeline } from '../agent1/PhaseTimeline.jsx'
import { ProgressViz } from '../agent1/ProgressViz.jsx'
import { planningPhases } from './planningPhases.js'
import '../agent1/agent1.css'

export default function PlanningProgress({ job }) {
  const { phases, completed, total, pct } = planningPhases(job)
  return (
    <div className="planning-progress">
      <p className="a2-note" aria-live="polite">{completed} of {total} planning stages complete.</p>
      <ProgressViz phases={phases} pct={pct} label="Planning progress" />
      <PhaseTimeline phases={phases} label="Planning stages" />
    </div>
  )
}
