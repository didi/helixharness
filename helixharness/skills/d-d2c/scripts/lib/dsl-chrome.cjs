/**
 * MasterGo DSL 宿主壳层剔除（状态栏 / 胶囊 / Home 指示条 / WebView 标题栏残留等）
 * filter-dsl.cjs 与 dsl-layout-core.cjs 共用，保证 dsl_visual / Layer.md / dom.md 一致。
 */

function round1(n) {
  return Math.round(Number(n || 0) * 10) / 10
}

/** 系统/壳层节点：默认剔除，避免 Agent 还原状态栏、胶囊、Home 指示条等 */
const CHROME_DROP = {
  instance: [/^Bars\/Status/i],
  frame: [
    /^全局样式_浅/,
    /^全局操作$/,
    /^关闭$/,
    /^返回$/,
    /^容器 815$/,
    /^shouye/i,
    /^容器 133$/, // iPhone Home 指示条容器
  ],
  group: [/^Symbols$/, /^Battery$/, /^身份识别模块$/],
  path: [/^更多$/, /^分割线$/],
  textContent: [/^9:41$/],
  textName: [/^Bars\/Status/, /^↳ Time$/],
}

/** 750 稿常见 WebView 原生标题栏容器（壳层子节点剔除后常仅剩居中 page title TEXT） */
const NAV_TITLE_BAR_FRAME = /^容器 52$/

function getTextContent(node) {
  const texts = node.text || []
  return texts
    .map((t) => (t && t.text ? t.text : ''))
    .filter(Boolean)
    .join('')
}

function getLayoutBox(node) {
  const layout = (node && (node.layoutStyle || node.absoluteBoundingBox)) || {}
  return {
    x: Number(layout.relativeX ?? layout.x ?? 0) || 0,
    y: Number(layout.relativeY ?? layout.y ?? 0) || 0,
    w: Number(layout.width ?? layout.w ?? 0) || 0,
    h: Number(layout.height ?? layout.h ?? 0) || 0,
  }
}

function collectDescendants(node, parentX, parentY, out) {
  if (!node) return out
  const box = getLayoutBox(node)
  const absX = parentX + box.x
  const absY = parentY + box.y
  out.push({ node, absX, absY, box })
  for (const child of node.children || []) {
    collectDescendants(child, absX, absY, out)
  }
  return out
}

function frameSizeForNodes(nodes) {
  if ((nodes || []).length === 1) {
    const box = getLayoutBox(nodes[0])
    if (box.w > 0 && box.h > 0) return { width: box.w, height: box.h }
  }
  let width = 0
  let height = 0
  for (const node of nodes || []) {
    const box = getLayoutBox(node)
    width = Math.max(width, box.x + box.w)
    height = Math.max(height, box.y + box.h)
  }
  return { width, height }
}

/**
 * 名称被扁平化后，状态栏常退化为顶部左右两簇紧凑 PATH。使用相对画板比例
 * 识别该结构，避免依赖 Bars/Status、Battery 等特定图层名。
 */
function isLikelyStatusCluster(node, frame, parentX = 0, parentY = 0) {
  if (!node || !frame.width || !frame.height) return false
  const box = getLayoutBox(node)
  const absX = parentX + box.x
  const absY = parentY + box.y
  const descendants = collectDescendants(node, parentX, parentY, [])
  const paths = descendants.filter((item) => item.node.type === 'PATH')
  const texts = descendants
    .filter((item) => item.node.type === 'TEXT')
    .map((item) => getTextContent(item.node).trim())
    .filter(Boolean)
  const textLooksSystemOnly = texts.every(
    (text) => text.length <= 8 && /^(?:\d{1,2}:\d{2}|\d+%?)$/.test(text)
  )
  const compactTopBand =
    absY <= Math.max(72, frame.height * 0.06) &&
    box.h > 0 &&
    box.h <= Math.max(48, frame.height * 0.045) &&
    box.w >= frame.width * 0.55
  if (!compactTopBand || paths.length < 6 || !textLooksSystemOnly) return false

  const centers = paths.map((item) => item.absX + item.box.w / 2)
  const leftCount = centers.filter((x) => x <= frame.width * 0.32).length
  const rightCount = centers.filter((x) => x >= frame.width * 0.68).length
  return leftCount >= 2 && rightCount >= 3 && absX < frame.width * 0.2
}

function findStatusCluster(node, frame, parentX = 0, parentY = 0) {
  if (!node) return null
  if (isLikelyStatusCluster(node, frame, parentX, parentY)) return node
  const box = getLayoutBox(node)
  const absX = parentX + box.x
  const absY = parentY + box.y
  for (const child of node.children || []) {
    const hit = findStatusCluster(child, frame, absX, absY)
    if (hit) return hit
  }
  return null
}

function isLikelyNativeWebViewShell(node, frame) {
  if (!node || !frame.width || !frame.height) return false
  const box = getLayoutBox(node)
  const fullWidthTopBand =
    box.y <= Math.max(16, frame.height * 0.02) &&
    box.w >= frame.width * 0.85 &&
    box.h >= Math.max(72, frame.height * 0.045) &&
    box.h <= Math.min(280, frame.height * 0.22)
  if (!fullWidthTopBand || !findStatusCluster(node, frame, 0, 0)) return false

  const descendants = collectDescendants(node, 0, 0, [])
  const hasCenteredTitle = descendants.some((item) => {
    if (item.node.type !== 'TEXT' || !getTextContent(item.node).trim()) return false
    const centerX = item.absX + item.box.w / 2
    return (
      Math.abs(centerX - frame.width / 2) <= frame.width * 0.12 &&
      item.absY >= box.y + box.h * 0.35
    )
  })
  const hasNavigationControl = descendants.some((item) => {
    const name = String(item.node.name || '')
    const inLowerLeftControlArea =
      item.absX <= frame.width * 0.15 &&
      item.absY >= box.y + box.h * 0.3 &&
      item.box.w >= 4 &&
      item.box.h >= 4 &&
      item.box.w <= Math.max(64, frame.width * 0.1) &&
      item.box.h <= Math.max(64, box.h * 0.5)
    return (
      (/^(?:返回|关闭|back|close)$/i.test(name) &&
        item.absY >= box.y + box.h * 0.3) ||
      (item.node.type === 'PATH' && inLowerLeftControlArea)
    )
  })
  return hasCenteredTitle && hasNavigationControl
}

/** 顶层宿主壳结构剔除；具体页面名称、节点 ID、固定像素均不参与判断。 */
function stripStructuralChromeFromNodes(nodes, stripLog) {
  const frame = frameSizeForNodes(nodes)
  if (!frame.width || !frame.height) return nodes || []
  const root = (nodes || []).length === 1 ? nodes[0] : null
  const children = root && (root.children || []).length ? root.children : nodes || []
  const rootName = root ? String(root.name || '') : ''
  const kept = []

  for (const child of children) {
    const structuralShell = isLikelyNativeWebViewShell(child, frame)
    const standaloneStatus = isLikelyStatusCluster(child, frame, 0, 0)
    if (structuralShell || standaloneStatus) {
      if (stripLog) {
        stripLog.push({
          reason: structuralShell
            ? 'native_webview_shell_structural'
            : 'status_bar_structural',
          type: child.type || '',
          name: String(child.name || ''),
          path: [rootName, String(child.name || '')].filter(Boolean).join(' > '),
          confidence: 'high',
          note: structuralShell
            ? '顶部全宽区域同时包含左右状态图形簇、居中标题和返回/关闭控件，判定为宿主 WebView 壳层'
            : '顶部紧凑区域包含左右系统图形簇，判定为原生状态栏',
        })
      }
      continue
    }
    kept.push(child)
  }

  if (!root) return kept
  const cloned = { ...root }
  if (kept.length) cloned.children = kept
  else delete cloned.children
  return [cloned]
}

function isTextAlignCenter(node) {
  const align = String(node.textAlign || '').toLowerCase()
  return align === 'center' || align === 'centre'
}

/**
 * WebView 原生标题栏残留：壳层剔除后，页面画布直接子 FRAME 内只剩 1~2 个居中标题 TEXT。
 * 设计图/H5 通常无「报价结果」等原生标题，勿进入 Layer.md。
 */
function isNavTitleBarRemnantFrame(node, parentNames) {
  if (!node || node.type !== 'FRAME') return false
  if ((parentNames || []).length !== 1) return false

  const layout = node.layoutStyle || {}
  const h = round1(layout.height)
  const y = round1(layout.relativeY ?? layout.y ?? 0)
  const name = String(node.name || '')

  const children = node.children || []
  if (!children.length) return false

  const allTextChildren = children.every((c) => c.type === 'TEXT')
  if (!allTextChildren || children.length > 2) return false

  const knownNavShell = NAV_TITLE_BAR_FRAME.test(name)
  const topTitleBand = y <= 30 && h >= 60 && h <= 280
  const centeredTitle =
    children.length === 1 && isTextAlignCenter(children[0])

  return knownNavShell || (topTitleBand && centeredTitle)
}

function shouldDropChrome(node, parentNames, stripLog) {
  const name = String(node.name || '')
  const type = node.type || ''
  const layout = node.layoutStyle || {}
  const parents = parentNames || []

  const logDrop = (reason) => {
    if (stripLog) {
      stripLog.push({
        reason,
        type,
        name,
        path: [...parents, name].join(' > '),
      })
    }
    return true
  }

  if (type === 'INSTANCE' && CHROME_DROP.instance.some((re) => re.test(name))) {
    return logDrop('status_bar_instance')
  }

  if (type === 'FRAME' && CHROME_DROP.frame.some((re) => re.test(name))) {
    return logDrop('nav_or_system_frame')
  }

  if (type === 'GROUP' && CHROME_DROP.group.some((re) => re.test(name))) {
    return logDrop('status_bar_group')
  }

  if (type === 'PATH' && CHROME_DROP.path.some((re) => re.test(name))) {
    return logDrop('miniprogram_capsule_path')
  }

  if (parents.includes('容器 133')) {
    return logDrop('home_indicator_child')
  }

  if (type === 'TEXT') {
    const content = getTextContent(node)
    if (CHROME_DROP.textContent.some((re) => re.test(content))) {
      return logDrop('placeholder_time')
    }
    if (CHROME_DROP.textName.some((re) => re.test(name))) {
      return logDrop('status_bar_text')
    }
  }

  if (type === 'GROUP') {
    const w = round1(layout.width)
    const h = round1(layout.height)
    const childCount = (node.children || []).length
    if (w === 0 && h === 0 && childCount === 0) {
      return logDrop('empty_group')
    }
  }

  if (
    (name === 'Wi-Fi' || name === 'Combined Shape') &&
    parents.some((p) => /Symbols|Battery|Bars\/Status/.test(p))
  ) {
    return logDrop('status_bar_icon')
  }

  return false
}

function stripChromeTreeNode(node, opts) {
  if (!node) return null

  const parentNames = opts.parentNames || []
  if (opts.stripChrome && shouldDropChrome(node, parentNames, opts.stripLog)) {
    return null
  }

  const children = node.children || []
  if (!children.length) return node

  const nextParents = [...parentNames, String(node.name || '')]
  const kept = children
    .map((child) =>
      stripChromeTreeNode(child, {
        stripChrome: opts.stripChrome,
        stripLog: opts.stripLog,
        parentNames: nextParents,
      })
    )
    .filter(Boolean)

  const cloned = { ...node, children: kept }
  if (!kept.length) delete cloned.children
  return cloned
}

/** 对 MasterGo 原始 nodes 数组剔除壳层 */
function stripChromeFromNodes(nodes, stripLog) {
  return (nodes || [])
    .map((n) =>
      stripChromeTreeNode(n, {
        stripChrome: true,
        stripLog,
        parentNames: [],
      })
    )
    .filter(Boolean)
}

/** 布局树剔除后修剪空 FRAME（仅原始 DSL 节点） */
function pruneEmptyLayoutBranches(node) {
  if (!node) return null
  const children = node.children || []
  if (children.length) {
    node.children = children.map(pruneEmptyLayoutBranches).filter(Boolean)
    if (!node.children.length) delete node.children
  }
  const hasContent =
    (node.text && node.text.length) ||
    (node.path && node.path.length) ||
    node.componentInfo ||
    (node.children && node.children.length)
  if (!hasContent && node.type === 'FRAME' && /^容器 52$/.test(node.name)) {
    return null
  }
  return node
}

/** 剔除 WebView 标题栏残留（见 isNavTitleBarRemnantFrame） */
function pruneNavTitleBarRemnants(node, stripLog, parentNames) {
  if (!node) return null

  const parents = parentNames || []
  const name = String(node.name || '')
  const nextParents = [...parents, name]

  const children = node.children || []
  if (children.length) {
    node.children = children
      .map((child) => pruneNavTitleBarRemnants(child, stripLog, nextParents))
      .filter(Boolean)
    if (!node.children.length) delete node.children
  }

  if (isNavTitleBarRemnantFrame(node, parents)) {
    if (stripLog) {
      const titleText = (node.children || [])
        .filter((c) => c.type === 'TEXT')
        .map((c) => getTextContent(c))
        .filter(Boolean)
        .join(' / ')
      stripLog.push({
        reason: 'nav_title_bar_remnant',
        type: node.type,
        name,
        path: nextParents.join(' > '),
        note: `WebView 原生标题栏（${titleText || '页面标题'}），设计图/H5 无此区块，勿还原`,
      })
    }
    return null
  }

  return node
}

function applyChromeStripToNodes(nodes, stripLog) {
  return stripChromeFromNodes(
    stripStructuralChromeFromNodes(nodes, stripLog),
    stripLog
  )
    .map((n) => pruneNavTitleBarRemnants(n, stripLog, []))
    .filter(Boolean)
    .map(pruneEmptyLayoutBranches)
    .filter(Boolean)
}

module.exports = {
  CHROME_DROP,
  NAV_TITLE_BAR_FRAME,
  getTextContent,
  isNavTitleBarRemnantFrame,
  shouldDropChrome,
  stripChromeFromNodes,
  stripChromeTreeNode,
  pruneEmptyLayoutBranches,
  pruneNavTitleBarRemnants,
  getLayoutBox,
  isLikelyStatusCluster,
  isLikelyNativeWebViewShell,
  stripStructuralChromeFromNodes,
  applyChromeStripToNodes,
}
