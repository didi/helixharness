#!/usr/bin/env node
/* eslint-disable no-console */
/**
 * d2c-geometry-check.cjs — DOM 实测 vs DSL bbox 几何自检（量化，不依赖肉眼）
 *
 * 原理：D2C 文案逐字来自 DSL，文本是最稳定的锚点。脚本打开已运行的页面，
 * 对 DSL 中每个 TEXT 节点按文案在 DOM 中定位元素，getBoundingClientRect 实测，
 * 换算回设计稿坐标后与 DSL bbox 对比，产出 geometry-diff.json 供
 * d2c-fidelity-evaluator 作为硬证据使用（violations 不阻断流程）。
 *
 * 检查项（通用，不限定具体缺陷）：
 *   - width-mismatch        : |实测宽 − DSL宽| > max(4 设计px, 2%)（文本左对齐时宽度差≈右端漂移）
 *   - left-edge-drift       : 实测左缘 vs DSL 祖先累加 X（仅 X 轴；Y 轴经 H5 rebase 后不可累加，不检查）
 *   - right-edge-drift      : 实测右缘 vs DSL 左缘+宽
 *   - whitespace-collapsed  : DOM 文本含连续空格但 computed white-space 会折叠（normal/nowrap/pre-line）
 *   - font-size-mismatch    : computed font-size vs DSL fontSize（换算后差 > 1.5 css px）
 *   - text-missing          : DSL 文案在 DOM 中找不到
 *
 * 匹配分级：只有 exact（文本完全相等）匹配参与上述几何判定。
 * contains 只确认文本存在；css-fallback / text-fuzzy / position-heuristic
 * 属于启发式猜测，命中记为 low-confidence-match warning，不产出几何违规。
 * 因此 --fallback-strategy 默认 off；存量未标记项目需显式开启。
 *
 * 用法：
 *   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-geometry-check.cjs \
 *     --url <页面完整URL> \
 *     --page-dir d-d2c/temp/<page-dir> \
 *     --out d-d2c/temp/<page-dir>/r<N>/
 *
 * 组件作用域（复杂页串行小闭环，见 rules/d-d2c-complex-split.md）：
 *   --component <id>  只校验该组件 layoutMarks 子树内的 TEXT 节点；
 *                     marks 取自 components/<id>/component-meta.json（缺省回退
 *                     component-plan.json / complexity-plan.json）；
 *                     --out 缺省为 components/<id>/（写 geometry-diff.json）。
 *                     组件须已挂载进页面（渐进拼装），仍用整页 URL 与整页 marked DSL，
 *                     祖先累加 X 在真实页面上下文中有效。
 *   --scope <mark[,mark…]>  直接指定子树根 layoutMark（不依赖组件清单）。
 *
 * 可选：--width <viewport宽,默认678> --wait <ms,默认3000>
 *      --tolerance-px <设计px,默认4> --tolerance-pct <百分比,默认2>
 *      --fail-on-violation （存在 violation 时退出码 3，供 CI 用；默认不阻断）
 *
 * Chrome 路径：优先 PUPPETEER_EXECUTABLE_PATH / CHROME_PATH，否则常见路径探测。
 * puppeteer-core 从仓库 node_modules 解析（必要时设 NODE_PATH=$(pwd)/node_modules）。
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
  getBBoxLocal,
  getNodeText,
  resolveComponentMarks,
  loadMarkedDsl,
  loadDesignFrameWidth,
  findRawNode,
  resolveDesignMeasurementFrame,
} = require('./lib/d2c-dom-measure-core.cjs')
const { evaluateGeometryTargets } = require('./lib/d2c-geometry-check-core.cjs')
// 结构化运行日志（非侵入、可降级）
let getLogger = () => ({ enabled: false })
try { ({ getLogger } = require('./lib/d2c-run-log.cjs')) } catch { /* 静默降级 */ }
// 登录态/额外 URL query 注入（--url-params 或 D2C_URL_PARAMS），与截图同一受保护 URL 必须贯穿
// 会话态注入（cookie/localStorage，--session-state 或 D2C_SESSION_STATE）：与截图同一会话必须贯穿，否则截图落地、几何却被踢
let appendUrlParams = (u) => u
let maskUrl = (u) => u
let applySessionState = async () => ({ applied: false })
try { ({ appendUrlParams, maskUrl, applySessionState } = require('./lib/d2c-url-params.cjs')) } catch { /* 静默降级 */ }

/** 收集 TEXT 节点：文案 + 自身 bbox + 祖先累加 absX（X 轴可累加；Y 轴 rebase 后不保证）
 *  scopeMarks 非空时只收集这些 layoutMark 子树内的 TEXT（absX 仍从页面根累加） */
function collectTextNodes(markedNodes, scopeMarks) {
  const out = []
  const scope = scopeMarks && scopeMarks.length ? new Set(scopeMarks) : null
  function walk(node, accX, inScope) {
    if (!node) return
    const bbox = getBBoxLocal(node)
    const absX = accX + bbox.x
    const nowInScope = inScope || (scope && scope.has(node._layoutMark))
    if (scope && !nowInScope) {
      for (const child of node.children || []) walk(child, absX, false)
      return
    }
    if (node.type === 'TEXT') {
      const text = getNodeText(node)
      if (text && text.trim()) {
        out.push({
          layoutMark: node._layoutMark || null,
          nodeId: node.id || null,
          name: node.name || '',
          text,
          w: bbox.w,
          h: bbox.h,
          absX,
          fontSize: (() => {
            // dsl_layout_marked 不内联 fontSize；由调用方按 styles 解析后补充，这里留 null
            return null
          })(),
        })
      }
    }
    for (const child of node.children || []) walk(child, absX, nowInScope)
  }
  for (const n of markedNodes || []) walk(n, 0, !scope)
  return out
}

/** 从 dsl_full/styles 解析 fontSize（design px），按 text[0].font 引用 */
function buildFontSizeResolver(dsl) {
  const styles = (dsl && dsl.styles) || {}
  return (node) => {
    const ref = node.text && node.text[0] && node.text[0].font
    if (!ref) return null
    const rec = styles[ref]
    const size = rec && rec.value && Number(rec.value.size)
    return Number.isFinite(size) ? size : null
  }
}

async function main() {
  const args = parseArgs(process.argv)

  // 默认关闭启发式回退：猜中的元素只能证明「文本存在」，拿它判几何会制造假违规。
  // 存量未标记项目可显式 --fallback-strategy conservative|aggressive 打开，
  // 但命中只会记为 low-confidence，不产生几何违规。
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
  let url =
    (args.url && args.url !== true ? args.url : process.env.D2C_CAPTURE_URL) ||
    null
  const pageDir = args['page-dir'] && args['page-dir'] !== true ? args['page-dir'] : null
  const componentId = args.component && args.component !== true ? args.component : null
  const outDir =
    (args.out && args.out !== true ? args.out : null) ||
    (componentId ? path.join(pageDir || '', 'components', componentId) : pageDir)

  if (!url || !pageDir) {
    console.error(
      '用法: node d2c-geometry-check.cjs --url <full-url> --page-dir d-d2c/temp/<page-dir> --out d-d2c/temp/<page-dir>/r<N>/ [--component <id> | --scope <mark,…>]'
    )
    process.exit(1)
  }

  assertUsableCaptureMaterial(args, outDir, 'd2c-geometry-check')

  // 注入登录态等额外 query 参数（须与截图同一 URL 一致，否则自检阶段照样进不去受保护页）
  url = appendUrlParams(url, args)

  let scopeMarks = null
  let scopeSource = null
  if (componentId) {
    const resolved = resolveComponentMarks(pageDir, componentId)
    if (!resolved) {
      console.error(
        `❌ 无法解析组件 ${componentId} 的 layoutMarks（查找 components/${componentId}/component-meta.json、component-plan.json、complexity-plan.json 均未命中）`
      )
      process.exit(1)
    }
    scopeMarks = resolved.marks
    scopeSource = resolved.source
  } else {
    const scopeValue = args.scope && args.scope !== true
      ? args.scope
      : process.env.D2C_DESIGN_SCOPE
    if (scopeValue) {
      scopeMarks = String(scopeValue).split(',').map((s) => s.trim()).filter(Boolean)
      scopeSource = args.scope && args.scope !== true ? '--scope' : 'D2C_DESIGN_SCOPE'
    }
  }

  const measurementContext = resolveMeasurementContext(args, outDir)
  const viewportWidth = measurementContext.viewportWidthCss
  const waitMs = parseInt(String(args.wait || '3000'), 10)
  const tolPx = parseFloat(String(args['tolerance-px'] || '4'))
  const tolPct = parseFloat(String(args['tolerance-pct'] || '2'))

  const { dsl, nodes } = loadMarkedDsl(pageDir)
  let hostLayout = null
  try {
    hostLayout = JSON.parse(
      fs.readFileSync(path.join(pageDir, 'layout-h5.json'), 'utf8')
    )
  } catch (_) {
    throw new Error('layout-h5.json is required; run d-d2c initialization first')
  }
  const fullDesignFrameWidth = loadDesignFrameWidth(pageDir)
  const designMeasurementFrame = resolveDesignMeasurementFrame({
    nodes,
    scopeMarks,
    captureMode: measurementContext.captureMode,
    fullDesignFrameWidth,
  })
  const designFrameWidth = designMeasurementFrame.width
  const resolveFontSize = buildFontSizeResolver(dsl)
  const textNodes = collectTextNodes(nodes, scopeMarks)
  if (designMeasurementFrame.mode === 'region') {
    for (const textNode of textNodes) {
      textNode.absX -= designMeasurementFrame.originX
    }
  }
  if (scopeMarks) {
    console.log(
      `作用域: ${componentId ? `组件 ${componentId} ` : ''}[${scopeMarks.join(', ')}]（来源 ${scopeSource}），命中 TEXT ${textNodes.length} 个`
    )
  }
  for (const t of textNodes) {
    const raw = nodes && findRawNode(nodes, t.layoutMark)
    t.fontSize = raw ? resolveFontSize(raw) : null
  }

  if (!textNodes.length) {
    console.error('⚠️ DSL 中没有可校验的 TEXT 节点，跳过几何自检。')
  }

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

  let measured
  try {
    const page = session.page

    measured = await page.evaluate((payload) => {
      const { targets, designFrameWidth, regionCss, useFallbackLayers } = payload
      const normalize = (s) => String(s || '').replace(/\s+/g, ' ').trim()
      const viewportCssWidth = document.documentElement.clientWidth
      const region = regionCss && regionCss.width > 0 ? regionCss : null
      const originLeft = region ? region.left : 0
      const effectiveCssWidth = region ? region.width : viewportCssWidth
      const scale = designFrameWidth / effectiveCssWidth // css px → design px

      const all = Array.from(document.querySelectorAll('body *')).filter((el) => {
        const tag = el.tagName
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') return false
        const r = el.getBoundingClientRect()
        return r.width > 0 && r.height > 0
      })
      // 预归一化每个元素的 textContent，避免重复计算
      const normOf = new Map()
      for (const el of all) normOf.set(el, normalize(el.textContent))
      // 已占用节点：避免多个 DSL 文案（尤其重复文案）都命中同一 DOM 节点
      const claimed = new Set()

      const measureEl = (best, targetLayoutMark) => {
        // 若已精确命中 data-d2c-mark，该元素就是 TEXT 契约本身，应保留其固定文本槽宽；
        // 否则用 Range 量文本内容实际渲染框（排除元素 padding/border，
        // 否则 TEXT 合并进带 padding 容器时会误报宽度）。
        const elementMark =
          best.getAttribute('data-d2c-mark') || best.getAttribute('data-layout-mark')
        const exactLayoutMark = elementMark === targetLayoutMark
        const range = document.createRange()
        range.selectNodeContents(best)
        let r = exactLayoutMark ? best.getBoundingClientRect() : range.getBoundingClientRect()
        if (!r || r.width === 0) r = best.getBoundingClientRect()
        const cs = getComputedStyle(best)
        return {
          rect: { left: r.left - originLeft, width: r.width, height: r.height },
          domText: best.textContent,
          whiteSpace: cs.whiteSpace,
          fontSizeCss: parseFloat(cs.fontSize),
        }
      }

      return {
        viewportCssWidth,
        effectiveCssWidth,
        measurementRegionCss: region,
        results: targets.map((t) => {
          const want = normalize(t.text)
          // 期望左缘（css px）：DSL absX 为设计px，按 scale 反算，供重复文案就近择一
          const wantLeftCss =
            Number.isFinite(t.absX) && scale > 0 ? originLeft + t.absX / scale : null
          // 一级：normalized textContent 完全等于目标文案的元素
          let candidates = all.filter((el) => normOf.get(el) === want)
          let matchType = 'exact'
          // 二级回退：无精确匹配时用“包含”匹配（应对文本被彩色/删除线 span 拆分、
          // 或 DSL 比 DOM 拆得更细导致整行被合并），只取最深的包含元素（最紧包裹）
          if (!candidates.length && want) {
            const contains = all.filter((el) => {
              const nt = normOf.get(el)
              return nt && nt.includes(want)
            })
            candidates = contains.filter(
              (el) => !contains.some((o) => o !== el && el.contains(o))
            )
            matchType = 'contains'
          }
          // === 多层回退匹配（存量项目无 data-d2c-mark 时启用） ===
          if (!candidates.length && useFallbackLayers && useFallbackLayers.length > 0) {
            const useLayers = new Set(useFallbackLayers)

            // Layer 2: CSS 选择器回退 — 按 DSL 节点类型匹配 DOM 元素
            if (useLayers.has('css')) {
              const nodeType = t.nodeType || ''
              if (nodeType === 'TEXT') {
                const textTags = ['span', 'p', 'div', 'a', 'button', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'label', 'li']
                for (const tag of textTags) {
                  const els = all.filter(el =>
                    el.tagName.toLowerCase() === tag &&
                    (normOf.get(el) || '').length > 0 &&
                    !claimed.has(el)
                  )
                  if (els.length > 0) {
                    const best = els.sort((a, b) => {
                      const ra = a.getBoundingClientRect()
                      const rb = b.getBoundingClientRect()
                      const aDist = wantLeftCss != null ? Math.abs(ra.left - wantLeftCss) : ra.top
                      const bDist = wantLeftCss != null ? Math.abs(rb.left - wantLeftCss) : rb.top
                      return aDist - bDist
                    })[0]
                    claimed.add(best)
                    candidates = [best]
                    matchType = 'css-fallback'
                    break
                  }
                }
              }
            }

            // Layer 3: Levenshtein 文本模糊匹配
            if (!candidates.length && useLayers.has('text-fuzzy') && want) {
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
                claimed.add(fuzzyMatch[0].el)
                candidates = [fuzzyMatch[0].el]
                matchType = 'text-fuzzy'
              }
            }

            // Layer 4: 位置启发式
            if (!candidates.length && useLayers.has('position') && wantLeftCss != null) {
              const posMatch = all
                .filter(el => !claimed.has(el) && (normOf.get(el) || '').length > 0)
                .map(el => {
                  const r = el.getBoundingClientRect()
                  const dx = r.left - wantLeftCss
                  const dy = r.top - originLeft
                  return { el, dist: Math.sqrt(dx * dx + dy * dy) }
                })
                .sort((a, b) => a.dist - b.dist)
              if (posMatch.length > 0 && posMatch[0].dist < 200) {
                claimed.add(posMatch[0].el)
                candidates = [posMatch[0].el]
                matchType = 'position-heuristic'
              }
            }
          }

          if (!candidates.length) return { layoutMark: t.layoutMark, matched: false }

          // 优先未占用节点；重复文案按「是否命中 DSL 标记 → 距期望左缘最近 → 面积最小（最深）」择一。
          // 文案相同的父容器和 TEXT 节点可能拥有完全相同的 bbox，优先使用 DSL 对应标记，
          // 避免把父容器的继承字体/内边距误判为文本契约。
          const pool = candidates.filter((el) => !claimed.has(el))
          const ranked = (pool.length ? pool : candidates)
            .map((el) => {
              const r = el.getBoundingClientRect()
              const elementMark =
                el.getAttribute('data-d2c-mark') || el.getAttribute('data-layout-mark')
              return {
                el,
                markPenalty: elementMark === t.layoutMark ? 0 : 1,
                area: r.width * r.height,
                dist: wantLeftCss != null ? Math.abs(r.left - wantLeftCss) : 0,
              }
            })
            .sort(
              (a, b) =>
                a.markPenalty - b.markPenalty || a.dist - b.dist || a.area - b.area
            )
          const best = ranked[0].el
          claimed.add(best)
          return { layoutMark: t.layoutMark, matched: true, matchType, ...measureEl(best, t.layoutMark) }
        }),
      }
    }, { targets: textNodes, designFrameWidth, regionCss: measurementContext.regionCss, useFallbackLayers: [...useFallbackLayers] })
  } finally {
    await closeBrowserSession(session)
  }

  const evaluated = evaluateGeometryTargets(textNodes, measured, {
    designFrameWidth,
    tolPx,
    tolPct,
  })
  const effectiveCssWidth = evaluated.effectiveCssWidth
  const scale = evaluated.scale
  const violations = evaluated.violations
  const items = evaluated.items
  const warnings = evaluated.warnings
  const matchedCount = evaluated.matchedCount

  const report = {
    version: '1.0',
    url,
    pageDir: path.resolve(pageDir),
    ...(scopeMarks
      ? { scope: { component: componentId || null, layoutMarks: scopeMarks } }
      : {}),
    designFrameWidth,
    fullDesignFrameWidth,
    designMeasurementFrame,
    viewportCssWidth: measured.viewportCssWidth,
    effectiveCssWidth,
    captureMode: measurementContext.captureMode,
    browserSessionId: session.sessionId,
    browserTargetId: session.targetId,
    browserAttached: session.attached,
    hostLayoutPolicy: hostLayout?.hostLayoutPolicy || 'none',
    hostChromeBottomY: Number(hostLayout?.hostChromeBottomY || 0),
    layoutShiftY: Number(hostLayout?.layoutShiftY ?? 0),
    ignoredDesignRegions: hostLayout?.ignoredDesignRegions || [],
    measurementRegionCss: measured.measurementRegionCss || null,
    scaleCssToDesign: Math.round(scale * 10000) / 10000,
    tolerance: { px: tolPx, pct: tolPct, note: '允差 = max(px, 期望宽×pct%)；edge 检查另有 6px 下限' },
    yAxisNote: 'Y 轴经 H5 rebase 后 DSL 不可累加，本脚本只校验 X 轴与尺寸；垂直对齐仍以叠图为准',
    checkedCount: textNodes.length,
    matchedCount,
    violationCount: violations.length,
    warningCount: warnings.length,
    warnings,
    lowConfidenceMatches: evaluated.lowConfidenceMatches,
    matchConfidenceNote:
      '只有 exact 匹配参与几何判定；contains / guess（css-fallback / text-fuzzy / position-heuristic）仅确认文本存在，其尺寸与边缘未被验证。',
    fallbackStrategy: fallbackRaw,
    violations,
    items,
  }

  fs.mkdirSync(path.resolve(outDir), { recursive: true })
  const outPath = path.join(path.resolve(outDir), 'geometry-diff.json')
  fs.writeFileSync(outPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8')

  // 结构化日志：记录几何自检产物与横向 violation（page 根目录 = --page-dir）
  try {
    const glog = getLogger(pageDir)
    if (glog.enabled) {
      glog.scriptComplete('d2c-geometry-check', { component: componentId || null, url })
      glog.artifactWrite(outPath)
      if (violations.length) {
        glog.checkViolation('geometry', {
          violationCount: violations.length, checkedCount: textNodes.length,
          matchedCount, component: componentId || null,
        })
      }
    }
  } catch { /* 日志失败不影响主流程 */ }

  console.log('===== d2c 几何自检（DOM 实测 vs DSL bbox） =====')
  console.log(`页面: ${maskUrl(url)}`)
  console.log(`文本节点: ${textNodes.length}，匹配: ${matchedCount}，violations: ${violations.length}，warnings: ${warnings.length}`)
  if (fallbackRaw !== 'off') {
    console.log(`回退匹配: --fallback-strategy ${fallbackRaw}（命中仅记为 low-confidence，不参与几何判定）`)
  }
  // === 诊断提示：低匹配率时给出可能原因 ===
  const matchRate = textNodes.length > 0 ? matchedCount / textNodes.length : 0
  if (matchedCount === 0 && textNodes.length > 0) {
    console.log('')
    console.log('⚠️  未匹配到任何节点。可能原因：')
    console.log('  1. SPA 未挂载 — 检查根节点是否存在（页面可能是空白/登录页）')
    console.log('  2. 缺少 data-d2c-mark — 存量项目可显式启用回退匹配（--fallback-strategy conservative），但命中只确认文本存在、不做几何判定')
    console.log('  3. 页面被登录页重定向 — 使用 --session-state 注入登录态')
    console.log('  4. --app-selector 不正确 — 当前值: ' + (appSelector || '#app（默认）'))
  } else if (matchRate > 0 && matchRate < 0.1 && textNodes.length > 10) {
    console.log('')
    console.log('⚠️  仅匹配到 ' + matchedCount + '/' + textNodes.length + ' 个节点（' + (matchRate * 100).toFixed(1) + '%）。可能原因：')
    console.log('  1. 页面内容与 DSL 不匹配 — 确认 --url 是否正确')
    console.log('  2. 大量文本来自异步加载 — 等待时间不足')
    console.log('  3. DSL 语言与页面语言不一致')
  }
  for (const warning of warnings) {
    console.log('')
    console.log(`⚠️  ${warning.kind}：${warning.actual}`)
    console.log(`   修法：${warning.repairHint}`)
  }
  console.log(`输出: ${outPath}`)
  if (violations.length) {
    console.log('')
    console.log('⚠️ 违规清单（必须作为 d2c-fidelity-evaluator 评测输入逐条核对）:')
    for (const v of violations) {
      console.log(
        `  - [${v.severityHint}] ${v.kind} @ ${v.layoutMark}「${v.text}」: ${v.actual}`
      )
    }
  } else {
    console.log('✅ 全部通过，无几何违规。')
  }

  if (violations.length && (args['fail-on-violation'] || args.failOnViolation)) {
    process.exit(3)
  }
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
