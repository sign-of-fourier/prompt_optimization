import React, { useState } from 'react'
import { placeholders } from './Canvas.jsx'

const TYPES = ['string', 'number', 'boolean', 'string[]']

export function ModulePanel({ spec, update, id, models, onClose }) {
  const m = spec.modules.find(x => x.id === id)
  if (!m) return null
  const set = patch => update(s => ({ ...s, modules: s.modules.map(x => x.id === id ? { ...x, ...patch } : x) }))
  const rename = nid => {
    nid = nid.replace(/[^a-zA-Z0-9_]/g, '_')
    if (!nid || spec.modules.some(x => x.id === nid)) return
    update(s => ({ ...s, modules: s.modules.map(x => x.id === id ? { ...x, id: nid } : x),
      edges: s.edges.map(e => ({ ...e, source: e.source === id ? nid : e.source, target: e.target === id ? nid : e.target })),
      entry: s.entry === id ? nid : s.entry, layout: { ...s.layout, [nid]: s.layout[id] }, mutate: s.mutate ? s.mutate.map(x => x === id ? nid : x) : null }))
  }
  const outs = spec.edges.filter(e => e.source === id)
  const orch = outs.length > 1
  const entry = spec.entry || (spec.modules[0] && spec.modules[0].id)
  const incoming = new Set(spec.edges.filter(e => e.target === id).flatMap(e => Object.keys(e.mapping)))
  const mutated = !spec.mutate || spec.mutate.includes(id)
  return (
    <div>
      <div className="row"><h3 className="grow">{orch ? 'Orchestrate' : 'Module'}: {m.id}</h3><button className="small" onClick={onClose}>×</button></div>
      <label>Name</label><input defaultValue={m.id} onBlur={e => rename(e.target.value)} />
      <label>What this step does <span className="muted">(read by the rewriter)</span></label>
      <input value={m.description} onChange={e => set({ description: e.target.value })} placeholder="e.g. extracts the key facts from the document" />
      <label>Prompt template <span className="muted">— placeholders in {'{braces}'}</span></label>
      <textarea data-tut="side-template" value={m.template} onChange={e => set({ template: e.target.value })} placeholder={'Summarize the following text in one sentence:\n\n{text}'} />
      <div className="chips" style={{ marginTop: 6 }}>{placeholders(m.template).map(p => <span key={p} className={'chip' + (incoming.has(p) ? '' : '')} title={incoming.has(p) ? 'fed by an edge' : 'from the dataset (or an earlier step)'}>{'{' + p + '}'}{incoming.has(p) ? ' ←edge' : ''}</span>)}</div>
      <label>Output fields <span className="muted">(structured output; empty = free text)</span></label>
      <div data-tut="side-fields">
      {m.schema_fields.map((f, i) => (
        <div className="row" key={i} style={{ marginBottom: 4 }}>
          <input style={{ width: 130 }} value={f.name} placeholder="field" onChange={e => set({ schema_fields: m.schema_fields.map((g, j) => j === i ? { ...g, name: e.target.value.replace(/[^a-zA-Z0-9_]/g, '_') } : g) })} />
          <select style={{ width: 100 }} value={f.type} onChange={e => set({ schema_fields: m.schema_fields.map((g, j) => j === i ? { ...g, type: e.target.value } : g) })}>{TYPES.map(t => <option key={t}>{t}</option>)}</select>
          <input className="grow" value={f.description || ''} placeholder="description" onChange={e => set({ schema_fields: m.schema_fields.map((g, j) => j === i ? { ...g, description: e.target.value } : g) })} />
          <button className="small" onClick={() => set({ schema_fields: m.schema_fields.filter((_, j) => j !== i) })}>×</button>
        </div>
      ))}
      <button className="small" onClick={() => set({ schema_fields: [...m.schema_fields, { name: `field${m.schema_fields.length + 1}`, type: 'string', description: '', enum: null }] })}>+ field</button>
      </div>
      <label>Connect to <span className="muted">(same as dragging the right dot)</span></label>
      <div className="row">
        <select className="grow" value="" onChange={e => { const t = e.target.value; if (!t) return; update(s => s.edges.some(x => x.source === id && x.target === t) ? s : { ...s, edges: [...s.edges, { source: id, target: t, name: outs.length ? `option${outs.length + 1}` : '', mapping: {}, default: false }] }) }}>
          <option value="">— choose a step —</option>{spec.modules.filter(x => x.id !== id && !outs.some(e => e.target === x.id)).map(x => <option key={x.id} value={x.id}>{x.id}</option>)}
        </select>
      </div>
      {outs.length > 0 && <div className="help">Outgoing: {outs.map(e => e.target).join(', ')}{outs.length === 0 ? '' : ''}. {outs.length === 0 ? '' : 'Click an edge to map fields.'}</div>}
      {outs.length === 0 && <div className="help">No outgoing edge: this step ends the pipeline and its output is what Evaluate grades.</div>}
      {orch && <div className="help" style={{ marginTop: 8 }}>This step has {outs.length} outgoing edges, so a <code>next</code> field is added automatically with the options <b>{outs.map(e => e.name || '(unnamed)').join(', ')}</b>. Name the edges by clicking them. Tell the model in the prompt what each option means.</div>}
      <label>Model</label>
      <select value={m.model || ''} onChange={e => set({ model: e.target.value || null })}><option value="">project default ({spec.eval_model})</option>{models.map(x => <option key={x.id} value={x.id}>{x.label}</option>)}</select>
      <div className="help">Evaluation calls run at temperature 0 (Nova's default of 0.7 makes scores stochastic; Anthropic models reject sampling parameters). Diversity in rewrites comes from the optimizer's reflection settings, not from here.</div>
      <label>Max output tokens</label><input type="number" value={m.max_tokens} onChange={e => set({ max_tokens: +e.target.value })} />
      <div className="row" style={{ marginTop: 12 }}>
        <label style={{ margin: 0 }}><input type="checkbox" style={{ width: 'auto', marginRight: 6 }} checked={m.id === entry} onChange={() => update({ ...spec, entry: id })} />entry step</label>
        <label style={{ margin: 0 }}><input type="checkbox" style={{ width: 'auto', marginRight: 6 }} checked={mutated} onChange={e => {
          const all = spec.modules.map(x => x.id); const cur = spec.mutate || all
          const next = e.target.checked ? [...cur, id] : cur.filter(x => x !== id)
          update({ ...spec, mutate: next.length === all.length ? null : next })
        }} />rewritten by the optimizer</label>
      </div>
    </div>
  )
}

export function EdgePanel({ spec, update, idx, onClose }) {
  const e = spec.edges[idx]
  if (!e) return null
  const set = patch => update(s => ({ ...s, edges: s.edges.map((x, i) => i === idx ? { ...x, ...patch } : x) }))
  const src = spec.modules.find(m => m.id === e.source), tgt = spec.modules.find(m => m.id === e.target)
  const fields = ['$text', ...(src ? src.schema_fields.map(f => f.name) : [])]
  const phs = tgt ? placeholders(tgt.template) : []
  const orch = spec.edges.filter(x => x.source === e.source).length > 1
  const setMap = (ph, field) => set({ mapping: Object.fromEntries(Object.entries({ ...e.mapping, [ph]: field }).filter(([, v]) => v)) })
  return (
    <div>
      <div className="row"><h3 className="grow">Edge {e.source} → {e.target}</h3><button className="small" onClick={onClose}>×</button></div>
      {orch && <>
        <label>Option name <span className="muted">(the value of <code>next</code> that takes this edge)</span></label>
        <input value={e.name} onChange={ev => set({ name: ev.target.value.replace(/[^a-zA-Z0-9_-]/g, '_') })} />
        <label style={{ margin: '10px 0 0' }}><input type="checkbox" style={{ width: 'auto', marginRight: 6 }} checked={e.default} onChange={ev => update(s => ({ ...s, edges: s.edges.map((x, i) => x.source === e.source ? { ...x, default: i === idx ? ev.target.checked : (ev.target.checked ? false : x.default) } : x) }))} />default edge — taken when the model's <code>next</code> cannot be parsed</label>
      </>}
      <label data-tut="edge-mapping">What {e.target} receives</label>
      <div className="help">Every step can read the dataset columns and anything mapped earlier on the path. Map a placeholder of <b>{e.target}</b> to an output field of <b>{e.source}</b> here (<code>$text</code> = the raw text).</div>
      {phs.length === 0 && <div className="muted">{e.target} has no placeholders.</div>}
      {phs.map(ph => (
        <div className="row" key={ph} style={{ marginBottom: 6 }}>
          <code style={{ width: 130 }}>{'{' + ph + '}'}</code><span className="muted">←</span>
          <select className="grow" value={e.mapping[ph] || ''} onChange={ev => setMap(ph, ev.target.value)}>
            <option value="">(dataset column or earlier step)</option>{fields.map(f => <option key={f} value={f}>{f}</option>)}
          </select>
        </div>
      ))}
      <button className="danger small" style={{ marginTop: 10 }} onClick={() => { update(s => ({ ...s, edges: s.edges.filter((_, i) => i !== idx) })); onClose() }}>delete edge</button>
    </div>
  )
}

const SCORERS = [
  ['exact_match', 'Exact match (normalized)', 'label equals the output field'],
  ['contains', 'Contains', 'the label appears in the output'],
  ['token_f1', 'Token F1', 'word overlap with the label (QA-style)'],
  ['regex', 'Regex', 'a pattern extracts the answer (or just must be present)'],
  ['json_field', 'JSON field equality', 'compare one field of a JSON label'],
  ['numeric', 'Numeric (tolerance)', 'numbers within a relative tolerance'],
  ['llm_judge', 'LLM judge vs reference', 'a judge model grades against the label'],
  ['llm_judge_free', 'LLM judge, no reference', 'a judge model grades with a rubric only'],
]

export function EvaluatePanel({ spec, update, models, onClose }) {
  const ev = spec.evaluate
  const set = patch => update(s => ({ ...s, evaluate: { ...s.evaluate, ...patch } }))
  const term = spec.modules.find(m => !spec.edges.some(e => e.source === m.id))
  const fields = term ? term.schema_fields.map(f => f.name) : []
  const metricName = sc => sc.name || ({ exact_match: 'accuracy', contains: 'contains', token_f1: 'f1', regex: 'regex_match', json_field: 'field_match', numeric: 'numeric_match', llm_judge: 'judge', llm_judge_free: 'judge' })[sc.type]
  const metrics = [...ev.scorers.map(metricName), ...(ev.token_count ? ['template_tokens', 'prompt_tokens', 'output_tokens'] : []), 'steps']
  return (
    <div>
      <div className="row"><h3 className="grow">Evaluate</h3><button className="small" onClick={onClose}>×</button></div>
      <div className="help">Scores the terminal step's output{term ? ` (${term.id})` : ''} on every dataset row. Metrics are a vector; the objective weighs them.</div>
      {ev.scorers.map((sc, i) => (
        <div className="card" key={i}>
          <div className="row"><select className="grow" value={sc.type} onChange={e => set({ scorers: ev.scorers.map((x, j) => j === i ? { ...x, type: e.target.value } : x) })}>{SCORERS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select>
            {ev.scorers.length > 1 && <button className="small" onClick={() => set({ scorers: ev.scorers.filter((_, j) => j !== i) })}>×</button>}</div>
          <div className="help">{SCORERS.find(s => s[0] === sc.type)[2]}</div>
          {!sc.type.startsWith('llm') && <><label>Output field to compare</label>
            <select value={sc.field || ''} onChange={e => set({ scorers: ev.scorers.map((x, j) => j === i ? { ...x, field: e.target.value || null } : x) })}><option value="">whole output</option>{fields.map(f => <option key={f}>{f}</option>)}</select></>}
          {sc.type === 'regex' && <><label>Pattern (group 1 = extracted answer)</label><input value={sc.pattern || ''} onChange={e => set({ scorers: ev.scorers.map((x, j) => j === i ? { ...x, pattern: e.target.value } : x) })} /></>}
          {sc.type === 'numeric' && <><label>Relative tolerance</label><input type="number" step="0.01" value={sc.tolerance} onChange={e => set({ scorers: ev.scorers.map((x, j) => j === i ? { ...x, tolerance: +e.target.value } : x) })} /></>}
          {sc.type.startsWith('llm') && <><label>Rubric</label><textarea style={{ minHeight: 60 }} value={sc.rubric} onChange={e => set({ scorers: ev.scorers.map((x, j) => j === i ? { ...x, rubric: e.target.value } : x) })} placeholder="Is the answer factually equivalent to the reference?" />
            <label>Judge model</label><select value={sc.judge_model || ''} onChange={e => set({ scorers: ev.scorers.map((x, j) => j === i ? { ...x, judge_model: e.target.value || null } : x) })}><option value="">same as evaluation model</option>{models.map(x => <option key={x.id} value={x.id}>{x.label}</option>)}</select></>}
          <label>Metric name</label><input value={sc.name} placeholder={metricName({ ...sc, name: '' })} onChange={e => set({ scorers: ev.scorers.map((x, j) => j === i ? { ...x, name: e.target.value } : x) })} />
          {['exact_match', 'contains', 'json_field'].includes(sc.type) && <label style={{ textTransform: 'none' }}><input type="checkbox" style={{ width: 'auto', marginRight: 6 }} checked={sc.normalize} onChange={e => set({ scorers: ev.scorers.map((x, j) => j === i ? { ...x, normalize: e.target.checked } : x) })} />normalize (case, punctuation, articles)</label>}
        </div>
      ))}
      <button className="small" onClick={() => set({ scorers: [...ev.scorers, { type: 'token_f1', name: '', field: fields[0] || null, normalize: true, pattern: null, tolerance: 0, rubric: '', judge_model: null }] })}>+ scorer</button>
      <label style={{ textTransform: 'none', marginTop: 12 }}><input type="checkbox" style={{ width: 'auto', marginRight: 6 }} checked={ev.token_count} onChange={e => set({ token_count: e.target.checked })} />also record tokens: template (the prompts themselves, summed over steps), prompt (rendered, with the data) and output</label>
      <label>Objective: weight per metric</label>
      {metrics.map(k => <div className="row" key={k} style={{ marginBottom: 4 }}><code style={{ width: 150 }}>{k}</code><input type="number" step="0.001" style={{ width: 110 }} value={ev.objective[k] ?? ''} placeholder="0" onChange={e => { const o = { ...ev.objective }; if (e.target.value === '') delete o[k]; else o[k] = +e.target.value; set({ objective: o }) }} /></div>)}
      <div className="help">Score = Σ weight × metric. A negative weight is an exchange rate: <code>template_tokens</code> −0.002 says 100 tokens are worth 0.2 of accuracy, and the search will make that trade if the rewriter offers it; −0.0005 says 100 tokens are worth 5 points. Pair it with the Optimizer's goal set to Compress (a penalty alone only rejects rewrites; the goal changes what the rewriter is asked for) and read the front on the run page, not just the best score.</div>
    </div>
  )
}

export function OptimizerPanel({ spec, update, models, tier, onClose }) {
  const o = spec.optimizer
  const set = patch => update(s => ({ ...s, optimizer: { ...s.optimizer, ...patch } }))
  const maxQ = (tier && tier.max_q) || 4
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="row"><h2 className="grow">Optimizer</h2><button className="small" onClick={onClose}>×</button></div>
        <div className="help">The loop is fixed: pick a prompt from the current best set → show a reflection model a few of its failures → rewrite one step → the rewrite earns a full evaluation only if it beats its parent on the same small batch. These are its knobs.{tier && <> Tier <b>{tier.tier}</b>: up to {tier.max_concurrency} calls in flight, {tier.max_q} parents per round.</>}</div>
        <div className="grid2">
          <div><label>Goal</label>
            <select value={o.goal || 'accuracy'} onChange={e => { const goal = e.target.value; update(s => ({ ...s, optimizer: { ...s.optimizer, goal },
              evaluate: goal === 'compress' && !(s.evaluate.objective.template_tokens < 0) ? { ...s.evaluate, token_count: true, objective: { ...s.evaluate.objective, template_tokens: -0.002 } } : s.evaluate })) }}>
              <option value="accuracy">Improve accuracy — the rewriter adds rules that fix the failures it is shown</option><option value="compress">Compress — the rewriter is asked for a shorter prompt that keeps the same accuracy</option></select>
            <div className="help">{(o.goal || 'accuracy') === 'compress' ? 'Reflection keeps whatever rule prevents the shown failures and cuts the rest; correct rows are shown as successes. Needs a negative template_tokens weight on the Evaluate block (set to −0.002 when you picked this; adjust there).' : 'The GEPA loop: each rewrite fixes the failures the reflection model saw.'}</div></div>
          <div><label>Which prompt to rewrite next</label>
            <select value={o.engine} onChange={e => set({ engine: e.target.value })}><option value="gepa">Explore — sample from the best set</option><option value="bo">Guided — a model of past results picks</option></select>
            <div className="help">{o.engine === 'gepa' ? 'Cheap and robust; the default. (GEPA-style Pareto sampling.)' : 'Spends more reflection calls; wins under noisy scores or a weak reflection model, no better on flat tasks. (Bayesian optimization, q-EI over prompt embeddings; see Findings.)'}</div></div>
          {o.engine === 'gepa' ? <div><label>How to sample it</label>
            <select value={o.mode} onChange={e => set({ mode: e.target.value })}><option value="weighted">favour prompts that win more examples</option><option value="uniform">any of the best set, equally</option><option value="best">always the top score</option></select></div>
            : <div><label>Prompts rewritten per round <span className="muted">— your tier allows up to {maxQ}</span></label><select value={Math.min(o.bo.q, maxQ)} onChange={e => set({ bo: { ...o.bo, q: +e.target.value } })}>{[1, 2, 3, 4, 6, 8, 12, 16].filter(q => q <= maxQ).map(q => <option key={q} value={q}>{q}{q === 1 ? ' — one at a time (weakest)' : q === 2 ? ' — recommended' : ''}</option>)}</select></div>}
        </div>
        {o.engine === 'bo' && <div className="grid2">
          <div><label>How the guide scores candidates</label><select value={o.bo.acquisition} onChange={e => set({ bo: { ...o.bo, acquisition: e.target.value } })}>
            <option value="qei">expected improvement, sampled (default)</option><option value="kriging">expected improvement, sequential</option><option value="quantecarlo">expected improvement, hosted service (different criterion)</option></select></div>
          <div><label>Embedding dimensions</label><input type="number" min={2} max={16} value={o.bo.pca} onChange={e => set({ bo: { ...o.bo, pca: +e.target.value } })} /></div>
        </div>}
        <div className="grid3">
          <div><label>Parents per round <span className="muted">— max {maxQ}</span></label><input type="number" min={1} max={maxQ} value={o.engine === 'bo' ? Math.min(o.bo.q, maxQ) : Math.min(o.parents_per_round, maxQ)} disabled={o.engine === 'bo'} onChange={e => set({ parents_per_round: Math.min(maxQ, +e.target.value) })} /></div>
          <div><label>Children per parent</label><input type="number" min={1} max={6} value={o.children} onChange={e => set({ children: +e.target.value })} /></div>
          <div><label>Check batch (rows)</label><input type="number" min={3} max={32} value={o.minibatch} onChange={e => set({ minibatch: Math.max(3, +e.target.value) })} /><div className="help">Rows the reflection model sees, and the rows a rewrite must beat its parent on before it gets a full evaluation. Floor 3.</div></div>
        </div>
        <h3 style={{ fontSize: 14, margin: '14px 0 0' }}>Reflection</h3>
        <div className="grid2">
          <div><label>Reflection model</label><select value={o.reflect_model} onChange={e => set({ reflect_model: e.target.value })}>{!models.some(m => m.id === o.reflect_model) && <option value={o.reflect_model}>{o.reflect_model} (not available on your plan)</option>}{models.map(m => <option key={m.id} value={m.id}>{m.label}</option>)}</select></div>
          <div><label>Reflection temperature</label><input type="number" step="0.1" min={0} max={1.5} value={o.reflect_temperature} onChange={e => set({ reflect_temperature: +e.target.value })} /></div>
          <div><label>What the reflection model is told about each failure</label><select value={o.feedback} onChange={e => set({ feedback: e.target.value })}>
            <option value="critic">a short written critique (a model reads the trace)</option><option value="templated">a template you write</option><option value="plain">expected answer and scores only</option></select></div>
          {o.feedback === 'critic' && <div><label>Critic model</label><select value={o.critic_model || ''} onChange={e => set({ critic_model: e.target.value || null })}><option value="">same as reflection model</option>{models.map(m => <option key={m.id} value={m.id}>{m.label}</option>)}</select></div>}
        </div>
        {o.feedback === 'templated' && <><label>Template — {'{expected} {predicted} {metrics} {inputs} {output}'}</label><input value={o.feedback_template} onChange={e => set({ feedback_template: e.target.value })} /></>}
        <h3 style={{ fontSize: 14, margin: '14px 0 0' }}>Budget and stopping</h3>
        <div className="grid3">
          <div><label>Rounds</label><input type="number" min={1} max={200} value={o.rounds} onChange={e => set({ rounds: +e.target.value })} /></div>
          <div><label>Stop after N rounds without improvement</label><input type="number" min={0} value={o.no_improvement_rounds ?? ''} onChange={e => set({ no_improvement_rounds: e.target.value === '' ? null : +e.target.value })} /></div>
          <div><label>Max spend (USD)</label><input type="number" step="0.5" min={0} value={o.max_usd ?? ''} onChange={e => set({ max_usd: e.target.value === '' ? null : +e.target.value })} /></div>
        </div>
        <h3 style={{ fontSize: 14, margin: '14px 0 0' }}>Evaluation set</h3>
        <div className="grid3">
          <div><label>Hold out (fraction)</label><input type="number" step="0.05" min={0} max={0.5} value={o.holdout_frac} onChange={e => set({ holdout_frac: +e.target.value })} /><div className="help">Reported at the end; never seen by the search.</div></div>
          <div><label>Downsample the eval set to N rows</label><input type="number" min={10} value={o.eval_rows ?? ''} placeholder="all" onChange={e => set({ eval_rows: e.target.value === '' ? null : +e.target.value })} /><div className="help">Cheaper rounds. Downsample here, never the minibatch.</div></div>
          <div><label>Seed</label><input type="number" value={o.seed} onChange={e => set({ seed: +e.target.value })} /></div>
        </div>
        <div className="row" style={{ marginTop: 16 }}><button className="primary" onClick={onClose}>Done</button></div>
      </div>
    </div>
  )
}
