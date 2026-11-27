#!/usr/bin/env node
'use strict'

const assert = require('assert')
const { evaluateGeometryTargets } = require('../lib/d2c-geometry-check-core.cjs')
const { buildTextWhitespaceManifest } = require('../lib/dsl-layout-core.cjs')

// A matching fixed-width box must not hide text lost by a template compiler.
// Inputs are raw browser textContent, not normalized matching keys.
function check(text, domText, whiteSpace = 'pre', matchType = 'exact') {
  const measurement = {
    matched: true, matchType, whiteSpace,
    rect: { left: 20, width: 240, height: 32 }, fontSizeCss: 14,
  }
  if (domText !== undefined) measurement.domText = domText
  return evaluateGeometryTargets(
    [{ layoutMark: 'L0.8', text, absX: 20, w: 240, fontSize: 14 }],
    { results: [measurement], effectiveCssWidth: 1440 },
    { designFrameWidth: 1440, tolPx: 4, tolPct: 2 }
  ).violations.map((entry) => entry.kind)
}

const cases = [
  ['compiler-condensed footer', 'Synthetic fixture  /  1440 × 900', 'Synthetic fixture / 1440 × 900', 'pre', ['whitespace-content-mismatch']],
  ['preserved consecutive spaces', 'A  /  B', 'A  /  B', 'pre', []],
  ['NBSP replacement preserves count', 'A  /  B', 'A\u00a0\u00a0/\u00a0\u00a0B', 'nowrap', []],
  ['NBSP replacement loses count', 'A  /  B', 'A\u00a0/\u00a0B', 'nowrap', ['whitespace-content-mismatch']],
  ['NBSP replacement adds count', 'A  B', 'A\u00a0\u00a0\u00a0B', 'nowrap', ['whitespace-content-mismatch']],
  ['CSS still collapses raw spaces', 'A  B', 'A  B', 'nowrap', ['whitespace-collapsed']],
  ['mixed replacement still has collapsing run', 'A  /  B', 'A\u00a0\u00a0/  B', 'normal', ['whitespace-collapsed']],
  ['fullwidth spaces do not collapse in CSS', 'A\u3000\u3000B', 'A\u3000\u3000B', 'normal', []],
  ['fullwidth must not become ASCII', 'A\u3000B', 'A B', 'pre', ['whitespace-content-mismatch']],
  ['Tab must not become spaces', 'A\tB', 'A  B', 'pre', ['whitespace-content-mismatch']],
  ['Tab preserved', 'A\tB', 'A\tB', 'pre-wrap', []],
  ['Tab collapses under pre-line', 'A\tB', 'A\tB', 'pre-line', ['whitespace-collapsed']],
  ['newline must not become space', 'A\nB', 'A B', 'pre', ['whitespace-content-mismatch']],
  ['newlines preserved under pre-line', 'A\n\nB', 'A\n\nB', 'pre-line', []],
  ['newline count must match', 'A\n\nB', 'A\nB', 'pre', ['whitespace-content-mismatch']],
  ['HTML line ending normalization', 'A\r\nB', 'A\nB', 'pre', []],
  ['newline collapses under normal', 'A\nB', 'A\nB', 'normal', ['whitespace-collapsed']],
  ['spaces adjacent to newline collapse under pre-line', 'A \n B', 'A \n B', 'pre-line', ['whitespace-collapsed']],
  ['leading ASCII space needs preserving CSS', ' A', ' A', 'normal', ['whitespace-collapsed']],
  ['trailing ASCII space loss', 'A ', 'A', 'pre', ['whitespace-content-mismatch']],
  ['leading and trailing NBSP replacements', ' A ', '\u00a0A\u00a0', 'normal', []],
  ['break-spaces preserves', ' A  B ', ' A  B ', 'break-spaces', []],
  ['risk node without raw DOM evidence', 'A  B', undefined, 'pre', ['whitespace-content-unavailable']],
  ['risk node with empty DOM text', 'A  B', '', 'pre', ['whitespace-content-mismatch']],
  ['ordinary text remains compatible without domText', 'A B', undefined, 'nowrap', []],
]

let failures = 0
for (const [name, text, domText, css, expected] of cases) {
  try { assert.deepStrictEqual(check(text, domText, css), expected, name) }
  catch (error) { failures += 1; console.error(`${name}: ${error.message}`) }
}
// Preserve the established matching confidence boundary: a guessed enclosing
// element's textContent cannot prove a risk node's exact text is wrong.
for (const type of ['contains', 'css-fallback', 'text-fuzzy', 'position-heuristic']) {
  assert.deepStrictEqual(check('A  B', 'something else', 'normal', type), [], type)
}
const manifest = buildTextWhitespaceManifest([
  { type: 'TEXT', _layoutMark: 'L0.1', text: [{ text: 'A\nB' }] },
])
try {
  assert.strictEqual(manifest.hasWhitespaceRisk, true, 'DSL newline must enter the risk manifest')
  assert.ok(manifest.items[0].patterns.includes('line-break'))
} catch (error) { failures += 1; console.error(error.message) }
assert.strictEqual(failures, 0, `${failures} whitespace fidelity cases failed`)
console.log(`d2c whitespace fidelity: ${cases.length + 5} cases passed`)
