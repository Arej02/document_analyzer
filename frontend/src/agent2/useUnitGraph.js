// Whole-plan graph at one level (`unit` for the module table's fan-in / fan-out, `phase` for a phase's
// dependencies), never with a `phase_id`. The drill-down graph inside `PlanGraph` fetches its own copy.
import { useEffect, useState } from 'react'
import { useApiFetch } from '../AuthProvider.jsx'

export function useUnitGraph(planId, level = 'unit') {
  const apiFetch = useApiFetch()
  const [state, setState] = useState({ graph: null, error: null })

  useEffect(() => {
    let cancelled = false
    setState({ graph: null, error: null })
    apiFetch(`/api/agent2/plans/${planId}/graph?level=${level}`)
      .then((response) => {
        if (!response.ok) throw new Error(`Request failed (${response.status})`)
        return response.json()
      })
      .then((graph) => { if (!cancelled) setState({ graph, error: null }) })
      .catch((err) => { if (!cancelled) setState({ graph: null, error: err.message }) })
    return () => { cancelled = true }
    // apiFetch is a new function every render; the plan id is the real dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [planId, level])

  return state
}
