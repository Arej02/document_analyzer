// The plan's dependency inventory: GET /api/agent2/plans/{id}/dependencies (design_agent2.md §11). Facts from
// Agent 1 plus, where the planner proposed one, a target. `rows` stays null until it loads or when it fails, so
// the section falls back to the declared-package count rather than showing an empty table.
import { useEffect, useState } from 'react'
import { useApiFetch } from '../AuthProvider.jsx'

export function useDependencies(planId) {
  const apiFetch = useApiFetch()
  const [state, setState] = useState({ rows: null, error: null })

  useEffect(() => {
    let cancelled = false
    setState({ rows: null, error: null })
    apiFetch(`/api/agent2/plans/${planId}/dependencies`)
      .then((response) => {
        if (!response.ok) throw new Error(`Request failed (${response.status})`)
        return response.json()
      })
      .then((body) => { if (!cancelled) setState({ rows: body.dependencies ?? [], error: null }) })
      .catch((err) => { if (!cancelled) setState({ rows: null, error: err.message }) })
    return () => { cancelled = true }
    // apiFetch is a new function every render; the plan id is the real dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [planId])

  return state
}
