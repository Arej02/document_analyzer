import test from 'node:test'
import assert from 'node:assert/strict'
import { internalEstimateView } from './internalEstimateModel.js'

const estimate = {
  ai_cost_p50: 12.5,
  ai_cost_p90: 20.25,
  n: 25,
  calibration: {
    unpriced_excluded: 2,
    unpriced_share: 0.07,
    low_confidence: false,
    buckets: [{ size: 'small', risk: 'low', units: 3, n: 25, source: 'historical', median_cost_usd: 0.42 }],
  },
  price_basis: { method: 'markup', currency: 'USD', pricing_version: '1' },
}

test('nothing to show without an estimate', () => {
  assert.equal(internalEstimateView(null), null)
})
test('a priced estimate shows the p50 to p90 range, the evidence and the price basis', () => {
  const view = internalEstimateView(estimate)
  assert.deepEqual([view.hasCost, view.cost], [true, '$12.50 – $20.25'])
  assert.equal(view.n, '25 past units')
  assert.equal(view.unpriced, '2 unpriced calls excluded')
  assert.equal(view.lowConfidence, null)
  assert.equal(view.priceBasis, 'markup in USD, pricing v1')
  assert.deepEqual(view.buckets, [{ key: 'small/low', label: 'small · low', units: 3, n: 25, source: 'historical', median: '$0.4200' }])
})
test('a cold-start estimate says there is no history rather than showing zero', () => {
  const view = internalEstimateView({ ...estimate, ai_cost_p50: null, ai_cost_p90: null, n: 0, price_basis: null })
  assert.equal(view.hasCost, false)
  assert.match(view.cost, /No cost history/)
  assert.equal(view.priceBasis, 'No pricing configured')
})
test('low confidence names the unpriced share', () => {
  const view = internalEstimateView({ ...estimate, calibration: { ...estimate.calibration, low_confidence: true, unpriced_share: 0.31 } })
  assert.match(view.lowConfidence, /31%/)
})
