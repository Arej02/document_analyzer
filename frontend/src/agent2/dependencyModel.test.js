import test from 'node:test'
import assert from 'node:assert/strict'
import { ariaSort, filterRows, nextSort, sortRows, statusOf, visibleColumns } from './dependencyModel.js'

const rows = [
  { name: 'guzzle', ecosystem: 'composer', version_spec: '^7.0', manifest_path: 'composer.json' },
  { name: 'axios', ecosystem: 'npm', version_spec: '^1.10.0', manifest_path: 'package.json', target_name: 'httpx', grounding_status: 'unverified' },
  { name: 'lodash', ecosystem: 'npm', version_spec: '^1.2.0', target_name: 'none needed', grounding_status: 'verified' },
]

test('only columns with data show, and target brings status with it', () => {
  assert.deepEqual(visibleColumns([{ name: 'a' }]), ['name'])
  assert.deepEqual(visibleColumns(rows.slice(0, 1)), ['name', 'ecosystem', 'version', 'manifest'])
  assert.deepEqual(visibleColumns(rows), ['name', 'ecosystem', 'version', 'manifest', 'target', 'status'])
  assert.deepEqual(visibleColumns([]), ['name'])
})
test('status: no target, unverified target, proposed target', () => {
  assert.deepEqual(rows.map((r) => statusOf(r).key), ['none', 'unverified', 'proposed'])
})
test('filter by ecosystem and status, combined', () => {
  assert.deepEqual(filterRows(rows, { ecosystem: 'npm' }).map((r) => r.name), ['axios', 'lodash'])
  assert.deepEqual(filterRows(rows, { status: 'unverified' }).map((r) => r.name), ['axios'])
  assert.deepEqual(filterRows(rows, { ecosystem: 'composer', status: 'unverified' }), [])
  assert.equal(filterRows(rows).length, 3)
})
test('sort is natural on versions, stable on ties, and does not mutate', () => {
  const copy = [...rows]
  assert.deepEqual(sortRows(rows, { key: 'name' }).map((r) => r.name), ['axios', 'guzzle', 'lodash'])
  assert.deepEqual(sortRows(rows, { key: 'name', dir: 'desc' }).map((r) => r.name), ['lodash', 'guzzle', 'axios'])
  assert.deepEqual(sortRows(rows, { key: 'version' }).map((r) => r.name), ['lodash', 'axios', 'guzzle'])
  assert.deepEqual(sortRows(rows, { key: 'ecosystem' }).map((r) => r.name), ['guzzle', 'axios', 'lodash'])
  assert.deepEqual(rows, copy)
})
test('sort toggling and aria-sort', () => {
  assert.deepEqual(nextSort({ key: 'name', dir: 'asc' }, 'name'), { key: 'name', dir: 'desc' })
  assert.deepEqual(nextSort({ key: 'name', dir: 'desc' }, 'name'), { key: 'name', dir: 'asc' })
  assert.deepEqual(nextSort({ key: 'name', dir: 'desc' }, 'version'), { key: 'version', dir: 'asc' })
  assert.deepEqual([ariaSort({ key: 'name', dir: 'asc' }, 'name'), ariaSort({ key: 'name', dir: 'desc' }, 'name'), ariaSort({ key: 'name', dir: 'asc' }, 'version')], ['ascending', 'descending', 'none'])
})
