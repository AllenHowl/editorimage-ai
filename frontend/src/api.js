// Trasporto verso il backend. Nessuna logica di editing qui: ogni modifica e' un
// metodo di Store chiamato per nome (/api/op/<nome>).

export async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body !== undefined ? 'POST' : 'GET'), headers: {} }
  if (opts.body instanceof FormData) init.body = opts.body
  else if (opts.body !== undefined) {
    init.headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(opts.body)
  }
  const res = await fetch(path, init)
  const text = await res.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = text }
  if (!res.ok) {
    const msg = (data && data.detail) || `HTTP ${res.status}`
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
  }
  return data
}

export const op = (name, body = {}) => api(`/api/op/${name}`, { body })

// flusso NDJSON della chat: un evento per riga
export async function chatStream(body, onEvent, signal) {
  const res = await fetch('/api/chat', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal,
  })
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`)
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim()
      buf = buf.slice(i + 1)
      if (line) { try { onEvent(JSON.parse(line)) } catch { /* riga rotta: si salta */ } }
    }
  }
}

export function connectEvents(onEvent) {
  let ws, stop = false, timer
  const open = () => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    ws = new WebSocket(`${proto}://${location.host}/ws`)
    ws.onmessage = e => { try { onEvent(JSON.parse(e.data)) } catch { /* */ } }
    ws.onclose = () => { if (!stop) timer = setTimeout(open, 1500) }
  }
  open()
  return () => { stop = true; clearTimeout(timer); ws && ws.close() }
}
