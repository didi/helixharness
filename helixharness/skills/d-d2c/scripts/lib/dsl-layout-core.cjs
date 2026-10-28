/**
 * MasterGo DSL → Layer.md / dom.md / styles.md / dsl_layout_marked.json
 * - 默认与 filter-dsl 共用壳层剔除（见 dsl-chrome.cjs）
 * - 兄弟节点按 Y 再 X 排序；GROUP 标注容器关系；功能区域按顶层 Y 轴分区
 * - 样式重点关注 fills / strokes / effects / bbox / itemSpacing / 文本属性
 */

const fs = require('fs')
const { applyChromeStripToNodes } = require('./dsl-chrome.cjs')
const { analyzeHostLayout } = require('./dsl-host-layout.cjs')
const {
  buildStyleMaps,
  resolvePaint,
  resolveFontRecord,
  resolveEffectCss,
  resolveEffectCssMap,
  fillToCssProps,
  strokeToCssProps,
  getBBox,
  flexGapToCss,
  fontRecordToCss,
  paintToCssValue,
  resolveTextSpans,
  hasDistinctTextSpans,
  needsSplitTextSpans,
  nodeVisualSpec,
} = require('./dsl-style-core.cjs')

const Y_TOLERANCE = 8

function round1(n) {
  return Math.round(Number(n || 0) * 10) / 10
}

function loadDslObject(inputPath) {
  let raw = fs.readFileSync(inputPath, 'utf8')
  try {
    const arr = JSON.parse(raw)
    if (Array.isArray(arr) && arr.length && arr[0].text !== undefined) {
      raw = arr[0].text
    }
  } catch (_) {
    /* keep */
  }
  const obj = JSON.parse(raw)
  const dsl = obj.dsl !== undefined ? obj.dsl : obj
  return { obj, dsl }
}

function sortChildrenByPosition(children) {
  return [...(children || [])].sort((a, b) => {
    const la = getBBox(a)
    const lb = getBBox(b)
    const dy = la.y - lb.y
    if (Math.abs(dy) > Y_TOLERANCE) return dy
    return la.x - lb.x
  })
}

function getTextContent(node) {
  return (node.text || [])
    .map((t) => (t && t.text ? t.text : ''))
    .filter(Boolean)
    .join('')
}

/**
 * 检测 HTML 渲染时会被折叠/异显的空白（通用，不限定具体字符）。
 * 背景：DSL 文案里的连续空格在 white-space: nowrap/normal 下会折叠为单空格，
 * 导致文本渲染宽度小于 DSL bbox、右端逐字累积横向漂移（见 text-whitespace.json）。
 */
const WS_PATTERN_DEFS = [
  { key: 'consecutive-spaces', re: /[ ]{2,}/, label: '连续空格(≥2)' },
  { key: 'leading-whitespace', re: /^[ \t 　]/, label: '前导空白' },
  { key: 'trailing-whitespace', re: /[ \t 　]$/, label: '尾随空白' },
  { key: 'fullwidth-space', re: /　/, label: '全角空格 U+3000' },
  { key: 'nbsp', re: / /, label: '不间断空格 U+00A0' },
  { key: 'tab', re: /\t/, label: 'Tab 制表符' },
]

function detectWhitespaceRisk(text) {
  if (!text) return []
  return WS_PATTERN_DEFS.filter((p) => p.re.test(text)).map((p) => p.key)
}

/** 空白可视化（仅用于人读清单，不参与匹配） */
function visualizeWhitespace(text) {
  return String(text || '')
    .replace(/ /g, '␣')
    .replace(/ /g, '⍽')
    .replace(/　/g, '【全角空格】')
    .replace(/\t/g, '⇥')
}

/** 遍历布局树收集空白风险 TEXT 节点（供 text-whitespace.json） */
function buildTextWhitespaceManifest(markedNodes) {
  const items = []
  function walk(node) {
    if (!node) return
    if (node.type === 'TEXT') {
      const content = getTextContent(node)
      const patterns = detectWhitespaceRisk(content)
      if (patterns.length) {
        items.push({
          layoutMark: node._layoutMark || null,
          nodeId: node.id || null,
          name: node.name || '',
          text: content,
          textVisible: visualizeWhitespace(content),
          patterns,
          patternLabels: patterns.map(
            (k) => (WS_PATTERN_DEFS.find((p) => p.key === k) || {}).label || k
          ),
          suggestion:
            '单行文本用 white-space: pre（替代 nowrap）保留空白序列，或将连续空格转 &nbsp;；禁止 nowrap 直接承载该文案',
        })
      }
    }
    for (const child of node.children || []) walk(child)
  }
  for (const n of markedNodes || []) walk(n)
  return {
    hasWhitespaceRisk: items.length > 0,
    count: items.length,
    note: 'HTML 会折叠连续/前后空白：命中节点必须保留空白序列（white-space: pre 或 &nbsp;），否则文本渲染宽度小于 DSL bbox，产生右端累积横向漂移。',
    items,
  }
}

function getPageRootChildren(nodes) {
  const sorted = sortChildrenByPosition(nodes)
  if (sorted.length === 1 && (sorted[0].children || []).length) {
    return sortChildrenByPosition(sorted[0].children)
  }
  return sorted
}

function subtreeHasText(node) {
  if (!node) return false
  if (node.type === 'TEXT' && getTextContent(node).trim()) return true
  return (node.children || []).some(subtreeHasText)
}

function isTopLevelBackdrop(node, designFrameSize) {
  if (!node || !designFrameSize?.width || !designFrameSize?.height) return false
  const box = getBBox(node)
  const visual = !!(
    node.fill ||
    node.fills ||
    node.path ||
    node.strokeColor ||
    node.effect
  )
  return (
    visual &&
    !subtreeHasText(node) &&
    box.y <= 8 &&
    box.w >= designFrameSize.width * 0.85 &&
    box.h >= Math.max(120, designFrameSize.height * 0.2)
  )
}

/**
 * @deprecated `contentOffsetY` used to be inferred from the first business Y,
 * which destroys immersive coordinates and the intentional gap after a stacked
 * host. Callers must pass the raw-DSL hostLayout analysis instead.
 */
function computeH5LayoutOffsetY(pageRootChildren, opts = {}) {
  if (!pageRootChildren.length) return 0
  if (opts.hostLayout && Number.isFinite(Number(opts.hostLayout.layoutShiftY))) {
    return Math.max(0, round1(opts.hostLayout.layoutShiftY))
  }
  return 0
}

/** MasterGo 画板根节点尺寸（DSL 坐标系，非导出 PNG 像素） */
function computeDesignFrameSize(rawNodes) {
  const sorted = sortChildrenByPosition(rawNodes)
  if (!sorted.length) return null
  const bbox = getBBox(sorted[0])
  if (!bbox.w || !bbox.h) return null
  return { width: round1(bbox.w), height: round1(bbox.h) }
}

/**
 * 按 stacked-host 的宿主底边把单个顶层业务块整体上移 offset。
 * firstBusinessY 可能大于 offset；两者之差是必须保留的业务顶部留白。
 * ⚠️ 只调整该节点自身的 relativeY/y，**绝不递归子孙**：
 * MasterGo 的 relativeY 是「相对直接父级」，平移父级即等效平移整棵子树；
 * 若对子孙再减 offset 就是重复扣减，且 Math.max(0,…) 会把组件/分组内
 * 小于 offset 的真实坐标钳到 0，污染绝对定位子树（如 KV 卡）的纵向布局。
 */
function shiftTopLevelNodeY(node, offset) {
  if (!node || offset <= 0) return node
  const layout = node.layoutStyle
  if (layout) {
    if (layout.relativeY != null) {
      layout.relativeY = round1(Math.max(0, layout.relativeY - offset))
    } else if (layout.y != null) {
      layout.y = round1(Math.max(0, layout.y - offset))
    }
  }
  return node
}

function rebaseRootFrameHeight(nodes, offset) {
  if (!Array.isArray(nodes) || nodes.length !== 1 || offset <= 0) return
  const layout = nodes[0] && nodes[0].layoutStyle
  if (!layout || layout.height == null) return
  layout.height = round1(Math.max(1, Number(layout.height) - offset))
}

function loadStripLogForLayout(outputDir, stripLog) {
  if (stripLog && stripLog.length) return stripLog
  if (!outputDir) return []
  try {
    const p = `${outputDir}/stripped-chrome.json`
    if (!fs.existsSync(p)) return []
    const data = JSON.parse(fs.readFileSync(p, 'utf8'))
    return data.items || []
  } catch (_) {
    return []
  }
}

function buildExcludedNavTitleSection(stripLog) {
  const items = (stripLog || []).filter((i) =>
    /(?:nav_title_bar_remnant|native_webview_shell_structural|status_bar_structural)/.test(
      String(i.reason || '')
    )
  )
  if (!items.length) return []
  const lines = [
    '## 已剔除（勿在 H5 还原）',
    '',
    '> 以下区块**不会**出现在下方「功能区域划分 / 布局树」中；设计图通常也不含原生页面标题（如「报价结果」）。',
    '',
  ]
  for (const item of items) {
    const title = item.note || item.name || item.path
    lines.push(`- \`${item.reason}\`：**${item.name}** — ${title}`)
  }
  lines.push('')
  return lines
}

function buildH5LayoutNoteSection(hostLayout, firstRegionName) {
  const policy = hostLayout?.policy || 'none'
  const layoutShiftY = Number(hostLayout?.layoutShiftY || 0)
  if (policy === 'immersive-overlay') {
    return [
      '## H5 布局坐标',
      '',
      `> 宿主策略：**immersive-overlay**（confidence=${hostLayout.confidence}）。原生状态栏/导航控件不在 H5 还原，但 H5 是全屏沉浸式画布，必须保留顶部坐标空间。`,
      `> 宿主底部 y=${hostLayout.hostChromeBottomY}，首个业务模块原始 y=${hostLayout.firstBusinessY}，业务顶部留白=${hostLayout.businessTopGap}px；**layoutShiftY=0，禁止把业务模块上移到 y=0**。`,
      '> 叠图不得裁掉顶部；宿主控件区域由 ignoredDesignRegions 排除视觉扣分，业务模块原始 Y 仍参与 geometry/vertical 硬校验。',
      '',
    ]
  }
  if (policy === 'none') {
    return [
      '## H5 布局坐标',
      '',
      '> 未检测到宿主壳层；使用原始设计画板坐标，layoutShiftY=0。',
      '',
    ]
  }
  return [
    '## H5 布局坐标',
    '',
    `> 宿主策略：**stacked-host**（confidence=${hostLayout.confidence}）。H5 画布从原生宿主底部开始，按 **layoutShiftY=${layoutShiftY}px** 上移并裁切宿主区域。`,
    `> 首个业务模块 **${firstRegionName || '—'}** 原始 y=${hostLayout.firstBusinessY}，上移后 y=${round1(Math.max(0, Number(hostLayout.firstBusinessY || 0) - layoutShiftY))}；宿主后的 ${hostLayout.businessTopGap}px 业务留白必须保留。`,
    '> 叠图裁切见 `layout-h5.json.designCropTopRaster`；禁止使用 firstBusinessY 代替 hostChromeBottomY。',
    '',
  ]
}

function buildFunctionalRegions(rootChildren) {
  const sorted = sortChildrenByPosition(rootChildren)
  const regions = []
  let idx = 0
  for (const child of sorted) {
    const layout = getBBox(child)
    const name = String(child.name || '')
    const semantic =
      name && !/^容器\s*\d+$/.test(name) ? name : `功能区域_${idx + 1}`
    regions.push({
      id: `region_${idx}`,
      name: semantic,
      y: layout.y,
      h: layout.h,
      rootName: name,
      rootType: child.type,
    })
    idx += 1
  }
  return regions
}

function shallowNodeForParen(node) {
  const copy = JSON.parse(JSON.stringify(node))
  delete copy.children
  if (copy.path && Array.isArray(copy.path)) {
    copy.path = copy.path.map((p) => {
      if (p && p.data && p.data.length > 120) {
        return { ...p, data: `${p.data.slice(0, 120)}…` }
      }
      return p
    })
  }
  return copy
}

function inferContainerHint(node) {
  if (node.type === 'GROUP') {
    return { role: 'group-container', ref: node.name || 'GROUP' }
  }
  const flex = node.flexContainerInfo
  if (flex && Object.keys(flex).length) {
    return { role: 'flex-container', direction: flex.flexDirection || 'row' }
  }
  if (node.type === 'FRAME') {
    return { role: 'frame-container', ref: node.name || 'FRAME' }
  }
  return null
}

function domTagForType(type) {
  const map = {
    FRAME: 'div',
    GROUP: 'div',
    TEXT: 'span',
    PATH: 'div',
    INSTANCE: 'div',
    LAYER: 'div',
    SVG_ELLIPSE: 'div',
  }
  return map[type] || 'div'
}

function nodeToCssProps(node, paintMap, fontMap, effectMap) {
  const css = {}
  const layout = getBBox(node)

  css.position = 'relative'
  if (layout.w > 0) css.width = `${layout.w}px`
  if (layout.h > 0) css.height = `${layout.h}px`
  css.left = `${layout.x}px`
  css.top = `${layout.y}px`

  Object.assign(css, fillToCssProps(resolvePaint(paintMap, node.fill)))
  Object.assign(css, strokeToCssProps(node, paintMap))

  if (node.borderRadius != null && node.borderRadius !== '') {
    const br = String(node.borderRadius)
    css['border-radius'] = /px|%|rem$/i.test(br) ? br : `${br}px`
  }

  const effectCss = resolveEffectCssMap(effectMap, node.effect)
  for (const [property, value] of Object.entries(effectCss)) css[property] = value

  const flex = node.flexContainerInfo
  if (flex && Object.keys(flex).length) {
    css.display = 'flex'
    css['flex-direction'] = flex.flexDirection === 'column' ? 'column' : 'row'
    const gap = flexGapToCss(flex)
    if (gap) css.gap = gap
    if (flex.mainAxisAlignItems) css['align-items'] = flex.mainAxisAlignItems
    if (flex.crossAxisAlignItems) css['justify-content'] = flex.crossAxisAlignItems
    const padding = flex.padding
    if (padding != null && padding !== '') css.padding = String(padding)
  }

  if (node.opacity != null && node.opacity !== 1) {
    css.opacity = String(node.opacity)
  }

  if (node.type === 'TEXT') {
    const textSpans = resolveTextSpans(node, fontMap, paintMap)
    const isMixedText = needsSplitTextSpans(node, fontMap, paintMap)
    if (textSpans.length && !isMixedText) {
      Object.assign(
        css,
        fontRecordToCss(resolveFontRecord(fontMap, textSpans[0].fontRef, layout.h))
      )
      if (textSpans[0].color) css.color = textSpans[0].color
    } else if (isMixedText) {
      const spanColors = [...new Set(textSpans.map((s) => s.color).filter(Boolean))]
      if (spanColors.length === 1) css.color = spanColors[0]
    }
    if (!css.color) {
      const colors = node.textColor || []
      if (colors.length) {
        const c = paintToCssValue(resolvePaint(paintMap, colors[0] && colors[0].color))
        if (c) css.color = c
      }
    }
    if (node.textAlign && node.textAlign !== 'left') css['text-align'] = node.textAlign
    if (detectWhitespaceRisk(getTextContent(node)).length) {
      css['white-space'] = 'pre'
    }
  }

  if (node.type === 'PATH') {
    const paths = node.path || []
    if (paths.length && paths[0].fill) {
      const pf = paintToCssValue(resolvePaint(paintMap, paths[0].fill))
      if (pf) css.background = pf
    }
  }

  return css
}

function cssObjectToString(css) {
  return Object.entries(css)
    .map(([k, v]) => `${k}: ${v}`)
    .join('; ')
}

function assignFunctionalRegion(pathIndices, ctx) {
  if (pathIndices.length <= 1) return null
  if (pathIndices.length === 2) {
    const r = ctx.regions[pathIndices[1]]
    return r ? r.name : null
  }
  return ctx.regionByPath.get(String(pathIndices[1])) || null
}

function markTree(node, pathIndices, ctx) {
  const layoutMark = pathIndices.length ? `L${pathIndices.join('.')}` : 'L0'
  const functionalRegion = assignFunctionalRegion(pathIndices, ctx)

  const container = inferContainerHint(node)
  const marked = JSON.parse(JSON.stringify(node))
  marked._layoutMark = layoutMark
  if (functionalRegion) marked._functionalRegion = functionalRegion
  if (container) marked._container = container

  if (pathIndices.length === 2 && functionalRegion) {
    ctx.regionByPath.set(String(pathIndices[1]), functionalRegion)
  }

  const sorted = sortChildrenByPosition(marked.children || [])
  marked.children = sorted.map((child, i) =>
    markTree(child, [...pathIndices, i], ctx)
  )
  return marked
}

function buildLayerLines(node, prefix, isLast, ctx) {
  const lines = []
  const connector = isLast ? '└── ' : '├── '
  const type = node.type || 'NODE'
  const name = node.name || ''
  const layout = getBBox(node)
  const detail = shallowNodeForParen(node)
  detail._layoutMark = node._layoutMark
  if (node._functionalRegion) detail._functionalRegion = node._functionalRegion
  if (node._container) detail._container = node._container

  const fontMap = ctx.fontMap
  const mixedHint =
    node.type === 'TEXT' && fontMap
      ? (() => {
          const spans = resolveTextSpans(node, fontMap, ctx.paintMap || {})
          return needsSplitTextSpans(node, fontMap, ctx.paintMap || {})
            ? spans
                .map((s) => {
                  const bits = [`${s.text}@${s.fontSize}px`]
                  if (s.color) bits.push(s.color)
                  return bits.join('/')
                })
                .join('+')
            : null
        })()
      : null
  if (mixedHint) detail._mixedText = mixedHint

  let label = `[${type}] ${name}`
  if (mixedHint) label = `[TEXT·混排] ${name}`
  if (node.type === 'GROUP') {
    label += ` {group-container:${name}}`
  }

  const detailStr = JSON.stringify(detail)
  lines.push(
    `${prefix}${connector}${label} (layout=${layout.w}×${layout.h}@${layout.x},${layout.y}; ${detailStr})`
  )

  const children = node.children || []
  children.forEach((child, i) => {
    const childPrefix = prefix + (isLast ? '    ' : '│   ')
    lines.push(...buildLayerLines(child, childPrefix, i === children.length - 1, ctx))
  })
  return lines
}

function buildMixedTextInnerHtml(node, paintMap, fontMap) {
  const layout = getBBox(node)
  const spans = resolveTextSpans(node, fontMap, paintMap)

  return spans
    .map((span, index) => {
      const css = fontRecordToCss(
        resolveFontRecord(fontMap, span.fontRef, layout.h)
      )
      if (span.color) css.color = span.color
      const cssStr = cssObjectToString(css).replace(/"/g, '&quot;')
      const safeText = String(span.text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
      return `<span data-span-index="${index}" style="${cssStr}">${safeText}</span>`
    })
    .join('')
}

function buildDomLines(node, depth, paintMap, fontMap, effectMap) {
  const lines = []
  const indent = '  '.repeat(depth)
  const tag = domTagForType(node.type)
  const css = nodeToCssProps(node, paintMap, fontMap, effectMap)
  const cssStr = cssObjectToString(css)
  const attrs = []
  attrs.push(`data-layout-mark="${node._layoutMark || ''}"`)
  attrs.push(`data-type="${node.type || ''}"`)
  if (node.name) attrs.push(`data-name="${String(node.name).replace(/"/g, '&quot;')}"`)
  if (node._functionalRegion) {
    attrs.push(`data-functional-region="${String(node._functionalRegion).replace(/"/g, '&quot;')}"`)
  }
  if (node._container && node._container.role === 'group-container') {
    attrs.push(`data-group-container="${String(node._container.ref).replace(/"/g, '&quot;')}"`)
  }
  const flex = node.flexContainerInfo
  const flexChildren = node.children || []
  if (flex && Object.keys(flex).length && flexChildren.length >= 2) {
    attrs.push('data-flex-group="true"')
    const childMarks = flexChildren.map((c) => c._layoutMark).filter(Boolean)
    if (childMarks.length) {
      attrs.push(`data-flex-children="${childMarks.join(',')}"`)
    }
  }

  const textContent = node.type === 'TEXT' ? getTextContent(node) : ''
  if (textContent && detectWhitespaceRisk(textContent).length) {
    attrs.push('data-preserve-whitespace="true"')
  }
  const isMixedText =
    node.type === 'TEXT' && needsSplitTextSpans(node, fontMap, paintMap)
  const open = `${indent}<${tag} ${attrs.join(' ')} style="${cssStr.replace(/"/g, '&quot;')}">`
  if (isMixedText && !(node.children || []).length) {
    lines.push(`${open}${buildMixedTextInnerHtml(node, paintMap, fontMap)}</${tag}>`)
  } else if (textContent && !(node.children || []).length) {
    lines.push(`${open}${textContent}</${tag}>`)
  } else {
    lines.push(open)
    for (const child of node.children || []) {
      lines.push(...buildDomLines(child, depth + 1, paintMap, fontMap, effectMap))
    }
    lines.push(`${indent}</${tag}>`)
  }
  return lines
}

function collectStyleSpecs(nodes, paintMap, fontMap, effectMap, out) {
  for (const node of nodes) {
    const spec = nodeVisualSpec(node, paintMap, fontMap, effectMap)
    const hasVisual =
      spec.fill ||
      spec.stroke ||
      spec.effect ||
      spec.gap ||
      spec.text ||
      spec.isMixedText ||
      spec.pathFill ||
      spec.borderRadius
    if (hasVisual) out.push(spec)
    collectStyleSpecs(node.children || [], paintMap, fontMap, effectMap, out)
  }
}

function escapeMdCell(v) {
  if (v == null || v === '') return '-'
  return String(v).replace(/\|/g, '\\|').replace(/\n/g, ' ')
}

/** 混排 TEXT 一行实现摘要，供清单与控制台提示 */
function formatMixedTextHint(spec) {
  const spans = spec.textSpans || []
  return spans
    .map((s) => {
      const parts = [`${s.fontSize}px/${s.fontWeight}`]
      if (s.fontFamily) parts.push(s.fontFamily)
      if (s.color) parts.push(s.color)
      return `\`${s.text}\` ${parts.join('/')}`
    })
    .join(' + ')
}

/** 供 Agent 判断是否存在混排及逐节点 span 规格（见 mixed-text.json） */
function buildMixedTextManifest(mixedTextRows) {
  const seen = new Set()
  const items = []
  for (const s of mixedTextRows) {
    const key = `${s.layoutMark}|${s.content}`
    if (seen.has(key)) continue
    seen.add(key)
    const spans = (s.textSpans || []).map((sp, index) => ({
      index,
      text: sp.text,
      fontSize: sp.fontSize,
      fontWeight: sp.fontWeight,
      lineHeightPx: sp.lineHeightPx,
      lineHeightSource: sp.lineHeightSource || null,
      letterSpacing: sp.letterSpacing || null,
      fontFamily: sp.fontFamily || null,
      color: sp.color || null,
      textDecoration: sp.textDecoration || 'none',
      fontStyle: sp.fontStyle || 'normal',
    }))
    items.push({
      layoutMark: s.layoutMark,
      content: s.content,
      spanCount: spans.length,
      spans,
    })
  }
  return {
    hasMixedText: items.length > 0,
    count: items.length,
    items,
  }
}

function buildFlexGroupsManifest(markedNodes) {
  const items = []

  function walk(node, parentLayoutMark = null) {
    if (!node) return
    const flex = node.flexContainerInfo
    const children = node.children || []
    const layoutMark = node._layoutMark || null
    if (flex && Object.keys(flex).length && children.length >= 2) {
      const childMarks = children.map((c) => c._layoutMark).filter(Boolean)
      items.push({
        layoutMark,
        name: node.name || '',
        functionalRegion: node._functionalRegion || null,
        parentLayoutMark,
        flexDirection: flex.flexDirection === 'column' ? 'column' : 'row',
        gap: flexGapToCss(flex) || null,
        childMarks,
        /** childMarks 不得提升为 parentLayoutMark 对应容器的直接子级 */
        doNotPromoteChildrenTo: parentLayoutMark,
      })
    }
    for (const child of children) walk(child, layoutMark || parentLayoutMark)
  }

  for (const n of markedNodes) walk(n)

  const markSet = new Set(items.map((i) => i.layoutMark))
  const highRiskFlatten = items
    .filter(
      (item) =>
        item.parentLayoutMark &&
        markSet.has(item.parentLayoutMark) &&
        item.childMarks.length >= 2
    )
    .map((item) => ({
      wrapperMark: item.layoutMark,
      wrapperName: item.name,
      parentMark: item.parentLayoutMark,
      gap: item.gap,
      childMarks: item.childMarks,
      rule: `禁止将 [${item.childMarks.join(', ')}] 提升为 ${item.parentLayoutMark} 的直接子级；Vue 中须保留 ${item.layoutMark}（${item.name}）包裹层，gap 与 dom.md 一致（${item.gap || '见 dom.md'}）`,
    }))

  return {
    hasFlexGroups: items.length > 0,
    count: items.length,
    doNotFlatten: true,
    highRiskFlattenCount: highRiskFlatten.length,
    highRiskFlatten,
    items,
  }
}

/** 嵌套 flex 组清单（代码生成前必查，防止拍平） */
function buildFlexNestingMarkdown(manifest) {
  const lines = [
    '# Flex 嵌套结构（禁止拍平）',
    '',
    '> 由 `flex-groups.json` 自动生成。写 Vue 前**必须**对照本文件与 `dom.md` 的 `data-flex-group` 节点，**禁止**把子 flex 组的 `childMarks` 提升为祖父容器的直接子级。',
    '',
    '## 原则',
    '',
    '1. `dom.md` 里带 `data-flex-group="true"` 的节点 → 代码里必须有对应 `display:flex` 包裹层。',
    '2. `data-flex-children` 所列 layoutMark → 只能是该 flex 包裹层的**直接子级**，不得与 flex 包裹层同级。',
    '3. 父级若 `justify-content: space-between` 且仅 2 个直接子级，不要把「子 flex 组」内的列再拆成 3 个同级列。',
    '',
  ]

  if (manifest.highRiskFlatten && manifest.highRiskFlatten.length) {
    lines.push(
      `## 高风险拍平（共 ${manifest.highRiskFlatten.length} 处，优先核对）`,
      '',
      '| 须保留的包裹层 | 名称 | 父级 layoutMark | gap | 禁止提升的子级 |',
      '|---|---|---|---|---|'
    )
    for (const row of manifest.highRiskFlatten) {
      lines.push(
        `| ${escapeMdCell(row.wrapperMark)} | ${escapeMdCell(row.wrapperName)} | ${escapeMdCell(row.parentMark)} | ${escapeMdCell(row.gap || '-')} | ${escapeMdCell(row.childMarks.join(', '))} |`
      )
    }
    lines.push('', '### 说明', '')
    for (const row of manifest.highRiskFlatten) {
      lines.push(`- **${row.wrapperMark}**：${row.rule}`)
    }
    lines.push('')
  } else {
    lines.push('## 高风险拍平', '', '（本页无嵌套 flex 组，仍须遵守上方原则。）', '')
  }

  if (manifest.items && manifest.items.length) {
    lines.push(
      `## 全部 Flex 组（${manifest.count} 处）`,
      '',
      '| layoutMark | name | parent | direction | gap | childMarks |',
      '|---|---|---|---|---|---|'
    )
    for (const item of manifest.items) {
      lines.push(
        `| ${escapeMdCell(item.layoutMark)} | ${escapeMdCell(item.name)} | ${escapeMdCell(item.parentLayoutMark || '-')} | ${escapeMdCell(item.flexDirection)} | ${escapeMdCell(item.gap || '-')} | ${escapeMdCell(item.childMarks.join(', '))} |`
      )
    }
    lines.push('')
  }

  return `${lines.join('\n')}\n`
}

function buildStylesMarkdown(specs) {
  const mixedTextRows = specs.filter((s) => s.type === 'TEXT' && s.isMixedText)
  const textRows = specs.filter(
    (s) => s.type === 'TEXT' && s.text && !s.isMixedText
  )
  const containerRows = specs.filter(
    (s) =>
      s.type !== 'TEXT' &&
      (s.fill || s.stroke || s.effect || s.gap || s.borderRadius)
  )

  const lines = [
    '# 样式规格（styles.md）',
    '',
    '> 由 MasterGo DSL 提取。代码生成时 **font-size / font-weight / line-height / letter-spacing / font-family / text-decoration / color / fill / stroke / border-radius / box-shadow / gap** 必须与本文件及 dom.md 一致，禁止凭经验改写。',
    '',
    '## 重点关注字段',
    '',
    '- **fills**：节点 `fill` / PATH `pathFill`',
    '- **strokes**：`strokeColor` + `strokeWidth`',
    '- **effects**：`effect` → `box-shadow`',
    '- **absoluteBoundingBox**：`bbox.x/y/w/h`（来自 layoutStyle）',
    '- **itemSpacing**：flex 容器 `gap`',
    '- **文本**：`fontSize` / `fontWeight` / `lineHeightPx` / `letterSpacing` / `fontFamily` / `textDecoration` / `fontStyle`',
    '- **混排文本**：见同目录 **`mixed-text.json`**（`hasMixedText`）；为 true 时读下方清单 + `dom.md` 的 `data-span-index`',
    '- **空白保真**：见同目录 **`text-whitespace.json`**（`hasWhitespaceRisk`）；命中节点须 `white-space: pre` 或 `&nbsp;` 保留空白序列，对应 `dom.md` 的 `data-preserve-whitespace`',
    '',
  ]

  const whitespaceRows = specs.filter(
    (s) => s.type === 'TEXT' && s.content && detectWhitespaceRisk(s.content).length
  )
  if (whitespaceRows.length) {
    lines.push(
      `## 文本空白保真清单（代码生成前必查，共 ${whitespaceRows.length} 处）`,
      '',
      '> ⚠️ 下列文案含 HTML 会折叠的空白（连续/前后/全角空格等）。`white-space: nowrap` 会把连续空格折叠成一个，导致渲染宽度小于 DSL bbox、右端逐字累积横向漂移。**必须** `white-space: pre`（单行）或将连续空格转 `&nbsp;`。',
      '',
      '| layoutMark | 文案（空白可视化） | 空白类型 | DSL 宽度 |',
      '|---|---|---|---:|'
    )
    const seenWs = new Set()
    for (const s of whitespaceRows) {
      const key = `${s.layoutMark}|${s.content}`
      if (seenWs.has(key)) continue
      seenWs.add(key)
      const labels = detectWhitespaceRisk(s.content)
        .map((k) => (WS_PATTERN_DEFS.find((p) => p.key === k) || {}).label || k)
        .join('、')
      lines.push(
        `| ${escapeMdCell(s.layoutMark)} | ${escapeMdCell(visualizeWhitespace(s.content))} | ${escapeMdCell(labels)} | ${escapeMdCell(s.bbox && s.bbox.w)} |`
      )
    }
    lines.push('')
  }

  if (mixedTextRows.length) {
    lines.push(
      `## 混排文本实现清单（代码生成前必查，共 ${mixedTextRows.length} 处）`,
      '',
      '> ⚠️ 下列节点 **不会** 出现在「文本样式」主表。须按「拆分要求」拆多个 `<span>`（字号/字重/字体/颜色任一不同即拆），与 dom.md 内 `data-span-index` 一致。',
      '',
      '| layoutMark | 完整文案 | 拆分要求 |',
      '|---|---|---|'
    )
    const seen = new Set()
    for (const s of mixedTextRows) {
      const key = `${s.layoutMark}|${s.content}`
      if (seen.has(key)) continue
      seen.add(key)
      lines.push(
        `| ${escapeMdCell(s.layoutMark)} | ${escapeMdCell(s.content)} | ${escapeMdCell(formatMixedTextHint(s))} |`
      )
    }
    lines.push('')
  }

  lines.push(
    '## 文本样式',
    '',
    '| layoutMark | 文案 | fontSize | fontWeight | lineHeightPx | letterSpacing | fontFamily | decoration | color | textAlign |',
    '|---|---|---:|---:|---:|---|---|---|---|---|'
  )

  for (const s of textRows) {
    lines.push(
      `| ${escapeMdCell(s.layoutMark)} | ${escapeMdCell(s.content)} | ${escapeMdCell(s.text.fontSize)} | ${escapeMdCell(s.text.fontWeight)} | ${escapeMdCell(s.text.lineHeightPx)} | ${escapeMdCell(s.text.letterSpacing || '-')} | ${escapeMdCell(s.text.fontFamily)} | ${escapeMdCell(s.text.textDecoration || '-')} | ${escapeMdCell(s.textColor)} | ${escapeMdCell(s.textAlign || '-')} |`
    )
  }

  if (mixedTextRows.length) {
    lines.push('', '## 混排文本样式（须拆 span）', '')
    lines.push(
      '> 已按 `text[]` + `textColor[]` 字符边界合并为原子 span；**fontSize / fontWeight / fontFamily / letterSpacing / textDecoration / color** 任一不同即须拆 `<span>`，禁止整段套用 `text[0]` 或 `textColor[0]`。',
      ''
    )
    lines.push(
      '| layoutMark | 完整文案 | spanIndex | 片段 | fontSize | fontWeight | lineHeightPx | letterSpacing | fontFamily | decoration | color |',
      '|---|---|---:|---|---:|---:|---:|---|---|---|---|'
    )
    for (const s of mixedTextRows) {
      const spans = s.textSpans || []
      spans.forEach((span, index) => {
        lines.push(
          `| ${escapeMdCell(s.layoutMark)} | ${escapeMdCell(s.content)} | ${index} | ${escapeMdCell(span.text)} | ${escapeMdCell(span.fontSize)} | ${escapeMdCell(span.fontWeight)} | ${escapeMdCell(span.lineHeightPx)} | ${escapeMdCell(span.letterSpacing || '-')} | ${escapeMdCell(span.fontFamily)} | ${escapeMdCell(span.textDecoration || '-')} | ${escapeMdCell(span.color || '-')} |`
        )
      })
    }
  }

  lines.push('', '## 容器样式', '')
  lines.push(
    '| layoutMark | name | bbox (x,y,w×h) | fill | stroke | effect | gap | borderRadius |',
    '|---|---|---|---|---|---|---|---|'
  )

  for (const s of containerRows) {
    const bbox = `${s.bbox.x},${s.bbox.y},${s.bbox.w}×${s.bbox.h}`
    const stroke = s.stroke
      ? `${s.stroke.width || 1} ${s.stroke.color || ''}`.trim()
      : '-'
    lines.push(
      `| ${escapeMdCell(s.layoutMark)} | ${escapeMdCell(s.name)} | ${escapeMdCell(bbox)} | ${escapeMdCell(s.fill)} | ${escapeMdCell(stroke)} | ${escapeMdCell(s.effect || '-')} | ${escapeMdCell(s.gap || '-')} | ${escapeMdCell(s.borderRadius || '-')} |`
    )
  }

  lines.push('')
  return `${lines.join('\n')}\n`
}

function generateLayoutArtifacts(inputPath, outputDir, opts = {}) {
  const { obj, dsl } = loadDslObject(inputPath)
  const styles = dsl.styles || {}
  const { paintMap, fontMap, effectMap } = buildStyleMaps(styles)
  const stripChrome = opts.stripChrome !== false
  const stripLog = loadStripLogForLayout(outputDir, opts.stripLog || null)
  const originalNodes = dsl.nodes || []
  const hostLayout =
    opts.hostLayout ||
    analyzeHostLayout(originalNodes, { override: opts.hostLayoutOverride })
  let rawNodes = opts.preStrippedNodes
  if (!rawNodes) {
    rawNodes = originalNodes
    if (stripChrome) {
      rawNodes = applyChromeStripToNodes(rawNodes, stripLog)
    }
  }

  const designFrameSize = computeDesignFrameSize(rawNodes)
  const layoutShiftY = computeH5LayoutOffsetY(getPageRootChildren(rawNodes), {
    hostLayout,
  })
  const workingNodes = JSON.parse(JSON.stringify(rawNodes))
  if (layoutShiftY > 0) {
    // 仅平移顶层业务块自身（非递归）；子孙的 relativeY 相对各自父级，随父级一起平移，不可再减。
    for (const node of getPageRootChildren(workingNodes)) {
      shiftTopLevelNodeY(node, layoutShiftY)
    }
    rebaseRootFrameHeight(workingNodes, layoutShiftY)
  }
  const h5FrameSize = computeDesignFrameSize(workingNodes)

  const regions = buildFunctionalRegions(
    getPageRootChildren(workingNodes)
  )

  const ctx = { regions, regionByPath: new Map(), fontMap, paintMap }
  const markedNodes = sortChildrenByPosition(workingNodes).map((n, i) =>
    markTree(n, [i], ctx)
  )

  const markedDsl = JSON.parse(JSON.stringify(obj))
  if (markedDsl.dsl !== undefined) markedDsl.dsl.nodes = markedNodes
  else markedDsl.nodes = markedNodes

  const chromeNote =
    stripChrome || opts.preStrippedNodes
      ? '已剔除宿主壳层（状态栏 / 胶囊 / WebView 标题栏残留等，见 stripped-chrome.json）；'
      : ''
  const firstRegionName = hostLayout.firstBusinessName || regions[0]?.name || ''
  const layerHeader = [
    '# Layer 布局树',
    '',
    `> 由 MasterGo DSL 生成：${chromeNote}兄弟节点按 Y→X 排序；GROUP 标注容器；功能区域见 _functionalRegion。`,
    '',
    ...buildExcludedNavTitleSection(stripLog),
    ...buildH5LayoutNoteSection(hostLayout, firstRegionName),
    '## 功能区域划分',
    ...regions.map(
      (r) =>
        `- **${r.name}** (${r.rootType} \`${r.rootName}\`) — y=${r.y}, h=${r.h}, id=${r.id}`
    ),
    '',
    '## 布局树',
    '',
  ]

  const layerBody = []
  markedNodes.forEach((node, i) => {
    layerBody.push(...buildLayerLines(node, '', i === markedNodes.length - 1, ctx))
  })

  const flexGroupsManifest = buildFlexGroupsManifest(markedNodes)
  const flexNestingMd = buildFlexNestingMarkdown(flexGroupsManifest)

  const domHeader = [
    '# DOM 树',
    '',
    `> 由 dsl_layout_marked.json + 布局标记生成；${chromeNote}宿主策略=${hostLayout.policy}，layoutShiftY=${layoutShiftY}px；含 fills/strokes/effects/bbox/gap 及文本 fontSize/fontWeight/lineHeightPx/letterSpacing。`,
    '',
    '> **Flex 禁止拍平**：带 `data-flex-group="true"` 的节点须在代码中保留为独立 flex 包裹层；`data-flex-children` 不得提升为父级同级。写代码前必读同目录 **`flex-nesting.md`** 与 **`flex-groups.json`**。',
    '',
    '```html',
  ]
  const domBody = []
  for (const node of markedNodes) {
    domBody.push(...buildDomLines(node, 0, paintMap, fontMap, effectMap))
  }
  const domFooter = ['```', '']

  const styleSpecs = []
  collectStyleSpecs(markedNodes, paintMap, fontMap, effectMap, styleSpecs)
  const stylesMd = buildStylesMarkdown(styleSpecs)
  const mixedTextRows = styleSpecs.filter((s) => s.type === 'TEXT' && s.isMixedText)
  const mixedTextManifest = buildMixedTextManifest(mixedTextRows)
  const textWhitespaceManifest = buildTextWhitespaceManifest(markedNodes)

  fs.mkdirSync(outputDir, { recursive: true })
  const layerPath = `${outputDir}/Layer.md`
  const domPath = `${outputDir}/dom.md`
  const stylesPath = `${outputDir}/styles.md`
  const markedPath = `${outputDir}/dsl_layout_marked.json`
  const mixedTextPath = `${outputDir}/mixed-text.json`
  const textWhitespacePath = `${outputDir}/text-whitespace.json`
  const flexGroupsPath = `${outputDir}/flex-groups.json`
  const flexNestingPath = `${outputDir}/flex-nesting.md`
  const layoutH5Path = `${outputDir}/layout-h5.json`
  const imageSourcePath = `${outputDir}/image-source.png`
  let imageSourceSize = null
  let designCropTopRaster = 0
  if (fs.existsSync(imageSourcePath)) {
    try {
      const { decodeImage, computeRasterDesignCropTop } = require('./d2c-visual-core.cjs')
      const img = decodeImage(imageSourcePath)
      imageSourceSize = { width: img.width, height: img.height }
      if (layoutShiftY > 0 && designFrameSize?.height) {
        designCropTopRaster = computeRasterDesignCropTop(
          layoutShiftY,
          designFrameSize.height,
          img.height
        )
      }
    } catch (_) {
      /* image-source 尚未落盘时跳过 */
    }
  }
  const layoutH5 = {
    schemaVersion: 2,
    hostLayoutPolicy: hostLayout.policy,
    hostLayoutConfidence: hostLayout.confidence,
    hostLayoutSource: hostLayout.source,
    hostLayoutEvidence: hostLayout.evidence,
    hostLayoutScores: hostLayout.scores,
    hostChromeBottomY: hostLayout.hostChromeBottomY,
    firstBusinessY: hostLayout.firstBusinessY,
    firstBusinessName: hostLayout.firstBusinessName,
    businessTopGap: hostLayout.businessTopGap,
    layoutShiftY,
    // Backward-compatible alias. New consumers must use layoutShiftY.
    contentOffsetY: layoutShiftY,
    designFrameSize,
    h5FrameSize,
    imageSourceSize,
    designCropTopDsl: layoutShiftY,
    designCropTopRaster,
    designCropTopFormula:
      'round(layoutShiftY * imageSource.height / designFrame.height)',
    ignoredDesignRegions: hostLayout.ignoredDesignRegions || [],
    firstBusinessRegion: firstRegionName,
    excludedNavTitleBars: (stripLog || [])
      .filter((i) =>
        /(?:nav_title_bar_remnant|native_webview_shell_structural|status_bar_structural)/.test(
          String(i.reason || '')
        )
      )
      .map((i) => ({
        name: i.name,
        path: i.path,
        note: i.note,
      })),
    h5CoordinateNote:
      hostLayout.policy === 'immersive-overlay'
        ? '沉浸式 H5：勿还原 excludedNavTitleBars，但保留完整画布与顶部坐标；设计图不裁顶'
        : hostLayout.policy === 'stacked-host'
          ? '叠层式宿主：按 hostChromeBottomY 平移 H5，并用 designCropTopRaster 裁掉设计侧宿主区'
          : '未检测到宿主壳层：使用原始设计/H5 坐标，不平移、不裁顶',
  }
  fs.writeFileSync(layoutH5Path, `${JSON.stringify(layoutH5, null, 2)}\n`, 'utf8')
  const hostLayoutPath = `${outputDir}/host-layout.json`
  fs.writeFileSync(
    hostLayoutPath,
    `${JSON.stringify(hostLayout, null, 2)}\n`,
    'utf8'
  )

  fs.writeFileSync(layerPath, `${layerHeader.join('\n')}${layerBody.join('\n')}\n`, 'utf8')
  fs.writeFileSync(
    domPath,
    `${domHeader.join('\n')}\n${domBody.join('\n')}\n${domFooter.join('\n')}`,
    'utf8'
  )
  fs.writeFileSync(stylesPath, stylesMd, 'utf8')
  fs.writeFileSync(markedPath, `${JSON.stringify(markedDsl, null, 2)}\n`, 'utf8')
  const filteredPath = `${outputDir}/dsl_filtered.json`
  const filteredJson = JSON.stringify(markedDsl)
  fs.writeFileSync(filteredPath, `${filteredJson}\n`, 'utf8')
  fs.writeFileSync(mixedTextPath, `${JSON.stringify(mixedTextManifest, null, 2)}\n`, 'utf8')
  fs.writeFileSync(
    textWhitespacePath,
    `${JSON.stringify(textWhitespaceManifest, null, 2)}\n`,
    'utf8'
  )
  fs.writeFileSync(flexGroupsPath, `${JSON.stringify(flexGroupsManifest, null, 2)}\n`, 'utf8')
  fs.writeFileSync(flexNestingPath, flexNestingMd, 'utf8')

  return {
    layerPath,
    domPath,
    stylesPath,
    markedPath,
    filteredPath,
    filteredBytes: Buffer.byteLength(filteredJson, 'utf8'),
    markedBytes: Buffer.byteLength(JSON.stringify(markedDsl, null, 2), 'utf8'),
    mixedTextPath,
    textWhitespacePath,
    textWhitespaceManifest,
    textWhitespaceCount: textWhitespaceManifest.count,
    flexGroupsPath,
    flexNestingPath,
    layoutH5Path,
    hostLayoutPath,
    flexGroupCount: flexGroupsManifest.count,
    highRiskFlattenCount: flexGroupsManifest.highRiskFlattenCount,
    mixedTextManifest,
    nodeCount: countMarkedNodes(markedNodes),
    regionCount: regions.length,
    styleSpecCount: styleSpecs.length,
    mixedTextCount: mixedTextManifest.count,
  }
}

function countMarkedNodes(nodes) {
  let n = 0
  for (const node of nodes) {
    n += 1
    n += countMarkedNodes(node.children || [])
  }
  return n
}

module.exports = {
  loadDslObject,
  sortChildrenByPosition,
  buildStyleMaps,
  generateLayoutArtifacts,
  nodeToCssProps,
  domTagForType,
  getBBox,
  getTextContent,
  detectWhitespaceRisk,
  visualizeWhitespace,
  buildTextWhitespaceManifest,
  computeH5LayoutOffsetY,
  isTopLevelBackdrop,
}
