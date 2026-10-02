// Assessment Report section of the project page (tasks_report.md Phase 4 frontend pair).
// Buttons to generate an X-ray / Full Assessment, generation progress (ProgressStages plus
// the job's events), and the project's report list. Contract: design_report.md §9.1.
//
// A report opens on ReportPage (`?report=<id>`), from its row here or from the "ready" card.

import { useCallback, useEffect, useRef, useState } from 'react'
import { useApiFetch } from '../AuthProvider'
import PrimaryButton from '../PrimaryButton'
import { ProgressStages } from '../ProgressStages'
import { formatJobErrorMessage } from '../shared.jsx'
import './report.css'
import { REPORT_STAGES, TIERS, buttonState, isActiveStatus, progressView, reportRow, trackedJob, warnings } from './reportModel.js'

const POLL_INTERVAL_MS = 2000

async function readJson(response) {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(formatJobErrorMessage(response.status, body))
  }
  return response.json()
}

// Polls one generation job's events until it finishes, then calls onFinished once.
function useReportJob(jobId, onFinished) {
  const [envelope, setEnvelope] = useState(null)
  const [events, setEvents] = useState([])
  const [error, setError] = useState(null)
  const apiFetch = useApiFetch()
  const finished = useRef(onFinished)
  finished.current = onFinished

  useEffect(() => {
    setEnvelope(null)
    setEvents([])
    setError(null)
    if (!jobId) return undefined
    let cancelled = false
    let timeoutId
    let after = 0

    const poll = async () => {
      try {
        const data = await readJson(await apiFetch(`/api/reports/jobs/${jobId}/events?after=${after}`))
        if (cancelled) return
        after = data.next_cursor
        setError(null)
        setEnvelope(data)
        if (data.events.length) setEvents((prev) => [...prev, ...data.events])
        if (isActiveStatus(data.status)) timeoutId = setTimeout(poll, POLL_INTERVAL_MS)
        else finished.current?.(data)
      } catch (err) {
        if (cancelled) return
        setError(err.message)
        timeoutId = setTimeout(poll, POLL_INTERVAL_MS * 3) // a blip should not end the view
      }
    }

    poll()
    return () => { cancelled = true; clearTimeout(timeoutId) }
  }, [jobId])

  return { envelope, events, error }
}

function GenerationProgress({ jobId, tier, onFinished, onDismiss, onOpenReport }) {
  const { envelope, events, error } = useReportJob(jobId, onFinished)
  const view = progressView(envelope)
  const notes = warnings(events)
  const label = TIERS[envelope?.tier ?? tier]?.label ?? 'Report'

  return (
    <div className="report-progress" role="status" aria-live="polite" aria-label={`${label} generation`}>
      <p className="report-progress-title">
        {view.finished
          ? view.status === 'done' ? `${label} is ready.` : view.status === 'cancelled' ? `${label} was cancelled.` : `${label} failed.`
          : `Generating ${label}…`}
      </p>
      <ProgressStages
        stages={REPORT_STAGES}
        status={view.status}
        progressStage={view.status === 'done' ? 'done' : view.progressStage}
        label="Report generation progress"
      />
      {notes.map((note) => <p key={note} className="reingest-hint report-note">{note}</p>)}
      {error && <p className="error">Lost contact with the server: {error}. Retrying…</p>}
      {view.status === 'failed' && <p className="error">{view.error || 'An unknown error occurred.'}</p>}
      {view.finished && (
        <div className="report-progress-actions">
          {view.status === 'done' && view.reportId && (
            <PrimaryButton label="Open report" type="button" onClick={() => onOpenReport(view.reportId)} />
          )}
          <button type="button" className="nav-btn" onClick={onDismiss}>Dismiss</button>
        </div>
      )}
    </div>
  )
}

function GenerateButton({ tier, state, onClick, loading }) {
  const { action } = TIERS[tier]
  return (
    <div className="report-generate-item">
      <span title={state.reason ?? undefined}>
        <PrimaryButton label={action} type="button" disabled={state.disabled} loading={loading} onClick={onClick} />
      </span>
      {state.reason && <p className="reingest-hint report-reason">{state.reason}</p>}
    </div>
  )
}

function ReportRow({ report, onOpen }) {
  const row = reportRow(report)
  return (
    <li className="report-row">
      <button type="button" className="project-link report-row-main" onClick={() => onOpen(row.id)}>
        <strong>{row.tier}</strong>
        <span className="run-diff-hint">
          {row.plan && ` · ${row.plan}`} · {new Date(row.createdAt).toLocaleString()}
        </span>
      </button>
      <span className="plan-row-badges">
        {row.outOfDate && (
          <span className="unverified-badge unverified-badge-warn plan-stale-badge">out of date</span>
        )}
        {row.summaryUnverified && <span className="unverified-badge unverified-badge-warn">summary unverified</span>}
        {row.summaryWithheld && <span className="unverified-badge unverified-badge-warn">summary withheld</span>}
        {row.pdf && <span className="plan-status">{row.pdf}</span>}
      </span>
    </li>
  )
}

// `tiers` limits which generate buttons show (the run view offers only the X-ray, the plan page
// only the Full Assessment); the report list always shows every report for the project.
export function ReportsSection({ projectId, onOpenReport, tiers = ['xray', 'full'], intro }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [submittingTier, setSubmittingTier] = useState(null)
  const [startError, setStartError] = useState(null)
  const [startedJob, setStartedJob] = useState(null) // { id, tier }
  const [dismissedJobs, setDismissedJobs] = useState(() => new Set())
  const apiFetch = useApiFetch()

  const load = useCallback(async () => {
    try {
      const body = await readJson(await apiFetch(`/api/projects/${projectId}/reports`))
      setData(body)
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }, [projectId])

  useEffect(() => {
    setData(null)
    setStartedJob(null)
    load()
  }, [projectId])

  const generate = async (tier) => {
    setStartError(null)
    setSubmittingTier(tier)
    try {
      const body = await readJson(await apiFetch(`/api/projects/${projectId}/reports`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tier }),
      }))
      setStartedJob({ id: body.report_job_id, tier })
      load()
    } catch (err) {
      setStartError(err.message)
      load() // eligibility may have changed (e.g. another tab started one)
    } finally {
      setSubmittingTier(null)
    }
  }

  const activeJobs = data?.active_jobs ?? []
  const jobId = trackedJob(startedJob?.id, activeJobs)
  const showProgress = jobId && !dismissedJobs.has(jobId)
  const jobTier = startedJob?.id === jobId ? startedJob.tier : activeJobs.find((j) => j.id === jobId)?.tier
  const activeTiers = activeJobs.map((j) => j.tier)

  const onFinished = useCallback(() => load(), [load])
  const dismiss = () => {
    setDismissedJobs((prev) => new Set(prev).add(jobId))
    setStartedJob(null)
  }

  return (
    <section className="job-history report-section" aria-label="Assessment reports">
      <h2>Assessment reports</h2>
      <p className="reingest-hint">
        {intro ?? 'A read-only report of the latest ingestion run. The X-ray covers the codebase; the Full Assessment adds the migration plan and an estimate.'}
      </p>
      {error && <p className="error">{error}</p>}
      {!error && !data && <p className="summary-loading">Loading reports…</p>}
      {data && (
        <>
          <div className="report-generate">
            {tiers.map((tier) => (
              <GenerateButton
                key={tier}
                tier={tier}
                state={buttonState(tier, data.eligibility, { submitting: submittingTier !== null, activeTiers })}
                loading={submittingTier === tier}
                onClick={() => generate(tier)}
              />
            ))}
          </div>
          {startError && <p className="error">{startError}</p>}
          {showProgress && (
            <GenerationProgress key={jobId} jobId={jobId} tier={jobTier} onFinished={onFinished} onDismiss={dismiss} onOpenReport={onOpenReport} />
          )}
          <h3 className="report-list-title">Reports</h3>
          {data.reports.length === 0 ? (
            <p className="summary-loading">No reports yet — generate one above.</p>
          ) : (
            <ul className="projects-list report-list">
              {data.reports.map((report) => <ReportRow key={report.id} report={report} onOpen={onOpenReport} />)}
            </ul>
          )}
        </>
      )}
    </section>
  )
}
