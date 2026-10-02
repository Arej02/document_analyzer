// Pure helpers for ReportPage (tasks_report.md Phase 5 frontend pair; contract: design_report.md §9, §9.2).
// No React, so node:test can import it.

import { TIERS } from './reportModel.js'

const PLAN_STATUS_TEXT = { draft: 'draft', in_review: 'in review', approved: 'approved', stale: 'stale', rejected: 'rejected' }

export const OUT_OF_DATE_MESSAGE =
  'This report is out of date: the repository has been analysed again, or its migration plan has changed, since it was generated. '
  + 'Generate a new report from the project page to see the current state.'

const planStatusText = (status) => PLAN_STATUS_TEXT[status] ?? (status ? String(status).replace('_', ' ') : null)

// The header block: what the report is and how reliable it is, in plain words.
export function headerView(report) {
  const tier = TIERS[report.tier]?.label ?? report.tier
  let plan = null
  if (report.tier === 'full' && report.plan_version != null) {
    plan = `Plan v${report.plan_version}, ${planStatusText(report.plan_status ?? report.plan_status_at_generation)}`
    if (report.plan_status && report.plan_status_at_generation && report.plan_status !== report.plan_status_at_generation) {
      plan += ` (was ${planStatusText(report.plan_status_at_generation)} when generated)`
    }
  }
  const flags = []
  if (report.exec_summary_status === 'unverified') flags.push('Executive summary unverified')
  if (report.exec_summary_status === 'withheld') flags.push('Executive summary withheld')
  return {
    tier,
    plan,
    outOfDate: Boolean(report.out_of_date),
    flags,
    generated: report.created_at,
  }
}

// The Download PDF button. A disabled button always says why; a failed PDF can be retried.
export function pdfButtonState(report, { retrying = false } = {}) {
  const status = retrying ? 'pending' : report.pdf_status
  if (status === 'done') return { disabled: false, reason: null, canRetry: false }
  if (status === 'failed') {
    return {
      disabled: true,
      reason: report.has_html ? 'The PDF could not be generated.' : 'The PDF could not be generated, and there is no stored report to retry from.',
      canRetry: Boolean(report.has_html),
    }
  }
  // A pending PDF on a report with no stored HTML is not on its way: it predates stored documents
  // (the worker only ever renders the PDF from stored HTML), so nothing will finish it.
  if (!report.has_html && !retrying) {
    return { disabled: true, reason: 'This report has no stored PDF. It was generated before reports were stored as documents.', canRetry: false }
  }
  return { disabled: true, reason: 'The PDF is still being prepared.', canRetry: false }
}

// Keep polling the report only while the PDF is genuinely on its way.
export const shouldPoll = (report, retrying = false) => retrying || (report?.pdf_status === 'pending' && Boolean(report?.has_html))

// The file name a download should have: the server's, when it sends one.
export function pdfFilename(report, contentDisposition) {
  const match = /filename="?([^";]+)"?/i.exec(contentDisposition || '')
  if (match && /^[\w.-]+$/.test(match[1])) return match[1]
  return `report-${report.tier}-${String(report.id).slice(0, 8)}.pdf`
}

// Share links (design_report.md §9.3). The API returns a path; the full URL needs the app's origin.
export const shareUrl = (origin, sharePath) => `${String(origin).replace(/\/+$/, '')}${sharePath}`

export const SHARE_ONCE_NOTICE = 'This link is shown only now and cannot be shown again. Copy it before you leave this page; if it is lost, create a new link.'

const SHARE_STATUS_TEXT = { active: 'Active', expired: 'Expired', revoked: 'Revoked' }

// One row of the list: only active links offer Revoke.
export function shareLinkView(link) {
  const status = link.status in SHARE_STATUS_TEXT ? link.status : 'active'
  const views = Number(link.access_count) || 0
  return {
    id: link.id,
    status,
    statusText: SHARE_STATUS_TEXT[status],
    canRevoke: status === 'active',
    expires: link.expires_at,
    views: `${views} ${views === 1 ? 'view' : 'views'}`,
    lastAccessed: link.last_accessed_at ?? null,
  }
}

// What the document area shows. Older reports (from before documents were stored) have none.
export function documentState(report, { html, htmlError }) {
  if (!report.has_html) return { kind: 'missing', message: 'This report has no stored document. It was generated before reports were stored as documents; generate a new one from the project page.' }
  if (htmlError) return { kind: 'error', message: htmlError }
  if (html == null) return { kind: 'loading' }
  return { kind: 'ready' }
}
