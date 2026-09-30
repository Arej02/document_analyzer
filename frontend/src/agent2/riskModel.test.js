import test from 'node:test'
import assert from 'node:assert/strict'
import { MAX_FILE_CHIPS, riskDetail, riskItems, severityCounts } from './riskModel.js'

const plan = {
  phases: [
    { phase_order: 1, label: 'Core', units: [
      { cluster_id: 'a', unit_order: 1, label: 'Auth', risk_level: 'medium' },
      { cluster_id: 'b', unit_order: 2, label: 'Users', risk_level: 'low' },
    ] },
    { phase_order: 2, label: null, units: [
      { cluster_id: 'c', unit_order: 1, label: '', risk_level: 'high', grounding_status: 'unverified' },
      { cluster_id: 'd', unit_order: 2, label: 'Cart', risk_level: 'high' },
      { cluster_id: 'e', unit_order: 3, label: 'X', risk_level: null },
    ] },
  ],
}

test('items are medium and high units, high first, plan order within a level', () => {
  assert.deepEqual(riskItems(plan).map((i) => [i.clusterId, i.level]), [['c', 'high'], ['d', 'high'], ['a', 'medium']])
})
test('items carry the phase that migrates them, label fallbacks and the unverified flag', () => {
  const [c] = riskItems(plan)
  assert.deepEqual([c.label, c.phaseLabel, c.phaseOrder, c.unitOrder, c.unverified], ['Unlabeled unit', 'Phase 2', 2, 1, true])
})
test('severity counts include low and ignore unknown levels', () => {
  assert.deepEqual(severityCounts(plan), { high: 2, medium: 1, low: 1 })
  assert.deepEqual(severityCounts(null), { high: 0, medium: 0, low: 0 })
})
test('detail: factors and files are distinct, sorted, merged units use constituent reports', () => {
  const d = riskDetail({
    report: null,
    constituent_reports: [{ risk_factors: ['pay()', 'tax()'] }, { risk_factors: ['pay()'] }, { risk_factors: null }],
    members: [{ file_path: 'b.php' }, { file_path: 'a.php' }, { file_path: 'a.php' }, { file_path: null }],
  })
  assert.deepEqual(d.factors, ['pay()', 'tax()'])
  assert.deepEqual(d.files, ['a.php', 'b.php'])
})
test('detail: file chips are capped with a hidden count; empty detail is safe', () => {
  const members = Array.from({ length: MAX_FILE_CHIPS + 3 }, (_, i) => ({ file_path: `f${String(i).padStart(2, '0')}.php` }))
  const d = riskDetail({ report: { risk_factors: [] }, members })
  assert.equal(d.visibleFiles.length, MAX_FILE_CHIPS)
  assert.equal(d.hiddenFiles, 3)
  assert.deepEqual(riskDetail(null), { factors: [], files: [], visibleFiles: [], hiddenFiles: 0 })
})
