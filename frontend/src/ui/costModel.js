/**
 * View model for the operator-only LLM cost panel (design.md §7.4). Pure, so it can be tested
 * without React. `report` is the body of a cost endpoint:
 *   { total, by_stage, by_purpose, by_unit?, cache? }
 * where every group has { calls, error_calls, unpriced_calls, tokens, cost_usd }.
 * `cost_usd` is a list-price estimate, and `null` when any successful call had an unknown price.
 */
import { formatInt } from './format.js'

/** `$0.0125`, `$12.40`, or `unknown` when the price of some call isn't known. */
export function formatUsd(value) {
  if (value == null || Number.isNaN(value)) return 'unknown'
  if (value === 0) return '$0.00'
  if (value < 1) return `$${value.toFixed(4)}`
  return `$${value.toFixed(2)}`
}

function costCell(group) {
  return {
    cost: formatUsd(group.cost_usd),
    calls: formatInt(group.calls),
    tokens: formatInt(group.tokens),
    // Unknown cost with some priced calls still deserves an explanation in the table.
    note: group.unpriced_calls > 0 ? `${formatInt(group.unpriced_calls)} unpriced` : null,
    errors: group.error_calls > 0 ? formatInt(group.error_calls) : null,
  }
}

export function buildCostView(report) {
  if (!report || !report.total) return null
  const { total } = report
  return {
    chip: formatUsd(total.cost_usd),
    total: costCell(total),
    unknown: total.cost_usd == null,
    cache: report.cache ? { hits: report.cache.hits, misses: report.cache.misses } : null,
    stageRows: (report.by_stage ?? []).map((r) => ({ key: r.stage ?? 'none', label: r.stage ?? '—', ...costCell(r) })),
    purposeRows: (report.by_purpose ?? []).map((r) => ({ key: r.purpose, label: r.purpose, ...costCell(r) })),
    unitRows: (report.by_unit ?? []).map((r) => ({
      key: r.cluster_id,
      label: r.cluster_id,
      rounds: r.rounds,
      attempts: r.attempts,
      ...costCell(r),
    })),
  }
}
