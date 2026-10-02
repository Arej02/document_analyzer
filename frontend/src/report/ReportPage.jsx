// Assessment report page, `?report=<id>` (tasks_report.md Phase 5 frontend pair; design_report.md §9, §9.2).
// A header (tier, plan status and version, an out-of-date banner, generation flags), the stored HTML
// document in a sandboxed iframe, and a Download PDF button (disabled with a reason while the PDF is
// not ready, retry when it failed).
//
// The document is attacker-influenced text from the analysed repository, rendered and gated server
// side. It is still shown as untrusted: fetched with the user's token and handed to
// `<iframe sandbox="" srcdoc>` with an empty sandbox (no scripts, no same-origin, no forms, no
// popups, no top navigation), so nothing in it can run or reach the app.

import { useEffect, useRef, useState } from 'react'
import { useApiFetch } from '../AuthProvider'
import PrimaryButton from '../PrimaryButton'
import { formatJobErrorMessage } from '../shared.jsx'
import './report.css'
import InternalEstimatePanel from './InternalEstimatePanel.jsx'
import ShareLinks from './ShareLinks.jsx'
import { OUT_OF_DATE_MESSAGE, documentState, headerView, pdfButtonState, pdfFilename, shouldPoll } from './reportPageModel.js'

const POLL_INTERVAL_MS = 3000

async function errorFrom(response) {
  const body = await response.json().catch(() => ({}))
  return new Error(formatJobErrorMessage(response.status, body))
}

export default function ReportPage({ reportId, onBackToProject, onBackToForm, onProjectId }) {
  const [report, setReport] = useState(null)
  const [error, setError] = useState(null)
  const [html, setHtml] = useState(null)
  const [htmlError, setHtmlError] = useState(null)
  const [retrying, setRetrying] = useState(false)
  const [actionError, setActionError] = useState(null)
  const [downloading, setDownloading] = useState(false)
  const apiFetch = useApiFetch()
  const onProjectIdRef = useRef(onProjectId)
  onProjectIdRef.current = onProjectId

  const loadReport = async () => {
    const response = await apiFetch(`/api/reports/${reportId}`)
    if (!response.ok) throw await errorFrom(response)
    return response.json()
  }

  // The report row, once per id.
  useEffect(() => {
    let cancelled = false
    setReport(null)
    setError(null)
    setHtml(null)
    setHtmlError(null)
    setRetrying(false)
    setActionError(null)
    loadReport()
      .then((data) => {
        if (cancelled) return
        setReport(data)
        onProjectIdRef.current?.(data.project_id)
      })
      .catch((err) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [reportId])

  // The stored document, once the report says it has one.
  useEffect(() => {
    if (!report?.has_html) return undefined
    let cancelled = false
    apiFetch(`/api/reports/${reportId}/html`)
      .then(async (response) => {
        if (!response.ok) throw await errorFrom(response)
        return response.text()
      })
      .then((text) => { if (!cancelled) setHtml(text) })
      .catch((err) => { if (!cancelled) setHtmlError(err.message) })
    return () => { cancelled = true }
  }, [reportId, report?.has_html])

  // While the PDF is on its way (or a retry is running), keep the status fresh.
  useEffect(() => {
    if (!shouldPoll(report, retrying)) return undefined
    let cancelled = false
    const timer = setTimeout(async () => {
      try {
        const data = await loadReport()
        if (cancelled) return
        setReport(data)
        if (data.pdf_status !== 'pending') setRetrying(false)
      } catch {
        // a blip: the next effect run polls again
      }
    }, POLL_INTERVAL_MS)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [report, retrying])

  const retry = async () => {
    setActionError(null)
    try {
      const response = await apiFetch(`/api/reports/${reportId}/pdf/retry`, { method: 'POST' })
      if (!response.ok) throw await errorFrom(response)
      setRetrying(true)
      setReport((current) => ({ ...current, pdf_status: 'pending' }))
    } catch (err) {
      setActionError(err.message)
    }
  }

  const download = async () => {
    setActionError(null)
    setDownloading(true)
    try {
      const response = await apiFetch(`/api/reports/${reportId}/pdf`)
      if (!response.ok) throw await errorFrom(response)
      const blob = await response.blob()
      const link = document.createElement('a')
      link.href = URL.createObjectURL(blob)
      link.download = pdfFilename(report, response.headers.get('content-disposition'))
      link.click()
      setTimeout(() => URL.revokeObjectURL(link.href), 10000)
    } catch (err) {
      setActionError(err.message)
    } finally {
      setDownloading(false)
    }
  }

  const header = report ? headerView(report) : null
  const pdf = report ? pdfButtonState(report, { retrying }) : null
  const doc = report ? documentState(report, { html, htmlError }) : null

  return (
    <main className="page-shell">
      <section className="hero report-page" aria-label="Assessment report">
        <button type="button" className="nav-btn" onClick={() => (report ? onBackToProject(report.project_id) : onBackToForm())}>
          <span aria-hidden="true">←</span> {report ? 'Back to project' : 'Back'}
        </button>
        {error && <p className="error" role="alert">{error}</p>}
        {!error && !report && <p className="summary-loading">Loading report…</p>}
        {report && (
          <>
            <p className="eyebrow">ASSESSMENT REPORT</p>
            <h1>{header.tier}</h1>
            <p className="a1-sub">
              {header.plan && <>{header.plan} · </>}Generated {new Date(header.generated).toLocaleString()}
            </p>
            {header.outOfDate && (
              <p className="report-banner" role="status">
                <strong>Out of date.</strong> {OUT_OF_DATE_MESSAGE.replace(/^This report is out of date: /, '')}
              </p>
            )}
            {header.flags.length > 0 && (
              <p className="plan-row-badges report-flags">
                {header.flags.map((flag) => <span key={flag} className="unverified-badge unverified-badge-warn">{flag}</span>)}
              </p>
            )}

            <div className="report-pdf">
              <span title={pdf.reason ?? undefined}>
                <PrimaryButton label="Download PDF" type="button" disabled={pdf.disabled} loading={downloading} onClick={download} />
              </span>
              {pdf.canRetry && <button type="button" className="nav-btn" onClick={retry}>Retry PDF</button>}
              {pdf.reason && <p className="reingest-hint report-reason">{pdf.reason}</p>}
            </div>
            {actionError && <p className="error" role="alert">{actionError}</p>}
            <ShareLinks reportId={reportId} />
          </>
        )}
      </section>

      {report && <InternalEstimatePanel reportId={reportId} />}

      {report && (
        <section className="report-document" aria-label="Report document">
          {doc.kind === 'loading' && <p className="summary-loading">Loading the report document…</p>}
          {(doc.kind === 'missing' || doc.kind === 'error') && <p className={doc.kind === 'error' ? 'error' : 'summary-loading'}>{doc.message}</p>}
          {doc.kind === 'ready' && (
            <iframe className="report-frame" title="Assessment report" sandbox="" srcDoc={html} referrerPolicy="no-referrer" />
          )}
        </section>
      )}
    </main>
  )
}
