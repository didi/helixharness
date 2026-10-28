const path = require('path')
const {
  buildStyleMaps,
  nodeVisualSpec,
} = require('./dsl-style-core.cjs')

const DEFAULT_TOLERANCES = Object.freeze({
  colorChannel: 3,
  alpha: 0.02,
  fontSizePx: 1.5,
  lineHeightPx: 2,
  letterSpacingPx: 1,
  radiusPx: 2,
  borderWidthPx: 1,
  opacity: 0.02,
})

const SYSTEM_FONT_ROOTS = new Set([
  'arial',
  'helvetica',
  'helveticaneue',
  'hiraginosansgb',
  'microsoftyahei',
  'pingfangsc',
  'sansserif',
  'serif',
  'systemui',
])

function round1(value) {
  return Math.round(Number(value || 0) * 10) / 10
}

function finite(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function containsInvalidCssToken(value) {
  if (value == null) return false
  if (Array.isArray(value)) return value.some(containsInvalidCssToken)
  if (typeof value === 'object') return Object.values(value).some(containsInvalidCssToken)
  return /(?:^|[^a-z])(?:nan|undefined|null|infinity)(?:[^a-z]|$)/i.test(String(value))
}

function splitTopLevelCssLayers(value) {
  const input = String(value || '').trim()
  if (!input) return []
  const layers = []
  let depth = 0
  let quote = ''
  let start = 0
  for (let index = 0; index < input.length; index += 1) {
    const char = input[index]
    if ((char === '"' || char === "'") && input[index - 1] !== '\\') {
      quote = quote === char ? '' : quote || char
      continue
    }
    if (quote) continue
    if (char === '(') depth += 1
    else if (char === ')') depth = Math.max(0, depth - 1)
    else if (char === ',' && depth === 0) {
      layers.push(input.slice(start, index).trim())
      start = index + 1
    }
  }
  layers.push(input.slice(start).trim())
  return layers.filter(Boolean)
}

function partitionCssLayers(value) {
  const layers = splitTopLevelCssLayers(value)
  return {
    valid: layers.filter((layer) => !containsInvalidCssToken(layer)),
    invalid: layers.filter((layer) => containsInvalidCssToken(layer)),
  }
}

function parseCssColor(value) {
  const raw = String(value || '').trim().toLowerCase()
  if (!raw) return null
  if (raw === 'transparent') return { r: 0, g: 0, b: 0, a: 0 }
  const hex = raw.match(/^#([0-9a-f]{3,8})$/i)
  if (hex) {
    let body = hex[1]
    if (body.length === 3 || body.length === 4) {
      body = body.split('').map((c) => c + c).join('')
    }
    if (body.length !== 6 && body.length !== 8) return null
    return {
      r: parseInt(body.slice(0, 2), 16),
      g: parseInt(body.slice(2, 4), 16),
      b: parseInt(body.slice(4, 6), 16),
      a: body.length === 8 ? parseInt(body.slice(6, 8), 16) / 255 : 1,
    }
  }
  const rgb = raw.match(/^rgba?\(\s*([\d.]+)\s*[, ]\s*([\d.]+)\s*[, ]\s*([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/)
  if (!rgb) return null
  const alphaRaw = rgb[4]
  const alpha = alphaRaw == null
    ? 1
    : alphaRaw.endsWith('%')
      ? Number(alphaRaw.slice(0, -1)) / 100
      : Number(alphaRaw)
  return {
    r: Number(rgb[1]),
    g: Number(rgb[2]),
    b: Number(rgb[3]),
    a: alpha,
  }
}

function compareColors(expected, actual, tolerances = DEFAULT_TOLERANCES) {
  const e = parseCssColor(expected)
  const a = parseCssColor(actual)
  if (!e || !a) return { comparable: false, equal: false, delta: null }
  const delta = {
    r: Math.abs(e.r - a.r),
    g: Math.abs(e.g - a.g),
    b: Math.abs(e.b - a.b),
    a: Math.abs(e.a - a.a),
  }
  return {
    comparable: true,
    equal:
      Math.max(delta.r, delta.g, delta.b) <= tolerances.colorChannel &&
      delta.a <= tolerances.alpha,
    delta,
  }
}

function splitFontFamilies(value) {
  const input = String(value || '')
  const out = []
  let token = ''
  let quote = ''
  for (const char of input) {
    if ((char === '"' || char === "'") && (!quote || quote === char)) {
      quote = quote ? '' : char
      token += char
    } else if (char === ',' && !quote) {
      if (token.trim()) out.push(token.trim())
      token = ''
    } else {
      token += char
    }
  }
  if (token.trim()) out.push(token.trim())
  return out
}

function normalizeFontToken(value) {
  return String(value || '')
    .replace(/["']/g, '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fff]/g, '')
}

function inferWeightFromFontToken(value) {
  const token = normalizeFontToken(value)
  if (/black$|heavy$|900$/.test(token)) return 900
  if (/extrabold$|bold$|700$/.test(token)) return 700
  if (/semibold$|demibold$|600$|中粗$/.test(token)) return 600
  if (/medium$|500$|中黑$/.test(token)) return 500
  if (/regular$|normal$|400$|常规$/.test(token)) return 400
  if (/light$|300$/.test(token)) return 300
  if (/thin$|100$/.test(token)) return 100
  return null
}

function fontFamilyRoot(value) {
  let token = normalizeFontToken(value)
  token = token.replace(/(?:black|heavy|extrabold|bold|semibold|demibold|medium|regular|normal|light|thin|[1-9]00)$/i, '')
  return token
}

function familiesEquivalent(expected, actual) {
  const expectedRoots = splitFontFamilies(expected).map(fontFamilyRoot).filter(Boolean)
  const actualRoots = splitFontFamilies(actual).map(fontFamilyRoot).filter(Boolean)
  if (!expectedRoots.length || !actualRoots.length) return false
  return expectedRoots.some((root) => actualRoots.includes(root))
}

function isSystemFontFamily(value) {
  return splitFontFamilies(value)
    .map(fontFamilyRoot)
    .filter(Boolean)
    .every((root) => SYSTEM_FONT_ROOTS.has(root))
}

function normalizeWeight(value) {
  if (String(value).toLowerCase() === 'normal') return 400
  if (String(value).toLowerCase() === 'bold') return 700
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? n : 400
}

function semanticFontWeight(fontFamily, computedWeight) {
  const first = splitFontFamilies(fontFamily)[0] || ''
  return inferWeightFromFontToken(first) || normalizeWeight(computedWeight)
}

function fontFaceSupportsWeight(faceWeight, expectedWeight) {
  const raw = String(faceWeight || 'normal').trim().toLowerCase()
  if (raw === 'normal') return expectedWeight === 400
  if (raw === 'bold') return expectedWeight === 700
  const range = raw.match(/^(\d+)\s+(\d+)$/)
  if (range) return expectedWeight >= Number(range[1]) && expectedWeight <= Number(range[2])
  return normalizeWeight(raw) === expectedWeight
}

function parsePx(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  const match = String(value || '').trim().match(/^(-?[\d.]+)px$/i)
  return match ? Number(match[1]) : null
}

function parseLetterSpacingDesign(value, fontSize) {
  if (value == null || value === '' || value === 'normal' || value === 'auto') return null
  const raw = String(value).trim()
  const pct = raw.match(/^(-?[\d.]+)%$/)
  if (pct) return (Number(pct[1]) / 100) * Number(fontSize || 0)
  return parsePx(raw)
}

function parseRadiusValues(value) {
  if (value == null || value === '') return null
  if (typeof value === 'number') return [value, value, value, value]
  if (typeof value === 'object' && !Array.isArray(value)) {
    const keys = ['topLeft', 'topRight', 'bottomRight', 'bottomLeft']
    const values = keys.map((key) => finite(value[key]))
    return values.every((item) => item != null) ? values : null
  }
  const raw = String(value).trim()
  if (!raw || raw.includes('/') || raw.includes('%')) return null
  const parts = raw.split(/\s+/).map((part) => {
    const px = parsePx(part)
    return px == null ? finite(part) : px
  })
  if (!parts.length || parts.some((item) => item == null)) return null
  if (parts.length === 1) return [parts[0], parts[0], parts[0], parts[0]]
  if (parts.length === 2) return [parts[0], parts[1], parts[0], parts[1]]
  if (parts.length === 3) return [parts[0], parts[1], parts[2], parts[1]]
  return parts.slice(0, 4)
}

function inferRoundedRectRadius(node) {
  if (!node || node.type !== 'PATH' || !Array.isArray(node.path) || node.path.length !== 1) return null
  const item = node.path[0] || {}
  const data = String(item.data || '').trim()
  if (!data || item.transform || !/[CQ]/i.test(data) || /[Aa]/.test(data)) return null
  const width = finite(node.layoutStyle && node.layoutStyle.width)
  const height = finite(node.layoutStyle && node.layoutStyle.height)
  if (!(width > 0) || !(height > 0)) return null

  const number = '(-?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:e[-+]?\\d+)?)'
  const move = data.match(new RegExp(`^M\\s*${number}[,\\s]+${number}`, 'i'))
  if (!move) return null
  const x = Number(move[1])
  const y = Number(move[2])
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null

  const nums = data.match(/-?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?/gi) || []
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (let index = 0; index + 1 < nums.length; index += 2) {
    const px = Number(nums[index])
    const py = Number(nums[index + 1])
    if (!Number.isFinite(px) || !Number.isFinite(py)) continue
    minX = Math.min(minX, px)
    minY = Math.min(minY, py)
    maxX = Math.max(maxX, px)
    maxY = Math.max(maxY, py)
  }
  const edgeTolerance = Math.max(0.5, Math.min(width, height) * 0.003)
  const explicitlyClosed = /[Zz]\s*$/.test(data)
  const lastX = Number(nums[nums.length - 2])
  const lastY = Number(nums[nums.length - 1])
  const geometricallyClosed = Number.isFinite(lastX) && Number.isFinite(lastY) && Math.abs(lastX - x) <= edgeTolerance && Math.abs(lastY - y) <= edgeTolerance
  if (!explicitlyClosed && !geometricallyClosed) return null
  if (
    Math.abs(minX) > edgeTolerance ||
    Math.abs(minY) > edgeTolerance ||
    Math.abs(maxX - width) > edgeTolerance ||
    Math.abs(maxY - height) > edgeTolerance
  ) return null

  let radius = null
  if (Math.abs(y) <= edgeTolerance) radius = Math.min(x, width - x)
  else if (Math.abs(x) <= edgeTolerance) radius = Math.min(y, height - y)
  else if (Math.abs(y - height) <= edgeTolerance) radius = Math.min(x, width - x)
  else if (Math.abs(x - width) <= edgeTolerance) radius = Math.min(y, height - y)
  if (!(radius > edgeTolerance) || radius > Math.min(width, height) / 2 + edgeTolerance) return null
  return round1(radius)
}

function isAxisAlignedRectPath(node) {
  if (!node || node.type !== 'PATH' || !Array.isArray(node.path) || node.path.length !== 1) return false
  const item = node.path[0] || {}
  const data = String(item.data || '').trim()
  if (!data || item.transform || /[CQAS]/i.test(data)) return false
  const tokens = data.match(/[MLHVZmlhvz]|-?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?/g) || []
  const points = []
  let index = 0
  let command = null
  let x = 0
  let y = 0
  let startX = 0
  let startY = 0
  let started = false

  const readNumber = () => {
    if (index >= tokens.length || /^[A-Za-z]$/.test(tokens[index])) return null
    const value = Number(tokens[index])
    index += 1
    return Number.isFinite(value) ? value : null
  }

  while (index < tokens.length) {
    if (/^[A-Za-z]$/.test(tokens[index])) {
      command = tokens[index]
      index += 1
    }
    if (!command) return false
    const relative = command === command.toLowerCase()
    const upper = command.toUpperCase()
    if (upper === 'Z') {
      x = startX
      y = startY
      points.push([x, y])
      command = null
      continue
    }
    if (upper === 'M' || upper === 'L') {
      const nextX = readNumber()
      const nextY = readNumber()
      if (nextX == null || nextY == null) return false
      x = relative ? x + nextX : nextX
      y = relative ? y + nextY : nextY
      if (!started) {
        startX = x
        startY = y
        started = true
      }
      points.push([x, y])
      if (upper === 'M') command = relative ? 'l' : 'L'
      continue
    }
    if (upper === 'H') {
      const nextX = readNumber()
      if (nextX == null) return false
      x = relative ? x + nextX : nextX
      points.push([x, y])
      continue
    }
    if (upper === 'V') {
      const nextY = readNumber()
      if (nextY == null) return false
      y = relative ? y + nextY : nextY
      points.push([x, y])
      continue
    }
    return false
  }

  if (points.length < 5) return false
  const xs = points.map((point) => point[0])
  const ys = points.map((point) => point[1])
  const minX = Math.min(...xs)
  const maxX = Math.max(...xs)
  const minY = Math.min(...ys)
  const maxY = Math.max(...ys)
  const width = finite(node.layoutStyle && node.layoutStyle.width)
  const height = finite(node.layoutStyle && node.layoutStyle.height)
  const tolerance = Math.max(0.5, Math.min(width || maxX - minX, height || maxY - minY) * 0.003)
  if (!(maxX - minX > 0) || !(maxY - minY > 0)) return false
  if (width != null && Math.abs(maxX - minX - width) > tolerance) return false
  if (height != null && Math.abs(maxY - minY - height) > tolerance) return false

  const corners = new Set()
  for (const [pointX, pointY] of points) {
    const onLeft = Math.abs(pointX - minX) <= tolerance
    const onRight = Math.abs(pointX - maxX) <= tolerance
    const onTop = Math.abs(pointY - minY) <= tolerance
    const onBottom = Math.abs(pointY - maxY) <= tolerance
    if (!(onLeft || onRight) || !(onTop || onBottom)) return false
    corners.add(`${onRight ? 1 : 0}:${onBottom ? 1 : 0}`)
  }
  return corners.size === 4
}

function pathCanUseCss(node) {
  if (!node || node.type !== 'PATH' || !Array.isArray(node.path) || node.path.length !== 1) return false
  const item = node.path[0] || {}
  const data = String(item.data || '')
  if (!data || item.transform) return false
  const subpaths = (data.match(/[Mm]/g) || []).length
  if (subpaths !== 1) return false
  if (inferRoundedRectRadius(node) != null) return true
  return isAxisAlignedRectPath(node)
}

function markInScope(mark, scopeMarks) {
  if (!scopeMarks || !scopeMarks.length) return true
  return scopeMarks.some((scope) => mark === scope || String(mark).startsWith(`${scope}.`))
}

function indexMarkedNodes(nodes) {
  const byId = new Map()
  const byMark = new Map()
  ;(function walk(items) {
    for (const node of items || []) {
      if (node.id) byId.set(String(node.id), node)
      if (node._layoutMark) byMark.set(String(node._layoutMark), node)
      walk(node.children || [])
    }
  })(nodes || [])
  return { byId, byMark }
}

function normalizeSvgPathData(value) {
  return String(value || '')
    .trim()
    .replace(/,/g, ' ')
    .replace(/\s+/g, ' ')
    .replace(/\s*([A-Za-z])\s*/g, '$1')
}

function svgPathFingerprint(value) {
  const input = normalizeSvgPathData(value)
  if (!input) return null
  let hash = 0x811c9dc5
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index)
    hash = Math.imul(hash, 0x01000193)
  }
  return `fnv1a-${(hash >>> 0).toString(16).padStart(8, '0')}`
}

function buildAssetMap(nodes, manifest) {
  const { byId } = indexMarkedNodes(nodes)
  const byNodeId = new Map()
  for (const item of (manifest && manifest.items) || []) {
    const refs = Array.isArray(item.refs) && item.refs.length
      ? item.refs
      : [{ nodeId: item.nodeId }]
    for (const ref of refs) {
      const nodeId = ref && ref.nodeId ? String(ref.nodeId) : ''
      const node = byId.get(nodeId)
      if (!node || !node._layoutMark) continue
      if (!byNodeId.has(nodeId)) byNodeId.set(nodeId, [])
      byNodeId.get(nodeId).push({
        assetId: item.assetId,
        kind: item.kind,
        source: item.source,
        status: item.status,
        fileBase: item.fileBase,
        ext: item.ext,
        localPath: item.localPath,
        pathHash: item.pathHash || null,
        contentSha256: item.contentSha256 || null,
        pathFingerprint: svgPathFingerprint(
          (node.path || []).map((pathItem) => pathItem && pathItem.data).filter(Boolean).join(' ')
        ),
        bbox: item.bbox || null,
        note: item.note || null,
      })
    }
  }
  return byNodeId
}

function collectRenderTargets(dsl, nodes, assetManifest, scopeMarks) {
  const styles = (dsl && dsl.styles) || {}
  const { paintMap, fontMap, effectMap } = buildStyleMaps(styles)
  const assetMap = buildAssetMap(nodes, assetManifest)
  const targets = []

  ;(function walk(items) {
    for (const node of items || []) {
      const mark = node._layoutMark || ''
      if (mark && markInScope(mark, scopeMarks)) {
        const spec = nodeVisualSpec(node, paintMap, fontMap, effectMap)
        const inferredRadius = spec.borderRadius == null ? inferRoundedRectRadius(node) : null
        const assets = (assetMap.get(String(node.id || '')) || []).filter((asset) => (
          asset.kind === 'raster' ||
          asset.source === 'dsl-path-complex' ||
          !pathCanUseCss(node)
        ))
        const rawVisualFill = spec.fill || spec.pathFill || null
        const visualLayers = partitionCssLayers(rawVisualFill)
        const visualFill = visualLayers.valid.length
          ? visualLayers.valid.join(', ')
          : rawVisualFill
        const hasExpected = Boolean(
          spec.text || visualFill || spec.stroke || spec.effect ||
          spec.borderRadius != null || inferredRadius != null ||
          spec.opacity != null || assets.length
        )
        if (hasExpected) {
          targets.push({
            layoutMark: mark,
            nodeId: node.id || null,
            name: node.name || '',
            type: node.type || '',
            bbox: spec.bbox,
            expected: {
              text: spec.text || null,
              textSpans: spec.textSpans || [],
              textColor: spec.textColor || null,
              visualFill,
              invalidVisualLayers: visualLayers.invalid,
              stroke: spec.stroke || null,
              effect: spec.effect || null,
              borderRadius: spec.borderRadius != null ? spec.borderRadius : inferredRadius,
              borderRadiusSource: spec.borderRadius != null
                ? 'dsl-border-radius'
                : inferredRadius != null
                  ? 'rounded-rect-path'
                  : null,
              opacity: spec.opacity,
              assets,
            },
          })
        }
      }
      walk(node.children || [])
    }
  })(nodes || [])
  return targets
}

function compactCss(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/\s+/g, '')
    .replace(/0px/g, '0')
}

function basenameWithoutExt(filePath) {
  const base = path.basename(String(filePath || ''))
  return base.replace(/\.[^.]+$/, '')
}

function assetCandidateMatches(asset, candidate) {
  if (!asset || !candidate) return false
  if (candidate.assetId && candidate.assetId === asset.assetId) return true
  if (
    asset.pathFingerprint &&
    candidate.pathFingerprint &&
    asset.pathFingerprint === candidate.pathFingerprint
  ) return true
  if (
    asset.contentSha256 &&
    candidate.contentSha256 &&
    String(asset.contentSha256).toLowerCase() === String(candidate.contentSha256).toLowerCase()
  ) return true
  let haystack = String(candidate.url || '')
  try { haystack = decodeURIComponent(haystack) } catch (_) { /* keep raw URL */ }
  haystack = haystack.toLowerCase()
  const names = [asset.fileBase, basenameWithoutExt(asset.localPath)]
    .filter(Boolean)
    .map((name) => String(name).toLowerCase())
  return names.some((name) => haystack.includes(name))
}

function evaluateRenderTargets(targets, measurements, options = {}) {
  const scale = Number(options.scaleCssToDesign) || 1
  const tolerances = { ...DEFAULT_TOLERANCES, ...(options.tolerances || {}) }
  const fontFaces = Array.isArray(options.fontFaces) ? options.fontFaces : []
  const byMark = new Map((measurements || []).map((item) => [item.layoutMark, item]))
  const violations = []
  const warnings = []
  const items = []

  const add = (target, kind, category, severity, expected, actual, extra = {}) => {
    const entry = {
      layoutMark: target.layoutMark,
      nodeId: target.nodeId,
      name: target.name,
      kind,
      category,
      severity,
      blocking: severity === 'hard',
      expected,
      actual,
      repairClass: extra.repairClass || 'implementation',
      ...extra,
    }
    if (severity === 'hard') violations.push(entry)
    else warnings.push(entry)
  }

  const compareLength = (target, kind, expected, actualCss, tolerance, category = 'typography') => {
    const actual = finite(actualCss)
    if (expected == null || actual == null) return
    const actualDesign = actual * scale
    const diffPx = round1(actualDesign - expected)
    if (Math.abs(diffPx) > tolerance) {
      add(target, kind, category, 'hard', expected, round1(actualDesign), { diffPx })
    }
  }

  for (const target of targets || []) {
    const measured = byMark.get(target.layoutMark)
    const item = {
      layoutMark: target.layoutMark,
      nodeId: target.nodeId,
      name: target.name,
      type: target.type,
      expected: target.expected,
      matched: Boolean(measured && measured.matched),
      measured: measured || null,
    }
    items.push(item)

    if (!measured || !measured.matched) {
      add(target, 'render-node-missing', 'render-integrity', 'hard', '带 data-d2c-mark 的真实渲染节点', 'DOM 未命中')
      continue
    }

    // 启发式回退命中的元素只证明「找到一个像它的节点」，不能作为样式契约的证据：
    // 拿猜中元素去比字体/颜色/阴影会制造假违规。降级为 warning 并跳过全部样式判定。
    if (measured.fallbackMatched) {
      item.matchConfidence = 'guess'
      item.method = 'heuristic-fallback'
      add(
        target,
        'low-confidence-match',
        'render-integrity',
        'warning',
        `节点 ${target.layoutMark} 带 data-d2c-mark`,
        `由启发式回退匹配命中 <${String(measured.tagName || '?').toLowerCase()}>，未做样式判定`,
        {
          repairClass: 'source-material',
          repairHint: '给该节点补 data-d2c-mark 后重跑；否则其视觉契约处于未验证状态。',
        }
      )
      continue
    }

    const expected = target.expected || {}
    const normalized = measured.normalizedExpected || {}
    if (
      Array.isArray(expected.invalidVisualLayers) &&
      expected.invalidVisualLayers.length &&
      expected.visualFill &&
      !containsInvalidCssToken(expected.visualFill)
    ) {
      add(
        target,
        'dsl-style-layer-unverifiable',
        'source-material',
        'warning',
        '所有 DSL paint layer 都可解析',
        expected.invalidVisualLayers,
        { repairClass: 'source-material' }
      )
    }
    const cssValues = [expected.visualFill, expected.effect]
    if (cssValues.some(containsInvalidCssToken) || normalized.backgroundValid === false || normalized.effectValid === false) {
      add(
        target,
        'dsl-style-invalid',
        'source-material',
        'hard',
        cssValues.filter(Boolean).join(' | '),
        normalized.invalidReason || '浏览器拒绝期望 CSS',
        { repairClass: 'source-material' }
      )
    }
    // 无法归类的 effect 属性不算缺陷：降级为 warning，避免使用者白跑一轮去猜。
    const unsupportedEffects = normalized.unsupportedEffects || []
    if (unsupportedEffects.length) {
      add(
        target,
        'unsupported-effect',
        'effect',
        'warning',
        '可逐项核对的 CSS 效果属性（box-shadow / backdrop-filter / filter / text-shadow）',
        unsupportedEffects,
        { repairClass: 'source-material', repairHint: '把该效果层改为受支持的 CSS 属性，或从 render 契约中排除。' }
      )
    }

    const expectedAssets = expected.assets || []
    const renderedAssets = measured.assetCandidates || []
    const matchedAssetCandidates = []
    for (const asset of expectedAssets) {
      if (!['downloaded', 'exported-svg'].includes(asset.status)) {
        add(
          target,
          'asset-unmaterialized',
          'asset',
          'hard',
          `${asset.assetId} 已落盘`,
          `${asset.status || 'missing'}${asset.note ? `: ${asset.note}` : ''}`,
          { repairClass: 'source-material', assetId: asset.assetId }
        )
        continue
      }
      const candidate = renderedAssets.find((entry) => assetCandidateMatches(asset, entry))
      if (!candidate) {
        add(
          target,
          'asset-reference-mismatch',
          'asset',
          'hard',
          `${asset.fileBase}${asset.ext || ''}（${asset.assetId}）`,
          renderedAssets.length
            ? renderedAssets.map((entry) => entry.url || entry.assetId || entry.tagName).join(', ')
            : '节点未使用图片/SVG 资源',
          { assetId: asset.assetId }
        )
      } else if (candidate.loadOk === false) {
        add(target, 'asset-load-failed', 'asset', 'hard', candidate.url || asset.fileBase, candidate.loadError || '资源未完成加载', { assetId: asset.assetId })
      } else {
        matchedAssetCandidates.push(candidate)
        if (expected.visualFill && Array.isArray(candidate.svgFills) && candidate.svgFills.length === 1) {
          const fill = compareColors(expected.visualFill, candidate.svgFills[0], tolerances)
          if (fill.comparable && !fill.equal) {
            add(target, 'asset-fill-color-mismatch', 'asset', 'hard', expected.visualFill, candidate.svgFills[0], {
              assetId: asset.assetId,
              colorDelta: fill.delta,
            })
          }
        }
      }
    }

    const transparentAsset = matchedAssetCandidates.find((candidate) => {
      const embeddedOpacity = finite(candidate.svgOpacity)
      return candidate.hasTransparentPixels === true ||
        candidate.svgHasCutout === true ||
        (embeddedOpacity != null && embeddedOpacity < 1 - tolerances.opacity)
    })
    const cssBackground = parseCssColor(measured.style && measured.style.backgroundColor)
    if (transparentAsset && cssBackground && cssBackground.a > tolerances.alpha) {
      add(
        target,
        'asset-background-contamination',
        'asset',
        'hard',
        {
          backgroundColor: 'transparent',
          paintSource: 'matched SVG/raster asset only',
        },
        {
          backgroundColor: measured.style.backgroundColor,
          svgOpacity: finite(transparentAsset.svgOpacity),
          svgHasCutout: transparentAsset.svgHasCutout === true,
          transparentPixelRatio: finite(transparentAsset.transparentPixelRatio),
        },
        {
          assetId: expectedAssets[0] && expectedAssets[0].assetId,
          repairHint: '移除同一 data-d2c-mark 节点上的 CSS background/background-color；资源已承载 PATH fill、孔洞与透明度。',
        }
      )
    }

    const assetOwnsVisual = expectedAssets.some((asset) => ['downloaded', 'exported-svg'].includes(asset.status))
    const compareSurfaceFill = String(target.type || '').toUpperCase() !== 'TEXT'
    if (expected.visualFill && compareSurfaceFill && !assetOwnsVisual && normalized.backgroundValid !== false) {
      const expectedImage = normalized.backgroundImage || 'none'
      const actualImage = measured.style && measured.style.backgroundImage
      if (expectedImage !== 'none') {
        const partialSource = Array.isArray(expected.invalidVisualLayers) && expected.invalidVisualLayers.length > 0
        const expectedLayers = splitTopLevelCssLayers(expectedImage).map(compactCss)
        const actualLayers = splitTopLevelCssLayers(actualImage).map(compactCss)
        const validLayersPresent = expectedLayers.every((layer) => actualLayers.includes(layer))
        const matches = partialSource
          ? validLayersPresent
          : compactCss(expectedImage) === compactCss(actualImage)
        if (!matches) {
          add(target, 'background-image-mismatch', 'paint', 'hard', expectedImage, actualImage || 'none')
        }
      } else {
        const svgPaintNode = /^(?:svg|path|rect|circle|ellipse|polygon|polyline|line|g)$/i.test(
          String(measured.tagName || '')
        )
        const actualColor = measured.style && (svgPaintNode && measured.style.svgFill && measured.style.svgFill !== 'none'
          ? measured.style.svgFill
          : measured.style.backgroundColor)
        const result = compareColors(normalized.backgroundColor || expected.visualFill, actualColor, tolerances)
        if (!result.comparable) {
          add(target, 'fill-color-unverifiable', 'paint', 'warning', expected.visualFill, actualColor || 'none')
        } else if (!result.equal) {
          add(target, 'fill-color-mismatch', 'paint', 'hard', normalized.backgroundColor || expected.visualFill, actualColor, { colorDelta: result.delta })
        }
      }
    }

    const radii = parseRadiusValues(expected.borderRadius)
    if (expected.borderRadius != null && !assetOwnsVisual) {
      if (!radii) {
        add(target, 'border-radius-unverifiable', 'shape', 'warning', expected.borderRadius, measured.style && measured.style.borderRadius)
      } else {
        const actual = (measured.style && measured.style.borderRadii) || []
        const actualDesign = []
        const cornerDiffs = []
        radii.forEach((radius, index) => {
          let actualPx = parsePx(actual[index])
          const percent = String(actual[index] || '').match(/^([\d.]+)%$/)
          if (actualPx == null && percent && measured.style) {
            actualPx = Math.min(Number(measured.style.rectWidth) || 0, Number(measured.style.rectHeight) || 0) * Number(percent[1]) / 100
          }
          if (actualPx == null) {
            actualDesign.push(actual[index] || 'none')
            cornerDiffs.push({ cornerIndex: index, expected: radius, actual: actual[index] || 'none', diffPx: null })
            return
          }
          const scaledActual = round1(actualPx * scale)
          const diffPx = round1(scaledActual - radius)
          actualDesign.push(scaledActual)
          if (Math.abs(diffPx) > tolerances.radiusPx) {
            cornerDiffs.push({ cornerIndex: index, expected: radius, actual: scaledActual, diffPx })
          }
        })
        if (cornerDiffs.length) {
          const numericDiffs = cornerDiffs
            .filter((item) => item.diffPx != null)
            .map((item) => Math.abs(Number(item.diffPx)))
            .filter(Number.isFinite)
          add(target, 'border-radius-mismatch', 'shape', 'hard', radii, actualDesign, {
            diffPx: numericDiffs.length ? Math.max(...numericDiffs) : null,
            cornerDiffs,
            radiusSource: expected.borderRadiusSource,
          })
        }
      }
    }

    if (expected.stroke && !assetOwnsVisual) {
      const expectedWidth = parsePx(String(expected.stroke.width || '1').match(/px$/) ? expected.stroke.width : `${expected.stroke.width || 1}px`)
      const actualWidth = parsePx(measured.style && measured.style.borderTopWidth)
      if (expectedWidth != null && actualWidth != null) {
        const diffPx = round1(actualWidth * scale - expectedWidth)
        if (Math.abs(diffPx) > tolerances.borderWidthPx) {
          add(target, 'border-width-mismatch', 'stroke', 'hard', expectedWidth, round1(actualWidth * scale), { diffPx })
        }
      }
      const strokeColor = compareColors(expected.stroke.color, measured.style && measured.style.borderTopColor, tolerances)
      if (strokeColor.comparable && !strokeColor.equal) {
        add(target, 'border-color-mismatch', 'stroke', 'hard', expected.stroke.color, measured.style.borderTopColor, { colorDelta: strokeColor.delta })
      }
    }

    if (expected.opacity != null) {
      const opacityCandidate = matchedAssetCandidates.find((candidate) => finite(candidate.svgOpacity) != null)
      const assetOpacity = opacityCandidate ? finite(opacityCandidate.svgOpacity) : null
      const wrapperOpacity = finite(measured.style && measured.style.opacity)
      const markerOwnsInlineSvgOpacity = /^(?:svg|path|rect|circle|ellipse|polygon|polyline|line|g)$/i.test(
        String(measured.tagName || '')
      )
      const actualOpacity = assetOpacity != null
        ? assetOpacity * (markerOwnsInlineSvgOpacity ? 1 : (wrapperOpacity == null ? 1 : wrapperOpacity))
        : wrapperOpacity
      if (actualOpacity == null || Math.abs(actualOpacity - Number(expected.opacity)) > tolerances.opacity) {
        add(target, 'opacity-mismatch', 'paint', 'hard', Number(expected.opacity), actualOpacity, {
          assetOpacity,
          wrapperOpacity,
          repairHint: assetOpacity != null
            ? 'SVG 已内置 opacity 时，禁止在承载节点重复应用同一 opacity；以有效乘积匹配 DSL。'
            : '按 DSL 修复承载节点的 computed opacity。',
        })
      }
    }

    if (expected.effect && normalized.effectValid !== false) {
      for (const declaration of normalized.effectList || []) {
        if (!declaration || !declaration.styleKey) continue
        const actual = measured.style ? measured.style[declaration.styleKey] : undefined
        if (compactCss(declaration.value) !== compactCss(actual)) {
          add(
            target,
            declaration.property === 'box-shadow' ? 'box-shadow-mismatch' : `${declaration.property}-mismatch`,
            'effect',
            'hard',
            declaration.value,
            actual == null ? 'computed style 无该属性' : actual
          )
        }
      }
    }

    const expectedSpans = expected.textSpans || []
    const measuredSpans = measured.textSpans || []
    for (let index = 0; index < expectedSpans.length; index += 1) {
      const expectedSpan = expectedSpans[index]
      const actualSpan = measuredSpans.find((span) => Number(span.index) === index)
      if (!actualSpan || !actualSpan.matched) {
        add(target, 'text-span-missing', 'typography', 'hard', `span ${index}: ${expectedSpan.text}`, 'DOM 未命中对应 data-span-index')
        continue
      }
      const style = actualSpan.style || {}
      compareLength(target, 'font-size-mismatch', finite(expectedSpan.fontSize), finite(style.fontSize), tolerances.fontSizePx)

      if (expectedSpan.fontFamily && !familiesEquivalent(expectedSpan.fontFamily, style.fontFamily)) {
        add(target, 'font-family-mismatch', 'typography', 'hard', expectedSpan.fontFamily, style.fontFamily)
      }
      const expectedWeight = normalizeWeight(expectedSpan.fontWeight)
      const actualSemanticWeight = semanticFontWeight(style.fontFamily, style.fontWeight)
      if (expectedWeight !== actualSemanticWeight) {
        add(target, 'font-weight-mismatch', 'typography', 'hard', expectedWeight, actualSemanticWeight, { computedWeight: style.fontWeight })
      }
      const expectedFontStyle = expectedSpan.fontStyle || 'normal'
      const actualFontStyle = /italic|oblique/i.test(String(style.fontStyle || 'normal')) ? 'italic' : 'normal'
      if (expectedFontStyle !== actualFontStyle) {
        add(target, 'font-style-mismatch', 'typography', 'hard', expectedFontStyle, actualFontStyle)
      }

      const firstFamily = splitFontFamilies(style.fontFamily)[0] || ''
      const familyWeight = inferWeightFromFontToken(firstFamily)
      const faceRoot = fontFamilyRoot(firstFamily)
      const matchingFaces = (measured.fontFaces || fontFaces).filter((face) => fontFamilyRoot(face.family) === faceRoot)
      if (familyWeight && normalizeWeight(style.fontWeight) > 400 && matchingFaces.length && !matchingFaces.some((face) => fontFaceSupportsWeight(face.weight, normalizeWeight(style.fontWeight)))) {
        add(
          target,
          'font-synthetic-weight',
          'typography',
          'hard',
          `${firstFamily} 自带 ${familyWeight} 字重时不再叠加浏览器合成字重`,
          `computed ${style.fontWeight}; @font-face=${matchingFaces.map((face) => face.weight).join(',')}`
        )
      }
      if (!isSystemFontFamily(expectedSpan.fontFamily) && !matchingFaces.some((face) => face.status === 'loaded')) {
        if (actualSpan.fontCheck === false || actualSpan.fontPrimaryDiffersFromFallback === false) {
          add(target, 'font-not-loaded', 'typography', 'hard', expectedSpan.fontFamily, actualSpan.fontCheck === false ? 'document.fonts.check=false' : '主字体与移除主字体后的字形测量完全相同')
        } else {
          add(target, 'font-load-unverified', 'typography', 'warning', expectedSpan.fontFamily, '未找到已加载的匹配 FontFace')
        }
      }

      if (expectedSpan.lineHeightSource === 'explicit') {
        compareLength(target, 'line-height-mismatch', finite(expectedSpan.lineHeightPx), finite(style.lineHeight), tolerances.lineHeightPx)
      }
      const expectedLetterSpacing = parseLetterSpacingDesign(expectedSpan.letterSpacing, expectedSpan.fontSize)
      if (expectedLetterSpacing != null) {
        const actualLetterSpacing = style.letterSpacing === 'normal' ? 0 : parsePx(style.letterSpacing)
        compareLength(target, 'letter-spacing-mismatch', expectedLetterSpacing, actualLetterSpacing, tolerances.letterSpacingPx)
      }

      if (expectedSpan.color) {
        const color = compareColors(expectedSpan.color, style.color, tolerances)
        if (color.comparable && !color.equal) {
          add(target, 'text-color-mismatch', 'typography', 'hard', expectedSpan.color, style.color, { colorDelta: color.delta })
        }
      }
      const expectedDecoration = expectedSpan.textDecoration || 'none'
      const actualDecoration = style.effectiveTextDecoration || style.textDecorationLine || 'none'
      if (compactCss(expectedDecoration) !== compactCss(actualDecoration)) {
        add(target, 'text-decoration-mismatch', 'typography', 'hard', expectedDecoration, actualDecoration)
      }
    }

    if (measured.clipping && measured.clipping.clipped) {
      add(target, 'text-clipped', 'text-integrity', 'hard', '全部字形位于文本容器内', measured.clipping.reason || '字形/scroll 尺寸超出 overflow 裁切容器')
    }
  }

  return {
    items,
    violations,
    warnings,
    hardViolationCount: violations.length,
    warningCount: warnings.length,
    sourceMaterialViolationCount: violations.filter((item) => item.repairClass === 'source-material').length,
  }
}

module.exports = {
  DEFAULT_TOLERANCES,
  assetCandidateMatches,
  collectRenderTargets,
  compareColors,
  containsInvalidCssToken,
  evaluateRenderTargets,
  familiesEquivalent,
  fontFamilyRoot,
  inferRoundedRectRadius,
  inferWeightFromFontToken,
  isAxisAlignedRectPath,
  parseCssColor,
  parseLetterSpacingDesign,
  partitionCssLayers,
  parseRadiusValues,
  pathCanUseCss,
  semanticFontWeight,
  splitFontFamilies,
  splitTopLevelCssLayers,
  svgPathFingerprint,
}
