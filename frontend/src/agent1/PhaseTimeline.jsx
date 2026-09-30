import { TriangleAlert } from 'lucide-react'
import { STATUS, StatusIcon } from '../ui/StatusPill.jsx'
import { formatInt } from '../ui/format.js'
import './agent1.css'

/**
 * Vertical phase list: icon + label per state, connector line, the running phase expanded
 * with its latest activity, and inline warning / failure notes.
 * `phases`, `warnings`, `log` and `error` come from buildRunView; warnings match a phase by
 * `stage === phase.id`. The restart button only renders when `onRetry` is passed, since a
 * control that cannot work must be absent rather than disabled.
 */
export function PhaseTimeline({ phases, warnings = [], log, error, onRetry, label = 'Analysis phases' }) {
  const latest = (id) => log?.entries.findLast((entry) => entry.stage === id)
  return (
    <ol className="timeline" aria-label={label}>
      {phases.map((p, i) => {
        const meta = STATUS[p.status]
        const running = p.status === 'in_progress'
        const last = running ? latest(p.id) : null
        return (
          <li key={p.id} className="tl-item" data-status={p.status} aria-current={running || p.status === 'failed' ? 'step' : undefined}>
            <span className="tl-node"><StatusIcon status={p.status} size={18} /></span>
            <div className="tl-body">
              <div className="tl-head">
                <span className="tl-name">{p.label}</span>
                <span className="tl-status" data-tone={meta.tone}>{meta.label}</span>
              </div>
              {running ? (
                <div className="phase-live">
                  {p.detail ? (
                    <>
                      <div className="phase-live-row">
                        <span className="tnum">{formatInt(p.detail.done)} / {formatInt(p.detail.total)} {p.detail.unit}</span>
                        <span className="tnum">{Math.floor(p.progress * 100)}%</span>
                      </div>
                      <div className="phase-bar" aria-hidden="true"><div className="phase-bar-fill" style={{ width: `${p.progress * 100}%` }} /></div>
                      {p.detail.currentPath ? <code className="phase-path" title={p.detail.currentPath}>{p.detail.currentPath}</code> : null}
                    </>
                  ) : (
                    <>
                      {/* No progress counts for this stage (design.md §2): show that work is happening, not how much. */}
                      <div className="phase-bar phase-bar-indeterminate" aria-hidden="true"><div className="phase-bar-fill" /></div>
                      {last ? <span aria-label="Latest activity">{last.message}</span> : null}
                    </>
                  )}
                </div>
              ) : null}
              {warnings.filter((w) => w.stage === p.id).map((w) => (
                <div key={w.id} className="tl-note" data-tone="warning" role="note">
                  <TriangleAlert size={16} className="ui-icon" aria-hidden="true" />
                  <div>
                    <p><strong>Warning.</strong> {w.message}</p>
                    {w.files.length ? <p className="tl-files">{w.files.map((file) => <code key={file} className="log-ref">{file}</code>)}</p> : null}
                  </div>
                </div>
              ))}
              {p.status === 'failed' ? (
                <div className="tl-note" data-tone="danger" role="alert">
                  <StatusIcon status="failed" />
                  <div>
                    <p><strong>Failed.</strong> {error || 'The run stopped before this phase finished.'}</p>
                    {onRetry ? <button type="button" className="tl-retry" onClick={onRetry}>Restart analysis</button> : null}
                  </div>
                </div>
              ) : null}
            </div>
            {i < phases.length - 1 ? (
              <span className="tl-line" aria-hidden="true" data-done={p.status === 'completed' || p.status === 'warning'} />
            ) : null}
          </li>
        )
      })}
    </ol>
  )
}
