import { isThemePreference, themes, themeStrategies } from './themes.js'

// Adapter: browser APIs stay outside the store and React components.
export function createBrowserThemeAdapter(browser) {
  const key = 'galvanise.theme'
  const media = browser.matchMedia('(prefers-color-scheme: dark)')
  return {
    read() { try { return browser.localStorage.getItem(key) } catch { return null } },
    write(value) { try { browser.localStorage.setItem(key, value) } catch { /* Session-only when storage is unavailable. */ } },
    prefersDark: () => media.matches,
    apply(name) {
      const blocker = browser.document.createElement?.('style')
      if (blocker) {
        blocker.textContent = '*,*::before,*::after{transition:none!important}'
        browser.document.head.appendChild(blocker)
      }
      const root = browser.document.documentElement
      root.dataset.theme = name
      root.classList.toggle('dark', name === 'dark')
      root.classList.toggle('light', name === 'light')
      root.style.colorScheme = name
      for (const [token, value] of Object.entries(themes[name])) root.style.setProperty(`--${token}`, value)
      browser.document.querySelector('meta[name="theme-color"]')?.setAttribute('content', themes[name].background)
      if (blocker) {
        browser.getComputedStyle(root).color
        browser.setTimeout(() => blocker.remove(), 1)
      }
    },
    listen(onSystem, onPreference) {
      const onStorage = (event) => {
        if (event.key === key || event.key === null) onPreference(event.newValue)
      }
      media.addEventListener('change', onSystem)
      browser.addEventListener('storage', onStorage)
      return () => {
        media.removeEventListener('change', onSystem)
        browser.removeEventListener('storage', onStorage)
      }
    },
  }
}

// Observer: stable snapshots and subscriptions integrate with useSyncExternalStore.
export function createThemeStore(adapter) {
  const normalize = (value) => isThemePreference(value) ? value : 'dark'
  let preference = normalize(adapter.read())
  let snapshot
  const listeners = new Set()
  const update = () => {
    const resolved = themeStrategies[preference](adapter.prefersDark())
    if (snapshot?.preference === preference && snapshot?.resolved === resolved) return
    snapshot = Object.freeze({ preference, resolved })
    adapter.apply(resolved)
    listeners.forEach((listener) => listener())
  }
  update()
  let stop
  return {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener)
      if (listeners.size === 1) {
        stop = adapter.listen(update, (value) => { preference = normalize(value); update() })
        preference = normalize(adapter.read())
        update()
      }
      return () => { listeners.delete(listener); if (!listeners.size) { stop?.(); stop = undefined } }
    },
    setPreference(value) {
      if (!isThemePreference(value)) throw new Error(`Unknown theme preference: ${value}`)
      preference = value
      adapter.write(value)
      update()
    },
  }
}
