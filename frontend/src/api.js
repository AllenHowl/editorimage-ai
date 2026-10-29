// 与后端通信，此处不实现编辑逻辑，每项修改都按名称调用
// Store 方法（/api/op/<方法名>）。

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

// 聊天使用 NDJSON 流，每行一个事件。
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
      if (line) { try { onEvent(JSON.parse(line)) } catch { /* 跳过损坏的事件行。 */ } }
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
