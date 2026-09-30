import test from 'node:test'
import assert from 'node:assert/strict'
import { allCollapsed, opened, toggled } from './collapse.js'

test('toggled adds then removes, without mutating the input', () => {
  const a = new Set()
  const b = toggled(a, 'x')
  assert.deepEqual([...a], [])
  assert.deepEqual([...b], ['x'])
  assert.deepEqual([...toggled(b, 'x')], [])
})
test('opened removes only that id, and returns the same set when already open', () => {
  const s = new Set(['x', 'y'])
  assert.deepEqual([...opened(s, 'x')], ['y'])
  assert.equal(opened(s, 'z'), s)
})
test('allCollapsed is false for an empty section list', () => {
  assert.equal(allCollapsed(new Set(), []), false)
  assert.equal(allCollapsed(new Set(['a', 'b']), ['a', 'b']), true)
  assert.equal(allCollapsed(new Set(['a']), ['a', 'b']), false)
})
