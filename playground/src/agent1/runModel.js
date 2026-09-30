// Mock analysis run. Everything is a pure function of `elapsed` (simulated seconds), so the UI
// can pause, scrub, restart and replay deterministically. Numbers are consistent with the
// Agent 2 mock plan (3,902 files, 184,306 LOC, 143 packages).
export const REPO = { name: 'acme-corp/legacy-storefront', branch: 'main', commit: '3f9a1c2', startedAt: [14, 2, 11] }
export const TOTALS = { files: 3902, loc: 184306, deps: 143, composer: 87, npm: 56 }

const P = (id, name, dur, o) => ({ id, name, dur, ...o })
export const PHASES = (() => {
  const list = [
    P('clone', 'Cloning repository', 14, {
      total: 11204, unit: 'objects', verb: (n, t) => `Receiving ${n} / ${t} objects`,
      paths: ['refs/heads/main', 'objects/pack/pack-9c1e4a.pack', 'refs/tags/v4.18.2', 'refs/heads/release/2023-q4', 'objects/pack/pack-31be07.idx'],
    }),
    P('index', 'Indexing files', 52, {
      total: TOTALS.files, unit: 'files', verb: (n, t) => `Indexing ${n} / ${t} files`,
      paths: ['wp-content/plugins/acme-core/includes/pricing/class-rule-engine.php', 'wp-content/themes/acme-shop/templates/product-card.php', 'wp-content/plugins/acme-core/includes/checkout/class-cart-totals.php', 'wp-content/themes/acme-shop/assets/src/js/cart-drawer.js', 'db/migrations/2021_06_08_add_price_tiers.sql', 'wp-content/plugins/acme-core/admin/views/quotes-table.php', 'assets/src/scss/components/_checkout.scss', 'wp-content/mu-plugins/acme-hooks.php', 'includes/legacy/ship-rates.php', 'wp-content/plugins/acme-core/includes/inventory/class-erp-sync.php'],
    }),
    P('stack', 'Detecting stack & frameworks', 31, {
      total: 214, unit: 'manifests and config files', verb: (n, t) => `Inspecting ${n} / ${t} manifests and config files`,
      paths: ['composer.json', 'wp-config.php', 'package.json', '.htaccess', 'wp-includes/version.php', 'wp-content/plugins/woocommerce/woocommerce.php', 'webpack.config.js', 'wp-content/themes/acme-shop/style.css'],
      warnAt: 0.42,
    }),
    P('deps', 'Resolving dependencies', 46, {
      total: TOTALS.deps, unit: 'packages', verb: (n, t) => `Resolving ${n} / ${t} packages`,
      paths: ['vendor/guzzlehttp/guzzle/composer.json', 'vendor/phpmailer/phpmailer/composer.json', 'node_modules/select2/package.json', 'vendor/stripe/stripe-php/composer.json', 'vendor/monolog/monolog/composer.json', 'node_modules/jquery/package.json', 'vendor/acme/erp-connector/composer.json', 'vendor/mpdf/mpdf/composer.json'],
    }),
    P('routes', 'Mapping routes & endpoints', 58, {
      total: 412, unit: 'routes', verb: (n, t) => `Mapping ${n} / ${t} routes`,
      paths: ['GET  /wp-json/acme/v1/products', 'POST /wp-admin/admin-ajax.php?action=acme_quote_submit', 'GET  /shop/category/{slug}/', 'POST /wc-api/stripe-webhook', 'GET  /wp-json/acme/v1/inventory/{sku}', 'POST /checkout/apply-coupon', 'GET  /my-account/quotes/{id}/'],
    }),
    P('schema', 'Analysing database schema', 37, {
      total: 58, unit: 'tables', verb: (n, t) => `Reading ${n} / ${t} tables`,
      paths: ['wp_posts', 'wp_postmeta', 'wp_woocommerce_order_items', 'acme_price_rules', 'acme_inventory_snapshots', 'acme_quotes', 'wp_usermeta', 'acme_customer_tiers'],
    }),
    P('risk', 'Assessing migration risk', 40, {
      total: 36, unit: 'modules', verb: (n, t) => `Scoring ${n} / ${t} modules`,
      paths: ['acme-core/pricing', 'acme-core/checkout', 'acme-core/payments', 'acme-core/inventory', 'acme-core/quotes', 'acme-shop/templates', 'acme-core/admin'],
    }),
  ]
  let t = 0
  return list.map((p) => { const s = { ...p, start: t, end: t + p.dur }; t += p.dur; return s })
})()
export const TOTAL_SECONDS = PHASES[PHASES.length - 1].end

export const WARNING = {
  id: 'w-mysql',
  phaseId: 'stack',
  text: '14 files call removed mysql_* functions (PHP 7.0+). Flagged for review; analysis continues.',
  files: ['includes/legacy/ship-rates.php', 'includes/legacy/report-export.php', 'wp-content/plugins/acme-core/includes/inventory/class-erp-sync.php'],
}
export const FAILURE = {
  phaseId: 'deps',
  atSeconds: 122,
  title: 'Could not resolve vendor/acme/erp-connector',
  detail: 'repo.acme.internal returned 401 Unauthorized. Add a read token for the private Composer repository and restart the analysis.',
}

const warnTime = (() => { const p = PHASES.find((x) => x.id === 'stack'); return p.start + p.warnAt * p.dur })()

const VERBS = { clone: 'Fetching', index: 'Reading', stack: 'Inspecting', deps: 'Resolving', routes: 'Mapping', schema: 'Reading', risk: 'Scoring' }
const NOTES = {
  index: [[0.35, 'Skipping 212 vendored files under wp-content/plugins/woocommerce'], [0.7, 'Skipping 4 binary assets over 5 MB']],
  stack: [[0.18, 'Detected WordPress 6.2.4 (wp-includes/version.php)'], [0.62, 'Detected WooCommerce 7.9.0 with 31 template overrides']],
  deps: [[0.3, 'composer.lock: 87 packages, 3 pinned to dev branches'], [0.66, 'package-lock.json: 56 packages']],
  routes: [[0.5, 'Found 43 admin-ajax actions and 27 REST routes in acme-core']],
  schema: [[0.45, 'Found 17 custom tables prefixed acme_']],
}

/** Full log script, built once. Each entry: { t, level, text, path?, phaseId } */
export const SCRIPT = (() => {
  const out = []
  PHASES.forEach((p) => {
    out.push({ t: p.start, level: 'info', phaseId: p.id, verb: 'Started', text: p.name })
    const n = Math.max(4, Math.round(p.dur / 2.4))
    for (let i = 1; i <= n; i++) {
      const t = p.start + (i / (n + 1)) * p.dur
      out.push({ t, level: 'info', phaseId: p.id, verb: VERBS[p.id], path: p.paths[(i * 3) % p.paths.length] })
    }
    ;(NOTES[p.id] || []).forEach(([f, text]) => out.push({ t: p.start + f * p.dur, level: 'info', phaseId: p.id, verb: 'Note', text }))
    out.push({ t: p.end - 0.01, level: 'info', phaseId: p.id, verb: 'Completed', text: p.name })
  })
  out.push({ t: warnTime, level: 'warn', phaseId: 'stack', verb: 'Warning', text: WARNING.text, path: WARNING.files[0] })
  out.push({ t: FAILURE.atSeconds, level: 'error', phaseId: 'deps', verb: 'Failed', text: FAILURE.title, path: 'vendor/acme/erp-connector/composer.json' })
  return out.sort((a, b) => a.t - b.t || (a.verb === 'Completed' ? -1 : 0))
})()

export function clockAt(seconds) {
  const [h, m, s] = REPO.startedAt
  const total = h * 3600 + m * 60 + s + Math.floor(seconds)
  const f = (n) => String(n).padStart(2, '0')
  return `${f(Math.floor(total / 3600) % 24)}:${f(Math.floor(total / 60) % 60)}:${f(total % 60)}`
}

const easeOutSlight = (x) => x + 0.06 * Math.sin(x * Math.PI * 2) * (1 - x) // small deterministic wobble so it doesn't look mechanical
const fmt = (n) => Math.round(n).toLocaleString('en-US')

/**
 * @param elapsed simulated seconds since start
 * @param failing when true the run stops at FAILURE.atSeconds
 */
export function snapshot(elapsed, failing) {
  const failed = failing && elapsed >= FAILURE.atSeconds
  const t = failed ? FAILURE.atSeconds : Math.min(elapsed, TOTAL_SECONDS)
  const done = !failed && t >= TOTAL_SECONDS

  const phases = PHASES.map((p) => {
    const raw = Math.min(1, Math.max(0, (t - p.start) / p.dur))
    const progress = raw >= 1 ? 1 : easeOutSlight(raw)
    let status = 'queued'
    if (t >= p.end) status = 'completed'
    else if (t >= p.start) status = 'in_progress'
    if (failed && p.id === FAILURE.phaseId) status = 'failed'
    const warned = p.id === WARNING.phaseId && t >= warnTime
    if (warned && status === 'completed') status = 'warning'
    const count = Math.round(progress * p.total)
    return { ...p, status, progress, warned, count, label: p.verb(fmt(count), fmt(p.total)), path: p.paths[Math.floor(count / 3) % p.paths.length], duration: Math.min(p.dur, Math.max(0, t - p.start)) }
  })
  const currentIndex = done ? phases.length - 1 : Math.max(0, phases.findIndex((p) => p.status === 'in_progress' || p.status === 'failed'))
  const current = phases[currentIndex]
  const pct = (t / TOTAL_SECONDS) * 100

  const P_ = Object.fromEntries(phases.map((p) => [p.id, p]))
  const idx = P_.index.progress, stk = P_.stack.progress, dep = P_.deps.progress
  const stats = {
    files: P_.index.status === 'queued' ? null : { value: fmt(idx * TOTALS.files), sub: idx >= 1 ? 'Indexed in full' : 'Indexing' },
    loc: P_.index.status === 'queued' ? null : { value: fmt(idx * TOTALS.loc), sub: 'Lines of code' },
    languages: stk < 0.3 ? null : { value: stk >= 1 ? '5' : stk > 0.7 ? '4' : '3', sub: 'PHP 71.4%, JavaScript 18.2%' },
    frameworks: stk < 0.5 ? null : { value: stk >= 1 ? '4' : '2', sub: stk >= 1 ? 'WordPress, WooCommerce, jQuery, Bootstrap' : 'WordPress, WooCommerce' },
    dependencies: P_.deps.status === 'queued' ? null : { value: fmt(dep * TOTALS.deps), sub: dep >= 1 ? `${TOTALS.composer} Composer, ${TOTALS.npm} npm` : 'Resolving' },
  }

  const log = SCRIPT.filter((e) => e.t <= t && (e.level !== 'error' || failed))
  const warnings = warnTime <= t ? [WARNING] : []
  return { t, done, failed, phases, current, currentIndex, pct, remaining: Math.max(0, TOTAL_SECONDS - t), stats, log, warnings }
}
