// Public share page, `?share=<token>` (tasks_report.md Phase 6 frontend pair; design_report.md §9.4).
// Rendered before the sign-in gate: no session, no Authorization header, no sidebar. It shows the stored
// document in the same empty-sandbox iframe as ReportPage, a "Snapshot as of <date>" heading and a
// Download PDF button. It never shows out-of-date state (the public API doesn't send any), and every
// failure is the one generic message, so the page can't be used to learn why a link doesn't work.

import { useEffect, useState } from 'react'
import PrimaryButton from '../PrimaryButton'
import './report.css'
import { UNAVAILABLE_MESSAGE, publicPdfFilename, snapshotHeading } from './publicReportModel.js'

export default function PublicReportPage({ token }) {
  const [doc, setDoc] = useState(null)
  const [unavailable, setUnavailable] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState(false)

  useEffect(() => {
    let cancelled = false
    fetch(`/api/public/reports/${token}`, { referrerPolicy: 'no-referrer' })
      .then(async (response) => {
        if (!response.ok) throw new Error(String(response.status))
        return {
          html: await response.text(),
          generatedAt: response.headers.get('x-report-generated-at'),
          pdf: response.headers.get('x-report-pdf') === 'available',
        }
      })
      .then((data) => { if (!cancelled) setDoc(data) })
      .catch(() => { if (!cancelled) setUnavailable(true) })
    return () => { cancelled = true }
  }, [token])

  const download = async () => {
    setDownloadError(false)
    setDownloading(true)
    try {
      const response = await fetch(`/api/public/reports/${token}/pdf`, { referrerPolicy: 'no-referrer' })
      if (!response.ok) throw new Error(String(response.status))
      const blob = await response.blob()
      const link = document.createElement('a')
      link.href = URL.createObjectURL(blob)
      link.download = publicPdfFilename(response.headers.get('content-disposition'))
      link.click()
      setTimeout(() => URL.revokeObjectURL(link.href), 10000)
    } catch {
      setDownloadError(true)
    } finally {
      setDownloading(false)
    }
  }

  return (
    <main className="page-shell">
      <section className="hero report-page" aria-label="Shared assessment report">
        {unavailable && <p className="error" role="alert">{UNAVAILABLE_MESSAGE}</p>}
        {!unavailable && !doc && <p className="summary-loading">Loading report…</p>}
        {doc && (
          <>
            <p className="eyebrow">ASSESSMENT REPORT</p>
            <h1>{snapshotHeading(doc.generatedAt)}</h1>
            {doc.pdf && (
              <div className="report-pdf">
                <PrimaryButton label="Download PDF" type="button" loading={downloading} onClick={download} />
              </div>
            )}
            {downloadError && <p className="error" role="alert">The PDF could not be downloaded. Try again later.</p>}
          </>
        )}
      </section>
      {doc && (
        <section className="report-document" aria-label="Report document">
          <iframe className="report-frame" title="Assessment report" sandbox="" srcDoc={doc.html} referrerPolicy="no-referrer" />
        </section>
      )}
    </main>
  )
}
