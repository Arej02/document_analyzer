import test from 'node:test'
import assert from 'node:assert/strict'
import { buildModuleRows, couplingHotspots } from './architectureModel.js'

const plan = {
  phases: [
    { phase_order: 1, label: 'Core', units: [{ cluster_id: 'a', unit_order: 1, label: 'Auth', size: 4, risk_level: 'low' }, { cluster_id: 'b', unit_order: 2, label: '', size: 2, risk_level: 'high', grounding_status: 'unverified' }] },
    { phase_order: 2, label: null, units: [{ cluster_id: 'c', unit_order: 1, label: 'Billing', size: 9, risk_level: 'medium' }] },
  ],
}
// b depends on a, c depends on a and b; the a<-c edge is listed twice; c->c is ignored.
const graph = { nodes: [], edges: [
  { from: 'b', to: 'a', weight: 1 }, { from: 'c', to: 'a', weight: 2 }, { from: 'c', to: 'a', weight: 2 },
  { from: 'c', to: 'b', weight: 1, flag: 'needs_facade' }, { from: 'c', to: 'c', weight: 1 },
] }

test('fan-in and fan-out count distinct dependencies, in plan order', () => {
  const rows = buildModuleRows(plan, graph)
  assert.deepEqual(rows.map((r) => [r.clusterId, r.fanIn, r.fanOut]), [['a', 2, 0], ['b', 1, 1], ['c', 0, 2]])
})
test('labels fall back, unverified and facade flags carry through', () => {
  const rows = buildModuleRows(plan, graph)
  assert.equal(rows[1].label, 'Unlabeled unit')
  assert.equal(rows[2].phaseLabel, 'Phase 2')
  assert.deepEqual(rows.map((r) => [r.unverified, r.needsFacade]), [[false, false], [true, true], [false, true]])
})
test('without a graph, coupling is unknown (null), not zero', () => {
  const rows = buildModuleRows(plan, null)
  assert.deepEqual(rows.map((r) => [r.fanIn, r.fanOut]), [[null, null], [null, null], [null, null]])
  assert.deepEqual(buildModuleRows(null, null), [])
})
test('hotspots: most depended-on first, zero fan-in excluded, limited', () => {
  const rows = buildModuleRows(plan, graph)
  assert.deepEqual(couplingHotspots(rows).map((r) => r.clusterId), ['a', 'b'])
  assert.deepEqual(couplingHotspots(rows, 1).map((r) => r.clusterId), ['a'])
})
