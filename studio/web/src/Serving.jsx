import React, { useEffect, useState } from 'react'
import { api } from './api.js'

// The Serving tab: what is published, what production did with it, and what came back. One page for PLAN.md's
// second half - publish a version, call it, collect outcomes, turn those into the next dataset.
export default function Serving({ pid, reload }) {
  const [n, setN] = useState(0)
  return (
    <div className="page">
      <Versions pid={pid} reloadKey={n} onRestore={reload} />
      <Traces pid={pid} onPromoted={() => { setN(x => x + 1); reload && reload() }} />
    </div>
  )
}

// A version is a frozen copy of the program: the prompts that earned the score, with every model id spelled out.
// Publishing one is how a run's result stops being a row in a tree and becomes something that can be served.
function Versions({ pid, reloadKey, onRestore }) {
  const [vs, setVs] = useState([])
  const [open, setOpen] = useState(null)
  useEffect(() => { api.get(`/projects/${pid}/versions`).then(setVs).catch(() => {}) }, [pid, reloadKey])
  return (
    <div className="card"><h3>Versions</h3>
      {vs.length === 0 && <div className="muted">None yet. Open a finished run and publish its best prompt as a version.</div>}
      {vs.length > 0 && <table><thead><tr><th>label</th><th>from</th><th>score</th><th>rows</th><th>hold-out</th><th>published</th><th>fingerprint</th><th></th></tr></thead><tbody>
        {vs.map(v => <tr key={v.id} style={{ cursor: 'pointer' }} onClick={() => setOpen(open === v.id ? null : v.id)}>
          <td><b>{v.label}</b></td>
          <td className="muted">{v.source.kind === 'canvas' ? 'the canvas' : `run ${v.source.run_id.slice(0, 6)} · node ${(v.source.node_id || '').slice(0, 6)}`}</td>
          <td>{v.score == null ? '—' : v.score.toFixed(3)}</td><td className="muted">{v.n_rows ?? '—'}</td>
          <td>{v.holdout ? v.holdout.best_score.toFixed(3) : '—'}</td>
          <td className="muted">{new Date(v.created * 1000).toLocaleString()}</td>
          <td className="mono muted" title="sha256 of what the version runs: same fingerprint, same behaviour">{v.fingerprint.slice(0, 8)}</td>
          <td><button className="small" title="Put these prompts back on the canvas, so the next run starts from what is deployed"
            onClick={e => { e.stopPropagation(); if (window.confirm(`Replace the canvas with ${v.label}? The version itself is unchanged.`)) api.post(`/projects/${pid}/versions/${v.id}/restore`).then(() => onRestore && onRestore()) }}>Load onto canvas</button></td>
        </tr>)}
      </tbody></table>}
      {open && <VersionDetail vid={open} />}
      <div className="help">A version never changes: editing the canvas afterwards does not touch one already published. Load one back
        onto the canvas when you want the next run to start from what is deployed rather than from whatever you last edited.</div>
    </div>
  )
}

function VersionDetail({ vid }) {
  const [v, setV] = useState(null)
  const [c, setC] = useState(null)
  useEffect(() => { setV(null); setC(null); api.get(`/versions/${vid}`).then(setV); api.get(`/v/${vid}`).then(setC).catch(() => {}) }, [vid])
  if (!v) return null
  const curl = c && ['curl -X POST ' + c.url, "  -H 'Authorization: Bearer <your api key>'", "  -H 'Content-Type: application/json'",
    '  -d \'{"inputs": {' + c.inputs.map(i => `"${i}": "…"`).join(', ') + '}}\''].join(' \\\n')
  return <div style={{ marginTop: 10, borderTop: '1px solid var(--line)', paddingTop: 10 }}>
    <div className="help">{v.label} · {v.spec.eval_model}{v.dataset_id ? ` · scored on dataset ${v.dataset_id.slice(0, 6)}` : ''}</div>
    {v.spec.modules.map(m => <div key={m.id} style={{ marginBottom: 8 }}><div className="row"><b>{m.id}</b><span className="muted" style={{ fontSize: 12 }}>{m.model}</span></div><pre>{m.template}</pre></div>)}
    {c && <>
      <div className="help">Serve it. Takes <code>{c.inputs.join(', ') || 'no inputs'}</code>, returns <code>{c.outputs.join(', ') || 'text'}</code>
        {' '}and a trace id. Make a key under Models &amp; keys.</div>
      <pre className="mono" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{curl}</pre>
    </>}
  </div>
}


// A trace is one served request. An outcome is what happened next - the correction an agent made, a rating, a
// reopen. A trace with a correction is a labelled row, and promoting a set of them makes an ordinary dataset.
function Traces({ pid, onPromoted }) {
  const [ts, setTs] = useState([])
  const [edit, setEdit] = useState({})
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const load = () => api.get(`/projects/${pid}/traces`).then(setTs).catch(() => {})
  useEffect(() => { load() }, [pid])
  const correct = async (t) => {
    const label = (edit[t.id] || '').trim()
    if (!label) return
    setErr('')
    try { await api.post(`/traces/${t.id}/outcome`, { kind: 'correction', label, source: 'studio' }); setEdit(e => ({ ...e, [t.id]: '' })); load() }
    catch (e) { setErr(e.message) }
  }
  const promote = async () => {
    setErr(''); setMsg('')
    try { const d = await api.post(`/projects/${pid}/datasets/from-traces`, {}); setMsg(`${d.name}: ${d.n_rows} rows, labelled by ${d.label_column}. It is on the Data tab.`); onPromoted && onPromoted() }
    catch (e) { setErr(e.message) }
  }
  const corrected = ts.filter(t => t.outcomes.some(o => o.label)).length
  return (
    <div className="card"><div className="row"><h3 style={{ margin: 0 }}>Traces</h3><div className="grow" />
      <span className="muted" style={{ fontSize: 12 }}>{ts.length} requests · {corrected} corrected</span>
      <button className="small primary" disabled={!corrected} onClick={promote}>Promote corrections to a dataset</button></div>
      {err && <div className="err">{err}</div>}
      {msg && <div className="ok" style={{ fontSize: 12.5, marginTop: 6 }}>{msg}</div>}
      {ts.length === 0 && <div className="muted">Nothing served yet. Publish a version, make an API key, and POST to its url.</div>}
      {ts.length > 0 && <table><thead><tr><th>when</th><th>inputs</th><th>answer</th><th>outcome</th><th>correction</th></tr></thead><tbody>
        {ts.map(t => {
          const last = t.outcomes.filter(o => o.label).slice(-1)[0]
          return <tr key={t.id}>
            <td className="muted" style={{ whiteSpace: 'nowrap' }}>{new Date(t.created * 1000).toLocaleTimeString()}</td>
            <td className="mono" style={{ maxWidth: 260, overflowWrap: 'anywhere', fontSize: 12 }}>{Object.entries(t.inputs).map(([k, val]) => `${k}=${val}`).join(' · ').slice(0, 160)}</td>
            <td className="mono" style={{ maxWidth: 220, overflowWrap: 'anywhere' }}>{t.error ? <span className="err">{t.error}</span> : (t.parsed ? JSON.stringify(t.parsed) : (t.output || '').slice(0, 120))}</td>
            <td>{last ? <span className="pill">{last.source}: {last.label}</span> : <span className="muted">—</span>}</td>
            <td><div className="row"><input value={edit[t.id] || ''} placeholder="the right answer" onChange={e => setEdit(x => ({ ...x, [t.id]: e.target.value }))}
              onKeyDown={e => e.key === 'Enter' && correct(t)} style={{ width: 150 }} /><button className="small" onClick={() => correct(t)}>Save</button></div></td>
          </tr>
        })}
      </tbody></table>}
      <div className="help">A correction is the label. Promoting builds a normal dataset - inputs, the right answer, and the
        trace it came from - which you then run like any other. Correcting the same trace twice keeps the last answer.</div>
    </div>
  )
}
