import test from 'node:test'
import assert from 'node:assert/strict'
import { buildSchemaRows, schemaCounts } from './dataLayerModel.js'

const entities = [
  { id: '1', name: 'zebra', file_path: 'a.php', summary_text: null, risk_level: 'low', grounding_status: 'verified' },
  { id: '2', name: '(dynamic meta at b.php:9)', file_path: 'b.php', summary_text: 'Stores prices.', risk_level: 'high', grounding_status: 'unverified' },
  { id: '3', name: 'apple', file_path: 'a.php', summary_text: '', risk_level: 'low', grounding_status: null },
  { id: '4', name: 'mystery', file_path: 'c.php', risk_level: null },
]

test('rows sort by risk (high first, unknown last) then name', () => {
  const { rows } = buildSchemaRows(entities, 4)
  assert.deepEqual(rows.map((r) => r.id), ['2', '3', '1', '4'])
})
test('notes come from the summary, empty becomes null; flags carry through', () => {
  const { rows } = buildSchemaRows(entities, 4)
  const by = Object.fromEntries(rows.map((r) => [r.id, r]))
  assert.equal(by['2'].note, 'Stores prices.')
  assert.equal(by['3'].note, null)
  assert.deepEqual([by['2'].unverified, by['2'].dynamic, by['1'].unverified, by['1'].dynamic], [true, true, false, false])
})
test('truncated when the API total exceeds what was loaded', () => {
  assert.equal(buildSchemaRows(entities, 250).truncated, true)
  assert.equal(buildSchemaRows(entities, 4).truncated, false)
  assert.deepEqual(buildSchemaRows(null, undefined), { rows: [], total: 0, truncated: false })
})
test('schemaCounts', () => {
  assert.deepEqual(schemaCounts(buildSchemaRows(entities, 4).rows), { total: 4, dynamic: 1, unverified: 1 })
})
