import React, { useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import { placeholders } from './Canvas.jsx'

export const EXAMPLE = {
  summarize: 'Read this customer message and state in one short line what the customer wants and which product or order it concerns.\n\nMessage: {message}',
  route: 'Assign this support request to one queue: billing, shipping, refund, bug, account, or other.\n\nRequest: {summary}',
}

// Each step: anchor = data-tut selector to spotlight; tab = tab to switch to; check(spec, ctx) -> done (action steps);
// action = { label, run(ctx) } does the step for the user. Steps without check are informational.
export const STEPS = [
  { title: 'Welcome to the studio', body: 'You will build a small program of prompt steps, attach a labelled dataset, and let the optimizer rewrite the prompts until they score better. The example is support-ticket triage: a message comes in, one step summarizes it, a second step picks the queue. Twelve short steps; skip any time.' },
  { title: 'Drag a Module onto the canvas', anchor: 'palette-module', tab: 'build',
    body: 'A Module is one prompt step. Press on it in the palette and drop it anywhere on the canvas.',
    check: s => s.modules.length >= 1, action: { label: 'Add it for me', run: ctx => ctx.addModule('summarize', EXAMPLE.summarize, 'summary', 'summarizes what a customer wants in one line') } },
  { title: 'Click it and write the prompt', anchor: 'side-template', tab: 'build',
    body: 'Click the step to open it on the right. Words in {braces} are placeholders; they are filled from your dataset\'s columns. Rename the step "summarize" and use this prompt:\n\n' + EXAMPLE.summarize,
    check: s => s.modules.some(m => placeholders(m.template).length > 0), action: { label: 'Use this prompt', run: ctx => ctx.setModule(0, { id: 'summarize', template: EXAMPLE.summarize, description: 'summarizes what a customer wants in one line' }) } },
  { title: 'Add an output field', anchor: 'side-fields', tab: 'build',
    body: 'Steps return structured fields rather than loose text, so the next step and the scorer can pick one out. Add a field named "summary".',
    check: s => s.modules.some(m => m.schema_fields.length > 0), action: { label: 'Add "summary"', run: ctx => ctx.setModule(0, { schema_fields: [{ name: 'summary', type: 'string', description: 'one line', enum: null }] }) } },
  { title: 'Drag a second Module', anchor: 'palette-module', tab: 'build',
    body: 'This one will route the ticket. Drop it to the right of the first step. Name it "route", give it an output field "queue", and use this prompt:\n\n' + EXAMPLE.route,
    check: s => s.modules.length >= 2, action: { label: 'Add it for me', run: ctx => ctx.addModule('route', EXAMPLE.route, 'queue', 'assigns a support request to a queue') } },
  { title: 'Connect the two steps', anchor: 'canvas', tab: 'build',
    body: 'Each step has a dot on its left (input) and right (output). Press on the first step\'s RIGHT dot, drag, and release on the second step\'s LEFT dot. A solid edge with an arrow appears.\n\nThe dashed lines you saw go to Evaluate: they only mean "this step currently ends the pipeline". They appear and disappear on their own; solid edges are yours.',
    check: s => s.edges.length >= 1, action: { label: 'Connect for me', run: ctx => ctx.connect(0, 1) } },
  { title: 'Map what the second step receives', anchor: 'edge-mapping', tab: 'build',
    body: 'Click the edge. The route step has a {summary} placeholder; map it to the summarize step\'s "summary" field. Every step can also read the dataset columns directly, so {message} would work in either step.',
    check: s => s.edges.some(e => Object.keys(e.mapping).length > 0), action: { label: 'Map it for me', run: ctx => ctx.mapEdge(0, { summary: 'summary' }) } },
  { title: 'Orchestrate (for later)', anchor: 'palette-orchestrate', tab: 'build',
    body: 'A step with two or more outgoing edges becomes an orchestrator: the model returns a "next" field naming the edge to take, one edge is the default if that cannot be parsed, and loops back to earlier steps are allowed, capped by "max steps per example". Not needed for this example.' },
  { title: 'Tell Evaluate what to grade', anchor: 'evaluate-node', tab: 'build',
    body: 'The step with no outgoing edge is what gets graded, here "route". Click the Evaluate block and set the scorer to compare the output field "queue" with the label. The objective weighs the metrics; accuracy alone is fine here.',
    check: s => s.evaluate.scorers.some(sc => sc.field), action: { label: 'Set it for me', run: ctx => ctx.setScorerField('queue') } },
  { title: 'The optimizer', anchor: 'btn-optimizer', tab: 'build',
    body: 'The loop is fixed: pick a parent prompt from the Pareto pool, show a reflection model a few of its failures, rewrite one step, and keep the child only if it beats its parent on the same rows. The gear opens its knobs: rounds, budget, minibatch, GEPA vs BO for choosing parents. Your tier caps how many run in parallel. Two rounds is enough for the tutorial.' },
  { title: 'Add labelled data', anchor: 'tab-data', tab: 'data',
    body: 'This is where the labelled data goes. Download the sample (50 tickets: message + queue), upload it, map {message} to the message column and choose "queue" as the label. Then "Check data" validates it and "Run pilot" measures the baseline, the model\'s noise, and the cost before you spend anything.',
    check: (s, ctx) => ctx.datasets.length > 0, action: { label: 'Load the sample for me', run: ctx => ctx.loadSample() } },
  { title: 'Models & keys', anchor: 'btn-keys', tab: 'data',
    body: 'Your tier runs on the site\'s models. You can also add your own endpoint and key (Bedrock, Anthropic, OpenAI, or any OpenAI-compatible server); models from your endpoints then appear in every dropdown and are used in preference to ours.' },
  { title: 'Run it', anchor: 'tab-run', tab: 'run',
    body: 'Start a run here and watch: the best-so-far curve with its noise bands, the tree of rewrites, and, for any node, the diff against its parent and the rows the reflector saw. The hold-out score at the end is on rows the search never touched. That\'s the whole loop - enjoy.' },
]

export function tutorialVisible(spec) {
  const t = (spec.layout && spec.layout.tutorial) || {}
  return spec.modules.length === 0 && !t.dismissed
}

export default function Tutorial({ spec, update, datasets, reload, pid, tab, setTab, onClose }) {
  const t = (spec.layout && spec.layout.tutorial) || {}
  const [i, setI] = useState(t.step || 0)
  const [rect, setRect] = useState(null)
  const [doneFlash, setDoneFlash] = useState(false)
  const step = STEPS[i]
  const save = patch => update(s => ({ ...s, layout: { ...s.layout, tutorial: { ...(s.layout.tutorial || {}), ...patch } } }))
  const ctx = useMemo(() => ({
    datasets,
    addModule: (id, template, field, description) => update(s => {
      if (s.modules.some(m => m.id === id)) return s
      const n = s.modules.length
      return { ...s, modules: [...s.modules, { id, template, description, schema_fields: [{ name: field, type: 'string', description: '', enum: null }], model: null, max_tokens: 256 }],
        layout: { ...s.layout, [id]: { x: 80 + n * 320, y: 160 } } }
    }),
    setModule: (idx, patch) => update(s => {
      const m = s.modules[idx]; if (!m) return s
      const nid = patch.id || m.id
      return { ...s, modules: s.modules.map((x, j) => j === idx ? { ...x, ...patch } : x),
        edges: s.edges.map(e => ({ ...e, source: e.source === m.id ? nid : e.source, target: e.target === m.id ? nid : e.target })),
        layout: { ...s.layout, [nid]: s.layout[m.id] || s.layout[nid] } }
    }),
    connect: (a, b) => update(s => (s.modules.length < 2 || s.edges.length) ? s : { ...s, edges: [{ source: s.modules[a].id, target: s.modules[b].id, name: '', mapping: {}, default: false }] }),
    mapEdge: (idx, mapping) => update(s => ({ ...s, edges: s.edges.map((e, j) => j === idx ? { ...e, mapping } : e) })),
    setScorerField: f => update(s => ({ ...s, evaluate: { ...s.evaluate, scorers: s.evaluate.scorers.map((sc, j) => j === 0 ? { ...sc, type: 'exact_match', field: f } : sc), objective: { accuracy: 1 } } })),
    loadSample: async () => { await api.post(`/projects/${pid}/datasets/sample`); await reload() },
  }), [update, datasets, pid, reload])

  useEffect(() => { if (step.tab && step.tab !== tab) setTab(step.tab) }, [i])
  useEffect(() => { save({ step: i }) }, [i])
  // spotlight: re-measure the anchor a few times a second (panels open, nodes move)
  useEffect(() => {
    const tick = () => {
      const el = step.anchor && document.querySelector(`[data-tut="${step.anchor}"]`)
      setRect(el ? el.getBoundingClientRect() : null)
    }
    tick(); const id = setInterval(tick, 300); return () => clearInterval(id)
  }, [i, step.anchor])
  const done = step.check ? !!step.check(spec, ctx) : true
  useEffect(() => {
    if (step.check && done) { setDoneFlash(true); const id = setTimeout(() => { setDoneFlash(false); if (i < STEPS.length - 1) setI(i + 1) }, 900); return () => clearTimeout(id) }
  }, [done, i])
  const finish = () => { save({ dismissed: true, step: 0 }); onClose() }
  const pos = rect ? { top: Math.min(window.innerHeight - 320, rect.bottom + 12), left: Math.max(12, Math.min(window.innerWidth - 400, rect.left)) } : { top: 90, left: Math.max(12, window.innerWidth / 2 - 190) }
  return (
    <>
      {rect && <div className="tut-hole" style={{ top: rect.top - 6, left: rect.left - 6, width: rect.width + 12, height: rect.height + 12 }} />}
      {!rect && <div className="tut-dim" />}
      <div className="tut-pop" style={pos}>
        <div className="row"><b>{step.title}</b><div className="grow" /><span className="muted" style={{ fontSize: 12 }}>{i + 1} / {STEPS.length}</span></div>
        <div style={{ whiteSpace: 'pre-wrap', margin: '8px 0 10px', fontSize: 13.5 }}>{step.body}</div>
        {step.check && !done && <div className="muted" style={{ fontSize: 12.5, marginBottom: 8 }}>Do it on the page to continue{step.action ? ', or:' : '.'} {step.action && <button className="small" onClick={() => step.action.run(ctx)}>{step.action.label}</button>}</div>}
        {doneFlash && <div className="ok" style={{ marginBottom: 8 }}>✓ done</div>}
        <div className="row">
          <button className="small" disabled={i === 0} onClick={() => setI(i - 1)}>Back</button>
          <button className="small" onClick={finish}>Skip tutorial</button>
          <div className="grow" />
          {i < STEPS.length - 1 ? <button className="small primary" disabled={!!step.check && !done} onClick={() => setI(i + 1)}>Next</button>
            : <button className="small primary" onClick={finish}>Done</button>}
        </div>
      </div>
    </>
  )
}
