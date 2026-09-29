import test from 'node:test'
import assert from 'node:assert/strict'
import { outlineIds, sectionOf } from './useOutline.js'

const sections = [{ id: 'a', subs: [{ id: 'a-1' }] }, { id: 'b', subs: [] }]
test('outlineIds lists sections then their subsections in order', () => assert.deepEqual(outlineIds(sections), ['a', 'a-1', 'b']))
test('sectionOf resolves a section or a subsection to its section', () => {
  assert.equal(sectionOf(sections, 'a-1'), 'a')
  assert.equal(sectionOf(sections, 'b'), 'b')
  assert.equal(sectionOf(sections, 'zzz'), undefined)
})
