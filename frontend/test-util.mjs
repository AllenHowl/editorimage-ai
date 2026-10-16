// Funzioni pure della UI, senza browser: node test-util.mjs
import assert from 'node:assert/strict'
import { addPoint, boxArr, defaultParams, findLayer, fitScale, flatLayers, hitLayer, hitLayerDeep, rectFrom, resizeFromCorner, siblingsOf, stepFor } from './src/util.js'

assert.deepEqual(rectFrom({ x: 10, y: 20 }, { x: 4, y: 30 }), { x: 4, y: 20, w: 6, h: 10 })
assert.deepEqual(boxArr({ x: 1.4, y: 2.6, w: 3, h: 4 }), [1, 3, 3, 4])

const layers = [
  { id: 'top', box: [0, 0, 10, 10] },
  { id: 'hidden', visible: false, box: [0, 0, 100, 100] },
  { id: 'bottom', box: [0, 0, 100, 100] },
]
assert.equal(hitLayer(layers, { x: 5, y: 5 }).id, 'top')
assert.equal(hitLayer(layers, { x: 50, y: 50 }).id, 'bottom')
assert.equal(hitLayer(layers, { x: 500, y: 50 }), null)

assert.ok(Math.abs(fitScale(1000, 500, 564, 1000) - 0.5) < 1e-9)
assert.equal(addPoint([[0, 0]], { x: 0.5, y: 0.5 }, 2).length, 1)
assert.equal(addPoint([[0, 0]], { x: 5, y: 0 }, 2).length, 2)

assert.deepEqual(defaultParams({ params: { a: { type: 'number', default: 2 }, c: { type: 'points', default: null } } }), { a: 2 })
assert.equal(stepFor({ type: 'int' }), 1)
assert.equal(stepFor({ type: 'number', min: -1, max: 1 }), 0.01)

// angolo in basso a destra: si allarga mantenendo le proporzioni
assert.deepEqual(resizeFromCorner([0, 0, 100, 50], 'se', { x: 100, y: 0 }), [0, 0, 200, 100])
// angolo in alto a sinistra: l'angolo opposto resta fermo
assert.deepEqual(resizeFromCorner([10, 10, 100, 50], 'nw', { x: 50, y: 0 }), [60, 35, 50, 25])

// albero dei livelli
const albero = [
  { id: 'g', type: 'group', children: [{ id: 'a', box: [0, 0, 10, 10] }, { id: 'b', box: [0, 0, 50, 50] }] },
  { id: 'h', type: 'group', visible: false, children: [{ id: 'c', box: [0, 0, 99, 99] }] },
  { id: 'z', box: [0, 0, 200, 200] },
]
assert.deepEqual(flatLayers(albero).map(l => l.id + l.depth), ['g0', 'a1', 'b1', 'h0', 'c1', 'z0'])
assert.deepEqual(flatLayers([{ ...albero[0], collapsed: true }], true).map(l => l.id), ['g'])
assert.equal(hitLayerDeep(albero, { x: 5, y: 5 }).id, 'a')
assert.equal(hitLayerDeep(albero, { x: 90, y: 90 }).id, 'z')
assert.equal(findLayer(albero, 'b').parent, 'g')
assert.deepEqual(siblingsOf(albero, 'b').map(l => l.id), ['a', 'b'])

console.log('test-util ok')
