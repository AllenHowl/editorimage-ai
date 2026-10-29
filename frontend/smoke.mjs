// 在 jsdom 中挂载编译后的界面并使用模拟后端，检查主面板正常显示，
// 避免白屏。请在 `npm run build` 后运行。
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { JSDOM } from 'jsdom'

const dist = new URL('./dist/', import.meta.url)
const html = readFileSync(new URL('index.html', dist), 'utf8')
const js = readdirSync(new URL('assets/', dist)).find(f => f.endsWith('.js'))
assert.ok(js, '缺少构建产物，请先运行 npm run build')

const progetto = {
  name: 'demo', width: 400, height: 200, background: '#ffffff', undo: 1, redo: 0,
  layers: [{ id: 'L1', name: 'Title', type: 'text', box: [10, 10, 100, 40], text: 'Hi',
    style: { font: 'sans', size: 40, color: '#000000' }, effects: [] }],
  assets: [],
}
const stato = { project: progetto, revision: 'r1', seq: 1, effects: [{ name: 'blur', kind: 'filter', group: 'detail', desc: '高斯模糊', params: {} }],
  looks: [{ name: 'warm', desc: '金色时刻的暖意' }], presets: { square: [1080, 1080] },
  blend_modes: ['normal', 'multiply'], chat: { ok: false, providers: [] }, home: '/tmp' }

const dom = new JSDOM(html.replace(/<script[^>]*src="[^"]*"[^>]*><\/script>/, ''), {
  url: 'http://127.0.0.1:8770/', pretendToBeVisual: true, runScripts: 'outside-only',
})
const w = dom.window
w.fetch = async url => ({ ok: true, status: 200, text: async () => JSON.stringify(String(url).includes('/api/') ? stato : {}) })
w.WebSocket = class { constructor() { setTimeout(() => {}, 0) } close() {} }
w.ResizeObserver = class { observe() {} disconnect() {} }
w.HTMLElement.prototype.scrollIntoView = () => {}
w.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} })
const errori = []
w.addEventListener('error', e => errori.push(e.message))
w.eval(readFileSync(new URL(`assets/${js}`, dist), 'utf8'))
await new Promise(r => setTimeout(r, 300))

const testo = w.document.body.textContent
assert.deepEqual(errori, [])
assert.ok(testo.includes('Title'), '应保留用户的原始图层名称')
assert.ok(testo.includes('助手'), '缺少中文侧边栏标签')
assert.ok(w.document.querySelector('.toolbar .tool-btn'), '缺少工具栏')
assert.ok(w.document.querySelector('.stage svg'), '缺少画布')
assert.equal(w.document.documentElement.lang, 'zh-CN')

const settle = () => new Promise(r => setTimeout(r, 50))
const clickText = async text => {
  const button = [...w.document.querySelectorAll('button')].find(b => b.textContent.trim() === text)
  assert.ok(button, `未找到按钮：${text}`)
  button.click()
  await settle()
}

// 检查中文显示与接口值分离，防止翻译破坏预设、字体和混合模式。
await clickText('新建')
assert.ok(w.document.querySelector('[role=dialog]').textContent.includes('新建文档'))
assert.equal(w.document.querySelector('option[value=square]').textContent, '正方形 — 1080×1080')
assert.equal(w.document.querySelector('.modal input').value, '未命名')
await clickText('取消')
w.document.querySelector('.layer').click()
await settle()
assert.equal(w.document.querySelector('option[value=multiply]').textContent, '正片叠底')
assert.equal(w.document.querySelector('option[value=normal]').textContent, '正常')
assert.equal(w.document.querySelector('option[value=sans]').textContent, '无衬线')
assert.equal(w.document.querySelector('option[value=blur]').textContent, '高斯模糊')
await clickText('风格')
assert.ok(w.document.querySelector('.look').textContent.includes('暖色'))
await clickText('助手')
assert.ok(w.document.querySelector('.chat-input textarea').placeholder.includes('你想如何修改'))
assert.ok(w.document.querySelector('.msgs').textContent.includes('移除背景'))
assert.deepEqual(errori, [])
console.log('界面冒烟测试通过')
process.exit(0)
