// Migration plan body: one card per phase. Phase approval stays the authoritative action and is
// supplied by the caller (`renderControls`, the existing `PhaseApprovalControls`, only while the plan is
// in review). Units are the reviewable items; `renderUnitExtra` slots the existing override controls
// (merge / move, which make a new plan version) next to each row. This component writes nothing.
import { useState } from 'react'
import { CircleAlert, CircleCheck, CircleDashed, CircleX, Info, TriangleAlert } from 'lucide-react'
import { APPROVAL, phaseCards, phaseFiles } from './migrationModel.js'
import { MAX_FILE_CHIPS } from './riskModel.js'
import { useRiskDetails } from './useRiskDetails.js'
import { formatInt } from '../ui/format.js'
import { Unverified } from './ExecutiveSummary.jsx'
import './agent2.css'

const RISK = {
  high: { label: 'High', tone: 'danger', Icon: TriangleAlert },
  medium: { label: 'Medium', tone: 'warning', Icon: CircleAlert },
  low: { label: 'Low', tone: 'success', Icon: Info },
}
const APPROVAL_ICON = { approved: CircleCheck, rejected: CircleX, pending: CircleDashed }

function PhaseFiles({ planId, units }) {
  const [requested, setRequested] = useState(false)
  // Nothing is fetched until asked: one request per unit.
  const { byId } = useRiskDetails(planId, requested ? units.map((u) => ({ clusterId: u.cluster_id })) : [], Infinity)
  const [showAll, setShowAll] = useState(false)
  if (!units.length) return <span className="a2-note">No units.</span>
  if (!requested) return <button type="button" className="a2-chip-btn" onClick={() => setRequested(true)}>Load affected files</button>
  const { files, pending, failed } = phaseFiles(units, byId)
  const shown = showAll ? files : files.slice(0, MAX_FILE_CHIPS)
  return (
    <div className="a2-files">
      {pending ? <span className="a2-note" role="status">Loading files from {formatInt(pending)} {pending === 1 ? 'unit' : 'units'}.</span> : null}
      {files.length ? (
        <p className="a2-chips" aria-label="Affected files">
          {shown.map((f) => <code key={f} className="a2-chip">{f}</code>)}
          {files.length > shown.length ? <button type="button" className="a2-chip-btn" onClick={() => setShowAll(true)}>+{formatInt(files.length - shown.length)} more</button> : null}
        </p>
      ) : !pending ? <span className="a2-note">No files recorded for these units.</span> : null}
      {failed ? <span className="a2-note">{formatInt(failed)} {failed === 1 ? 'unit' : 'units'} could not be loaded.</span> : null}
    </div>
  )
}

function PhaseRefs({ refs, none }) {
  if (refs == null) return <span className="a2-note">Not available</span>
  if (!refs.length) return <span className="a2-note">{none}</span>
  return <>{refs.map((r) => <span key={r.id} className="a2-tag">{r.order}. {r.label}</span>)}</>
}

function PhaseCard({ card, planId, selectedUnitId, onSelectUnit, renderControls, renderUnitExtra }) {
  const risk = RISK[card.riskLevel]
  const ap = APPROVAL[card.approval.key]
  const ApIcon = APPROVAL_ICON[card.approval.key]
  return (
    <article className="a2-card a2-phase" aria-labelledby={card.anchor}>
      <header className="a2-phase-head">
        <h3 id={card.anchor} className="a2-h3" tabIndex={-1}><span className="a2-sub-path">Phase {card.order}</span>{card.label}</h3>
        <div className="a2-phase-badges">
          {risk ? <span className="ui-pill" data-tone={risk.tone}><risk.Icon size={14} className="ui-icon" aria-hidden="true" />{risk.label} risk</span> : null}
          {card.unverified ? <Unverified /> : null}
          <span className="ui-pill" data-tone={ap.tone}><ApIcon size={14} className="ui-icon" aria-hidden="true" />{ap.label}</span>
        </div>
      </header>
      {card.approval.note ? <p className="a2-note">Reviewer note: “{card.approval.note}”</p> : null}
      <dl className="a2-phase-facts">
        <div><dt>Goal</dt><dd>{card.summary ?? <span className="a2-note">No report is available for this phase.</span>}</dd></div>
        <div><dt>Depends on</dt><dd><PhaseRefs refs={card.dependsOn} none="Nothing, it can start first" /></dd></div>
        <div><dt>Depended on by</dt><dd><PhaseRefs refs={card.dependedOnBy} none="No later phase" /></dd></div>
        <div><dt>Affected files</dt><dd><PhaseFiles planId={planId} units={card.units} /></dd></div>
      </dl>
      <h4 className="a2-h4">Units <span className="a2-num-cell">{formatInt(card.units.length)}</span></h4>
      {card.units.length ? (
        <ol className="a2-unit-list">
          {card.units.map((u, index) => {
            const r = RISK[u.risk_level]
            const label = u.label || `Unit ${u.unit_order}`
            return (
              <li key={u.cluster_id} className="a2-unit" data-selected={u.cluster_id === selectedUnitId}>
                <span className="a2-sub-path">{card.order}.{u.unit_order}</span>
                <button type="button" className="a2-link-btn" aria-current={u.cluster_id === selectedUnitId ? 'true' : undefined} onClick={() => onSelectUnit(u.cluster_id)}>{label}</button>
                {u.flags?.includes('needs_facade') ? <span className="ui-pill" data-tone="danger"><TriangleAlert size={14} className="ui-icon" aria-hidden="true" />Needs facade</span> : null}
                {u.grounding_status === 'unverified' ? <Unverified /> : null}
                <span className="a2-num-cell">{u.size ?? 'n/a'} {u.size === 1 ? 'entity' : 'entities'}</span>
                {r ? <span className="ui-pill" data-tone={r.tone}><r.Icon size={14} className="ui-icon" aria-hidden="true" />{r.label}</span> : null}
                {renderUnitExtra ? renderUnitExtra(u, index, card) : null}
              </li>
            )
          })}
        </ol>
      ) : <p className="a2-note">This phase has no units.</p>}
      {renderControls ? renderControls(card) : null}
    </article>
  )
}

export default function MigrationPlan({ plan, planId, phaseGraph, selectedUnitId, onSelectUnit, renderControls, renderUnitExtra }) {
  const cards = phaseCards(plan, phaseGraph)
  if (!cards.length) return <p className="a2-note">This plan has no phases.</p>
  return (
    <div className="a2-phases">
      {cards.map((c) => (
        <PhaseCard key={c.id} card={c} planId={planId} selectedUnitId={selectedUnitId} onSelectUnit={onSelectUnit} renderControls={renderControls} renderUnitExtra={renderUnitExtra} />
      ))}
    </div>
  )
}
