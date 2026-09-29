import test from 'node:test'
import assert from 'node:assert/strict'
import { openQuestions } from './openQuestionsModel.js'

const plan = {
  open_facade_flags: 3,
  phases: [
    { phase_cluster_id: 'p1', phase_order: 1, label: 'Core', units: [{ cluster_id: 'u1', unit_order: 1, label: 'Auth' }, { cluster_id: 'u2', unit_order: 2, label: '' }] },
    { phase_cluster_id: 'p2', phase_order: 2, label: null, units: [] },
  ],
  facade_cycles: [
    { level: 'unit', scc_id: 7, cluster_ids: ['u1', 'u2'] },
    { level: 'phase', scc_id: null, cluster_ids: ['p1', 'p2', 'deadbeef-0000'] },
  ],
}

test('cards resolve members to unit and phase labels, with fallbacks', () => {
  const { cards } = openQuestions(plan)
  assert.deepEqual(cards[0].members.map((m) => [m.kind, m.label]), [['unit', 'Auth'], ['unit', 'Unit 1.2']])
  assert.deepEqual(cards[1].members.map((m) => [m.kind, m.label]), [['phase', 'Phase 1 · Core'], ['phase', 'Phase 2'], ['unknown', 'deadbeef']])
})
test('a cycle without an id cannot be acknowledged by id', () => {
  const { cards } = openQuestions(plan)
  assert.deepEqual(cards.map((c) => [c.key, c.acknowledgeable]), [['unit:7', true], ['phase:null', false]])
})
test('flags not listed as cycles are counted, never negative', () => {
  assert.equal(openQuestions(plan).unlisted, 1)
  assert.equal(openQuestions({ ...plan, open_facade_flags: 0 }).unlisted, 0)
  assert.deepEqual(openQuestions(null), { cards: [], openFlags: 0, unlisted: 0 })
})
