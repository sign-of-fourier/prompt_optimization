import React, { useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import Hint from './Hint.jsx'

// The library: project templates we curate. Nobody else publishes here, so there is no trust surface to design -
// what the screen has to do is stay navigable as the catalogue grows, and answer the three questions a card cannot:
// what does this do, what do I need before it works, and what will it cost me to run.
export default function Library({ onOpen, onBack, deepLink }) {
  const [data, setData] = useState({ entries: [], tags: [] })
  const [q, setQ] = useState('')
  const [tag, setTag] = useState('')
  const [sel, setSel] = useState(deepLink || null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  useEffect(() => { api.get('/examples').then(setData).catch(e => setErr(e.message)) }, [])

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return data.entries.filter(e =>
      (!tag || e.tags.includes(tag)) &&
      (!needle || [e.name, e.blurb, e.description, ...e.tags].join(' ').toLowerCase().includes(needle)))
  }, [data, q, tag])

  const entry = sel && data.entries.find(e => e.slug === sel)
  const add = async e => {
    setBusy(e.slug); setErr('')
    try { const r = await api.post(`/examples/${e.slug}/clone`); onOpen(r.id) }
    catch (ex) { setErr(ex.message) } finally { setBusy('') }
  }

  return (
    <div className="page">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>Library<Hint id="library" /></h2>
        <span className="muted">{data.entries.length} template{data.entries.length === 1 ? '' : 's'}</span>
        <div className="grow" />
        <input placeholder="search" value={q} onChange={e => setQ(e.target.value)} style={{ width: 220 }} />
        <button className="small" onClick={onBack}>← projects</button>
      </div>
      <div className="row" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
        <button className={'small' + (tag ? '' : ' primary')} onClick={() => setTag('')}>all</button>
        {data.tags.map(t => <button key={t} className={'small' + (tag === t ? ' primary' : '')} onClick={() => setTag(tag === t ? '' : t)}>{t}</button>)}
      </div>
      {err && <div className="err">{err}</div>}
      {shown.length === 0 && <p className="muted">Nothing matches. <a href="#" onClick={ev => { ev.preventDefault(); setQ(''); setTag('') }}>Clear the filters</a>.</p>}

      <div className="grid2">
        {shown.map(e => (
          <div className="card" key={e.slug} style={{ cursor: 'pointer', borderColor: sel === e.slug ? 'var(--accent)' : undefined }}
            onClick={() => setSel(sel === e.slug ? null : e.slug)}>
            <div className="row"><b>{e.name}</b>{e.requires.length > 0 && <span className="pill" title="needs setting up before it will run">setup</span>}</div>
            <p className="muted" style={{ margin: '6px 0 10px', fontSize: 13 }}>{e.blurb}</p>
            <div className="row" style={{ flexWrap: 'wrap' }}>{e.tags.map(t => <span className="chip" key={t}>{t}</span>)}</div>
            <div className="row" style={{ marginTop: 8 }}>
              <span className="muted" style={{ fontSize: 12 }}>
                {e.modules} prompt{e.modules === 1 ? '' : 's'}{e.steps ? ` · ${e.steps} external step${e.steps === 1 ? '' : 's'}` : ''} · {e.rows} rows
              </span>
              <div className="grow" />
              <button className="small primary" disabled={busy === e.slug} onClick={ev => { ev.stopPropagation(); add(e) }}>{busy === e.slug ? 'adding…' : 'Add'}</button>
            </div>
          </div>
        ))}
      </div>

      {entry && <div className="card" style={{ marginTop: 14, borderColor: 'var(--accent)' }}>
        <div className="row"><h3 style={{ margin: 0 }}>{entry.name}</h3><div className="grow" />
          <button className="small" onClick={() => setSel(null)}>×</button></div>
        <p style={{ fontSize: 13.5, lineHeight: 1.65 }}>{entry.description || entry.blurb}</p>
        {entry.requires.length > 0 && <>
          <label>Before it will run</label>
          <ul className="muted" style={{ fontSize: 13, margin: '4px 0 10px', paddingLeft: 18 }}>
            {entry.requires.map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </>}
        <div className="kv">
          <b>program</b><span>{entry.modules} prompt{entry.modules === 1 ? '' : 's'}{entry.steps ? `, ${entry.steps} external step${entry.steps === 1 ? '' : 's'}` : ''}</span>
          <b>dataset</b><span>{entry.rows} rows{entry.dataset ? ` · ${entry.dataset}` : ''}</span>
          <b>objective</b><span>{entry.goal === 'compress' ? 'compression — accuracy against tokens' : 'accuracy'}</span>
          <b>defaults</b><span>{entry.rounds} rounds on {entry.eval_model}</span>
          {entry.updated && <><b>updated</b><span>{entry.updated}</span></>}
        </div>
        <div className="help">Adding copies the whole thing into your projects — the program, its settings and the
          dataset. Edit and run it like your own; the original is untouched.</div>
        <div className="row" style={{ marginTop: 10 }}>
          <button className="primary" disabled={busy === entry.slug} onClick={() => add(entry)}>{busy === entry.slug ? 'adding…' : 'Add to my projects'}</button>
          <span className="muted" style={{ fontSize: 12 }}>link: <code>{location.origin + location.pathname}#library/{entry.slug}</code></span>
        </div>
      </div>}
    </div>
  )
}
