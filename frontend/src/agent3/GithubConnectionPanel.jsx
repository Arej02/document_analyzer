import { useEffect, useRef, useState } from 'react'
import { useApiFetch } from '../AuthProvider'
import PrimaryButton from '../PrimaryButton'
import { formatJobErrorMessage } from '../shared.jsx'
import { SETTINGS_PATH, connectBlocker, panelMode } from './githubConnection.js'

// design_agent3.md §15.7, §15.12: connect (or rotate / disconnect) the separate GitHub repo that generated
// code is delivered to as pull requests. GET/PUT/DELETE /api/agent3/projects/{id}/github-connection.
//
// The token is typed into a password field, sent once in the PUT body, and cleared from state as soon as the
// request succeeds. It is never put in a URL, localStorage or an error message, and the API never returns it.
// The backend's own refusal reasons (wrong token, no push access, a repo that already has branches, "Automatically
// delete head branches" off, a delivery still running) are shown verbatim: they are written to be acted on.
export default function GithubConnectionPanel({ projectId }) {
  const apiFetch = useApiFetch()
  const url = `/api/agent3/projects/${projectId}/github-connection`

  const [connection, setConnection] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)
  const [editing, setEditing] = useState(false)
  const [repo, setRepo] = useState('')
  const [token, setToken] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const [warnings, setWarnings] = useState([])
  const [confirmingDisconnect, setConfirmingDisconnect] = useState(false)
  const [disconnecting, setDisconnecting] = useState(false)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  const load = async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const response = await apiFetch(url)
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(formatJobErrorMessage(response.status, body))
      }
      const data = await response.json()
      if (alive.current) setConnection(data)
    } catch (err) {
      if (alive.current) setLoadError(err.message)
    } finally {
      if (alive.current) setLoading(false)
    }
  }

  useEffect(() => { load() }, [projectId])

  const mode = panelMode({ loading, loadError, connection, editing })
  const blocker = connectBlocker({ repo, token })

  const submit = async (event) => {
    event.preventDefault()
    if (blocker || submitting) return
    setError(null)
    setWarnings([])
    setSubmitting(true)
    try {
      const response = await apiFetch(url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo_full_name: repo.trim(), token: token.trim() }),
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(formatJobErrorMessage(response.status, body))
      if (!alive.current) return
      setConnection({ connected: true, repo_full_name: body.repo_full_name, default_branch: body.default_branch })
      setWarnings(body.warnings || [])
      setToken('')
      setRepo('')
      setEditing(false)
    } catch (err) {
      if (alive.current) setError(err.message)
    } finally {
      if (alive.current) setSubmitting(false)
    }
  }

  const disconnect = async () => {
    setError(null)
    setDisconnecting(true)
    try {
      const response = await apiFetch(url, { method: 'DELETE' })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(formatJobErrorMessage(response.status, body))
      }
      if (!alive.current) return
      setConnection({ connected: false })
      setConfirmingDisconnect(false)
      setWarnings([])
    } catch (err) {
      if (alive.current) setError(err.message)
    } finally {
      if (alive.current) setDisconnecting(false)
    }
  }

  const form = (
    <form className="github-connection-form" onSubmit={submit} aria-label="Connect a GitHub repository">
      <label className="github-connection-label" htmlFor="github-repo">Target repository</label>
      <input
        id="github-repo"
        className="token-input"
        placeholder="owner/name"
        value={repo}
        onChange={(e) => setRepo(e.target.value)}
        autoComplete="off"
        spellCheck={false}
        disabled={submitting}
      />
      <label className="github-connection-label" htmlFor="github-token">Access token</label>
      <input
        id="github-token"
        className="token-input"
        type="password"
        placeholder="GitHub access token"
        value={token}
        onChange={(e) => setToken(e.target.value)}
        autoComplete="off"
        spellCheck={false}
        disabled={submitting}
      />
      <p className="reingest-hint github-connection-help">
        Use a <strong>new or empty</strong> repository, not your existing codebase. A fine-grained token limited to that one
        repository works best, with <strong>Contents</strong> and <strong>Pull requests</strong> set to read and write. In the
        repository's settings turn on {SETTINGS_PATH}, so stacked pull requests reach the default branch as you merge them.
        The token is stored encrypted and is never shown again.
      </p>
      <div className="delete-confirm-row">
        <PrimaryButton label={mode === 'rotate' ? 'Save connection' : 'Connect'} type="submit" disabled={Boolean(blocker)} loading={submitting} />
        {mode === 'rotate' && (
          <button type="button" className="secondary-button" onClick={() => { setEditing(false); setError(null); setToken('') }} disabled={submitting}>
            Cancel
          </button>
        )}
      </div>
      {blocker && (repo || token) && <p className="reingest-hint" role="note">{blocker}</p>}
    </form>
  )

  return (
    <section className="reingest-action github-connection" aria-label="GitHub delivery">
      <h2>GitHub delivery</h2>
      {mode === 'loading' && <p className="summary-loading">Loading connection…</p>}
      {mode === 'error' && (
        <>
          <p className="error">{loadError}</p>
          <button type="button" className="secondary-button" onClick={load}>Try again</button>
        </>
      )}
      {mode === 'connect' && (
        <>
          <p className="reingest-hint">
            Deliver generated code to a GitHub repository as pull requests, one per phase, instead of only downloading a zip.
            Not connected.
          </p>
          {form}
        </>
      )}
      {(mode === 'connected' || mode === 'rotate') && (
        <>
          <p className="reingest-hint">
            Connected to <code>{connection.repo_full_name}</code> (default branch <code>{connection.default_branch}</code>).
          </p>
          {mode === 'connected' && !confirmingDisconnect && (
            <div className="delete-confirm-row">
              <button type="button" className="secondary-button" onClick={() => { setEditing(true); setError(null); setWarnings([]) }}>
                Change repository or token
              </button>
              <button type="button" className="danger-button" onClick={() => { setConfirmingDisconnect(true); setError(null) }}>
                Disconnect…
              </button>
            </div>
          )}
          {mode === 'connected' && confirmingDisconnect && (
            <>
              <p className="reingest-hint">
                Removes the stored token. Pull requests already opened on GitHub are not touched.
              </p>
              <div className="delete-confirm-row">
                <button type="button" className="danger-button" onClick={disconnect} disabled={disconnecting}>
                  {disconnecting ? 'Disconnecting…' : 'Yes, disconnect'}
                </button>
                <button type="button" className="secondary-button" onClick={() => { setConfirmingDisconnect(false); setError(null) }} disabled={disconnecting}>
                  Cancel
                </button>
              </div>
            </>
          )}
          {mode === 'rotate' && form}
        </>
      )}
      {warnings.length > 0 && (
        <ul className="github-connection-warnings" role="status">
          {warnings.map((warning) => <li key={warning}>{warning}</li>)}
        </ul>
      )}
      {error && <p className="error">{error}</p>}
    </section>
  )
}
