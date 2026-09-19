import React, { useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import { placeholders } from './Canvas.jsx'

export default function DataPanel({ pid, spec, update, datasets, reload, mock }) {
  const [did, setDid] = useState(datasets[0] && datasets[0].id)
  const [ds, setDs] = useState(null)
  const [map, setMap] = useState({})
  const [label, setLabel] = useState(null)
  const [report, setReport] = useState(null)
  const [pilot, setPilot] = useState(null)
  const [cost, setCost] = useState(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [rows, setRows] = useState(12)
  useEffect(() => { if (!did && datasets[0]) setDid(datasets[0].id) }, [datasets])
  useEffect(() => {
    if (!did) { setDs(null); return }
    api.get(`/datasets/${did}`).then(d => { setDs(d); setMap(d.input_map || {}); setLabel(d.label_column); setReport(null); setPilot(null); setCost(null) })
  }, [did])
  const allPh = useMemo(() => {
    // forward walk from the entry: an edge into an already-visited step is a loop-back. Placeholders fed by a forward
    // edge need no column; ones fed only by a loop-back need a first-visit column (seeded).
    const entry = spec.entry || (spec.modules[0] && spec.modules[0].id)
    const forward = new Set(), back = new Set(), seen = new Set(), todo = entry ? [entry] : []
    while (todo.length) {
      const x = todo.shift(); if (seen.has(x)) continue; seen.add(x)
      spec.edges.filter(e => e.source === x).forEach(e => { (seen.has(e.target) ? back : forward); Object.keys(e.mapping).forEach(p => (seen.has(e.target) ? back : forward).add(p)); if (!seen.has(e.target)) todo.push(e.target) })
    }
    const out = []
    spec.modules.forEach(m => placeholders(m.template).forEach(p => {
      if (forward.has(p) || out.find(x => x.ph === p)) return
      out.push({ ph: p, seeded: back.has(p) })
    }))
    return out
  }, [spec])
  // auto-map placeholders to same-named columns
  useEffect(() => { if (ds) setMap(m => { const n = { ...m }; allPh.forEach(({ ph }) => { if (!n[ph] && ds.columns.includes(ph)) n[ph] = ph }); return n }) }, [ds, allPh])

  const upload = async e => {
    const f = e.target.files[0]; if (!f) return
    setBusy('uploading'); setErr('')
    try { const r = await api.upload(`/projects/${pid}/datasets`, f); await reload(); setDid(r.id) } catch (ex) { setErr(ex.message) }
    setBusy('')
  }
  const saveMapping = async () => { await api.put(`/datasets/${did}/mapping`, { input_map: map, label_column: label }); update(s => ({ ...s, evaluate: { ...s.evaluate, label_column: label } })) }
  const validate = async () => {
    setBusy('validating'); setErr(''); setPilot(null)
    try { await saveMapping(); const r = await api.post(`/projects/${pid}/validate`, { dataset_id: did }); setReport(r.report) } catch (ex) { setErr(ex.message) }
    setBusy('')
  }
  const runPilot = async () => {
    setBusy('pilot'); setErr('')
    try { await saveMapping(); const r = await api.post(`/projects/${pid}/pilot`, { dataset_id: did, rows, mock: mock || undefined }); setReport(r.report); setPilot(r.pilot); setCost(r.cost) } catch (ex) { setErr(ex.message) }
    setBusy('')
  }
  const errors = report ? report.issues.filter(i => i.level === 'error').length : null
  const pilotCalls = rows * 3 * (spec.edges.length ? spec.max_steps : 1)
  return (
    <div className="page">
      <div className="grid2">
        <div className="card">
          <h3>Dataset</h3>
          <div className="row"><select className="grow" value={did || ''} onChange={e => setDid(e.target.value)}><option value="">— choose —</option>{datasets.map(d => <option key={d.id} value={d.id}>{d.name} ({d.n_rows} rows)</option>)}</select>
            <label className="row" style={{ margin: 0 }}><input type="file" accept=".jsonl,.json,.csv,.tsv" onChange={upload} style={{ width: 'auto' }} /></label></div>
          <div className="help">JSONL, JSON, CSV or TSV. One row per example: input columns plus a label column. bpto-style rows ({'{"inputs": {...}, "answer": ...}'}) are flattened.
            {' '}No data yet? <a href="/studio/api/sample/tickets.jsonl" download>Download the sample</a> (50 support tickets, columns <code>message</code>, <code>queue</code>) and upload it, or <a href="#" onClick={async e => { e.preventDefault(); setBusy('sample'); const r = await api.post(`/projects/${pid}/datasets/sample`); await reload(); setDid(r.id); setBusy('') }}>load it directly</a>.</div>
          {ds && <>
            <label>Map placeholders to columns</label>
            {allPh.length === 0 && <div className="muted">No placeholders in any prompt yet.</div>}
            {allPh.map(({ ph, seeded }) => (
              <div className="row" key={ph} style={{ marginBottom: 4 }}><code style={{ width: 130 }}>{'{' + ph + '}'}</code>
                <select className="grow" value={map[ph] || ''} onChange={e => setMap({ ...map, [ph]: e.target.value })}><option value="">{seeded ? '— written by an edge (needs a first-visit column)' : '— not mapped'}</option>{ds.columns.map(c => <option key={c}>{c}</option>)}</select></div>
            ))}
            <label>Label column</label>
            <select value={label || ''} onChange={e => setLabel(e.target.value || null)}><option value="">none (reference-free judge only)</option>{ds.columns.map(c => <option key={c}>{c}</option>)}</select>
            <div className="row" style={{ marginTop: 12 }}>
              <button className="primary" disabled={!!busy} onClick={validate}>{busy === 'validating' ? 'checking…' : 'Check data'}</button>
              <button disabled={!!busy || errors !== 0} onClick={runPilot} title={errors !== 0 ? 'fix the errors first' : ''}>{busy === 'pilot' ? 'running pilot…' : `Run pilot (${rows} rows${mock ? ', mock' : `, ≤${pilotCalls} calls`})`}</button>
              <input type="number" min={4} max={50} value={rows} onChange={e => setRows(+e.target.value)} style={{ width: 70 }} />
            </div>
            {err && <div className="err">{err}</div>}
          </>}
        </div>
        <div className="card">
          <h3>Preview</h3>
          {ds ? <div style={{ overflow: 'auto', maxHeight: 300 }}><table><thead><tr>{ds.columns.map(c => <th key={c}>{c}</th>)}</tr></thead>
            <tbody>{ds.preview.slice(0, 8).map((r, i) => <tr key={i}>{ds.columns.map(c => <td key={c} className="mono" style={{ maxWidth: 220, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{String(r[c] ?? '')}</td>)}</tr>)}</tbody></table></div>
            : <div className="muted">Upload a file to see it here.</div>}
        </div>
      </div>
      {report && <Report report={report} />}
      {pilot && <Pilot pilot={pilot} cost={cost} spec={spec} />}
    </div>
  )
}

export function Report({ report }) {
  const n = l => report.issues.filter(i => i.level === l).length
  return (
    <div className="card">
      <h3>Validation {n('error') ? <span className="err">— {n('error')} error{n('error') > 1 ? 's' : ''}, Run disabled</span> : <span className="ok">— no errors</span>}</h3>
      {report.summary && report.summary.label_kind && <div className="help">{report.summary.rows} rows · labels look like <b>{report.summary.label_kind}</b>{report.summary.label_histogram && <> · {Object.entries(report.summary.label_histogram).slice(0, 8).map(([k, v]) => `${k}: ${v}`).join(', ')}</>}</div>}
      {report.issues.length === 0 && <div className="muted">Nothing to report.</div>}
      {['error', 'warn', 'info'].flatMap(l => report.issues.filter(i => i.level === l)).map((i, k) => (
        <div className={'issue ' + i.level} key={k}><span className="lvl">{i.level}</span><div><div>{i.message}</div>{i.where && <div className="where">{i.stage} · {i.where}</div>}</div></div>
      ))}
    </div>
  )
}

function Pilot({ pilot, cost, spec }) {
  const key = Object.keys(spec.evaluate.objective)[0]
  return (
    <div className="card">
      <h3>Pilot ({pilot.rows} rows, {pilot.calls} calls)</h3>
      <div className="grid3">
        <div className="stat"><div className="num">{pilot.baseline.toFixed(3)}</div><div className="lbl">baseline objective (root prompt)</div></div>
        <div className="stat"><div className="num alt">±{pilot.nondeterminism_band.toFixed(3)}</div><div className="lbl">non-determinism band — {pilot.rows_changed_on_rerun} of {pilot.rows} rows changed when re-run at temperature 0, cache bypassed</div></div>
        {pilot.rewrite && <div className="stat"><div className="num" style={{ color: Math.abs(pilot.rewrite.delta) <= pilot.nondeterminism_band ? 'var(--muted)' : 'var(--accent)' }}>{pilot.rewrite.delta >= 0 ? '+' : ''}{pilot.rewrite.delta.toFixed(3)}</div><div className="lbl">one random rewrite of the root</div></div>}
      </div>
      {pilot.avg_steps && <div className="help">avg {pilot.avg_steps.toFixed(1)} steps per example · tokens per module: {Object.entries(pilot.tokens_per_module).map(([k, v]) => `${k} ${Math.round(v)}`).join(', ')}</div>}
      <div className="help">Sampling error (SE) per metric: {Object.entries(pilot.sampling_se).filter(([k]) => !k.startsWith('tokens')).map(([k, v]) => `${k} ${v.toFixed(3)}`).join(' · ')}</div>
      <table style={{ marginTop: 8 }}><thead><tr><th>id</th><th>path</th><th>output</th><th>label</th><th>{key}</th></tr></thead>
        <tbody>{pilot.examples.map(e => <tr key={e.id}><td className="mono">{e.id}</td><td className="mono muted">{e.path.join(' → ')}</td>
          <td className="mono" style={{ maxWidth: 380 }}>{e.error ? <span className="err">{e.error}</span> : (e.parsed ? JSON.stringify(e.parsed) : e.output)}</td>
          <td className="mono">{String(e.answer ?? '')}</td><td>{e.metrics && e.metrics[key] !== undefined ? e.metrics[key].toFixed(2) : '—'}</td></tr>)}</tbody></table>
      {cost && <Cost cost={cost} />}
    </div>
  )
}

export function Cost({ cost }) {
  const c = cost.calls, w = cost.calls_worst_case, u = cost.usd
  const tot = Object.values(c).reduce((a, b) => a + b, 0), wtot = Object.values(w).reduce((a, b) => a + b, 0)
  return (
    <div style={{ marginTop: 12 }}>
      <h3 style={{ fontSize: 14 }}>Projected cost for {cost.assumptions.rounds} rounds</h3>
      <div className="kv">
        <b>eval calls</b><span>{Math.round(c.round0 + c.gate + c.full)} (root {Math.round(c.round0)}, gate {Math.round(c.gate)}, full {Math.round(c.full)}){wtot !== tot && <span className="muted"> · worst case with max_steps: {Math.round(w.round0 + w.gate + w.full)}</span>}</span>
        <b>reflection calls</b><span>{Math.round(c.reflect)}{c.critic ? ` + ${Math.round(c.critic)} critic` : ''}</span>
        <b>USD</b><span><b>${u.total.toFixed(2)}</b> (eval ${u.eval.toFixed(2)}, reflection ${u.reflect.toFixed(2)}{u.critic ? `, critic $${u.critic.toFixed(2)}` : ''})</span>
        <b>assumes</b><span className="muted">{cost.assumptions.full_rows} eval rows, {cost.assumptions.avg_steps.toFixed(1)} steps/example, {Math.round(cost.assumptions.acceptance * 100)}% of children accepted{cost.assumptions.unpriced_models.length ? ` · no price known for ${cost.assumptions.unpriced_models.join(', ')}` : ''}</span>
      </div>
    </div>
  )
}
