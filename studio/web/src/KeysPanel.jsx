import React, { useEffect, useState } from 'react'
import { api } from './api.js'

// Workspace keys for the serving endpoint. Model credentials (above) are what the studio calls out with; these are
// what calls the studio in. Shown once, stored as a sha256.
function ApiKeys() {
  const [keys, setKeys] = useState([])
  const [fresh, setFresh] = useState(null)
  const [label, setLabel] = useState('')
  const load = () => api.get('/keys').then(setKeys).catch(() => {})
  useEffect(() => { load() }, [])
  const add = async () => { const k = await api.post('/keys', { label }); setLabel(''); setFresh(k); load() }
  const del = async id => { await api.del(`/keys/${id}`); setFresh(null); load() }
  return (
    <div style={{ marginTop: 14 }}>
      <h3 style={{ fontSize: 14, margin: '0 0 6px' }}>API keys <span className="pill">serving</span></h3>
      <div className="help">Used to call a published version over HTTP: <code>Authorization: Bearer &lt;key&gt;</code>. The session
        cookie does not work on that endpoint. A key is shown once - if it is lost, revoke it and make another.</div>
      {fresh && <div className="card" style={{ borderColor: 'var(--accent)' }}>
        <b>Copy this now.</b><pre className="mono" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{fresh.key}</pre></div>}
      {keys.map(k => <div className="card row" key={k.id}><b>{k.label}</b><span className="mono muted">{k.prefix}…</span>
        <span className="muted" style={{ fontSize: 12 }}>{k.last_used ? `last used ${new Date(k.last_used * 1000).toLocaleString()}` : 'never used'}</span>
        <div className="grow" /><button className="small danger" onClick={() => del(k.id)}>Revoke</button></div>)}
      <div className="row" style={{ marginTop: 6 }}><input placeholder="label, e.g. production" value={label} onChange={e => setLabel(e.target.value)} style={{ width: 220 }} />
        <button onClick={add}>+ New key</button></div>
    </div>
  )
}

// Connections are the other direction again: not a key we hold, but an authorization the user granted us against
// their own account somewhere else - with a consent screen they saw and can revoke from their side.
function Connections() {
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState('')
  const [probe, setProbe] = useState({})
  const load = () => api.get('/connections').then(setData).catch(() => {})
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t) }, [])
  if (!data) return null
  const connect = async name => {
    setBusy(name)
    try { const r = await api.get(`/connect/${name}`); window.open(r.url, '_blank', 'noopener') }
    catch (e) { setProbe(p => ({ ...p, [name]: { ok: false, error: e.message } })) } finally { setBusy('') }
  }
  const test = async c => {
    setProbe(p => ({ ...p, [c.id]: { busy: true } }))
    const r = await api.post(`/connections/${c.id}/probe`)
    setProbe(p => ({ ...p, [c.id]: r }))
  }
  const drop = async c => { await api.del(`/connections/${c.id}`); load() }
  return (
    <div style={{ marginTop: 14 }}>
      <h3 style={{ fontSize: 14, margin: '0 0 6px' }}>Connections <span className="pill">oauth</span></h3>
      <div className="help">Accounts you have authorised {`this studio`} to read on your behalf. You approve it on their
        consent screen and can revoke it from either side. Access tokens are short-lived and refreshed automatically.</div>
      {data.connections.map(c => (
        <div className="card" key={c.id}>
          <div className="row"><b>{c.label}</b><span className="pill">{c.provider}</span>
            <span className="muted" style={{ fontSize: 12 }}>{c.expires ? `token expires ${new Date(c.expires * 1000).toLocaleTimeString()}` : 'no expiry'}</span>
            <div className="grow" /><button className="small" onClick={() => test(c)}>{probe[c.id] && probe[c.id].busy ? 'testing…' : 'Test'}</button>
            <button className="small danger" onClick={() => drop(c)}>Disconnect</button></div>
          <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>scopes: <code>{c.scopes}</code></div>
          {probe[c.id] && !probe[c.id].busy && <div className={probe[c.id].ok ? 'ok' : 'err'} style={{ fontSize: 12.5, marginTop: 6 }}>
            {probe[c.id].ok ? `✓ ${probe[c.id].status} · ${probe[c.id].records} record${probe[c.id].records === 1 ? '' : 's'} returned${probe[c.id].has_more ? ' (more available)' : ''}`
              : `✗ ${probe[c.id].error || probe[c.id].status}`}</div>}
        </div>
      ))}
      {data.providers.map(p => (
        <div className="row" key={p.name} style={{ marginTop: 6 }}>
          <button disabled={!p.configured || busy === p.name} onClick={() => connect(p.name)}>{busy === p.name ? 'opening…' : `Connect ${p.label}`}</button>
          {!p.configured && <span className="muted" style={{ fontSize: 12 }}>not configured on this server</span>}
          {probe[p.name] && !probe[p.name].ok && <span className="err" style={{ fontSize: 12 }}>{probe[p.name].error}</span>}
        </div>
      ))}
      {data.providers.some(p => !p.configured) && <div className="help">A provider needs its client id and secret in the
        studio's .env, and this exact redirect URL registered with it: <code>{data.providers[0].redirect_uri}</code></div>}
    </div>
  )
}

const FIELDS = {
  access_key_id: ['AWS access key id', 'AKIA…'], secret_access_key: ['AWS secret access key', ''], region: ['Region', 'us-east-1'],
  api_key: ['API key', 'sk-…'], base_url: ['Base URL', 'https://host/v1'], models: ['Model ids (comma-separated)', 'llama-3.3-70b, …'],
}

export default function KeysPanel({ models, features = {}, onClose }) {
  const [data, setData] = useState(null)
  const [usage, setUsage] = useState(null)
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState({ provider: 'anthropic', label: '', config: {}, models: '' })
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [tests, setTests] = useState({})
  const load = () => api.get('/credentials').then(d => { setData(d); if (d.credentials.length === 0) setAdding(true) })
  useEffect(() => { load(); api.get('/usage').then(setUsage) }, [])
  const provider = data && data.providers[form.provider]
  const add = async () => {
    setBusy('add'); setErr('')
    try {
      const ms = form.models.split(',').map(s => s.trim()).filter(Boolean)
      const r = await api.post('/credentials', { provider: form.provider, label: form.label, config: form.config, models: ms.length ? ms : null })
      setForm({ provider: form.provider, label: '', config: {}, models: '' }); setAdding(false); await load(); test(r.id)
    } catch (ex) { setErr(ex.message) }
    setBusy('')
  }
  const test = async id => { setTests(t => ({ ...t, [id]: { busy: true } })); const r = await api.post(`/credentials/${id}/test`); setTests(t => ({ ...t, [id]: r })) }
  const del = async id => { await api.del(`/credentials/${id}`); load() }
  const setModels = async (id, text) => { await api.put(`/credentials/${id}/models`, { models: text.split(',').map(s => s.trim()) }); load() }
  const house = models.models.filter(m => m.source === 'house')
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="row"><h2 className="grow">Models &amp; keys</h2><span className="pill">{models.tier}</span><button className="small" onClick={onClose}>×</button></div>
        <div className="help">
          {models.house_keys ? <>Your tier runs on the site's keys for: <b>{house.map(m => m.label).join(', ') || 'no models yet'}</b>, with up to {models.max_concurrency} calls in flight and {models.max_q} parents per round. </>
            : <>Your tier runs only on your own credentials ({models.max_concurrency} call in flight at a time). </>}
          Models from your own endpoints are always available and are used in preference to ours. Keys are stored encrypted and never shown again in full.
        </div>

        <h3 style={{ fontSize: 14, margin: '12px 0 6px' }}>Your models</h3>
        {data && data.credentials.length === 0 && <div className="muted" style={{ marginBottom: 8 }}>You have not added any endpoints.</div>}
        {data && data.credentials.map(c => (
          <div className="card" key={c.id}>
            <div className="row"><b>{c.label}</b><span className="pill">{c.provider}</span><div className="grow" />
              <button className="small" onClick={() => test(c.id)}>{tests[c.id] && tests[c.id].busy ? 'testing…' : 'Test'}</button>
              <button className="small danger" onClick={() => del(c.id)}>Remove</button></div>
            <div className="kv" style={{ marginTop: 6 }}>
              {Object.entries(c.config).map(([k, v]) => <React.Fragment key={k}><b>{k}</b><span className="mono">{String(v)}</span></React.Fragment>)}
              <b>models</b><input defaultValue={c.models.join(', ')} onBlur={e => setModels(c.id, e.target.value)} className="mono" />
            </div>
            {tests[c.id] && !tests[c.id].busy && <div className={tests[c.id].ok ? 'ok' : 'err'} style={{ marginTop: 6, fontSize: 12.5 }}>
              {tests[c.id].ok ? `✓ ${tests[c.id].model} replied “${tests[c.id].reply}”` : `✗ ${tests[c.id].model}: ${tests[c.id].error}`}</div>}
          </div>
        ))}
        {data && models.own_keys && !adding && <button onClick={() => setAdding(true)}>+ Add an endpoint</button>}
        {data && models.own_keys && adding && <div className="card">
          <div className="row"><h3 className="grow" style={{ margin: 0 }}>Add an endpoint</h3>{data.credentials.length > 0 && <button className="small" onClick={() => setAdding(false)}>×</button>}</div>
          <label>Provider</label>
          <select value={form.provider} onChange={e => setForm({ provider: e.target.value, label: '', config: {}, models: '' })}>{Object.entries(data.providers).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}</select>
          <label>Label</label><input value={form.label} placeholder={provider && provider.label} onChange={e => setForm({ ...form, label: e.target.value })} />
          {provider && provider.fields.filter(f => f !== 'models').map(f => <div key={f}><label>{FIELDS[f][0]}</label>
            <input type={f.includes('secret') || f === 'api_key' ? 'password' : 'text'} placeholder={FIELDS[f][1]} value={form.config[f] || ''} onChange={e => setForm({ ...form, config: { ...form.config, [f]: e.target.value } })} /></div>)}
          <label>{FIELDS.models[0]} <span className="muted">— default: {(data.default_models[form.provider] || []).join(', ') || 'none, list them'}</span></label>
          <input className="mono" placeholder={FIELDS.models[1]} value={form.models} onChange={e => setForm({ ...form, models: e.target.value })} />
          {err && <div className="err">{err}</div>}
          <div className="row" style={{ marginTop: 10 }}><button className="primary" disabled={!!busy} onClick={add}>{busy ? 'adding…' : 'Add and test'}</button></div>
          {form.provider === 'bedrock' && <div className="help">An IAM user or role with <code>bedrock:InvokeModel</code> on the models you list. The site's own Bedrock access is a bearer token; yours is a key pair.</div>}
        </div>}

        {features.v0 && <ApiKeys />}
        {features.v0 && <Connections />}

        {usage && usage.totals.calls > 0 && <div style={{ marginTop: 14 }}>
          <h3 style={{ fontSize: 14, margin: '0 0 6px' }}>Usage, last {usage.days} days</h3>
          <div className="help">{usage.totals.calls} calls · {usage.totals.input_tokens.toLocaleString()} in / {usage.totals.output_tokens.toLocaleString()} out tokens · on the site's keys: ${usage.totals.house_usd.toFixed(3)}</div>
          <table><thead><tr><th>model</th><th>via</th><th>calls</th><th>cached</th><th>tokens in / out</th><th>usd</th></tr></thead><tbody>
            {usage.by_model.map((m, i) => <tr key={i}><td className="mono">{m.model}</td><td>{m.source === 'house' ? 'site keys' : m.source === 'mock' ? 'mock' : 'own endpoint'}</td><td>{m.calls}</td><td>{m.cached}</td><td>{m.input_tokens} / {m.output_tokens}</td><td>${(m.usd || 0).toFixed(4)}</td></tr>)}
          </tbody></table>
        </div>}
      </div>
    </div>
  )
}
