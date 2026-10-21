'use strict'

/**
 * d2c-geometry-check 的判定核心（纯函数，不依赖浏览器）。
 *
 * 与 d2c-render-check-core.cjs 同构：浏览器只负责「按文案找到元素并实测」，
 * 判定与分级全部在这里完成，便于单测覆盖。
 *
 * 匹配分级（matchType → confidence）：
 *   exact                                   → exact   文本完全相等，可判几何
 *   contains                                → contains 只确认文本存在
 *   css-fallback / text-fuzzy / position-heuristic → guess 启发式猜测
 *
 * 只有 exact 能产出几何违规。contains / guess 的实测盒子来自「猜中的元素」，
 * 用它的宽高/左右缘去判漂移只会制造假违规——历史行为对 guess 也判了几何，
 * 这是「使用者白跑一轮」的主要来源之一。
 */

/** 匹配类型 → 置信度。 */
function classifyMatchConfidence(matchType) {
  const type = matchType ? String(matchType) : 'exact'
  if (type === 'exact') return 'exact'
  if (type === 'contains') return 'contains'
  return 'guess'
}

function round1(value) {
  return Math.round(Number(value || 0) * 10) / 10
}

function round2(value) {
  return Math.round(Number(value || 0) * 100) / 100
}

/**
 * @param {Array} textNodes 目标 TEXT 节点（含 layoutMark / text / w / absX / fontSize）
 * @param {object} measured 浏览器返回 { results, effectiveCssWidth, viewportCssWidth, measurementRegionCss }
 * @param {object} options { designFrameWidth, tolPx, tolPct }
 */
function evaluateGeometryTargets(textNodes, measured, options) {
  const { designFrameWidth, tolPx, tolPct } = options || {}
  const effectiveCssWidth = (measured && measured.effectiveCssWidth) || (measured && measured.viewportCssWidth)
  const scale = designFrameWidth / effectiveCssWidth // css px → design px
  const results = (measured && measured.results) || []
  const violations = []
  const warnings = []
  const items = []
  const lowConfidenceMatches = []
  let matchedCount = 0

  for (let i = 0; i < textNodes.length; i += 1) {
    const t = textNodes[i]
    const m = results[i]
    const item = {
      layoutMark: t.layoutMark,
      text: t.text,
      expected: { w: t.w, absX: t.absX, fontSize: t.fontSize },
      matched: !!(m && m.matched),
    }

    if (!m || !m.matched) {
      violations.push({
        layoutMark: t.layoutMark,
        text: t.text,
        kind: 'text-missing',
        expected: `文案「${t.text}」存在于页面`,
        actual: 'DOM 中未找到等值文本（规范化比较）',
        diffPx: null,
        severityHint: 'major',
      })
      items.push(item)
      continue
    }

    matchedCount += 1
    const confidence = classifyMatchConfidence(m.matchType)
    item.matchType = m.matchType || 'exact'
    item.matchConfidence = confidence
    item.method = item.matchType

    if (confidence !== 'exact') {
      // 猜中的元素只能证明「文本存在」，不能用来判尺寸/边缘漂移，
      // 否则会把放宽的匹配变成新的几何误报。
      item.measured = {
        matchType: item.matchType,
        matchConfidence: confidence,
        whiteSpace: m.whiteSpace,
        fontSizeCss: m.fontSizeCss,
      }
      if (confidence === 'guess') {
        lowConfidenceMatches.push({
          layoutMark: t.layoutMark,
          matchType: item.matchType,
          text: t.text,
        })
      }
      items.push(item)
      continue
    }

    const wDesign = m.rect.width * scale
    const leftDesign = m.rect.left * scale
    const rightDesign = leftDesign + wDesign
    const expectedRight = t.absX + t.w
    item.measured = {
      matchType: item.matchType,
      matchConfidence: confidence,
      wDesign: round1(wDesign),
      leftDesign: round1(leftDesign),
      whiteSpace: m.whiteSpace,
      fontSizeCss: m.fontSizeCss,
    }

    const tol = Math.max(tolPx, (t.w * tolPct) / 100)

    // 1) 空白折叠（最高优先：根因类）
    const hasConsecutiveWs = /[  　]{2,}|\t/.test(t.text)
    const collapsingWs = ['normal', 'nowrap', 'pre-line'].includes(m.whiteSpace)
    if (hasConsecutiveWs && collapsingWs) {
      violations.push({
        layoutMark: t.layoutMark,
        text: t.text,
        kind: 'whitespace-collapsed',
        expected: `保留 DSL 空白序列（white-space: pre 或 &nbsp;），文案「${t.text.replace(/ /g, '␣')}」`,
        actual: `computed white-space=${m.whiteSpace}，连续空白被折叠 → 渲染宽度小于 DSL bbox，右端累积漂移`,
        diffPx: round1(t.w - wDesign),
        severityHint: 'major',
      })
    }

    // 2) 宽度（文本左对齐时宽度差≈右端漂移）
    const wDiff = wDesign - t.w
    if (Math.abs(wDiff) > tol) {
      violations.push({
        layoutMark: t.layoutMark,
        text: t.text,
        kind: 'width-mismatch',
        expected: `渲染宽 ≈ ${t.w} 设计px（允差 ${round1(tol)}px）`,
        actual: `实测 ${round1(wDesign)} 设计px（差 ${round1(wDiff)}px；文本左对齐时即右端漂移）`,
        diffPx: round1(wDiff),
        severityHint: Math.abs(wDiff) > Math.max(8, tol * 2) ? 'major' : 'minor',
      })
    }

    // 3) 左/右缘（X 轴；DSL absX 由祖先 relativeX 累加，异常值跳过）
    const absXValid = t.absX >= 0 && t.absX <= designFrameWidth
    if (absXValid) {
      const leftDiff = leftDesign - t.absX
      if (Math.abs(leftDiff) > Math.max(6, tol)) {
        violations.push({
          layoutMark: t.layoutMark,
          text: t.text,
          kind: 'left-edge-drift',
          expected: `左缘 ≈ x=${t.absX} 设计px`,
          actual: `实测左缘 x=${round1(leftDesign)}（差 ${round1(leftDiff)}px）`,
          diffPx: round1(leftDiff),
          severityHint: Math.abs(leftDiff) > 12 ? 'major' : 'minor',
        })
      }
      const rightDiff = rightDesign - expectedRight
      if (Math.abs(rightDiff) > Math.max(6, tol)) {
        violations.push({
          layoutMark: t.layoutMark,
          text: t.text,
          kind: 'right-edge-drift',
          expected: `右缘 ≈ x=${expectedRight} 设计px`,
          actual: `实测右缘 x=${round1(rightDesign)}（差 ${round1(rightDiff)}px）`,
          diffPx: round1(rightDiff),
          severityHint: Math.abs(rightDiff) > 12 ? 'major' : 'minor',
        })
      }
    }

    // 4) 字号（DSL design px → css px）
    if (t.fontSize && m.fontSizeCss) {
      const expectedCss = t.fontSize / scale
      const fsDiff = m.fontSizeCss - expectedCss
      if (Math.abs(fsDiff) > 1.5) {
        violations.push({
          layoutMark: t.layoutMark,
          text: t.text,
          kind: 'font-size-mismatch',
          expected: `font-size ≈ ${round2(expectedCss)} css px（DSL ${t.fontSize} 设计px）`,
          actual: `实测 ${m.fontSizeCss} css px（差 ${round2(fsDiff)}px）`,
          diffPx: round2(fsDiff),
          severityHint: Math.abs(fsDiff) > 4 ? 'major' : 'minor',
        })
      }
    }

    items.push(item)
  }

  if (lowConfidenceMatches.length) {
    warnings.push({
      kind: 'low-confidence-match',
      count: lowConfidenceMatches.length,
      expected: '每个 TEXT 节点都有 data-d2c-mark（或精确/包含匹配）作为几何证据',
      actual: `${lowConfidenceMatches.length} 个节点由启发式回退匹配命中，已记录存在性但未做几何判定`,
      repairHint:
        '这些节点的尺寸/边缘漂移未被验证。给对应节点补 data-d2c-mark 后重跑，或改用 --fallback-strategy off 让缺失节点显式报 text-missing。',
      layoutMarks: lowConfidenceMatches.map((entry) => entry.layoutMark),
    })
  }

  return {
    effectiveCssWidth,
    scale,
    violations,
    warnings,
    items,
    matchedCount,
    lowConfidenceMatches,
  }
}

module.exports = {
  classifyMatchConfidence,
  evaluateGeometryTargets,
}
