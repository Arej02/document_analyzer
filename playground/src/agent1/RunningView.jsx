// Agent 1 — live analysis run. Drives the mock simulation and composes the presentational parts.
import { useEffect, useMemo, useRef, useState } from 'react'
import { Ban, CircleCheck, CircleX, FileText, GitBranch, LoaderCircle, Pause, Play, RotateCcw, ScrollText, Server, TriangleAlert } from 'lucide-react'
import { FAILURE, PHASES, REPO, TOTAL_SECONDS, clockAt, snapshot } from './runModel.js'
import ProgressViz, { RingViz } from './ProgressViz.jsx'
import PhaseTimeline from './PhaseTimeline.jsx'
import StatChips from './StatChips.jsx'
import LiveLog from './LiveLog.jsx'
import { StatePanel, Toast, fmtClock, fmtDuration, useCopy } from '../components/shared.jsx'

function Header({ status, warningCount, children }) {
  const pill = {
    running: { tone: 'active', label: 'Running', icon: <LoaderCircle size={14} className="icon spin" aria-hidden="true" /> },
    paused: { tone: 'neutral', label: 'Paused', icon: <Pause size={14} className="icon" aria-hidden="true" /> },
    completed: { tone: 'success', label: 'Completed', icon: <CircleCheck size={14} className="icon" aria-hidden="true" /> },
    failed: { tone: 'danger', label: 'Failed', icon: <CircleX size={14} className="icon" aria-hidden="true" /> },
    cancelled: { tone: 'neutral', label: 'Cancelled', icon: <Ban size={14} className="icon" aria-hidden="true" /> },
    queued: { tone: 'neutral', label: 'Queued', icon: <Server size={14} className="icon" aria-hidden="true" /> },
  }[status]
  return (
    <header className="a1-head">
      <div className="a1-title">
        <h1>Analysing <span className="repo">{REPO.name}</span></h1>
        <p className="a1-sub muted">
          <span className="a1-sub-item"><GitBranch size={14} className="icon" aria-hidden="true" /><code>{REPO.branch}</code></span>
          <span className="a1-sub-item">Commit <code>{REPO.commit}</code></span>
          <span className="a1-sub-item">Job <code>job_8f21c0</code></span>
        </p>
      </div>
      <div className="a1-head-side">
        <span className="pill" data-tone={pill.tone}>{pill.icon}{pill.label}</span>
        {warningCount ? <span className="pill" data-tone="warning"><TriangleAlert size={14} className="icon" aria-hidden="true" />{warningCount} warning{warningCount > 1 ? 's' : ''}</span> : null}
      </div>
      {children}
    </header>
  )
}

function QueuedView({ onStart, onCancel }) {
  return (
    <>
      <Header status="queued" />
      <div className="card queued" aria-busy="true">
        <p><strong>Waiting for a worker.</strong> <span className="muted">2 jobs are ahead of this one. Analysis usually starts within a minute.</span></p>
        <div className="queued-skel" aria-hidden="true">
          <div className="skel" style={{ height: 40, width: 120 }} />
          <div className="skel" style={{ height: 12, width: '100%' }} />
          <div className="skel" style={{ height: 12, width: '78%' }} />
          <div className="skel" style={{ height: 12, width: '64%' }} />
        </div>
        <div className="state-actions">
          <button type="button" className="btn btn-danger" onClick={onCancel}>Cancel job</button>
          <button type="button" className="btn" onClick={onStart}>Skip queue (demo)</button>
        </div>
      </div>
    </>
  )
}

export default function RunningView({ settings, update, simCmd, onOpenPlan }) {
  const failing = settings.a1State === 'failed'
  const [elapsed, setElapsed] = useState(0)
  const [status, setStatus] = useState('running') // running | paused | cancelled
  const [confirmCancel, setConfirmCancel] = useState(false)
  const [copyMsg, copy] = useCopy()
  const logRef = useRef(null)

  const restart = () => { setElapsed(0); setStatus('running'); setConfirmCancel(false) }
  useEffect(restart, [settings.a1State])
  useEffect(() => { if (simCmd.n === 0) return; simCmd.type === 'end' ? setElapsed(failing ? FAILURE.atSeconds : TOTAL_SECONDS) : restart() }, [simCmd])

  const live = settings.a1State === 'live' || failing
  const snap = useMemo(() => snapshot(elapsed, failing), [elapsed, failing])
  const finished = snap.done || snap.failed
  const ticking = live && status === 'running' && !finished

  useEffect(() => {
    if (!ticking) return
    const speed = settings.simSpeed
    const id = setInterval(() => setElapsed((e) => e + 0.2 * speed), 200)
    return () => clearInterval(id)
  }, [ticking, settings.simSpeed])

  if (settings.a1State === 'idle') {
    return (
      <div className="a1">
        <StatePanel icon={<ScrollText size={20} />} title="No analysis is running" actions={<button type="button" className="btn btn-primary" onClick={() => update({ a1State: 'live' })}><Play size={14} className="icon" aria-hidden="true" />Start analysis</button>}>
          Analysis reads the repository without executing it, then builds the knowledge base that migration planning uses. Choose a repository and start a run to see progress here.
        </StatePanel>
      </div>
    )
  }
  if (settings.a1State === 'queued') {
    return <div className="a1"><QueuedView onStart={() => update({ a1State: 'live' })} onCancel={() => update({ a1State: 'idle' })} /></div>
  }

  const shownStatus = snap.failed ? 'failed' : snap.done ? 'completed' : status
  const tone = snap.failed ? 'danger' : snap.warnings.length ? 'warning' : 'active'
  const { current } = snap
  const settled = shownStatus !== 'running' && shownStatus !== 'paused'

  return (
    <div className="a1">
      <Header status={shownStatus} warningCount={snap.warnings.length}>
        <div className="a1-controls">
          {snap.done ? (
            <>
              <button type="button" className="btn btn-primary" onClick={onOpenPlan}><FileText size={14} className="icon" aria-hidden="true" />View migration plan</button>
              <button type="button" className="btn" onClick={restart}><RotateCcw size={14} className="icon" aria-hidden="true" />Run again</button>
            </>
          ) : snap.failed || status === 'cancelled' ? (
            <button type="button" className="btn" onClick={restart}><RotateCcw size={14} className="icon" aria-hidden="true" />Restart analysis</button>
          ) : (
            <>
              <button type="button" className="btn" onClick={() => setStatus((s) => (s === 'paused' ? 'running' : 'paused'))}>
                {status === 'paused' ? <><Play size={14} className="icon" aria-hidden="true" />Resume</> : <><Pause size={14} className="icon" aria-hidden="true" />Pause</>}
              </button>
              {confirmCancel ? (
                <span className="a1-confirm" role="group" aria-label="Confirm cancel">
                  <button type="button" className="btn btn-danger" autoFocus onClick={() => { setStatus('cancelled'); setConfirmCancel(false) }}>Cancel analysis</button>
                  <button type="button" className="btn btn-ghost" onClick={() => setConfirmCancel(false)}>Keep running</button>
                </span>
              ) : (
                <button type="button" className="btn btn-danger" onClick={() => setConfirmCancel(true)}><Ban size={14} className="icon" aria-hidden="true" />Cancel</button>
              )}
            </>
          )}
          <button type="button" className="btn" onClick={() => logRef.current?.reveal()}><ScrollText size={14} className="icon" aria-hidden="true" />View logs</button>
        </div>
      </Header>

      <p className="sr-only" role="status" aria-live="polite">
        {snap.failed ? `Analysis failed during ${current.name}` : snap.done ? 'Analysis completed' : status === 'cancelled' ? 'Analysis cancelled' : `Now running: ${current.name}, step ${snap.currentIndex + 1} of ${PHASES.length}`}
      </p>

      {status === 'cancelled' ? (
        <p className="a1-banner" role="note"><Ban size={16} className="icon" aria-hidden="true" />Cancelled at {fmtClock(snap.t)}. Partial results were discarded. Restart to analyse from the beginning.</p>
      ) : null}

      <section className="card overall" aria-label="Overall progress">
        <div className={`overall-top overall-top-${settings.progressStyle}`}>
          {settings.progressStyle === 'ring' ? (
            <div role="progressbar" aria-label="Overall analysis progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.floor(snap.pct)}><RingViz pct={snap.pct} tone={tone} /></div>
          ) : (
            <div className="pct tnum" aria-hidden="true"><strong>{Math.floor(snap.pct)}</strong><span>%</span></div>
          )}
          <div className="overall-phase">
            <p className="muted">{snap.done ? 'All phases finished' : `Step ${snap.currentIndex + 1} of ${PHASES.length}`}</p>
            <h2>{snap.done ? 'Analysis complete' : current.name}</h2>
            <p className="muted tnum">{snap.done ? 'Knowledge base is ready for planning.' : snap.failed ? 'Stopped before completion.' : current.label}</p>
          </div>
          <dl className="metrics">
            <div><dt>Elapsed</dt><dd className="tnum">{fmtClock(snap.t)}</dd></div>
            <div><dt>Est. remaining</dt><dd className="tnum">{settled || snap.done ? '—' : `≈ ${fmtDuration(snap.remaining)}`}</dd></div>
            <div><dt>Started</dt><dd className="tnum">{clockAt(0)}</dd></div>
          </dl>
        </div>
        {settings.progressStyle !== 'ring' ? <ProgressViz variant={settings.progressStyle} pct={snap.pct} phases={snap.phases} tone={tone} /> : null}
      </section>

      <div className="a1-grid">
        <section className="card" aria-labelledby="phases-title">
          <h2 id="phases-title" className="card-title">Phases</h2>
          <PhaseTimeline phases={snap.phases} warnings={snap.warnings} failure={snap.failed ? FAILURE : null} paused={status === 'paused'} onRetry={restart} />
        </section>
        <div className="a1-side">
          <section className="card" aria-labelledby="found-title">
            <h2 id="found-title" className="card-title">Discovered so far</h2>
            <StatChips stats={snap.stats} />
          </section>
        </div>
      </div>

      <LiveLog ref={logRef} entries={snap.log} onCopy={copy} />
      <Toast message={copyMsg} />
    </div>
  )
}
