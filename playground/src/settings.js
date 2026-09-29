// Playground settings: the single source of truth for every tweak-bar option.
// `cssVars(settings)` turns a settings object into the --pg-* token set, and is used both to
// style the page and to produce the "Copy config → CSS variables" output, so what you see is
// exactly what gets copied.
import { useCallback, useEffect, useState } from 'react'
import { themes } from './theme/themes.js'

export const SANS = [
  { id: 'geist', label: 'Geist (current theme)', stack: "'Geist Variable', 'Geist', system-ui, sans-serif" },
  { id: 'inter', label: 'Inter', stack: "'Inter Variable', system-ui, sans-serif" },
  { id: 'plex', label: 'IBM Plex Sans', stack: "'IBM Plex Sans', system-ui, sans-serif" },
  { id: 'source', label: 'Source Sans 3', stack: "'Source Sans 3 Variable', system-ui, sans-serif" },
  { id: 'manrope', label: 'Manrope', stack: "'Manrope Variable', system-ui, sans-serif" },
  { id: 'public', label: 'Public Sans', stack: "'Public Sans Variable', system-ui, sans-serif" },
]
export const MONO = [
  { id: 'geist-mono', label: 'Geist Mono (current theme)', stack: "'Geist Mono Variable', 'Geist Mono', ui-monospace, monospace" },
  { id: 'jetbrains', label: 'JetBrains Mono', stack: "'JetBrains Mono Variable', ui-monospace, monospace" },
  { id: 'plex-mono', label: 'IBM Plex Mono', stack: "'IBM Plex Mono', ui-monospace, monospace" },
]
// Accent values are chosen per theme so accent text/icons stay >= 4.5:1 on that theme's surfaces.
export const ACCENTS = [
  { id: 'lavender', label: 'Lavender (theme)', dark: '#a0a1cf', light: '#55569a' },
  { id: 'teal', label: 'Teal (workflow)', dark: 'oklch(0.75 0.1 195)', light: 'oklch(0.48 0.09 195)' },
  { id: 'steel', label: 'Steel blue', dark: '#86aee6', light: '#2b5fa8' },
  { id: 'slate', label: 'Slate', dark: '#b6bdce', light: '#434a5f' },
]
export const RADII = { sharp: 2, subtle: 6, rounded: 10 }
export const DENSITIES = {
  compact: { row: 4, card: 12, gap: 12, gapLg: 16, section: 24 },
  comfortable: { row: 8, card: 16, gap: 16, gapLg: 24, section: 32 },
  spacious: { row: 12, card: 24, gap: 24, gapLg: 32, section: 48 },
}

export const DEFAULTS = {
  screen: 'agent1',
  theme: 'dark',
  sans: 'geist',
  mono: 'geist-mono',
  fontSize: 14,
  lineHeight: 1.5,
  density: 'comfortable',
  accent: 'lavender',
  radius: 'rounded',
  maxWidth: 1200,
  progressStyle: 'segmented',
  sidebarStyle: 'progress',
  a1State: 'live', // live | queued | idle | failed
  a2State: 'ready', // ready | loading | empty | error
  simSpeed: 6,
}
// Keys that belong in the exported theme config (the rest is playground-only state).
export const CONFIG_KEYS = ['theme', 'sans', 'mono', 'fontSize', 'lineHeight', 'density', 'accent', 'radius', 'maxWidth', 'progressStyle', 'sidebarStyle']

const STORAGE_KEY = 'galvanise.playground.v1'
const clampNum = (v, lo, hi, fallback) => (Number.isFinite(+v) ? Math.min(hi, Math.max(lo, +v)) : fallback)

function sanitize(raw) {
  const s = { ...DEFAULTS, ...(raw && typeof raw === 'object' ? raw : {}) }
  const pick = (val, allowed, fb) => (allowed.includes(val) ? val : fb)
  s.screen = pick(s.screen, ['agent1', 'agent2'], DEFAULTS.screen)
  s.theme = pick(s.theme, ['dark', 'light'], DEFAULTS.theme)
  s.sans = pick(s.sans, SANS.map((f) => f.id), DEFAULTS.sans)
  s.mono = pick(s.mono, MONO.map((f) => f.id), DEFAULTS.mono)
  s.density = pick(s.density, Object.keys(DENSITIES), DEFAULTS.density)
  s.accent = pick(s.accent, ACCENTS.map((a) => a.id), DEFAULTS.accent)
  s.radius = pick(s.radius, Object.keys(RADII), DEFAULTS.radius)
  s.progressStyle = pick(s.progressStyle, ['linear', 'segmented', 'ring'], DEFAULTS.progressStyle)
  s.sidebarStyle = pick(s.sidebarStyle, ['minimal', 'numbered', 'progress'], DEFAULTS.sidebarStyle)
  s.a1State = pick(s.a1State, ['live', 'queued', 'idle', 'failed'], DEFAULTS.a1State)
  s.a2State = pick(s.a2State, ['ready', 'loading', 'empty', 'error'], DEFAULTS.a2State)
  s.fontSize = clampNum(s.fontSize, 12, 18, DEFAULTS.fontSize)
  s.lineHeight = clampNum(s.lineHeight, 1.3, 1.8, DEFAULTS.lineHeight)
  s.maxWidth = clampNum(s.maxWidth, 880, 1680, DEFAULTS.maxWidth)
  s.simSpeed = pick(+s.simSpeed, [1, 3, 6, 12, 24], DEFAULTS.simSpeed)
  return s
}

function load() {
  try { return sanitize(JSON.parse(localStorage.getItem(STORAGE_KEY))) } catch { return { ...DEFAULTS } }
}

export function useSettings() {
  const [settings, setSettings] = useState(load)
  useEffect(() => {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(settings)) } catch { /* storage unavailable: run in memory */ }
  }, [settings])
  const update = useCallback((patch) => setSettings((s) => sanitize({ ...s, ...patch })), [])
  const reset = useCallback(() => setSettings((s) => sanitize({ ...DEFAULTS, screen: s.screen })), [])
  return [settings, update, reset]
}

const by = (list, id) => list.find((x) => x.id === id) || list[0]

/** The --pg-* design tokens for a settings object. Spacing scale (4/8) lives in base.css. */
export function cssVars(s) {
  const r = RADII[s.radius]
  const d = DENSITIES[s.density]
  const accent = by(ACCENTS, s.accent)[s.theme]
  const dark = s.theme === 'dark'
  return {
    '--pg-font-sans': by(SANS, s.sans).stack,
    '--pg-font-mono': by(MONO, s.mono).stack,
    '--pg-fs': `${s.fontSize}px`,
    '--pg-lh': String(s.lineHeight),
    '--pg-r-sm': `${Math.max(2, r - 4)}px`,
    '--pg-r': `${r}px`,
    '--pg-r-lg': `${r + 4}px`,
    '--pg-pad-row': `${d.row}px`,
    '--pg-pad-card': `${d.card}px`,
    '--pg-gap': `${d.gap}px`,
    '--pg-gap-lg': `${d.gapLg}px`,
    '--pg-section': `${d.section}px`,
    '--pg-maxw': `${s.maxWidth}px`,
    '--pg-accent': accent,
    '--pg-accent-fg': dark ? '#1d2130' : '#ffffff',
    '--pg-success': dark ? '#7fd6a0' : '#16724a',
  }
}

export function applySettings(s) {
  const root = document.documentElement
  for (const [k, v] of Object.entries(themes[s.theme])) root.style.setProperty(`--${k}`, v)
  for (const [k, v] of Object.entries(cssVars(s))) root.style.setProperty(k, v)
  root.dataset.theme = s.theme
  root.style.colorScheme = s.theme
}

export const configJson = (s) => JSON.stringify(Object.fromEntries(CONFIG_KEYS.map((k) => [k, s[k]])), null, 2)
export const configCss = (s) => `:root[data-theme="${s.theme}"] {\n${Object.entries(cssVars(s)).map(([k, v]) => `  ${k}: ${v};`).join('\n')}\n}`
