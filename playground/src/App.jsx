import { useEffect, useState } from 'react'
import { applySettings, useSettings } from './settings.js'
import { Segmented } from './components/shared.jsx'
import RunningView from './agent1/RunningView.jsx'
import OutputView from './agent2/OutputView.jsx'
import TweakBar from './tweakbar/TweakBar.jsx'

export default function App() {
  const [settings, update, reset] = useSettings()
  const [simCmd, setSimCmd] = useState({ type: null, n: 0 })

  // Apply tokens synchronously enough that the first paint already uses the saved theme.
  useEffect(() => { applySettings(settings) }, [settings])
  applyOnce(settings)

  return (
    <>
      <a href="#content" className="skip-link">Skip to content</a>
      <div className="pg-topbar">
        <span className="pg-topbar-title">Galvanise UI playground</span>
        <Segmented name="screen" legend="Screen" value={settings.screen} onChange={(v) => update({ screen: v })}
          options={[{ value: 'agent1', label: 'Agent 1: Running' }, { value: 'agent2', label: 'Agent 2: Plan' }]} />
        <span className="pg-topbar-note">Mock data only</span>
      </div>
      <div id="content" tabIndex={-1}>
        {settings.screen === 'agent1'
          ? <main><RunningView settings={settings} update={update} simCmd={simCmd} onOpenPlan={() => update({ screen: 'agent2', a2State: 'ready' })} /></main>
          : <OutputView settings={settings} update={update} />}
      </div>
      <TweakBar settings={settings} update={update} reset={reset} onSim={(type) => setSimCmd((c) => ({ type, n: c.n + 1 }))} />
    </>
  )
}

let applied = false
function applyOnce(settings) {
  if (applied) return
  applied = true
  applySettings(settings)
}
