import React, { useEffect, useState } from 'react'
import { api } from './api.js'

// Documents a prompt can look things up in, through the "Search documents" step. The browser shows each document as
// the passages retrieval actually searches - the unit that decides what a prompt gets to see - not as the raw file.
export default function Documents() {
  const [sets, setSets] = useState([])
  const [limits, setLimits] = useState(null)
  const [cid, setCid] = useState('')
  const [cur, setCur] = useState(null)
  const [open, setOpen] = useState(null)   // a document with its chunks
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [note, setNote] = useState('')

  const loadSets = async (pick) => {
    const r = await api.get('/corpora')
    setSets(r.corpora); setLimits(r.limits)
    const want = pick || cid || (r.corpora[0] && r.corpora[0].id) || ''
    setCid(want)
  }
  const loadSet = async () => { if (cid) setCur(await api.get(`/corpora/${cid}`)); else setCur(null) }
  useEffect(() => { loadSets().catch(e => setErr(e.message)) }, [])
  useEffect(() => { setOpen(null); loadSet().catch(e => setErr(e.message)) }, [cid])

  const act = async (label, fn) => {
    setBusy(label); setErr(''); setNote('')
    try { await fn() } catch (e) { setErr(e.message) } finally { setBusy('') }
  }
  const create = () => act('creating', async () => {
    const name = window.prompt('Name this document set', 'Help centre'); if (!name) return
    const c = await api.post('/corpora', { name }); await loadSets(c.id)
  })
  const upload = e => {
    const files = [...e.target.files]; e.target.value = ''
    if (!files.length) return
    act('uploading', async () => {
      const r = await api.uploadMany(`/corpora/${cid}/documents`, files)
      const bits = [`${r.added.length} added`]
      if (r.replaced.length) bits.push(`${r.replaced.length} replaced`)
      if (r.unchanged.length) bits.push(`${r.unchanged.length} unchanged`)
      if (r.embedded && r.embedded.error) setErr(r.embedded.error)
      setNote(bits.join(' · ')); await loadSet(); await loadSets(cid)
    })
  }
  const view = d => act('opening', async () => setOpen(open && open.id === d.id ? null : await api.get(`/corpora/${cid}/documents/${d.id}`)))
  const remove = d => {
    if (!window.confirm(`Delete ${d.name}? It is removed for good, including from any published version that searches it.`)) return
    act('deleting', async () => {
      const r = await api.del(`/corpora/${cid}/documents/${d.id}`)
      if (r.versions_affected && r.versions_affected.length)
        setNote(`${r.versions_affected.length} published version(s) searched ${d.name} and will now refuse to answer until you publish a new one.`)
      setOpen(null); await loadSet(); await loadSets(cid)
    })
  }
  const embed = () => act('embedding', async () => { await api.post(`/corpora/${cid}/embed`); await loadSet() })
  const kb = n => n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`
  const em = cur && cur.embedding

  return (
    <div className="card">
      <h3>Documents</h3>
      <div className="help">Files your prompts can look things up in: policies, help articles, product sheets. Add a
        <b> Search documents</b> step on the canvas and it finds the passages that match each question and hands
        them to your prompt. Markdown or plain text; headings make better passages.</div>
      <div className="row" style={{ marginTop: 8 }}>
        <select className="grow" value={cid} onChange={e => setCid(e.target.value)}>
          <option value="">— choose a document set —</option>
          {sets.map(s => <option key={s.id} value={s.id}>{s.name} ({s.n_documents} files)</option>)}
        </select>
        <button className="small" disabled={!!busy} onClick={create}>New set</button>
      </div>
      {cur && <>
        <div className="row" style={{ marginTop: 8 }}>
          <label className="row" style={{ margin: 0 }}><input type="file" multiple accept=".md,.markdown,.txt" disabled={!!busy} onChange={upload} style={{ width: 'auto' }} /></label>
          <div className="grow" />
          <span className="muted" style={{ fontSize: 12 }}>{cur.n_documents} files · {cur.n_chunks} passages · {kb(cur.bytes)}
            {limits && ` (up to ${limits.files} files, ${kb(limits.bytes)}, ${limits.chunks} passages)`}</span>
        </div>
        {em && (em.pending
          ? <div className="issue warn" style={{ marginTop: 8 }}><span className="lvl">wait</span><div>{em.pending} file(s) are stored but not searchable yet.
              <button className="small" style={{ marginLeft: 8 }} disabled={!!busy} onClick={embed}>{busy === 'embedding' ? 'preparing…' : `Make searchable (≈ $${em.pending_usd_est.toFixed(4)})`}</button></div></div>
          : cur.n_documents > 0 && <div className="help"><span className="pill">searchable</span> every file is ready for the Search documents step.</div>)}
        {busy && busy !== 'embedding' && <div className="muted">{busy}…</div>}
        {note && <div className="help">{note}</div>}
        {cur.documents.length > 0 && <div style={{ maxHeight: 520, overflow: 'auto', marginTop: 8 }}><table><thead><tr><th>file</th><th>passages</th><th>size</th><th /></tr></thead>
          <tbody>{cur.documents.map(d => <React.Fragment key={d.id}>
            <tr><td className="mono">{d.name}</td><td>{d.n_chunks}</td><td>{kb(d.bytes)}</td>
              <td style={{ whiteSpace: 'nowrap' }}><button className="small" onClick={() => view(d)}>{open && open.id === d.id ? 'Hide' : 'View'}</button>{' '}
                <button className="small" onClick={() => remove(d)}>Delete</button></td></tr>
            {open && open.id === d.id && <tr><td colSpan={4}>
              <div className="help">What the search sees: {open.chunks.length} passage{open.chunks.length === 1 ? '' : 's'}, each under its headings.</div>
              <div style={{ maxHeight: 360, overflow: 'auto' }}>{open.chunks.map(c => <div key={c.id} style={{ borderTop: '1px solid var(--line)', padding: '6px 0' }}>
                <div className="muted" style={{ fontSize: 12 }}>{c.heading || '(no heading)'} · ~{c.tokens} tokens</div>
                <div className="mono" style={{ fontSize: 12, whiteSpace: 'pre-wrap' }}>{c.text}</div></div>)}</div>
            </td></tr>}
          </React.Fragment>)}</tbody></table></div>}
      </>}
      {err && <div className="err">{err}</div>}
    </div>
  )
}
