// Floating, collapsible tweak panel. Every control writes straight into the persisted settings.
import { useEffect, useRef, useState } from 'react'
import { Copy, Moon, RotateCcw, SlidersHorizontal, Sun, X } from 'lucide-react'
import { ACCENTS, MONO, SANS, configCss, configJson } from '../settings.js'
import { Segmented, Toast, useCopy } from '../components/shared.jsx'

function Field({ label, hint, children, htmlFor }) {
  return (
    <div className="tw-field">
      <div className="tw-label"><label htmlFor={htmlFor}>{label}</label>{hint ? <span className="tnum">{hint}</span> : null}</div>
      {children}
    </div>
  )
}
const Group = ({ title, children }) => <fieldset className="tw-group"><legend>{title}</legend>{children}</fieldset>

export default function TweakBar({ settings: s, update, reset, onSim }) {
  const [open, setOpen] = useState(false)
  const [msg, copy] = useCopy()
  const panel = useRef(null)
  const trigger = useRef(null)

  useEffect(() => {
    if (!open) return
    const onKey = (e) => { if (e.key === 'Escape') { setOpen(false); trigger.current?.focus() } }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open])

  const a1 = s.screen === 'agent1'
  return (
    <>
      <button ref={trigger} type="button" className="tw-trigger btn" aria-expanded={open} aria-controls="tweak-panel" onClick={() => setOpen((o) => !o)}>
        <SlidersHorizontal size={16} className="icon" aria-hidden="true" />Tweaks
      </button>
      {open ? (
        <div id="tweak-panel" ref={panel} className="tw-panel" role="dialog" aria-label="Design tweaks">
          <header className="tw-head">
            <h2>Design tweaks</h2>
            <button type="button" className="btn btn-ghost icon-btn" aria-label="Close tweaks" onClick={() => { setOpen(false); trigger.current?.focus() }}><X size={16} className="icon" aria-hidden="true" /></button>
          </header>
          <div className="tw-body">
            <Group title="Preview">
              <Segmented name="tw-screen" legend="Screen" value={s.screen} onChange={(v) => update({ screen: v })} options={[{ value: 'agent1', label: 'Agent 1' }, { value: 'agent2', label: 'Agent 2' }]} />
              {a1 ? (
                <Field label="State">
                  <Segmented name="tw-a1state" legend="Agent 1 state" value={s.a1State} onChange={(v) => update({ a1State: v })} options={[{ value: 'live', label: 'Live' }, { value: 'queued', label: 'Queued' }, { value: 'idle', label: 'Empty' }, { value: 'failed', label: 'Error' }]} />
                </Field>
              ) : (
                <Field label="State">
                  <Segmented name="tw-a2state" legend="Agent 2 state" value={s.a2State} onChange={(v) => update({ a2State: v })} options={[{ value: 'ready', label: 'Ready' }, { value: 'loading', label: 'Loading' }, { value: 'empty', label: 'Empty' }, { value: 'error', label: 'Error' }]} />
                </Field>
              )}
              {a1 ? (
                <div className="tw-row">
                  <Field label="Sim speed" htmlFor="tw-speed">
                    <select id="tw-speed" className="tw-select" value={s.simSpeed} onChange={(e) => update({ simSpeed: +e.target.value })}>
                      {[1, 3, 6, 12, 24].map((n) => <option key={n} value={n}>{n}× real time</option>)}
                    </select>
                  </Field>
                  <button type="button" className="btn btn-sm" onClick={() => onSim('restart')}><RotateCcw size={14} className="icon" aria-hidden="true" />Restart</button>
                  <button type="button" className="btn btn-sm" onClick={() => onSim('end')}>Jump to end</button>
                </div>
              ) : null}
            </Group>

            <Group title="Typography">
              <Field label="Font pairing" htmlFor="tw-sans">
                <select id="tw-sans" className="tw-select" value={s.sans} onChange={(e) => update({ sans: e.target.value })}>{SANS.map((f) => <option key={f.id} value={f.id}>{f.label}</option>)}</select>
              </Field>
              <Field label="Monospace" htmlFor="tw-mono">
                <select id="tw-mono" className="tw-select" value={s.mono} onChange={(e) => update({ mono: e.target.value })}>{MONO.map((f) => <option key={f.id} value={f.id}>{f.label}</option>)}</select>
              </Field>
              <Field label="Base size" hint={`${s.fontSize}px`} htmlFor="tw-fs"><input id="tw-fs" type="range" min="12" max="18" step="1" value={s.fontSize} onChange={(e) => update({ fontSize: +e.target.value })} /></Field>
              <Field label="Line height" hint={s.lineHeight.toFixed(2)} htmlFor="tw-lh"><input id="tw-lh" type="range" min="1.3" max="1.8" step="0.05" value={s.lineHeight} onChange={(e) => update({ lineHeight: +e.target.value })} /></Field>
            </Group>

            <Group title="Layout">
              <Field label="Density"><Segmented name="tw-density" legend="Density" value={s.density} onChange={(v) => update({ density: v })} options={[{ value: 'compact', label: 'Compact' }, { value: 'comfortable', label: 'Comfortable' }, { value: 'spacious', label: 'Spacious' }]} /></Field>
              <Field label="Corner radius"><Segmented name="tw-radius" legend="Border radius" value={s.radius} onChange={(v) => update({ radius: v })} options={[{ value: 'sharp', label: 'Sharp' }, { value: 'subtle', label: 'Subtle' }, { value: 'rounded', label: 'Rounded' }]} /></Field>
              <Field label="Content max width" hint={`${s.maxWidth}px`} htmlFor="tw-mw"><input id="tw-mw" type="range" min="880" max="1680" step="40" value={s.maxWidth} onChange={(e) => update({ maxWidth: +e.target.value })} /></Field>
            </Group>

            <Group title="Color">
              <Field label="Mode">
                <Segmented name="tw-theme" legend="Color mode" value={s.theme} onChange={(v) => update({ theme: v })} options={[{ value: 'light', label: 'Light', icon: <Sun size={14} className="icon" aria-hidden="true" /> }, { value: 'dark', label: 'Dark', icon: <Moon size={14} className="icon" aria-hidden="true" /> }]} />
              </Field>
              <Field label="Accent">
                <div className="tw-swatches" role="radiogroup" aria-label="Accent color">
                  {ACCENTS.map((a) => (
                    <label key={a.id} className="tw-swatch" title={a.label}>
                      <input type="radio" name="tw-accent" value={a.id} checked={s.accent === a.id} onChange={() => update({ accent: a.id })} />
                      <span style={{ background: a[s.theme] }} aria-hidden="true" />
                      <span className="tw-swatch-name">{a.label}</span>
                    </label>
                  ))}
                </div>
              </Field>
            </Group>

            {a1 ? (
              <Group title="Agent 1 progress">
                <Segmented name="tw-progress" legend="Progress style" value={s.progressStyle} onChange={(v) => update({ progressStyle: v })} options={[{ value: 'linear', label: 'Linear' }, { value: 'segmented', label: 'Segmented' }, { value: 'ring', label: 'Ring' }]} />
              </Group>
            ) : (
              <Group title="Agent 2 sidebar">
                <Segmented name="tw-sidebar" legend="Sidebar style" value={s.sidebarStyle} onChange={(v) => update({ sidebarStyle: v })} options={[{ value: 'minimal', label: 'Minimal' }, { value: 'numbered', label: 'Numbered' }, { value: 'progress', label: 'Progress' }]} />
              </Group>
            )}
          </div>
          <footer className="tw-foot">
            <button type="button" className="btn btn-primary" onClick={() => copy(configJson(s), 'Config copied as JSON')}><Copy size={14} className="icon" aria-hidden="true" />Copy JSON</button>
            <button type="button" className="btn" onClick={() => copy(configCss(s), 'Config copied as CSS variables')}><Copy size={14} className="icon" aria-hidden="true" />Copy CSS</button>
            <button type="button" className="btn btn-ghost" onClick={reset}>Reset</button>
          </footer>
        </div>
      ) : null}
      <Toast message={msg} />
    </>
  )
}
