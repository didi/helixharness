#!/usr/bin/env node
'use strict'

const assert = require('assert')
const fs = require('fs')
const os = require('os')
const path = require('path')

const {
  resolveMeasurementContext,
  resolveDesignScopeFrame,
  resolveDesignMeasurementFrame,
} = require('../lib/d2c-dom-measure-core.cjs')
const {
  cropRgbaRect,
  designRectToRasterRect,
} = require('../lib/d2c-visual-core.cjs')
const { resolveDesignRegion } = require('../lib/d2c-design-region-core.cjs')
const {
  buildCaptureRegionGeometry,
  parseCapturePadding,
} = require('../d2c-capture-page.cjs')
const { buildReportScopes } = require('../d2c-vertical-check.cjs')
const { resolvePageFlowReportMarks } = require('../d2c-workflow-checkpoint.cjs')

function fixtureNodes() {
  return [{
    id: 'root',
    name: 'page',
    _layoutMark: 'L0',
    layoutStyle: { relativeX: 0, relativeY: 0, width: 750, height: 1624 },
    children: [{
      id: 'banner',
      name: 'banner',
      _layoutMark: 'L0.5',
      layoutStyle: { relativeX: 24, relativeY: 1166, width: 702, height: 139 },
      children: [{
        id: 'title',
        name: 'title',
        _layoutMark: 'L0.5.1',
        layoutStyle: { relativeX: 40, relativeY: 32, width: 224, height: 39 },
      }],
    }],
  }]
}

function testScopeCoordinateFrame() {
  const nodes = fixtureNodes()
  const scope = resolveDesignScopeFrame(nodes, ['L0.5'])
  assert.deepStrictEqual(
    { x: scope.x, y: scope.y, width: scope.width, height: scope.height },
    { x: 24, y: 1166, width: 702, height: 139 },
  )
  const region = resolveDesignMeasurementFrame({
    nodes,
    scopeMarks: ['L0.5'],
    captureMode: 'region',
    fullDesignFrameWidth: 750,
  })
  assert.strictEqual(region.width, 702)
  assert.strictEqual(region.originX, 24)
  assert.strictEqual(region.originY, 1166)
  const full = resolveDesignMeasurementFrame({
    nodes,
    scopeMarks: null,
    captureMode: 'full-page',
    fullDesignFrameWidth: 750,
  })
  assert.strictEqual(full.width, 750)
  assert.throws(
    () => resolveDesignMeasurementFrame({
      nodes,
      scopeMarks: ['L0.5', 'L0.6'],
      captureMode: 'region',
      fullDesignFrameWidth: 750,
    }),
    /只允许一个/,
  )
}

function testScopeRasterCrop() {
  const rect = designRectToRasterRect(
    { x: 24, y: 1166, width: 702, height: 139 },
    { width: 750, height: 1624 },
    { width: 750, height: 1624 },
  )
  assert.deepStrictEqual(rect, {
    left: 24,
    top: 1166,
    right: 726,
    bottom: 1305,
    width: 702,
    height: 139,
  })
  const rgba = Buffer.alloc(750 * 1624 * 4, 255)
  const cropped = cropRgbaRect(rgba, 750, 1624, rect)
  assert.strictEqual(cropped.width, 702)
  assert.strictEqual(cropped.height, 139)
}

function testPageDirResolution() {
  const pageDir = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-region-scope-'))
  fs.writeFileSync(
    path.join(pageDir, 'dsl_layout_marked.json'),
    JSON.stringify({ dsl: { nodes: fixtureNodes() } }),
  )
  const region = resolveDesignRegion({
    pageDir,
    scope: 'L0.5',
    designFrameSize: { width: 750, height: 1624 },
    imageSize: { width: 750, height: 1624 },
  })
  assert.strictEqual(region.scopeFrame.layoutMark, 'L0.5')
  assert.strictEqual(region.rasterRect.width, 702)
  assert.strictEqual(region.rasterRect.height, 139)
}

function testVisualPaddingDoesNotChangeMeasurementRegion() {
  assert.deepStrictEqual(parseCapturePadding('0 45 50 45'), {
    top: 0,
    right: 45,
    bottom: 50,
    left: 45,
  })

  const region = buildCaptureRegionGeometry({
    left: 45,
    top: 0,
    right: 795,
    bottom: 1733,
    width: 750,
    height: 1733,
    scrollX: 0,
    scrollY: 0,
  }, '0 45 50 45')

  assert.deepStrictEqual(region.bboxCss, {
    left: 45,
    top: 0,
    width: 750,
    height: 1733,
    right: 795,
    bottom: 1733,
  })
  assert.deepStrictEqual(region.documentBBoxCss, {
    left: 45,
    top: 0,
    width: 750,
    height: 1733,
    right: 795,
    bottom: 1733,
  })
  assert.deepStrictEqual(region.clip, {
    x: 0,
    y: 0,
    width: 840,
    height: 1783,
  })
}

function testRegionMeasurementKeepsCaptureDesignScope() {
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-region-meta-'))
  fs.writeFileSync(path.join(outDir, 'capture-meta.json'), JSON.stringify({
    captureMode: 'region',
    designScope: 'L0',
    viewportWidthCss: 840,
    regionBBoxCss: {
      left: 45,
      top: 8,
      width: 750,
      height: 1733,
      right: 795,
      bottom: 1741,
    },
  }))

  const context = resolveMeasurementContext({}, outDir)
  assert.deepStrictEqual(context.designScopeMarks, ['L0'])
  assert.strictEqual(context.effectiveCssWidth, 750)
}

function testPageFlowReportSeparatesMaterialAndTargetScopes() {
  const scopes = buildReportScopes({
    captureMode: 'region',
    designScopeMarks: ['L0'],
    targetScopeMarks: ['L0.0', 'L0.1'],
    componentId: null,
    pageFlow: true,
    targetScopeSource: 'page-flow-plan.json(items)',
  })
  assert.deepStrictEqual(scopes, {
    scope: {
      component: null,
      pageFlow: true,
      source: 'capture-meta.json.designScope',
      layoutMarks: ['L0'],
    },
    targetScope: {
      source: 'page-flow-plan.json(items)',
      layoutMarks: ['L0.0', 'L0.1'],
    },
  })
  assert.deepStrictEqual(resolvePageFlowReportMarks(scopes), ['L0.0', 'L0.1'])
}

testScopeCoordinateFrame()
testScopeRasterCrop()
testPageDirResolution()
testVisualPaddingDoesNotChangeMeasurementRegion()
testRegionMeasurementKeepsCaptureDesignScope()
testPageFlowReportSeparatesMaterialAndTargetScopes()
console.log('d2c region scope tests: passed')
