import React, { useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import { Cost } from './DataPanel.jsx'

export default function RunView({ pid, spec, datasets, mock }) {
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
  if (rid) return <Run rid={rid} spec={spec} onBack={() => { setRid(null); refresh() }} />
  return (
    <div className="page">
      <div className="card">
        <h3>Start a run</h3>
        <div className="row"><select value={did || ''} onChange={e => setDid(e.target.value)} style={{ width: 300 }}><option value="">— dataset —</option>{datasets.map(d => <option key={d.id} value={d.id}>{d.name} ({d.n_rows} rows)</option>)}</select>
          <button className="primary" disabled={!did} onClick={start}>Run {spec.optimizer.engine.toUpperCase()} · {spec.optimizer.rounds} rounds{mock ? ' (mock)' : ''}</button></div>
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

function Run({ rid, spec, onBack }) {
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
        <div className="grow" />{live.state === 'running' && <button className="danger small" onClick={() => api.post(`/runs/${rid}/stop`)}>stop</button>}</div>
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
      {sel && <NodeDetail rid={rid} nid={sel} tree={tree} spec={spec} />}
      {st.summary && <div className="card"><h3>Summary</h3><div className="kv"><b>stopped</b><span>{st.summary.stopped_because}</span><b>rounds</b><span>{st.summary.rounds}</span><b>seconds</b><span>{Math.round(st.summary.seconds)}</span>
        {st.summary.holdout && <><b>hold-out metrics (best)</b><span className="mono">{JSON.stringify(st.summary.holdout.best)}</span><b>hold-out metrics (root)</b><span className="mono">{JSON.stringify(st.summary.holdout.root)}</span></>}</div></div>}
    </div>
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

function diffWords(a, b) {
  const A = (a || '').split(/(\s+)/), B = (b || '').split(/(\s+)/)
  const n = A.length, m = B.length, dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
  const out = []; let i = 0, j = 0
  while (i < n && j < m) { if (A[i] === B[j]) { out.push(['=', A[i]]); i++; j++ } else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push(['-', A[i]]); i++ } else { out.push(['+', B[j]]); j++ } }
  while (i < n) out.push(['-', A[i++]]); while (j < m) out.push(['+', B[j++]])
  return out
}

function NodeDetail({ rid, nid, tree, spec }) {
  const [d, setD] = useState(null)
  useEffect(() => { api.get(`/runs/${rid}/nodes/${nid}`).then(setD) }, [rid, nid])
  if (!d) return null
  const n = d.node, ev = n.evaluation, mods = n.prompt.modules || { prompt: { template: n.prompt.template } }
  const changed = d.parent_modules ? Object.keys(mods).filter(k => d.parent_modules[k] !== mods[k].template) : Object.keys(mods)
  const key = Object.keys(spec.evaluate.objective)[0]
  return (
    <div className="card">
      <h3>Node {n.id} <span className="muted">· {n.origin.op}{n.origin.params.module ? ` on ${n.origin.params.module}` : ''} · depth {n.depth}{ev ? ` · score ${ev.score.toFixed(3)} on ${ev.n} rows` : ' · not evaluated'}</span></h3>
      {ev && <div className="help">metrics: {Object.entries(ev.metrics).map(([k, v]) => `${k} ${v.toFixed(3)}`).join(' · ')}</div>}
      {Object.keys(mods).map(k => (
        <div key={k} style={{ marginBottom: 10 }}>
          <div className="row"><b>{k}</b>{changed.includes(k) && d.parent_modules && <span className="pill">changed vs parent</span>}</div>
          {changed.includes(k) && d.parent_modules ? <pre className="diff">{diffWords(d.parent_modules[k], mods[k].template).map(([t, w], i) => t === '=' ? <span key={i}>{w}</span> : t === '-' ? <del key={i}>{w}</del> : <ins key={i}>{w}</ins>)}</pre> : <pre>{mods[k].template}</pre>}
        </div>
      ))}
      {n.origin.op === 'reflect' && <div className="help">Rewritten from node {n.origin.params.source}, after seeing rows {JSON.stringify(n.origin.params.minibatch_ids)}.</div>}
      {ev && ev.per_example && <details><summary className="muted" style={{ cursor: 'pointer' }}>per-example results ({ev.per_example.length})</summary>
        <table><thead><tr><th>id</th><th>path</th><th>output</th><th>{key}</th><th>critic note</th></tr></thead><tbody>
          {ev.per_example.slice(0, 50).map(r => <tr key={r.example_id}><td className="mono">{r.example_id}</td><td className="mono muted">{Object.keys(r.trace || {}).filter(k => !k.startsWith('_')).join(' → ')}</td>
            <td className="mono" style={{ maxWidth: 360 }}>{r.error ? <span className="err">{r.error}</span> : (r.parsed ? JSON.stringify(r.parsed) : r.output.slice(0, 200))}</td>
            <td>{r.metrics && r.metrics[key] !== undefined ? r.metrics[key].toFixed(2) : '—'}</td><td className="muted" style={{ maxWidth: 300, fontSize: 12 }}>{r.trace && r.trace._critic}</td></tr>)}
        </tbody></table></details>}
    </div>
  )
}
