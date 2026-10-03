// View model for the operator-only internal estimate panel (design_report.md §9.5). Pure, so node:test can import it.
import { formatUsd } from '../ui/costModel.js'

const percent = (share) => `${Math.round((Number(share) || 0) * 100)}%`

export function internalEstimateView(estimate) {
  if (!estimate) return null
  const calibration = estimate.calibration ?? {}
  const hasCost = estimate.ai_cost_p50 != null && estimate.ai_cost_p90 != null
  const basis = estimate.price_basis
  return {
    // Null cost is "nothing priced yet", never zero.
    cost: hasCost
      ? `${formatUsd(Number(estimate.ai_cost_p50))} – ${formatUsd(Number(estimate.ai_cost_p90))}`
      : 'No cost history yet, estimated from defaults',
    hasCost,
    n: `${estimate.n ?? 0} past ${estimate.n === 1 ? 'unit' : 'units'}`,
    lowConfidence: calibration.low_confidence
      ? `Low confidence: ${percent(calibration.unpriced_share)} of past calls had no known price`
      : null,
    unpriced: calibration.unpriced_excluded ? `${calibration.unpriced_excluded} unpriced calls excluded` : null,
    priceBasis: basis ? `${basis.method} in ${basis.currency}${basis.pricing_version ? `, pricing v${basis.pricing_version}` : ''}` : 'No pricing configured',
    buckets: (calibration.buckets ?? []).map((b) => ({
      key: `${b.size}/${b.risk}`,
      label: `${b.size} · ${b.risk}`,
      units: b.units,
      n: b.n,
      source: b.source,
      median: b.median_cost_usd == null ? '—' : formatUsd(Number(b.median_cost_usd)),
    })),
  }
}
