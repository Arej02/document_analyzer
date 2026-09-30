// Architecture section body: the existing `PlanGraph` (passed in as `graph`, it still lives in
// main.jsx until A2.5 splits PlanPage out) and a module table. A row opens `PlanUnitDetail` through
// `onSelectUnit`, the same handler the graph uses. Subsection ids match `ARCHITECTURE_SUBS`.
import { CircleAlert, Info, TriangleAlert } from 'lucide-react'
import { couplingHotspots } from './architectureModel.js'
import { formatInt } from '../ui/format.js'
import { Unverified } from './ExecutiveSummary.jsx'
import SystemMap from './SystemMap.jsx'
import './agent2.css'

export const ARCHITECTURE_SUBS = [
  { id: 'architecture-system', title: 'System map' },
  { id: 'architecture-graph', title: 'Dependency graph' },
  { id: 'architecture-modules', title: 'Module map' },
  { id: 'architecture-hotspots', title: 'Most depended-on units' },
]

const RISK = {
  high: { label: 'High', tone: 'danger', Icon: TriangleAlert },
  medium: { label: 'Medium', tone: 'warning', Icon: CircleAlert },
  low: { label: 'Low', tone: 'success', Icon: Info },
}
const Sub = ({ id, children }) => <h3 id={id} className="a2-h3" tabIndex={-1}>{children}</h3>
const count = (n) => (n == null ? 'n/a' : formatInt(n))

function UnitButton({ row, onSelectUnit }) {
  return <button type="button" className="a2-link-btn" onClick={() => onSelectUnit(row.clusterId)}>{row.label}</button>
}

export default function Architecture({ graph, rows, graphError, onSelectUnit, systemMap }) {
  const hotspots = couplingHotspots(rows)
  return (
    <>
      <Sub id="architecture-system">System map</Sub>
      {systemMap ? <SystemMap {...systemMap} onSelectUnit={onSelectUnit} /> : null}

      <Sub id="architecture-graph">Dependency graph</Sub>
      {graph}

      <Sub id="architecture-modules">Module map</Sub>
      {graphError ? <p className="a2-note">Coupling counts could not be loaded ({graphError}). Size and risk are shown.</p> : null}
      {rows.length ? (
        <div className="a2-table-wrap" role="region" aria-label="Migration units with size, risk and coupling" tabIndex={0}>
          <table className="a2-table">
            <thead>
              <tr>
                <th scope="col">Unit</th><th scope="col">Phase</th>
                <th scope="col" className="a2-num-col">Entities</th><th scope="col">Risk</th>
                <th scope="col" className="a2-num-col">Depended on by</th><th scope="col" className="a2-num-col">Depends on</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const risk = RISK[row.riskLevel]
                return (
                  <tr key={row.clusterId}>
                    <th scope="row">
                      <UnitButton row={row} onSelectUnit={onSelectUnit} />
                      <span className="a2-sub-path">{row.phaseOrder}.{row.unitOrder}</span>
                      {row.needsFacade ? <span className="ui-pill" data-tone="danger"><TriangleAlert size={14} className="ui-icon" aria-hidden="true" />Needs facade</span> : null}
                      {row.unverified ? <Unverified /> : null}
                    </th>
                    <td>{row.phaseLabel}</td>
                    <td className="a2-num-col">{count(row.size)}</td>
                    <td>{risk ? <span className="ui-pill" data-tone={risk.tone}><risk.Icon size={14} className="ui-icon" aria-hidden="true" />{risk.label}</span> : 'n/a'}</td>
                    <td className="a2-num-col">{count(row.fanIn)}</td>
                    <td className="a2-num-col">{count(row.fanOut)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : <p className="a2-note">This plan has no migration units.</p>}

      <Sub id="architecture-hotspots">Most depended-on units</Sub>
      {hotspots.length ? (
        <ul className="a2-bullets">
          {hotspots.map((row) => (
            <li key={row.clusterId}><UnitButton row={row} onSelectUnit={onSelectUnit} />: {formatInt(row.fanIn)} {row.fanIn === 1 ? 'unit depends' : 'units depend'} on it.</li>
          ))}
        </ul>
      ) : <p className="a2-note">{graphError ? 'Not available without coupling counts.' : 'No unit is depended on by another.'}</p>}
    </>
  )
}
