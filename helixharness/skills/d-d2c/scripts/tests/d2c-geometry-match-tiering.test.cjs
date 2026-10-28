#!/usr/bin/env node
'use strict'

/**
 * 几何匹配分级测试。
 *
 * 回归目标：启发式回退匹配不能产出几何违规。
 * 历史行为：--fallback-strategy 默认 conservative（css / text-fuzzy 层常开），
 * 猜中的元素照样跑宽度/左右缘判定 → 使用者拿到的是一串假漂移；
 * 同时重复文案在「父容器」和「TEXT 节点」之间没有优先级，父容器的继承字体
 * 会被当成文本契约。
 */

const assert = require('assert')
const fs = require('fs')
const path = require('path')
const {
  classifyMatchConfidence,
  evaluateGeometryTargets,
} = require('../lib/d2c-geometry-check-core.cjs')
const { evaluateRenderTargets } = require('../lib/d2c-render-check-core.cjs')

function target(overrides) {
  return Object.assign({
    layoutMark: 'L0.1',
    text: '立即借款',
    w: 100,
    absX: 20,
    fontSize: 28,
  }, overrides)
}

function measured(matchType, overrides) {
  return [Object.assign({
    layoutMark: 'L0.1',
    matched: true,
    matchType,
    rect: { left: 20, width: 100, height: 30 },
    whiteSpace: 'nowrap',
    fontSizeCss: 28,
  }, overrides)]
}

function evaluate(matchType, measurementOverrides, targetOverrides) {
  return evaluateGeometryTargets(
    [target(targetOverrides)],
    { results: measured(matchType, measurementOverrides), effectiveCssWidth: 375 },
    { designFrameWidth: 375, tolPx: 4, tolPct: 2 }
  )
}

function testConfidenceClassification() {
  assert.strictEqual(classifyMatchConfidence(undefined), 'exact')
  assert.strictEqual(classifyMatchConfidence('exact'), 'exact')
  assert.strictEqual(classifyMatchConfidence('contains'), 'contains')
  for (const type of ['css-fallback', 'text-fuzzy', 'position-heuristic']) {
    assert.strictEqual(classifyMatchConfidence(type), 'guess', type)
  }
}

function testExactMatchStillProducesGeometryViolations() {
  const result = evaluate('exact', { rect: { left: 20, width: 200, height: 30 } })
  const kinds = new Set(result.violations.map((v) => v.kind))
  assert.ok(kinds.has('width-mismatch'), `expected width-mismatch, got ${[...kinds].join(',')}`)
  assert.strictEqual(result.matchedCount, 1)
}

function testExactMatchCleanRunHasNoViolations() {
  const result = evaluate('exact', {})
  assert.strictEqual(result.violations.length, 0)
  assert.strictEqual(result.warnings.length, 0)
  assert.strictEqual(result.items[0].matchConfidence, 'exact')
  assert.strictEqual(result.items[0].method, 'exact')
}

function testGuessMatchProducesNoGeometryViolation() {
  // 猜中的元素尺寸/位置全错，但因为是 guess，不得产出几何违规
  for (const type of ['css-fallback', 'text-fuzzy', 'position-heuristic']) {
    const result = evaluate(type, { rect: { left: 300, width: 40, height: 30 } })
    assert.strictEqual(result.violations.length, 0, `${type} must not emit violations`)
    assert.strictEqual(result.matchedCount, 1, `${type} still counts as matched`)
    assert.strictEqual(result.items[0].matchConfidence, 'guess')
  }
}

function testGuessMatchIsReportedAsLowConfidence() {
  const result = evaluate('css-fallback', { rect: { left: 300, width: 40, height: 30 } })
  assert.strictEqual(result.lowConfidenceMatches.length, 1)
  assert.strictEqual(result.lowConfidenceMatches[0].layoutMark, 'L0.1')
  assert.strictEqual(result.warnings.length, 1)
  const warning = result.warnings[0]
  assert.strictEqual(warning.kind, 'low-confidence-match')
  assert.deepStrictEqual(warning.layoutMarks, ['L0.1'])
  assert.match(warning.repairHint, /data-d2c-mark/)
  // 猜中的节点仍要能看到实测证据，供人工判断
  assert.strictEqual(result.items[0].measured.fontSizeCss, 28)
  assert.strictEqual(result.items[0].measured.wDesign, undefined)
}

function testContainsMatchStaysExistenceOnlyAndSilent() {
  const result = evaluate('contains', { rect: { left: 300, width: 40, height: 30 } })
  assert.strictEqual(result.violations.length, 0)
  assert.strictEqual(result.lowConfidenceMatches.length, 0, 'contains is not a guess')
  assert.strictEqual(result.warnings.length, 0)
  assert.strictEqual(result.items[0].matchConfidence, 'contains')
}

function testUnmatchedNodeIsAHardMissingViolation() {
  const result = evaluateGeometryTargets(
    [target({})],
    { results: [{ layoutMark: 'L0.1', matched: false }], effectiveCssWidth: 375 },
    { designFrameWidth: 375, tolPx: 4, tolPct: 2 }
  )
  assert.strictEqual(result.violations.length, 1)
  assert.strictEqual(result.violations[0].kind, 'text-missing')
  assert.strictEqual(result.matchedCount, 0)
}

function testRenderGateTreatsFallbackMatchAsWarning() {
  const targets = [{
    layoutMark: 'L0.1',
    nodeId: '1:1',
    name: 'card',
    type: 'FRAME',
    expected: {
      textSpans: [], visualFill: '#FFFFFF', stroke: null, effect: null,
      borderRadius: null, opacity: null, assets: [],
    },
  }]
  const measurement = [{
    layoutMark: 'L0.1',
    matched: true,
    fallbackMatched: true,
    tagName: 'DIV',
    // 样式完全不符，但因为是回退命中，不得产出 hard violation
    style: { backgroundColor: 'rgb(1, 2, 3)', backgroundImage: 'none', boxShadow: 'none' },
    normalizedExpected: { backgroundValid: true, backgroundColor: 'rgb(255, 255, 255)', backgroundImage: 'none', effectValid: true, effectList: [] },
    assetCandidates: [],
    textSpans: [],
    clipping: { clipped: false },
  }]
  const result = evaluateRenderTargets(targets, measurement, { scaleCssToDesign: 1, fontFaces: [] })
  assert.strictEqual(result.hardViolationCount, 0, JSON.stringify(result.violations))
  assert.ok(result.warnings.some((item) => item.kind === 'low-confidence-match'))
}

function testGeometrySourceMatchesPortedContract() {
  const source = fs.readFileSync(path.join(__dirname, '..', 'd2c-geometry-check.cjs'), 'utf8')
  // 重复文案必须优先选命中 DSL 标记的元素
  assert.match(source, /markPenalty:\s*elementMark\s*===\s*t\.layoutMark\s*\?\s*0\s*:\s*1/)
  assert.match(source, /a\.markPenalty\s*-\s*b\.markPenalty/)
  // 精确命中标记时保留元素自身盒子，否则用 Range 排除 padding/border
  assert.match(source, /exactLayoutMark\s*\?\s*best\.getBoundingClientRect\(\)/)
  // 判定已抽到纯函数核心，脚本只做浏览器测量
  assert.match(source, /evaluateGeometryTargets\(textNodes, measured/)
  // 回退默认关闭
  assert.match(source, /D2C_FALLBACK_STRATEGY\s*\|\|\s*'off'/)
}

function testFallbackDefaultsAreOffInBothGates() {
  for (const file of ['d2c-geometry-check.cjs', 'd2c-render-check.cjs']) {
    const source = fs.readFileSync(path.join(__dirname, '..', file), 'utf8')
    assert.match(source, /D2C_FALLBACK_STRATEGY\s*\|\|\s*'off'/, `${file} must default fallback off`)
    assert.ok(!/D2C_FALLBACK_STRATEGY\s*\|\|\s*'conservative'/.test(source), `${file} must not default to conservative`)
  }
}

function main() {
  testConfidenceClassification()
  testExactMatchStillProducesGeometryViolations()
  testExactMatchCleanRunHasNoViolations()
  testGuessMatchProducesNoGeometryViolation()
  testGuessMatchIsReportedAsLowConfidence()
  testContainsMatchStaysExistenceOnlyAndSilent()
  testUnmatchedNodeIsAHardMissingViolation()
  testRenderGateTreatsFallbackMatchAsWarning()
  testGeometrySourceMatchesPortedContract()
  testFallbackDefaultsAreOffInBothGates()
  console.log('d2c geometry match tiering tests passed')
}

main()
