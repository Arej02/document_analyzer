import test from 'node:test'
import assert from 'node:assert/strict'
import { buildCostView, formatUsd } from './costModel.js'

const group = (over = {}) => ({ calls: 5, error_calls: 0, unpriced_calls: 0, tokens: 1200, cost_usd: 0.0125, ...over })

test('formatUsd shows amounts under a dollar to four places and larger ones to cents', () => {
  assert.equal(formatUsd(0.0125), '$0.0125')
  assert.equal(formatUsd(12.4), '$12.40')
  assert.equal(formatUsd(0), '$0.00')
})

test('formatUsd says unknown rather than a number when the price is not known', () => {
  assert.equal(formatUsd(null), 'unknown')
  assert.equal(formatUsd(undefined), 'unknown')
})

test('buildCostView returns null for a missing or malformed report', () => {
  assert.equal(buildCostView(null), null)
  assert.equal(buildCostView({}), null)
})

test('buildCostView builds the chip, stage rows and cache counts', () => {
  const view = buildCostView({
    total: group({ error_calls: 1 }),
    by_stage: [{ stage: 'summarize', ...group() }, { stage: 'fold', ...group({ cost_usd: 0.001 }) }],
    by_purpose: [{ purpose: 'entity_summary', ...group() }],
    cache: { hits: 3, misses: 2 },
  })
  assert.equal(view.chip, '$0.0125')
  assert.equal(view.unknown, false)
  assert.equal(view.total.errors, '1')
  assert.deepEqual(view.cache, { hits: 3, misses: 2 })
  assert.deepEqual(view.stageRows.map((r) => [r.label, r.cost]), [['summarize', '$0.0125'], ['fold', '$0.0010']])
  assert.equal(view.purposeRows[0].label, 'entity_summary')
  assert.deepEqual(view.unitRows, [])
})

test('an unknown total is flagged and the unpriced count is surfaced', () => {
  const view = buildCostView({ total: group({ cost_usd: null, unpriced_calls: 2 }), by_stage: [] })
  assert.equal(view.chip, 'unknown')
  assert.equal(view.unknown, true)
  assert.equal(view.total.note, '2 unpriced')
})

test('execution reports expose per-unit rounds and attempts', () => {
  const view = buildCostView({
    total: group(),
    by_stage: [],
    by_unit: [{ cluster_id: 'u1', rounds: 2, attempts: 3, ...group({ cost_usd: 0.1 }) }],
  })
  assert.deepEqual(
    view.unitRows.map((r) => [r.key, r.rounds, r.attempts, r.cost]),
    [['u1', 2, 3, '$0.1000']],
  )
})

test('a stage-less group (Q&A) is labelled with a dash', () => {
  const view = buildCostView({ total: group(), by_stage: [{ stage: null, ...group() }] })
  assert.equal(view.stageRows[0].label, '—')
})
