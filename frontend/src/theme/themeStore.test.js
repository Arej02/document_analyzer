import test from 'node:test'
import assert from 'node:assert/strict'
import { createThemeStore, createBrowserThemeAdapter } from './themeStore.js'

function fixture(saved = null) {
  let dark = false
  let system, storage
  let stopped = 0
  const applied = []
  const adapter = {
    read: () => saved, write: (value) => { saved = value }, prefersDark: () => dark,
    apply: (name) => applied.push(name),
    listen: (onSystem, onStorage) => { system = onSystem; storage = onStorage; return () => stopped++ },
  }
  return { store: createThemeStore(adapter), applied, saved: () => saved, stopped: () => stopped,
    system: (value) => { dark = value; system() }, storage: (value) => { saved = value; storage(value) } }
}
test('defaults to reference dark theme, persists explicit choice, rejects invalid choices', () => {
  const f = fixture('invalid')
  assert.deepEqual(f.store.getSnapshot(), { preference: 'dark', resolved: 'dark' })
  f.store.setPreference('light')
  assert.equal(f.saved(), 'light')
  assert.equal(f.applied.at(-1), 'light')
  assert.throws(() => f.store.setPreference('invalid'))
})
test('system changes follow only system preference; observers unsubscribe cleanly', () => {
  const f = fixture('system')
  let updates = 0
  const unsubscribe = f.store.subscribe(() => updates++)
  const initial = f.store.getSnapshot()
  f.system(false)
  assert.equal(initial, f.store.getSnapshot())
  f.system(true)
  assert.equal(f.store.getSnapshot().resolved, 'dark')
  f.store.setPreference('light')
  f.system(true)
  assert.equal(f.store.getSnapshot().resolved, 'light')
  assert.equal(updates, 2)
  unsubscribe()
  assert.equal(f.stopped(), 1)
})
test('cross-tab preferences and removal update current appearance', () => {
  const f = fixture()
  const unsubscribe = f.store.subscribe(() => {})
  f.storage('light')
  assert.equal(f.store.getSnapshot().resolved, 'light')
  f.storage(null)
  assert.equal(f.store.getSnapshot().resolved, 'dark')
  unsubscribe()
})
test('blocked storage does not prevent theme changes', () => {
  const properties = new Map()
  const root = { dataset: {}, classList: { toggle() {} }, style: { setProperty: (k, v) => properties.set(k, v) } }
  const browser = {
    matchMedia: () => ({ matches: false }),
    get localStorage() { throw new Error('Blocked') },
    document: { documentElement: root, querySelector: () => null },
  }
  const store = createThemeStore(createBrowserThemeAdapter(browser))
  store.setPreference('light')
  assert.equal(root.dataset.theme, 'light')
  assert.equal(properties.get('--background'), 'oklch(1 0 0)')
  assert.equal(properties.get('--button'), 'oklch(0.205 0 0)')
})
