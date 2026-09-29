# Galvanise UI playground

Standalone prototype for the Agent 1 (running) and Agent 2 (plan) screens. Mock data only. It imports nothing from `frontend/`, so the existing app is untouched.

```
cd playground
npm install
npm run dev
```

Opens on http://localhost:5199. Use the segmented control in the top bar to switch screens and the **Tweaks** button (bottom right) to change fonts, size, density, accent, radius, mode, max width and the per-screen variants. **Copy JSON** / **Copy CSS** export the current choices. Settings persist in `localStorage` (`galvanise.playground.v1`); plan review state persists under `galvanise.playground.review.v1`.

## Layout

- `src/settings.js`: every tweak option, the `--pg-*` token derivation and config export
- `src/theme/themes.js`: copy of the app's theme tokens (light and dark)
- `src/styles/base.css`: token scale, buttons, pills, chips, states
- `src/agent1/`: `runModel.js` (deterministic mock run), `RunningView`, `ProgressViz`, `PhaseTimeline`, `StatChips`, `LiveLog`
- `src/agent2/`: `planData.js` (mock LAMP to Next.js/MERN plan), `OutputView`, `Toc`, `review.js` (reviewer state and Markdown export)
- `src/tweakbar/`: the floating panel

## Moving into the real app

Components take plain props and use only `var(--token)` styles, so they can be lifted as-is. New tokens are all prefixed `--pg-*`; rename them when merging into the theme.
