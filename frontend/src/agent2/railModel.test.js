import test from 'node:test'
import assert from 'node:assert/strict'
import { overallRisk, planMetrics, railModel, reviewProgress } from './railModel.js'

const phases = [
  { label: 'Core', risk_level: 'medium', approval: { status: 'approved' }, units: [{ size: 3 }, { size: 4 }] },
  { label: 'Billing', risk_level: 'high', approval: null, units: [{ size: 10 }] },
  { label: 'Admin', risk_level: 'high', approval: { status: 'rejected' }, units: [] },
]

test('overallRisk is the highest phase risk and names every phase carrying it', () => {
  assert.deepEqual(overallRisk(phases), { level: 'high', carriers: ['Billing', 'Admin'] })
})
test('overallRisk: no phases is low, unknown levels are ignored', () => {
  assert.deepEqual(overallRisk([]), { level: 'low', carriers: [] })
  assert.deepEqual(overallRisk([{ label: 'x', risk_level: null }]), { level: null, carriers: [] })
})
test('reviewProgress counts approved, rejected and pending phases', () => {
  assert.deepEqual(reviewProgress(phases), { approved: 1, rejected: 1, pending: 1, total: 3 })
})
test('planMetrics sums unit sizes and tolerates a sparse plan', () => {
  assert.deepEqual(planMetrics({ phases, open_facade_flags: 2 }), { phases: 3, units: 3, entities: 17, openFacadeFlags: 2 })
  assert.deepEqual(planMetrics({}), { phases: 0, units: 0, entities: 0, openFacadeFlags: 0 })
})
test('railModel bundles the three', () => assert.deepEqual(Object.keys(railModel({ phases })), ['risk', 'review', 'metrics']))
