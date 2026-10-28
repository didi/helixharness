#!/usr/bin/env node
/* eslint-disable no-console */

/**
 * d2c-code-placement-check.cjs
 *
 * Static source placement proof for d-d2c --update-page.
 * It verifies that an implemented block exists in the requested host Vue file
 * and that it appears in the expected relation to a stable host-page anchor.
 *
 * This is intentionally conservative: it proves source structure, not runtime
 * route reachability. Runtime/page screenshots still belong to phase 2.
 */

const fs = require('fs')
const path = require('path')

function parseArgs(argv) {
  const args = {}
  for (let i = 2; i < argv.length; i++) {
    const token = argv[i]
    if (!token.startsWith('--')) continue
    const key = token.slice(2)
    const next = argv[i + 1]
    if (next && !next.startsWith('--')) {
      args[key] = next
      i++
    } else {
      args[key] = true
    }
  }
  return args
}

function usage(exitCode = 0) {
  const out = exitCode ? console.error : console.log
  out(`用法:
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-code-placement-check.cjs \\
    --page-dir d-d2c/temp/<page-dir> \\
    --target <目标页面.vue> \\
    --selector '<新增块根选择器>' \\
    --anchor-text '<宿主锚点文案>' \\
    --relation after

可选:
  --component <name>              等价于 --selector '[data-d2c-component="<name>"]'
  --anchor-selector <selector>    用稳定选择器作为宿主锚点
  --anchor-text <text>            用宿主已有文案作为锚点
  --relation after|below|before|above|inside|near
  --out <path>                    默认写入 <page-dir>/placement-check.json
  --route <url-or-path>           记录真实宿主路由，仅作证据元数据`)
  process.exit(exitCode)
}

function resolvePath(p) {
  if (!p || p === true) return ''
  return path.isAbsolute(p) ? p : path.resolve(process.cwd(), p)
}

function lineInfo(source, index) {
  const safeIndex = Math.max(0, Math.min(source.length, Number(index) || 0))
  const before = source.slice(0, safeIndex)
  const line = before.split(/\r?\n/).length
  const lineStart = source.lastIndexOf('\n', safeIndex - 1) + 1
  const lineEndRaw = source.indexOf('\n', safeIndex)
  const lineEnd = lineEndRaw === -1 ? source.length : lineEndRaw
  return {
    line,
    column: safeIndex - lineStart + 1,
    text: source.slice(lineStart, lineEnd).trim(),
  }
}

function regexEscape(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function normalizeSelector(selector, component) {
  if (selector && selector !== true) return String(selector).trim()
  if (component && component !== true) return `[data-d2c-component="${String(component).trim()}"]`
  return ''
}

function parseAttributeSelector(selector) {
  const m = String(selector || '').match(/^\[\s*([A-Za-z_:@][\w:.-]*)(?:\s*=\s*["']?([^"'\]]+)["']?)?\s*\]$/)
  if (!m) return null
  return { name: m[1], value: m[2] || null }
}

function findSelector(source, selector) {
  const raw = String(selector || '').trim()
  if (!raw) return null

  const attr = parseAttributeSelector(raw)
  if (attr) {
    let re
    if (attr.value === null) {
      re = new RegExp(`\\b${regexEscape(attr.name)}(?:\\s*=|\\s|>|/)`, 'm')
    } else {
      re = new RegExp(`\\b${regexEscape(attr.name)}\\s*=\\s*(["'])${regexEscape(attr.value)}\\1`, 'm')
    }
    const match = re.exec(source)
    if (match) return { index: match.index, matched: match[0], kind: 'attribute' }
  }

  if (raw.startsWith('.')) {
    const cls = raw.slice(1)
    const re = new RegExp(`\\bclass\\s*=\\s*(["'])[^"']*(?:^|\\s)${regexEscape(cls)}(?:\\s|$)[^"']*\\1`, 'm')
    const match = re.exec(source)
    if (match) return { index: match.index, matched: match[0], kind: 'class' }
  }

  if (raw.startsWith('#')) {
    const id = raw.slice(1)
    const re = new RegExp(`\\bid\\s*=\\s*(["'])${regexEscape(id)}\\1`, 'm')
    const match = re.exec(source)
    if (match) return { index: match.index, matched: match[0], kind: 'id' }
  }

  const idx = source.indexOf(raw)
  if (idx >= 0) return { index: idx, matched: raw, kind: 'literal' }
  return null
}

function findText(source, text) {
  const raw = String(text || '')
  if (!raw) return null
  const idx = source.indexOf(raw)
  if (idx >= 0) return { index: idx, matched: raw, kind: 'text' }
  const collapsed = raw.replace(/\s+/g, ' ').trim()
  if (collapsed && collapsed !== raw) {
    const sourceCollapsedIndex = source.replace(/\s+/g, ' ').indexOf(collapsed)
    if (sourceCollapsedIndex >= 0) {
      return { index: sourceCollapsedIndex, matched: collapsed, kind: 'text-collapsed' }
    }
  }
  return null
}

function findTagStart(source, index) {
  const start = source.lastIndexOf('<', index)
  if (start < 0) return -1
  const close = source.lastIndexOf('>', index)
  if (close > start) return -1
  return start
}

function findTagEnd(source, tagStart) {
  const end = source.indexOf('>', tagStart)
  return end < 0 ? tagStart : end
}

function tagNameAt(source, tagStart) {
  if (tagStart < 0) return ''
  const m = source.slice(tagStart, tagStart + 80).match(/^<\s*([A-Za-z][\w.-]*)\b/)
  return m ? m[1] : ''
}

function isSelfClosingTag(source, tagStart) {
  const end = findTagEnd(source, tagStart)
  return /\/\s*>$/.test(source.slice(tagStart, end + 1))
}

function estimateElementRange(source, matchIndex) {
  const tagStart = findTagStart(source, matchIndex)
  const name = tagNameAt(source, tagStart)
  if (!name) return null
  const openEnd = findTagEnd(source, tagStart)
  if (isSelfClosingTag(source, tagStart)) return { start: tagStart, end: openEnd + 1, tag: name }

  const tokenRe = new RegExp(`<\\s*(/?)\\s*${regexEscape(name)}\\b[^>]*>`, 'gi')
  tokenRe.lastIndex = tagStart
  let depth = 0
  let match
  while ((match = tokenRe.exec(source))) {
    const token = match[0]
    const closing = Boolean(match[1])
    const selfClosing = /\/\s*>$/.test(token)
    if (!closing && !selfClosing) depth++
    if (closing) depth--
    if (depth === 0) return { start: tagStart, end: tokenRe.lastIndex, tag: name }
  }
  return { start: tagStart, end: openEnd + 1, tag: name }
}

function normalizeRelation(value) {
  const raw = String(value || 'after').trim().toLowerCase()
  if (raw === 'below') return 'after'
  if (raw === 'above') return 'before'
  if (['after', 'before', 'inside', 'near'].includes(raw)) return raw
  return 'after'
}

function relationSatisfied(source, component, anchor, relation) {
  if (!component || !anchor) {
    return { ok: false, detail: 'component-or-anchor-missing' }
  }
  const rel = normalizeRelation(relation)
  const componentLine = lineInfo(source, component.index).line
  const anchorLine = lineInfo(source, anchor.index).line
  if (rel === 'after') {
    return {
      ok: component.index > anchor.index,
      detail: component.index > anchor.index ? `component line ${componentLine} is after anchor line ${anchorLine}` : `component line ${componentLine} is not after anchor line ${anchorLine}`,
    }
  }
  if (rel === 'before') {
    return {
      ok: component.index < anchor.index,
      detail: component.index < anchor.index ? `component line ${componentLine} is before anchor line ${anchorLine}` : `component line ${componentLine} is not before anchor line ${anchorLine}`,
    }
  }
  if (rel === 'near') {
    const delta = Math.abs(componentLine - anchorLine)
    return {
      ok: delta <= 80,
      detail: `line distance=${delta}`,
    }
  }
  if (rel === 'inside') {
    const range = estimateElementRange(source, anchor.index)
    if (!range || range.end <= range.start) {
      return { ok: false, detail: 'anchor element range could not be estimated' }
    }
    return {
      ok: component.index > range.start && component.index < range.end,
      detail: component.index > range.start && component.index < range.end
        ? `component is inside anchor <${range.tag}>`
        : `component is outside anchor <${range.tag}> range`,
    }
  }
  return { ok: false, detail: `unsupported relation=${relation}` }
}

function snippetAround(source, index, radius = 3) {
  const lines = source.split(/\r?\n/)
  const line = lineInfo(source, index).line
  const start = Math.max(1, line - radius)
  const end = Math.min(lines.length, line + radius)
  const out = []
  for (let n = start; n <= end; n++) {
    out.push(`${n}: ${lines[n - 1]}`)
  }
  return out
}

function scanRisks(source, component) {
  if (!component) return []
  const range = estimateElementRange(source, component.index)
  const fallbackStart = Math.max(0, component.index - 800)
  const fallbackEnd = Math.min(source.length, component.index + 1600)
  const chunk = range && range.end > range.start
    ? source.slice(range.start, Math.min(range.end, range.start + 5000))
    : source.slice(fallbackStart, fallbackEnd)
  const warnings = []
  if (/position\s*:\s*(fixed|absolute)\b/i.test(chunk)) {
    warnings.push('implemented block contains fixed/absolute positioning; confirm it participates in the host page flow when required')
  }
  if (/display\s*:\s*none\b/i.test(chunk) || /\bv-show\s*=\s*["']false["']/i.test(chunk)) {
    warnings.push('implemented block appears hidden in source')
  }
  if (/\bv-if\s*=/.test(chunk)) {
    warnings.push('implemented block/root contains v-if; ensure the condition is true for the target page state')
  }
  return warnings
}

function writeJson(outPath, data) {
  const dir = path.dirname(outPath)
  fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(outPath, `${JSON.stringify(data, null, 2)}\n`, 'utf8')
}

function main() {
  const args = parseArgs(process.argv)
  if (args.help || args.h) usage(0)

  const pageDir = resolvePath(args['page-dir'])
  const targetPath = resolvePath(args.target || args['target-vue'] || args.file)
  const selector = normalizeSelector(args.selector, args.component)
  const relation = normalizeRelation(args.relation)
  const outPath = resolvePath(args.out) || (pageDir ? path.join(pageDir, 'placement-check.json') : '')

  if (!targetPath || !selector || !outPath) {
    console.error('❌ 缺少必要参数：--target、--selector/--component、--page-dir 或 --out')
    usage(2)
  }
  if (!fs.existsSync(targetPath)) {
    console.error(`❌ 目标 Vue 文件不存在: ${targetPath}`)
    process.exit(2)
  }

  const source = fs.readFileSync(targetPath, 'utf8')
  const componentMatch = findSelector(source, selector)
  const anchorSelector = args['anchor-selector'] && args['anchor-selector'] !== true ? String(args['anchor-selector']).trim() : ''
  const anchorText = args['anchor-text'] && args['anchor-text'] !== true ? String(args['anchor-text']) : ''
  const anchorMatch = anchorSelector
    ? findSelector(source, anchorSelector)
    : (anchorText ? findText(source, anchorText) : null)

  const failures = []
  const warnings = []
  if (!componentMatch) failures.push(`未找到新增/改动块根选择器: ${selector}`)
  if (!anchorSelector && !anchorText) {
    failures.push('缺少宿主页锚点：请传 --anchor-selector 或 --anchor-text，避免只能证明组件存在、不能证明页面内位置')
  } else if (!anchorMatch) {
    failures.push(`未找到宿主页锚点: ${anchorSelector || anchorText}`)
  }

  let relationResult = { ok: false, detail: 'not-evaluated' }
  if (componentMatch && anchorMatch) {
    relationResult = relationSatisfied(source, componentMatch, anchorMatch, relation)
    if (!relationResult.ok) {
      failures.push(`新增/改动块与宿主锚点的 relation=${relation} 不满足：${relationResult.detail}`)
    }
  }
  warnings.push(...scanRisks(source, componentMatch))

  const componentInfo = componentMatch
    ? { ...lineInfo(source, componentMatch.index), selector, matchKind: componentMatch.kind, snippet: snippetAround(source, componentMatch.index) }
    : { selector }
  const anchorInfo = anchorMatch
    ? { ...lineInfo(source, anchorMatch.index), selector: anchorSelector || null, text: anchorText || null, matchKind: anchorMatch.kind, snippet: snippetAround(source, anchorMatch.index) }
    : { selector: anchorSelector || null, text: anchorText || null }

  const status = failures.length ? 'failed' : 'passed'
  const report = {
    tool: 'd2c-code-placement-check',
    version: 1,
    mode: 'update-page',
    status,
    targetVue: targetPath,
    route: args.route && args.route !== true ? String(args.route) : null,
    pageDir: pageDir || null,
    expected: {
      selector,
      anchorSelector: anchorSelector || null,
      anchorText: anchorText || null,
      relation,
    },
    evidence: {
      componentFound: Boolean(componentMatch),
      anchorFound: Boolean(anchorMatch),
      relationSatisfied: Boolean(relationResult.ok),
      relationDetail: relationResult.detail,
      component: componentInfo,
      anchor: anchorInfo,
    },
    warnings,
    failures,
    generatedAt: new Date().toISOString(),
  }

  writeJson(outPath, report)
  if (status === 'passed') {
    console.log(`✅ update-page 源码落位检查通过: ${outPath}`)
  } else {
    console.error(`❌ update-page 源码落位检查失败: ${outPath}`)
    failures.forEach((f) => console.error(`   - ${f}`))
  }
  if (warnings.length) {
    console.error(`⚠️ warnings: ${warnings.join('；')}`)
  }
  process.exit(status === 'passed' ? 0 : 1)
}

main()
