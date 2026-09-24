import React, { useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import { Cost } from './DataPanel.jsx'

export default function RunView({ pid, spec, datasets, mock, features = {} }) {
  const [runs, setRuns] = useState([])
  const [rid, setRid] = useState(null)
  const [did, setDid] = useState(datasets[0] && datasets[0].id)
  const [cost, setCost] = useState(null)
  const [err, setErr] = useState('')
  const refresh = () => api.get(`/projects/${pid}/runs`).then(setRuns)
  useEffect(() => { refresh() }, [pid])
  useEffect(() => { if (did) api.get(`/projects/${pid}/cost?dataset_id=${did}`).then(setCost).catch(() => setCost(null)) }, [did, spec])
  const start = async () => {
    setErr('')
    try { const r = await api.post(`/projects/${pid}/runs`, { dataset_id: did, mock: mock || undefined }); await refresh(); setRid(r.id) }
    catch (ex) { setErr(ex.detail && ex.detail.issues ? ex.detail.issues.map(i => i.message).join(' · ') : ex.message) }
  }
  if (rid) return <Run pid={pid} rid={rid} spec={spec} features={features} onBack={() => { setRid(null); refresh() }} />
  return (
    <div className="page">
      <div className="card">
        <h3>Start a run</h3>
        <div className="row"><select value={did || ''} onChange={e => setDid(e.target.value)} style={{ width: 300 }}><option value="">— dataset —</option>{datasets.map(d => <option key={d.id} value={d.id}>{d.name} ({d.n_rows} rows)</option>)}</select>
          <button className="primary" disabled={!did} onClick={start}>Run {spec.optimizer.goal === 'compress' ? 'compression' : spec.optimizer.engine.toUpperCase()} · {spec.optimizer.rounds} rounds{mock ? ' (mock)' : ''}</button></div>
        {err && <div className="err">{err}</div>}
        <div className="help">Static validation runs again on start; run the pilot on the Data tab first for a cost projection with measured token counts.</div>
        {cost && <Cost cost={cost} />}
      </div>
      <div className="card"><h3>Runs</h3>
        {runs.length === 0 && <div className="muted">No runs yet.</div>}
        <table><thead><tr><th>started</th><th>status</th><th>best</th><th>hold-out</th><th>spent</th><th></th></tr></thead><tbody>
          {runs.map(r => <tr key={r.id} style={{ cursor: 'pointer' }} onClick={() => setRid(r.id)}>
            <td>{new Date(r.started * 1000).toLocaleString()}</td><td><span className={'pill ' + r.status}>{r.status}</span></td>
            <td>{r.summary && r.summary.best ? `${r.summary.best.score.toFixed(3)} (root ${(r.summary.root_score ?? 0).toFixed(3)})` : '—'}</td>
            <td>{r.summary && r.summary.holdout ? `${r.summary.holdout.best_score.toFixed(3)} (root ${r.summary.holdout.root_score.toFixed(3)})` : '—'}</td>
            <td>{r.summary ? `$${r.summary.spent_usd.toFixed(3)}` : ''}</td><td className="err">{r.error || ''}</td></tr>)}
        </tbody></table>
      </div>
    </div>
  )
}

function Run({ pid, rid, spec, features = {}, onBack }) {
  const [st, setSt] = useState(null)
  const [tree, setTree] = useState({ nodes: [] })
  const [sel, setSel] = useState(null)
  const live = st && st.live
  useEffect(() => {
    let stop = false
    const tick = async () => {
      if (stop) return
      try { const s = await api.get(`/runs/${rid}`); setSt(s); const t = await api.get(`/runs/${rid}/tree`); setTree(t); if (!['running'].includes(s.live.state)) return } catch {}
      setTimeout(tick, 2500)
    }
    tick(); return () => { stop = true }
  }, [rid])
  if (!st) return <div className="page muted">loading…</div>
  const best = live.best
  const hist = live.history || []
  const band = live.nondeterminism_band || 0
  const key = Object.keys(spec.evaluate.objective)[0]
  const rootSE = (() => { const r = tree.nodes.find(n => n.id === tree.root); return r && r.metrics_std && r.n ? (r.metrics_std[key] || 0) / Math.sqrt(r.n) : 0 })()
  return (
    <div className="page">
      <div className="row" style={{ marginBottom: 10 }}><button className="small" onClick={onBack}>← runs</button><span className={'pill ' + live.state}>{live.state}</span>
        <span className="muted">round {live.round ?? 0} · {live.step || ''} · {live.nodes || tree.nodes.length} nodes · {live.usage && live.usage.calls} calls · ${(live.spent_usd || 0).toFixed(3)}</span>
        <span className="muted">accepted {live.accepted ?? 0} / proposed {live.proposed ?? 0}</span>
        <div className="grow" />{live.state === 'running' && <button className="danger small" onClick={() => api.post(`/runs/${rid}/stop`)}>stop</button>}
        {features.v0 && best && live.state !== 'running' && <Publish pid={pid} rid={rid} nid={null} label="Publish best as version" />}</div>
      {live.error && <div className="err">{live.error}</div>}
      <div className="grid3">
        <div className="stat card"><div className="num">{best ? best.score.toFixed(3) : '—'}</div><div className="lbl">best objective on the eval set ({live.train_rows} rows)</div></div>
        <div className="stat card"><div className="num alt">{st.summary && st.summary.holdout ? st.summary.holdout.best_score.toFixed(3) : '—'}</div><div className="lbl">hold-out ({live.holdout_rows} rows){st.summary && st.summary.holdout ? ` · root ${st.summary.holdout.root_score.toFixed(3)}` : ' · reported when the run ends'}</div></div>
        <div className="stat card"><div className="num" style={{ color: 'var(--muted)' }}>±{band.toFixed(3)} / ±{rootSE.toFixed(3)}</div><div className="lbl">non-determinism band (pilot, cache bypassed) / sampling SE of the root</div></div>
      </div>
      <div className="grid2">
        <div className="card"><h3>Best so far</h3><Curve hist={hist} band={band} se={rootSE} root={tree.nodes.find(n => n.id === tree.root)} /></div>
        <div className="card"><h3>Tree</h3><Tree tree={tree} onSelect={setSel} sel={sel} /></div>
      </div>
      {spec.optimizer.goal === 'compress' && <div className="card"><h3>The front: accuracy against template tokens</h3>
        <Front tree={tree} spec={spec} rows={live.train_rows} onSelect={setSel} sel={sel} /></div>}
      {sel && <NodeDetail rid={rid} nid={sel} tree={tree} spec={spec} pid={pid} features={features} />}
      {st.summary && <div className="card"><h3>Summary</h3><div className="kv"><b>stopped</b><span>{st.summary.stopped_because}</span><b>rounds</b><span>{st.summary.rounds}</span><b>seconds</b><span>{Math.round(st.summary.seconds)}</span>
        </div>
        {st.summary.holdout && <HoldoutTable h={st.summary.holdout} />}</div>}
    </div>
  )
}

function HoldoutTable({ h }) {
  const keys = Object.keys(h.best).filter(k => !k.startsWith('parse_fail.') || h.best[k] || h.root[k])
  const fmt = v => v == null ? '—' : Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(3)
  return (
    <div style={{ marginTop: 10 }}>
      <div className="help">Hold-out metrics: the rows the search never saw, root prompt vs best. Objective root {fmt(h.root_score)} → best {fmt(h.best_score)}.</div>
      <table style={{ marginTop: 4 }}><thead><tr><th>metric</th><th>root</th><th>best</th><th>Δ</th></tr></thead><tbody>
        {keys.map(k => <tr key={k}><td><code>{k}</code></td><td>{fmt(h.root[k])}</td><td>{fmt(h.best[k])}</td><td className="muted">{h.root[k] == null || h.best[k] == null ? '—' : (h.best[k] - h.root[k] >= 0 ? '+' : '') + fmt(h.best[k] - h.root[k])}</td></tr>)}
      </tbody></table>
    </div>
  )
}

// Every fully evaluated prompt on the accuracy / template-token plane, root starred, the non-dominated ones joined:
// the weighted objective picks one point on this line, the exchange rate you set decides which (see Findings).
function Front({ tree, spec, rows, onSelect, sel }) {
  const acc = Object.keys(spec.evaluate.objective).find(k => spec.evaluate.objective[k] > 0) || 'accuracy'
  const pts = tree.nodes.filter(n => n.metrics && n.metrics.template_tokens != null && n.metrics[acc] != null && (!rows || !n.n || n.n >= rows))
  if (pts.length < 2) return <div className="muted">appears once a rewrite has been fully evaluated</div>
  const W = 1000, H = 240, P = 36
  const xsAll = pts.map(p => p.metrics.template_tokens), ysAll = pts.map(p => p.metrics[acc])
  const xlo = 0, xhi = Math.max(...xsAll) * 1.05, ylo = Math.max(0, Math.min(...ysAll) - 0.05), yhi = Math.min(1.02, Math.max(...ysAll) + 0.03)
  const xv = t => P + ((t - xlo) / Math.max(1e-9, xhi - xlo)) * (W - 2 * P)
  const yv = a => H - P - ((a - ylo) / Math.max(1e-9, yhi - ylo)) * (H - 2 * P)
  const dominated = p => pts.some(q => q !== p && q.metrics.template_tokens <= p.metrics.template_tokens && q.metrics[acc] >= p.metrics[acc] && (q.metrics.template_tokens < p.metrics.template_tokens || q.metrics[acc] > p.metrics[acc]))
  const front = pts.filter(p => !dominated(p)).sort((a, b) => a.metrics.template_tokens - b.metrics.template_tokens)
  const path = front.map((p, i) => `${i ? 'L' : 'M'}${xv(p.metrics.template_tokens)},${yv(p.metrics[acc])}`).join(' ')
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => xlo + f * (xhi - xlo))
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', background: '#0a0f1e', borderRadius: 8 }}>
      {ticks.map(t => <g key={t}><line x1={xv(t)} x2={xv(t)} y1={P} y2={H - P} stroke="#1a2340" /><text x={xv(t)} y={H - P + 14} fill="#9aa6c8" fontSize={10} textAnchor="middle">{Math.round(t)}</text></g>)}
      {[ylo, yhi].map(a => <text key={a} x={P - 4} y={yv(a) + 3} fill="#9aa6c8" fontSize={10} textAnchor="end">{a.toFixed(2)}</text>)}
      <path d={path} fill="none" stroke="#5ee3c8" strokeWidth={1.5} strokeDasharray="5 3" />
      {pts.map(p => <g key={p.id} style={{ cursor: 'pointer' }} onClick={() => onSelect(p.id)}>
        <circle cx={xv(p.metrics.template_tokens)} cy={yv(p.metrics[acc])} r={sel === p.id ? 7 : 5} fill={dominated(p) ? '#3b4a7a' : '#5ee3c8'} stroke={sel === p.id ? '#e8ecf7' : 'none'} strokeWidth={1.5} />
        {p.id === tree.root && <text x={xv(p.metrics.template_tokens)} y={yv(p.metrics[acc]) - 9} fill="#ffb454" fontSize={14} textAnchor="middle">★</text>}
        <title>{p.id}{p.id === tree.root ? ' (root)' : ''} · {acc} {p.metrics[acc].toFixed(3)} · {Math.round(p.metrics.template_tokens)} template tokens</title>
      </g>)}
      <text x={W - P} y={12} fill="#9aa6c8" fontSize={10} textAnchor="end">★ root · teal: on the front · grey: a shorter or more accurate prompt exists · template tokens →</text>
    </svg>
  )
}

function Curve({ hist, band, se, root }) {
  const pts = hist.filter(h => h.best !== null && h.best !== undefined)
  if (!pts.length) return <div className="muted">waiting for the first evaluation…</div>
  const W = 520, H = 220, P = 30
  const xs = pts.map((_, i) => i), ys = pts.map(p => p.best)
  const rootY = root && root.score !== null ? root.score : ys[0]
  const lo = Math.min(...ys, rootY - band - se) , hi = Math.max(...ys, rootY + band + se)
  const yv = v => H - P - ((v - lo) / Math.max(1e-9, hi - lo)) * (H - 2 * P)
  const xv = i => P + (i / Math.max(1, xs.length - 1)) * (W - 2 * P)
  const path = pts.map((p, i) => `${i ? 'L' : 'M'}${xv(i)},${yv(p.best)}`).join(' ')
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', background: '#0a0f1e', borderRadius: 8 }}>
      <rect x={P} y={yv(rootY + band)} width={W - 2 * P} height={Math.max(0, yv(rootY - band) - yv(rootY + band))} fill="rgba(255,180,84,.15)" />
      <rect x={P} y={yv(rootY + se)} width={W - 2 * P} height={Math.max(0, yv(rootY - se) - yv(rootY + se))} fill="rgba(154,166,200,.18)" />
      <line x1={P} x2={W - P} y1={yv(rootY)} y2={yv(rootY)} stroke="#9aa6c8" strokeDasharray="4 3" />
      <path d={path} fill="none" stroke="#5ee3c8" strokeWidth={2} />
      {pts.map((p, i) => <circle key={i} cx={xv(i)} cy={yv(p.best)} r={2.5} fill="#5ee3c8" />)}
      <text x={P} y={12} fill="#9aa6c8" fontSize={10}>{hi.toFixed(3)}</text><text x={P} y={H - 4} fill="#9aa6c8" fontSize={10}>{lo.toFixed(3)} · steps →</text>
      <text x={W - P} y={12} fill="#ffb454" fontSize={10} textAnchor="end">orange: non-determinism · grey: sampling SE · dashed: root</text>
    </svg>
  )
}

function Tree({ tree, onSelect, sel }) {
  const layout = useMemo(() => {
    const byDepth = {}
    tree.nodes.forEach(n => (byDepth[n.depth] = byDepth[n.depth] || []).push(n))
    const pos = {}
    Object.entries(byDepth).forEach(([d, ns]) => ns.forEach((n, i) => { pos[n.id] = { x: 40 + i * (Math.min(120, 900 / Math.max(1, ns.length))), y: 30 + d * 70 } }))
    return pos
  }, [tree])
  const scores = tree.nodes.filter(n => n.score !== null).map(n => n.score)
  const lo = Math.min(...scores, 0), hi = Math.max(...scores, 1e-9)
  const color = s => s === null ? '#26325a' : `hsl(${(s - lo) / Math.max(1e-9, hi - lo) * 140}, 70%, 45%)`
  const maxD = Math.max(0, ...tree.nodes.map(n => n.depth))
  const maxW = Math.max(...Object.values(layout).map(p => p.x), 200) + 60
  return (
    <div style={{ overflow: 'auto', maxHeight: 420 }}><svg className="tree" width={maxW} height={maxD * 70 + 70}>
      {tree.nodes.filter(n => n.parent_id && layout[n.parent_id]).map(n => <line key={'l' + n.id} x1={layout[n.parent_id].x} y1={layout[n.parent_id].y} x2={layout[n.id].x} y2={layout[n.id].y} stroke="#26325a" />)}
      {tree.nodes.map(n => <g key={n.id} transform={`translate(${layout[n.id].x},${layout[n.id].y})`} style={{ cursor: 'pointer' }} onClick={() => onSelect(n.id)}>
        <circle r={16} fill="transparent" />
        <circle r={sel === n.id ? 11 : 8} fill={color(n.score)} stroke={sel === n.id ? '#5ee3c8' : (n.state === 'expanded' ? '#e8ecf7' : 'none')} strokeWidth={1.5} />
        <title>{n.id} · {n.origin.op}{n.origin.params && n.origin.params.module ? ' · ' + n.origin.params.module : ''} · score {n.score === null ? '—' : n.score.toFixed(3)}{n.n ? ` (n=${n.n})` : ''}</title>
        {n.score !== null && <text y={22} textAnchor="middle" fontSize={9} fill="#9aa6c8">{n.score.toFixed(2)}{n.n && n.n < 20 ? '*' : ''}</text>}
      </g>)}
      <text x={8} y={maxD * 70 + 62} fontSize={9} fill="#9aa6c8">* check batch only · white ring = rewritten from · greener = higher</text>
    </svg></div>
  )
}

// What a score of 0.79 hides: which classes it is getting wrong. A per-class table makes a collapse obvious -
// "billing 0/14, nine of them answered bug" - where a single number and a list of rows does not.
function ByClass({ per, expected, spec }) {
  const [full, setFull] = useState(false)
  const field = (spec.evaluate.scorers[0] || {}).field
  const answer = r => {
    const p = r.parsed
    if (p && typeof p === 'object') {
      if (field && field in p) return String(p[field])
      const ks = Object.keys(p)
      if (ks.length === 1) return String(p[ks[0]])
    }
    return (r.output || '').trim().slice(0, 40)
  }
  const rows = per.filter(r => expected[r.example_id] !== undefined)
  const classes = [...new Set(rows.map(r => String(expected[r.example_id])))]
  if (classes.length < 2 || classes.length > 14 || rows.length === 0) return null
  const key = Object.keys(spec.evaluate.objective)[0]
  const table = {}
  classes.forEach(c => (table[c] = { n: 0, right: 0, answers: {} }))
  rows.forEach(r => {
    const t = String(expected[r.example_id]), a = answer(r)
    const cell = table[t]
    cell.n++
    if (r.metrics && r.metrics[key] >= 1) cell.right++
    else cell.answers[a] = (cell.answers[a] || 0) + 1
  })
  const order = classes.sort((a, b) => (table[a].right / table[a].n) - (table[b].right / table[b].n))
  const shown = full ? order : order.slice(0, 8)
  return (
    <div style={{ marginTop: 10 }}>
      <div className="row"><b style={{ fontSize: 13 }}>Per class</b><span className="muted" style={{ fontSize: 12 }}>worst first</span>
        <div className="grow" />{order.length > 8 && <button className="small" onClick={() => setFull(!full)}>{full ? 'top 8' : `all ${order.length}`}</button>}</div>
      <table style={{ marginTop: 4 }}><thead><tr><th>label</th><th>rows</th><th>right</th><th>answered instead</th></tr></thead><tbody>
        {shown.map(c => {
          const t = table[c], pct = t.right / t.n
          return <tr key={c}>
            <td><code>{c}</code></td><td className="muted">{t.n}</td>
            <td style={{ color: pct >= 0.8 ? 'var(--accent)' : pct >= 0.4 ? 'var(--accent2)' : 'var(--danger)' }}>{t.right}/{t.n}</td>
            <td className="muted" style={{ fontSize: 12 }}>{Object.entries(t.answers).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([a, n]) => `${a} ×${n}`).join(' · ') || '—'}</td>
          </tr>
        })}
      </tbody></table>
    </div>
  )
}

function diffWords(a, b) {
  const A = (a || '').split(/(\s+)/), B = (b || '').split(/(\s+)/)
  const n = A.length, m = B.length, dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
  const out = []; let i = 0, j = 0
  while (i < n && j < m) { if (A[i] === B[j]) { out.push(['=', A[i]]); i++; j++ } else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push(['-', A[i]]); i++ } else { out.push(['+', B[j]]); j++ } }
  while (i < n) out.push(['-', A[i++]]); while (j < m) out.push(['+', B[j++]])
  return out
}

function Publish({ pid, rid, nid, label, onDone }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [done, setDone] = useState(null)
  const go = async () => {
    setBusy(true); setErr('')
    try { setDone(await api.post(`/projects/${pid}/versions`, { run_id: rid, node_id: nid || undefined })); onDone && onDone() }
    catch (e) { setErr(e.message) } finally { setBusy(false) }
  }
  return <>
    <button className="small" disabled={busy} onClick={go} title="Freeze these prompts, with their score, as something that can be served">{busy ? 'publishing…' : label}</button>
    {done && <span className="ok" style={{ fontSize: 12.5 }}>published as <b>{done.label}</b> — see the Serving tab</span>}
    {err && <span className="err">{err}</span>}
  </>
}

function NodeDetail({ rid, nid, tree, spec, pid, features = {} }) {
  const [d, setD] = useState(null)
  useEffect(() => { api.get(`/runs/${rid}/nodes/${nid}`).then(setD) }, [rid, nid])
  if (!d) return null
  const n = d.node, ev = n.evaluation, mods = n.prompt.modules || { prompt: { template: n.prompt.template } }
  const changed = d.parent_modules ? Object.keys(mods).filter(k => d.parent_modules[k] !== mods[k].template) : Object.keys(mods)
  const key = Object.keys(spec.evaluate.objective)[0]
  return (
    <div className="card">
      <div className="row"><h3 style={{ margin: 0 }}>Node {n.id} <span className="muted">· {n.origin.op}{n.origin.params.module ? ` on ${n.origin.params.module}` : ''} · depth {n.depth}{ev ? ` · score ${ev.score.toFixed(3)} on ${ev.n} rows` : ' · not evaluated'}</span></h3>
        <div className="grow" />{features.v0 && ev && <Publish pid={pid} rid={rid} nid={n.id} label="Publish this node as a version" />}</div>
      {ev && <div className="help">metrics: {Object.entries(ev.metrics).map(([k, v]) => `${k} ${v.toFixed(3)}`).join(' · ')}</div>}
      {Object.keys(mods).map(k => (
        <div key={k} style={{ marginBottom: 10 }}>
          <div className="row"><b>{k}</b>{changed.includes(k) && d.parent_modules && <span className="pill">changed vs parent</span>}</div>
          {changed.includes(k) && d.parent_modules ? <pre className="diff">{diffWords(d.parent_modules[k], mods[k].template).map(([t, w], i) => t === '=' ? <span key={i}>{w}</span> : t === '-' ? <del key={i}>{w}</del> : <ins key={i}>{w}</ins>)}</pre> : <pre>{mods[k].template}</pre>}
        </div>
      ))}
      {n.origin.op === 'reflect' && <div className="help">Rewritten from node {n.origin.params.source}, after seeing rows {JSON.stringify(n.origin.params.minibatch_ids)}.</div>}
      {ev && ev.per_example && d.expected && <ByClass per={ev.per_example} expected={d.expected} spec={spec} />}
      {ev && ev.per_example && <details><summary className="muted" style={{ cursor: 'pointer' }}>per-example results ({ev.per_example.length})</summary>
        <table><thead><tr><th>id</th><th>path</th><th>output</th><th>{key}</th><th>critic note</th></tr></thead><tbody>
          {ev.per_example.slice(0, 50).map(r => <tr key={r.example_id}><td className="mono">{r.example_id}</td><td className="mono muted">{Object.keys(r.trace || {}).filter(k => !k.startsWith('_')).join(' → ')}</td>
            <td className="mono" style={{ maxWidth: 360 }}>{r.error ? <span className="err">{r.error}</span> : (r.parsed ? JSON.stringify(r.parsed) : r.output.slice(0, 200))}</td>
            <td>{r.metrics && r.metrics[key] !== undefined ? r.metrics[key].toFixed(2) : '—'}</td><td className="muted" style={{ maxWidth: 300, fontSize: 12 }}>{r.trace && r.trace._critic}</td></tr>)}
        </tbody></table></details>}
    </div>
  )
}
