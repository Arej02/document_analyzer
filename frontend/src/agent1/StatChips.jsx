import { Boxes, CircleDashed, FileCode, Files, Languages, Package } from 'lucide-react'
import './agent1.css'

const CHIPS = [
  { key: 'files', label: 'Files', Icon: Files },
  { key: 'loc', label: 'Lines of code', Icon: FileCode },
  { key: 'languages', label: 'Languages', Icon: Languages },
  { key: 'frameworks', label: 'Frameworks', Icon: Boxes },
  { key: 'dependencies', label: 'Dependencies', Icon: Package },
]

/** What the run has discovered so far. `chips` is buildStatChips(); a missing key renders as Pending, in words. */
export function StatChips({ chips = {} }) {
  return (
    <dl className="stats">
      {CHIPS.map(({ key, label, Icon }) => {
        const chip = chips[key]
        return (
          <div key={key} className="stat" data-pending={!chip}>
            <dt><Icon size={14} className="ui-icon" aria-hidden="true" />{label}</dt>
            {chip ? (
              <>
                <dd className="stat-value tnum">{chip.value}</dd>
                {chip.sub ? <dd className="stat-sub">{chip.sub}</dd> : null}
              </>
            ) : (
              <dd className="stat-pending"><CircleDashed size={14} className="ui-icon" aria-hidden="true" />Pending</dd>
            )}
          </div>
        )
      })}
    </dl>
  )
}
