// Agent 1 run view model (tasks_frontend.md A1.1). Pure: no fetching, timers or DOM.
//
//   job    GET /api/agent1/jobs/{id}: { status, progress_stage, error, ... } or null.
//   events the folded state of workflow/workflowReducer.js (what useWorkflowEvents
//          returns), not the raw event rows.
//   now    epoch milliseconds, passed in so the function stays deterministic.
//
// Phase status vocabulary: queued | in_progress | completed | warning | failed | cancelled.

import { DEFAULT_STAGE_WEIGHT, JOB_PHASES, STAGE_WEIGHTS } from './stages.js'

const FROM_REDUCER = { pending: 'queued', active: 'in_progress', complete: 'completed', failed: 'failed', cancelled: 'cancelled' }
const TERMINAL = ['done', 'failed', 'cancelled']

// A remaining-time estimate is only ever a rough guess, so it is never shown to the second:
// nearest 10 s under ten minutes, nearest minute beyond.
const roundEta = (sec) => (sec < 600 ? Math.round(sec / 10) * 10 : Math.round(sec / 60) * 60)

const toMs = (iso) => {
  const ms = iso ? Date.parse(iso) : NaN
  return Number.isNaN(ms) ? null : ms
}

// Before the first workflow event arrives, the job row alone says where the run is.
function phasesFromJob(job) {
  const current = JOB_PHASES.findIndex((p) => p.key === job?.progress_stage)
  return JOB_PHASES.map((p, i) => {
    let status = 'queued'
    if (job?.status === 'done') status = 'completed'
    else if (current !== -1 && i < current) status = 'completed'
    else if (current !== -1 && i === current) status = { failed: 'failed', cancelled: 'cancelled' }[job?.status] ?? 'in_progress'
    return { id: p.key, label: p.label, status }
  })
}

function phasesFromEvents(stages) {
  return stages.map((s) => ({ id: s.key, label: s.label, status: FROM_REDUCER[s.state] ?? 'queued' }))
}

export function buildRunView({ job = null, events = null, now = Date.now() } = {}) {
  const hasEvents = Boolean(events && events.stages.length)
  const status = job?.status ?? events?.status ?? 'queued'
  const error = job?.error ?? events?.error ?? null

  // Warnings come only from `warn`-level activity events.
  const warnings = (events?.activity.entries ?? [])
    .filter((entry) => entry.level === 'warn')
    .map((entry) => ({
      id: entry.id,
      stage: entry.stage,
      message: entry.message,
      at: entry.created_at,
      // The affected files are the event's `file` refs (design.md §2).
      files: (entry.refs ?? []).filter((ref) => ref?.kind === 'file').map((ref) => ref.value),
    }))
  const warnedStages = new Set(warnings.map((w) => w.stage))

  const phases = (hasEvents ? phasesFromEvents(events.stages) : phasesFromJob(job)).map((p) => {
    // The job row can reach `done` a beat before the event stream is drained.
    const status2 = status === 'done' && p.status !== 'failed' ? 'completed' : p.status
    // A running phase's fraction comes from its progress.updated counts when the worker sends them.
    const detail = events?.progress?.[p.id] ?? null
    const fraction = status2 === 'in_progress' && detail && detail.total > 0 ? Math.min(detail.done / detail.total, 1) : 0
    return {
      ...p,
      weight: STAGE_WEIGHTS[p.id] ?? DEFAULT_STAGE_WEIGHT,
      status: status2 === 'completed' && warnedStages.has(p.id) ? 'warning' : status2,
      progress: status2 === 'completed' ? 1 : fraction,
      detail: status2 === 'in_progress' ? detail : null,
    }
  })

  const active = phases.findIndex((p) => ['in_progress', 'failed', 'cancelled'].includes(p.status))
  const current = active === -1 ? null : { ...phases[active], step: active + 1, total: phases.length }

  const totalWeight = phases.reduce((sum, p) => sum + p.weight, 0)
  const doneWeight = phases.reduce((sum, p) => sum + p.weight * p.progress, 0)
  const fraction = totalWeight ? doneWeight / totalWeight : 0
  const pct = status === 'done' ? 100 : Math.floor(fraction * 100)

  const startedMs = toMs(events?.startedAt)
  const endMs = TERMINAL.includes(status) ? toMs(events?.finishedAt) : now
  const elapsed = startedMs == null ? 0 : endMs == null ? null : Math.max(0, (endMs - startedMs) / 1000)

  // No ETA ("Estimating…") until some progress exists: before that there is nothing to extrapolate from.
  let remaining = null
  if (status === 'done') remaining = 0
  else if (status === 'running' && elapsed != null && fraction > 0 && fraction < 1) remaining = roundEta((elapsed * (1 - fraction)) / fraction)

  return {
    status,
    phases,
    pct,
    startedAt: events?.startedAt ?? null,
    elapsed,
    remaining,
    current,
    stats: events?.stats ?? {},
    log: { entries: events?.activity.entries ?? [], hiddenCount: events?.activity.hiddenCount ?? 0 },
    warnings,
    error,
  }
}
