import { useState } from 'react'
import { Ban } from 'lucide-react'
import { useApiFetch } from '../AuthProvider'
import './agent1.css'

function errorMessage(status, body) {
  const detail = body?.detail
  if (typeof detail === 'string' && detail) return detail
  return `Request failed (${status})`
}

/**
 * Cancel a queued or running analysis: POST /api/agent1/jobs/{id}/cancel behind an inline two-step
 * confirm (no browser confirm()). `onCancelled` runs once the API has accepted it. Render only while
 * the job is queued or running.
 */
export function CancelControl({ jobId, onCancelled }) {
  const apiFetch = useApiFetch()
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const cancel = async () => {
    setBusy(true)
    setError(null)
    try {
      const response = await apiFetch(`/api/agent1/jobs/${jobId}/cancel`, { method: 'POST' })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(errorMessage(response.status, body))
      }
      setConfirming(false)
      onCancelled()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="cancel-control">
      {confirming ? (
        <span className="cancel-confirm" role="group" aria-label="Confirm cancel">
          <button type="button" className="log-btn cancel-danger" autoFocus disabled={busy} onClick={cancel}>
            {busy ? 'Cancelling…' : 'Cancel analysis'}
          </button>
          <button type="button" className="log-btn" disabled={busy} onClick={() => { setConfirming(false); setError(null) }}>
            Keep running
          </button>
        </span>
      ) : (
        <button type="button" className="log-btn cancel-danger" onClick={() => setConfirming(true)}>
          <Ban size={14} className="ui-icon" aria-hidden="true" />Cancel
        </button>
      )}
      {error ? <p className="cancel-error" role="alert">{error}</p> : null}
    </div>
  )
}
