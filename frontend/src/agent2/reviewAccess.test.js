import test from 'node:test'
import assert from 'node:assert/strict'
import { reviewAccess } from './reviewAccess.js'

const can = (plan) => { const a = reviewAccess(plan); return [a.canSubmit, a.canDecide, a.canOverride, a.readOnly] }

test('draft: submit and override, not decide', () => assert.deepEqual(can({ status: 'draft' }), [true, false, true, false]))
test('in review: decide and override, not submit', () => assert.deepEqual(can({ status: 'in_review' }), [false, true, true, false]))
test('approved and rejected are read-only', () => {
  assert.deepEqual(can({ status: 'approved' }), [false, false, false, true])
  assert.deepEqual(can({ status: 'rejected' }), [false, false, false, true])
})
test('stale is read-only by flag or by status, whatever else is true', () => {
  assert.deepEqual(can({ status: 'in_review', is_stale: true }), [false, false, false, true])
  assert.deepEqual(can({ status: 'draft', is_stale: true }), [false, false, false, true])
  assert.deepEqual(can({ status: 'stale' }), [false, false, false, true])
  assert.equal(reviewAccess({ status: 'approved', is_stale: true }).banner.key, 'stale')
})
test('a control that cannot act says why; one that can has no reason', () => {
  const draft = reviewAccess({ status: 'draft' })
  assert.equal(draft.reasons.submit, null)
  assert.equal(draft.reasons.override, null)
  assert.match(draft.reasons.decide, /Submit this draft for review before you approve or reject/)
  const review = reviewAccess({ status: 'in_review' })
  assert.match(review.reasons.submit, /already in review/)
  assert.match(reviewAccess({ status: 'approved' }).reasons.decide, /approved and cannot be changed/)
  assert.match(reviewAccess({ status: 'rejected' }).reasons.override, /rejected/)
  assert.match(reviewAccess({ status: 'in_review', is_stale: true }).reasons.decide, /stale/)
})
test('banners: none while in review, one per closed state, none without a plan', () => {
  assert.equal(reviewAccess({ status: 'in_review' }).banner, null)
  assert.deepEqual(['draft', 'approved', 'rejected'].map((status) => reviewAccess({ status }).banner.key), ['draft', 'approved', 'rejected'])
  const none = reviewAccess(null)
  assert.deepEqual([none.state, none.banner, none.readOnly], ['loading', null, true])
})
