import { TriangleAlert } from 'lucide-react'
import { STATUS, StatusIcon, fmtClock } from '../components/shared.jsx'

function LiveDetail({ phase, paused }) {
  return (
    <div className="phase-live">
      <div className="phase-live-row">
        <span className="tnum">{phase.label}</span>
        <span className="muted tnum">{Math.floor(phase.progress * 100)}%{paused ? ' (paused)' : ''}</span>
      </div>
      <div className="bar bar-thin"><div className="bar-fill" style={{ width: `${phase.progress * 100}%` }} /></div>
      <code className="phase-path" title={phase.path}>{phase.path}</code>
    </div>
  )
}

export default function PhaseTimeline({ phases, warnings, failure, paused, onRetry }) {
  return (
    <ol className="timeline" aria-label="Analysis phases">
      {phases.map((p, i) => {
        const meta = STATUS[p.status]
        const right = p.status === 'queued' ? '' : fmtClock(p.duration)
        return (
          <li key={p.id} className="tl-item" data-status={p.status} aria-current={p.status === 'in_progress' ? 'step' : undefined}>
            <span className="tl-node"><StatusIcon status={p.status} size={18} /></span>
            <div className="tl-body">
              <div className="tl-head">
                <span className="tl-name">{p.name}</span>
                <span className="tl-status" data-tone={meta.tone}>{p.status === 'in_progress' && paused ? 'Paused' : meta.label}</span>
                <span className="tl-time muted tnum">{right}</span>
              </div>
              {p.status === 'in_progress' ? <LiveDetail phase={p} paused={paused} /> : null}
              {p.warned ? warnings.filter((w) => w.phaseId === p.id).map((w) => (
                <div key={w.id} className="tl-note" data-tone="warning" role="note">
                  <TriangleAlert size={16} className="icon" aria-hidden="true" />
                  <div>
                    <p><strong>Warning.</strong> {w.text}</p>
                    <p className="tl-files">{w.files.map((f) => <code key={f} className="chip">{f}</code>)}</p>
                  </div>
                </div>
              )) : null}
              {p.status === 'failed' && failure ? (
                <div className="tl-note" data-tone="danger" role="alert">
                  <StatusIcon status="failed" />
                  <div>
                    <p><strong>{failure.title}.</strong> {failure.detail}</p>
                    <button type="button" className="btn btn-sm" onClick={onRetry}>Restart analysis</button>
                  </div>
                </div>
              ) : null}
            </div>
            {i < phases.length - 1 ? <span className="tl-line" aria-hidden="true" data-done={p.status === 'completed' || p.status === 'warning'} /> : null}
          </li>
        )
      })}
    </ol>
  )
}
