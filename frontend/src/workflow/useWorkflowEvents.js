// Live workflow run view — design.md §4, tasks.md "Live workflow run view".
//
// Polls GET .../events?after=<cursor> and folds each response into
// workflowReducer.js's pure state via applyWorkflowEvents. `url` is the full events
// endpoint for one workflow — e.g. `/api/agent1/jobs/${jobId}/events` — so this hook
// stays agent-agnostic, same reasoning as the reducer it's built on.
//
// Same shape as JobStatusPage's own poll effect (main.jsx): a `cancelled` flag plus
// `clearTimeout` on cleanup, and a recursive setTimeout rather than setInterval so a
// slow request can't pile up overlapping polls.

import { useEffect, useRef, useState } from 'react'
import { useApiFetch } from '../AuthProvider'
import { applyWorkflowEvents, createInitialWorkflowState } from './workflowReducer.js'

// design.md §2's decision: "Cursor polling at about 1.5 s while running".
export const WORKFLOW_POLL_INTERVAL_MS = 1500

// 1.5 s, 3 s, 6 s ... capped at 15 s.
const retryDelayMs = (failures) => Math.min(WORKFLOW_POLL_INTERVAL_MS * 2 ** (failures - 1), 15000)

// FastAPI error bodies are either {"detail": "..."} or {"detail": [{"msg": "...", ...}]}
// — same parsing main.jsx's formatJobErrorMessage does for the job-status poll; kept
// local here rather than imported from that page-level file.
function formatEventsErrorMessage(status, body) {
  const detail = body?.detail
  if (typeof detail === 'string' && detail) return detail
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message
  if (Array.isArray(detail) && detail.length) {
    const messages = detail.map((item) => item?.msg).filter(Boolean)
    if (messages.length) return messages.join('; ')
  }
  return `Request failed (${status})`
}

export function useWorkflowEvents(url) {
  const [state, setState] = useState(createInitialWorkflowState)
  const [error, setError] = useState(null)
  // Consecutive failed polls; a failed poll is retried with backoff instead of ending the feed.
  const [failures, setFailures] = useState(0)
  const pollNow = useRef(null)
  const apiFetch = useApiFetch()

  useEffect(() => {
    let cancelled = false
    let timeoutId
    let cursor = 0
    let localState = createInitialWorkflowState()
    // design.md §4: "The client polls ... once more after a terminal status to drain
    // the tail" — the job's status row can flip to done/failed slightly before the
    // emitter's own trailing flush (worker/run.py calls mark_job_done_if_ready, *then*
    // emitter.complete()), so one extra poll after the first terminal response is
    // what actually picks up the last batch of events, including workflow.completed/
    // workflow.failed itself.
    let didDrain = false

    // Reset to a fresh run whenever the target url changes, not just on mount.
    setState(localState)
    setError(null)
    setFailures(0)
    let consecutiveFailures = 0

    const poll = async () => {
      try {
        const response = await apiFetch(`${url}?after=${cursor}`)
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(formatEventsErrorMessage(response.status, body))
        }
        const data = await response.json()
        if (cancelled) return
        consecutiveFailures = 0
        setFailures(0)
        setError(null)

        cursor = data.next_cursor
        localState = applyWorkflowEvents(localState, data.events)
        // Not a workflow_events event — the response envelope's own server_time
        // (design.md §4), folded in here rather than in workflowReducer.js, which
        // only ever sees individual events. useJobRun's elapsed timer uses this
        // plus the moment it was received to correct for this browser's clock skew,
        // instead of trusting Date.now() against a server-issued startedAt outright.
        localState = { ...localState, serverTime: data.server_time, clientReceivedAt: Date.now() }
        setState(localState)

        const terminal = data.status === 'done' || data.status === 'failed' || data.status === 'cancelled'
        if (terminal && didDrain) return // this poll was the drain — stop for good
        didDrain = didDrain || terminal
        timeoutId = setTimeout(poll, WORKFLOW_POLL_INTERVAL_MS)
      } catch (err) {
        if (cancelled) return
        consecutiveFailures += 1
        setFailures(consecutiveFailures)
        setError(err.message)
        timeoutId = setTimeout(poll, retryDelayMs(consecutiveFailures))
      }
    }

    // A manual retry polls right away instead of waiting out the backoff.
    pollNow.current = () => { clearTimeout(timeoutId); poll() }
    poll()
    // apiFetch is a new closure every render (useApiFetch isn't memoized) — left out
    // of the deps on purpose, same as JobStatusPage's own poll effect, so a re-render
    // doesn't restart polling and reset the cursor.
    return () => { cancelled = true; clearTimeout(timeoutId); pollNow.current = null }
  }, [url])

  return { state, error, failures, retry: () => pollNow.current?.() }
}
