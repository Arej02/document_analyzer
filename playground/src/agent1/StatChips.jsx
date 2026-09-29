import { Boxes, CircleDashed, FileCode, Files, Languages, Package } from 'lucide-react'

const CHIPS = [
  { key: 'files', label: 'Files', Icon: Files },
  { key: 'loc', label: 'Lines of code', Icon: FileCode },
  { key: 'languages', label: 'Languages', Icon: Languages },
  { key: 'frameworks', label: 'Frameworks', Icon: Boxes },
  { key: 'dependencies', label: 'Dependencies', Icon: Package },
]

export default function StatChips({ stats }) {
  return (
    <dl className="stats">
      {CHIPS.map(({ key, label, Icon }) => {
        const s = stats[key]
        return (
          <div key={key} className="stat" data-pending={!s}>
            <dt><Icon size={14} className="icon" aria-hidden="true" />{label}</dt>
            {s ? (
              <>
                <dd className="stat-value tnum">{s.value}</dd>
                <dd className="stat-sub">{s.sub}</dd>
              </>
            ) : (
              <dd className="stat-pending"><CircleDashed size={14} className="icon" aria-hidden="true" />Pending</dd>
            )}
          </div>
        )
      })}
    </dl>
  )
}
