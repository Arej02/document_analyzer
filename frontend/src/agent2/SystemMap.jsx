// System map: the legacy application as a call flow, in the style of an architecture diagram. Entry points on the
// left, the application boundary with one node per migration phase (arrows point from caller to callee), and the
// data and packages it uses on the right. Selecting a phase lists what it calls, what calls it, and its units.
import { useMemo, useState } from 'react'
import { Background, Handle, MarkerType, Position, ReactFlow } from '@xyflow/react'
import { Boxes, Database, Globe, Package } from 'lucide-react'
import { formatInt } from '../ui/format.js'
import { NODE_H, NODE_W, SIDE_W, buildSystemMap, phaseLinks } from './systemMapModel.js'
import './agent2.css'

function PhaseNode({ data }) {
  return (
    <div className={`sm-node sm-phase${data.selected ? ' sm-selected' : ''}`} data-risk={data.risk ?? 'none'} style={{ width: NODE_W, height: NODE_H }}>
      <Handle type="target" position={Position.Left} id="in" />
      <Handle type="source" position={Position.Right} id="out" />
      <Handle type="target" position={Position.Top} id="back-in" />
      <Handle type="source" position={Position.Top} id="back-out" />
      <div className="sm-node-head">
        <Boxes size={14} aria-hidden="true" />
        <span className="sm-badge">{data.unitCount} {data.unitCount === 1 ? 'unit' : 'units'}</span>
      </div>
      <div className="sm-node-title" title={data.title}>Phase {data.order} · {data.title}</div>
      <div className="sm-node-sub">{formatInt(data.entities)} entities{data.risk ? ` · ${data.risk} risk` : ''}</div>
    </div>
  )
}

function SideNode({ data }) {
  const Icon = data.icon
  return (
    <div className={`sm-node sm-side-${data.kind}`} style={{ width: SIDE_W }}>
      <Handle type="target" position={Position.Left} id="in" />
      <Handle type="source" position={Position.Right} id="out" />
      <div className="sm-node-head">
        <Icon size={14} aria-hidden="true" />
        <span className="sm-badge">{data.count == null ? 'n/a' : formatInt(data.count)}</span>
      </div>
      <div className="sm-node-title">{data.title}</div>
      <div className="sm-node-sub" title={data.sub}>{data.sub}</div>
    </div>
  )
}

function BoundaryNode({ data }) {
  return (
    <div className="sm-boundary" style={{ width: data.w, height: data.h }}>
      <Handle type="target" position={Position.Left} id="in" />
      <Handle type="source" position={Position.Right} id="out" />
      <span className="sm-boundary-label">Legacy application · {data.phases} {data.phases === 1 ? 'phase' : 'phases'}</span>
    </div>
  )
}

const nodeTypes = { phase: PhaseNode, side: SideNode, boundary: BoundaryNode }
const arrow = (color) => ({ type: MarkerType.ArrowClosed, color })
const edgeLabel = { labelStyle: { fontSize: 10, fill: 'var(--muted-foreground)' }, labelBgStyle: { fill: 'var(--background)' } }
const fixed = { draggable: false, selectable: false }

export default function SystemMap({ plan, phaseGraph, graphError, routes, schemaTotal, packageCount, onSelectUnit }) {
  const [selected, setSelected] = useState(null)
  const map = useMemo(
    () => buildSystemMap({ plan, phaseGraph, routes: routes ?? [], schemaTotal, packageCount }),
    [plan, phaseGraph, routes, schemaTotal, packageCount],
  )

  const flow = useMemo(() => {
    const { boundary, sides } = map
    const nodes = [
      { id: 'boundary', type: 'boundary', position: { x: boundary.x, y: boundary.y }, data: { w: boundary.w, h: boundary.h, phases: map.nodes.length }, zIndex: -1, ...fixed },
      { id: 'entry', type: 'side', position: { x: sides.entry.x, y: sides.entry.y }, ...fixed, data: { kind: 'entry', icon: Globe, title: 'Entry points', count: sides.entry.count, sub: sides.entry.names.length ? sides.entry.names.join(' · ') : 'routes and hooks' } },
      { id: 'data', type: 'side', position: { x: sides.data.x, y: sides.data.y }, ...fixed, data: { kind: 'data', icon: Database, title: 'Data model', count: sides.data.count, sub: 'post types · meta keys' } },
      { id: 'packages', type: 'side', position: { x: sides.packages.x, y: sides.packages.y }, ...fixed, data: { kind: 'pkg', icon: Package, title: 'Packages', count: sides.packages.count, sub: 'declared dependencies' } },
      ...map.nodes.map((n) => ({ id: n.id, type: 'phase', position: { x: n.x, y: n.y }, ...fixed, data: { ...n, selected: n.id === selected } })),
    ]
    const col = new Map(map.nodes.map((n) => [n.id, n.col]))
    const dim = selected != null
    const edges = [
      ...map.edges.map((e) => {
        const facade = e.flag === 'needs_facade'
        const color = facade ? '#ff5c4d' : 'var(--graph-edge)'
        const back = col.get(e.to) <= col.get(e.from)
        const active = !dim || e.from === selected || e.to === selected
        return {
          id: `${e.from}>${e.to}`,
          source: e.from,
          target: e.to,
          sourceHandle: back ? 'back-out' : 'out',
          targetHandle: back ? 'back-in' : 'in',
          type: 'smoothstep',
          ...(back ? { pathOptions: { offset: 24 } } : {}),
          label: facade ? 'cycle · needs facade' : (e.weight ? String(e.weight) : undefined),
          ...edgeLabel,
          markerEnd: arrow(color),
          style: { stroke: color, strokeWidth: dim && active ? 2.4 : 1.6, opacity: active ? 1 : 0.18, ...(facade ? { strokeDasharray: '6 4' } : {}) },
        }
      }),
      ...[['entry', 'boundary', 'requests'], ['boundary', 'data', 'reads and writes'], ['boundary', 'packages', 'uses']].map(([source, target, label]) => ({
        id: `${source}>${target}`,
        source,
        target,
        sourceHandle: 'out',
        targetHandle: 'in',
        type: 'smoothstep',
        animated: true,
        label,
        ...edgeLabel,
        markerEnd: arrow('var(--ring)'),
        style: { stroke: 'var(--ring)', strokeWidth: 1.4, strokeDasharray: '5 5', opacity: dim ? 0.35 : 1 },
      })),
    ]
    return { nodes, edges }
  }, [map, selected])

  if (!map.nodes.length) return <p className="a2-note">This plan has no phases to draw.</p>
  const picked = selected ? map.nodes.find((n) => n.id === selected) : null
  const links = picked ? phaseLinks(map, picked.id) : null
  const height = Math.min(560, Math.max(300, map.height * 0.55 + 60))

  return (
    <div className="sm">
      {graphError ? <p className="a2-note">Phase dependencies could not be loaded ({graphError}), so the boxes are shown without arrows.</p> : null}
      <div className="sm-canvas" style={{ height }}>
        <ReactFlow
          nodes={flow.nodes}
          edges={flow.edges}
          nodeTypes={nodeTypes}
          onNodeClick={(_event, node) => { if (node.type === 'phase') setSelected((cur) => (cur === node.id ? null : node.id)) }}
          onPaneClick={() => setSelected(null)}
          fitView
          fitViewOptions={{ padding: 0.08 }}
          minZoom={0.2}
          maxZoom={1.2}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={18} color="rgba(var(--neutral-rgb),.07)" />
        </ReactFlow>
      </div>
      <ul className="sm-legend" aria-label="Legend">
        <li><i className="sm-key" data-risk="low" />Low risk</li>
        <li><i className="sm-key" data-risk="medium" />Medium risk</li>
        <li><i className="sm-key" data-risk="high" />High risk</li>
        <li><i className="sm-key sm-key-flow" />Calls: the arrow points at what is called, the number counts call links</li>
        <li><i className="sm-key sm-key-ctx" />Dashed: applies to the whole application, not to one phase</li>
      </ul>
      {/* The canvas nodes are not reachable by keyboard, so every phase is also a button here. */}
      <div className="sm-picker" role="group" aria-label="Phases">
        {map.nodes.map((n) => (
          <button
            key={n.id}
            type="button"
            className="sm-pick"
            aria-pressed={n.id === selected}
            onClick={() => setSelected((cur) => (cur === n.id ? null : n.id))}
          >
            Phase {n.order}
          </button>
        ))}
      </div>
      {picked ? (
        <div className="sm-detail" role="region" aria-label={`Phase ${picked.order} detail`}>
          <h4 className="a2-h4">Phase {picked.order} · {picked.title}</h4>
          <div className="sm-detail-grid">
            <div>
              <strong>Calls</strong>
              {links.calls.length
                ? <ul>{links.calls.map((l) => <li key={l.to}>Phase {l.node.order} · {l.node.title}</li>)}</ul>
                : <p className="a2-note">Nothing, it is a leaf.</p>}
            </div>
            <div>
              <strong>Called by</strong>
              {links.calledBy.length
                ? <ul>{links.calledBy.map((l) => <li key={l.from}>Phase {l.node.order} · {l.node.title}</li>)}</ul>
                : <p className="a2-note">No other phase.</p>}
            </div>
            <div>
              <strong>Units</strong>
              <ul>
                {picked.units.map((u) => (
                  <li key={u.id}><button type="button" className="a2-link-btn" onClick={() => onSelectUnit(u.id)}>{picked.order}.{u.order} {u.label}</button></li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      ) : <p className="a2-note">Select a phase, on the map or from the buttons above, to see what it calls, what calls it, and its units.</p>}
    </div>
  )
}
