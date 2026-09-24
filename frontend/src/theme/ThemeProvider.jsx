import { createContext, useContext, useEffect, useSyncExternalStore } from 'react'
import { Moon, Sun } from 'lucide-react'
import { LinkedInLink } from '../showcase/linkedin-link'
const ThemeContext = createContext(null)
export function ThemeProvider({ store, children }) {
  return <ThemeContext.Provider value={store}><ThemeHotkey />{children}</ThemeContext.Provider>
}
export function useTheme() {
  const store = useContext(ThemeContext)
  if (!store) throw new Error('useTheme requires ThemeProvider')
  const theme = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot)
  return { ...theme, setPreference: store.setPreference }
}
function ThemeHotkey() {
  const { resolved, setPreference } = useTheme()
  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.defaultPrevented || event.repeat || event.metaKey || event.ctrlKey || event.altKey) return
      const target = event.target
      if (target instanceof HTMLElement && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))) return
      if (typeof event.key !== 'string' || event.key.toLowerCase() !== 'd') return
      setPreference(resolved === 'dark' ? 'light' : 'dark')
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [resolved, setPreference])
  return null
}
export function ThemeToggle() {
  const { resolved, setPreference } = useTheme()
  const isDark = resolved === 'dark'
  return (
    <div className="showcase-controls fixed top-[26px] right-6 z-50 flex items-center gap-1 sm:right-12">
      <LinkedInLink />
      <button
        type="button"
        aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
        onClick={() => setPreference(isDark ? 'light' : 'dark')}
        className="flex size-9 items-center justify-center rounded-full text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        {isDark ? <Sun className="size-4" /> : <Moon className="size-4" />}
      </button>
    </div>
  )
}
