// Loads the Agent 1 side of the plan document (read-only, allowed: design_agent2.md §16 decision 4).
// Three independent reads, so one failing leaves the others showing. `source_job_id` comes from the plan.
import { useEffect, useState } from 'react'
import { useApiFetch } from '../AuthProvider.jsx'
import { useWorkflowEvents } from '../workflow/useWorkflowEvents.js'

export function useRepoOverview(sourceJobId) {
  const apiFetch = useApiFetch()
  const [summary, setSummary] = useState(null)
  const [routes, setRoutes] = useState(null)
  const [commitSha, setCommitSha] = useState(null)
  const [failed, setFailed] = useState({ summary: false, routes: false })
  const { state: events } = useWorkflowEvents(`/api/agent1/jobs/${sourceJobId}/events`)

  useEffect(() => {
    let cancelled = false
    setSummary(null)
    setRoutes(null)
    setCommitSha(null)
    setFailed({ summary: false, routes: false })
    const load = async (path, key, set, pick) => {
      try {
        const response = await apiFetch(path)
        if (!response.ok) throw new Error(String(response.status))
        const data = await response.json()
        if (!cancelled) set(pick(data))
      } catch {
        if (!cancelled) setFailed((f) => ({ ...f, [key]: true }))
      }
    }
    load(`/api/agent1/jobs/${sourceJobId}/summary`, 'summary', setSummary, (d) => d)
    load(`/api/agent1/jobs/${sourceJobId}/entities?entity_type=route&limit=200`, 'routes', setRoutes, (d) => d.entities ?? [])
    // Provenance only: a failure just leaves the commit out of the footer, so it isn't tracked in `failed`.
    apiFetch(`/api/agent1/jobs/${sourceJobId}`)
      .then((response) => (response.ok ? response.json() : null))
      .then((job) => { if (!cancelled) setCommitSha(job?.source_commit_sha ?? null) })
      .catch(() => {})
    return () => { cancelled = true }
    // apiFetch is a new function every render; the job id is the real dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceJobId])

  return { summary, routes, stats: events?.stats ?? {}, commitSha, failed }
}
