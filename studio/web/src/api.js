import { API as BASE } from './brand.js'

async function req(method, path, body, isForm) {
  const opts = { method, credentials: 'include', headers: {} }
  if (body !== undefined) {
    if (isForm) opts.body = body
    else { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body) }
  }
  const r = await fetch(BASE + path, opts)
  if (r.status === 401) throw Object.assign(new Error('not signed in'), { status: 401 })
  const text = await r.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = text }
  if (!r.ok) {
    const d = data && data.detail
    const msg = typeof d === 'string' ? d : (d && d.message) || JSON.stringify(d || data)
    throw Object.assign(new Error(msg), { status: r.status, detail: d })
  }
  return data
}

export const api = {
  get: p => req('GET', p),
  post: (p, b) => req('POST', p, b),
  put: (p, b) => req('PUT', p, b),
  del: p => req('DELETE', p),
  upload: (p, file) => { const f = new FormData(); f.append('file', file); return req('POST', p, f, true) },
}
