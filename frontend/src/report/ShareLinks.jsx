// Share links for one report (tasks_report.md Phase 6 frontend pair; contract: design_report.md §9.3).
// "Copy share link" creates a link and copies it. The token only exists in that one response, so the
// new link stays on screen with a notice that it cannot be shown again; the list below never has it.
// Revoking asks for a second click first.

import { useEffect, useState } from 'react'
import { useApiFetch } from '../AuthProvider'
import { formatJobErrorMessage } from '../shared.jsx'
import { Toast } from '../ui/Toast.jsx'
import { useCopy } from '../ui/useCopy.js'
import { SHARE_ONCE_NOTICE, shareLinkView, shareUrl } from './reportPageModel.js'

async function errorFrom(response) {
  const body = await response.json().catch(() => ({}))
  return new Error(formatJobErrorMessage(response.status, body))
}

const when = (iso) => (iso ? new Date(iso).toLocaleString() : null)

export default function ShareLinks({ reportId }) {
  const apiFetch = useApiFetch()
  const [links, setLinks] = useState(null)
  const [created, setCreated] = useState(null)
  const [creating, setCreating] = useState(false)
  const [confirming, setConfirming] = useState(null)
  const [error, setError] = useState(null)
  const [copyMessage, copy] = useCopy()

  const load = async () => {
    const response = await apiFetch(`/api/reports/${reportId}/share-links`)
    if (!response.ok) throw await errorFrom(response)
    setLinks((await response.json()).share_links)
  }

  useEffect(() => {
    setLinks(null)
    setCreated(null)
    setConfirming(null)
    setError(null)
    load().catch((err) => setError(err.message))
  }, [reportId])

  const create = async () => {
    setError(null)
    setCreating(true)
    try {
      const response = await apiFetch(`/api/reports/${reportId}/share-links`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
      if (!response.ok) throw await errorFrom(response)
      const data = await response.json()
      const url = shareUrl(window.location.origin, data.share_path)
      setCreated({ url, expires: data.expires_at })
      await copy(url, 'Share link copied')
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setCreating(false)
    }
  }

  const revoke = async (id) => {
    setError(null)
    try {
      const response = await apiFetch(`/api/share-links/${id}/revoke`, { method: 'POST' })
      if (!response.ok) throw await errorFrom(response)
      setConfirming(null)
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <section className="report-share" aria-label="Share links">
      <h2>Share this report</h2>
      <button type="button" className="nav-btn" onClick={create} disabled={creating}>
        {creating ? 'Creating link…' : 'Copy share link'}
      </button>
      {created && (
        <div className="report-share-new" role="status">
          <input readOnly value={created.url} aria-label="New share link" onFocus={(e) => e.target.select()} />
          <p className="reingest-hint">{SHARE_ONCE_NOTICE} It expires {when(created.expires)}.</p>
        </div>
      )}
      {error && <p className="error" role="alert">{error}</p>}
      {links && links.length > 0 && (
        <ul className="report-share-list">
          {links.map((link) => {
            const view = shareLinkView(link)
            return (
              <li key={view.id}>
                <span className={view.status === 'active' ? 'risk-level risk-level-low' : 'unverified-badge unverified-badge-warn'}>{view.statusText}</span>
                {' '}Expires {when(view.expires)} · {view.views}
                {view.lastAccessed ? ` · last opened ${when(view.lastAccessed)}` : ''}
                {view.canRevoke && (confirming === view.id ? (
                  <>
                    {' '}<span>Anyone with this link will lose access.</span>{' '}
                    <button type="button" className="nav-btn" onClick={() => revoke(view.id)}>Yes, revoke link</button>
                    <button type="button" className="nav-btn" onClick={() => setConfirming(null)}>Cancel</button>
                  </>
                ) : (
                  <>{' '}<button type="button" className="nav-btn" onClick={() => setConfirming(view.id)}>Revoke link</button></>
                ))}
              </li>
            )
          })}
        </ul>
      )}
      {links && links.length === 0 && <p className="reingest-hint">No share links yet.</p>}
      <Toast message={copyMessage} />
    </section>
  )
}
