// Server-side approvals are not a local toggle (tasks_frontend.md A2.4): the playground's "Reset review"
// is deliberately not ported. A phase decision is changed by deciding again (an upsert), never cleared.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'

test('no agent2 source offers a review reset', () => {
  const dir = new URL('./', import.meta.url)
  const files = readdirSync(dir).filter((f) => /\.(jsx?|css)$/.test(f) && !f.endsWith('.test.js'))
  assert.ok(files.length > 0)
  for (const f of files) {
    const src = readFileSync(new URL(f, dir), 'utf8').replace(/\/\/.*$/gm, '')
    assert.doesNotMatch(src, /reset\s*review|resetReview/i, f)
  }
})
