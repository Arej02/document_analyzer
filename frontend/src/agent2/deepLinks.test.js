// Deep links (`?plan=<id>&unit=<cluster_id>`) belong to `main.jsx` (`navigateToPage`,
// `setPlanParam`, `PLAN_SUB_PARAMS`). The outline and scrollspy must never write to the URL, or a
// scroll would rewrite the address bar and drop those params. Guard it structurally.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

for (const file of ['useOutline.js', 'Toc.jsx', 'Section.jsx', 'collapse.js', 'PlanDocument.jsx', 'PlanDocumentView.jsx']) {
  test(`${file} does not touch the URL or history`, () => {
    const src = readFileSync(new URL(`./${file}`, import.meta.url), 'utf8').replace(/\/\/.*$/gm, '')
    assert.doesNotMatch(src, /history\.|location\.|searchParams|URLSearchParams/)
  })
}
