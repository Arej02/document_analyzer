// Live Agent 1 run: polls the job row, subscribes to its workflow events and returns the
// buildRunView view model. Every timer and poll is cancelled on unmount or jobId change
// (same rule as PlanningJobStatusPage's poll effect).

import { useEffect, useMemo, useRef, useState } from 'react'
import { useApiFetch } from '../AuthProvider'
import { useWorkflowEvents } from '../workflow/useWorkflowEvents.js'
import { buildRunView } from './buildRunView.js'

// Same cadence JobStatusPage uses for the job row; the events poll has its own (1.5 s).
export const JOB_POLL_INTERVAL_MS = 3000
const TICK_MS = 1000
// Two failed polls in a row count as a lost connection; retries back off 3 s, 6 s ... up to 15 s.
export const LOST_AFTER_FAILURES = 2
const retryDelayMs = (failures) => Math.min(JOB_POLL_INTERVAL_MS * 2 ** (failures - 1), 15000)

function formatJobErrorMessage(status, body) {
  const detail = body?.detail
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail) && detail.length) {
    const messages = detail.map((item) => item?.msg).filter(Boolean)
    if (messages.length) return messages.join('; ')
  }
  return `Request failed (${status})`
}

export function useJobRun(jobId) {
  const [job, setJob] = useState(null)
  const [jobError, setJobError] = useState(null)
  const [jobFailures, setJobFailures] = useState(0)
  const pollNow = useRef(null)
  const [tick, setTick] = useState(() => Date.now())
  const apiFetch = useApiFetch()
  const { state: events, error: eventsError, failures: eventsFailures, retry: retryEvents } = useWorkflowEvents(`/api/agent1/jobs/${jobId}/events`)

  useEffect(() => {
    let cancelled = false
    let timeoutId
    setJob(null)
    setJobError(null)
    setJobFailures(0)
    let failures = 0

    const poll = async () => {
      try {
        const response = await apiFetch(`/api/agent1/jobs/${jobId}`)
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(formatJobErrorMessage(response.status, body))
        }
        const data = await response.json()
        if (cancelled) return
        failures = 0
        setJobFailures(0)
        setJobError(null)
        setJob(data)
        if (data.status === 'queued' || data.status === 'running') timeoutId = setTimeout(poll, JOB_POLL_INTERVAL_MS)
      } catch (err) {
        if (cancelled) return
        failures += 1
        setJobFailures(failures)
        setJobError(err.message)
        // Keep the last known job in state and try again; only unmount or a new jobId stops it.
        timeoutId = setTimeout(poll, retryDelayMs(failures))
      }
    }

    // A manual retry polls right away instead of waiting out the backoff.
    pollNow.current = () => { clearTimeout(timeoutId); poll() }
    poll()
    // apiFetch is a new closure every render; left out of the deps so a re-render doesn't restart polling.
    return () => { cancelled = true; clearTimeout(timeoutId); pollNow.current = null }
  }, [jobId])

  // Re-render once a second while the run is live so elapsed and ETA advance between polls.
  const live = job == null || job.status === 'queued' || job.status === 'running'
  useEffect(() => {
    if (!live) return undefined
    const id = setInterval(() => setTick(Date.now()), TICK_MS)
    return () => clearInterval(id)
  }, [live])

  // Correct for this browser's clock skew against the server-issued timestamps, as WorkflowRun does.
  const skewMs = events.serverTime != null && events.clientReceivedAt != null
    ? new Date(events.serverTime).getTime() - events.clientReceivedAt
    : 0
  const now = tick + (Number.isFinite(skewMs) ? skewMs : 0)

  const view = useMemo(() => buildRunView({ job, events, now }), [job, events, now])
  // `lost`: repeated failures. The last known job and events stay in `view`, so the page keeps showing them.
  const connection = {
    lost: jobFailures >= LOST_AFTER_FAILURES || eventsFailures >= LOST_AFTER_FAILURES,
    retry: () => { pollNow.current?.(); retryEvents() },
  }
  return { job, view, error: jobError ?? eventsError, connection }
}
