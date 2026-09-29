import React, { useEffect, useState } from 'react'
import { api } from './api.js'

// What each hosted version is doing (DASHBOARD.md, step 2): traffic, errors, speed, cost, and the score it earned.
// Reviews, the Jev line, the out-of-date badge and alerts are later steps; their columns are not shown until they exist.
const usd = (x, d = 2) => x == null ? '—' : x < 0.01 ? '$' + (x === 0 ? '0' : x.toPrecision(2)) : '$' + x.toFixed(d)
const secs = x => x == null ? '—' : x.toFixed(x < 10 ? 1 : 0) + ' s'

function Spark({ vals, w = 110, h = 28 }) {
  const max = Math.max(1, ...vals), n = vals.length
  const x = i => 2 + i * (w - 4) / Math.max(1, n - 1), y = v => h - 3 - v / max * (h - 6)
  const d = vals.map((v, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1)).join(' ')
  return <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true" style={{ display: 'block' }}>
    <path d={`${d} L${x(n - 1).toFixed(1)} ${h - 1} L${x(0).toFixed(1)} ${h - 1} Z`} fill="rgba(94,227,200,.12)" />
    <path d={d} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
    <circle cx={x(n - 1)} cy={y(vals[n - 1])} r="2.5" fill="var(--accent)" />
  </svg>
}

const STATUS = { healthy: 'var(--ok)', errors: 'var(--danger)', idle: 'var(--muted)' }

export default function Dashboard({ onOpen }) {
  const [win, setWin] = useState('24h')
  const [d, setD] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    let live = true
    const load = () => api.get(`/dashboard?window=${win}`).then(x => live && setD(x)).catch(e => live && setErr(e.message))
    load(); const t = setInterval(load, 30000)
    return () => { live = false; clearInterval(t) }
  }, [win])
  if (err) return <div className="page"><div className="err">{err}</div></div>
  if (!d) return <div className="page muted">Loading…</div>
  const t = d.totals
  return (
    <div className="page">
      <div className="row" style={{ marginBottom: 12, alignItems: 'flex-end' }}>
        <div className="grow"><h2 style={{ margin: 0 }}>Hosted versions <span className="muted" style={{ fontSize: 12, fontWeight: 400 }}>beta</span></h2>
          <div className="muted" style={{ fontSize: 12.5 }}>Updated {new Date(d.updated * 1000).toLocaleTimeString()} · requests reach this page within a minute</div></div>
        <div className="row" style={{ gap: 0 }}>{['24h', '7d'].map(w => <button key={w} className="small" style={{ borderRadius: w === '24h' ? '8px 0 0 8px' : '0 8px 8px 0', background: win === w ? 'var(--card)' : 'transparent', color: win === w ? 'var(--text)' : 'var(--muted)' }} onClick={() => setWin(w)}>{w === '24h' ? '24 h' : '7 d'}</button>)}</div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: 12, marginBottom: 16 }}>
        <Stat k={`Requests, ${d.window === '7d' ? 'last 7 days' : 'last 24 h'}`} v={t.requests.toLocaleString()} s="across all hosted versions" />
        <Stat k="Spend this month" v={usd(t.month)} s={t.forecast != null ? `forecast ${usd(t.forecast)} by month end` : t.forecast_partial ? `forecast when every version is 3+ days old` : ''} />
        <Stat k="Last month" v={usd(t.last_month)} s="AWS cost; the surcharge is not set yet" />
        <Stat k="Hosted versions" v={t.hosted} s="answering requests" />
      </div>
      {d.rows.length === 0 && <div className="card muted">Nothing is hosted. Publish a version from a run, then Host it from the project's Serving tab.</div>}
      {d.rows.length > 0 && <div className="card" style={{ padding: 0, overflowX: 'auto' }}>
        <table style={{ minWidth: 900 }}><thead><tr>
          <th>Hosted version</th><th>Requests</th><th>Errors</th><th>Latency p50 / p95</th><th>Cost</th><th>Earned</th><th>Status</th>
        </tr></thead><tbody>
          {d.rows.map(r => <tr key={r.version_id} style={{ cursor: 'pointer' }} onClick={() => onOpen(r.project_id)} title="Open the project's Serving tab">
            <td><b>{r.project}</b> · {r.label}<div className="muted" style={{ fontSize: 12 }}>hosted {new Date(r.hosted_at * 1000).toLocaleDateString()}</div></td>
            <td className="mono">{r.requests.toLocaleString()}<Spark vals={r.sparkline} /></td>
            <td className="mono" style={{ color: r.error_rate > 0.01 ? 'var(--accent2)' : undefined }}>{r.requests ? (r.error_rate * 100).toFixed(1) + '%' : '—'}</td>
            <td className="mono">{secs(r.p50)} / {secs(r.p95)}</td>
            <td><span className="mono">{usd(r.cost.today, 3)}</span> <span className="muted" style={{ fontSize: 12 }}>today</span>
              <div><span className="mono">{usd(r.cost.month)}</span> <span className="muted" style={{ fontSize: 12 }}>this month · {r.cost.forecast != null ? `forecast ${usd(r.cost.forecast)}` : r.cost.forecast_note}</span></div>
              <div className="muted mono" style={{ fontSize: 12, color: r.cost.per_request && r.cost.estimate && r.cost.per_request > 1.2 * r.cost.estimate ? 'var(--accent2)' : undefined }}>
                {usd(r.cost.per_request)} / request{r.cost.estimate ? ` (est. ${usd(r.cost.estimate)})` : ''}</div></td>
            <td className="mono">{r.earned.score == null ? '—' : r.earned.score.toFixed(2)}<div className="muted" style={{ fontSize: 12 }}>{r.earned.holdout != null ? `hold-out ${r.earned.holdout.toFixed(2)}` : 'no hold-out'}</div></td>
            <td><span style={{ color: STATUS[r.status], border: '1px solid currentColor', borderRadius: 999, padding: '1px 8px', fontSize: 12, whiteSpace: 'nowrap' }}>● {r.status_text}</span></td>
          </tr>)}
        </tbody></table></div>}
      <div className="help" style={{ marginTop: 10 }}>Cost is the AWS cost of each request, loops and document searches included. The forecast adds the last 7 days' average for each day left in the month. Click a row to see its traces.</div>
    </div>
  )
}

function Stat({ k, v, s }) {
  return <div className="card" style={{ margin: 0 }}>
    <div className="muted" style={{ fontSize: 11.5, textTransform: 'uppercase', letterSpacing: '.5px' }}>{k}</div>
    <div className="mono" style={{ fontSize: 22, fontWeight: 600 }}>{v}</div>
    <div className="muted" style={{ fontSize: 12.5 }}>{s}</div>
  </div>
}
