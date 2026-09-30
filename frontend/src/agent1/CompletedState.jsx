import { CircleCheck } from 'lucide-react'
import PrimaryButton from '../PrimaryButton'
import { formatClock } from '../ui/format.js'
import './agent1.css'

/**
 * Completed: the run finished. `view` is buildRunView().
 * `onViewPlan` navigates to the project page, where GeneratePlanAction lives. It is a navigation, not
 * a trigger: Agent 1 never enqueues Agent 2 (CLAUDE.md). The button is absent until the job page knows
 * its project (A1.6), so no dead control is shown.
 * `children` is the results area: the existing JobSummary, ExtractionCoverage and AskCodebase, passed in
 * by the page because they still live in main.jsx.
 */
export function CompletedState({ view, onViewPlan, children }) {
  const warned = view.warnings.length
  return (
    <>
      <section className="completed" aria-label="Analysis complete">
        <CircleCheck size={20} className="ui-icon completed-icon" aria-hidden="true" />
        <div className="completed-body">
          <h2>Analysis complete</h2>
          <p>
            The knowledge base is ready for migration planning
            {view.elapsed != null ? <> · took <span className="tnum">{formatClock(view.elapsed)}</span></> : null}
            {warned ? <> · {warned} {warned === 1 ? 'warning' : 'warnings'} along the way</> : null}.
          </p>
          {onViewPlan ? <PrimaryButton label="View migration plan" onClick={onViewPlan} /> : null}
        </div>
      </section>
      {children}
    </>
  )
}
