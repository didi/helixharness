#!/usr/bin/env node
const assert = require('assert')
const crypto = require('crypto')
const fs = require('fs')
const os = require('os')
const path = require('path')
const {
  assetCandidateMatches,
  collectRenderTargets,
  compareColors,
  containsInvalidCssToken,
  evaluateRenderTargets,
  familiesEquivalent,
  inferRoundedRectRadius,
  isAxisAlignedRectPath,
  pathCanUseCss,
  partitionCssLayers,
  parseRadiusValues,
  semanticFontWeight,
  svgPathFingerprint,
} = require('../lib/d2c-render-check-core.cjs')
const {
  paintToCssValue,
  parseFontRecord,
} = require('../lib/dsl-style-core.cjs')
const { materializeAssets } = require('../lib/dsl-asset-core.cjs')
const {
  collectRenderGateViolations,
  combinePhase2Gate,
  diagnoseRenderRepairPlan,
} = require('../d2c-workflow-checkpoint.cjs')

function roundedRectNode(width, height, radius) {
  return {
    type: 'PATH',
    id: 'rect:1',
    name: 'rounded surface',
    _layoutMark: 'L0.1',
    layoutStyle: { width, height, relativeX: 0, relativeY: 0 },
    path: [{
      fill: 'paint:white',
      data: `M0,${height - radius}C0,${height - radius / 2},${radius / 2},${height},${radius},${height}L${width - radius},${height}C${width - radius / 2},${height},${width},${height - radius / 2},${width},${height - radius}L${width},${radius}C${width},${radius / 2},${width - radius / 2},0,${width - radius},0L${radius},0C${radius / 2},0,0,${radius / 2},0,${radius}L0,${height - radius}Z`,
    }],
  }
}

function testStyleNormalization() {
  assert.strictEqual(
    paintToCssValue(['linear-gradient(90deg, #fff, #000)', 'linear-gradient(0deg, transparent, #fff)']),
    'linear-gradient(90deg, #fff, #000), linear-gradient(0deg, transparent, #fff)'
  )
  assert.strictEqual(paintToCssValue(['#fff', '#000']), '#fff')
  assert.strictEqual(containsInvalidCssToken('radial-gradient(NaN% at 0 0, #fff NaN%)'), true)
  assert.strictEqual(containsInvalidCssToken('linear-gradient(90deg, #fff 0%, #000 100%)'), false)
  const partial = partitionCssLayers(
    'linear-gradient(90deg, #fff 0%, #000 100%), radial-gradient(NaN% at 0 0, #fff NaN%)'
  )
  assert.deepStrictEqual(partial.valid, ['linear-gradient(90deg, #fff 0%, #000 100%)'])
  assert.strictEqual(partial.invalid.length, 1)

  const font = parseFontRecord({
    family: 'Acme Sans',
    size: 28,
    style: '{"fontStyle":"Medium"}',
    lineHeight: 39,
    decoration: 'line-through',
  }, 39)
  assert.strictEqual(font.fontWeight, 500)
  assert.strictEqual(font.lineHeightSource, 'explicit')
  assert.strictEqual(font.textDecoration, 'line-through')
}

function testComparableValues() {
  assert.strictEqual(compareColors('#1A79FF', 'rgb(26, 121, 255)').equal, true)
  assert.strictEqual(compareColors('#1A79FF', 'rgb(40, 121, 255)').equal, false)
  assert.strictEqual(familiesEquivalent('Acme Sans', 'AcmeSans-Black, "PingFang SC"'), true)
  assert.strictEqual(familiesEquivalent('PingFang SC', 'Arial, sans-serif'), false)
  assert.strictEqual(semanticFontWeight('AcmeSans-Black, sans-serif', '400'), 900)
  assert.deepStrictEqual(parseRadiusValues('12px 16px'), [12, 16, 12, 16])
  const expectedPath = svgPathFingerprint('M0,0 L10,0 L10,10 Z')
  const inlinePath = svgPathFingerprint('M0 0L10 0L10 10Z')
  assert.strictEqual(expectedPath, inlinePath)
  assert.strictEqual(assetCandidateMatches(
    { assetId: 'vector-a', fileBase: 'shape', pathFingerprint: expectedPath },
    { url: 'data:image/svg+xml;base64,...', pathFingerprint: inlinePath }
  ), true)
  assert.strictEqual(assetCandidateMatches(
    { assetId: 'raster-a', fileBase: 'hero', contentSha256: 'abc123' },
    { url: 'data:image/png;base64,...', contentSha256: 'ABC123' }
  ), true)
}

function testRoundedRectInference() {
  assert.strictEqual(inferRoundedRectRadius(roundedRectNode(702, 247, 28)), 28)
  assert.strictEqual(inferRoundedRectRadius(roundedRectNode(702, 139, 38.4)), 38.4)
  assert.strictEqual(pathCanUseCss(roundedRectNode(702, 247, 28)), true)
  assert.strictEqual(inferRoundedRectRadius({
    type: 'PATH',
    layoutStyle: { width: 100, height: 100 },
    path: [{ data: 'M0,0L100,0L50,100Z' }],
  }), null)
  assert.strictEqual(pathCanUseCss({
    ...roundedRectNode(128, 56, 12),
    path: [{ data: `${roundedRectNode(128, 56, 12).path[0].data}M2,2L126,2L126,54L2,54Z` }],
  }), false)
  const plainRect = {
    type: 'PATH',
    layoutStyle: { width: 100, height: 40 },
    path: [{ data: 'M0,0H100V40H0Z' }],
  }
  assert.strictEqual(isAxisAlignedRectPath(plainRect), true)
  assert.strictEqual(pathCanUseCss(plainRect), true)
  const triangle = {
    type: 'PATH',
    layoutStyle: { width: 100, height: 100 },
    path: [{ data: 'M0,0L100,0L50,100Z' }],
  }
  assert.strictEqual(isAxisAlignedRectPath(triangle), false)
  assert.strictEqual(pathCanUseCss(triangle), false)
}

function testTargetCollection() {
  const textNode = {
    type: 'TEXT',
    id: 'text:1',
    name: 'amount',
    _layoutMark: 'L0.0',
    layoutStyle: { width: 100, height: 46, relativeX: 0, relativeY: 0 },
    text: [{ text: '12.34', font: 'font_1:amount' }],
    textColor: [{ start: 0, end: 5, color: 'paint_1:orange' }],
  }
  const shapeNode = roundedRectNode(200, 80, 16)
  const dsl = {
    styles: {
      'font_1:amount': { value: { family: 'Acme Sans', size: 36, style: 'Black', lineHeight: 46 } },
      'paint_1:orange': { value: ['#FF6600'] },
      'paint:white': { value: ['#FFFFFF'] },
    },
  }
  const manifest = {
    items: [{
      assetId: 'vector-one',
      kind: 'vector',
      status: 'exported-svg',
      fileBase: 'amount-icon',
      ext: '.svg',
      nodeId: 'text:1',
      refs: [{ nodeId: 'text:1' }],
    }],
  }
  const targets = collectRenderTargets(dsl, [textNode, shapeNode], manifest, null)
  assert.strictEqual(targets.length, 2)
  assert.strictEqual(targets[0].expected.textSpans[0].fontWeight, 900)
  assert.strictEqual(targets[0].expected.assets[0].assetId, 'vector-one')
  assert.strictEqual(targets[1].expected.borderRadius, 16)
  assert.strictEqual(targets[1].expected.borderRadiusSource, 'rounded-rect-path')
}

function testRenderEvaluation() {
  const targets = [{
    layoutMark: 'L0.1',
    nodeId: '1:1',
    name: 'value',
    type: 'TEXT',
    expected: {
      textSpans: [{
        text: '8000',
        fontSize: 80,
        fontWeight: 900,
        fontFamily: 'Acme Sans',
        fontStyle: 'italic',
        lineHeightPx: 102,
        lineHeightSource: 'bbox-fallback',
        letterSpacing: '-2%',
        color: '#000000',
        textDecoration: 'none',
      }],
      visualFill: null,
      stroke: null,
      effect: null,
      borderRadius: null,
      opacity: null,
      assets: [{ assetId: 'vector-value', status: 'exported-svg', fileBase: 'value-shape', ext: '.svg' }],
    },
  }]
  const measurements = [{
    layoutMark: 'L0.1',
    matched: true,
    style: { backgroundImage: 'none', opacity: '1' },
    normalizedExpected: { backgroundValid: true, shadowValid: true },
    assetCandidates: [],
    textSpans: [{
      index: 0,
      matched: true,
      fontCheck: true,
      style: {
        fontFamily: 'AcmeSans-Black, sans-serif',
        fontWeight: '900',
        fontStyle: 'normal',
        fontSize: 80,
        lineHeight: 102,
        letterSpacing: '-7px',
        color: 'rgb(0, 0, 0)',
        effectiveTextDecoration: 'none',
      },
    }],
    clipping: { clipped: true, reason: 'scrollWidth > clientWidth' },
  }]
  const result = evaluateRenderTargets(targets, measurements, {
    scaleCssToDesign: 1,
    fontFaces: [{ family: 'AcmeSans-Black', weight: 'normal', status: 'loaded' }],
  })
  const kinds = new Set(result.violations.map((item) => item.kind))
  assert.ok(kinds.has('asset-reference-mismatch'))
  assert.ok(kinds.has('font-synthetic-weight'))
  assert.ok(kinds.has('font-style-mismatch'))
  assert.ok(kinds.has('letter-spacing-mismatch'))
  assert.ok(kinds.has('text-clipped'))
}

function testHtmlFillDoesNotUseSvgDefault() {
  const targets = [{
    layoutMark: 'L0.2',
    nodeId: '1:2',
    name: 'card',
    type: 'FRAME',
    expected: {
      textSpans: [],
      visualFill: '#FFFFFF',
      stroke: null,
      effect: null,
      borderRadius: null,
      opacity: null,
      assets: [],
    },
  }]
  const result = evaluateRenderTargets(targets, [{
    layoutMark: 'L0.2',
    matched: true,
    tagName: 'DIV',
    style: {
      backgroundColor: 'rgb(255, 255, 255)',
      backgroundImage: 'none',
      svgFill: 'rgb(0, 0, 0)',
    },
    normalizedExpected: {
      backgroundValid: true,
      backgroundColor: 'rgb(255, 255, 255)',
      backgroundImage: 'none',
      shadowValid: true,
    },
    assetCandidates: [],
    textSpans: [],
    clipping: { clipped: false },
  }], { scaleCssToDesign: 1, fontFaces: [] })
  assert.strictEqual(result.hardViolationCount, 0)
}

function testPartialInvalidPaintLayerDoesNotBlockValidLayer() {
  const gradient = 'linear-gradient(90deg, rgb(255, 255, 255) 0%, rgb(0, 0, 0) 100%)'
  const actualGradient = `${gradient}, radial-gradient(70% 70% at 100% 0%, rgba(0, 0, 0, 0.2), transparent)`
  const result = evaluateRenderTargets([{
    layoutMark: 'L0.25',
    nodeId: '1:25',
    name: 'background',
    type: 'FRAME',
    expected: {
      textSpans: [],
      visualFill: gradient,
      invalidVisualLayers: ['radial-gradient(NaN% at 0 0, #fff NaN%)'],
      stroke: null,
      effect: null,
      borderRadius: null,
      opacity: null,
      assets: [],
    },
  }], [{
    layoutMark: 'L0.25',
    matched: true,
    tagName: 'DIV',
    style: { backgroundImage: actualGradient },
    normalizedExpected: {
      backgroundValid: true,
      backgroundImage: gradient,
      shadowValid: true,
    },
    assetCandidates: [],
    textSpans: [],
    clipping: { clipped: false },
  }], { scaleCssToDesign: 1, fontFaces: [] })
  assert.strictEqual(result.hardViolationCount, 0)
  assert.ok(result.warnings.some((item) => item.kind === 'dsl-style-layer-unverifiable'))
}

function testTextPaintUsesTypographyColorOnly() {
  const targets = [{
    layoutMark: 'L0.3',
    nodeId: '1:3',
    name: 'currency',
    type: 'TEXT',
    expected: {
      textSpans: [{
        text: '￥',
        fontSize: 24,
        fontWeight: 400,
        fontFamily: 'Arial',
        letterSpacing: 0,
        color: '#000000',
        textDecoration: 'none',
      }],
      visualFill: '#000000',
      stroke: null,
      effect: null,
      borderRadius: null,
      opacity: null,
      assets: [],
    },
  }]
  const result = evaluateRenderTargets(targets, [{
    layoutMark: 'L0.3',
    matched: true,
    tagName: 'SPAN',
    style: { backgroundColor: 'rgba(0, 0, 0, 0)', backgroundImage: 'none' },
    normalizedExpected: {
      backgroundValid: true,
      backgroundColor: 'rgb(0, 0, 0)',
      backgroundImage: 'none',
      shadowValid: true,
    },
    assetCandidates: [],
    textSpans: [{
      index: 0,
      matched: true,
      fontCheck: true,
      style: {
        fontFamily: 'Arial',
        fontWeight: '400',
        fontSize: 24,
        lineHeight: 24,
        letterSpacing: '0px',
        color: 'rgb(0, 0, 0)',
        effectiveTextDecoration: 'none',
      },
    }],
    clipping: { clipped: false },
  }], { scaleCssToDesign: 1, fontFaces: [] })
  assert.strictEqual(result.hardViolationCount, 0)
}

function testRadiusMismatchIsAggregated() {
  const targets = [{
    layoutMark: 'L0.4',
    nodeId: '1:4',
    name: 'card',
    type: 'PATH',
    expected: {
      textSpans: [],
      visualFill: null,
      stroke: null,
      effect: null,
      borderRadius: 28,
      borderRadiusSource: 'rounded-rect-path',
      opacity: null,
      assets: [],
    },
  }]
  const result = evaluateRenderTargets(targets, [{
    layoutMark: 'L0.4',
    matched: true,
    tagName: 'DIV',
    style: {
      backgroundImage: 'none',
      borderRadius: '12px',
      borderRadii: ['12px', '12px', '12px', '12px'],
      rectWidth: 200,
      rectHeight: 80,
    },
    normalizedExpected: { backgroundValid: true, shadowValid: true },
    assetCandidates: [],
    textSpans: [],
    clipping: { clipped: false },
  }], { scaleCssToDesign: 1, fontFaces: [] })
  assert.strictEqual(result.hardViolationCount, 1)
  assert.strictEqual(result.violations[0].kind, 'border-radius-mismatch')
  assert.strictEqual(result.violations[0].cornerDiffs.length, 4)
}

function testAssetOpacityStillParticipatesInGate() {
  const targets = [{
    layoutMark: 'L0.5',
    nodeId: '1:5',
    name: 'button-shape',
    type: 'PATH',
    expected: {
      textSpans: [],
      visualFill: '#1A79FF',
      stroke: null,
      effect: null,
      borderRadius: null,
      opacity: 0.5,
      assets: [{
        assetId: 'vector-button',
        status: 'exported-svg',
        fileBase: 'button',
        ext: '.svg',
        pathFingerprint: 'fnv1a-button',
      }],
    },
  }]
  const result = evaluateRenderTargets(targets, [{
    layoutMark: 'L0.5',
    matched: true,
    tagName: 'IMG',
    style: { backgroundImage: 'none', opacity: '0.5' },
    normalizedExpected: { backgroundValid: true, shadowValid: true },
    assetCandidates: [{
      tagName: 'img',
      url: 'button.svg',
      loadOk: true,
      pathFingerprint: 'fnv1a-button',
      svgFills: ['#1A79FF'],
      svgOpacity: 0.5,
    }],
    textSpans: [],
    clipping: { clipped: false },
  }], { scaleCssToDesign: 1, fontFaces: [] })
  const violation = result.violations.find((item) => item.kind === 'opacity-mismatch')
  assert.ok(violation)
  assert.strictEqual(violation.actual, 0.25)
}

function testTransparentAssetIsTheOnlyPaintSource() {
  const target = {
    layoutMark: 'L0.55',
    nodeId: '1:55',
    name: 'outlined-button',
    type: 'PATH',
    expected: {
      textSpans: [],
      visualFill: '#1A79FF',
      stroke: null,
      effect: null,
      borderRadius: 12,
      opacity: 0.5,
      assets: [{
        assetId: 'vector-outline',
        status: 'exported-svg',
        fileBase: 'outline',
        ext: '.svg',
        pathFingerprint: 'fnv1a-outline',
      }],
    },
  }
  const candidate = {
    tagName: 'div',
    url: 'outline.svg',
    loadOk: true,
    pathFingerprint: 'fnv1a-outline',
    svgFills: ['#1A79FF'],
    svgOpacity: 0.5,
    svgHasCutout: true,
    hasTransparentPixels: true,
    transparentPixelRatio: 0.82,
  }
  const measurement = (backgroundColor) => ({
    layoutMark: 'L0.55',
    matched: true,
    tagName: 'DIV',
    style: {
      backgroundColor,
      backgroundImage: 'url("outline.svg")',
      borderRadius: '12px',
      borderRadii: ['12px', '12px', '12px', '12px'],
      rectWidth: 128,
      rectHeight: 56,
      opacity: '1',
    },
    normalizedExpected: { backgroundValid: true, shadowValid: true },
    assetCandidates: [candidate],
    textSpans: [],
    clipping: { clipped: false },
  })

  const contaminated = evaluateRenderTargets([target], [measurement('rgb(26, 121, 255)')], {
    scaleCssToDesign: 1,
    fontFaces: [],
  })
  const contamination = contaminated.violations.find((item) => item.kind === 'asset-background-contamination')
  assert.ok(contamination)
  assert.strictEqual(contamination.actual.svgHasCutout, true)
  assert.strictEqual(contamination.actual.transparentPixelRatio, 0.82)
  assert.ok(!contaminated.violations.some((item) => item.kind === 'opacity-mismatch'))

  const assetOnly = evaluateRenderTargets([target], [measurement('rgba(0, 0, 0, 0)')], {
    scaleCssToDesign: 1,
    fontFaces: [],
  })
  assert.strictEqual(assetOnly.hardViolationCount, 0)
}

function testGateAggregation() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-render-gate-'))
  try {
    const reportPath = path.join(temp, 'render-diff.json')
    fs.writeFileSync(reportPath, JSON.stringify({
      hardViolationCount: 1,
      violations: [{
        layoutMark: 'L0.1',
        kind: 'asset-unmaterialized',
        category: 'asset',
        severity: 'hard',
        blocking: true,
        repairClass: 'source-material',
      }],
    }))
    const renderFailures = collectRenderGateViolations(reportPath)
    assert.strictEqual(renderFailures.length, 1)
    const renderGate = {
      roundNum: 2,
      maxRounds: 8,
      hasRender: true,
      failing: renderFailures.map((item) => ({ ...item, source: 'render' })),
      top: renderFailures,
    }
    renderGate.repairPlan = diagnoseRenderRepairPlan(renderGate)
    assert.strictEqual(renderGate.repairPlan.strategy, 'render-source-material-fix')

    const combined = combinePhase2Gate({
      tolerancePx: 8,
      maxRounds: 8,
      roundNum: 2,
      roundDir: temp,
      hasGeom: true,
      hasVert: true,
      failing: [],
      top: [],
      repairPlan: null,
    }, renderGate)
    assert.strictEqual(combined.failing.length, 1)
    assert.strictEqual(combined.sourceMaterialFailing.length, 1)
  } finally {
    fs.rmSync(temp, { recursive: true, force: true })
  }
}

async function testMaterializedAssetContentHash() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-asset-hash-'))
  const svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0H10V10H0Z" fill="#fff"/></svg>'
  try {
    const manifest = await materializeAssets([{
      assetId: 'vector-hash',
      kind: 'vector',
      fileBase: 'shape',
      bucket: 'icons',
      source: 'dsl-path-svg',
      ext: '.svg',
      pathHash: 'path-hash',
      svg,
      primary: { nodeId: '1:1', nodeName: 'shape', nodeType: 'PATH', layoutMark: 'L0.1' },
      refs: [],
      bbox: { w: 10, h: 10 },
    }], temp)
    assert.strictEqual(manifest[0].status, 'exported-svg')
    assert.strictEqual(
      manifest[0].contentSha256,
      crypto.createHash('sha256').update(svg).digest('hex')
    )
  } finally {
    fs.rmSync(temp, { recursive: true, force: true })
  }
}

async function main() {
  testStyleNormalization()
  testComparableValues()
  testRoundedRectInference()
  testTargetCollection()
  testRenderEvaluation()
  testHtmlFillDoesNotUseSvgDefault()
  testPartialInvalidPaintLayerDoesNotBlockValidLayer()
  testTextPaintUsesTypographyColorOnly()
  testRadiusMismatchIsAggregated()
  testAssetOpacityStillParticipatesInGate()
  testTransparentAssetIsTheOnlyPaintSource()
  testGateAggregation()
  await testMaterializedAssetContentHash()
  console.log('d2c render integrity gate tests passed')
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
