import { Ban } from 'lucide-react'
import { formatClock } from '../ui/format.js'
import './agent1.css'

/**
 * Cancelled: the user stopped the run. Not an error, so it is a neutral note rather than an alert.
 * `restart` is the same re-ingest slot FailedState takes.
 */
export function CancelledState({ view, restart = null }) {
  const phase = view.current
  return (
    <section className="cancelled" role="status" aria-label="Analysis cancelled">
      <Ban size={20} className="ui-icon cancelled-icon" aria-hidden="true" />
      <div className="cancelled-body">
        <h2>Analysis cancelled</h2>
        <p>
          Stopped{phase ? ` during ${phase.label}` : ''}
          {view.elapsed != null ? <> after <span className="tnum">{formatClock(view.elapsed)}</span></> : null}.
          Nothing from this run is used for migration planning. Start a new analysis to try again.
        </p>
        {restart}
      </div>
    </section>
  )
}
