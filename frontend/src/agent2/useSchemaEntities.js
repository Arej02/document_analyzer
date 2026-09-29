// Agent 1 `schema` entities of the plan's source job (read-only, design_agent2.md §16 decision 4).
import { useEffect, useState } from 'react'
import { useApiFetch } from '../AuthProvider.jsx'
import { PAGE_LIMIT } from './dataLayerModel.js'

export function useSchemaEntities(sourceJobId) {
  const apiFetch = useApiFetch()
  const [state, setState] = useState({ data: null, error: null })

  useEffect(() => {
    let cancelled = false
    setState({ data: null, error: null })
    apiFetch(`/api/agent1/jobs/${sourceJobId}/entities?entity_type=schema&limit=${PAGE_LIMIT}`)
      .then((response) => {
        if (!response.ok) throw new Error(`Request failed (${response.status})`)
        return response.json()
      })
      .then((data) => { if (!cancelled) setState({ data, error: null }) })
      .catch((err) => { if (!cancelled) setState({ data: null, error: err.message }) })
    return () => { cancelled = true }
    // apiFetch is a new function every render; the job id is the real dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceJobId])

  return state
}
