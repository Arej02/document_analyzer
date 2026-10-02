import test from 'node:test'
import assert from 'node:assert/strict'
import { publicPdfFilename, shareTokenFromSearch, snapshotHeading } from './publicReportModel.js'

const TOKEN = 'a'.repeat(43)

test('only a well-formed share token is picked up from the URL', () => {
  assert.equal(shareTokenFromSearch(`?share=${TOKEN}`), TOKEN)
  assert.equal(shareTokenFromSearch(`?report=r1&share=${TOKEN}`), TOKEN)
  for (const search of ['', '?share=', '?share=short', `?share=${TOKEN}x`, `?share=${'!'.repeat(43)}`, '?report=r1']) {
    assert.equal(shareTokenFromSearch(search), null)
  }
})
test('the heading says snapshot, with the date when there is a usable one', () => {
  assert.match(snapshotHeading('2026-10-01T12:00:00+00:00'), /^Snapshot as of .+/)
  assert.equal(snapshotHeading(null), 'Snapshot')
  assert.equal(snapshotHeading('not a date'), 'Snapshot')
})
test('the download name is the server one when it is plain, else a safe default', () => {
  assert.equal(publicPdfFilename('attachment; filename="report-abcd1234.pdf"'), 'report-abcd1234.pdf')
  assert.equal(publicPdfFilename('attachment; filename="../x.pdf"'), 'report.pdf')
  assert.equal(publicPdfFilename(null), 'report.pdf')
})
