import test from 'node:test'
import assert from 'node:assert/strict'
import { applyWorkflowEvents, createInitialWorkflowState } from '../workflow/workflowReducer.js'
import { buildRunView } from './buildRunView.js'

const STAGES = [{ key: 'fetch', label: 'Fetch' }, { key: 'extract', label: 'Extract' }, { key: 'summarize', label: 'Summarize' }]
const at = (s) => new Date(Date.UTC(2026, 0, 1, 0, 0, s)).toISOString()
const ev = (id, event_type, stage = null, data = {}, message = null) => ({ id, event_type, stage, message, data, created_at: at(id * 10) })
const fold = (...events) => applyWorkflowEvents(createInitialWorkflowState(), events)
const started = ev(1, 'workflow.started', null, { stages: STAGES })
const statuses = (view) => view.phases.map((p) => p.status)

test('a queued job with no events falls back to the job stage list, all queued', () => {
  const view = buildRunView({ job: { status: 'queued', progress_stage: null }, events: null, now: 0 })
  assert.equal(view.status, 'queued')
  assert.deepEqual(view.phases.map((p) => p.id), ['extracting', 'summarizing', 'grounding'])
  assert.deepEqual(statuses(view), ['queued', 'queued', 'queued'])
  assert.equal(view.current, null)
  assert.equal(view.elapsed, 0)
})

test('fallback derives phase status from the job row', () => {
  const running = buildRunView({ job: { status: 'running', progress_stage: 'summarizing' } })
  assert.deepEqual(statuses(running), ['completed', 'in_progress', 'queued'])
  assert.deepEqual([running.current.step, running.current.total], [2, 3])
  const done = buildRunView({ job: { status: 'done', progress_stage: null } })
  assert.deepEqual(statuses(done), ['completed', 'completed', 'completed'])
  const failed = buildRunView({ job: { status: 'failed', progress_stage: 'extracting', error: 'boom' } })
  assert.deepEqual(statuses(failed), ['failed', 'queued', 'queued'])
  assert.equal(failed.error, 'boom')
})

test('event stage states map onto queued / in_progress / completed / failed', () => {
  const events = fold(started, ev(2, 'stage.started', 'fetch'), ev(3, 'stage.completed', 'fetch'), ev(4, 'stage.started', 'extract'))
  const view = buildRunView({ job: { status: 'running' }, events, now: Date.parse(at(100)) })
  assert.deepEqual(statuses(view), ['completed', 'in_progress', 'queued'])
  assert.equal(view.current.id, 'extract')
  assert.equal(view.pct, 7) // fetch is 5 of the 70 weight units in this three-stage run
  assert.deepEqual(view.phases.map((p) => p.weight), [5, 20, 45])

  const failedEvents = applyWorkflowEvents(events, [ev(5, 'workflow.failed', 'extract', { error: 'parse error' })])
  const failed = buildRunView({ job: { status: 'failed', error: null }, events: failedEvents })
  assert.deepEqual(statuses(failed), ['completed', 'failed', 'queued'])
  assert.equal(failed.error, 'parse error')
  assert.equal(failed.current.id, 'extract')
})

test('a failed job freezes elapsed at its failure time', () => {
  const events = fold(started, ev(2, 'stage.started', 'fetch'), ev(3, 'workflow.failed', 'fetch', { error: 'x' }))
  const early = buildRunView({ job: { status: 'failed' }, events, now: Date.parse(at(1000)) })
  const late = buildRunView({ job: { status: 'failed' }, events, now: Date.parse(at(9000)) })
  assert.equal(early.elapsed, 20) // started at t=10s, failed at t=30s
  assert.equal(late.elapsed, 20)
})

test('elapsed keeps running while the job runs, and is unknown for a terminal job with no finish event', () => {
  const events = fold(started)
  assert.equal(buildRunView({ job: { status: 'running' }, events, now: Date.parse(at(70)) }).elapsed, 60)
  assert.equal(buildRunView({ job: { status: 'done' }, events, now: Date.parse(at(70)) }).elapsed, null)
})

test('warnings come only from warn-level activity events, and mark their phase', () => {
  const events = fold(
    started,
    ev(2, 'stage.started', 'summarize'),
    ev(3, 'activity', 'summarize', { level: 'info' }, 'fine'),
    ev(4, 'activity', 'summarize', { level: 'warn' }, 'unverified summary'),
    ev(5, 'activity', 'summarize', { level: 'error' }, 'not a warning'),
    ev(6, 'stage.completed', 'summarize'),
  )
  const view = buildRunView({ job: { status: 'running' }, events, now: 0 })
  assert.deepEqual(view.warnings.map((w) => w.message), ['unverified summary'])
  assert.deepEqual(view.warnings.map((w) => w.files), [[]])
  assert.equal(view.phases.find((p) => p.id === 'summarize').status, 'warning')
  assert.equal(view.phases.find((p) => p.id === 'fetch').status, 'queued')
  assert.equal(view.log.entries.length, 3)
})

test('stats pass through and a done job reads 100% with every phase resolved', () => {
  const events = fold(started, ev(2, 'stats.updated', null, { summaries_done: 4 }))
  const view = buildRunView({ job: { status: 'done' }, events, now: 0 })
  assert.deepEqual(view.stats, { summaries_done: 4 })
  assert.equal(view.pct, 100)
  assert.equal(view.startedAt, at(10))
  assert.deepEqual(statuses(view), ['completed', 'completed', 'completed'])
})

test('pct follows stage weights, not stage count', () => {
  const events = fold(started, ev(2, 'stage.completed', 'fetch'), ev(3, 'stage.completed', 'extract'))
  // weights 5 + 20 of 5 + 20 + 45
  assert.equal(buildRunView({ job: { status: 'running' }, events, now: 0 }).pct, 35)
})

test('the ETA is absent before the first completed stage', () => {
  const events = fold(started, ev(2, 'stage.started', 'fetch'))
  const view = buildRunView({ job: { status: 'running' }, events, now: Date.parse(at(500)) })
  assert.equal(view.remaining, null)
  assert.equal(buildRunView({ job: { status: 'queued' }, events: null }).remaining, null)
})

test('the ETA extrapolates from elapsed time and weight, rounded rather than precise', () => {
  const events = fold(started, ev(2, 'stage.completed', 'fetch'), ev(3, 'stage.completed', 'extract'))
  // started at t=10s, now t=110s: 100 s elapsed for 25 of 70 weight -> 180 s left
  const view = buildRunView({ job: { status: 'running' }, events, now: Date.parse(at(110)) })
  assert.equal(view.elapsed, 100)
  assert.equal(view.remaining, 180)
  const odd = buildRunView({ job: { status: 'running' }, events, now: Date.parse(at(113)) })
  assert.equal(odd.remaining % 10, 0)
})

test('done reads zero remaining, failed has no estimate', () => {
  const events = fold(started, ev(2, 'stage.completed', 'fetch'), ev(3, 'workflow.failed', 'extract', { error: 'x' }))
  assert.equal(buildRunView({ job: { status: 'failed' }, events }).remaining, null)
  assert.equal(buildRunView({ job: { status: 'done' }, events: null }).remaining, 0)
})

test('a cancelled run: the open phase is cancelled, elapsed freezes, there is no ETA and it is not an error', () => {
  const events = fold(started, ev(2, 'stage.started', 'fetch'), ev(3, 'stage.completed', 'fetch'), ev(4, 'stage.started', 'extract'), ev(5, 'workflow.cancelled', 'extract'))
  const view = buildRunView({ job: { status: 'cancelled', error: null }, events, now: Date.parse(at(9000)) })
  assert.equal(view.status, 'cancelled')
  assert.deepEqual(statuses(view), ['completed', 'cancelled', 'queued'])
  assert.equal(view.current.id, 'extract')
  assert.equal(view.elapsed, 40) // started at t=10s, cancelled at t=50s
  assert.equal(view.remaining, null)
  assert.equal(view.error, null)

  const fromJob = buildRunView({ job: { status: 'cancelled', progress_stage: 'summarizing' } })
  assert.deepEqual(statuses(fromJob), ['completed', 'cancelled', 'queued'])
})

test('a running phase with progress counts fills its segment and moves pct and the ETA', () => {
  const progress = (id, done, total) => ({ id, event_type: 'progress.updated', stage: 'summarize', message: null, data: { done, total, unit: 'entities', current_path: 'a.php' }, created_at: at(id * 10) })
  const base = [started, ev(2, 'stage.completed', 'fetch'), ev(3, 'stage.completed', 'extract'), ev(4, 'stage.started', 'summarize')]
  const view = buildRunView({ job: { status: 'running' }, events: fold(...base, progress(5, 9, 10)), now: Date.parse(at(110)) })
  const summarize = view.phases.find((p) => p.id === 'summarize')
  assert.equal(summarize.status, 'in_progress')
  assert.equal(summarize.progress, 0.9)
  assert.deepEqual(summarize.detail, { done: 9, total: 10, unit: 'entities', currentPath: 'a.php' })
  // weights 5 + 20 done, 45 * 0.9 = 40.5 of 70
  assert.equal(view.pct, Math.floor(((5 + 20 + 40.5) / 70) * 100))
  assert.ok(view.remaining > 0)

  // Without progress events the phase stays at 0 and has no detail.
  const plain = buildRunView({ job: { status: 'running' }, events: fold(...base), now: 0 })
  assert.equal(plain.phases.find((p) => p.id === 'summarize').progress, 0)
  assert.equal(plain.phases.find((p) => p.id === 'summarize').detail, null)

  // A finished phase drops its detail and reads 1.
  const finished = fold(...base, progress(5, 10, 10), ev(6, 'stage.completed', 'summarize'))
  const done = buildRunView({ job: { status: 'running' }, events: finished, now: 0 }).phases.find((p) => p.id === 'summarize')
  assert.equal(done.progress, 1)
  assert.equal(done.detail, null)
})

test('a warning lists its file refs and ignores other ref kinds', () => {
  const events = fold(
    started,
    ev(2, 'stage.started', 'extract'),
    ev(3, 'activity', 'extract', { level: 'warn', refs: [{ kind: 'file', value: 'a.py' }, { kind: 'entity', value: 'Foo' }, { kind: 'file', value: 'b.py' }] }, '2 files were only partly extracted'),
  )
  const [warning] = buildRunView({ job: { status: 'running' }, events, now: 0 }).warnings
  assert.deepEqual(warning.files, ['a.py', 'b.py'])
  assert.equal(warning.stage, 'extract')
})
