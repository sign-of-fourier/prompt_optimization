import React, { useEffect, useState, useCallback, useRef } from 'react'
import { api } from './api.js'
import Canvas from './Canvas.jsx'
import DataPanel from './DataPanel.jsx'
import { OptimizerPanel } from './panels.jsx'
import RunView from './RunView.jsx'
import Serving from './Serving.jsx'
import KeysPanel from './KeysPanel.jsx'
import Tutorial, { tutorialVisible } from './Tutorial.jsx'
import { NAME, SITE_URL, COMPANY_URL } from './brand.js'

const EMPTY = {
  name: 'untitled', modules: [], edges: [], entry: null, max_steps: 8, eval_model: 'us.amazon.nova-micro-v1:0',
  evaluate: { label_column: 'answer', scorers: [{ type: 'exact_match', name: '', field: null, normalize: true, pattern: null, tolerance: 0, rubric: '', judge_model: null }],
              token_count: true, objective: { accuracy: 1.0 }, pareto: {} },
  optimizer: { goal: 'accuracy', engine: 'gepa', mode: 'weighted', bo: { q: 2, pca: 4, acquisition: 'qei' }, parents_per_round: 1, children: 1, minibatch: 5, rounds: 10,
               no_improvement_rounds: 4, max_usd: 2.0, max_calls: null, reflect_model: 'us.amazon.nova-lite-v1:0', reflect_temperature: 1.0,
               feedback: 'critic', feedback_template: 'expected: {expected}; model answered: {predicted}; metrics: {metrics}', critic_model: null,
               eval_rows: null, holdout_frac: 0.2, seed: 0 },
  mutate: null, layout: {},
}

export default function App() {
  const [user, setUser] = useState(undefined)
  const [models, setModels] = useState({ models: [], mock: false, house_keys: true, own_keys: true, tier: '', max_q: 4, max_concurrency: 4 })
  const [showKeys, setShowKeys] = useState(false)
  const [features, setFeatures] = useState({ v0: false })   // PLAN.md's v0 pieces, off unless the service enables them
  const reloadModels = useCallback(() => api.get('/models').then(setModels).catch(() => {}), [])
  useEffect(() => { api.get('/auth/me').then(setUser).catch(() => setUser(null)) }, [])
  useEffect(() => { if (user) { reloadModels(); api.get('/features').then(setFeatures).catch(() => {}) } }, [user, reloadModels])
  if (user === undefined) return <div className="login muted">loading…</div>
  if (!user) return <Login onUser={setUser} />
  return <>
    <Workspace user={user} models={models} features={features} onKeys={() => setShowKeys(true)} onLogout={() => api.post('/auth/logout').then(() => setUser(null))} />
    {showKeys && <KeysPanel models={models} features={features} onClose={() => { setShowKeys(false); reloadModels() }} />}
  </>
}

function Login({ onUser }) {
  const [mode, setMode] = useState('login')
  const [email, setEmail] = useState(''); const [pw, setPw] = useState(''); const [err, setErr] = useState('')
  const submit = async e => {
    e.preventDefault(); setErr('')
    try { onUser(await api.post(mode === 'login' ? '/auth/login' : '/auth/signup', { email, password: pw })) }
    catch (ex) { setErr(ex.message) }
  }
  return (
    <div className="login card">
      <h1><span style={{ color: 'var(--accent)' }}>{NAME}</span></h1>
      <p className="muted" style={{ margin: '0 0 14px' }}>{mode === 'login' ? 'Sign in to your projects.' : 'Create an account.'}</p>
      <form onSubmit={submit}>
        <label>Email</label><input value={email} onChange={e => setEmail(e.target.value)} autoFocus />
        <label>Password</label><input type="password" value={pw} onChange={e => setPw(e.target.value)} />
        {err && <div className="err">{err}</div>}
        <div className="row" style={{ marginTop: 14 }}>
          <button className="primary" type="submit">{mode === 'login' ? 'Sign in' : 'Sign up'}</button>
          <button type="button" onClick={() => setMode(mode === 'login' ? 'signup' : 'login')}>{mode === 'login' ? 'Create account' : 'Have an account? Sign in'}</button>
        </div>
      </form>
      <p className="muted" style={{ marginTop: 18, fontSize: 12.5 }}><a href={SITE_URL}>Why compress prompts</a> · <a href={COMPANY_URL}>About Quante Carlo</a></p>
    </div>
  )
}

function Workspace({ user, models, features, onKeys, onLogout }) {
  const [projects, setProjects] = useState([])
  const [pid, setPid] = useState(null)
  const refresh = useCallback(() => api.get('/projects').then(setProjects), [])
  useEffect(() => { refresh() }, [refresh])
  const [examples, setExamples] = useState([])
  const [err, setErr] = useState('')
  const fileRef = useRef(null)
  useEffect(() => { api.get('/examples').then(setExamples).catch(() => {}) }, [])
  const create = async () => { const r = await api.post('/projects', { ...EMPTY, name: 'untitled' }); await refresh(); setPid(r.id) }
  const remove = async p => { if (window.confirm(`Delete project "${p.name}" and its datasets and runs?`)) { await api.del(`/projects/${p.id}`); refresh() } }
  const clone = async ex => { setErr(''); try { const r = await api.post(`/examples/${ex.slug}/clone`); await refresh(); setPid(r.id) } catch (e) { setErr(e.message) } }
  const importFile = async file => {
    setErr('')
    try { const b = JSON.parse(await file.text()); const r = await api.post('/projects/import', b); await refresh(); setPid(r.id) }
    catch (e) { setErr('import failed: ' + e.message) }
  }
  if (pid) return <Editor pid={pid} models={models} features={features} user={user} onBack={() => { setPid(null); refresh() }} onKeys={onKeys} onLogout={onLogout} />
  return (
    <div className="shell">
      <div className="top"><div className="brand"><span>{NAME}</span></div><a className="muted" style={{ fontSize: 12.5 }} href={SITE_URL}>Why compress</a><a className="muted" style={{ fontSize: 12.5 }} href={COMPANY_URL}>About</a>
        <div className="grow" /><KeysHint models={models} onKeys={onKeys} /><span className="muted email" title={user.email}>{user.email}</span><button className="small" onClick={onLogout}>Sign out</button></div>
      <div className="page">
        {!models.house_keys && models.models.length === 0 && <div className="card" style={{ borderColor: 'var(--accent2)' }}><b>Add an endpoint to run anything.</b> Your account runs on your own model credentials. <a href="#" onClick={e => { e.preventDefault(); onKeys() }}>Add one under Endpoints &amp; keys</a>.</div>}
        <div className="row" style={{ marginBottom: 14 }}><h2 style={{ margin: 0 }}>Projects</h2><div className="grow" />
          <input ref={fileRef} type="file" accept=".json,application/json" style={{ display: 'none' }} onChange={e => { if (e.target.files[0]) importFile(e.target.files[0]); e.target.value = '' }} />
          <button title="A project file exported from the studio: program, settings and dataset" onClick={() => fileRef.current.click()}>Import</button>
          <button className="primary" onClick={create}>New project</button></div>
        {err && <div className="err">{err}</div>}
        {projects.length === 0 && <p className="muted">No projects yet. A project is a program (one or more prompt modules), a dataset, and the runs that optimized it. Start from an example below, or from an empty canvas with the tutorial.</p>}
        {projects.map(p => (
          <div className="card row" key={p.id} style={{ cursor: 'pointer' }} onClick={() => setPid(p.id)}>
            <b>{p.name}</b><span className="muted">updated {new Date(p.updated * 1000).toLocaleString()}</span>
            <div className="grow" /><button className="small" title="Delete project" onClick={e => { e.stopPropagation(); remove(p) }}>Delete</button>
          </div>
        ))}
        {examples.length > 0 && <>
          <h3 style={{ margin: '26px 0 4px' }}>Examples</h3>
          <p className="muted" style={{ margin: '0 0 12px' }}>Ready-made projects, dataset included. Adding one copies it into your projects; edit and run it like your own.</p>
          <div className="grid2">
            {examples.map(ex => (
              <div className="card" key={ex.slug}>
                <div className="row"><b>{ex.name}</b><span className="pill">{ex.goal === 'compress' ? 'compression' : 'accuracy'}</span></div>
                <p className="muted" style={{ margin: '6px 0 10px', fontSize: 13 }}>{ex.blurb}</p>
                <div className="row"><span className="muted" style={{ fontSize: 12 }}>{ex.steps} steps · {ex.rows} rows{ex.dataset ? ` · ${ex.dataset}` : ''}</span><div className="grow" /><button className="small primary" onClick={() => clone(ex)}>Add to my projects</button></div>
              </div>
            ))}
          </div>
        </>}
      </div>
    </div>
  )
}

function Editor({ pid, models, features, user, onBack, onKeys, onLogout }) {
  const [spec, setSpec] = useState(null)
  const [datasets, setDatasets] = useState([])
  const [tab, setTab] = useState('build')
  const [showOpt, setShowOpt] = useState(false)
  const [showTut, setShowTut] = useState(false)
  const [saved, setSaved] = useState('saved')
  const timer = useRef(null)
  const load = useCallback(async () => { const p = await api.get(`/projects/${pid}`); setSpec({ ...EMPTY, ...p.spec }); setDatasets(p.datasets) }, [pid])
  useEffect(() => { load() }, [load])
  const opened = useRef(false)
  useEffect(() => { if (spec && !opened.current) { opened.current = true; if (tutorialVisible(spec)) setShowTut(true) } }, [spec])
  const update = useCallback(fn => {
    setSpec(prev => {
      const next = typeof fn === 'function' ? fn(prev) : fn
      setSaved('saving…')
      clearTimeout(timer.current)
      timer.current = setTimeout(() => api.put(`/projects/${pid}`, next).then(() => setSaved('saved')).catch(e => setSaved('save failed: ' + e.message)), 500)
      return next
    })
  }, [pid])
  if (!spec) return <div className="login muted">loading…</div>
  return (
    <div className="shell">
      <div className="top">
        <div className="brand"><a href="#" onClick={e => { e.preventDefault(); onBack() }} style={{ color: 'inherit' }}>{NAME}</a> /</div>
        <input value={spec.name} onChange={e => update({ ...spec, name: e.target.value })} style={{ width: 160 }} />
        <div className="tabs">
          {[['build', 'Build'], ['data', 'Data & validation'], ['run', 'Runs'], ...(features.v0 ? [['serve', 'Serving']] : [])].map(([k, l]) => <button key={k} data-tut={'tab-' + k} className={tab === k ? 'active' : ''} onClick={() => setTab(k)}>{l}</button>)}
        </div>
        <button data-tut="btn-optimizer" onClick={() => setShowOpt(true)}>Optimizer ⚙</button>
        <button data-tut="btn-tutorial" className={showTut ? 'primary' : ''} onClick={() => setShowTut(true)}>Tutorial</button>
        <button title="Download this project as a file: the program, its settings and the dataset. Import it on the projects page, here or in another account." onClick={async () => {
          const b = await api.get(`/projects/${pid}/bundle`)
          const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(b, null, 1)], { type: 'application/json' }))
          a.download = (spec.name || 'project').replace(/[^\w.-]+/g, '-') + '.json'; a.click(); URL.revokeObjectURL(a.href)
        }}>Export</button>
        <div className="grow" /><span className="muted" style={{ fontSize: 12 }}>{saved}{models.mock ? ' · mock mode' : ''}</span>
        <KeysHint models={models} onKeys={onKeys} /><span className="muted email" title={user.email}>{user.email}</span><button className="small" onClick={onLogout}>Sign out</button>
      </div>
      <div className="main">
        {tab === 'build' && <Canvas spec={spec} update={update} models={models.models} features={features} datasets={datasets} pid={pid} />}
        {tab === 'data' && <DataPanel pid={pid} spec={spec} update={update} datasets={datasets} reload={load} mock={models.mock} features={features} />}
        {tab === 'run' && <RunView pid={pid} spec={spec} datasets={datasets} mock={models.mock} features={features} />}
        {tab === 'serve' && <Serving pid={pid} reload={load} />}
      </div>
      {showOpt && <OptimizerPanel spec={spec} update={update} models={models.models} tier={models} onClose={() => setShowOpt(false)} />}
      {showTut && <Tutorial spec={spec} update={update} datasets={datasets} reload={load} pid={pid} tab={tab} setTab={setTab} onClose={() => setShowTut(false)} />}
    </div>
  )
}

function KeysHint({ models, onKeys }) {
  const own = models.models.filter(m => m.source !== 'house').length
  return <button className="small" data-tut="btn-keys" onClick={onKeys} title={models.house_keys ? 'Runs use the site\'s keys unless you add your own' : 'Runs use your own keys'}>
    <span className="pill" style={{ marginRight: 6 }}>{models.tier}</span>Models &amp; keys{own ? ` (${own} own)` : ''}</button>
}
