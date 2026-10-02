import { useEffect, useRef, useState } from 'react'
import { useApiFetch } from '../AuthProvider'
import PrimaryButton from '../PrimaryButton'
import { formatJobErrorMessage } from '../shared.jsx'
import { POLL_MS, deliverBlocker, isActive, newestDelivery, parityNote, prLabel, safePrUrl } from './delivery.js'

// design_agent3.md §15.8–§15.12: deliver this execution's generated code to the project's connected GitHub repository
// as a stack of pull requests (a scaffold PR, then one per phase), alongside the zip download rather than instead of it.
//   POST /api/agent3/executions/{id}/deliver   -> 202, the delivery runs in the background
//   GET  /api/agent3/executions/{id}/delivery  -> polled every few seconds while a delivery is queued or running
// The backend's refusal and failure messages (stale plan, token no longer accepted, "Automatically delete head
// branches" off, an interrupted run ...) are shown verbatim: they are written to be acted on.
export default function DeliverToGithubAction({ executionJobId, projectId, hasOutput }) {
  const apiFetch = useApiFetch()
  const [connection, setConnection] = useState(null)
  const [deliveries, setDeliveries] = useState(null)
  const [loadError, setLoadError] = useState(null)
  const [error, setError] = useState(null)
  const [starting, setStarting] = useState(false)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  const readJson = async (response) => {
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(formatJobErrorMessage(response.status, body))
    return body
  }

  const loadDeliveries = async () => {
    const body = await readJson(await apiFetch(`/api/agent3/executions/${executionJobId}/delivery`))
    if (alive.current) setDeliveries(body.deliveries || [])
  }

  useEffect(() => {
    let cancelled = false
    setLoadError(null)
    ;(async () => {
      try {
        if (projectId) {
          const body = await readJson(await apiFetch(`/api/agent3/projects/${projectId}/github-connection`))
          if (!cancelled) setConnection(body)
        } else if (!cancelled) {
          setConnection({ connected: false })
        }
        await loadDeliveries()
      } catch (err) {
        if (!cancelled) setLoadError(err.message)
      }
    })()
    return () => { cancelled = true }
  }, [executionJobId, projectId])

  const latest = newestDelivery(deliveries)
  const polling = latest ? isActive(latest.status) : false

  useEffect(() => {
    if (!polling) return undefined
    const timer = setInterval(() => { loadDeliveries().catch((err) => { if (alive.current) setLoadError(err.message) }) }, POLL_MS)
    return () => clearInterval(timer)
  }, [polling, executionJobId])

  const start = async () => {
    setError(null)
    setStarting(true)
    try {
      await readJson(await apiFetch(`/api/agent3/executions/${executionJobId}/deliver`, { method: 'POST' }))
      await loadDeliveries()
    } catch (err) {
      if (alive.current) setError(err.message)
    } finally {
      if (alive.current) setStarting(false)
    }
  }

  const blocker = deliverBlocker({ hasOutput, connection, latest })
  const note = parityNote(latest)

  return (
    <section className="deliver-github" aria-label="Deliver to GitHub" style={{ marginTop: 18 }}>
      <h2>Deliver to GitHub</h2>
      {blocker && <p className="reingest-hint">{blocker}</p>}
      {connection?.connected && (
        <p className="reingest-hint">
          Opens a scaffold pull request and one stacked pull request per phase in <code>{connection.repo_full_name}</code>.
          The zip download stays available.
        </p>
      )}
      <PrimaryButton
        label={latest?.status === 'failed' ? 'Try again' : 'Deliver to GitHub'}
        type="button"
        disabled={Boolean(blocker) || polling}
        loading={starting}
        onClick={start}
      />
      {latest?.status === 'failed' && <p className="reingest-hint">Steps already completed are reused when you try again.</p>}
      {error && <p className="error" role="alert">{error}</p>}
      {loadError && <p className="error">{loadError}</p>}

      {latest && polling && (
        <p className="summary-loading" role="status">
          Delivering to <code>{latest.repo_full_name}</code>… {latest.prs.length > 0 ? `${latest.prs.length} pull request${latest.prs.length === 1 ? '' : 's'} opened so far.` : ''}
        </p>
      )}
      {latest?.status === 'failed' && latest.error && <p className="error">{latest.error}</p>}

      {latest && latest.prs.length > 0 && (
        <>
          <ol className="deliver-prs">
            {latest.prs.map((pr) => {
              const href = safePrUrl(pr.url)
              return (
                <li key={pr.branch}>
                  {href ? <a href={href} target="_blank" rel="noopener noreferrer">{prLabel(pr)} (#{pr.pr_number})</a> : <span>{prLabel(pr)} (#{pr.pr_number})</span>}
                </li>
              )
            })}
          </ol>
          <p className="reingest-hint">
            These pull requests are stacked: each is based on the one before it, not on the default branch, so review each against its
            own base and merge them in order, using “Create a merge commit”. The CI run on each pull request is the independent check
            on this code; Agent 3's own test verdicts are advisory.
          </p>
        </>
      )}
      {note && <p className="reingest-hint">{note}</p>}
    </section>
  )
}
