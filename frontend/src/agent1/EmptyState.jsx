import { ScrollText } from 'lucide-react'
import PrimaryButton from '../PrimaryButton'
import { StatePanel } from '../ui/StatePanel.jsx'
import '../ui/ui.css'

/**
 * Empty: the project has no analysis run yet. `onStart` navigates to the ingestion form
 * (the app's start page, `backToForm` in main.jsx); it does not start a run itself.
 */
export function EmptyState({ onStart }) {
  return (
    <StatePanel
      icon={<ScrollText size={20} />}
      title="No analysis has run yet"
      actions={<PrimaryButton label="Start an analysis" onClick={onStart} />}
    >
      Analysis reads your repository without ever executing it, then builds the knowledge base that migration planning uses.
      Choose a repository on the start page to see its progress here.
    </StatePanel>
  )
}
