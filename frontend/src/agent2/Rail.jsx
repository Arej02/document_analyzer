// Right rail cards: overall risk and open facade flags, review progress (and who approved the plan), key metrics.
// `approval` is "by you at …" on an approved plan, else null. Render it as `PlanDocument`'s
// `rail` (>= 1280px) and again as its `glance` (inline, below 1280px): same content, both places.
import { CircleAlert, CircleCheck, CircleDashed, CircleX, Info, TriangleAlert } from 'lucide-react'
import { railModel } from './railModel.js'
import { formatInt } from '../ui/format.js'
import './agent2.css'

const RISK = {
  high: { label: 'High', tone: 'danger', Icon: TriangleAlert },
  medium: { label: 'Medium', tone: 'warning', Icon: CircleAlert },
  low: { label: 'Low', tone: 'success', Icon: Info },
}

export default function Rail({ plan, approval = null }) {
  const { risk, review, metrics } = railModel(plan)
  const r = risk.level ? RISK[risk.level] : null
  return (
    <div className="a2-glance">
      <section className="a2-card" aria-labelledby="a2-rail-risk">
        <h2 id="a2-rail-risk" className="a2-card-title">Overall risk</h2>
        {r ? (
          <>
            <p><span className="ui-pill" data-tone={r.tone}><r.Icon size={14} className="ui-icon" aria-hidden="true" />{r.label}</span></p>
            <p className="a2-note">{risk.carriers.length ? `Carried by ${risk.carriers.join(', ')}.` : 'No phase carries any risk.'}</p>
          </>
        ) : <p className="a2-note">Not available for this plan.</p>}
        <p>
          <span className="ui-pill" data-tone={metrics.openFacadeFlags ? 'warning' : undefined}>
            {metrics.openFacadeFlags} open facade {metrics.openFacadeFlags === 1 ? 'flag' : 'flags'}
          </span>
        </p>
      </section>

      <section className="a2-card" aria-labelledby="a2-rail-review">
        <h2 id="a2-rail-review" className="a2-card-title">Review progress</h2>
        <div className="a2-stack" role="img" aria-label={`${review.approved} approved, ${review.rejected} rejected, ${review.pending} pending of ${review.total} phases`}>
          <span data-k="approved" style={{ flexGrow: review.approved }} />
          <span data-k="rejected" style={{ flexGrow: review.rejected }} />
          <span data-k="pending" style={{ flexGrow: review.pending }} />
        </div>
        <ul className="a2-rp-list">
          <li><CircleCheck size={14} className="ui-icon" aria-hidden="true" />Approved <span>{review.approved}</span></li>
          <li><CircleX size={14} className="ui-icon" aria-hidden="true" />Rejected <span>{review.rejected}</span></li>
          <li><CircleDashed size={14} className="ui-icon" aria-hidden="true" />Pending <span>{review.pending}</span></li>
        </ul>
        {approval ? <p className="a2-note">Plan approved {approval}.</p> : null}
      </section>

      <section className="a2-card" aria-labelledby="a2-rail-metrics">
        <h2 id="a2-rail-metrics" className="a2-card-title">Plan</h2>
        <dl className="a2-kv">
          <div><dt>Phases</dt><dd>{formatInt(metrics.phases)}</dd></div>
          <div><dt>Units</dt><dd>{formatInt(metrics.units)}</dd></div>
          <div><dt>Entities</dt><dd>{formatInt(metrics.entities)}</dd></div>
        </dl>
      </section>
    </div>
  )
}
