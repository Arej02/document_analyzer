import test from 'node:test'
import assert from 'node:assert/strict'
import { connectBlocker, isRepoShaped, isTokenShaped, panelMode } from './githubConnection.js'

const TOKEN = `ghp_${'A'.repeat(36)}`

test('repo names: owner/name only, no dot segments or odd characters', () => {
  for (const ok of ['acme/shop', 'a-b_c.d/e.f-g_h', '  acme/shop  ']) assert.equal(isRepoShaped(ok), true, ok)
  for (const bad of ['', 'x', 'a/b/c', '../..', 'a/..', './b', 'a b/c', 'a/b?x=1', 'a/b#f', 'a/b%2f', 'https://github.com/a/b', null, undefined]) {
    assert.equal(isRepoShaped(bad), false, String(bad))
  }
})

test('token shape: GitHub-like tokens pass, short or spaced text does not', () => {
  assert.equal(isTokenShaped(TOKEN), true)
  assert.equal(isTokenShaped(`github_pat_${'a1_'.repeat(20)}`), true)
  assert.equal(isTokenShaped(`  ${TOKEN}\n`), true) // pasted whitespace is trimmed
  for (const bad of ['', 'short', 'has spaces in it which a token never does', null, undefined]) assert.equal(isTokenShaped(bad), false)
})

test('the connect blocker explains the first problem, and is null when ready', () => {
  assert.match(connectBlocker({ repo: '', token: TOKEN }), /owner\/name/)
  assert.match(connectBlocker({ repo: 'nope', token: TOKEN }), /owner\/name/)
  assert.match(connectBlocker({ repo: 'acme/shop', token: '' }), /Paste a GitHub access token/)
  assert.match(connectBlocker({ repo: 'acme/shop', token: 'short' }), /doesn't look like/)
  assert.equal(connectBlocker({ repo: 'acme/shop', token: TOKEN }), null)
})

test('the blocker never repeats the token', () => {
  assert.doesNotMatch(connectBlocker({ repo: 'bad repo', token: TOKEN }), /ghp_/)
  assert.doesNotMatch(connectBlocker({ repo: 'acme/shop', token: 'short secret' }), /short secret/)
})

test('panel modes', () => {
  assert.equal(panelMode({ loading: true }), 'loading')
  assert.equal(panelMode({ loading: false, loadError: 'boom' }), 'error')
  assert.equal(panelMode({ loading: false, connection: { connected: false } }), 'connect')
  assert.equal(panelMode({ loading: false, connection: null }), 'connect')
  assert.equal(panelMode({ loading: false, connection: { connected: true }, editing: false }), 'connected')
  assert.equal(panelMode({ loading: false, connection: { connected: true }, editing: true }), 'rotate')
})
