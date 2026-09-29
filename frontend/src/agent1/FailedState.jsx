import { CircleX } from 'lucide-react'
import './agent1.css'

/**
 * Failed: which phase stopped the run and the job's error text, unaltered (it is written by the worker,
 * so it is shown as-is; request failures already go through formatJobErrorMessage in useJobRun).
 * `view` is buildRunView(). `restart` is a slot for the re-ingest control (`ReingestAction`, which needs
 * the project object). It renders nothing there until the job page knows its project (A1.6).
 * When this is on screen, don't also pass `error` to PhaseTimeline, so the message appears once.
 */
export function FailedState({ view, restart = null }) {
  const phase = view.current
  return (
    <section className="failed" role="alert" aria-label="Analysis failed">
      <CircleX size={20} className="ui-icon failed-icon" aria-hidden="true" />
      <div className="failed-body">
        <h2>Analysis failed{phase ? ` during ${phase.label}` : ''}</h2>
        {view.error ? <p className="failed-error">{view.error}</p> : <p className="failed-error">The worker stopped without giving a reason.</p>}
        {restart}
      </div>
    </section>
  )
}
