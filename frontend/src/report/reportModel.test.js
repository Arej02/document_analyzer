import test from 'node:test'
import assert from 'node:assert/strict'
import { buttonState, progressView, reportRow, trackedJob, warnings } from './reportModel.js'

const eligibility = {
  xray: { eligible: true, reason: null },
  full: { eligible: false, reason: 'no migration plan for the latest ingestion yet' },
}

test('buttons are disabled while eligibility is loading, with no reason shown', () => {
  assert.deepEqual(buttonState('xray', null), { disabled: true, reason: null })
})
test('an eligible tier is enabled; an ineligible one says why', () => {
  assert.deepEqual(buttonState('xray', eligibility), { disabled: false, reason: null })
  assert.deepEqual(buttonState('full', eligibility), { disabled: true, reason: 'no migration plan for the latest ingestion yet' })
})
test('submitting disables without a reason; an active job of the tier disables with one', () => {
  assert.deepEqual(buttonState('xray', eligibility, { submitting: true }), { disabled: true, reason: null })
  const active = buttonState('xray', eligibility, { activeTiers: ['xray'] })
  assert.equal(active.disabled, true)
  assert.match(active.reason, /already being generated/)
  assert.equal(buttonState('xray', eligibility, { activeTiers: ['full'] }).disabled, false)
})

test('x-ray row has no plan text', () => {
  const row = reportRow({ id: 'r', tier: 'xray', exec_summary_status: 'verified', pdf_status: 'pending', out_of_date: false })
  assert.deepEqual([row.tier, row.plan, row.outOfDate, row.summaryUnverified, row.pdf], ['Codebase X-ray', null, false, false, 'PDF pending'])
})
test('full row shows plan version and status, and notes a status change since generation', () => {
  const same = reportRow({ tier: 'full', plan_version: 3, plan_status: 'approved', plan_status_at_generation: 'approved' })
  assert.equal(same.plan, 'Plan v3 · approved')
  const changed = reportRow({ tier: 'full', plan_version: 3, plan_status: 'stale', plan_status_at_generation: 'in_review', out_of_date: true })
  assert.equal(changed.plan, 'Plan v3 · stale (was in review)')
  assert.equal(changed.outOfDate, true)
})
test('summary status and unknown pdf status', () => {
  assert.equal(reportRow({ tier: 'xray', exec_summary_status: 'unverified' }).summaryUnverified, true)
  assert.equal(reportRow({ tier: 'xray', exec_summary_status: 'withheld' }).summaryWithheld, true)
  assert.equal(reportRow({ tier: 'xray', pdf_status: 'weird' }).pdf, null)
})

test('tracked job: the one just started wins, else the newest active', () => {
  assert.equal(trackedJob('new', [{ id: 'old' }]), 'new')
  assert.equal(trackedJob(null, [{ id: 'a' }, { id: 'b' }]), 'a')
  assert.equal(trackedJob(null, []), null)
  assert.equal(trackedJob(null, undefined), null)
})

test('progress view: nothing yet is queued; terminal statuses finish', () => {
  assert.equal(progressView(null).status, 'queued')
  assert.equal(progressView({ status: 'running', progress_stage: 'estimate' }).finished, false)
  const done = progressView({ status: 'done', report_id: 'r1' })
  assert.deepEqual([done.finished, done.reportId], [true, 'r1'])
  const failed = progressView({ status: 'failed', progress_stage: 'collect', error: 'boom' })
  assert.deepEqual([failed.finished, failed.error, failed.progressStage], [true, 'boom', 'collect'])
  assert.equal(progressView({ status: 'cancelled' }).finished, true)
})

test('warnings: only warn-level activity, newest few', () => {
  const events = [
    { event_type: 'activity', message: 'fine', data: { level: 'info' } },
    { event_type: 'activity', message: 'w1', data: { level: 'warn' } },
    { event_type: 'stage.started', message: 'x', data: { level: 'warn' } },
    { event_type: 'activity', message: 'w2', data: { level: 'warn' } },
    { event_type: 'activity', message: 'w3', data: { level: 'warn' } },
    { event_type: 'activity', message: 'w4', data: { level: 'warn' } },
  ]
  assert.deepEqual(warnings(events), ['w2', 'w3', 'w4'])
  assert.deepEqual(warnings(undefined), [])
})
