#!/usr/bin/env node
/* eslint-disable no-console */
/**
 * d2c-vertical-check.cjs — DOM 实测 vs DSL 的纵向几何自检（top / height / bottom）
 *
 * 背景：d2c-geometry-check.cjs 只校验 X 轴与尺寸（文本锚点纵向有 baseline 噪声，故弃 Y）。
 * 本脚本改用「容器锚点」做纵向对账：DSL relativeY 是相对直接父级，H5 rebase 只平移顶层
 * 业务块自身的 relativeY（见 lib/dsl-layout-core.cjs），沿树累加得到 H5 绝对 top 仍有效。
 *
 * 锚点 = 组件根 + 所有 flex group（flexContainerInfo 且 children≥2，与 dom.md 的
 * data-flex-group 同一判据）。生成 Vue 时这些 wrapper 须带 data-d2c-mark（镜像 _layoutMark），
 * 组件根带 data-d2c-component；缺标记时回退按子树聚合文本定位（产 warning，不阻断）。
 *
 * 检查项（换算回设计 px 后）：
 *   - height-mismatch (primary)  : |实测高 − DSL高| > max(tolPx, 高×tolPct%)
 *   - top-drift      (derived?)  : |实测 top − DSL top| > max(tolPx, 8)  ← 位置容差用绝对值，不随 top 百分比放大
 *   - bottom-drift   (derived?)  : |实测 bottom − DSL bottom| > 同上
 * 累积漂移去重：height-mismatch 恒为 primary（本地、可定位根因）；某锚点 top-drift 若能被
 * 「其祖先 / 流上方锚点」的同向 height 误差解释，则标 derived（derivedFrom）；bottom-drift
 * 若同节点已有 height/top 误差则 derived。violationCount 只计 primary（可被软闸 ack 处理）。
 *
 * 用法（组件作用域，复杂页串行小闭环）：
 *   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-vertical-check.cjs \
 *     --url <页面完整URL> --page-dir d-d2c/temp/<page-dir> --component <id> \
 *     [--out d-d2c/temp/<page-dir>/components/<id>/]
 *
 * 用法（页面拼装流，校验顶层根及其内部高风险纵向锚点）：
 *   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-vertical-check.cjs \
 *     --url <页面完整URL> --page-dir d-d2c/temp/<page-dir> --page-flow \
 *     [--out d-d2c/temp/<page-dir>/r<N>/]
 *
 * 可选：--scope <mark[,mark…]> --width <678> --wait <3000>
 *      --tolerance-px <4> --tolerance-pct <2>（pct 仅作用于 height；top/bottom 位置用 max(px,8) 绝对容差）--fail-on-violation（primary>0 退出码 3）
 */
const fs = require('fs')
const path = require('path')
const {
  round1,
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
  resolveDesignMeasurementFrame,
} = require('./lib/d2c-dom-measure-core.cjs')
// 结构化运行日志（非侵入、可降级）
let getLogger = () => ({ enabled: false })
try { ({ getLogger } = require('./lib/d2c-run-log.cjs')) } catch { /* 静默降级 */ }
// 登录态/额外 URL query 注入（--url-params 或 D2C_URL_PARAMS），与截图同一受保护 URL 必须贯穿
// 会话态注入（cookie/localStorage，--session-state 或 D2C_SESSION_STATE）：与截图同一会话必须贯穿，否则截图落地、垂直自检却被踢
let appendUrlParams = (u) => u
let maskUrl = (u) => u
let applySessionState = async () => ({ applied: false })
try { ({ appendUrlParams, maskUrl, applySessionState } = require('./lib/d2c-url-params.cjs')) } catch { /* 静默降级 */ }

function isFlexContainer(node) {
  const fi = node && node.flexContainerInfo
  return !!(fi && Object.keys(fi).length)
}

function isFlexGroup(node) {
  return !!(isFlexContainer(node) && (node.children || []).length >= 2)
}

function nodeRotate(node) {
  return (node && (node.rotate || (node.layoutStyle && node.layoutStyle.rotate))) || 0
}

// 非流式定位的文本行：父容器不是 flex（子靠 relativeX/Y 绝对摆放），其纵向位置不由文档流保证，
// 须单独建锚点，否则「绝对定位逐行漂移」整类漏检（flex 内文本由流+容器锚点已覆盖，不在此列）。
// 排除旋转文本（如装饰印章），其包围盒 top 不可直接与 DSL 对账。
function isNonFlowTextRow(node, parent) {
  return !!(
    node &&
    node.type === 'TEXT' &&
    getNodeText(node).trim() &&
    !nodeRotate(node) &&
    parent &&
    !isFlexContainer(parent)
  )
}

function subtreeText(node) {
  let s = ''
  ;(function walk(n) {
    if (!n) return
    if (n.type === 'TEXT') s += getNodeText(n)
    for (const c of n.children || []) walk(c)
  })(node)
  return s
}

/**
 * 收集容器锚点：累加 relativeY 得 H5 绝对 top。
 * scopeMarks 限定子树；rootsOnly 仅用于明确只检查拼装根的调用方。页面级
 * page-flow 默认继续遍历 scope 根内部的 flex group / 非流式文本行，防止
 * 「外框已对齐、卡片内部多行坍到同一 top」被顶层根检查放过。
 */
function collectContainerMarks(nodes, scopeMarks, opts = {}) {
  const rootsOnly = !!opts.rootsOnly
  const scope = scopeMarks && scopeMarks.length ? new Set(scopeMarks) : null
  const out = []
  function walk(node, accY, inScope, parent) {
    if (!node) return
    const b = getBBoxLocal(node)
    const absY = accY + b.y
    const isScopeRoot = !!(scope && scope.has(node._layoutMark))
    const nowInScope = inScope || isScopeRoot
    if (scope && !nowInScope) {
      for (const c of node.children || []) walk(c, absY, false, node)
      return
    }
    const isAnchor = rootsOnly
      ? isScopeRoot
      : isScopeRoot || isFlexGroup(node) || isNonFlowTextRow(node, parent)
    if (isAnchor && node._layoutMark) {
      out.push({
        layoutMark: node._layoutMark,
        type: node.type || '',
        name: node.name || '',
        absY: round1(absY),
        h: round1(b.h),
        bottom: round1(absY + b.h),
        isRoot: isScopeRoot,
        aggText: subtreeText(node),
      })
    }
    for (const c of node.children || []) walk(c, absY, nowInScope, node)
  }
  for (const n of nodes || []) walk(n, 0, !scope, null)
  const seen = new Set()
  return out.filter((a) => {
    if (seen.has(a.layoutMark)) return false
    seen.add(a.layoutMark)
    return true
  })
}

/** 页面拼装流：简单页优先读取 page-flow-plan，复杂页回退 component-plan。 */
function resolvePageFlowRoots(pageDir) {
  const flowFile = path.join(pageDir, 'page-flow-plan.json')
  if (fs.existsSync(flowFile)) {
    try {
      const flowPlan = JSON.parse(fs.readFileSync(flowFile, 'utf8'))
      const roots = (flowPlan.items || [])
        .map((item) => {
          const mark = item && item.layoutMark
          return mark ? { id: item.nodeId || mark, name: item.name || '', mark } : null
        })
        .filter(Boolean)
      if (roots.length) return { roots, source: 'page-flow-plan.json(items)' }
    } catch (_) {
      // 继续尝试复杂页 component-plan；两者都不可用时由调用方硬失败。
    }
  }

  const componentFile = path.join(pageDir, 'component-plan.json')
  if (!fs.existsSync(componentFile)) return null
  let plan
  try {
    plan = JSON.parse(fs.readFileSync(componentFile, 'utf8'))
  } catch (_) {
    return null
  }
  const comps = plan.components || (Array.isArray(plan) ? plan : [])
  const shallowest = (marks) =>
    marks
      .slice()
      .sort(
        (a, b) => a.split('.').length - b.split('.').length || a.localeCompare(b)
      )[0]
  const roots = comps
    .map((c) => {
      const mark =
        c.layoutMark ||
        (Array.isArray(c.layoutMarks) && c.layoutMarks.length
          ? shallowest(c.layoutMarks)
          : null)
      return mark ? { id: c.id, name: c.name || '', mark } : null
    })
    .filter(Boolean)
  return roots.length ? { roots, source: 'component-plan.json(roots)' } : null
}

async function main() {
  const args = parseArgs(process.argv)
  const appSelector =
    args['app-selector'] && args['app-selector'] !== true
      ? args['app-selector']
      : process.env.D2C_APP_SELECTOR || null
  let url =
    (args.url && args.url !== true ? args.url : process.env.D2C_CAPTURE_URL) ||
    null
  const pageDir = args['page-dir'] && args['page-dir'] !== true ? args['page-dir'] : null
  const componentId = args.component && args.component !== true ? args.component : null
  const pageFlow = !!args['page-flow']

  if (!url || !pageDir) {
    console.error(
      '用法: node d2c-vertical-check.cjs --url <full-url> --page-dir d-d2c/temp/<page-dir> [--component <id> | --page-flow | --scope <mark,…>] [--out <dir>]'
    )
    process.exit(1)
  }

  const outDir =
    (args.out && args.out !== true ? args.out : null) ||
    (componentId ? path.join(pageDir, 'components', componentId) : pageDir)

  assertUsableCaptureMaterial(args, outDir, 'd2c-vertical-check')

  // 注入登录态等额外 query 参数（须与截图同一 URL 一致）
  url = appendUrlParams(url, args)

  // 作用域解析
  let scopeMarks = null
  let scopeSource = null
  let rootsOnly = false
  let pageFlowRoots = null
  let effectivePageFlow = pageFlow
  if (pageFlow) {
    const resolvedPageFlow = resolvePageFlowRoots(pageDir)
    if (!resolvedPageFlow) {
      console.error(
        `❌ --page-flow 需要可用的 ${path.join(pageDir, 'page-flow-plan.json')} 或 component-plan.json；禁止降级为普通 page 模式。`
      )
      process.exit(1)
    }
    pageFlowRoots = resolvedPageFlow.roots
    scopeMarks = pageFlowRoots.map((r) => r.mark)
    scopeSource = resolvedPageFlow.source
    rootsOnly = false
  } else if (componentId) {
    const resolved = resolveComponentMarks(pageDir, componentId)
    if (!resolved) {
      console.error(
        `❌ 无法解析组件 ${componentId} 的 layoutMarks（component-meta.json / component-plan.json / complexity-plan.json 均未命中）`
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

  const { nodes } = loadMarkedDsl(pageDir)
  const fullDesignFrameWidth = loadDesignFrameWidth(pageDir)
  const designMeasurementFrame = resolveDesignMeasurementFrame({
    nodes,
    scopeMarks,
    captureMode: measurementContext.captureMode,
    fullDesignFrameWidth,
  })
  const designFrameWidth = designMeasurementFrame.width
  const anchors = collectContainerMarks(nodes, scopeMarks, { rootsOnly })

  console.log(
    `作用域: ${effectivePageFlow ? '页面拼装流' : componentId ? `组件 ${componentId}` : scopeMarks ? '自定义' : '整页'}` +
      `${scopeMarks ? `（来源 ${scopeSource}）` : ''}，容器锚点 ${anchors.length} 个`
  )
  if (!anchors.length) {
    console.error('⚠️ 未收集到容器锚点，跳过纵向自检。')
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

    measured = await page.evaluate(
      (payload) => {
        const { targets, comp, regionCss } = payload
        const normalize = (s) => String(s || '').replace(/\s+/g, ' ').trim()
        const viewportCssWidth = document.documentElement.clientWidth
        const region = regionCss && regionCss.width > 0 ? regionCss : null
        const originTop = region ? region.top : 0
        const effectiveCssWidth = region ? region.width : viewportCssWidth
        const all = Array.from(document.querySelectorAll('body *')).filter((el) => {
          const tag = el.tagName
          if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') return false
          const r = el.getBoundingClientRect()
          return r.width > 0 && r.height > 0
        })
        const results = targets.map((t) => {
          // 1) 优先 data-d2c-mark（在组件作用域内）
          let el = null
          let method = null
          const sels = comp
            ? [
                `[data-d2c-component="${comp}"][data-d2c-mark="${t.layoutMark}"]`,
                `[data-d2c-component="${comp}"] [data-d2c-mark="${t.layoutMark}"]`,
              ]
            : [`[data-d2c-mark="${t.layoutMark}"]`]
          for (const s of sels) {
            try {
              const found = document.querySelector(s)
              if (found) {
                el = found
                method = 'mark'
                break
              }
            } catch (_) {
              /* 非法选择器，忽略 */
            }
          }
          // 组件根：组件元素本身
          if (!el && comp && t.isRoot) {
            const found = document.querySelector(`[data-d2c-component="${comp}"]`)
            if (found) {
              el = found
              method = 'mark'
            }
          }
          // 2) 文本回退：子树聚合文本完全相等、面积最小者
          if (!el) {
            const want = normalize(t.aggText)
            if (want) {
              let best = null
              let bestArea = Infinity
              for (const e of all) {
                if (normalize(e.textContent) !== want) continue
                const r = e.getBoundingClientRect()
                const a = r.width * r.height
                if (a < bestArea) {
                  best = e
                  bestArea = a
                }
              }
              if (best) {
                el = best
                method = 'text-fallback'
              }
            }
          }
          if (!el) return { layoutMark: t.layoutMark, matched: false }
          const r = el.getBoundingClientRect()
          const cs = getComputedStyle(el)
          return {
            layoutMark: t.layoutMark,
            matched: true,
            method,
            position: cs.position,
            rect: { top: r.top - originTop, height: r.height },
          }
        })
        return { viewportCssWidth, effectiveCssWidth, measurementRegionCss: region, results }
      },
      { targets: anchors, comp: effectivePageFlow ? null : componentId, regionCss: measurementContext.regionCss }
    )
  } finally {
    await closeBrowserSession(session)
  }

  const effectiveCssWidth = measured.effectiveCssWidth || measured.viewportCssWidth
  const scale = designFrameWidth / effectiveCssWidth // css px → design px

  // 匹配回填
  const items = anchors.map((a, i) => ({ ...a, m: measured.results[i] }))
  const matched = items.filter((it) => it.m && it.m.matched)
  const warnings = []
  let markMatchedCount = 0
  let textFallbackCount = 0
  for (const it of items) {
    if (!it.m || !it.m.matched) {
      warnings.push({
        layoutMark: it.layoutMark,
        name: it.name,
        kind: 'mark-unresolved',
        note: `data-d2c-mark 与文本回退均未命中（建议给 wrapper 补 data-d2c-mark="${it.layoutMark}"）`,
      })
    } else if (it.m.method === 'mark') {
      markMatchedCount += 1
    } else {
      textFallbackCount += 1
      warnings.push({
        layoutMark: it.layoutMark,
        name: it.name,
        kind: 'text-fallback',
        note: `未找到 data-d2c-mark，按子树文本回退定位（建议补 data-d2c-mark="${it.layoutMark}"）`,
      })
    }
  }

  // 纵向参照：取已匹配锚点中期望 top 最小者（≈ 组件根 / 首个业务块），相对它度量纵向
  let ref = null
  for (const it of matched) {
    if (!ref || it.absY < ref.absY) ref = it
  }
  const refExpectedY = ref ? ref.absY : 0
  const refTop = ref ? ref.m.rect.top : 0

  // 逐锚点算差
  for (const it of matched) {
    const positioned = ['absolute', 'fixed'].includes(it.m.position)
    // 仅「视口/滚动钉死」(fixed/sticky) 不参与文档流纵向对账：其 DSL 流位置≠钉死位置。
    // position:absolute 仍参与——top 是「相对参照锚点(组件根/首块)」度量的，卡内绝对定位行 /
    // 全局浮标(DSL 给了坐标)的相对 top 有意义且应与 DSL 对齐；否则绝对定位逐行漂移会被整类放过（已实测漏报）。
    const viewportPinned = ['fixed', 'sticky'].includes(it.m.position)
    const actualH = it.m.rect.height * scale
    const expectedTop = round1(it.absY - refExpectedY)
    const actualTop = round1((it.m.rect.top - refTop) * scale)
    const actualBottom = round1(actualTop + actualH)
    const expectedBottom = round1(expectedTop + it.h)
    const tolH = Math.max(tolPx, (it.h * tolPct) / 100)
    // 位置容差（top/bottom）用绝对值，不随 expectedTop 做百分比放大：百分比的分母是绝对坐标，
    // 越往页面底部允差越大（如 y≈1847 时 2%≈37px），会放过明显的纵向偏移（实测过 34px 漏报）。
    // 累积舍入（css→设计 px 缩放）在整页量级也只有几 px，8px 绝对容差足够。
    const tolT = Math.max(tolPx, 8)
    it.calc = {
      positioned,
      isRef: ref && it.layoutMark === ref.layoutMark,
      actualH: round1(actualH),
      expectedTop,
      actualTop,
      expectedBottom,
      actualBottom,
      hDiff: round1(actualH - it.h),
      topDiff: round1(actualTop - expectedTop),
      bottomDiff: round1(actualBottom - expectedBottom),
      tolH: round1(tolH),
      tolT: round1(tolT),
      heightFired: Math.abs(actualH - it.h) > tolH,
      viewportPinned,
      // 仅 fixed/sticky 跳过；absolute 参与对账（相对参照锚点度量）
      topFired:
        !viewportPinned && !(ref && it.layoutMark === ref.layoutMark) && Math.abs(actualTop - expectedTop) > tolT,
      bottomFired: !viewportPinned && Math.abs(actualBottom - expectedBottom) > tolT,
    }
  }

  // 根因去重：祖先关系 + 流上方同向 height 误差解释 top-drift
  const isAncestor = (a, b) => b.layoutMark.startsWith(`${a.layoutMark}.`)
  const sign = (n) => (n > 0 ? 1 : n < 0 ? -1 : 0)
  function findTopContributor(x) {
    let best = null
    for (const c of matched) {
      if (c === x || !c.calc.heightFired) continue
      const upstream =
        isAncestor(c, x) || c.absY < x.absY // 祖先 或 流上方
      if (!upstream) continue
      if (sign(c.calc.hDiff) !== sign(x.calc.topDiff)) continue
      // 取最贴近 x 的贡献者（absY 最大者）
      if (!best || c.absY > best.absY) best = c
    }
    return best
  }

  const violations = []
  for (const it of matched) {
    const c = it.calc
    if (c.heightFired) {
      violations.push({
        layoutMark: it.layoutMark,
        name: it.name,
        kind: 'height-mismatch',
        severity: 'primary',
        expected: `高 ≈ ${it.h} 设计px（允差 ${c.tolH}px）`,
        actual: `实测 ${c.actualH} 设计px（差 ${c.hDiff}px）`,
        diffPx: c.hDiff,
      })
    }
    if (c.topFired) {
      const contributor = findTopContributor(it)
      violations.push({
        layoutMark: it.layoutMark,
        name: it.name,
        kind: 'top-drift',
        severity: contributor ? 'derived' : 'primary',
        ...(contributor ? { derivedFrom: contributor.layoutMark } : {}),
        expected: `top ≈ ${c.expectedTop} 设计px（相对参照 ${ref ? ref.layoutMark : '-'}，允差 ${c.tolT}px）`,
        actual: `实测 top ${c.actualTop} 设计px（差 ${c.topDiff}px）${contributor ? `；可由 ${contributor.layoutMark} 高度误差解释` : ''}`,
        diffPx: c.topDiff,
      })
    }
    if (c.bottomFired) {
      const selfCause = c.heightFired || c.topFired
      violations.push({
        layoutMark: it.layoutMark,
        name: it.name,
        kind: 'bottom-drift',
        severity: selfCause ? 'derived' : 'primary',
        ...(selfCause ? { derivedFrom: `${it.layoutMark}(self height/top)` } : {}),
        expected: `bottom ≈ ${c.expectedBottom} 设计px（允差 ${c.tolT}px）`,
        actual: `实测 bottom ${c.actualBottom} 设计px（差 ${c.bottomDiff}px）`,
        diffPx: c.bottomDiff,
      })
    }
  }

  const primaryCount = violations.filter((v) => v.severity === 'primary').length
  const derivedCount = violations.length - primaryCount

  let hostLayout = null
  try {
    hostLayout = JSON.parse(
      fs.readFileSync(path.join(pageDir, 'layout-h5.json'), 'utf8')
    )
  } catch (_) {
    throw new Error('layout-h5.json is required; run d-d2c initialization first')
  }

/**
 * 决定报告应记录哪些 scope。
 * region 物料模式下，测量必须对齐 capture-meta.json 的 designScope（物料 scope）；
 * 若它与本次目标 scope 不同，则额外记录 targetScope，避免「物料对齐了但目标没覆盖」
 * 被报告掩盖。两者相同或非 region 模式时只记录 scope。
 */
  const report = {
    version: '1.0',
    tool: 'd2c-vertical-check',
    mode: effectivePageFlow ? 'page-flow' : componentId ? 'component' : 'page',
    url,
    pageDir: path.resolve(pageDir),
    ...buildReportScopes({
      captureMode: measurementContext.captureMode,
      designScopeMarks: measurementContext.designScopeMarks,
      targetScopeMarks: scopeMarks,
      componentId,
      pageFlow: effectivePageFlow,
      targetScopeSource: scopeSource,
    }),
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
    referenceMark: ref ? ref.layoutMark : null,
    tolerance: {
      px: tolPx,
      pct: tolPct,
      note: 'height 允差 = max(px, 高×pct%)；top/bottom 位置允差 = max(px, 8) 绝对值，不随期望坐标百分比放大',
    },
    checkedCount: anchors.length,
    matchedCount: matched.length,
    markMatchedCount,
    textFallbackCount,
    violationCount: primaryCount, // 软闸只对 primary 负责，避免级联派生噪声
    primaryCount,
    derivedCount,
    violations,
    warnings,
    items: items.map((it) => ({
      layoutMark: it.layoutMark,
      name: it.name,
      type: it.type,
      isRoot: it.isRoot,
      expected: { top: it.calc ? it.calc.expectedTop : null, height: it.h, bottom: it.calc ? it.calc.expectedBottom : null },
      matched: !!(it.m && it.m.matched),
      method: it.m && it.m.matched ? it.m.method : null,
      measured: it.calc
        ? { top: it.calc.actualTop, height: it.calc.actualH, bottom: it.calc.actualBottom, positioned: it.calc.positioned }
        : null,
    })),
  }

  fs.mkdirSync(path.resolve(outDir), { recursive: true })
  const outPath = path.join(path.resolve(outDir), 'vertical-diff.json')
  fs.writeFileSync(outPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8')

  // 结构化日志：记录纵向自检产物与纵向 primary violation / warning
  try {
    const vlog = getLogger(pageDir)
    if (vlog.enabled) {
      vlog.scriptComplete('d2c-vertical-check', { component: componentId || null, url })
      vlog.artifactWrite(outPath)
      if (primaryCount > 0) vlog.checkViolation('vertical', { violationCount: primaryCount, checkedCount: anchors.length, matchedCount: matched.length })
      if (warnings && warnings.length) vlog.checkWarning('vertical', { warningCount: warnings.length })
    }
  } catch { /* 日志失败不影响主流程 */ }

  console.log('===== d2c 纵向几何自检（DOM 实测 vs DSL top/height/bottom） =====')
  console.log(`页面: ${maskUrl(url)}`)
  console.log(
    `锚点: ${anchors.length}，匹配: ${matched.length}（mark ${markMatchedCount} / 文本回退 ${textFallbackCount}），参照: ${ref ? ref.layoutMark : '-'}`
  )
  console.log(`primary violations: ${primaryCount}，derived: ${derivedCount}`)
  console.log(`输出: ${outPath}`)
  if (warnings.length) {
    console.log('')
    console.log('⚠️ 标记缺失/回退（不阻断，建议补 data-d2c-mark）:')
    for (const w of warnings) console.log(`  - ${w.kind} @ ${w.layoutMark}「${w.name}」`)
  }
  if (violations.length) {
    console.log('')
    console.log('违规清单（primary 为可定位根因，derived 为级联派生）:')
    for (const v of violations) {
      console.log(`  - [${v.severity}] ${v.kind} @ ${v.layoutMark}「${v.name}」: ${v.actual}`)
    }
  } else {
    console.log('✅ 全部通过，无纵向几何违规。')
  }

  if (primaryCount && (args['fail-on-violation'] || args.failOnViolation)) {
    process.exit(3)
  }
}

if (require.main === module) {
  main().catch((err) => {
    console.error(err)
    process.exit(1)
  })
}

function buildReportScopes(options = {}) {
  const targetScopeMarks = Array.isArray(options.targetScopeMarks)
    ? options.targetScopeMarks.map(String)
    : []
  const designScopeMarks = Array.isArray(options.designScopeMarks)
    ? options.designScopeMarks.map(String)
    : []
  const useMaterialScope = options.captureMode === 'region' && designScopeMarks.length > 0
  const layoutMarks = useMaterialScope ? designScopeMarks : targetScopeMarks
  if (!layoutMarks.length) return {}
  const result = {
    scope: {
      component: options.componentId || null,
      pageFlow: !!options.pageFlow,
      source: useMaterialScope ? 'capture-meta.json.designScope' : options.targetScopeSource,
      layoutMarks,
    },
  }
  if (useMaterialScope && targetScopeMarks.join(',') !== designScopeMarks.join(',')) {
    result.targetScope = {
      source: options.targetScopeSource,
      layoutMarks: targetScopeMarks,
    }
  }
  return result
}

module.exports = {
  buildReportScopes,
  isFlexContainer,
  isFlexGroup,
  isNonFlowTextRow,
  collectContainerMarks,
  resolvePageFlowRoots,
}
