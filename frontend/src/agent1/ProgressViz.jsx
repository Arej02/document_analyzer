import { STATUS } from '../ui/StatusPill.jsx'
import './agent1.css'

/**
 * Overall progress as one segment per phase, each as wide as its `weight`.
 * `phases`: [{ id, label, status, progress (0..1), weight }] from buildRunView; `pct` is 0..100.
 * The phase list is never hardcoded here, so planning and execution runs can reuse it.
 * State never relies on colour: hatch = warning, red outline = failed, dashed = queued,
 * accent outline = in progress, and each segment carries its status as screen-reader text.
 */
export function ProgressViz({ phases, pct, label }) {
  const value = Math.floor(pct)
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value}
      aria-valuetext={`${value} percent`}
      className="viz"
    >
      <ol className="segs">
        {phases.map((p, i) => (
          <li key={p.id} className="seg-cell" style={{ flexGrow: p.weight }} data-status={p.status}>
            <div className="seg-track"><div className="seg-fill" style={{ width: `${p.progress * 100}%` }} /></div>
            <span className="seg-num tnum" aria-hidden="true">{i + 1}</span>
            <span className="sr-only">{`Phase ${i + 1}, ${p.label}: ${STATUS[p.status].label}`}</span>
          </li>
        ))}
      </ol>
    </div>
  )
}
