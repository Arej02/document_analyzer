import test from 'node:test'
import assert from 'node:assert/strict'
import { PLANNING_PHASES, planningPhases } from './planningPhases.js'

const statuses = (job) => planningPhases(job).phases.map((p) => p.status)

test('the phase list is the four planner stages, without a synthetic done step', () => {
  assert.deepEqual(PLANNING_PHASES.map((p) => p.key), ['graph', 'cluster', 'report', 'order'])
})
test('queued, or running before a stage is recorded: everything waits', () => {
  assert.deepEqual(statuses({ status: 'queued', progress_stage: null }), ['queued', 'queued', 'queued', 'queued'])
  assert.deepEqual(statuses({ status: 'running', progress_stage: null }), ['queued', 'queued', 'queued', 'queued'])
  assert.deepEqual(statuses(null), ['queued', 'queued', 'queued', 'queued'])
})
test('running: earlier stages complete, the current one in progress, the rest queued', () => {
  assert.deepEqual(statuses({ status: 'running', progress_stage: 'report' }), ['completed', 'completed', 'in_progress', 'queued'])
})
test('failed marks the stage it failed in, done completes all', () => {
  assert.deepEqual(statuses({ status: 'failed', progress_stage: 'cluster' }), ['completed', 'failed', 'queued', 'queued'])
  assert.deepEqual(statuses({ status: 'failed', progress_stage: null }), ['queued', 'queued', 'queued', 'queued'])
  assert.deepEqual(statuses({ status: 'done', progress_stage: 'order' }), ['completed', 'completed', 'completed', 'completed'])
})
test('progress is whole stages: no fraction for the running stage, pct is completed over total', () => {
  const p = planningPhases({ status: 'running', progress_stage: 'report' })
  assert.deepEqual(p.phases.map((x) => x.progress), [1, 1, 0, 0])
  assert.deepEqual([p.completed, p.total, p.pct], [2, 4, 50])
  assert.equal(planningPhases({ status: 'done' }).pct, 100)
  assert.ok(planningPhases({ status: 'running', progress_stage: 'graph' }).phases.every((x) => x.weight === 1))
})
