#!/usr/bin/env node
/**
 * MasterGo DSL 噪音过滤脚本（Node 版，与 Python 版逻辑对齐）
 *
 * 用法:
 *   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/filter-dsl.cjs --input <dsl文件路径> --output <输出目录> [--host-layout auto|immersive|stacked]
 *
 * 输入格式支持:
 *   - MCP 工具结果文件: [{type: "text", text: "...json..."}]
 *   - 普通 DSL JSON: {"dsl": {...}, ...} 或 {"nodes": [...], ...}
 *
 * 输出:
 *   - dsl_visual.json        : 含 layout / fill / font 等视觉字段（本地预览/备查）
 *   - dsl_layout_marked.json : 完整 DSL + _layoutMark / _functionalRegion / _container 布局标记（格式化）
 *   - dsl_filtered.json      : 与 dsl_layout_marked 同内容，单行压缩 JSON（省体积、便于传输）
 *   - Layer.md               : 布局树（├── 全展开，() 内为原始节点数据 + 布局标记）
 *   - dom.md                 : DOM 树 + CSS 样式属性
 *   - styles.md              : 文本/容器样式规格表（fills/strokes/effects/bbox/gap/字体）
 *   - mixed-text.json        : 混排标记（hasMixedText / count / items，供 Agent 判断）
 *   - text-whitespace.json   : 文本空白保真标记（hasWhitespaceRisk；连续/前后/全角空格须 white-space: pre）
 *   - stripped-chrome.json   : 壳层剔除清单（默认开启；Layer/dom 与 visual 同源剔除）
 *   - dsl-assets.json        : 图标/位图提取清单（filter 后自动 extract-dsl-assets）
 *   - page-flow-plan.json    : 顶层页面流 / 叠层判定（背景层不占流、重叠关系、推荐实现方式）
 *   - flex-groups.json       : flex 组标记（禁止拍平子级，对齐 mixed-text.json）
 *   - flex-nesting.md        : 嵌套 flex 高风险拍平清单（代码生成前必查）
 */

/* eslint-disable no-console */
const fs = require('fs')
const path = require('path')
const { generateLayoutArtifacts } = require('./lib/dsl-layout-core.cjs')
// 结构化运行日志（非侵入、可降级）
let getLogger = () => ({ enabled: false })
try { ({ getLogger } = require('./lib/d2c-run-log.cjs')) } catch { /* 静默降级 */ }
const { extractDslAssets } = require('./lib/dsl-asset-core.cjs')
const {
  shouldDropChrome,
  applyChromeStripToNodes,
} = require('./lib/dsl-chrome.cjs')
const { analyzeHostLayout } = require('./lib/dsl-host-layout.cjs')
const {
  buildStyleMaps,
  resolvePaint,
  resolveFontRecord,
  resolveEffectCss,
  paintToCssValue,
  flexGapToCss,
  getBBox,
  resolveTextSpans,
  needsSplitTextSpans,
} = require('./lib/dsl-style-core.cjs')

function parseArgs(argv) {
  const args = {}
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i]
    if (token.startsWith('--')) {
      const key = token.slice(2)
      const value =
        argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : true
      args[key] = value
      if (value !== true) i += 1
    }
  }
  return args
}

function round1(n) {
  return Math.round(Number(n || 0) * 10) / 10
}

function resolveFont(fontMap, ref, layoutHeight) {
  return resolveFontRecord(fontMap, ref, layoutHeight)
}

function buildStyleMapsFromStyles(styles) {
  return buildStyleMaps(styles)
}

function pruneEmptyBranches(node) {
  if (!node) return null
  const children = node.children || []
  if (children.length) {
    node.children = children.map(pruneEmptyBranches).filter(Boolean)
    if (!node.children.length) delete node.children
  }
  const hasContent =
    node.content ||
    node.pathFills ||
    node.componentInfo ||
    (node.children && node.children.length)
  if (!hasContent && node.type === 'FRAME' && /^容器 52$/.test(node.name)) {
    return null
  }
  return node
}

function filterNode(node, paintMap, fontMap, effectMap, opts = {}) {
  if (node.mask) return null

  const parentNames = opts.parentNames || []
  if (opts.stripChrome && shouldDropChrome(node, parentNames, opts.stripLog)) {
    return null
  }

  const ntype = node.type || ''
  const layout = getBBox(node)

  const result = {
    type: ntype,
    name: node.name || '',
    layout: {
      x: layout.x,
      y: layout.y,
      w: layout.w,
      h: layout.h,
    },
  }

  const flexInfo = node.flexContainerInfo
  if (flexInfo && Object.keys(flexInfo).length) {
    result.flex = flexInfo.flexDirection || 'row'
    const gap = flexGapToCss(flexInfo)
    if (gap) result.itemSpacing = gap
  }

  const fill = node.fill
  if (fill) {
    const c = paintToCssValue(resolvePaint(paintMap, fill))
    if (c) result.fill = c
  }

  const br = node.borderRadius
  if (br) result.borderRadius = br

  const sc = node.strokeColor
  if (sc) {
    const c = paintToCssValue(resolvePaint(paintMap, sc))
    const sw = node.strokeWidth !== undefined ? node.strokeWidth : 1
    if (c) result.stroke = { color: c, width: sw }
  }

  const shadow = resolveEffectCss(effectMap, node.effect)
  if (shadow) result.effect = shadow

  if (ntype === 'TEXT') {
    const textSpans = resolveTextSpans(node, fontMap, paintMap)
    const textParts = textSpans.map((s) => s.text).filter(Boolean)
    if (textParts.length) result.content = textParts.join('')

    if (textSpans.length) {
      result.textSpans = textSpans
      result.isMixedText = needsSplitTextSpans(node, fontMap, paintMap)
      const fontInfo = resolveFont(fontMap, textSpans[0].fontRef, layout.h)
      if (fontInfo) {
        result.font = {
          fontSize: fontInfo.fontSize,
          fontWeight: fontInfo.fontWeight,
          lineHeightPx: fontInfo.lineHeightPx,
          letterSpacing: fontInfo.letterSpacing,
          fontFamily: fontInfo.fontFamilyRaw || fontInfo.fontFamily,
        }
      }
    }

    const colors = node.textColor || []
    if (colors.length) {
      const c0 = colors[0] && colors[0].color
      const c = paintToCssValue(resolvePaint(paintMap, c0))
      if (c) result.textColor = c
    }

    const align = node.textAlign
    if (align && align !== 'left') result.textAlign = align

    const mode = node.textMode
    if (mode) result.textMode = mode
  }

  if (ntype === 'PATH') {
    result._note = '图形/图标'
    const paths = node.path || []
    if (paths.length) {
      const simplified = []
      for (const p of paths) {
        const pf = resolvePaint(paintMap, p.fill)
        if (pf) simplified.push({ fill: pf })
      }
      if (simplified.length) result.pathFills = simplified
    }
  }

  const ci = node.componentInfo
  if (ci) result.componentInfo = ci

  const rawChildren = node.children || []
  const children = []
  const childOpts = {
    stripChrome: opts.stripChrome,
    stripLog: opts.stripLog,
    parentNames: [...parentNames, result.name],
  }
  for (const child of rawChildren) {
    // === _mergedVector 展开：将合并的矢量分解为独立 PATH ===
    if (child._mergedVector && Array.isArray(child.children) && child.children.length > 0) {
      for (const grandchild of child.children) {
        if (grandchild.type === 'PATH') {
          const childEntry = {
            type: 'PATH',
            layoutMark: grandchild.layoutMark || grandchild.id || grandchild.name || '',
            svgShortKey: grandchild.svgShortKey || null,
            fill: grandchild.fill || '',
            stroke: grandchild.stroke || '',
          }
          children.push(childEntry)
        }
      }
      continue // 跳过 merged 本身的输出
    }
    const fc = filterNode(child, paintMap, fontMap, effectMap, childOpts)
    if (fc) children.push(fc)
  }
  if (children.length) result.children = children

  return result
}

function countNodes(nodes) {
  let total = 0
  for (const n of nodes) {
    total += 1
    total += countNodes(n.children || [])
  }
  return total
}

function assertNonEmptyFilteredDsl({ nodeCount, layoutNodeCount, inputPath, outputDir }) {
  if (nodeCount > 0 && layoutNodeCount > 0) return
  const message = [
    `filter-dsl produced empty DSL artifacts (visualNodes=${nodeCount}, layoutNodes=${layoutNodeCount}).`,
    'This usually means dsl_full.json is section metadata, an MCP fallback stub, or an invalid hand-written file.',
    'Re-fetch DSL via fetch-mastergo-dsl, normalize-mastergo-dsl, or assemble-mastergo-sections; do not generate page code from empty Layer/dom/styles.',
  ].join(' ')

  try {
    const flog = getLogger(outputDir)
    if (flog.enabled) {
      flog.error(message, {
        script: 'filter-dsl',
        inputPath,
        visualNodes: nodeCount,
        layoutNodes: layoutNodeCount,
      })
    }
  } catch { /* 日志失败不影响错误抛出 */ }

  const err = new Error(message)
  err.code = 'D2C_EMPTY_DSL'
  throw err
}

function countByType(nodes, counts) {
  const c = counts || {}
  for (const n of nodes) {
    const t = n.type || 'UNKNOWN'
    c[t] = (c[t] || 0) + 1
    countByType(n.children || [], c)
  }
  return c
}

function loadRaw(inputPath) {
  let raw = fs.readFileSync(inputPath, 'utf8')
  try {
    const arr = JSON.parse(raw)
    if (Array.isArray(arr) && arr.length && arr[0].text !== undefined) {
      raw = arr[0].text
    }
  } catch (_) {
    /* 保持原字符串 */
  }
  return raw
}

function nodeLayout(node) {
  const layout = (node && (node.layoutStyle || node.layout)) || {}
  return {
    x: round1(layout.relativeX != null ? layout.relativeX : layout.x),
    y: round1(layout.relativeY != null ? layout.relativeY : layout.y),
    width: round1(layout.width != null ? layout.width : layout.w),
    height: round1(layout.height != null ? layout.height : layout.h),
  }
}

function markOf(node) {
  return node && (node._layoutMark || node.layoutMark || node.mark || null)
}

function countDescendants(node, pred) {
  let count = pred(node) ? 1 : 0
  for (const child of (node && node.children) || []) count += countDescendants(child, pred)
  return count
}

function isTextNode(node) {
  return !!node && node.type === 'TEXT'
}

function isVisualNode(node) {
  if (!node || node.type === 'TEXT') return false
  if (node.fill || node.fills || node.strokeColor || node.stroke || node.effect || node.path || node.opacity != null) return true
  return /^(PATH|LAYER|SVG_|RECTANGLE|ELLIPSE|VECTOR)/i.test(String(node.type || ''))
}

function overlap1d(aStart, aEnd, bStart, bEnd) {
  return Math.max(0, Math.min(aEnd, bEnd) - Math.max(aStart, bStart))
}

function topLevelChildren(markedDsl) {
  const nodes = (markedDsl && markedDsl.nodes) || []
  if (nodes.length === 1 && Array.isArray(nodes[0].children) && nodes[0].children.length) {
    return {
      root: nodes[0],
      children: nodes[0].children,
    }
  }
  return { root: nodes[0] || null, children: nodes }
}

function buildPageFlowPlan(markedDsl) {
  const { root, children } = topLevelChildren(markedDsl)
  const rootLayout = nodeLayout(root || {})
  const rootWidth = Number(rootLayout.width) || 0
  const rootHeight = Number(rootLayout.height) || 0
  const items = children
    .map((node, index) => {
      const layout = nodeLayout(node)
      const width = Number(layout.width) || 0
      const height = Number(layout.height) || 0
      const x = Number(layout.x) || 0
      const y = Number(layout.y) || 0
      const textNodeCount = countDescendants(node, isTextNode)
      const visualNodeCount = countDescendants(node, isVisualNode)
      return {
        index,
        layoutMark: markOf(node),
        nodeId: node.id || null,
        name: node.name || '',
        type: node.type || '',
        x,
        y,
        width,
        height,
        bottom: round1(y + height),
        textNodeCount,
        visualNodeCount,
        hasText: textNodeCount > 0,
      }
    })
    .filter((item) => item.layoutMark)

  const sorted = [...items].sort((a, b) => (a.y - b.y) || (a.x - b.x) || (a.index - b.index))
  const overlapPairs = []

  for (const item of items) {
    const overlaps = []
    for (const other of items) {
      if (other === item) continue
      const ox = overlap1d(item.x, item.x + item.width, other.x, other.x + other.width)
      const oy = overlap1d(item.y, item.bottom, other.y, other.bottom)
      if (ox > 1 && oy > 1) {
        overlaps.push({
          layoutMark: other.layoutMark,
          name: other.name,
          overlapX: round1(ox),
          overlapY: round1(oy),
          hasText: other.hasText,
        })
        if (item.index < other.index) {
          overlapPairs.push({
            a: item.layoutMark,
            b: other.layoutMark,
            overlapX: round1(ox),
            overlapY: round1(oy),
          })
        }
      }
    }
    item.overlapsWith = overlaps.sort((a, b) => b.overlapY - a.overlapY)
  }

  for (let i = 0; i < sorted.length; i += 1) {
    const item = sorted[i]
    const next = sorted[i + 1]
    if (next) {
      item.nextLayoutMark = next.layoutMark
      item.nextGapY = round1(next.y - item.bottom)
    }
    const largeBackdrop =
      item.textNodeCount === 0 &&
      item.visualNodeCount > 0 &&
      item.y <= 8 &&
      (item.height >= Math.max(240, rootHeight * 0.25) || (rootWidth && item.width >= rootWidth * 0.9 && item.height >= 120))
    const overlapsTextContent = item.overlapsWith.some((o) => o.hasText && o.overlapY >= 20)
    if (largeBackdrop && overlapsTextContent) {
      item.role = 'background-layer'
      item.implementation = 'absolute-background-layer'
      item.reason = 'visual-only top-level layer overlaps text/content siblings; it must not occupy normal document flow'
    } else {
      item.role = item.overlapsWith.length ? 'overlap-content-or-decoration' : 'content-flow'
      item.implementation = item.overlapsWith.length ? 'preserve-overlap-with-absolute-or-negative-gap' : 'normal-flow'
    }
  }

  return {
    version: 1,
    root: root ? { layoutMark: markOf(root), name: root.name || '', width: rootWidth, height: rootHeight } : null,
    summary: {
      itemCount: items.length,
      backgroundLayerCount: items.filter((i) => i.implementation === 'absolute-background-layer').length,
      overlappingPairCount: overlapPairs.length,
      negativeGapCount: sorted.filter((i) => Number(i.nextGapY) < 0).length,
    },
    items: sorted,
    overlapPairs,
  }
}

async function run(inputPath, outputDir, runOpts = {}) {
  const raw = loadRaw(inputPath)
  const origSize = raw.length

  const obj = JSON.parse(raw)
  const dsl = obj.dsl !== undefined ? obj.dsl : obj
  const styles = dsl.styles || {}

  const stripChrome = runOpts.stripChrome !== false
  const stripLog = stripChrome ? [] : null

  const { paintMap, fontMap, effectMap } = buildStyleMapsFromStyles(styles)
  let layoutNodes = dsl.nodes || []
  const hostLayout = stripChrome
    ? analyzeHostLayout(layoutNodes, { override: runOpts.hostLayout })
    : analyzeHostLayout([], { override: 'none' })
  if (stripChrome) {
    layoutNodes = applyChromeStripToNodes(layoutNodes, stripLog)
  }
  const filteredNodes = layoutNodes
    .map((n) =>
      filterNode(n, paintMap, fontMap, effectMap, {
        stripChrome: false,
        stripLog: null,
        parentNames: [],
      })
    )
    .filter(Boolean)
    .map(pruneEmptyBranches)
    .filter(Boolean)

  const visualJson = JSON.stringify({ nodes: filteredNodes })

  fs.mkdirSync(outputDir, { recursive: true })
  const visualPath = path.join(outputDir, 'dsl_visual.json')
  fs.writeFileSync(visualPath, visualJson, 'utf8')

  const layoutResult = generateLayoutArtifacts(inputPath, outputDir, {
    stripChrome: false,
    preStrippedNodes: layoutNodes,
    stripLog,
    hostLayout,
  })
  const nodeCount = countNodes(filteredNodes)
  const layoutNodeCount = Number(layoutResult.nodeCount) || 0
  assertNonEmptyFilteredDsl({ nodeCount, layoutNodeCount, inputPath, outputDir })

  let pageFlowPlanPath = null
  try {
    const marked = JSON.parse(fs.readFileSync(layoutResult.markedPath, 'utf8'))
    const pageFlowPlan = buildPageFlowPlan(marked)
    pageFlowPlanPath = path.join(outputDir, 'page-flow-plan.json')
    fs.writeFileSync(pageFlowPlanPath, `${JSON.stringify(pageFlowPlan, null, 2)}\n`, 'utf8')
  } catch (err) {
    console.warn(`   ⚠ 页面流计划生成失败: ${err.message || err}`)
  }

  // 结构化日志：filter 产物（page 目录 = outputDir）
  try {
    const flog = getLogger(outputDir)
    if (flog.enabled) {
      flog.scriptComplete('filter-dsl', {})
      flog.artifactWrite(visualPath)
      if (pageFlowPlanPath) flog.artifactWrite(pageFlowPlanPath)
      for (const f of ['Layer.md', 'dom.md', 'styles.md', 'dsl_layout_marked.json', 'host-layout.json', 'layout-h5.json', 'complexity-plan.json']) {
        const p = path.join(outputDir, f)
        if (fs.existsSync(p)) flog.artifactWrite(p)
      }
    }
  } catch { /* 日志失败不影响主流程 */ }

  if (stripLog && stripLog.length) {
    const stripPath = path.join(outputDir, 'stripped-chrome.json')
    fs.writeFileSync(
      stripPath,
      `${JSON.stringify({ count: stripLog.length, items: stripLog }, null, 2)}\n`,
      'utf8'
    )
    console.log(`   壳层剔除  : ${stripLog.length} 个节点 → ${stripPath}`)
  }
  if (hostLayout.policy !== 'none') {
    console.log(
      `   宿主布局  : ${hostLayout.policy}（confidence=${hostLayout.confidence}, hostBottom=${hostLayout.hostChromeBottomY}, shiftY=${hostLayout.layoutShiftY}）`
    )
  }

  const typeCounts = countByType(filteredNodes, {})
  const sortedTypes = Object.entries(typeCounts).sort((a, b) => b[1] - a[1])
  const visualSize = visualJson.length
  const reduction =
    origSize > 0
      ? Math.floor((100 * (origSize - visualSize)) / origSize)
      : 0

  console.log('✅ DSL 过滤完成')
  console.log(`   原始大小  : ${origSize.toLocaleString()} 字符`)
  console.log(
    `   视觉 JSON : ${visualSize.toLocaleString()} 字符（相对原始减少 ${reduction}%）`
  )
  console.log(`   视觉输出  : ${visualPath}`)
  console.log(`   节点总数  : ${nodeCount}`)
  console.log(
    `   节点类型  : ${JSON.stringify(Object.fromEntries(sortedTypes))}`
  )
  console.log(`   Layer     : ${layoutResult.layerPath}`)
  console.log(`   宿主分析  : ${layoutResult.hostLayoutPath}`)
  console.log(`   DOM       : ${layoutResult.domPath}`)
  console.log(`   Styles    : ${layoutResult.stylesPath}（${layoutResult.styleSpecCount} 条）`)
  console.log(
    `   混排标记  : ${layoutResult.mixedTextPath}（hasMixedText=${layoutResult.mixedTextManifest.hasMixedText}, count=${layoutResult.mixedTextCount}）`
  )
  console.log(
    `   空白保真  : ${layoutResult.textWhitespacePath}（hasWhitespaceRisk=${layoutResult.textWhitespaceManifest.hasWhitespaceRisk}, count=${layoutResult.textWhitespaceCount}）${layoutResult.textWhitespaceCount > 0 ? ' ⚠️ 命中节点须 white-space: pre' : ''}`
  )
  console.log(`   布局标记  : ${layoutResult.markedPath}（${layoutResult.nodeCount} 节点, ${layoutResult.regionCount} 功能区）`)
  if (layoutResult.filteredPath) {
    const markedSaved =
      layoutResult.markedBytes > 0
        ? Math.floor(
            (100 * (layoutResult.markedBytes - layoutResult.filteredBytes)) /
              layoutResult.markedBytes
          )
        : 0
    console.log(
      `   过滤 JSON  : ${layoutResult.filteredPath}（${layoutResult.filteredBytes.toLocaleString()} 字符，相对 layout_marked 减少 ${markedSaved}%）`
    )
  }
  if (pageFlowPlanPath) {
    console.log(`   页面流计划: ${pageFlowPlanPath}`)
  }
  console.log(
    `   Flex 标记  : ${layoutResult.flexGroupsPath}（${layoutResult.flexGroupCount} 组）`
  )
  if (layoutResult.flexNestingPath) {
    console.log(
      `   Flex 嵌套  : ${layoutResult.flexNestingPath}（高风险拍平 ${layoutResult.highRiskFlattenCount || 0} 处，写代码前必读）`
    )
  }

  // 复杂度判定（前置分流：simple 走现有路径 / complex 走 d-d2c-complex-split.md 拆解路径）
  try {
    const { runComplexityPlan } = require('./d2c-complexity-plan.cjs')
    const { plan, outPath: complexityPath } = runComplexityPlan(outputDir)
    console.log(
      `   复杂度    : ${plan.verdict}${plan.reasons.length ? `（${plan.reasons.join('; ')}）` : ''} → ${complexityPath}`
    )
    if (plan.verdict === 'complex') {
      console.log(
        `   ⚠️ 复杂页：禁止整页一把梭，按 rules/d-d2c-complex-split.md 拆解为 ${plan.suggestedComponents.length} 个组件串行实现`
      )
    }
  } catch (err) {
    console.warn(`   ⚠ 复杂度判定失败: ${err.message || err}`)
  }

  let assetsResult = null
  if (runOpts.extractAssets !== false) {
    const pageDir = path.basename(outputDir)
  const assetsDir =
      runOpts.assetsDir && runOpts.assetsDir !== true
        ? path.resolve(runOpts.assetsDir)
        : undefined
    try {
      assetsResult = await extractDslAssets(inputPath, {
        pageDir,
        assetsDir,
        manifestPath: path.join(outputDir, 'dsl-assets.json'),
        workspaceRoot: runOpts.workspaceRoot || process.cwd(),
      })
      console.log(
        `   资源提取  : ${assetsResult.manifest.materialized}/${assetsResult.manifest.count} → ${assetsResult.manifestPath}`
      )
      if (assetsResult.manifest.needsMasterGoExport.length) {
        console.log(
          `   待 MG 导出: ${assetsResult.manifest.needsMasterGoExport.map((i) => i.fileBase || i.nodeId).join(', ')}`
        )
      }
    } catch (err) {
      console.warn(`   ⚠ 资源提取失败: ${err.message || err}`)
    }
  }

  return {
    visualPath,
    layerPath: layoutResult.layerPath,
    domPath: layoutResult.domPath,
    stylesPath: layoutResult.stylesPath,
    markedPath: layoutResult.markedPath,
    pageFlowPlanPath,
    filteredPath: layoutResult.filteredPath || null,
    assetsManifestPath: assetsResult?.manifestPath || null,
  }
}

function main() {
  const args = parseArgs(process.argv)
  const inputPath = args.input
  const outputDir = args.output

  if (!inputPath || !outputDir) {
    console.error(
      'Usage: node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/filter-dsl.cjs --input <dsl文件> --output <输出目录> [--host-layout auto|immersive|stacked]'
    )
    process.exit(1)
  }

  if (!fs.existsSync(inputPath)) {
    console.error(`❌ 输入文件不存在: ${inputPath}`)
    process.exit(1)
  }

  const stripChrome = args['no-strip-chrome'] !== true
  const extractAssets = args['no-extract-assets'] !== true
  const hostLayout =
    (args['host-layout'] && args['host-layout'] !== true
      ? args['host-layout']
      : null) ||
    (args['host-chrome-policy'] && args['host-chrome-policy'] !== true
      ? args['host-chrome-policy']
      : 'auto')
  const assetsDir =
    args['assets-dir'] && args['assets-dir'] !== true ? args['assets-dir'] : undefined

  run(path.resolve(inputPath), path.resolve(outputDir), {
    stripChrome,
    extractAssets,
    assetsDir,
    hostLayout,
  }).catch((err) => {
    console.error(err)
    process.exit(1)
  })
}

main()
