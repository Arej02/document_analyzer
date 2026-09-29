import test from 'node:test'
import assert from 'node:assert/strict'
import {
  createInitialWorkflowState,
  workflowReducer,
  applyWorkflowEvents,
  MAX_ACTIVITY_ENTRIES,
} from './workflowReducer.js'

function started(id, stages, created_at = '2026-09-28T00:00:00Z') {
  return { id, event_type: 'workflow.started', stage: null, message: null, data: { stages }, created_at }
}

function stageEvent(id, event_type, stage) {
  return { id, event_type, stage, message: null, data: {}, created_at: `t${id}` }
}

function activity(id, stage, message, data = {}) {
  return { id, event_type: 'activity', stage, message, data, created_at: `t${id}` }
}

function output(id, event_type, data) {
  return { id, event_type, stage: null, message: null, data, created_at: `t${id}` }
}

function stats(id, data) {
  return { id, event_type: 'stats.updated', stage: null, message: null, data, created_at: `t${id}` }
}

const STAGES = [{ key: 'fetch', label: 'Fetch' }, { key: 'fold', label: 'Fold' }]

test('initial state is queued with no stages, empty feed/outputs/stats', () => {
  const state = createInitialWorkflowState()
  assert.equal(state.status, 'queued')
  assert.equal(state.startedAt, null)
  assert.equal(state.finishedAt, null)
  assert.deepEqual(state.stages, [])
  assert.deepEqual(state.activity, { entries: [], hiddenCount: 0 })
  assert.deepEqual(state.outputs, {})
  assert.deepEqual(state.outputOrder, [])
  assert.deepEqual(state.stats, {})
  assert.equal(state.lastEventId, 0)
})

test('workflow.started sets status/startedAt and declares every stage pending', () => {
  const state = workflowReducer(createInitialWorkflowState(), started(1, STAGES))
  assert.equal(state.status, 'running')
  assert.equal(state.startedAt, '2026-09-28T00:00:00Z')
  assert.deepEqual(state.stages, [
    { key: 'fetch', label: 'Fetch', state: 'pending' },
    { key: 'fold', label: 'Fold', state: 'pending' },
  ])
})

test('stage.started then stage.completed transition only the matching stage', () => {
  let state = applyWorkflowEvents(createInitialWorkflowState(), [
    started(1, STAGES),
    stageEvent(2, 'stage.started', 'fetch'),
  ])
  assert.deepEqual(state.stages.map((s) => s.state), ['active', 'pending'])

  state = workflowReducer(state, stageEvent(3, 'stage.completed', 'fetch'))
  assert.deepEqual(state.stages.map((s) => s.state), ['complete', 'pending'])
})

test('workflow.completed resolves every remaining stage to complete', () => {
  // A job with zero entities never opens its last stage at all (worker/run.py only
  // calls emitter.stage("fold") when there were entity_outcomes) — it must not be
  // left "pending" forever once the run is actually done.
  const state = applyWorkflowEvents(createInitialWorkflowState(), [
    started(1, STAGES),
    stageEvent(2, 'stage.started', 'fetch'),
    stageEvent(3, 'stage.completed', 'fetch'),
    { id: 4, event_type: 'workflow.completed', stage: null, message: null, data: {}, created_at: 't4' },
  ])
  assert.equal(state.status, 'done')
  assert.equal(state.finishedAt, 't4')
  assert.deepEqual(state.stages.map((s) => s.state), ['complete', 'complete'])
})

test('workflow.failed marks only the open stage failed and leaves later stages pending', () => {
  const state = applyWorkflowEvents(createInitialWorkflowState(), [
    started(1, STAGES),
    stageEvent(2, 'stage.started', 'fetch'),
    { id: 3, event_type: 'workflow.failed', stage: 'fetch', message: null, data: { error: 'could not clone repo' }, created_at: 't3' },
  ])
  assert.equal(state.status, 'failed')
  assert.equal(state.finishedAt, 't3')
  assert.equal(state.error, 'could not clone repo')
  assert.deepEqual(state.stages, [
    { key: 'fetch', label: 'Fetch', state: 'failed' },
    { key: 'fold', label: 'Fold', state: 'pending' },
  ])
})

test('workflow.failed with no open stage (failed before workflow.started) leaves stages untouched', () => {
  const state = workflowReducer(createInitialWorkflowState(), {
    id: 1, event_type: 'workflow.failed', stage: null, message: null, data: { error: 'bad org id' }, created_at: 't1',
  })
  assert.equal(state.status, 'failed')
  assert.equal(state.error, 'bad org id')
  assert.deepEqual(state.stages, [])
})

test('activity entries carry stage/level/refs, defaulting level to info and refs to []', () => {
  const state = applyWorkflowEvents(createInitialWorkflowState(), [
    activity(1, 'extract', 'PhpExtractor: 3 entities, 2 edges'),
    activity(2, 'summarize', 'foo (app.php): summarized', { level: 'warn', refs: [{ kind: 'entity', value: 'foo' }] }),
  ])
  assert.deepEqual(state.activity.entries, [
    { id: 1, stage: 'extract', message: 'PhpExtractor: 3 entities, 2 edges', level: 'info', refs: [], created_at: 't1' },
    { id: 2, stage: 'summarize', message: 'foo (app.php): summarized', level: 'warn', refs: [{ kind: 'entity', value: 'foo' }], created_at: 't2' },
  ])
  assert.equal(state.activity.hiddenCount, 0)
})

test('activity feed keeps only the most recent MAX_ACTIVITY_ENTRIES and counts the rest as hidden', () => {
  const events = Array.from({ length: MAX_ACTIVITY_ENTRIES + 3 }, (_, i) => activity(i + 1, 'summarize', `event ${i}`))
  const state = applyWorkflowEvents(createInitialWorkflowState(), events)

  assert.equal(state.activity.entries.length, MAX_ACTIVITY_ENTRIES)
  assert.equal(state.activity.hiddenCount, 3)
  assert.equal(state.activity.entries[0].message, 'event 3') // the first 3 were evicted
  assert.equal(state.activity.entries.at(-1).message, `event ${MAX_ACTIVITY_ENTRIES + 2}`)
})

test('output.created then output.updated update the same entry in place, preserving first-seen order', () => {
  let state = applyWorkflowEvents(createInitialWorkflowState(), [
    output(1, 'output.created', { output_id: 'source_snapshot', kind: 'snapshot', label: 'Source snapshot', status: 'pending' }),
    output(2, 'output.created', { output_id: 'module:app.php', kind: 'module_summary', label: 'app.php', status: 'done' }),
  ])
  assert.deepEqual(state.outputOrder, ['source_snapshot', 'module:app.php'])

  state = workflowReducer(state, output(3, 'output.updated', { output_id: 'source_snapshot', kind: 'snapshot', label: 'Source snapshot', status: 'done' }))
  assert.deepEqual(state.outputOrder, ['source_snapshot', 'module:app.php']) // unchanged — updated in place, not re-appended
  assert.deepEqual(state.outputs.source_snapshot, { output_id: 'source_snapshot', kind: 'snapshot', label: 'Source snapshot', status: 'done' })
  assert.equal(Object.keys(state.outputs).length, 2)
})

test('an output event with no output_id is a harmless no-op', () => {
  const state = workflowReducer(createInitialWorkflowState(), output(1, 'output.created', {}))
  assert.deepEqual(state.outputs, {})
  assert.deepEqual(state.outputOrder, [])
})

test('stats.updated events merge into a flat, ever-growing map', () => {
  const state = applyWorkflowEvents(createInitialWorkflowState(), [
    stats(1, { boilerplate_filtered: 3 }),
    stats(2, { evidence_chunks: 42 }),
    stats(3, { boilerplate_filtered: 3, summaries_done: 1, summaries_total: 2 }),
  ])
  assert.deepEqual(state.stats, { boilerplate_filtered: 3, evidence_chunks: 42, summaries_done: 1, summaries_total: 2 })
})

test('events are deduped by id — an old or repeated id is a no-op', () => {
  let state = applyWorkflowEvents(createInitialWorkflowState(), [started(1, STAGES), stageEvent(2, 'stage.started', 'fetch')])
  const beforeReplay = state

  state = workflowReducer(state, stageEvent(2, 'stage.started', 'fetch')) // replayed
  assert.equal(state, beforeReplay) // same reference — no new object was even built

  state = workflowReducer(state, stageEvent(1, 'stage.completed', 'fetch')) // stale id
  assert.equal(state, beforeReplay)
})

test('an unrecognized event_type still advances the cursor without touching other state', () => {
  const state = applyWorkflowEvents(createInitialWorkflowState(), [
    started(1, STAGES),
    { id: 2, event_type: 'something.future', stage: 'fetch', message: 'from a newer backend', data: {}, created_at: 't2' },
  ])
  assert.equal(state.lastEventId, 2)
  assert.deepEqual(state.stages.map((s) => s.state), ['pending', 'pending'])
})

test('applyWorkflowEvents folding a batch matches reducing one event at a time', () => {
  const events = [
    started(1, STAGES),
    stageEvent(2, 'stage.started', 'fetch'),
    activity(3, 'fetch', 'Fetched source'),
    stageEvent(4, 'stage.completed', 'fetch'),
  ]
  const viaBatch = applyWorkflowEvents(createInitialWorkflowState(), events)
  const viaLoop = events.reduce(workflowReducer, createInitialWorkflowState())
  assert.deepEqual(viaBatch, viaLoop)
})

test('applyWorkflowEvents treats a missing/empty events array as a no-op', () => {
  const state = createInitialWorkflowState()
  assert.equal(applyWorkflowEvents(state, []), state)
  assert.equal(applyWorkflowEvents(state, undefined), state)
})

test('workflow.cancelled marks only the open stage cancelled and finishes the run', () => {
  const state = applyWorkflowEvents(createInitialWorkflowState(), [
    started(1, STAGES),
    stageEvent(2, 'stage.started', 'fetch'),
    { id: 3, event_type: 'workflow.cancelled', stage: 'fetch', message: null, data: {}, created_at: 't3' },
  ])
  assert.equal(state.status, 'cancelled')
  assert.equal(state.finishedAt, 't3')
  assert.equal(state.error, null)
  assert.deepEqual(state.stages.map((s) => s.state), ['cancelled', 'pending'])
})

test('progress.updated keeps the latest counts per stage and never goes backwards', () => {
  const progress = (id, stage, done, total = 10, current_path = null) =>
    ({ id, event_type: 'progress.updated', stage, message: null, data: { done, total, unit: 'entities', current_path }, created_at: 't' })
  let state = applyWorkflowEvents(createInitialWorkflowState(), [
    started(1, STAGES),
    progress(2, 'fetch', 3, 10, 'a.php'),
    progress(3, 'fetch', 5, 10, 'b.php'),
    progress(4, 'fetch', 4, 10, 'late.php'), // out of order: ignored
    progress(5, 'other', 1, 2),
  ])
  assert.deepEqual(state.progress.fetch, { done: 5, total: 10, unit: 'entities', currentPath: 'b.php' })
  assert.deepEqual(state.progress.other, { done: 1, total: 2, unit: 'entities', currentPath: null })

  // A malformed event is ignored rather than corrupting state.
  state = applyWorkflowEvents(state, [{ id: 6, event_type: 'progress.updated', stage: 'fetch', data: { done: 'x' }, created_at: 't' }])
  assert.equal(state.progress.fetch.done, 5)
})
