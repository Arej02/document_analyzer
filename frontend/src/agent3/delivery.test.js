import test from 'node:test'
import assert from 'node:assert/strict'
import { deliverBlocker, isActive, newestDelivery, parityNote, prLabel, safePrUrl } from './delivery.js'

const connected = { connected: true, repo_full_name: 'acme/shop' }

test('only queued and running deliveries are active', () => {
  assert.equal(isActive('queued'), true)
  assert.equal(isActive('running'), true)
  for (const status of ['done', 'failed', undefined, null, '']) assert.equal(isActive(status), false)
})

test('links are only ever real GitHub pull request URLs', () => {
  assert.equal(safePrUrl('https://github.com/acme/shop/pull/12'), 'https://github.com/acme/shop/pull/12')
  for (const bad of [
    'javascript:alert(1)',
    'http://github.com/acme/shop/pull/1',
    'https://evil.example/acme/shop/pull/1',
    'https://github.com.evil.example/a/b/pull/1',
    'https://github.com/acme/shop/pull/1/files',
    'https://github.com/acme/shop/issues/1',
    'https://github.com/acme/shop/pull/1" onclick="x',
    '', null, undefined, 12,
  ]) {
    assert.equal(safePrUrl(bad), null, String(bad))
  }
})

test('the deliver blocker says why, in priority order, and is null when ready', () => {
  assert.match(deliverBlocker({ hasOutput: false, connection: connected }), /once this execution finishes/)
  assert.match(deliverBlocker({ hasOutput: true, connection: { connected: false } }), /Connect a GitHub repository/)
  assert.match(deliverBlocker({ hasOutput: true, connection: null }), /Connect a GitHub repository/)
  assert.match(deliverBlocker({ hasOutput: true, connection: connected, latest: { status: 'running' } }), /already running/)
  assert.equal(deliverBlocker({ hasOutput: true, connection: connected, latest: { status: 'failed' } }), null)
  assert.equal(deliverBlocker({ hasOutput: true, connection: connected, latest: { status: 'done' } }), null)
  assert.equal(deliverBlocker({ hasOutput: true, connection: connected }), null)
})

test('the newest delivery is the first one the API returns', () => {
  assert.deepEqual(newestDelivery([{ delivery_id: 'b' }, { delivery_id: 'a' }]), { delivery_id: 'b' })
  assert.equal(newestDelivery([]), null)
  assert.equal(newestDelivery(undefined), null)
})

test('the zip-parity note appears only when it was observed to be true', () => {
  assert.equal(parityNote({ status: 'done', root_matches_baseline: true }), 'Commit hashes match the downloaded zip.')
  for (const delivery of [
    { status: 'done', root_matches_baseline: false },
    { status: 'done', root_matches_baseline: null },
    { status: 'failed', root_matches_baseline: true },
    { status: 'running', root_matches_baseline: true },
    null,
  ]) {
    assert.equal(parityNote(delivery), null)
  }
})

test('pull request labels', () => {
  assert.equal(prLabel({ kind: 'scaffold' }), 'Project scaffold')
  assert.equal(prLabel({ kind: 'phase', phase_order: 2, label: 'Checkout' }), 'Phase 2 · Checkout')
  assert.equal(prLabel({ kind: 'phase', phase_order: 3, label: null }), 'Phase 3')
})
