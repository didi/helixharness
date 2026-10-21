/**
 * MasterGo DSL 样式解析（fills / strokes / effects / bbox / gap / 文本属性）
 * 供 filter-dsl.cjs 与 dsl-layout-core.cjs 共用。
 */

function round1(n) {
  return Math.round(Number(n || 0) * 10) / 10
}

function normalizeFontFamily(family) {
  const raw = String(family || '').trim()
  if (!raw) return 'PingFang SC, PingFangSC, sans-serif'
  if (/^PingFang/i.test(raw)) return 'PingFang SC, PingFangSC, sans-serif'
  if (/^DIN/i.test(raw)) return `"${raw}", PingFang SC, sans-serif`
  if (raw.includes(',')) return raw
  return `"${raw}", PingFang SC, sans-serif`
}

function inferWeightFromFamily(family) {
  const f = String(family || '')
  if (/Black|Heavy|900/i.test(f)) return 900
  if (/Bold|700/i.test(f)) return 700
  if (/Semibold|600|中粗/i.test(f)) return 600
  if (/Medium|500/i.test(f)) return 500
  if (/Regular|400/i.test(f)) return 400
  if (/Light|300/i.test(f)) return 300
  return null
}

function parseNumericWeight(weight, family, styleStr) {
  if (weight != null && weight !== '') {
    const n = Number(weight)
    if (!Number.isNaN(n) && n > 0) return n
  }
  const fromFamily = inferWeightFromFamily(family)
  if (fromFamily != null) return fromFamily
  const style = String(styleStr || '')
  if (/bold|Bold|粗体|加粗|中粗|semibold|SemiBold|Black|Heavy/i.test(style)) {
    if (/Black|Heavy|900/i.test(style)) return 900
    if (/semibold|SemiBold|600|中粗/i.test(style)) return 600
    return 700
  }
  if (/Medium|500/.test(style)) return 500
  return 400
}

function parseLetterSpacing(raw) {
  if (raw == null || raw === '' || raw === 'auto') return null
  const s = String(raw).trim()
  if (s === 'auto') return null
  if (/^-?\d+(\.\d+)?px$/i.test(s)) return s
  if (/^-?\d+(\.\d+)?%$/.test(s)) return s
  const n = Number(s)
  if (!Number.isNaN(n)) return `${n}px`
  return null
}

function parseLineHeightPx(lineHeight, layoutHeight, fontSize) {
  const lh = lineHeight == null ? '' : String(lineHeight).trim()
  if (lh && lh !== 'auto' && lh !== '-1') {
    const n = Number(lh)
    if (!Number.isNaN(n) && n > 0) return round1(n)
  }
  if (layoutHeight > 0) return round1(layoutHeight)
  if (fontSize > 0) return round1(Math.round(fontSize * 1.4))
  return null
}

function lineHeightSource(lineHeight) {
  const raw = lineHeight == null ? '' : String(lineHeight).trim()
  return raw && raw !== 'auto' && raw !== '-1' && Number.isFinite(Number(raw))
    ? 'explicit'
    : 'bbox-fallback'
}

function normalizeTextDecoration(value) {
  const raw = String(value || 'none').trim().toLowerCase()
  if (raw.includes('line-through') || raw.includes('strikethrough')) return 'line-through'
  if (raw.includes('underline')) return 'underline'
  if (raw.includes('overline')) return 'overline'
  return 'none'
}

function normalizeFontStyle(value) {
  const raw = String(value || '').toLowerCase()
  return /italic|oblique/.test(raw) ? 'italic' : 'normal'
}

function parseFontRecord(fv, layoutHeight) {
  if (!fv) return null
  const fontSize = round1(Number(fv.size) || 0)
  const fontWeight = parseNumericWeight(fv.weight, fv.family, fv.style)
  const lineHeightPx = parseLineHeightPx(fv.lineHeight, layoutHeight, fontSize)
  const letterSpacing = parseLetterSpacing(fv.letterSpacing)
  return {
    fontSize,
    fontWeight,
    lineHeightPx,
    lineHeightSource: lineHeightSource(fv.lineHeight),
    letterSpacing,
    fontFamily: normalizeFontFamily(fv.family),
    fontFamilyRaw: fv.family || '',
    textDecoration: normalizeTextDecoration(fv.decoration),
    fontStyle: normalizeFontStyle(fv.style),
  }
}

function buildStyleMaps(styles) {
  const paintMap = {}
  const fontMap = {}
  const effectMap = {}
  if (!styles || typeof styles !== 'object') {
    return { paintMap, fontMap, effectMap }
  }

  for (const [k, v] of Object.entries(styles)) {
    if (k.startsWith('paint_')) {
      const vals = v && v.value !== undefined ? v.value : []
      paintMap[k] = Array.isArray(vals)
        ? vals.length === 1
          ? vals[0]
          : vals.length
          ? vals
          : null
        : vals
    } else if (k.startsWith('font_')) {
      const fv = v && v.value
      if (fv) fontMap[k] = fv
    } else if (k.startsWith('effect_')) {
      const vals = v && v.value !== undefined ? v.value : []
      effectMap[k] = Array.isArray(vals) ? vals : vals ? [vals] : []
    }
  }
  return { paintMap, fontMap, effectMap }
}

function resolvePaint(paintMap, ref) {
  if (!ref) return null
  const val = paintMap[ref]
  if (Array.isArray(val)) return val.length === 1 ? val[0] : val
  return val
}

function resolveFontRecord(fontMap, ref, layoutHeight) {
  if (!ref) return null
  return parseFontRecord(fontMap[ref], layoutHeight)
}

/**
 * DSL effect 层允许落到的 CSS 属性。effect 不等于 box-shadow：投影、背景模糊
 * （backdrop-filter）、滤镜（filter）与文字阴影语义不同，混为一谈会生成非法 CSS，
 * 也会让 render 自检把可核对的属性误判成「浏览器拒绝的期望值」。
 */
const EFFECT_CSS_PROPERTIES = ['box-shadow', 'backdrop-filter', 'filter', 'text-shadow']

const EFFECT_PROPERTY_ALIASES = {
  'box-shadow': 'box-shadow',
  boxshadow: 'box-shadow',
  shadow: 'box-shadow',
  'drop-shadow': 'filter',
  dropshadow: 'filter',
  'backdrop-filter': 'backdrop-filter',
  backdropfilter: 'backdrop-filter',
  'backdrop-blur': 'backdrop-filter',
  backdropblur: 'backdrop-filter',
  'background-blur': 'backdrop-filter',
  backgroundblur: 'backdrop-filter',
  filter: 'filter',
  'text-shadow': 'text-shadow',
  textshadow: 'text-shadow',
}

function normalizeEffectProperty(raw) {
  if (raw === null || raw === undefined) return null
  const key = String(raw).trim().toLowerCase()
  if (!key) return null
  return EFFECT_PROPERTY_ALIASES[key] || (EFFECT_CSS_PROPERTIES.includes(key) ? key : null)
}

function shadowGeometryToCss(e) {
  const x = e.offsetX ?? e.x ?? 0
  const y = e.offsetY ?? e.y ?? 0
  const blur = e.blur ?? e.radius ?? 0
  const spread = e.spread ?? 0
  const color = e.color || 'rgba(0,0,0,0.1)'
  const inset = e.inset ? 'inset ' : ''
  return `${inset}${x}px ${y}px ${blur}px ${spread}px ${color}`
}

/**
 * 把单个 DSL effect 条目解析成 { property, value }。
 * 无法归类时返回 { unsupported: true, raw }，由调用方降级为 warning，
 * 绝不猜测成 box-shadow 后当成硬违规。
 */
function parseEffectDeclarations(item) {
  if (!item) return null
  if (typeof item === 'string') {
    const raw = item.trim()
    if (!raw) return null
    const match = /^([a-z][a-z-]*)\s*:\s*(.+)$/i.exec(raw)
    if (!match) return { property: 'box-shadow', value: raw }
    const property = normalizeEffectProperty(match[1])
    if (!property) return { unsupported: true, raw, reason: `未知 effect 属性 ${match[1]}` }
    return { property, value: match[2].trim() }
  }
  if (typeof item !== 'object') return null

  const hint = item.property ?? item.cssProperty ?? item.type ?? item.kind ?? item.name
  const hinted = normalizeEffectProperty(hint)
  if (hinted) {
    const value = item.value ?? item.css ?? item.blur
    if (typeof value === 'string' && /[a-z(]/i.test(value)) return { property: hinted, value: value.trim() }
    if (hinted === 'box-shadow') return { property: hinted, value: shadowGeometryToCss(item) }
  } else if (hint !== null && hint !== undefined && String(hint).trim()) {
    return { unsupported: true, raw: `${hint}: ${item.value ?? ''}`.trim(), reason: `未知 effect 类型 ${hint}` }
  }
  if (item.value !== undefined && typeof item.value === 'string' && /^[a-z-]+\s*:/i.test(item.value.trim())) {
    return parseEffectDeclarations(item.value.trim())
  }
  return { property: 'box-shadow', value: shadowGeometryToCss(item) }
}

/**
 * effect 展示字符串 → 声明列表。与 resolveEffectCss 的输出互为逆运算，
 * 供 render 自检按属性逐个核对 computed style。
 */
function splitEffectCss(value) {
  if (!value) return []
  const out = []
  const segments = String(value).split(';').map((segment) => segment.trim()).filter(Boolean)
  for (const segment of segments) {
    const match = /^([a-z][a-z-]*)\s*:\s*(.+)$/i.exec(segment)
    if (!match) {
      out.push({ property: 'box-shadow', value: segment, declared: false })
      continue
    }
    const property = normalizeEffectProperty(match[1])
    if (!property) {
      out.push({ unsupported: true, raw: segment, reason: `未知 effect 属性 ${match[1]}` })
      continue
    }
    out.push({ property, value: match[2].trim(), declared: true })
  }
  return out
}

/** CSS 属性名 → CSSStyleDeclaration / computed style 的驼峰键。 */
const EFFECT_STYLE_KEYS = {
  'box-shadow': 'boxShadow',
  'backdrop-filter': 'backdropFilter',
  filter: 'filter',
  'text-shadow': 'textShadow',
}

function resolveEffectDeclarations(effectMap, ref) {
  const declarations = []
  const unsupported = []
  if (!ref) return { declarations, unsupported }
  const items = (effectMap && effectMap[ref]) || []
  for (const item of items) {
    const parsed = parseEffectDeclarations(item)
    if (!parsed) continue
    if (parsed.unsupported) {
      unsupported.push({ value: parsed.raw, reason: parsed.reason })
      continue
    }
    declarations.push({ property: parsed.property, value: parsed.value })
  }
  return { declarations, unsupported }
}

/**
 * effect 的并按属性合并结果，供代码生成直接落到对应 CSS 属性上。
 */
function resolveEffectCssMap(effectMap, ref) {
  const { declarations } = resolveEffectDeclarations(effectMap, ref)
  const map = {}
  for (const declaration of declarations) {
    map[declaration.property] = map[declaration.property]
      ? `${map[declaration.property]}, ${declaration.value}`
      : declaration.value
  }
  return map
}

/**
 * effect 的展示/契约字符串。纯投影时保持历史行为（只返回阴影值）；
 * 含非投影属性时带上属性名，避免被上层当成 box-shadow。
 */
function resolveEffectCss(effectMap, ref) {
  const { declarations, unsupported } = resolveEffectDeclarations(effectMap, ref)
  if (!declarations.length) {
    return unsupported.length ? unsupported.map((entry) => entry.value).join(', ') : null
  }
  if (declarations.every((declaration) => declaration.property === 'box-shadow')) {
    return declarations.map((declaration) => declaration.value).join(', ')
  }
  return declarations.map((declaration) => `${declaration.property}: ${declaration.value}`).join('; ')
}

function paintToCssValue(fill) {
  if (!fill) return null
  if (typeof fill === 'string') return fill
  if (fill && typeof fill === 'object') {
    if (fill.url) return `url('${String(fill.url).replace(/'/g, "\\'")}')`
    if (fill.color) return fill.color
    if (Array.isArray(fill.stops) && fill.stops.length) {
      const stops = fill.stops
        .map(
          (s) =>
            `${s.color || s.fill || '#000'} ${
              s.position != null ? `${Math.round(s.position * 100)}%` : ''
            }`
        )
        .join(', ')
      const angle = fill.angle != null ? `${fill.angle}deg` : '180deg'
      return `linear-gradient(${angle}, ${stops})`
    }
  }
  if (Array.isArray(fill) && fill.length) {
    const layers = fill.map((item) => paintToCssValue(item)).filter(Boolean)
    if (!layers.length) return null
    const allBackgroundImages = layers.every((item) => /gradient\(|^url\(/i.test(String(item)))
    return allBackgroundImages ? layers.join(', ') : layers[0]
  }
  return null
}

function fillToCssProps(fill) {
  const css = {}
  const val = paintToCssValue(fill)
  if (!val) return css
  if (String(val).startsWith('url(') || String(val).includes('gradient')) {
    css.background = val
  } else {
    css.background = val
  }
  return css
}

function strokeToCssProps(node, paintMap) {
  const css = {}
  if (!node.strokeColor && node.strokeWidth == null) return css
  const color = resolvePaint(paintMap, node.strokeColor)
  const widthRaw =
    node.strokeWidth !== undefined ? String(node.strokeWidth) : '1'
  if (!color) return css

  if (/^\d+px(\s+\d+px){0,3}$/.test(widthRaw.trim())) {
    const parts = widthRaw
      .trim()
      .split(/\s+/)
      .map((p) => parseFloat(p))
    if (parts.length === 4) {
      css['border-width'] = widthRaw
      css['border-style'] = 'solid'
      css['border-color'] = color
    } else if (parts.length === 1) {
      css.border = `${parts[0]}px solid ${color}`
    } else {
      css.border = `${widthRaw} solid ${color}`
    }
  } else {
    const w = parseFloat(widthRaw) || 1
    css.border = `${w}px solid ${color}`
  }
  return css
}

function getBBox(node) {
  const layout = node.layoutStyle || node.absoluteBoundingBox || {}
  return {
    x: round1(layout.relativeX ?? layout.x ?? 0),
    y: round1(layout.relativeY ?? layout.y ?? 0),
    w: round1(layout.width ?? 0),
    h: round1(layout.height ?? 0),
  }
}

function flexGapToCss(flex) {
  if (!flex) return null
  const gap = flex.itemSpacing ?? flex.gap
  if (gap == null || gap === '') return null
  const s = String(gap).trim()
  if (/px|rem|em|%$/i.test(s)) return s
  const n = Number(gap)
  if (!Number.isNaN(n)) return `${round1(n)}px`
  return s
}

function fontRecordToCss(fontInfo) {
  if (!fontInfo) return {}
  const css = {}
  if (fontInfo.fontSize) css['font-size'] = `${fontInfo.fontSize}px`
  if (fontInfo.fontFamily) css['font-family'] = fontInfo.fontFamily
  if (fontInfo.fontWeight != null)
    css['font-weight'] = String(fontInfo.fontWeight)
  if (fontInfo.lineHeightPx != null)
    css['line-height'] = `${fontInfo.lineHeightPx}px`
  if (fontInfo.letterSpacing) css['letter-spacing'] = fontInfo.letterSpacing
  if (fontInfo.textDecoration && fontInfo.textDecoration !== 'none')
    css['text-decoration-line'] = fontInfo.textDecoration
  if (fontInfo.fontStyle && fontInfo.fontStyle !== 'normal')
    css['font-style'] = fontInfo.fontStyle
  return css
}

function resolveSpanColorAt(colorRuns, offset, paintMap) {
  if (!colorRuns.length) return null
  const run = colorRuns.find((c) => offset >= c.start && offset < c.end)
  if (!run) return null
  return paintToCssValue(resolvePaint(paintMap, run.color)) || null
}

/** 将 text[] 与 textColor[] 按字符边界切分为原子 span */
function resolveTextSpans(node, fontMap, paintMap = {}) {
  const texts = (node.text || []).filter((t) => t && t.text)
  const bbox = getBBox(node)
  if (!texts.length) return []

  let offset = 0
  const fontRuns = texts.map((t) => {
    const start = offset
    const end = offset + String(t.text).length
    offset = end
    const fontInfo = resolveFontRecord(fontMap, t.font, bbox.h)
    return {
      start,
      end,
      text: String(t.text),
      fontRef: t.font || '',
      fontSize: fontInfo?.fontSize,
      fontWeight: fontInfo?.fontWeight,
      lineHeightPx: fontInfo?.lineHeightPx,
      lineHeightSource: fontInfo?.lineHeightSource,
      letterSpacing: fontInfo?.letterSpacing,
      fontFamily: fontInfo
        ? fontInfo.fontFamilyRaw || fontInfo.fontFamily
        : undefined,
      textDecoration: fontInfo?.textDecoration || 'none',
      fontStyle: fontInfo?.fontStyle || 'normal',
    }
  })

  const totalLen = offset
  const colorRuns = (node.textColor || [])
    .map((c) => ({
      start: Number(c.start) || 0,
      end: Number(c.end) || 0,
      color: c.color,
    }))
    .filter((c) => c.end > c.start)

  const boundaries = new Set([0, totalLen])
  for (const r of fontRuns) {
    boundaries.add(r.start)
    boundaries.add(r.end)
  }
  for (const c of colorRuns) {
    boundaries.add(Math.max(0, c.start))
    boundaries.add(Math.min(totalLen, c.end))
  }

  const points = [...boundaries].sort((a, b) => a - b)
  const spans = []

  for (let i = 0; i < points.length - 1; i += 1) {
    const start = points[i]
    const end = points[i + 1]
    if (end <= start) continue
    const fontRun = fontRuns.find((r) => r.start <= start && r.end >= end)
    if (!fontRun) continue
    const text = fontRun.text.slice(start - fontRun.start, end - fontRun.start)
    if (!text) continue
    const color = resolveSpanColorAt(colorRuns, start, paintMap)

    spans.push({
      text,
      start,
      end,
      fontRef: fontRun?.fontRef || '',
      fontSize: fontRun?.fontSize,
      fontWeight: fontRun?.fontWeight,
      lineHeightPx: fontRun?.lineHeightPx,
      lineHeightSource: fontRun?.lineHeightSource,
      letterSpacing: fontRun?.letterSpacing,
      fontFamily: fontRun?.fontFamily,
      textDecoration: fontRun?.textDecoration || 'none',
      fontStyle: fontRun?.fontStyle || 'normal',
      color,
      colorRef:
        colorRuns.find((c) => start >= c.start && start < c.end)?.color || null,
    })
  }

  return spans
}

/** 字号 / 字重 / 字体 / 字间距 / 颜色 任一不同则须拆 span */
function spanStyleKey(s) {
  return [
    s.fontSize,
    s.fontWeight,
    s.fontFamily,
    s.letterSpacing || '',
    s.textDecoration || 'none',
    s.fontStyle || 'normal',
    s.fontRef || '',
    s.color || '',
  ].join('|')
}

function hasDistinctTextSpans(spans) {
  if (!spans || spans.length <= 1) return false
  const keys = new Set(spans.map(spanStyleKey))
  return keys.size > 1
}

/** TEXT 是否须拆多 span（含仅颜色分段、仅字体分段） */
function needsSplitTextSpans(node, fontMap, paintMap) {
  const spans = resolveTextSpans(node, fontMap, paintMap)
  return spans.length > 1 || hasDistinctTextSpans(spans)
}

function nodeVisualSpec(node, paintMap, fontMap, effectMap) {
  const bbox = getBBox(node)
  const spec = {
    layoutMark: node._layoutMark || '',
    name: node.name || '',
    type: node.type || '',
    bbox,
    fill: paintToCssValue(resolvePaint(paintMap, node.fill)) || null,
    stroke: null,
    effect: resolveEffectCss(effectMap, node.effect),
    gap: flexGapToCss(node.flexContainerInfo),
    borderRadius:
      node.borderRadius != null && node.borderRadius !== ''
        ? node.borderRadius
        : null,
    opacity: node.opacity != null && node.opacity !== 1 ? node.opacity : null,
  }

  if (node.strokeColor) {
    spec.stroke = {
      color: paintToCssValue(resolvePaint(paintMap, node.strokeColor)),
      width: node.strokeWidth,
    }
  }

  if (node.type === 'TEXT') {
    const textSpans = resolveTextSpans(node, fontMap, paintMap)
    const fontInfo = textSpans.length
      ? resolveFontRecord(fontMap, textSpans[0].fontRef, bbox.h)
      : null
    spec.content = textSpans.map((s) => s.text).join('')
    spec.textSpans = textSpans
    spec.isMixedText = needsSplitTextSpans(node, fontMap, paintMap)
    spec.text = fontInfo
      ? {
          fontSize: fontInfo.fontSize,
          fontWeight: fontInfo.fontWeight,
          lineHeightPx: fontInfo.lineHeightPx,
          lineHeightSource: fontInfo.lineHeightSource,
          letterSpacing: fontInfo.letterSpacing,
          fontFamily: fontInfo.fontFamilyRaw || fontInfo.fontFamily,
          textDecoration: fontInfo.textDecoration || 'none',
          fontStyle: fontInfo.fontStyle || 'normal',
        }
      : null
    const spanColors = [
      ...new Set(textSpans.map((s) => s.color).filter(Boolean)),
    ]
    if (spanColors.length === 1) {
      spec.textColor = spanColors[0]
    } else if (spanColors.length > 1) {
      spec.textColor = '(mixed)'
    } else {
      const colors = node.textColor || []
      if (colors.length) {
        spec.textColor = paintToCssValue(
          resolvePaint(paintMap, colors[0] && colors[0].color)
        )
      }
    }
    if (node.textAlign && node.textAlign !== 'left')
      spec.textAlign = node.textAlign
  }

  if (node.type === 'PATH' && (node.path || []).length) {
    const pf = resolvePaint(paintMap, node.path[0].fill)
    spec.pathFill = paintToCssValue(pf)
  }

  return spec
}

module.exports = {
  round1,
  normalizeFontFamily,
  parseFontRecord,
  parseLineHeightPx,
  lineHeightSource,
  parseLetterSpacing,
  parseNumericWeight,
  normalizeTextDecoration,
  normalizeFontStyle,
  buildStyleMaps,
  resolvePaint,
  resolveFontRecord,
  resolveEffectCss,
  resolveEffectCssMap,
  resolveEffectDeclarations,
  parseEffectDeclarations,
  splitEffectCss,
  normalizeEffectProperty,
  EFFECT_CSS_PROPERTIES,
  EFFECT_STYLE_KEYS,
  paintToCssValue,
  fillToCssProps,
  strokeToCssProps,
  getBBox,
  flexGapToCss,
  fontRecordToCss,
  resolveTextSpans,
  hasDistinctTextSpans,
  needsSplitTextSpans,
  spanStyleKey,
  nodeVisualSpec,
}
