// Three interchangeable overall-progress renderings. All expose the same progressbar semantics.
import { PHASES, TOTAL_SECONDS } from './runModel.js'

const RING_R = 58
const RING_C = 2 * Math.PI * RING_R

export function RingViz({ pct, tone }) {
  return (
    <div className="ring" data-tone={tone}>
      <svg viewBox="0 0 140 140" width="140" height="140" aria-hidden="true">
        <circle cx="70" cy="70" r={RING_R} className="ring-track" />
        <circle cx="70" cy="70" r={RING_R} className="ring-fill" strokeDasharray={RING_C} strokeDashoffset={RING_C * (1 - pct / 100)} transform="rotate(-90 70 70)" />
      </svg>
      <div className="ring-label tnum"><strong>{Math.floor(pct)}</strong><span>%</span></div>
    </div>
  )
}

function Linear({ pct, tone }) {
  return (
    <div className="bar" data-tone={tone}>
      <div className="bar-fill" style={{ width: `${pct}%` }} />
    </div>
  )
}

function Segmented({ phases }) {
  return (
    <div>
      <ol className="segs">
        {phases.map((p, i) => (
          <li key={p.id} className="seg-cell" style={{ flexGrow: PHASES[i].dur }} data-status={p.status}>
            <div className="seg-track"><div className="seg-fill" style={{ width: `${p.progress * 100}%` }} /></div>
            <span className="seg-num tnum">{i + 1}</span>
          </li>
        ))}
      </ol>
    </div>
  )
}

export default function ProgressViz({ variant, pct, phases, tone }) {
  const value = Math.floor(pct)
  return (
    <div role="progressbar" aria-label="Overall analysis progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={value} aria-valuetext={`${value} percent`} className={`viz viz-${variant}`}>
      {variant === 'ring' ? <RingViz pct={pct} tone={tone} /> : variant === 'segmented' ? <Segmented phases={phases} /> : <Linear pct={pct} tone={tone} />}
    </div>
  )
}

export { TOTAL_SECONDS }
