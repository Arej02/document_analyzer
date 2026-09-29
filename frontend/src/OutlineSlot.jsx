// The sidebar has a slot below the repository picker where a page can put its own outline (the plan
// document's sections). The sidebar owns the element, `App` holds it in state and hands it down here,
// and a page renders into it with a portal. `close` closes the drawer on narrow screens after a jump.
// With no slot (no sidebar, or before it mounts) a page falls back to drawing the outline inline.
import { createContext, useContext } from 'react'

export const OutlineSlotContext = createContext({ el: null, close: null })

export function useOutlineSlot() {
  return useContext(OutlineSlotContext)
}
