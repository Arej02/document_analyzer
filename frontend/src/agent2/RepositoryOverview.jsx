// Repository overview body: stack, size, entry points. Subsection ids match the outline
// (`overview-stack`, `overview-size`, `overview-entry`). A part with no data says so; nothing is invented.
import { formatInt } from '../ui/format.js'
import './agent2.css'

const Sub = ({ id, children }) => <h3 id={id} className="a2-h3" tabIndex={-1}>{children}</h3>
const num = (n) => (n == null ? 'Not available' : formatInt(n))

export const OVERVIEW_SUBS = [
  { id: 'overview-stack', title: 'Stack' },
  { id: 'overview-size', title: 'Size' },
  { id: 'overview-entry', title: 'Entry points' },
]

export default function RepositoryOverview({ model, loading = {}, failed = {} }) {
  const { languages, frameworks, size, entryPoints } = model
  return (
    <>
      <Sub id="overview-stack">Stack</Sub>
      {frameworks.length ? <p>Frameworks: {frameworks.join(', ')}.</p> : <p className="a2-note">No framework recognised.</p>}
      {languages.length ? (
        <ul className="a2-lang-list" aria-label="Source files by language">
          {languages.map((l) => (
            <li key={l.name}>
              <span>{l.name}</span>
              <span className="a2-minibar" aria-hidden="true"><span style={{ width: `${Math.max(2, l.pct)}%` }} /></span>
              <span className="a2-num-cell">{formatInt(l.count)} {l.count === 1 ? 'file' : 'files'}, {l.pct.toFixed(1)}%</span>
            </li>
          ))}
        </ul>
      ) : <p className="a2-note">No source languages were recorded for this analysis.</p>}

      <Sub id="overview-size">Size</Sub>
      <dl className="a2-metrics">
        <div><dt>Lines of code</dt><dd>{num(size.loc)}</dd></div>
        <div><dt>Source files</dt><dd>{num(size.sourceFiles)}</dd></div>
        <div><dt>Declared packages</dt><dd>{num(size.dependencies)}</dd></div>
        <div><dt>Extracted entities</dt><dd>{failed.summary ? 'Unavailable' : loading.summary ? 'Loading' : num(size.entities)}</dd></div>
      </dl>

      <Sub id="overview-entry">Entry points</Sub>
      {failed.routes ? <p className="a2-note">Entry points could not be loaded.</p>
        : loading.routes ? <p className="a2-note">Loading entry points.</p>
        : entryPoints.length ? (
          <div className="a2-table-wrap" role="region" aria-label="Entry points (routes)" tabIndex={0}>
            <table className="a2-table">
              <thead><tr><th scope="col">Route</th><th scope="col">File</th></tr></thead>
              <tbody>{entryPoints.map((e) => <tr key={e.id}><th scope="row">{e.name}</th><td><code>{e.filePath}</code></td></tr>)}</tbody>
            </table>
          </div>
        ) : <p className="a2-note">No routes were extracted.</p>}
    </>
  )
}
