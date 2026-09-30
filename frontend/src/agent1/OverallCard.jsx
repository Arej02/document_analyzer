import { formatClock } from '../ui/format.js'
import { ProgressViz } from './ProgressViz.jsx'
import './agent1.css'

const startTime = (iso) => {
  const ms = iso ? Date.parse(iso) : NaN
  return Number.isNaN(ms) ? '—' : new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

// The estimate is a rough guess, so it is always prefixed with ≈ and rounded by buildRunView.
function remainingText(view) {
  if (view.status === 'running') return view.remaining == null ? 'Estimating…' : `≈ ${formatClock(view.remaining)}`
  return '—'
}

/** Overall progress card. `view` is the buildRunView result. */
export function OverallCard({ view, label = 'Overall analysis progress' }) {
  const { status, current, phases } = view
  const done = status === 'done'
  const failed = status === 'failed'
  const heading = done ? 'Analysis complete' : current ? current.label : 'Waiting to start'
  const kicker = done ? 'All phases finished' : current ? `Step ${current.step} of ${current.total}` : `${phases.length} steps`
  const detail = done ? 'Knowledge base is ready for planning.' : failed ? 'Stopped before completion.' : null
  return (
    <section className="overall" aria-label="Overall progress">
      <div className="overall-top">
        <div className="pct tnum" aria-hidden="true"><strong>{Math.floor(view.pct)}</strong><span>%</span></div>
        <div className="overall-phase">
          <p>{kicker}</p>
          <h2>{heading}</h2>
          {detail ? <p>{detail}</p> : null}
        </div>
        <dl className="metrics">
          <div><dt>Elapsed</dt><dd className="tnum">{view.elapsed == null ? '—' : formatClock(view.elapsed)}</dd></div>
          <div><dt>Est. remaining</dt><dd className="tnum">{remainingText(view)}</dd></div>
          <div><dt>Started</dt><dd className="tnum">{startTime(view.startedAt)}</dd></div>
        </dl>
      </div>
      <ProgressViz phases={phases} pct={view.pct} label={label} />
    </section>
  )
}
