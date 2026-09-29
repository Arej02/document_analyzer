import test from 'node:test'
import assert from 'node:assert/strict'
import { buildExecutiveSummary, buildRepoOverview } from './overviewModel.js'

const plan = {
  phases: [
    { phase_cluster_id: 'p1', label: 'Core', summary_text: 'Core first.', grounding_status: 'verified', units: [{}, {}] },
    { phase_cluster_id: 'p2', label: 'Billing', summary_text: 'Billing next.', grounding_status: 'unverified', units: [{}] },
    { phase_cluster_id: 'p3', label: 'Admin', summary_text: null, grounding_status: null, units: [] },
  ],
}

test('executive summary: repo paragraph first, phases with a report after, unverified flagged', () => {
  const s = buildExecutiveSummary(plan, { repo_summary: 'A shop.', unverified_count: 2, risk_level: 'high' })
  assert.deepEqual(s.paragraphs.map((p) => [p.key, p.unverified]), [['repo', false], ['phase-p1', false], ['phase-p2', true]])
  assert.equal(s.unverifiedRepo, 2)
  assert.deepEqual(s.highlights, [{ label: 'Phases', value: 3 }, { label: 'Migration units', value: 3 }, { label: 'Repository risk', value: 'high' }])
})
test('executive summary survives no repo summary and no plan', () => {
  assert.deepEqual(buildExecutiveSummary(null, null).paragraphs, [])
  assert.equal(buildExecutiveSummary(plan, null).highlights.length, 2)
})

test('repo overview: languages sorted with percentages that sum to 100, frameworks sorted', () => {
  const o = buildRepoOverview({ stats: { loc: 1200, dependencies: 4, 'language.PHP': 6, 'language.JavaScript': 2, 'language.Go': 2, 'framework.WordPress': 1 } })
  assert.deepEqual(o.languages.map((l) => l.name), ['PHP', 'Go', 'JavaScript'])
  assert.equal(o.languages.reduce((n, l) => n + l.pct, 0), 100)
  assert.deepEqual(o.frameworks, ['WordPress'])
  assert.deepEqual(o.size, { loc: 1200, sourceFiles: 10, dependencies: 4, entities: null })
})
test('repo overview: entities from counts, entry points from routes, and nothing when empty', () => {
  const o = buildRepoOverview({ summary: { entity_counts: { function: 5, route: 2 } }, routes: [{ id: 'r', name: 'GET /', file_path: 'a.php' }] })
  assert.equal(o.size.entities, 7)
  assert.deepEqual(o.entryPoints, [{ id: 'r', name: 'GET /', filePath: 'a.php' }])
  const empty = buildRepoOverview()
  assert.deepEqual([empty.languages, empty.frameworks, empty.entryPoints], [[], [], []])
  assert.equal(empty.size.loc, null)
})
