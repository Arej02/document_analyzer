import test from 'node:test'
import assert from 'node:assert/strict'
import { buildStatChips } from './statChips.js'

test('no data yet gives no chips, so every chip reads Pending', () => {
  assert.deepEqual(buildStatChips({}, null), {})
  assert.deepEqual(buildStatChips(undefined, { changed_file_count: null, unchanged_file_count: null }), {})
})

test('files sums changed and unchanged from the event stats', () => {
  const chips = buildStatChips({ changed_files: 1200, unchanged_files: 84, removed_files: 3 })
  assert.deepEqual(chips.files, { value: '1,284', sub: '1,200 changed · 84 unchanged · 3 removed' })
})

test('files falls back to the job row counts, and needs both counts', () => {
  const chips = buildStatChips({}, { changed_file_count: 5, unchanged_file_count: 0, removed_file_count: 0 })
  assert.deepEqual(chips.files, { value: '5', sub: '5 changed · 0 unchanged' })
  assert.equal(buildStatChips({ changed_files: 5 }, null).files, undefined)
})

test('chips with no data source are never invented', () => {
  const chips = buildStatChips({ changed_files: 1, unchanged_files: 1, summaries_done: 4, evidence_chunks: 9 })
  assert.deepEqual(Object.keys(chips), ['files'])
})

test('discovered stats fill lines, languages, frameworks and dependencies', () => {
  const chips = buildStatChips({
    loc: 184306, dependencies: 143,
    'language.PHP': 3100, 'language.JavaScript': 640, 'language.Python': 12,
    'framework.WordPress': 1,
  })
  assert.deepEqual(chips.loc, { value: '184,306', sub: 'across 3,752 source files' })
  assert.deepEqual(chips.languages, { value: '3', sub: 'PHP 3,100 · JavaScript 640 · Python 12' })
  assert.deepEqual(chips.frameworks, { value: '1', sub: 'WordPress' })
  assert.deepEqual(chips.dependencies, { value: '143', sub: 'declared in manifests' })
})

test('discovery that found no frameworks says so; before discovery those chips stay pending', () => {
  const found = buildStatChips({ loc: 10, dependencies: 0, 'language.Go': 1 })
  assert.deepEqual(found.frameworks, { value: '0', sub: 'None recognised' })
  assert.deepEqual(found.dependencies, { value: '0', sub: 'declared in manifests' })
  assert.deepEqual(found.loc, { value: '10', sub: 'across 1 source file' })

  const before = buildStatChips({ changed_files: 1, unchanged_files: 1 })
  assert.equal(before.loc, undefined)
  assert.equal(before.languages, undefined)
  assert.equal(before.frameworks, undefined)
  assert.equal(before.dependencies, undefined)
})

test('an empty repo reports zero languages rather than pending', () => {
  const chips = buildStatChips({ loc: 0, dependencies: 0 })
  assert.deepEqual(chips.languages, { value: '0', sub: 'No source code found' })
  assert.deepEqual(chips.loc, { value: '0', sub: undefined })
})
