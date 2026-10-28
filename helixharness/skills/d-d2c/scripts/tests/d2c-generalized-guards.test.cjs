#!/usr/bin/env node
const assert = require('assert')
const fs = require('fs')
const os = require('os')
const path = require('path')
const {
  applyChromeStripToNodes,
} = require('../lib/dsl-chrome.cjs')
const {
  collectAssetCandidates,
  inferCompoundFillRule,
  pathDataToSvg,
} = require('../lib/dsl-asset-core.cjs')
const {
  computeH5LayoutOffsetY,
  generateLayoutArtifacts,
} = require('../lib/dsl-layout-core.cjs')
const {
  analyzeHostLayout,
} = require('../lib/dsl-host-layout.cjs')
const {
  computeRasterDesignCropTop,
  maskIgnoredDesignRegions,
  buildOpacityPng,
  buildBlendOverlayPng,
} = require('../lib/d2c-visual-core.cjs')
const {
  collectContainerMarks,
} = require('../d2c-vertical-check.cjs')

function layout(width, height, relativeX = 0, relativeY = 0) {
  return { width, height, relativeX, relativeY }
}

function pathNode(name, x, y, width = 6, height = 16) {
  return {
    type: 'PATH',
    name,
    layoutStyle: layout(width, height, x, y),
    path: [{ fill: 'paint:black', data: `M${x},${y}L${x + width},${y}L${x + width},${y + height}Z` }],
  }
}

function nativeShellFixture() {
  const statusPaths = [
    pathNode('a', 0, 2),
    pathNode('b', 20, 2),
    pathNode('c', 550, 8),
    pathNode('d', 575, 5),
    pathNode('e', 600, 2),
    pathNode('f', 635, 1, 30, 20),
  ]
  return [{
    type: 'FRAME',
    name: 'page',
    layoutStyle: layout(750, 1600),
    children: [
      {
        type: 'GROUP',
        name: 'group-a',
        layoutStyle: layout(750, 160),
        children: [
          {
            type: 'FRAME',
            name: 'group-b',
            layoutStyle: layout(670, 24, 40, 24),
            children: statusPaths,
          },
          {
            type: 'FRAME',
            name: 'group-c',
            layoutStyle: layout(40, 40, 24, 90),
            children: [{ ...pathNode('path-c', 0, 0, 20, 20), name: 'path-c' }],
          },
          {
            type: 'TEXT',
            name: 'page-title',
            layoutStyle: layout(70, 40, 340, 90),
            text: [{ text: 'Title' }],
          },
        ],
      },
      {
        type: 'GROUP',
        name: 'business-content',
        layoutStyle: layout(700, 200, 25, 180),
        children: [{ type: 'TEXT', name: 'content', text: [{ text: 'Content' }], layoutStyle: layout(100, 30) }],
      },
    ],
  }]
}

function testStructuralChrome() {
  const log = []
  const stripped = applyChromeStripToNodes(nativeShellFixture(), log)
  assert.deepStrictEqual(
    stripped[0].children.map((node) => node.name),
    ['business-content']
  )
  assert.strictEqual(log[0].reason, 'native_webview_shell_structural')

  const h5Header = nativeShellFixture()
  h5Header[0].children[0].children = h5Header[0].children[0].children.filter(
    (node) => node.name !== 'group-b'
  )
  const kept = applyChromeStripToNodes(h5Header, [])
  assert.strictEqual(kept[0].children[0].name, 'group-a')
}

function immersiveHostFixture() {
  const page = nativeShellFixture()[0]
  const background = {
    type: 'PATH',
    name: 'background',
    layoutStyle: layout(750, 750),
    path: [{ fill: 'paint:bg', data: 'M0,0L750,0L750,750Z' }],
  }
  const content = {
    type: 'GROUP',
    name: 'content',
    layoutStyle: layout(700, 200, 25, 180),
    children: [{ type: 'TEXT', text: [{ text: 'Content' }], layoutStyle: layout(100, 30) }],
  }
  const secondCard = {
    type: 'GROUP',
    name: 'second-card',
    layoutStyle: layout(700, 300, 25, 400),
    children: [{ type: 'TEXT', text: [{ text: 'More' }], layoutStyle: layout(100, 30) }],
  }
  const bottomBar = {
    type: 'GROUP',
    name: 'bottom-bar',
    layoutStyle: layout(750, 150, 0, 1450),
    children: [{ type: 'TEXT', text: [{ text: 'Continue' }], layoutStyle: layout(120, 40, 315, 40) }],
  }
  page.children = [background, page.children[0], content, secondCard, bottomBar]
  return [page]
}

function stackedHostFixture() {
  const page = nativeShellFixture()[0]
  page.children = [
    page.children[0],
    {
      type: 'FRAME',
      name: 'h5-content-canvas',
      clipsContent: true,
      layoutStyle: layout(750, 1440, 0, 160),
      children: [
        {
          type: 'PATH',
          name: 'content-background',
          layoutStyle: layout(750, 1440),
          path: [{ fill: 'paint:bg', data: 'M0,0L750,0L750,1440Z' }],
        },
        {
          type: 'GROUP',
          name: 'first-card',
          layoutStyle: layout(700, 200, 25, 20),
          children: [{ type: 'TEXT', text: [{ text: 'Content' }], layoutStyle: layout(100, 30) }],
        },
      ],
    },
  ]
  return [page]
}

function testHostLayoutPolicy() {
  const immersive = analyzeHostLayout(immersiveHostFixture())
  assert.strictEqual(immersive.policy, 'immersive-overlay')
  assert.strictEqual(immersive.hostChromeBottomY, 160)
  assert.strictEqual(immersive.firstBusinessY, 180)
  assert.strictEqual(immersive.firstBusinessName, 'content')
  assert.strictEqual(immersive.businessTopGap, 20)
  assert.strictEqual(immersive.layoutShiftY, 0)
  assert.strictEqual(immersive.designCropTopDsl, 0)
  assert.strictEqual(immersive.ignoredDesignRegions[0].height, 160)
  assert.ok(immersive.evidence.includes('full-width-backdrop-crosses-host-boundary'))

  const stacked = analyzeHostLayout(stackedHostFixture())
  assert.strictEqual(stacked.policy, 'stacked-host')
  assert.strictEqual(stacked.hostChromeBottomY, 160)
  assert.strictEqual(stacked.firstBusinessY, 180)
  assert.strictEqual(stacked.firstBusinessName, 'first-card')
  assert.strictEqual(stacked.businessTopGap, 20)
  assert.strictEqual(stacked.layoutShiftY, 160)
  assert.strictEqual(stacked.ignoredDesignRegions.length, 0)
  const stackedWithoutClip = stackedHostFixture()
  stackedWithoutClip[0].children[1].clipsContent = false
  assert.strictEqual(
    analyzeHostLayout(stackedWithoutClip).policy,
    'stacked-host'
  )
  const forcedImmersive = analyzeHostLayout(stackedHostFixture(), {
    override: 'immersive',
  })
  assert.strictEqual(forcedImmersive.policy, 'immersive-overlay')
  assert.strictEqual(forcedImmersive.source, 'explicit-override')
  assert.strictEqual(forcedImmersive.layoutShiftY, 0)

  const ambiguous = analyzeHostLayout(nativeShellFixture())
  assert.strictEqual(ambiguous.policy, 'immersive-overlay')
  assert.strictEqual(ambiguous.source, 'conservative-fallback')
  assert.ok(ambiguous.confidence < 0.5)

  const noShell = analyzeHostLayout([{
    type: 'FRAME',
    name: 'plain-page',
    layoutStyle: layout(750, 1600),
    children: [{ type: 'TEXT', name: 'hello', text: [{ text: 'Hello' }], layoutStyle: layout(100, 30, 20, 20) }],
  }])
  assert.strictEqual(noShell.policy, 'none')
  assert.strictEqual(noShell.layoutShiftY, 0)

  assert.strictEqual(
    computeH5LayoutOffsetY(immersiveHostFixture(), { hostLayout: immersive }),
    0
  )
  assert.strictEqual(
    computeH5LayoutOffsetY(stackedHostFixture(), { hostLayout: stacked }),
    160
  )
  assert.strictEqual(computeRasterDesignCropTop(160, 1600, 800), 80)
}

function testIgnoredDesignRegionMask() {
  const decoded = {
    width: 2,
    height: 4,
    data: Buffer.alloc(2 * 4 * 4, 255),
  }
  const masked = maskIgnoredDesignRegions(
    decoded,
    [{ x: 0, y: 0, width: 750, height: 160 }],
    { width: 750, height: 640 }
  )
  assert.strictEqual(masked.data[3], 0)
  assert.strictEqual(masked.data[(2 * 1 * 4) + 3], 255)
  assert.strictEqual(decoded.data[3], 255)

  const opacity = buildOpacityPng(Buffer.from([20, 40, 60, 0]), 1, 1, 0.3)
  assert.deepStrictEqual(Array.from(opacity.data), [255, 255, 255, 255])
  const overlay = buildBlendOverlayPng(
    { width: 1, height: 1, data: Buffer.from([20, 40, 60, 0]) },
    { width: 1, height: 1, data: Buffer.from([100, 120, 140, 255]) },
    0.3
  )
  assert.deepStrictEqual(Array.from(overlay.data), [100, 120, 140, 255])
}

function testGeneratedHostCoordinates() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-host-layout-test-'))
  try {
    for (const [name, nodes, expected] of [
      [
        'immersive',
        immersiveHostFixture(),
        { policy: 'immersive-overlay', rootHeight: 1600, childY: 180, crop: 0 },
      ],
      [
        'stacked',
        stackedHostFixture(),
        { policy: 'stacked-host', rootHeight: 1440, childY: 0, crop: 160 },
      ],
    ]) {
      const input = path.join(temp, `${name}.json`)
      const output = path.join(temp, name)
      fs.writeFileSync(input, JSON.stringify({ styles: {}, nodes }))
      generateLayoutArtifacts(input, output)
      const layoutH5 = JSON.parse(
        fs.readFileSync(path.join(output, 'layout-h5.json'), 'utf8')
      )
      const marked = JSON.parse(
        fs.readFileSync(path.join(output, 'dsl_layout_marked.json'), 'utf8')
      )
      const root = marked.nodes[0]
      const content = root.children.find((node) =>
        ['content', 'h5-content-canvas'].includes(node.name)
      )
      assert.ok(
        content,
        `${name} content missing; children=${root.children.map((node) => node.name).join(',')}`
      )
      assert.strictEqual(layoutH5.hostLayoutPolicy, expected.policy)
      assert.strictEqual(layoutH5.layoutShiftY, expected.crop)
      assert.strictEqual(layoutH5.designCropTopDsl, expected.crop)
      assert.strictEqual(root.layoutStyle.height, expected.rootHeight)
      assert.strictEqual(content.layoutStyle.relativeY, expected.childY)
    }
  } finally {
    fs.rmSync(temp, { recursive: true, force: true })
  }
}

function testCompoundPathExport() {
  const outline = {
    type: 'PATH',
    name: 'outline-control',
    opacity: 0.5,
    layoutStyle: layout(128, 56),
    path: [{
      fill: 'paint:accent',
      data: 'M0,0L128,0L128,56L0,56Z M2,2L126,2L126,54L2,54Z',
    }],
  }
  assert.strictEqual(inferCompoundFillRule(outline), 'evenodd')
  const svg = pathDataToSvg(outline, '#1677ff')
  assert.match(svg, /fill-rule="evenodd"/)
  assert.match(svg, /clip-rule="evenodd"/)
  assert.match(svg, /opacity="0.5"/)

  const disjoint = {
    ...outline,
    opacity: 1,
    path: [{
      fill: 'paint:accent',
      data: 'M0,0L10,0L10,10L0,10Z M20,0L30,0L30,10L20,10Z',
    }],
  }
  assert.strictEqual(inferCompoundFillRule(disjoint), null)

  const mask = { ...outline, mask: 'outline' }
  assert.deepStrictEqual(
    collectAssetCandidates([mask], { 'paint:accent': '#1677ff' }),
    []
  )
}

function testPageFlowNestedAnchors() {
  const nodes = [{
    type: 'FRAME',
    name: 'page',
    _layoutMark: 'L0',
    layoutStyle: layout(750, 1000),
    children: [{
      type: 'GROUP',
      name: 'card',
      _layoutMark: 'L0.1',
      layoutStyle: layout(700, 400, 25, 100),
      children: [
        { type: 'TEXT', name: 'row-a', _layoutMark: 'L0.1.1', text: [{ text: 'Row A' }], layoutStyle: layout(100, 40, 40, 210) },
        { type: 'TEXT', name: 'row-b', _layoutMark: 'L0.1.2', text: [{ text: 'Row B' }], layoutStyle: layout(100, 40, 40, 290) },
      ],
    }],
  }]
  const nested = collectContainerMarks(nodes, ['L0.1'], { rootsOnly: false })
  assert.deepStrictEqual(
    nested.map((item) => [item.layoutMark, item.absY]),
    [['L0.1', 100], ['L0.1.1', 310], ['L0.1.2', 390]]
  )
  const roots = collectContainerMarks(nodes, ['L0.1'], { rootsOnly: true })
  assert.deepStrictEqual(roots.map((item) => item.layoutMark), ['L0.1'])
}

testStructuralChrome()
testHostLayoutPolicy()
testIgnoredDesignRegionMask()
testGeneratedHostCoordinates()
testCompoundPathExport()
testPageFlowNestedAnchors()
console.log('d2c generalized guards: ok')
