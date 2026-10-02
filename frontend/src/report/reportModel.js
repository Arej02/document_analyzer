// Pure helpers for the Assessment Report section of the project page (tasks_report.md
// Phase 4 frontend pair; contract: design_report.md §9.1). No React, so node:test can import it.

// design_report.md §4 stages, plus the synthetic final step the other steppers use.
export const REPORT_STAGES = [
  { key: 'collect', label: 'Collect' },
  { key: 'estimate', label: 'Estimate' },
  { key: 'summarize', label: 'Summary' },
  { key: 'render_html', label: 'Render' },
  { key: 'render_pdf', label: 'PDF' },
  { key: 'publish', label: 'Publish' },
  { key: 'done', label: 'Done' },
]

export const TIERS = {
  xray: { label: 'Codebase X-ray', action: 'Generate X-ray' },
  full: { label: 'Full Assessment', action: 'Generate Full Assessment' },
}

export const ACTIVE_STATUSES = ['queued', 'running']
export const isActiveStatus = (status) => ACTIVE_STATUSES.includes(status)

// What a tier's button shows. `eligibility` is the list endpoint's block, null while loading.
// A job being started or already active disables it too, with the reason stated.
export function buttonState(tier, eligibility, { submitting = false, activeTiers = [] } = {}) {
  const state = eligibility?.[tier]
  if (!state) return { disabled: true, reason: null }
  if (submitting) return { disabled: true, reason: null }
  if (activeTiers.includes(tier)) return { disabled: true, reason: 'A report of this type is already being generated.' }
  return { disabled: !state.eligible, reason: state.eligible ? null : state.reason }
}

// One row of the report list. Plan text is only for the full tier.
export function reportRow(report) {
  const tier = TIERS[report.tier]?.label ?? report.tier
  let plan = null
  if (report.tier === 'full' && report.plan_version != null) {
    const current = report.plan_status ?? report.plan_status_at_generation
    plan = `Plan v${report.plan_version}${current ? ` · ${current.replace('_', ' ')}` : ''}`
    if (report.plan_status && report.plan_status_at_generation && report.plan_status !== report.plan_status_at_generation) {
      plan += ` (was ${report.plan_status_at_generation.replace('_', ' ')})`
    }
  }
  return {
    id: report.id,
    tier,
    plan,
    outOfDate: Boolean(report.out_of_date),
    summaryUnverified: report.exec_summary_status === 'unverified',
    summaryWithheld: report.exec_summary_status === 'withheld',
    pdf: { pending: 'PDF pending', failed: 'PDF failed', done: 'PDF ready' }[report.pdf_status] ?? null,
    createdAt: report.created_at,
  }
}

// The job the page should show progress for: the one just started, else the newest active one.
export function trackedJob(startedJobId, activeJobs) {
  if (startedJobId) return startedJobId
  return activeJobs?.[0]?.id ?? null
}

// Events polling envelope -> what the progress panel renders.
export function progressView(envelope) {
  if (!envelope) return { status: 'queued', progressStage: null, error: null, reportId: null, finished: false }
  return {
    status: envelope.status,
    progressStage: envelope.progress_stage ?? null,
    error: envelope.error ?? null,
    reportId: envelope.report_id ?? null,
    finished: !isActiveStatus(envelope.status),
  }
}

// Latest warning messages from the event stream (e.g. "Executive summary is unverified").
export function warnings(events, max = 3) {
  return (events ?? [])
    .filter((e) => e.event_type === 'activity' && e.data?.level === 'warn' && e.message)
    .map((e) => e.message)
    .slice(-max)
}
