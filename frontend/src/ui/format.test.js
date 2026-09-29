import test from 'node:test'
import assert from 'node:assert/strict'
import { formatClock, formatDuration, formatInt } from './format.js'

test('formatInt rounds and groups thousands', () => {
  assert.equal(formatInt(184306), '184,306')
  assert.equal(formatInt(2.6), '3')
})
test('formatClock switches to h:mm:ss past an hour and clamps negatives', () => {
  assert.equal(formatClock(187), '3:07')
  assert.equal(formatClock(3723), '1:02:03')
  assert.equal(formatClock(-5), '0:00')
})
test('formatDuration uses seconds under a minute, then m ss', () => {
  assert.equal(formatDuration(42.4), '42s')
  assert.equal(formatDuration(187), '3m 07s')
})
