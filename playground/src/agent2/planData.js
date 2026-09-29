// Mock migration plan: acme-corp/legacy-storefront (LAMP + WordPress/WooCommerce) → Next.js 15 / Express 5 / MongoDB 7.
// Figures match the Agent 1 simulation (3,902 files, 184,306 LOC, 143 packages).

export const REPO = { name: 'acme-corp/legacy-storefront', commit: '3f9a1c2', generated: '4m ago', job: 'job_8f21c0' }

export const SUMMARY = [
  'acme-corp/legacy-storefront is a 184,306-line PHP 7.2 storefront built on WordPress 6.2 and WooCommerce 7.9. Most business logic is not in WordPress itself: a first-party plugin, acme-core (61,842 lines), owns pricing, inventory sync and B2B quoting, and a custom theme, acme-shop, renders every customer-facing page. It runs on two self-managed EC2 hosts behind Apache 2.4, with MySQL 5.7 (end of life since October 2023).',
  'We recommend a strangler-fig migration in five phases: put a routing layer in front of the legacy site, then move catalog, accounts, pricing and checkout to Next.js 15 (App Router), Express 5 and MongoDB 7 one path at a time. Checkout moves last because pricing behaviour is defined implicitly by 214 WordPress filter callbacks and has to be proven equivalent before customers see it.',
]
export const HIGHLIGHTS = [
  { label: 'Phases', value: '5' },
  { label: 'Plan tasks', value: '18' },
  { label: 'Effort', value: '34–46 person-weeks' },
  { label: 'Decisions open', value: '6' },
]

export const STACK = [
  { label: 'Runtime', current: 'PHP 7.2.34 (unsupported since Nov 2020)', target: 'Node.js 22 LTS' },
  { label: 'Framework', current: 'WordPress 6.2.4 with WooCommerce 7.9.0', target: 'Next.js 15 (App Router) with Express 5' },
  { label: 'Database', current: 'MySQL 5.7.42, 58 tables, 18.6 GB', target: 'MongoDB 7 (Atlas), 21 collections' },
  { label: 'Web server', current: 'Apache 2.4 with 48 mod_rewrite rules', target: 'CDN and edge router, Next.js server' },
  { label: 'Front-end build', current: 'Webpack 4.46, jQuery 3.6, Bootstrap 4.6', target: 'Next.js build, React 19, Tailwind CSS 4' },
  { label: 'Scheduling', current: '11 wp-cron events and 4 system crontab jobs', target: 'BullMQ workers on Redis' },
]

export const STRUCTURE = [
  { path: 'wp-content/plugins/acme-core/', files: 612, loc: 61842, note: 'First-party plugin: pricing, checkout, quotes, inventory' },
  { path: 'wp-content/plugins/ (third-party)', files: 1127, loc: 48916, note: 'WooCommerce and 9 vendored plugins' },
  { path: 'wp-content/themes/acme-shop/', files: 341, loc: 22190, note: 'Custom theme, 63 template files' },
  { path: 'assets/', files: 402, loc: 19486, note: 'JavaScript and SCSS sources' },
  { path: 'db/', files: 74, loc: 11458, note: 'Migrations and seed SQL' },
  { path: 'includes/legacy/', files: 96, loc: 9870, note: 'Standalone scripts outside WordPress' },
  { path: 'wp-content/mu-plugins/', files: 38, loc: 3204, note: 'Must-use hooks loaded on every request' },
  { path: 'Other (config, docs, fixtures)', files: 1212, loc: 7340, note: 'Excluded from module analysis' },
]
export const LANGUAGES = [
  { name: 'PHP', pct: 71.4 }, { name: 'JavaScript', pct: 18.2 }, { name: 'SQL', pct: 5.1 }, { name: 'SCSS and CSS', pct: 3.9 }, { name: 'Shell and other', pct: 1.4 },
]
export const SIZE = { files: 3902, loc: 184306, packages: 143, tables: 58 }

export const ENTRY_POINTS = [
  { path: 'index.php', kind: 'Front controller', detail: 'WordPress request bootstrap for every page' },
  { path: 'wp-admin/admin-ajax.php', kind: 'AJAX', detail: '43 acme_* actions registered by acme-core' },
  { path: 'wp-json/acme/v1/*', kind: 'REST', detail: '27 routes, consumed by the storefront and the v2 mobile app' },
  { path: 'wc-api/stripe-webhook', kind: 'Webhook', detail: 'Payment events, signature verified in class-stripe-gateway.php' },
  { path: 'cron/sync-inventory.php', kind: 'Cron', detail: 'ERP inventory pull, every 15 minutes from system crontab' },
  { path: 'xmlrpc.php', kind: 'Unused', detail: 'Enabled, no callers found in 90 days of access logs' },
]

export const MODULES = [
  { name: 'Catalog and search', path: 'acme-core/includes/catalog', files: 84, loc: 9412, coupling: 'Low', fanIn: 3, target: 'Express /catalog API, Next.js product pages' },
  { name: 'Pricing engine', path: 'acme-core/includes/pricing', files: 41, loc: 7880, coupling: 'High', fanIn: 6, target: 'Declarative rules service with parity tests' },
  { name: 'Cart and checkout', path: 'acme-core/includes/checkout', files: 97, loc: 12204, coupling: 'High', fanIn: 4, target: 'Cart service, Next.js checkout' },
  { name: 'Payments', path: 'acme-core/includes/payments', files: 23, loc: 4116, coupling: 'Medium', fanIn: 2, target: 'Stripe Node SDK, idempotent webhooks' },
  { name: 'Accounts and B2B quotes', path: 'acme-core/includes/quotes', files: 66, loc: 8930, coupling: 'Medium', fanIn: 2, target: 'Auth.js, quotes API' },
  { name: 'Inventory sync', path: 'acme-core/includes/inventory', files: 31, loc: 5502, coupling: 'Medium', fanIn: 3, target: 'BullMQ worker and ERP client' },
  { name: 'Admin dashboards', path: 'acme-core/admin', files: 118, loc: 13660, coupling: 'Medium', fanIn: 1, target: 'React admin app' },
  { name: 'Theme templates', path: 'themes/acme-shop', files: 341, loc: 22190, coupling: 'Low', fanIn: 0, target: 'React components' },
]
export const HOTSPOTS = [
  'Pricing engine is called from 6 modules, and 214 apply_filters("acme_price_*") callbacks run in priority order, so behaviour depends on hook registration order.',
  'Cart and checkout reads and writes wp_postmeta directly in 38 places, bypassing WooCommerce data stores.',
  'Inventory sync and catalog both write stock counts, with no single owner of the stock_quantity field.',
]

const D = (pkg, ecosystem, current, target, status, note) => ({ pkg, ecosystem, current, target, status, note })
export const DEPENDENCIES = [
  D('wordpress/wordpress', 'Composer', '6.2.4', 'Next.js 15 and Express 5', 'rewrite', 'Application framework; no drop-in replacement'),
  D('woocommerce/woocommerce', 'Composer', '7.9.0', 'Custom services and Stripe SDK', 'rewrite', '31 template overrides and 38 direct meta writes'),
  D('guzzlehttp/guzzle', 'Composer', '7.5.0', 'fetch (Node 22 built-in)', 'direct', 'Used in 19 files for ERP and shipping calls'),
  D('phpmailer/phpmailer', 'Composer', '6.8.1', 'nodemailer 6.9', 'direct', 'SMTP transport config maps one to one'),
  D('stripe/stripe-php', 'Composer', '10.14.0', 'stripe 17', 'direct', 'Move to PaymentIntents while migrating'),
  D('monolog/monolog', 'Composer', '2.9.1', 'pino 9', 'direct', 'Structured JSON logs'),
  D('nesbot/carbon', 'Composer', '2.66.0', 'date-fns 4', 'direct', '112 call sites, mostly formatting'),
  D('ramsey/uuid', 'Composer', '4.7.4', 'crypto.randomUUID()', 'retire', 'Built into Node 22'),
  D('phpoffice/phpspreadsheet', 'Composer', '1.28.0', 'exceljs 4.4', 'partial', 'Two report exports use formula features exceljs lacks'),
  D('mpdf/mpdf', 'Composer', '8.1.4', 'Playwright PDF rendering', 'partial', 'Invoice and quote templates need re-authoring in HTML'),
  D('acme/erp-connector', 'Composer', '2.3.0', 'None (private, SOAP)', 'none', 'Undocumented; rebuild against the ERP REST API'),
  D('jquery', 'npm', '3.6.4', 'React 19', 'retire', 'Replaced as components are ported'),
  D('bootstrap', 'npm', '4.6.2', 'Tailwind CSS 4', 'partial', 'Grid and utility classes need a mapping pass'),
  D('webpack', 'npm', '4.46.0', 'Next.js build pipeline', 'retire', 'Removed with the asset pipeline'),
  D('select2', 'npm', '4.0.13', 'react-select 5', 'direct', '7 usages, all in admin'),
  D('lodash', 'npm', '4.17.21', 'lodash-es 4.17', 'direct', 'Tree-shakeable drop-in'),
]
export const DEP_STATUS = {
  direct: { label: 'Direct replacement', tone: 'success' },
  partial: { label: 'Adapter needed', tone: 'warning' },
  rewrite: { label: 'Rewrite required', tone: 'danger' },
  none: { label: 'No equivalent', tone: 'danger' },
  retire: { label: 'Retire', tone: 'neutral' },
}
export const DEP_TOTAL = { shown: DEPENDENCIES.length, total: 143, composer: 87, npm: 56 }

export const SCHEMA_STATS = [
  { label: 'Tables', value: '58' }, { label: 'Custom (acme_*)', value: '17' }, { label: 'Storage', value: '18.6 GB' }, { label: 'Foreign keys', value: '23' },
]
export const TABLES = [
  { name: 'wp_posts', rows: 412880, target: 'products, categories', note: 'Products and orders stored as custom post types; split by post_type.' },
  { name: 'wp_postmeta', rows: 2418306, target: 'Embedded in products', note: 'PHP-serialized arrays in meta_value; must be converted without unserialize().' },
  { name: 'wp_woocommerce_order_items', rows: 1204117, target: 'orders.items[]', note: 'Embed items and item meta in the order document.' },
  { name: 'acme_price_rules', rows: 8412, target: 'pricing_rules', note: 'Conditions stored as JSON in a TEXT column; validate on import.' },
  { name: 'acme_inventory_snapshots', rows: 9846120, target: 'inventory (time series)', note: 'Append-only history; fits a MongoDB time-series collection.' },
  { name: 'wp_users, wp_usermeta', rows: 96304, target: 'users', note: 'phpass hashes ($P$) are re-hashed on next login.' },
  { name: 'acme_quotes, acme_quote_lines', rows: 12090, target: 'quotes (embedded lines)', note: 'Lines never queried on their own.' },
]
export const DATA_STRATEGY = [
  'Backfill each collection with idempotent batch jobs keyed on the legacy primary key.',
  'Dual-write behind a per-collection flag once a path is served by the new stack.',
  'Cut reads over one collection at a time, comparing responses against MySQL for 7 days.',
  'Keep MySQL read-only for 30 days after checkout cutover, then decommission.',
]

const R = (id, severity, title, description, files, mitigation, phase) => ({ id, severity, title, description, files, mitigation, phase })
export const RISKS = [
  R('r1', 'critical', 'Pricing behaviour is defined by hook order', '214 apply_filters("acme_price_*") callbacks modify prices in priority order. There is no single function that computes a price, so a straight port can silently change totals.', ['acme-core/includes/pricing/class-rule-engine.php', 'mu-plugins/acme-hooks.php'], 'Record 14 days of production price calculations and replay them against the new engine in CI before any checkout traffic moves.', 'p3'),
  R('r2', 'critical', 'Serialized PHP data with class references', '2.4 million wp_postmeta rows hold PHP-serialized values, some referencing classes that no longer exist. Unserializing them in a migration script is unsafe.', ['acme-core/includes/catalog/class-meta-store.php'], 'Write a parser that reads the serialize() format without instantiating classes, and quarantine rows that fail to parse.', 'p1'),
  R('r3', 'high', 'Payment webhook idempotency', 'Stripe and ShipStation webhooks are handled in-request with no idempotency keys. Running old and new handlers in parallel could double-capture or double-ship.', ['acme-core/includes/payments/class-stripe-gateway.php', 'includes/legacy/shipstation-hook.php'], 'Store event IDs, route each event type to exactly one system, and cut over per event type.', 'p3'),
  R('r4', 'high', 'Password hashes use phpass', '96,304 users have portable phpass hashes that Auth.js does not verify by default. 41,870 have not signed in for over 12 months.', ['wp-includes/class-phpass.php'], 'Verify phpass on login, re-hash with argon2id, and send a reset email to dormant accounts after cutover.', 'p2'),
  R('r5', 'high', 'Undocumented ERP connector', 'acme/erp-connector is a private SOAP client with no tests and no documentation. Inventory accuracy depends on it.', ['vendor/acme/erp-connector/src/Client.php', 'cron/sync-inventory.php'], 'Capture SOAP request and response fixtures in staging and rebuild against the ERP REST API behind a contract test.', 'p4'),
  R('r6', 'medium', 'Search engine URL parity', '48 rewrite rules serve about 61,000 indexed URLs. Missed redirects would drop organic traffic.', ['.htaccess'], 'Generate a redirect map from the sitemap and access logs, and verify with a crawl in staging.', 'p0'),
  R('r7', 'medium', 'Overlapping scheduled jobs', '15 scheduled jobs (wp-cron and system cron) write to the same tables, and two of them assume they never run concurrently.', ['wp-content/mu-plugins/acme-cron.php', 'cron/'], 'Move jobs to BullMQ with per-job concurrency limits and document each job’s side effects.', 'p4'),
  R('r8', 'medium', 'Unknown API consumers', '43 admin-ajax actions and 27 REST routes are called by the v2 mobile app and possibly third parties. Access logs show 9 unrecognised user agents.', ['acme-core/includes/rest/'], 'Freeze the legacy API contract, log callers, and keep the proxy route until traffic reaches zero.', 'p0'),
  R('r9', 'low', 'Unused endpoint and templates', 'xmlrpc.php is enabled with no callers. 63 theme templates have no route that renders them.', ['xmlrpc.php', 'themes/acme-shop/templates/'], 'Do not migrate. Disable xmlrpc.php in phase 0 and delete the templates after cutover.', 'p0'),
]
export const SEVERITY = {
  critical: { label: 'Critical', weight: 10, tone: 'danger' },
  high: { label: 'High', weight: 6, tone: 'danger' },
  medium: { label: 'Medium', weight: 3, tone: 'warning' },
  low: { label: 'Low', weight: 1, tone: 'neutral' },
}
export const RISK_MAX = 72 // normalising constant: the weighted total that scores 100
export const riskScore = () => Math.round((RISKS.reduce((n, r) => n + SEVERITY[r.severity].weight, 0) / RISK_MAX) * 100)
export const riskBand = (score) => (score >= 80 ? 'Severe' : score >= 60 ? 'High' : score >= 35 ? 'Moderate' : 'Low')

const T = (id, title, detail, effort) => ({ id, title, detail, effort })
export const PHASES = [
  {
    id: 'p0', name: 'Foundation and strangler proxy', effort: '4–5 person-weeks', weeks: [4, 5], dependsOn: [],
    goals: ['Route every request through a layer that can send a path to either system.', 'Have the new stack deployable and observable before any feature moves.'],
    tasks: [
      T('t01', 'Scaffold a Turborepo with apps/web and apps/api', 'Next.js 15 and Express 5 in TypeScript, shared lint and test config, CI on every pull request.', '1 pw'),
      T('t02', 'Add a CDN routing layer with per-path cutover flags', 'Default every path to the legacy origin. Flags switch one path prefix at a time.', '1.5 pw'),
      T('t03', 'Record 14 days of production traffic for parity replay', 'Sanitised request and response pairs for catalog, cart and quote paths.', '1 pw'),
      T('t04', 'Generate the redirect map and provision MongoDB Atlas', 'Redirects from 48 rewrite rules and the sitemap; cluster, secrets and backups.', '1 pw'),
    ],
    files: ['.htaccess', 'wp-config.php', 'index.php', 'xmlrpc.php'],
  },
  {
    id: 'p1', name: 'Catalog and search', effort: '6–8 person-weeks', weeks: [6, 8], dependsOn: ['p0'],
    goals: ['Serve all product and category pages from Next.js.', 'Move product data to MongoDB with no unsafe deserialization.'],
    tasks: [
      T('t11', 'Model products and categories in MongoDB', 'Embed variants and attributes; index by slug, category and SKU.', '1.5 pw'),
      T('t12', 'Write a safe parser and backfill from wp_posts and wp_postmeta', 'Reads serialize() output without instantiating classes; quarantines rows that fail.', '2.5 pw'),
      T('t13', 'Build /catalog API and product pages with incremental regeneration', 'Listing, detail and category routes with SEO metadata parity.', '2 pw'),
      T('t14', 'Port search to Atlas Search', 'Match ranking on the 500 most frequent queries from the last 90 days.', '1.5 pw'),
    ],
    files: ['acme-core/includes/catalog/class-meta-store.php', 'acme-core/includes/catalog/class-search.php', 'themes/acme-shop/templates/product-card.php', 'themes/acme-shop/templates/archive-product.php'],
  },
  {
    id: 'p2', name: 'Accounts and B2B quotes', effort: '6–8 person-weeks', weeks: [6, 8], dependsOn: ['p0'],
    goals: ['Let existing customers sign in on the new stack without a password reset.', 'Move quote requests and approvals off admin-ajax.'],
    tasks: [
      T('t21', 'Implement Auth.js with phpass verification and re-hash on login', 'Argon2id for new hashes; audit log for every legacy verification.', '2 pw'),
      T('t22', 'Migrate users and customer tiers', 'Backfill 96,304 users; map acme_customer_tiers to a tier field.', '1.5 pw'),
      T('t23', 'Build the quotes API and account pages', 'Create, revise, approve and convert-to-order flows with embedded lines.', '3 pw'),
    ],
    files: ['wp-includes/class-phpass.php', 'acme-core/includes/quotes/class-quote-controller.php', 'themes/acme-shop/templates/my-account/', 'acme-core/includes/rest/quotes.php'],
  },
  {
    id: 'p3', name: 'Pricing, cart and checkout', effort: '10–14 person-weeks', weeks: [10, 14], dependsOn: ['p1', 'p2'],
    goals: ['Prove the new pricing engine matches production totals before it takes traffic.', 'Move payment handling to idempotent webhooks.'],
    tasks: [
      T('t31', 'Extract pricing rules into a declarative engine', 'Turn hook callbacks into ordered rule documents with explicit priority.', '4 pw'),
      T('t32', 'Build the parity harness against recorded price calculations', 'Nightly replay in CI with a per-SKU diff report; zero unexplained diffs to proceed.', '2 pw'),
      T('t33', 'Build the cart service and checkout pages', 'Coupons, shipping rates, tax and order creation.', '4 pw'),
      T('t34', 'Move Stripe and ShipStation webhooks to idempotent handlers', 'Event ID store, signature verification, per-event-type cutover.', '2 pw'),
    ],
    files: ['acme-core/includes/pricing/class-rule-engine.php', 'mu-plugins/acme-hooks.php', 'acme-core/includes/checkout/class-cart-totals.php', 'acme-core/includes/payments/class-stripe-gateway.php', 'includes/legacy/shipstation-hook.php'],
  },
  {
    id: 'p4', name: 'Admin, jobs and cutover', effort: '8–11 person-weeks', weeks: [8, 11], dependsOn: ['p3'],
    goals: ['Retire wp-admin for staff and every scheduled job on PHP.', 'Decommission the legacy hosts after a 30-day read-only period.'],
    tasks: [
      T('t41', 'Build the React admin for orders, quotes and inventory', 'Role-based access matching the 6 existing WordPress roles.', '4 pw'),
      T('t42', 'Move 15 scheduled jobs to BullMQ and rebuild the ERP client', 'Concurrency limits per job; contract tests against captured SOAP fixtures.', '3 pw'),
      T('t43', 'Run final cutover and decommission legacy', 'Switch remaining paths, freeze MySQL, monitor for 30 days, then shut down.', '2 pw'),
    ],
    files: ['acme-core/admin/', 'wp-content/mu-plugins/acme-cron.php', 'cron/sync-inventory.php', 'vendor/acme/erp-connector/src/Client.php'],
  },
]
export const ALL_TASKS = PHASES.flatMap((p) => p.tasks.map((t) => ({ ...t, phase: p.id })))

const Q = (id, title, why, options, recommended, blocks) => ({ id, title, why, options, recommended, blocks })
export const QUESTIONS = [
  Q('q1', 'Should MongoDB be the system of record for orders?', 'Orders and pricing rules are relational in shape, and financial reporting currently joins them across 6 tables.', ['MongoDB for everything', 'PostgreSQL for orders and pricing, MongoDB for catalog'], 0, 'p1'),
  Q('q2', 'Which authentication provider should the new stack use?', 'Self-hosted Auth.js keeps password hashes in your control; a hosted provider removes operational work but needs a bulk import.', ['Auth.js, self-hosted', 'Hosted provider with bulk import'], 0, 'p2'),
  Q('q3', 'Must existing order numbers and order-page URLs stay valid?', 'Support and accounting reference WooCommerce order IDs. Changing them affects 4 external reports.', ['Keep legacy order IDs', 'New IDs with a lookup for old ones'], 0, 'p3'),
  Q('q4', 'What is the plan for the v2 mobile app?', 'It calls 12 legacy REST routes. Keeping them means proxying past cutover.', ['Freeze the v2 contract and proxy', 'Ship an app update before cutover'], 1, 'p3'),
  Q('q5', 'Cut over checkout in one step or by market?', 'A single switch is simpler; per-market limits the impact of a pricing defect.', ['Single cutover', 'By market, smallest first'], 1, 'p3'),
  Q('q6', 'Where will the new stack be hosted?', 'Affects the CDN routing layer designed in phase 0.', ['Vercel and MongoDB Atlas', 'AWS ECS and MongoDB Atlas'], 0, 'p0'),
]
