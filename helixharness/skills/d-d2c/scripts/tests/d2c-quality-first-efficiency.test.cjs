#!/usr/bin/env node
const assert = require('assert')
const fs = require('fs')
const os = require('os')
const path = require('path')
const {
  buildCaptureHashes,
  designMd5,
} = require('../d2c-capture-page.cjs')
const {
  compareRoundMaterialFingerprints,
  diagnoseCoordinateRepairPlan,
  markDepth,
  markParent,
  markRoot,
  measurementReportDigest,
  readRoundMaterialFingerprint,
  recordPhase2AutoFixBlock,
  validateMixedTextContractsFromSources,
} = require('../d2c-workflow-checkpoint.cjs')

function testCaptureHashSemantics() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-capture-hash-'))
  try {
    const design = path.join(temp, 'design.png')
    const actual = path.join(temp, 'actual.png')
    fs.writeFileSync(design, 'stable-design')
    fs.writeFileSync(actual, 'render-a')
    const first = buildCaptureHashes(design, actual)

    assert.strictEqual(first.designMd5, designMd5(design))
    assert.strictEqual(first.designMd5.length, 12)
    assert.strictEqual(first.actualMd5.length, 12)
    assert.strictEqual(first.actualSha256.length, 64)

    fs.writeFileSync(actual, 'render-b')
    const second = buildCaptureHashes(design, actual)
    assert.strictEqual(second.designMd5, first.designMd5)
    assert.notStrictEqual(second.actualSha256, first.actualSha256)
  } finally {
    fs.rmSync(temp, { recursive: true, force: true })
  }
}

function testMeasurementDigestIgnoresVolatileMetadata() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-measurement-digest-'))
  try {
    const firstPath = path.join(temp, 'first.json')
    const secondPath = path.join(temp, 'second.json')
    const measurement = {
      checkedCount: 1,
      matchedCount: 1,
      items: [{
        layoutMark: 'L0.1',
        matched: true,
        expected: { top: 10, left: 20 },
        measured: { left: 20, top: 10 },
      }],
      violations: [],
    }
    fs.writeFileSync(firstPath, JSON.stringify({ ...measurement, generatedAt: 'one', outPath: '/tmp/a' }))
    fs.writeFileSync(secondPath, JSON.stringify({ ...measurement, generatedAt: 'two', outPath: '/tmp/b' }))
    assert.strictEqual(measurementReportDigest(firstPath), measurementReportDigest(secondPath))
  } finally {
    fs.rmSync(temp, { recursive: true, force: true })
  }
}

function testEffectiveRoundAccounting() {
  const sameRoundState = { round: 3 }
  const sameRound = recordPhase2AutoFixBlock(
    sameRoundState,
    null,
    'render-no-progress',
    { currentRound: 3, consumeRound: false }
  )
  assert.strictEqual(sameRound.currentRound, 3)
  assert.strictEqual(sameRound.nextRound, 3)
  assert.strictEqual(sameRound.consumeRound, false)

  const repairState = { round: 3 }
  const nextRound = recordPhase2AutoFixBlock(repairState, null, 'coordinate-gate-failed')
  assert.strictEqual(nextRound.nextRound, 4)
  assert.strictEqual(nextRound.consumeRound, true)

  const baseFingerprint = {
    valid: true,
    actualSha256: 'actual-a',
    geometryMeasurementsSha256: 'geometry-a',
    verticalMeasurementsSha256: 'vertical-a',
    renderMeasurementsSha256: 'render-a',
  }
  assert.strictEqual(
    compareRoundMaterialFingerprints(baseFingerprint, { ...baseFingerprint }).identical,
    true
  )
  assert.strictEqual(
    compareRoundMaterialFingerprints(
      { ...baseFingerprint, actualSha256: 'actual-b' },
      baseFingerprint
    ).identical,
    false
  )

  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-round-fingerprint-'))
  try {
    const pageDir = 'page'
    const report = {
      checkedCount: 1,
      matchedCount: 1,
      items: [{ layoutMark: 'L0.1', matched: true, expected: { top: 10 }, measured: { top: 20 } }],
      violations: [{ layoutMark: 'L0.1', kind: 'top-drift', diffPx: 10 }],
    }
    for (const round of [1, 2]) {
      const roundDir = path.join(temp, pageDir, `r${round}`)
      fs.mkdirSync(roundDir, { recursive: true })
      fs.writeFileSync(path.join(roundDir, 'page-design-actual.png'), 'same-render')
      fs.writeFileSync(path.join(roundDir, 'geometry-diff.json'), JSON.stringify({ ...report, generatedAt: `g${round}` }))
      fs.writeFileSync(path.join(roundDir, 'vertical-diff.json'), JSON.stringify({ ...report, generatedAt: `v${round}` }))
      fs.writeFileSync(path.join(roundDir, 'render-diff.json'), JSON.stringify({ ...report, generatedAt: `r${round}` }))
    }
    const first = readRoundMaterialFingerprint(temp, pageDir, 1)
    const second = readRoundMaterialFingerprint(temp, pageDir, 2)
    assert.strictEqual(compareRoundMaterialFingerprints(second, first).identical, true)
    fs.writeFileSync(path.join(temp, pageDir, 'r2', 'page-design-actual.png'), 'changed-render')
    const changed = readRoundMaterialFingerprint(temp, pageDir, 2)
    assert.strictEqual(compareRoundMaterialFingerprints(changed, first).identical, false)
  } finally {
    fs.rmSync(temp, { recursive: true, force: true })
  }
}

function testMixedTextContract() {
  const manifest = {
    hasMixedText: true,
    count: 1,
    items: [{
      layoutMark: 'L0.2.0.6',
      spanCount: 2,
      spans: [
        { index: 0, text: '18.144% ' },
        { index: 1, text: '21.22%' },
      ],
    }],
  }
  const validVue = `
    <div data-d2c-mark="L0.2.0.6">
      <span data-span-index="0">18.144% </span><span data-span-index="1">21.22%</span>
    </div>`
  assert.deepStrictEqual(validateMixedTextContractsFromSources(manifest, [validVue]), [])

  const flattened = '<div data-d2c-mark="L0.2.0.6">18.144% 21.22%</div>'
  assert.strictEqual(validateMixedTextContractsFromSources(manifest, [flattened]).length, 1)

  const reversed = `
    <div data-d2c-mark="L0.2.0.6">
      <span data-span-index="1">21.22%</span><span data-span-index="0">18.144% </span>
    </div>`
  assert.strictEqual(validateMixedTextContractsFromSources(manifest, [reversed]).length, 1)
}

function testGeometryMatcherPrefersExactLayoutMark() {
  const source = fs.readFileSync(
    path.join(__dirname, '..', 'd2c-geometry-check.cjs'),
    'utf8'
  )
  assert.match(source, /markPenalty:\s*elementMark\s*===\s*t\.layoutMark\s*\?\s*0\s*:\s*1/)
  assert.match(source, /a\.markPenalty\s*-\s*b\.markPenalty/)
  assert.match(source, /exactLayoutMark\s*\?\s*best\.getBoundingClientRect\(\)/)
}

function coordinateFailure(layoutMark, diffPx, name = '') {
  return { source: 'vertical', layoutMark, kind: 'top-drift', diffPx, name }
}

function horizontalFailure(layoutMark, diffPx) {
  return { source: 'geometry', layoutMark, kind: 'right-edge-drift', diffPx, name: 'text' }
}

function testCoordinateRepairDiagnosis() {
  assert.strictEqual(markRoot('L0.2.0.6'), 'L0.2')
  assert.strictEqual(markParent('L0.2.0.6'), 'L0.2.0')
  assert.strictEqual(markDepth('L0.2.0.6'), 3)

  const isolated = diagnoseCoordinateRepairPlan({
    tolerancePx: 8,
    failing: [coordinateFailure('L0.2.0.6', 63), horizontalFailure('L0.2.0.6', 26.8)],
  })
  assert.strictEqual(isolated.strategy, 'local-anchor-position-fix')
  assert.ok(isolated.secondaryStrategies.includes('horizontal-geometry-fix'))

  const card = diagnoseCoordinateRepairPlan({
    tolerancePx: 8,
    failing: [
      coordinateFailure('L0.3.1.0', 10),
      coordinateFailure('L0.3.1.1', 52),
      coordinateFailure('L0.3.1.2', 94),
    ],
  })
  assert.strictEqual(card.strategy, 'card-internal-rhythm-rebuild')

  const pageFlow = diagnoseCoordinateRepairPlan({
    tolerancePx: 8,
    failing: [coordinateFailure('L0.1', 20), coordinateFailure('L0.2', 48)],
  })
  assert.strictEqual(pageFlow.strategy, 'page-flow-skeleton-rebuild')

  const declaredDeepPageFlow = diagnoseCoordinateRepairPlan({
    tolerancePx: 8,
    topLevelMarks: ['L0.1.2', 'L0.2.4'],
    failing: [coordinateFailure('L0.1.2', 20), coordinateFailure('L0.2.4', 48)],
  })
  assert.strictEqual(declaredDeepPageFlow.strategy, 'page-flow-skeleton-rebuild')

  const fixedCta = diagnoseCoordinateRepairPlan({
    tolerancePx: 8,
    failing: [coordinateFailure('L0.7.1.1', -752, 'continue button')],
  })
  assert.strictEqual(fixedCta.strategy, 'fixed-cta-misuse')

  const horizontalOnly = diagnoseCoordinateRepairPlan({
    tolerancePx: 8,
    failing: [horizontalFailure('L0.4.0', 40)],
  })
  assert.strictEqual(horizontalOnly.strategy, 'text-horizontal-fix')
}

testCaptureHashSemantics()
testMeasurementDigestIgnoresVolatileMetadata()
testEffectiveRoundAccounting()
testMixedTextContract()
testGeometryMatcherPrefersExactLayoutMark()
testCoordinateRepairDiagnosis()
console.log('d2c quality-first efficiency tests passed')
