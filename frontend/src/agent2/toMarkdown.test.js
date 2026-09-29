import test from 'node:test'
import assert from 'node:assert/strict'
import { SECTION_TITLES, code, inline, toMarkdown } from './toMarkdown.js'

const plan = {
  status: 'in_review',
  open_facade_flags: 1,
  facade_cycles: [{ level: 'unit', scc_id: 3, cluster_ids: ['u1', 'u2'] }],
  phases: [
    { phase_cluster_id: 'p1', phase_order: 1, label: 'Core', summary_text: 'Core first.', risk_level: 'medium', approval: { status: 'approved', note: 'Looks fine.\nShip it.' },
      units: [{ cluster_id: 'u1', unit_order: 1, label: 'Auth | login', size: 4, risk_level: 'medium' }, { cluster_id: 'u2', unit_order: 2, label: 'Users', size: 2, risk_level: 'low', flags: ['needs_facade'] }] },
    { phase_cluster_id: 'p2', phase_order: 2, label: 'Billing', summary_text: null, risk_level: 'high', approval: null, units: [] },
  ],
}
const full = { plan, repoSummary: { repo_summary: 'A shop.', risk_level: 'high', entity_counts: { function: 3 }, unverified_count: 0 }, stats: { loc: 1200, 'language.PHP': 4, dependencies: 5 }, routes: [{ id: 'r', name: 'GET /', file_path: 'index.php' }], repoName: 'shop' }

const headings = (md) => [...md.matchAll(/^## (\d+)\. (.+)$/gm)].map((m) => [Number(m[1]), m[2]])

test('sections come out numbered in outline order, with all eight present', () => {
  assert.deepEqual(headings(toMarkdown(full)), SECTION_TITLES.map((t, i) => [i + 1, t]))
  assert.equal(SECTION_TITLES.length, 8)
})
test('an empty plan still has every section, each saying why it is empty', () => {
  const md = toMarkdown({})
  assert.deepEqual(headings(md).map(([, t]) => t), SECTION_TITLES)
  assert.match(md, /_No summary text is available/)
  assert.match(md, /_This plan has no phases\._/)
  assert.match(md, /_No open decisions\._/)
  assert.ok(md.endsWith('\n') && !md.endsWith('\n\n'))
})
test('approval state, risk and the saved note are exported as stored, note as a quote', () => {
  const md = toMarkdown(full)
  assert.match(md, /### Phase 1: Core\n\nApproval: Approved\. Risk: Medium\./)
  assert.match(md, /### Phase 2: Billing\n\nApproval: Not reviewed\. Risk: High\./)
  assert.match(md, /\*\*Reviewer note\*\*\n\n> Looks fine\.\n> Ship it\./)
  assert.match(md, /1 of 2 phases approved/)
  assert.match(md, /Plan status: in\\_review/)
})
test('table cells cannot gain columns: pipes and newlines in names are neutralised', () => {
  const md = toMarkdown(full)
  const line = md.split('\n').find((l) => l.includes('Auth'))
  assert.match(line, /Auth \\\| login/)
  const cols = (l) => l.replace(/\\\|/g, '').split('|').length
  const header = md.split('\n').find((l) => l.startsWith('| Unit |'))
  assert.equal(cols(line), cols(header))
})
test('inline escapes markup and block markers; code fences survive backticks and pipes', () => {
  assert.equal(inline('a\nb'), 'a b')
  assert.equal(inline('# heading'), '\\# heading')
  assert.equal(inline('- item'), '\\- item')
  assert.equal(inline('<script>x</script>'), '&lt;script&gt;x&lt;/script&gt;')
  assert.equal(inline('[l](u) *b* _i_ `c`'), '\\[l\\](u) \\*b\\* \\_i\\_ \\`c\\`')
  assert.equal(code('a/b.php'), '`a/b.php`')
  assert.equal(code('we`ird.php'), '`` we`ird.php ``')
  assert.equal(code('a|b'), '`a&#124;b`')
  assert.equal(inline(null), '')
})
test('hostile text in a phase summary cannot start a heading or a list', () => {
  const md = toMarkdown({ plan: { phases: [{ phase_cluster_id: 'p', phase_order: 1, label: 'X', summary_text: '# Injected\n- item', units: [] }] } })
  assert.doesNotMatch(md, /^# Injected/m)
  assert.doesNotMatch(md, /^- item/m)
})
test('dependencies: count-only without rows, table with them, target columns only when present', () => {
  assert.match(toMarkdown(full), /5 packages are declared in the manifests/)
  const facts = toMarkdown({ ...full, dependencies: [{ name: 'guzzle', ecosystem: 'composer', version_spec: '^7', manifest_path: 'composer.json' }] })
  assert.match(facts, /\| Package \| Ecosystem \| Declared version \| Manifest \|\n/)
  assert.doesNotMatch(facts, /Target equivalent/)
  const withTarget = toMarkdown({ ...full, dependencies: [{ name: 'a', target_name: 'b', grounding_status: 'unverified' }] })
  assert.match(withTarget, /Target equivalent \| Status/)
  assert.match(withTarget, /Target unverified/)
})
test('risk items list files as code and factors when details are loaded', () => {
  const riskDetails = { u1: { detail: { report: { risk_factors: ['pay()'] }, members: [{ file_path: 'a.php' }] } } }
  const md = toMarkdown({ ...full, riskDetails })
  assert.match(md, /\*\*Medium: Auth \\\| login\.\*\*/)
  assert.match(md, /Because of: pay\(\)\./)
  assert.match(md, /Files: `a\.php`\./)
})
test('provenance line appears only when there is something to say', () => {
  assert.doesNotMatch(toMarkdown(full), /Generated/)
  assert.match(toMarkdown({ ...full, commitSha: 'b4288c1aaaa', generatedAt: new Date(Date.now() - 3 * 3600e3).toISOString() }), /Generated from commit `b4288c1` 3 hours ago\./)
})
