import test from 'node:test'
import assert from 'node:assert/strict'
import { approvalState, migrationSubs, phaseCards, phaseFiles } from './migrationModel.js'

const plan = {
  phases: [
    { phase_cluster_id: 'p1', phase_order: 1, label: 'Core', risk_level: 'low', approval: { status: 'approved', note: 'ok' }, units: [{ cluster_id: 'u1' }] },
    { phase_cluster_id: 'p2', phase_order: 2, label: null, risk_level: 'high', grounding_status: 'unverified', summary_text: 'Billing.', approval: null, units: [{ cluster_id: 'u2' }, { cluster_id: 'u3' }] },
    { phase_cluster_id: 'p3', phase_order: 3, label: 'Admin', approval: { status: 'weird' }, units: [] },
  ],
}
const graph = { edges: [
  { from: 'p3', to: 'p1' }, { from: 'p3', to: 'p2' }, { from: 'p2', to: 'p1' }, { from: 'p2', to: 'p1' },
  { from: 'p2', to: 'p2' }, { from: 'p2', to: 'ghost' },
] }

test('cards keep plan order, with label fallbacks and flags', () => {
  const cards = phaseCards(plan, graph)
  assert.deepEqual(cards.map((c) => [c.id, c.label, c.riskLevel, c.unverified, c.summary]), [
    ['p1', 'Core', 'low', false, null], ['p2', 'Phase 2', 'high', true, 'Billing.'], ['p3', 'Admin', null, false, null]])
})
test('dependencies: distinct, ordered by phase, self and unknown edges ignored', () => {
  const [p1, p2, p3] = phaseCards(plan, graph)
  assert.deepEqual(p3.dependsOn.map((d) => d.id), ['p1', 'p2'])
  assert.deepEqual(p2.dependsOn.map((d) => d.id), ['p1'])
  assert.deepEqual(p1.dependedOnBy.map((d) => d.id), ['p2', 'p3'])
  assert.deepEqual(p1.dependsOn, [])
})
test('with no graph, dependencies are unknown (null), not empty', () => {
  const [p1] = phaseCards(plan, null)
  assert.deepEqual([p1.dependsOn, p1.dependedOnBy], [null, null])
  assert.deepEqual(phaseCards(null, null), [])
})
test('approval state: approved with note, none, and an unknown status reads as not reviewed', () => {
  assert.deepEqual(approvalState(plan.phases[0]), { key: 'approved', note: 'ok' })
  assert.deepEqual(approvalState(plan.phases[1]), { key: 'pending', note: null })
  assert.deepEqual(approvalState(plan.phases[2]), { key: 'pending', note: null })
})
test('subsections match the card heading ids', () => {
  assert.deepEqual(migrationSubs(plan), [{ id: 'phase-p1', title: 'Core' }, { id: 'phase-p2', title: 'Phase 2' }, { id: 'phase-p3', title: 'Admin' }])
  assert.equal(phaseCards(plan, null)[0].anchor, 'phase-p1')
})
test('phaseFiles: distinct sorted files, counting unanswered and failed units', () => {
  const units = [{ cluster_id: 'u1' }, { cluster_id: 'u2' }, { cluster_id: 'u3' }]
  const byId = { u1: { detail: { members: [{ file_path: 'b.php' }, { file_path: 'a.php' }] } }, u2: { error: 'x' } }
  assert.deepEqual(phaseFiles(units, byId), { files: ['a.php', 'b.php'], pending: 1, failed: 1 })
})
