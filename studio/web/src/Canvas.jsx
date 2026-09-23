import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api.js'
import { ReactFlow, ReactFlowProvider, Background, Controls, Handle, Position, MarkerType, useReactFlow } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { ModulePanel, EdgePanel, EvaluatePanel, StepPanel } from './panels.jsx'

export const placeholders = t => { const out = []; const rx = /\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g; let m; while ((m = rx.exec(t || ''))) if (!out.includes(m[1])) out.push(m[1]); return out }
const EVAL = '__evaluate__'

function ModuleNode({ data, selected }) {
  const orch = data.orch
  return (
    <div className={'node' + (selected ? ' selected' : '') + (orch ? ' orch' : '')}>
      <Handle type="target" position={Position.Left} title="input — drop a connection here" />
      <div className={'kind' + (orch ? ' orch' : '')}>{orch ? 'orchestrate' : 'module'}{data.entry ? ' · entry' : ''}{data.unreachable ? ' · not reachable' : ''}</div>
      <div className="title">{data.id}</div>
      <div className="prev">{data.template || <span className="muted">click to add a prompt</span>}</div>
      <div className="chips">{placeholders(data.template).map(p => <span className="chip" key={p}>{'{' + p + '}'}</span>)}</div>
      <div className="hint">drag the right dot to another step to connect</div>
      <Handle type="source" position={Position.Right} title="output — drag to another step's left dot" />
    </div>
  )
}
function EvalNode({ data, selected }) {
  return (
    <div className={'node eval' + (selected ? ' selected' : '')} data-tut="evaluate-node">
      <Handle type="target" position={Position.Left} />
      <div className="kind">evaluate</div>
      <div className="title">{data.label}</div>
      <div className="prev">{data.sub}</div>
    </div>
  )
}
// An external step is not part of the program: the optimizer never rewrites it. It looks different on purpose.
function StepNode({ data, selected }) {
  return (
    <div className={'node step' + (selected ? ' selected' : '') + (data.disabled ? ' off' : '')}>
      <div className="kind">external · not optimized</div>
      <div className="title">{data.id}</div>
      <div className="prev">{data.manifest}{data.disabled ? ' · off' : ''}</div>
      <div className="chips">{(data.outputs || []).map(o => <span className="chip" key={o}>{'{' + o + '}'}</span>)}</div>
      <div className="hint">runs once per row before the program</div>
      <Handle type="source" position={Position.Right} />
    </div>
  )
}
const nodeTypes = { module: ModuleNode, evaluate: EvalNode, step: StepNode }

function Inner({ spec, update, models, features = {}, datasets = [], pid }) {
  const [manifests, setManifests] = useState([])
  useEffect(() => { if (features.v0) api.get('/steps').then(setManifests).catch(() => {}) }, [features.v0])
  const [sel, setSel] = useState(null) // {type:'module'|'edge'|'evaluate', id}
  const rf = useReactFlow()
  const wrap = useRef(null)
  const entry = spec.entry || (spec.modules[0] && spec.modules[0].id)
  const outgoing = id => spec.edges.filter(e => e.source === id)
  const terminals = spec.modules.filter(m => outgoing(m.id).length === 0).map(m => m.id)
  const reachable = useMemo(() => { const seen = new Set(), todo = entry ? [entry] : []; while (todo.length) { const x = todo.pop(); if (seen.has(x)) continue; seen.add(x); outgoing(x).forEach(e => todo.push(e.target)) } return seen }, [spec, entry])

  const nodes = useMemo(() => {
    const ns = spec.modules.map((m, i) => ({
      id: m.id, type: 'module', position: spec.layout[m.id] || { x: 60 + i * 260, y: 120 },
      data: { id: m.id, template: m.template, orch: outgoing(m.id).length > 1, entry: m.id === entry, unreachable: !reachable.has(m.id) },
      selected: sel && sel.type === 'module' && sel.id === m.id,
    }))
    const sc = spec.evaluate.scorers.map(s => s.type).join(', ')
    ns.push({ id: EVAL, type: 'evaluate', position: spec.layout[EVAL] || { x: 60 + spec.modules.length * 260 + 40, y: 120 },
      data: { label: sc || 'no scorer', tut: 'evaluate-node', sub: 'objective: ' + Object.entries(spec.evaluate.objective).map(([k, v]) => `${v >= 0 ? '+' : ''}${v} ${k}`).join(' ') },
      selected: sel && sel.type === 'evaluate' })
    ;(spec.steps || []).forEach((st, i) => {
      const man = manifests.find(m => m.ref === st.manifest || m.id === st.manifest.split('@')[0])
      ns.push({ id: 'step:' + st.id, type: 'step', position: spec.layout['step:' + st.id] || { x: 60 + i * 260, y: -60 },
        data: { id: st.id, manifest: st.manifest, disabled: !st.enabled, outputs: (man ? man.outputs : []).map(o => st.id + '_' + o) },
        selected: sel && sel.type === 'step' && sel.id === st.id })
    })
    return ns
  }, [spec, sel, entry, manifests])

  const edges = useMemo(() => {
    const es = spec.edges.map((e, i) => ({
      id: `e${i}`, source: e.source, target: e.target, label: e.name || (Object.keys(e.mapping).length ? Object.entries(e.mapping).map(([p, f]) => `{${p}}←${f}`).join(' ') : ''),
      selected: sel && sel.type === 'edge' && sel.id === i, markerEnd: { type: MarkerType.ArrowClosed },
      style: e.default ? { strokeDasharray: '0' } : undefined, data: { idx: i },
      labelStyle: e.default && outgoing(e.source).length > 1 ? { fontWeight: 700 } : undefined,
    }))
    ;(spec.steps || []).forEach(st => entry && es.push({ id: 'st-' + st.id, source: 'step:' + st.id, target: entry, selectable: false,
      style: { strokeDasharray: '2 5', stroke: st.enabled ? 'var(--accent2)' : 'var(--line)' }, label: 'fields' }))
    terminals.forEach(t => es.push({ id: 'ev-' + t, source: t, target: EVAL, selectable: false, markerEnd: { type: MarkerType.ArrowClosed },
      style: reachable.has(t) ? { strokeDasharray: '5 4' } : { strokeDasharray: '3 4', stroke: 'var(--danger)' }, label: reachable.has(t) ? undefined : 'not reachable from the entry' }))
    return es
  }, [spec, sel])

  const onNodesChange = useCallback(changes => {
    const moves = changes.filter(c => c.type === 'position' && c.position && !c.dragging)
    if (moves.length) update(s => ({ ...s, layout: { ...s.layout, ...Object.fromEntries(moves.map(c => [c.id, c.position])) } }))
    const removed = changes.filter(c => c.type === 'remove' && c.id !== EVAL).map(c => c.id)
    if (removed.length) {
      const steps = removed.filter(r => r.startsWith('step:')).map(r => r.slice(5))
      update(s => ({ ...s, modules: s.modules.filter(m => !removed.includes(m.id)), steps: (s.steps || []).filter(x => !steps.includes(x.id)),
                     edges: s.edges.filter(e => !removed.includes(e.source) && !removed.includes(e.target)) }))
      setSel(null)
    }
  }, [update])

  const onEdgesChange = useCallback(changes => {
    const removed = changes.filter(c => c.type === 'remove' && c.id.startsWith('e')).map(c => parseInt(c.id.slice(1)))
    if (removed.length) { update(s => ({ ...s, edges: s.edges.filter((_, i) => !removed.includes(i)) })); setSel(null) }
  }, [update])

  const onConnect = useCallback(c => {
    if (c.target === EVAL || c.source === EVAL) return
    update(s => {
      if (s.edges.some(e => e.source === c.source && e.target === c.target)) return s
      const siblings = s.edges.filter(e => e.source === c.source)
      const name = siblings.length ? `option${siblings.length + 1}` : ''
      const edges = [...s.edges, { source: c.source, target: c.target, name, mapping: {}, default: false }]
      if (siblings.length === 1 && !siblings[0].name) { // becoming an orchestrator: name the first edge, make it default
        const i = edges.findIndex(e => e === siblings[0]); edges[i] = { ...siblings[0], name: 'option1', default: true }
      }
      return { ...s, edges }
    })
  }, [update])

  const onDrop = useCallback(ev => {
    ev.preventDefault()
    const kind = ev.dataTransfer.getData('kind'); if (!kind) return
    const pos = rf.screenToFlowPosition({ x: ev.clientX, y: ev.clientY })
    if (kind === 'step') {
      const man = manifests[0]
      if (!man) return
      update(s => {
        let id = man.id, n = 1
        while ((s.steps || []).some(x => x.id === id)) id = `${man.id}${++n}`
        return { ...s, steps: [...(s.steps || []), { id, manifest: man.ref, manifest_sha: '', inputs: {}, credential_id: null, tunables: {}, enabled: true }],
                 layout: { ...s.layout, ['step:' + id]: pos } }
      })
      return
    }
    update(s => {
      let base = kind === 'orchestrate' ? 'decide' : 'step'; let id = base; let n = 1
      while (s.modules.some(m => m.id === id)) id = `${base}${++n}`
      const template = kind === 'orchestrate' ? 'Given this draft:\n{draft}\n\nDecide whether it is good enough or needs another pass.' : ''
      const schema_fields = kind === 'orchestrate' ? [{ name: 'reason', type: 'string', description: 'one sentence', enum: null }] : []
      return { ...s, modules: [...s.modules, { id, template, description: '', schema_fields, model: null, max_tokens: 512 }], layout: { ...s.layout, [id]: pos } }
    })
    setTimeout(() => rf.fitView({ padding: 0.25, duration: 200 }), 50)
  }, [rf, update, manifests])

  useEffect(() => { const id = setTimeout(() => rf.fitView({ padding: 0.25, duration: 200 }), 60); return () => clearTimeout(id) }, [spec.modules.length])
  const onNodeClick = useCallback((_, n) => setSel(n.id === EVAL ? { type: 'evaluate' }
    : n.id.startsWith('step:') ? { type: 'step', id: n.id.slice(5) } : { type: 'module', id: n.id }), [])
  const onEdgeClick = useCallback((_, e) => { if (e.data) setSel({ type: 'edge', id: e.data.idx }) }, [])
  const onPaneClick = useCallback(() => setSel(null), [])

  return (
    <>
      <div className="palette">
        <h4>Palette</h4>
        <div className="pal" data-tut="palette-module" draggable onDragStart={e => e.dataTransfer.setData('kind', 'module')}><b>Module</b><small>a prompt step</small></div>
        <div className="pal" data-tut="palette-orchestrate" draggable onDragStart={e => e.dataTransfer.setData('kind', 'orchestrate')}><b>Orchestrate</b><small>a step that chooses the next step</small></div>
        {features.v0 && manifests.length > 0 && <div className="pal" draggable onDragStart={e => e.dataTransfer.setData('kind', 'step')}><b>External step</b><small>fetch data the prompts can use — never rewritten</small></div>}
        <div className="help" style={{ marginTop: 10 }}>Drag onto the canvas. Connect steps by dragging from a right handle to a left handle. A step with two or more outgoing edges becomes an orchestrator: the model returns <code>next</code>.</div>
        <div className="help">Steps with no outgoing edge feed <b>Evaluate</b>. Click it to choose the scorer.</div>
        <div className="help">The search loop is fixed; its knobs are under <b>Optimizer ⚙</b>.</div>
      </div>
      <div className="canvas" data-tut="canvas" ref={wrap} onDragOver={e => { e.preventDefault(); e.dataTransfer.dropEffect = 'move' }} onDrop={onDrop}>
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect}
          onNodeClick={onNodeClick} onEdgeClick={onEdgeClick} onPaneClick={onPaneClick} fitView deleteKeyCode={['Backspace', 'Delete']} proOptions={{ hideAttribution: true }}>
          <Background gap={22} color="#1b2646" /><Controls />
        </ReactFlow>
      </div>
      <div className="side">
        {sel && sel.type === 'module' && <ModulePanel spec={spec} update={update} id={sel.id} models={models} onClose={() => setSel(null)} />}
        {sel && sel.type === 'edge' && <EdgePanel spec={spec} update={update} idx={sel.id} onClose={() => setSel(null)} />}
        {sel && sel.type === 'evaluate' && <EvaluatePanel spec={spec} update={update} models={models} onClose={() => setSel(null)} />}
        {sel && sel.type === 'step' && <StepPanel spec={spec} update={update} id={sel.id} manifests={manifests} datasets={datasets} pid={pid} onClose={() => setSel(null)} />}
        {!sel && <div className="muted"><h3 style={{ color: 'var(--text)' }}>Program</h3>
          <p>{spec.modules.length} step{spec.modules.length === 1 ? '' : 's'}, {spec.edges.length} edge{spec.edges.length === 1 ? '' : 's'}. Entry: <code>{entry || '—'}</code>. Terminal: <code>{terminals.join(', ') || '—'}</code>.</p>
          <p>Click a step, an edge or the Evaluate block to edit it. Delete with Backspace.</p>
          <label>Evaluation model (default for every step)</label>
          <select value={spec.eval_model} onChange={e => update({ ...spec, eval_model: e.target.value })}>{!models.some(m => m.id === spec.eval_model) && <option value={spec.eval_model}>{spec.eval_model} (not available on your plan)</option>}{models.map(m => <option key={m.id} value={m.id}>{m.label}</option>)}</select>
          <label>Max steps per example (loops)</label>
          <input type="number" min={1} max={32} value={spec.max_steps} onChange={e => update({ ...spec, max_steps: +e.target.value })} />
        </div>}
      </div>
    </>
  )
}

export default function Canvas(props) { return <ReactFlowProvider><Inner {...props} /></ReactFlowProvider> }
