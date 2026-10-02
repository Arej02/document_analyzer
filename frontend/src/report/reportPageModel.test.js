import test from 'node:test'
import assert from 'node:assert/strict'
import { OUT_OF_DATE_MESSAGE, documentState, headerView, pdfButtonState, pdfFilename, shareLinkView, shareUrl, shouldPoll } from './reportPageModel.js'

test('header for an x-ray has no plan text', () => {
  const view = headerView({ tier: 'xray', exec_summary_status: 'verified', out_of_date: false, created_at: 't' })
  assert.deepEqual([view.tier, view.plan, view.outOfDate, view.flags], ['Codebase X-ray', null, false, []])
})
test('header for a full report shows plan version and status, and a change since generation', () => {
  const same = headerView({ tier: 'full', plan_version: 4, plan_status: 'approved', plan_status_at_generation: 'approved' })
  assert.equal(same.plan, 'Plan v4, approved')
  const changed = headerView({ tier: 'full', plan_version: 4, plan_status: 'stale', plan_status_at_generation: 'in_review', out_of_date: true })
  assert.equal(changed.plan, 'Plan v4, stale (was in review when generated)')
  assert.equal(changed.outOfDate, true)
})
test('header flags an unverified or withheld summary', () => {
  assert.deepEqual(headerView({ tier: 'xray', exec_summary_status: 'unverified' }).flags, ['Executive summary unverified'])
  assert.deepEqual(headerView({ tier: 'xray', exec_summary_status: 'withheld' }).flags, ['Executive summary withheld'])
})
test('the out-of-date message says what to do', () => {
  assert.match(OUT_OF_DATE_MESSAGE, /out of date/)
  assert.match(OUT_OF_DATE_MESSAGE, /project page/)
})

test('pdf button: enabled only when done; always a reason when disabled', () => {
  assert.deepEqual(pdfButtonState({ pdf_status: 'done' }), { disabled: false, reason: null, canRetry: false })
  const pending = pdfButtonState({ pdf_status: 'pending', has_html: true })
  assert.equal(pending.disabled, true)
  assert.match(pending.reason, /still being prepared/)
  assert.equal(pending.canRetry, false)
})
test('a failed pdf can be retried only when the html is stored', () => {
  const failed = pdfButtonState({ pdf_status: 'failed', has_html: true })
  assert.deepEqual([failed.disabled, failed.canRetry], [true, true])
  assert.match(failed.reason, /could not be generated/)
  const noHtml = pdfButtonState({ pdf_status: 'failed', has_html: false })
  assert.equal(noHtml.canRetry, false)
  assert.match(noHtml.reason, /no stored report/)
})
test('while a retry is in flight the button reads as pending', () => {
  const state = pdfButtonState({ pdf_status: 'failed', has_html: true }, { retrying: true })
  assert.deepEqual([state.disabled, state.canRetry], [true, false])
  assert.match(state.reason, /still being prepared/)
})
test('unknown pdf status is treated as not ready', () => {
  assert.equal(pdfButtonState({ pdf_status: 'weird', has_html: true }).disabled, true)
})
test('a report from before documents were stored is not "being prepared" and is never polled', () => {
  const legacy = pdfButtonState({ pdf_status: 'pending', has_html: false })
  assert.deepEqual([legacy.disabled, legacy.canRetry], [true, false])
  assert.match(legacy.reason, /no stored PDF/)
  assert.equal(shouldPoll({ pdf_status: 'pending', has_html: false }), false)
  assert.equal(shouldPoll({ pdf_status: 'pending', has_html: true }), true)
})

test('polling continues only while the pdf is pending or a retry is running', () => {
  assert.equal(shouldPoll({ pdf_status: 'pending', has_html: true }), true)
  assert.equal(shouldPoll({ pdf_status: 'done' }), false)
  assert.equal(shouldPoll({ pdf_status: 'failed' }), false)
  assert.equal(shouldPoll({ pdf_status: 'failed' }, true), true)
  assert.equal(shouldPoll(null), false)
})

test('download file name: the server\'s safe name, else a derived one', () => {
  assert.equal(pdfFilename({ tier: 'xray', id: 'abcdef123456' }, 'attachment; filename="report-xray-abcdef12.pdf"'), 'report-xray-abcdef12.pdf')
  assert.equal(pdfFilename({ tier: 'full', id: 'abcdef123456' }, null), 'report-full-abcdef12.pdf')
  assert.equal(pdfFilename({ tier: 'full', id: 'abcdef123456' }, 'attachment; filename="../../etc/passwd"'), 'report-full-abcdef12.pdf')
})

test('document state: missing, error, loading, ready', () => {
  assert.equal(documentState({ has_html: false }, { html: null }).kind, 'missing')
  assert.match(documentState({ has_html: false }, { html: null }).message, /no stored document/)
  assert.deepEqual(documentState({ has_html: true }, { html: null, htmlError: 'boom' }), { kind: 'error', message: 'boom' })
  assert.equal(documentState({ has_html: true }, { html: null }).kind, 'loading')
  assert.equal(documentState({ has_html: true }, { html: '<p>x</p>' }).kind, 'ready')
})
test('the share url is the origin plus the returned path, whatever trailing slash the origin has', () => {
  assert.equal(shareUrl('https://app.example.com', '/?share=abc'), 'https://app.example.com/?share=abc')
  assert.equal(shareUrl('https://app.example.com/', '/?share=abc'), 'https://app.example.com/?share=abc')
})
test('only an active share link can be revoked, and views are pluralised', () => {
  const active = shareLinkView({ id: 'a', status: 'active', access_count: 1, expires_at: 't' })
  assert.deepEqual([active.canRevoke, active.views, active.statusText], [true, '1 view', 'Active'])
  for (const status of ['expired', 'revoked']) assert.equal(shareLinkView({ id: 'b', status, access_count: 0 }).canRevoke, false)
  assert.equal(shareLinkView({ id: 'c', status: 'active', access_count: 0 }).views, '0 views')
  assert.equal(shareLinkView({ id: 'd', status: 'active', access_count: 3, last_accessed_at: 'x' }).lastAccessed, 'x')
})
