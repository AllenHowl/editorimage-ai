// Monta la UI compilata in jsdom con un backend finto: niente schermo bianco,
// i pannelli principali ci sono. Va lanciato dopo `npm run build`.
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { JSDOM } from 'jsdom'

const dist = new URL('./dist/', import.meta.url)
const html = readFileSync(new URL('index.html', dist), 'utf8')
const js = readdirSync(new URL('assets/', dist)).find(f => f.endsWith('.js'))
assert.ok(js, 'build missing: run npm run build')

const progetto = {
  name: 'demo', width: 400, height: 200, background: '#ffffff', undo: 1, redo: 0,
  layers: [{ id: 'L1', name: 'Title', type: 'text', box: [10, 10, 100, 40], text: 'Hi',
    style: { font: 'sans', size: 40, color: '#000000' }, effects: [] }],
  assets: [],
}
const stato = { project: progetto, revision: 'r1', seq: 1, effects: [{ name: 'blur', kind: 'filter', group: 'detail', desc: '', params: {} }],
  looks: [{ name: 'warm', desc: 'warm' }], presets: { square: [1080, 1080] }, chat: { ok: false, providers: [] }, home: '/tmp' }

const dom = new JSDOM(html.replace(/<script[^>]*src="[^"]*"[^>]*><\/script>/, ''), {
  url: 'http://127.0.0.1:8770/', pretendToBeVisual: true, runScripts: 'outside-only',
})
const w = dom.window
w.fetch = async url => ({ ok: true, status: 200, text: async () => JSON.stringify(String(url).includes('/api/') ? stato : {}) })
w.WebSocket = class { constructor() { setTimeout(() => {}, 0) } close() {} }
w.ResizeObserver = class { observe() {} disconnect() {} }
w.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} })
const errori = []
w.addEventListener('error', e => errori.push(e.message))
w.eval(readFileSync(new URL(`assets/${js}`, dist), 'utf8'))
await new Promise(r => setTimeout(r, 300))

const testo = w.document.body.textContent
assert.deepEqual(errori, [])
assert.ok(testo.includes('Title'), 'layer list not rendered')
assert.ok(testo.includes('Assistant'), 'side tabs missing')
assert.ok(w.document.querySelector('.toolbar .tool-btn'), 'toolbar missing')
assert.ok(w.document.querySelector('.stage svg'), 'canvas missing')
console.log('smoke ok')
process.exit(0)
