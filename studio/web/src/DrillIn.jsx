import React, { useEffect, useState } from 'react'
import { api } from './api.js'

// One hosted version up close (DASHBOARD.md step 3): its traces, a review queue whose random sample is the only
// estimate of live accuracy, and its project's version history. Reviews are outcomes that record how the trace was
// chosen, so later steps can tell an estimate from a pile of hand-picked failures.
const usd = x => x == null ? '—' : x < 0.01 ? '$' + (x === 0 ? '0' : x.toPrecision(2)) : '$' + x.toFixed(2)
const brief = t => { const p = t.parsed; if (p && typeof p === 'object' && Object.keys(p).length === 1) return String(Object.values(p)[0]); return t.output || '' }
const firstInput = t => { const v = Object.values(t.inputs || {})[0]; return v == null ? '' : String(v) }
const verdict = t => {
  const r = [...(t.outcomes || [])].reverse().find(o => o.kind === 'review' || o.kind === 'correction')
  if (!r) return null
  return r.kind === 'review' && r.value === 1 ? 'right' : 'wrong'
}

export default function DrillIn({ row, onOpenProject, onReviewed }) {
  return <div style={{ marginTop: 16, border: '1px solid var(--line)', borderRadius: 14, background: 'var(--bg2)', padding: 16 }}>
    <div className="row" style={{ marginBottom: 12, alignItems: 'flex-start' }}>
      <div className="grow"><div className="muted" style={{ fontSize: 11.5, textTransform: 'uppercase', letterSpacing: '.5px' }}>Hosted version</div>
        <h3 style={{ margin: 0 }}>{row.project} · {row.label}</h3>
        <div className="mono muted" style={{ fontSize: 12 }}>{row.url}</div></div>
      <button className="small" onClick={() => onOpenProject(row.project_id)}>Open project</button>
    </div>
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: 14 }}>
      <Traces vid={row.version_id} />
      <Review vid={row.version_id} onReviewed={onReviewed} />
      <History pid={row.project_id} />
    </div>
  </div>
}

function Traces({ vid }) {
  const [filter, setFilter] = useState('all')
  const [ts, setTs] = useState(null)
  const [open, setOpen] = useState(null)
  const [detail, setDetail] = useState(null)
  useEffect(() => { setTs(null); api.get(`/versions/${vid}/traces?filter=${filter}&limit=50`).then(setTs).catch(() => setTs([])) }, [vid, filter])
  useEffect(() => { setDetail(null); if (open) api.get(`/traces/${open}`).then(setDetail).catch(() => {}) }, [open])
  return <div className="card" style={{ margin: 0 }}>
    <h4 style={{ margin: '0 0 8px' }}>Traces</h4>
    <div className="row" style={{ gap: 6, marginBottom: 8 }}>{['all', 'errors', 'reviewed', 'unreviewed'].map(f =>
      <button key={f} className="small" style={{ borderRadius: 999, borderColor: filter === f ? 'var(--accent)' : undefined, color: filter === f ? 'var(--text)' : 'var(--muted)' }} onClick={() => setFilter(f)}>{f}</button>)}</div>
    {ts == null && <div className="muted">Loading…</div>}
    {ts && ts.length === 0 && <div className="muted">No requests match.</div>}
    {ts && ts.map(t => <div key={t.id}>
      <div onClick={() => setOpen(open === t.id ? null : t.id)} style={{ display: 'grid', gridTemplateColumns: '48px minmax(0,1fr) 90px 52px 54px', gap: 8, alignItems: 'center', padding: '6px 4px', borderBottom: '1px solid var(--line)', fontSize: 12.5, cursor: 'pointer', background: open === t.id ? 'rgba(94,227,200,.06)' : undefined }}>
        <span className="mono muted">{new Date(t.created * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{firstInput(t)}</span>
        <span className="mono" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.error ? '—' : brief(t)}</span>
        <span className="mono muted">{t.latency_s == null ? '' : t.latency_s.toFixed(1) + ' s'}</span>
        <span>{t.error ? <span style={{ color: 'var(--danger)' }}>error</span> : t.capped ? <span style={{ color: 'var(--accent2)' }}>capped</span>
          : verdict(t) === 'right' ? <span style={{ color: 'var(--ok)' }}>right</span> : verdict(t) === 'wrong' ? <span style={{ color: 'var(--danger)' }}>wrong</span> : <span className="muted">·</span>}</span>
      </div>
      {open === t.id && detail && <div style={{ margin: '6px 0 10px', padding: 10, border: '1px solid var(--line)', borderRadius: 8, background: '#0a0f1e', fontSize: 12.5 }}>
        <div className="muted">Inputs</div><pre>{JSON.stringify(detail.inputs, null, 1)}</pre>
        {detail.steps && Object.keys(detail.steps).length > 0 && <><div className="muted" style={{ marginTop: 6 }}>What the steps returned</div><pre>{JSON.stringify(detail.steps, null, 1)}</pre></>}
        <div className="muted" style={{ marginTop: 6 }}>Path</div><pre>{(detail.path || []).join(' → ')}</pre>
        <div className="muted" style={{ marginTop: 6 }}>{detail.error ? 'Error' : 'Output'}</div><pre>{detail.error || detail.output}</pre>
        <div className="muted mono" style={{ marginTop: 6 }}>{detail.latency_s != null ? detail.latency_s.toFixed(2) + ' s · ' : ''}{usd(detail.usd)} · {detail.input_tokens ?? '?'} in / {detail.output_tokens ?? '?'} out tokens · trace {detail.id}</div>
        {detail.outcomes.length > 0 && <div style={{ marginTop: 6 }}>{detail.outcomes.map(o => <div key={o.id} className="muted">{o.kind}{o.value != null ? ` (${o.value === 1 ? 'right' : o.value === 0 ? 'wrong' : o.value})` : ''}{o.label ? `: ${o.label}` : ''} · {o.chosen || o.source} · {new Date(o.created * 1000).toLocaleString()}</div>)}</div>}
      </div>}
    </div>)}
  </div>
}

function Review({ vid, onReviewed }) {
  const [mode, setMode] = useState('random')
  const [queue, setQueue] = useState(null)
  const [i, setI] = useState(0)
  const [fix, setFix] = useState('')
  const [done, setDone] = useState({ right: 0, wrong: 0 })
  const [err, setErr] = useState('')
  const load = m => { setQueue(null); setI(0); setErr(''); api.get(`/versions/${vid}/review-queue?mode=${m}&n=10`).then(setQueue).catch(e => setErr(e.message)) }
  useEffect(() => { load(mode) }, [vid, mode])
  const t = queue && queue[i]
  const send = async (v, label) => {
    setErr('')
    try {
      await api.post(`/traces/${t.id}/review`, { verdict: v, label: label || null, chosen: mode })
      setDone(d => ({ ...d, [v]: d[v] + 1 })); setFix(''); setI(x => x + 1); onReviewed && onReviewed()
    } catch (e) { setErr(e.message) }
  }
  return <div className="card" style={{ margin: 0 }}>
    <div className="row"><h4 style={{ margin: 0 }} className="grow">Review traces</h4>
      <select value={mode} onChange={e => setMode(e.target.value)} style={{ width: 'auto' }} aria-label="Which traces">
        <option value="random">random sample</option><option value="suspicious">suspicious first</option></select></div>
    <div className="help">{mode === 'random'
      ? 'A random sample of the last 30 days. Only these reviews count toward the accuracy estimate on the dashboard.'
      : 'Errors and loops that hit the step cap first. Useful for finding problems; these reviews add labels but do not count toward the accuracy estimate.'}</div>
    {err && <div className="err">{err}</div>}
    {queue == null && !err && <div className="muted">Loading…</div>}
    {queue && queue.length === 0 && <div className="muted">Nothing left to review in the last 30 days.</div>}
    {t && <div style={{ border: '1px solid var(--line)', borderRadius: 10, padding: 12, background: '#0a0f1e', marginTop: 8 }}>
      <div className="muted" style={{ fontSize: 11.5 }}>{i + 1} of {queue.length} · {new Date(t.created * 1000).toLocaleString()}</div>
      <pre style={{ margin: '6px 0', background: 'none', border: 'none', padding: 0 }}>{JSON.stringify(t.inputs, null, 1)}</pre>
      <div className="mono" style={{ color: t.error ? 'var(--danger)' : 'var(--accent)' }}>{t.error ? `error: ${t.error}` : `answer: ${t.answer ?? t.output}`}</div>
      <div className="row" style={{ marginTop: 10, gap: 8 }}>
        {!t.error && <button className="small" onClick={() => send('right')}>Right</button>}
        <button className="small" onClick={() => send('wrong')}>Wrong</button>
        <input value={fix} onChange={e => setFix(e.target.value)} placeholder="the right answer" style={{ width: 150 }} aria-label="The right answer"
          onKeyDown={e => { if (e.key === 'Enter' && fix.trim()) send('wrong', fix.trim()) }} />
        <button className="small" disabled={!fix.trim()} onClick={() => send('wrong', fix.trim())}>Save correction</button>
      </div>
    </div>}
    {queue && i >= queue.length && queue.length > 0 && <div className="row" style={{ marginTop: 8 }}><span className="muted">Done with this batch.</span><button className="small" onClick={() => load(mode)}>Next 10</button></div>}
    <div className="help" style={{ marginTop: 8 }}>This session: {done.right} right, {done.wrong} wrong. A correction also becomes a labelled row for the next optimization.</div>
  </div>
}

function History({ pid }) {
  const [vs, setVs] = useState(null)
  useEffect(() => { api.get(`/projects/${pid}/versions`).then(setVs).catch(() => setVs([])) }, [pid])
  return <div className="card" style={{ margin: 0 }}>
    <h4 style={{ margin: '0 0 8px' }}>Version history</h4>
    {vs == null && <div className="muted">Loading…</div>}
    {vs && vs.map(v => <div key={v.id} className="row" style={{ fontSize: 13, padding: '3px 0' }}>
      <span style={{ color: v.hosted ? 'var(--ok)' : 'var(--muted)', border: '1px solid currentColor', borderRadius: 999, padding: '0 8px', fontSize: 11.5 }}>{v.hosted ? 'hosted' : 'not hosted'}</span>
      <b>{v.label}</b>
      <span className="muted">{v.score == null ? 'not scored' : `scored ${v.score.toFixed(2)}`}{v.holdout ? ` · hold-out ${v.holdout.best_score.toFixed(2)}` : ''} · {new Date(v.created * 1000).toLocaleDateString()}</span>
    </div>)}
    <div className="help">Read-only here. Host or unhost from the project's Serving tab.</div>
  </div>
}
