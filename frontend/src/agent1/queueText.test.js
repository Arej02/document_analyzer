import test from 'node:test'
import assert from 'node:assert/strict'
import { queueText } from './queueText.js'

test('says how many jobs are ahead, with the singular and the next-in-line case', () => {
  assert.equal(queueText(0), 'This job is next in line.')
  assert.equal(queueText(1), '1 job is ahead of this one.')
  assert.equal(queueText(4), '4 jobs are ahead of this one.')
})

test('says nothing when the position is unknown', () => {
  for (const value of [null, undefined, -1, 1.5, 'x']) assert.equal(queueText(value), null)
})
