// Unit details for the risky units, so risk items can show their factors and files. Fetches at most
// `limit` units, a few at a time, and never fails as a whole: each unit resolves to {detail} or {error}.
import { useEffect, useMemo, useState } from 'react'
import { useApiFetch } from '../AuthProvider.jsx'
import { MAX_DETAILED } from './riskModel.js'

const CONCURRENCY = 4

export function useRiskDetails(planId, items, limit = MAX_DETAILED) {
  const apiFetch = useApiFetch()
  const [byId, setById] = useState({})
  const ids = useMemo(() => items.slice(0, limit).map((i) => i.clusterId), [items, limit])
  const key = ids.join(',')

  useEffect(() => {
    let cancelled = false
    setById({})
    const queue = [...ids]
    const worker = async () => {
      while (queue.length && !cancelled) {
        const id = queue.shift()
        let result
        try {
          const response = await apiFetch(`/api/agent2/plans/${planId}/units/${id}`)
          if (!response.ok) throw new Error(`Request failed (${response.status})`)
          result = { detail: await response.json() }
        } catch (err) {
          result = { error: err.message }
        }
        if (!cancelled) setById((prev) => ({ ...prev, [id]: result }))
      }
    }
    Promise.all(Array.from({ length: Math.min(CONCURRENCY, ids.length) }, worker))
    return () => { cancelled = true }
    // apiFetch is a new function every render; the plan and the unit ids are the real dependencies.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [planId, key])

  return { byId, detailedIds: ids }
}
