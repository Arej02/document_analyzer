// The Agent 1 run page body: composes the view model and every component of A1.2 / A1.3 / A1.4.
// JobStatusPage (main.jsx) renders this with the same props it always had.
//
// The pieces that still live in main.jsx (job summary, coverage, entity browser, ask, the
// live entity tree, the diff summary and the re-ingest action) come in through `slots`
// rather than imports, which would be circular. Each slot is a function returning a node.

import { useRef } from 'react'
import PrimaryButton from '../PrimaryButton'
import { StatusPill } from '../ui/StatusPill.jsx'
import { Toast } from '../ui/Toast.jsx'
import { useCopy } from '../ui/useCopy.js'
import { CancelControl } from './CancelControl.jsx'
import { CancelledState } from './CancelledState.jsx'
import { CompletedState } from './CompletedState.jsx'
import { ConnectionBanner } from './ConnectionBanner.jsx'
import { FailedState } from './FailedState.jsx'
import { LiveLog } from './LiveLog.jsx'
import { OverallCard } from './OverallCard.jsx'
import { PhaseAnnouncer } from './PhaseAnnouncer.jsx'
import { PhaseTimeline } from './PhaseTimeline.jsx'
import { QueuedState } from './QueuedState.jsx'
import { StatChips } from './StatChips.jsx'
import { ViewLogsButton } from './ViewLogsButton.jsx'
import { buildStatChips } from './statChips.js'
import { useJobRun } from './useJobRun.js'
import './agent1.css'

const PILL_STATUS = { queued: 'queued', running: 'in_progress', done: 'completed', failed: 'failed', cancelled: 'cancelled' }
const PILL_LABEL = { queued: 'Queued', running: 'Running', done: 'Completed', failed: 'Failed', cancelled: 'Cancelled' }

/**
 * slots: { diffSummary(job), liveTree(), restart(projectId), results(), browse() }
 * onViewProject(projectId) navigates to the project page (where Generate plan lives).
 */
export function RunView({ jobId, onBackToForm, onViewProject, slots }) {
  const { job, view, error, connection } = useJobRun(jobId)
  const [copyMessage, copy] = useCopy()
  const logRef = useRef(null)

  const status = view.status
  const active = status === 'queued' || status === 'running'
  const done = status === 'done'
  const restart = job?.project_id ? slots.restart(job.project_id) : null
  // A finished job that predates workflow_events has no log to show.
  const showLog = view.log.entries.length > 0 || active

  let body
  if (!job) {
    body = error
      ? <p className="error">{error}</p>
      : <QueuedState loading />
  } else if (status === 'queued') {
    body = <QueuedState position={job.queue_position} />
  } else {
    body = (
      <>
        <OverallCard view={view} />
        <div className="a1-grid">
          <section className="a1-panel" aria-labelledby="a1-phases-title">
            <h2 id="a1-phases-title" className="a1-panel-title">Phases</h2>
            <PhaseTimeline phases={view.phases} warnings={view.warnings} log={view.log} />
          </section>
          <div className="a1-side">
            <section className="a1-panel" aria-labelledby="a1-found-title">
              <h2 id="a1-found-title" className="a1-panel-title">Discovered so far</h2>
              <StatChips chips={buildStatChips(view.stats, job)} />
              {slots.diffSummary(job)}
            </section>
          </div>
        </div>
        {status === 'failed' ? <FailedState view={view} restart={restart} /> : null}
        {status === 'cancelled' ? <CancelledState view={view} restart={restart} /> : null}
        {showLog ? <LiveLog ref={logRef} log={view.log} onCopy={copy} /> : null}
      </>
    )
  }

  return (
    <main className="page-shell">
      <div className="a1">
        <header className="a1-head">
          <div className="a1-title">
            <h1>{active ? 'Understanding your codebase…' : 'Analysis run'}</h1>
            <p className="a1-sub">Job <code>{jobId}</code></p>
          </div>
          <div className="a1-head-side">
            {job ? <StatusPill status={PILL_STATUS[status]} label={PILL_LABEL[status]} /> : null}
            {job && view.log.entries.length > 0 ? <ViewLogsButton logRef={logRef} /> : null}
            {active && job ? <CancelControl jobId={jobId} onCancelled={connection.retry} /> : null}
          </div>
        </header>

        {connection.lost ? <ConnectionBanner onRetry={connection.retry} /> : null}
        {done ? (
          <CompletedState view={view} onViewPlan={job.project_id ? () => onViewProject(job.project_id) : undefined}>
            {slots.results()}
          </CompletedState>
        ) : null}
        {body}
        {active ? slots.liveTree() : null}

        <div className="a1-foot">
          <PrimaryButton label={status === 'failed' ? 'Retry' : 'Analyze'} type="button" onClick={onBackToForm} />
        </div>
        {done ? slots.browse() : null}
        {done && job?.project_id && slots.nextStep ? (
          <section className="next-step" aria-label="Next step">{slots.nextStep(job)}</section>
        ) : null}
      </div>
      <PhaseAnnouncer view={view} />
      <Toast message={copyMessage} />
    </main>
  )
}
