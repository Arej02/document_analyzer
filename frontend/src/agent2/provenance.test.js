import test from 'node:test'
import assert from 'node:assert/strict'
import { provenance, shortSha, timeAgo } from './provenance.js'

const now = Date.parse('2026-09-29T12:00:00Z')

test('timeAgo picks the largest unit and pluralises', () => {
  assert.equal(timeAgo('2026-09-29T11:59:30Z', now), 'just now')
  assert.equal(timeAgo('2026-09-29T11:59:00Z', now), '1 minute ago')
  assert.equal(timeAgo('2026-09-29T09:00:00Z', now), '3 hours ago')
  assert.equal(timeAgo('2026-09-27T12:00:00Z', now), '2 days ago')
})
test('timeAgo: a bad or future timestamp never throws', () => {
  assert.equal(timeAgo('nope', now), null)
  assert.equal(timeAgo('2026-09-30T00:00:00Z', now), 'just now')
})
test('shortSha keeps 7 hex characters and rejects anything else', () => {
  assert.equal(shortSha('b4288c1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'), 'b4288c1')
  assert.equal(shortSha(null), null)
  assert.equal(shortSha('<script>'), null)
})
test('provenance is null with no data, partial with one piece', () => {
  assert.equal(provenance({}, now), null)
  assert.deepEqual(provenance({ commitSha: 'b4288c1' }, now), { commit: 'b4288c1', ago: null })
  assert.deepEqual(provenance({ generatedAt: '2026-09-29T09:00:00Z' }, now), { commit: null, ago: '3 hours ago' })
})
