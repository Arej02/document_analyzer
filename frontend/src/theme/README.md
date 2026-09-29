# Theme architecture

`~/galvanise/showcase` is the source of truth. `themes.js` copies every light/dark token from its app/globals.css exactly, with aliases for the product UI. The shared header, footer, LinkedIn SVG and logo styles live in src/showcase. Geist, Geist Mono, Stardom and favicon are served locally. The product opens directly into authentication or its current screen. Only the header’s three circles assemble into the logo on mount using CSS keyframes; reduced-motion disables this effect. There is no landing page, splash overlay, scroll animation, scroll lock, or animation delay before using the product.

Three Gang of Four patterns support this implementation:

- **Strategy**: `themeStrategies` provides interchangeable dark, light and system resolution functions. The store uses the chosen strategy without branching on each mode.
- **Observer**: `createThemeStore` publishes immutable snapshots to subscribers. React consumes these through `useSyncExternalStore`; the last unsubscribe removes browser listeners.
- **Adapter**: `createBrowserThemeAdapter` converts localStorage, matchMedia, storage events and document CSS APIs into the store's small interface. Storage failures fall back to in-memory operation. Tests inject an independent adapter.

`ThemeProvider` supplies the store; `useTheme` is the component API. Initialization applies tokens before React renders. The sun/moon ThemeToggle button (and D hotkey outside editable fields) is available on every route, including authentication, and switches directly between dark and light. Dark is the default to match the reference. Explicit preferences persist under `galvanise.theme`; Previously saved System preferences follow operating-system changes until the toggle selects an explicit theme. Storage events synchronize tabs.

To extend a theme, add the same semantic token to both theme definitions and consume it with `var(--token-name)` in CSS or graph styles. Keep logo pigment colors fixed.

The redesign tokens (from the locked playground config in `docs/tasks_frontend.md`) are `accent-ui` / `accent-ui-foreground` (lavender UI accent and the text on it), `success`, `surface-sunken` (alias of `sidebar`), and `radius-sm` / `radius-lg` (next to the existing `radius`, 10px). Pills and other text on tinted fills must stay at 4.5:1 or better, so mix the accent with `text` rather than using it raw. Spacing, type and motion scales live in `styles.css`, not here. Changes to visual.html are deliberate design input, not automatically parsed executable configuration.

Verify with `node --test frontend/src/theme/themeStore.test.js` from the repository root and `npm run build --prefix frontend`.
