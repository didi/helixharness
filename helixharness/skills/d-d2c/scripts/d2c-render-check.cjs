#!/usr/bin/env node
/* eslint-disable no-console */
/**
 * d2c-render-check.cjs — DSL visual contracts vs live DOM computed styles.
 *
 * This is the deterministic phase-2 companion to geometry/vertical checks. It
 * checks only high-confidence browser contracts; perceptual equivalence remains
 * the responsibility of d2c-fidelity-evaluator.
 */
const fs = require('fs')
const path = require('path')
const {
  parseArgs,
  assertUsableCaptureMaterial,
  resolveMeasurementContext,
  loadPuppeteer,
  openBrowserSession,
  closeBrowserSession,
  resolveComponentMarks,
  loadMarkedDsl,
  loadDesignFrameWidth,
  resolveDesignMeasurementFrame,
} = require('./lib/d2c-dom-measure-core.cjs')
const {
  DEFAULT_TOLERANCES,
  collectRenderTargets,
  evaluateRenderTargets,
} = require('./lib/d2c-render-check-core.cjs')
const { splitEffectCss, EFFECT_STYLE_KEYS } = require('./lib/dsl-style-core.cjs')

let getLogger = () => ({ enabled: false })
try { ({ getLogger } = require('./lib/d2c-run-log.cjs')) } catch { /* optional */ }

let appendUrlParams = (url) => url
let maskUrl = (url) => url
let applySessionState = async () => ({ applied: false })
try { ({ appendUrlParams, maskUrl, applySessionState } = require('./lib/d2c-url-params.cjs')) } catch { /* optional */ }

function readJson(filePath, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'))
  } catch (_) {
    return fallback
  }
}

/**
 * 把每个待核对节点的 expected.effect 解析成可逐属性探测的声明列表。
 * 未知属性保留下来交给浏览器侧记 warning，不再默认当成 box-shadow。
 */
function buildEffectPlans(targets) {
  const plans = {}
  for (const target of targets || []) {
    plans[target.layoutMark] = splitEffectCss(target.expected && target.expected.effect)
      .map((declaration) => ({
        property: declaration.property || null,
        styleKey: declaration.property ? EFFECT_STYLE_KEYS[declaration.property] || null : null,
        value: declaration.value || '',
        raw: declaration.raw || '',
        reason: declaration.reason || '',
      }))
  }
  return plans
}

function parseScope(args, pageDir) {
  const componentId = args.component && args.component !== true ? String(args.component) : null
  if (componentId) {
    const resolved = resolveComponentMarks(pageDir, componentId)
    if (!resolved) throw new Error(`无法解析组件 ${componentId} 的 layoutMarks`)
    return { componentId, marks: resolved.marks, source: resolved.source }
  }
  const scopeValue = args.scope && args.scope !== true
    ? args.scope
    : process.env.D2C_DESIGN_SCOPE
  if (scopeValue) {
    return {
      componentId: null,
      marks: String(scopeValue).split(',').map((item) => item.trim()).filter(Boolean),
      source: args.scope && args.scope !== true ? '--scope' : 'D2C_DESIGN_SCOPE',
    }
  }
  return { componentId: null, marks: null, source: null }
}

async function main() {
  const args = parseArgs(process.argv)

  // 默认关闭启发式回退：猜中的元素不能作为样式契约的证据。
  const fallbackRaw = args['fallback-strategy'] || process.env.D2C_FALLBACK_STRATEGY || 'off'
  const FALLBACK_STRATEGIES = { off: [], conservative: ['css', 'text-fuzzy'], aggressive: ['css', 'text-fuzzy', 'position'] }
  if (!Object.prototype.hasOwnProperty.call(FALLBACK_STRATEGIES, fallbackRaw)) {
    console.error(
      `❌ 未知的 --fallback-strategy=${fallbackRaw}；可选值：${Object.keys(FALLBACK_STRATEGIES).join(' | ')}`
    )
    process.exit(1)
  }
  const enabledLayers = FALLBACK_STRATEGIES[fallbackRaw]
  const useFallbackLayers = new Set(enabledLayers)

  const appSelector =
    args['app-selector'] && args['app-selector'] !== true
      ? args['app-selector']
      : process.env.D2C_APP_SELECTOR || null
  let url = (args.url && args.url !== true ? args.url : process.env.D2C_CAPTURE_URL) || null
  const pageDir = args['page-dir'] && args['page-dir'] !== true ? args['page-dir'] : null
  const scope = pageDir ? parseScope(args, pageDir) : { componentId: null, marks: null, source: null }
  const outDir =
    (args.out && args.out !== true ? args.out : null) ||
    (scope.componentId ? path.join(pageDir || '', 'components', scope.componentId) : pageDir)

  if (!url || !pageDir || !outDir) {
    console.error('用法: node d2c-render-check.cjs --url <full-url> --page-dir d-d2c/temp/<page-dir> --out d-d2c/temp/<page-dir>/r<N>/ [--component <id> | --scope <mark,…>]')
    process.exit(1)
  }

  assertUsableCaptureMaterial(args, outDir, 'd2c-render-check')
  url = appendUrlParams(url, args)

  const measurementContext = resolveMeasurementContext(args, outDir)
  const viewportWidth = measurementContext.viewportWidthCss
  const waitMs = parseInt(String(args.wait || '3000'), 10)
  const { dsl, nodes } = loadMarkedDsl(pageDir)
  const fullDesignFrameWidth = loadDesignFrameWidth(pageDir)
  const designMeasurementFrame = resolveDesignMeasurementFrame({
    nodes,
    scopeMarks: scope.marks,
    captureMode: measurementContext.captureMode,
    fullDesignFrameWidth,
  })
  const designFrameWidth = designMeasurementFrame.width
  const assetManifest = readJson(path.join(pageDir, 'dsl-assets.json'), { items: [] })
  const targets = collectRenderTargets(dsl, nodes, assetManifest, scope.marks)
  if (!targets.length) {
    console.error('❌ 没有可执行 render 校验的带标记视觉节点；请确认 filter-dsl 已生成样式规格和 data-d2c-mark。')
    process.exit(1)
  }
  // effect 不等于 box-shadow：按属性解析后再交给浏览器逐项探测 computed style。
  const effectPlans = buildEffectPlans(targets)

  const { chrome, puppeteer } = loadPuppeteer()
  const session = await openBrowserSession({
    puppeteer,
    chrome,
    args,
    url,
    viewport: { width: viewportWidth, height: 900, deviceScaleFactor: 1 },
    waitMs,
    applySessionState,
  })
  let browserResult
  try {
    const page = session.page
    await page.evaluate(async () => { await document.fonts.ready })

    browserResult = await page.evaluate(async (payload) => {
      const { targets: expectedTargets, designFrameWidth: frameWidth, regionCss, useFallbackLayers, effectPlans: expectedEffects } = payload
      const viewportCssWidth = document.documentElement.clientWidth
      const region = regionCss && regionCss.width > 0 ? regionCss : null
      const effectiveCssWidth = region ? region.width : viewportCssWidth
      const scaleCssToDesign = frameWidth / effectiveCssWidth
      const cssFactor = 1 / scaleCssToDesign

      const scalePxTokens = (value) => String(value || '').replace(
        /(-?(?:\d+(?:\.\d*)?|\.\d+))px\b/gi,
        (_, raw) => `${Math.round(Number(raw) * cssFactor * 1000) / 1000}px`
      )
      const probe = document.createElement('div')
      Object.assign(probe.style, {
        position: 'fixed',
        left: '-10000px',
        top: '-10000px',
        width: '100px',
        height: '100px',
        visibility: 'hidden',
        pointerEvents: 'none',
      })
      document.body.appendChild(probe)

      const normalizeBackground = (value) => {
        if (!value) return { backgroundValid: true, backgroundColor: null, backgroundImage: 'none' }
        probe.style.background = ''
        const scaled = scalePxTokens(value).replace(/;\s*$/, '').trim()
        probe.style.background = scaled
        if (!probe.style.background) {
          return {
            backgroundValid: false,
            backgroundColor: null,
            backgroundImage: 'none',
            invalidReason: `CSSStyleDeclaration 拒绝 background=${value}`,
          }
        }
        const style = getComputedStyle(probe)
        return {
          backgroundValid: true,
          backgroundColor: style.backgroundColor,
          backgroundImage: style.backgroundImage,
        }
      }

      const normalizeShadow = (plan) => {
        const declarations = Array.isArray(plan) ? plan : []
        if (!declarations.length) return { effectValid: true, effectList: [], unsupportedEffects: [] }
        const effectList = []
        const unsupportedEffects = []
        for (const declaration of declarations) {
          if (!declaration.property || !declaration.styleKey) {
            unsupportedEffects.push({
              value: declaration.value || declaration.raw || '',
              reason: declaration.reason || '无法归类的 effect 属性',
            })
            continue
          }
          if (!(declaration.styleKey in probe.style)) {
            unsupportedEffects.push({
              value: `${declaration.property}: ${declaration.value}`,
              reason: `浏览器不支持 ${declaration.property}`,
            })
            continue
          }
          probe.style[declaration.styleKey] = ''
          probe.style[declaration.styleKey] = scalePxTokens(declaration.value).replace(/;\s*$/, '').trim()
          const assigned = probe.style[declaration.styleKey]
          if (!assigned) {
            return {
              effectValid: false,
              effectList,
              unsupportedEffects,
              invalidReason: `CSSStyleDeclaration 拒绝 ${declaration.property}=${declaration.value}`,
            }
          }
          effectList.push({
            property: declaration.property,
            styleKey: declaration.styleKey,
            value: getComputedStyle(probe)[declaration.styleKey] || assigned,
          })
        }
        return { effectValid: true, effectList, unsupportedEffects }
      }

      const styleSnapshot = (element) => {
        const style = getComputedStyle(element)
        const rect = element.getBoundingClientRect()
        return {
          rectWidth: rect.width,
          rectHeight: rect.height,
          fontFamily: style.fontFamily,
          fontWeight: style.fontWeight,
          fontSize: parseFloat(style.fontSize),
          fontStyle: style.fontStyle,
          lineHeight: style.lineHeight === 'normal' ? null : parseFloat(style.lineHeight),
          letterSpacing: style.letterSpacing,
          color: style.color,
          textDecorationLine: style.textDecorationLine || 'none',
          backgroundColor: style.backgroundColor,
          backgroundImage: style.backgroundImage,
          borderTopWidth: style.borderTopWidth,
          borderTopColor: style.borderTopColor,
          borderRadius: style.borderRadius,
          borderRadii: [
            style.borderTopLeftRadius,
            style.borderTopRightRadius,
            style.borderBottomRightRadius,
            style.borderBottomLeftRadius,
          ],
          opacity: style.opacity,
          boxShadow: style.boxShadow,
          backdropFilter: style.backdropFilter || style.webkitBackdropFilter || 'none',
          filter: style.filter,
          textShadow: style.textShadow,
          overflowX: style.overflowX,
          overflowY: style.overflowY,
          svgFill: style.fill,
        }
      }

      const effectiveTextDecoration = (element) => {
        const lines = new Set()
        let current = element
        while (current && current.nodeType === 1) {
          const line = getComputedStyle(current).textDecorationLine || ''
          for (const item of line.split(/\s+/).filter(Boolean)) {
            if (item !== 'none') lines.add(item)
          }
          if (current.hasAttribute && current.hasAttribute('data-d2c-mark') && current !== element) break
          current = current.parentElement
        }
        return lines.size ? Array.from(lines).sort().join(' ') : 'none'
      }

      const firstFontFamily = (fontFamily) => {
        const match = String(fontFamily || '').match(/^\s*("[^"]+"|'[^']+'|[^,]+)/)
        return match ? match[1].trim() : 'sans-serif'
      }

      const primaryFontDiffersFromFallback = (text, style) => {
        try {
          const canvas = document.createElement('canvas')
          const context = canvas.getContext('2d')
          const family = String(style.fontFamily || '')
          const fallback = family.replace(/^\s*("[^"]+"|'[^']+'|[^,]+)\s*,?\s*/, '') || 'sans-serif'
          const prefix = `${style.fontStyle || 'normal'} ${style.fontWeight || '400'} ${style.fontSize || 16}px`
          context.font = `${prefix} ${family}`
          const primaryWidth = context.measureText(text || '0123456789').width
          context.font = `${prefix} ${fallback}`
          const fallbackWidth = context.measureText(text || '0123456789').width
          return Math.abs(primaryWidth - fallbackWidth) > 0.05
        } catch (_) {
          return null
        }
      }

      const clippingInfo = (element) => {
        const style = getComputedStyle(element)
        const clipsX = style.overflowX === 'hidden' || style.overflowX === 'clip'
        const clipsY = style.overflowY === 'hidden' || style.overflowY === 'clip'
        if (!clipsX && !clipsY) return { clipped: false }
        const elementRect = element.getBoundingClientRect()
        const range = document.createRange()
        range.selectNodeContents(element)
        const textRect = range.getBoundingClientRect()
        const horizontal = clipsX && (
          (element.clientWidth > 0 && element.scrollWidth > element.clientWidth + 0.5) ||
          textRect.left < elementRect.left - 0.5 ||
          textRect.right > elementRect.right + 0.5
        )
        const vertical = clipsY && (
          (element.clientHeight > 0 && element.scrollHeight > element.clientHeight + 0.5) ||
          textRect.top < elementRect.top - 0.5 ||
          textRect.bottom > elementRect.bottom + 0.5
        )
        return {
          clipped: horizontal || vertical,
          reason: horizontal || vertical
            ? `overflow=${style.overflowX}/${style.overflowY}; client=${element.clientWidth}×${element.clientHeight}; scroll=${element.scrollWidth}×${element.scrollHeight}; textRect=${Math.round(textRect.width * 10) / 10}×${Math.round(textRect.height * 10) / 10}`
            : null,
        }
      }

      const extractUrls = (backgroundImage) => {
        const out = []
        const re = /url\((?:"([^"]+)"|'([^']+)'|([^\)]+))\)/g
        let match
        while ((match = re.exec(String(backgroundImage || '')))) {
          out.push((match[1] || match[2] || match[3] || '').trim())
        }
        return out
      }

      const normalizeSvgPathData = (value) => String(value || '')
        .trim()
        .replace(/,/g, ' ')
        .replace(/\s+/g, ' ')
        .replace(/\s*([A-Za-z])\s*/g, '$1')

      const svgPathFingerprint = (value) => {
        const input = normalizeSvgPathData(value)
        if (!input) return null
        let hash = 0x811c9dc5
        for (let index = 0; index < input.length; index += 1) {
          hash ^= input.charCodeAt(index)
          hash = Math.imul(hash, 0x01000193)
        }
        return `fnv1a-${(hash >>> 0).toString(16).padStart(8, '0')}`
      }

      const svgDetailsFromText = (text) => {
        try {
          const doc = new DOMParser().parseFromString(String(text || ''), 'image/svg+xml')
          if (doc.querySelector('parsererror')) return {}
          const paths = Array.from(doc.querySelectorAll('path'))
          const pathData = paths.map((path) => path.getAttribute('d')).filter(Boolean).join(' ')
          const svgFills = Array.from(new Set(paths
            .map((path) => path.getAttribute('fill'))
            .filter((fill) => fill && fill !== 'none')))
          const explicitOpacities = paths.map((path) => {
            let current = path
            let opacity = 1
            let explicit = false
            while (current && current.nodeType === 1) {
              if (current.hasAttribute('opacity')) {
                const value = Number(current.getAttribute('opacity'))
                if (Number.isFinite(value)) {
                  opacity *= value
                  explicit = true
                }
              }
              current = current.parentElement
            }
            return explicit ? Math.round(opacity * 10000) / 10000 : null
          }).filter((value) => value != null)
          const uniqueOpacities = Array.from(new Set(explicitOpacities))
          return {
            pathFingerprint: svgPathFingerprint(pathData),
            svgFills,
            svgOpacity: uniqueOpacities.length === 1 ? uniqueOpacities[0] : null,
            svgHasCutout: Boolean(doc.querySelector(
              'mask, clipPath, [fill-rule="evenodd"], [clip-rule="evenodd"]'
            )),
          }
        } catch (_) {
          return {}
        }
      }

      const svgTextFromDataUrl = (url) => {
        const match = String(url || '').match(/^data:image\/svg\+xml(?:;charset=[^;,]+)?(;base64)?,(.*)$/i)
        if (!match) return null
        try {
          return match[1] ? atob(match[2]) : decodeURIComponent(match[2])
        } catch (_) {
          return null
        }
      }

      const loadImage = (url) => new Promise((resolve) => {
        const image = new Image()
        const timer = setTimeout(() => resolve({ loadOk: false, loadError: 'image load timeout' }), 3000)
        image.onload = () => {
          clearTimeout(timer)
          const result = {
            loadOk: true,
            naturalWidth: image.naturalWidth,
            naturalHeight: image.naturalHeight,
            hasTransparentPixels: null,
            transparentPixelRatio: null,
          }
          try {
            const maxSide = 64
            const sourceWidth = Math.max(1, image.naturalWidth || 1)
            const sourceHeight = Math.max(1, image.naturalHeight || 1)
            const sampleScale = Math.min(1, maxSide / Math.max(sourceWidth, sourceHeight))
            const width = Math.max(1, Math.round(sourceWidth * sampleScale))
            const height = Math.max(1, Math.round(sourceHeight * sampleScale))
            const canvas = document.createElement('canvas')
            canvas.width = width
            canvas.height = height
            const context = canvas.getContext('2d', { willReadFrequently: true })
            context.clearRect(0, 0, width, height)
            context.drawImage(image, 0, 0, width, height)
            const pixels = context.getImageData(0, 0, width, height).data
            let transparent = 0
            for (let index = 3; index < pixels.length; index += 4) {
              if (pixels[index] <= 8) transparent += 1
            }
            const ratio = transparent / Math.max(1, width * height)
            result.transparentPixelRatio = Math.round(ratio * 10000) / 10000
            result.hasTransparentPixels = ratio >= 0.005
          } catch (_) {
            // Cross-origin canvas or an unsupported image keeps transparency unknown.
          }
          resolve(result)
        }
        image.onerror = () => {
          clearTimeout(timer)
          resolve({ loadOk: false, loadError: 'image load error' })
        }
        image.src = url
      })

      const sha256Hex = async (buffer) => {
        if (!globalThis.crypto || !globalThis.crypto.subtle) return null
        const digest = await globalThis.crypto.subtle.digest('SHA-256', buffer)
        return Array.from(new Uint8Array(digest))
          .map((byte) => byte.toString(16).padStart(2, '0'))
          .join('')
      }

      const fetchAssetDetails = async (url, parseSvg) => {
        try {
          const response = await fetch(url)
          if (!response.ok) return {}
          const buffer = await response.arrayBuffer()
          return {
            contentSha256: await sha256Hex(buffer),
            ...(parseSvg ? svgDetailsFromText(new TextDecoder().decode(buffer)) : {}),
          }
        } catch (_) {
          return {}
        }
      }

      const loadUrl = async (url) => {
        if (!url) return { loadOk: false, loadError: 'empty url' }
        const dataSvg = svgTextFromDataUrl(url)
        if (url.startsWith('data:')) {
          return {
            ...(await loadImage(url)),
            ...(dataSvg ? svgDetailsFromText(dataSvg) : {}),
            ...(await fetchAssetDetails(url, Boolean(dataSvg))),
          }
        }
        const loaded = await loadImage(url)
        if (!loaded.loadOk) return loaded
        Object.assign(loaded, await fetchAssetDetails(url, /\.svg(?:[?#]|$)/i.test(url)))
        return loaded
      }

      let fontFaces = []
      try {
        fontFaces = Array.from(document.fonts).map((face) => ({
          family: face.family,
          weight: face.weight,
          style: face.style,
          status: face.status,
        }))
      } catch (_) {
        fontFaces = []
      }

      const normalize = (s) => String(s || '').replace(/\s+/g, ' ').trim()
      const all = Array.from(document.querySelectorAll('body *')).filter((el) => {
        const tag = el.tagName
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') return false
        const r = el.getBoundingClientRect()
        return r.width > 0 && r.height > 0
      })
      const normOf = new Map()
      for (const el of all) normOf.set(el, normalize(el.textContent))
      const claimed = new Set()

      const marked = new Map()
      for (const element of document.querySelectorAll('[data-d2c-mark]')) {
        const mark = element.getAttribute('data-d2c-mark')
        if (!marked.has(mark)) marked.set(mark, element)
      }

      const bboxOriginLeft = region ? region.left : 0
      const results = []
      for (const target of expectedTargets) {
        let element = marked.get(target.layoutMark)
        let fallbackMatched = false
        if (!element && useFallbackLayers && useFallbackLayers.length > 0) {
          const useLayers = new Set(useFallbackLayers)
          const want = target.expected && target.expected.text ? normalize(target.expected.text) : ''
          const targetBbox = target.bbox || {}
          const wantLeftCss = Number.isFinite(targetBbox.x) && scaleCssToDesign > 0
            ? bboxOriginLeft + targetBbox.x / scaleCssToDesign
            : null

          // Layer 2: CSS 选择器回退
          if (useLayers.has('css')) {
            const isText = target.type === 'TEXT' || !!want
            if (isText) {
              const textTags = ['span', 'p', 'div', 'a', 'button', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'label', 'li']
              for (const tag of textTags) {
                const els = all.filter(el =>
                  el.tagName.toLowerCase() === tag &&
                  (normOf.get(el) || '').length > 0 &&
                  !claimed.has(el)
                )
                if (els.length > 0) {
                  element = els.sort((a, b) => {
                    const ra = a.getBoundingClientRect()
                    const rb = b.getBoundingClientRect()
                    const aDist = wantLeftCss != null ? Math.abs(ra.left - wantLeftCss) : ra.top
                    const bDist = wantLeftCss != null ? Math.abs(rb.left - wantLeftCss) : rb.top
                    return aDist - bDist
                  })[0]
                  break
                }
              }
            }
          }

          // Layer 3: Levenshtein 文本模糊匹配
          if (!element && useLayers.has('text-fuzzy') && want) {
            const levThreshold = 3
            const levDist = (a, b) => {
              const alen = a.length, blen = b.length
              if (Math.abs(alen - blen) > levThreshold) return Infinity
              const dp = Array.from({ length: alen + 1 }, () => new Array(blen + 1).fill(0))
              for (let i = 0; i <= alen; i++) dp[i][0] = i
              for (let j = 0; j <= blen; j++) dp[0][j] = j
              for (let i = 1; i <= alen; i++)
                for (let j = 1; j <= blen; j++)
                  dp[i][j] = Math.min(dp[i-1][j]+1, dp[i][j-1]+1, dp[i-1][j-1]+(a[i-1]===b[j-1]?0:1))
              return dp[alen][blen]
            }
            const fuzzyMatch = all
              .filter(el => !claimed.has(el) && (normOf.get(el) || '').length > 0)
              .map(el => ({ el, dist: levDist(normOf.get(el), want) }))
              .filter(m => m.dist <= levThreshold)
              .sort((a, b) => a.dist - b.dist)
            if (fuzzyMatch.length > 0) {
              element = fuzzyMatch[0].el
            }
          }

          // Layer 4: 位置启发式
          if (!element && useLayers.has('position') && wantLeftCss != null) {
            const posMatch = all
              .filter(el => !claimed.has(el) && (normOf.get(el) || '').length > 0)
              .map(el => {
                const r = el.getBoundingClientRect()
                const dx = r.left - wantLeftCss
                const dy = r.top - bboxOriginLeft
                return { el, dist: Math.sqrt(dx * dx + dy * dy) }
              })
              .sort((a, b) => a.dist - b.dist)
            if (posMatch.length > 0 && posMatch[0].dist < 200) {
              element = posMatch[0].el
            }
          }

          if (element) claimed.add(element)
          fallbackMatched = Boolean(element)
        }
        if (!element) {
          results.push({ layoutMark: target.layoutMark, matched: false })
          continue
        }
        const style = styleSnapshot(element)
        const expectedSpans = (target.expected && target.expected.textSpans) || []
        const textSpans = expectedSpans.map((expectedSpan, index) => {
          const span = expectedSpans.length > 1
            ? element.querySelector(`[data-span-index="${index}"]`)
            : element
          if (!span) return { index, matched: false }
          const spanStyle = styleSnapshot(span)
          spanStyle.effectiveTextDecoration = effectiveTextDecoration(span)
          let fontCheck = null
          try {
            fontCheck = document.fonts.check(
              `${spanStyle.fontWeight} ${spanStyle.fontSize}px ${firstFontFamily(spanStyle.fontFamily)}`,
              expectedSpan.text || target.name || '0'
            )
          } catch (_) {
            fontCheck = null
          }
          return {
            index,
            matched: true,
            style: spanStyle,
            fontCheck,
            fontPrimaryDiffersFromFallback: primaryFontDiffersFromFallback(expectedSpan.text, spanStyle),
          }
        })

        const candidateElements = [element, ...element.querySelectorAll('img, source, object, svg, [data-d2c-asset-id]')]
        const candidateMap = new Map()
        for (const candidateElement of candidateElements) {
          const tagName = candidateElement.tagName.toLowerCase()
          const assetId = candidateElement.getAttribute && candidateElement.getAttribute('data-d2c-asset-id')
          const urls = []
          if (tagName === 'img') urls.push(candidateElement.currentSrc || candidateElement.src)
          if (tagName === 'source') urls.push(candidateElement.srcset || candidateElement.src)
          if (tagName === 'object') urls.push(candidateElement.data)
          extractUrls(getComputedStyle(candidateElement).backgroundImage).forEach((url) => urls.push(url))
          if (assetId) {
            candidateMap.set(`asset:${assetId}`, { tagName, assetId, url: null, loadOk: true })
          }
          const svgElement = tagName === 'svg'
            ? candidateElement
            : candidateElement.ownerSVGElement || null
          if (svgElement) {
            const details = svgDetailsFromText(svgElement.outerHTML)
            const key = details.pathFingerprint || `inline-svg:${candidateMap.size}`
            candidateMap.set(`svg:${key}`, { tagName: 'svg', assetId: null, url: null, loadOk: true, ...details })
          }
          for (const url of urls.filter(Boolean)) {
            if (!candidateMap.has(url)) candidateMap.set(url, { tagName, assetId: null, url })
          }
        }
        const assetCandidates = []
        for (const candidate of candidateMap.values()) {
          if (candidate.url) Object.assign(candidate, await loadUrl(candidate.url))
          assetCandidates.push(candidate)
        }

        const background = normalizeBackground(target.expected && target.expected.visualFill)
        const shadow = normalizeShadow((expectedEffects || {})[target.layoutMark])
        results.push({
          layoutMark: target.layoutMark,
          matched: true,
          fallbackMatched,
          tagName: element.tagName,
          style,
          textSpans,
          clipping: expectedSpans.length ? clippingInfo(element) : { clipped: false },
          assetCandidates,
          normalizedExpected: {
            ...background,
            ...shadow,
            invalidReason: background.invalidReason || shadow.invalidReason || null,
          },
        })
      }

      probe.remove()
      return { viewportCssWidth, effectiveCssWidth, scaleCssToDesign, fontFaces, results }
    }, {
      targets,
      designFrameWidth,
      regionCss: measurementContext.regionCss,
      useFallbackLayers: [...useFallbackLayers],
      effectPlans,
    })
  } finally {
    await closeBrowserSession(session)
  }

  const evaluated = evaluateRenderTargets(targets, browserResult.results, {
    scaleCssToDesign: browserResult.scaleCssToDesign,
    tolerances: DEFAULT_TOLERANCES,
    fontFaces: browserResult.fontFaces,
  })
  const hostLayout = readJson(path.join(pageDir, 'layout-h5.json'), {})
  const report = {
    version: '1.0',
    gate: 'render-integrity',
    url,
    pageDir: path.resolve(pageDir),
    ...(scope.marks ? { scope: { component: scope.componentId, layoutMarks: scope.marks, source: scope.source } } : {}),
    designFrameWidth,
    fullDesignFrameWidth,
    designMeasurementFrame,
    viewportCssWidth: browserResult.viewportCssWidth,
    effectiveCssWidth: browserResult.effectiveCssWidth,
    captureMode: measurementContext.captureMode,
    browserSessionId: session.sessionId,
    browserTargetId: session.targetId,
    browserAttached: session.attached,
    measurementRegionCss: measurementContext.regionCss || null,
    scaleCssToDesign: Math.round(browserResult.scaleCssToDesign * 10000) / 10000,
    hostLayoutPolicy: hostLayout.hostLayoutPolicy || 'none',
    layoutShiftY: Number(hostLayout.layoutShiftY ?? hostLayout.contentOffsetY ?? 0),
    tolerances: DEFAULT_TOLERANCES,
    checkedCount: targets.length,
    matchedCount: evaluated.items.filter((item) => item.matched).length,
    violationCount: evaluated.hardViolationCount,
    hardViolationCount: evaluated.hardViolationCount,
    warningCount: evaluated.warningCount,
    sourceMaterialViolationCount: evaluated.sourceMaterialViolationCount,
    violations: evaluated.violations,
    warnings: evaluated.warnings,
    items: evaluated.items,
  }

  fs.mkdirSync(path.resolve(outDir), { recursive: true })
  const outPath = path.join(path.resolve(outDir), 'render-diff.json')
  fs.writeFileSync(outPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8')

  try {
    const log = getLogger(pageDir)
    if (log.enabled) {
      log.scriptComplete('d2c-render-check', { component: scope.componentId, url })
      log.artifactWrite(outPath)
      if (report.violationCount) log.checkViolation('render', { violationCount: report.violationCount, checkedCount: report.checkedCount, matchedCount: report.matchedCount })
      if (report.warningCount) log.checkWarning('render', { warningCount: report.warningCount })
    }
  } catch { /* logging must not affect the gate */ }

  console.log('===== d2c render 自检（DSL visual spec vs DOM computed style） =====')
  console.log(`页面: ${maskUrl(url)}`)
  console.log(`视觉节点: ${report.checkedCount}，匹配: ${report.matchedCount}，hard violations: ${report.violationCount}，warnings: ${report.warningCount}`)
  // === 诊断提示：低匹配率时给出可能原因 ===
  const checkedCount = report.checkedCount
  const matchCount = report.matchedCount
  const matchRate = checkedCount > 0 ? matchCount / checkedCount : 0
  if (matchCount === 0 && checkedCount > 0) {
    console.log('')
    console.log('⚠️  未匹配到任何视觉节点。可能原因：')
    console.log('  1. SPA 未挂载 — 检查根节点是否存在（页面可能是空白/登录页）')
    console.log('  2. 缺少 data-d2c-mark — 存量项目可显式启用回退匹配（--fallback-strategy conservative）')
    console.log('  3. 页面被登录页重定向 — 使用 --session-state 注入登录态')
    console.log('  4. --app-selector 不正确 — 当前值: ' + (appSelector || '#app（默认）'))
  } else if (matchRate > 0 && matchRate < 0.1 && checkedCount > 10) {
    console.log('')
    console.log('⚠️  仅匹配到 ' + matchCount + '/' + checkedCount + ' 个节点（' + (matchRate * 100).toFixed(1) + '%）。可能原因：')
    console.log('  1. 页面内容与 DSL 不匹配 — 确认 --url 是否正确')
    console.log('  2. 大量节点来自异步加载 — 等待时间不足')
    console.log('  3. DSL 语言与页面语言不一致')
  }
  console.log(`输出: ${outPath}`)
  for (const violation of report.violations.slice(0, 20)) {
    console.log(`  - [hard] ${violation.kind} @ ${violation.layoutMark}「${violation.name}」: expected=${JSON.stringify(violation.expected)} actual=${JSON.stringify(violation.actual)}`)
  }
  if (!report.violationCount) console.log('✅ 可确定比较的 render 契约全部通过。')

  if (report.violationCount && (args['fail-on-violation'] || args.failOnViolation)) process.exit(3)
}

if (require.main === module) {
  main().catch((err) => {
    console.error(err)
    process.exit(1)
  })
}

module.exports = { main, parseScope, buildEffectPlans }
