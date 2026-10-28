#!/usr/bin/env node
'use strict'

/**
 * effect 层契约测试。
 *
 * 回归目标：DSL 的 effect 层不等于 box-shadow。投影、backdrop-filter、filter、
 * text-shadow 语义不同，历史上全部被序列化成 box-shadow，导致
 *   1) 生成的 CSS 非法（box-shadow: backdrop-filter: blur(12px)）；
 *   2) render 自检在浏览器拒绝该值后报 hard `dsl-style-invalid`，
 *      使用者拿到的是一个假违规。
 */

const assert = require('assert')
const {
  parseEffectDeclarations,
  resolveEffectCss,
  resolveEffectCssMap,
  resolveEffectDeclarations,
  splitEffectCss,
  EFFECT_STYLE_KEYS,
} = require('../lib/dsl-style-core.cjs')
const { nodeToCssProps } = require('../lib/dsl-layout-core.cjs')
const { evaluateRenderTargets } = require('../lib/d2c-render-check-core.cjs')
const { buildEffectPlans } = require('../d2c-render-check.cjs')

function effectMapOf(value) {
  return { effect_1: Array.isArray(value) ? value : [value] }
}

function testBackdropFilterKeepsItsProperty() {
  const map = effectMapOf('backdrop-filter: blur(12px)')
  assert.strictEqual(resolveEffectCss(map, 'effect_1'), 'backdrop-filter: blur(12px)')
  assert.deepStrictEqual(resolveEffectCssMap(map, 'effect_1'), { 'backdrop-filter': 'blur(12px)' })
  const declarations = resolveEffectDeclarations(map, 'effect_1')
  assert.deepStrictEqual(declarations.declarations, [{ property: 'backdrop-filter', value: 'blur(12px)' }])
  assert.deepStrictEqual(declarations.unsupported, [])
}

function testPlainShadowKeepsHistoricalShape() {
  const map = effectMapOf('0px 2px 8px rgba(0,0,0,0.16)')
  assert.strictEqual(resolveEffectCss(map, 'effect_1'), '0px 2px 8px rgba(0,0,0,0.16)')
  const geometry = effectMapOf({ offsetX: 0, offsetY: 2, blur: 8, spread: 0, color: 'rgba(0,0,0,0.16)' })
  assert.strictEqual(resolveEffectCss(geometry, 'effect_1'), '0px 2px 8px 0px rgba(0,0,0,0.16)')
  const inset = effectMapOf({ offsetX: 0, offsetY: 1, blur: 2, spread: 0, color: '#000', inset: true })
  assert.strictEqual(resolveEffectCss(inset, 'effect_1'), 'inset 0px 1px 2px 0px #000')
}

function testMultipleShadowLayersStillJoinAsOneProperty() {
  const map = effectMapOf([
    '0px 1px 2px #000',
    'inset 0px 0px 4px #fff',
  ])
  assert.strictEqual(resolveEffectCss(map, 'effect_1'), '0px 1px 2px #000, inset 0px 0px 4px #fff')
  assert.deepStrictEqual(resolveEffectCssMap(map, 'effect_1'), {
    'box-shadow': '0px 1px 2px #000, inset 0px 0px 4px #fff',
  })
}

function testExplicitPropertyHintOnObjectEffect() {
  assert.deepStrictEqual(
    parseEffectDeclarations({ property: 'backdrop-filter', value: 'blur(4px)' }),
    { property: 'backdrop-filter', value: 'blur(4px)' }
  )
  assert.deepStrictEqual(
    parseEffectDeclarations({ type: 'drop-shadow', blur: 'blur(4px)' }),
    { property: 'filter', value: 'blur(4px)' }
  )
  assert.deepStrictEqual(
    parseEffectDeclarations({ property: 'mask-image', value: 'url(#m)' }),
    { unsupported: true, raw: 'mask-image: url(#m)', reason: '未知 effect 类型 mask-image' }
  )
}

function testCodegenUsesTheCorrectCssProperty() {
  const map = effectMapOf('backdrop-filter: blur(12px)')
  const css = nodeToCssProps(
    { id: '1:1', type: 'FRAME', effect: 'effect_1', bbox: { x: 0, y: 0, w: 10, h: 10 } },
    {},
    {},
    map
  )
  assert.strictEqual(css['backdrop-filter'], 'blur(12px)')
  assert.strictEqual(css['box-shadow'], undefined)
}

function testSplitEffectCssSeparatesMixedDeclarations() {
  assert.deepStrictEqual(splitEffectCss('box-shadow: 0px 1px 2px #000; backdrop-filter: blur(12px)'), [
    { property: 'box-shadow', value: '0px 1px 2px #000', declared: true },
    { property: 'backdrop-filter', value: 'blur(12px)', declared: true },
  ])
  assert.deepStrictEqual(splitEffectCss('0px 1px 2px #000'), [
    { property: 'box-shadow', value: '0px 1px 2px #000', declared: false },
  ])
  const unknown = splitEffectCss('mask-image: url(#m)')
  assert.strictEqual(unknown.length, 1)
  assert.strictEqual(unknown[0].unsupported, true)
  assert.ok(EFFECT_STYLE_KEYS['backdrop-filter'])
}

function effectTarget() {
  return [{
    layoutMark: 'L1.1',
    nodeId: '1:1',
    name: 'glass-card',
    type: 'FRAME',
    expected: {
      textSpans: [],
      visualFill: null,
      stroke: null,
      effect: 'backdrop-filter: blur(12px)',
      borderRadius: null,
      opacity: null,
      assets: [],
    },
  }]
}

function effectMeasurement(extra) {
  return [Object.assign({
    layoutMark: 'L1.1',
    matched: true,
    tagName: 'DIV',
    style: { backdropFilter: 'blur(12px)', filter: 'none', boxShadow: 'none' },
    normalizedExpected: { backgroundValid: true, effectValid: true, effectList: [] },
    assetCandidates: [],
    textSpans: [],
    clipping: { clipped: false },
  }, extra)]
}

function testBackdropFilterMatchIsNotAViolation() {
  const result = evaluateRenderTargets(effectTarget(), effectMeasurement({
    normalizedExpected: {
      backgroundValid: true,
      effectValid: true,
      effectList: [{ property: 'backdrop-filter', styleKey: 'backdropFilter', value: 'blur(12px)' }],
    },
  }), { scaleCssToDesign: 1, fontFaces: [] })
  assert.strictEqual(result.hardViolationCount, 0)
}

function testBackdropFilterMismatchReportsItsOwnKind() {
  const result = evaluateRenderTargets(effectTarget(), effectMeasurement({
    style: { backdropFilter: 'blur(2px)', filter: 'none', boxShadow: 'none' },
    normalizedExpected: {
      backgroundValid: true,
      effectValid: true,
      effectList: [{ property: 'backdrop-filter', styleKey: 'backdropFilter', value: 'blur(12px)' }],
    },
  }), { scaleCssToDesign: 1, fontFaces: [] })
  const kinds = new Set(result.violations.map((item) => item.kind))
  assert.ok(kinds.has('backdrop-filter-mismatch'), `unexpected kinds: ${[...kinds].join(',')}`)
  assert.ok(!kinds.has('dsl-style-invalid'))
}

function testUnsupportedEffectIsAWarningNotAViolation() {
  const result = evaluateRenderTargets(effectTarget(), effectMeasurement({
    normalizedExpected: {
      backgroundValid: true,
      effectValid: true,
      unsupportedEffects: [{ value: 'mask-image: url(#m)', reason: '未知 effect 属性 mask-image' }],
    },
  }), { scaleCssToDesign: 1, fontFaces: [] })
  assert.strictEqual(result.hardViolationCount, 0)
  assert.ok(result.warnings.some((item) => item.kind === 'unsupported-effect'))
  assert.strictEqual(result.sourceMaterialViolationCount, 0)
}

function testBrowserRejectedEffectStaysSourceMaterial() {
  const result = evaluateRenderTargets(effectTarget(), effectMeasurement({
    normalizedExpected: {
      backgroundValid: true,
      effectValid: false,
      effectList: [],
      invalidReason: 'CSSStyleDeclaration 拒绝 backdrop-filter=blur(12px)',
    },
  }), { scaleCssToDesign: 1, fontFaces: [] })
  const invalid = result.violations.find((item) => item.kind === 'dsl-style-invalid')
  assert.ok(invalid, 'expected dsl-style-invalid')
  assert.strictEqual(invalid.repairClass, 'source-material')
}

function testBrowserProbePlanTargetsTheRightProperty() {
  const plans = buildEffectPlans([
    { layoutMark: 'L1.1', expected: { effect: 'backdrop-filter: blur(12px)' } },
    { layoutMark: 'L1.2', expected: { effect: '0px 1px 2px #000' } },
    { layoutMark: 'L1.3', expected: { effect: 'mask-image: url(#m)' } },
    { layoutMark: 'L1.4', expected: { effect: null } },
  ])
  assert.deepStrictEqual(plans['L1.1'], [{
    property: 'backdrop-filter',
    styleKey: 'backdropFilter',
    value: 'blur(12px)',
    raw: '',
    reason: '',
  }])
  assert.strictEqual(plans['L1.2'][0].styleKey, 'boxShadow')
  assert.strictEqual(plans['L1.3'][0].property, null)
  assert.strictEqual(plans['L1.3'][0].styleKey, null)
  assert.deepStrictEqual(plans['L1.4'], [])
  // 关键回归：backdrop-filter 绝不能被规划成 boxShadow 探测
  assert.ok(plans['L1.1'].every((entry) => entry.styleKey !== 'boxShadow'))
}

function main() {
  testBackdropFilterKeepsItsProperty()
  testPlainShadowKeepsHistoricalShape()
  testMultipleShadowLayersStillJoinAsOneProperty()
  testExplicitPropertyHintOnObjectEffect()
  testCodegenUsesTheCorrectCssProperty()
  testSplitEffectCssSeparatesMixedDeclarations()
  testBrowserProbePlanTargetsTheRightProperty()
  testBackdropFilterMatchIsNotAViolation()
  testBackdropFilterMismatchReportsItsOwnKind()
  testUnsupportedEffectIsAWarningNotAViolation()
  testBrowserRejectedEffectStaysSourceMaterial()
  console.log('d2c effect contract tests passed')
}

main()
