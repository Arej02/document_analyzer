import test from 'node:test'
import assert from 'node:assert/strict'
import { MAX_ROWS, buildSystemMap } from './systemMapModel.js'

const phases = (n) => Array.from({ length: n }, (_, i) => ({
  phase_cluster_id: `p${i}`, phase_order: i + 1, label: `P${i}`, units: [{ cluster_id: `u${i}`, unit_order: 1, size: 1 }],
}))

test('a small plan stays in one column', () => {
  const map = buildSystemMap({ plan: { phases: phases(MAX_ROWS) }, phaseGraph: { edges: [] } })
  assert.deepEqual([...new Set(map.nodes.map((n) => n.col))], [0])
})

test('a tall level wraps into columns of at most MAX_ROWS, with no overlapping nodes', () => {
  const map = buildSystemMap({ plan: { phases: phases(46) }, phaseGraph: { edges: [] } })
  const perCol = new Map()
  map.nodes.forEach((n) => perCol.set(n.col, (perCol.get(n.col) ?? 0) + 1))
  assert.equal(Math.max(...perCol.values()), MAX_ROWS)
  assert.equal(perCol.size, Math.ceil(46 / MAX_ROWS))
  assert.equal(new Set(map.nodes.map((n) => `${n.x},${n.y}`)).size, 46)
  assert.ok(map.height < MAX_ROWS * 140)
})

test('callers stay left of what they call after wrapping', () => {
  // Plan order lists dependencies first, so the caller is last.
  const list = [...phases(20).slice(1), phases(20)[0]]
  const edges = list.slice(0, -1).map((p) => ({ from: 'p0', to: p.phase_cluster_id }))
  const map = buildSystemMap({ plan: { phases: list }, phaseGraph: { edges } })
  const col = (id) => map.nodes.find((n) => n.id === id).col
  assert.ok(col('p0') < Math.min(...list.slice(0, -1).map((p) => col(p.phase_cluster_id))))
})
