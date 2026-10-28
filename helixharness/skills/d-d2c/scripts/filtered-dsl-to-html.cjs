#!/usr/bin/env node
/**
 * 将 filter-dsl.cjs 产出的 dsl_visual.json（{ nodes: [...] }）还原为静态 HTML，
 * 用于浏览器快速预览结构、文案与大致布局（非 MasterGo 1:1 渲染，PATH 为色块占位）。
 *
 * 用法:
 *   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/filtered-dsl-to-html.cjs \
 *     --input d-d2c/temp/<page-dir>/dsl_visual.json \
 *     --output d-d2c/temp/<page-dir>/dsl-preview.html
 */

/* eslint-disable no-console */
const fs = require('fs')
const path = require('path')

function parseArgs(argv) {
  const args = {}
  for (let i = 2; i < argv.length; i += 1) {
    const t = argv[i]
    if (t.startsWith('--')) {
      const key = t.slice(2)
      const val =
        argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : true
      args[key] = val
      if (val !== true) i += 1
    }
  }
  return args
}

function escAttr(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

function escHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

function fillToCss(fill) {
  if (!fill) return ''
  if (typeof fill === 'string') return `background-color:${fill};`
  if (fill && typeof fill === 'object' && fill.url) {
    const u = String(fill.url).replace(/'/g, "\\'")
    return `background-image:url('${u}');background-size:cover;background-position:center;background-repeat:no-repeat;`
  }
  return ''
}

function fontToCss(node) {
  const font = node.font || {}
  let s = ''
  if (font.size) s += `font-size:${Number(font.size)}px;`
  if (font.family) s += `font-family:${escAttr(font.family)},-apple-system,sans-serif;`
  if (font.weight) s += `font-weight:${font.weight};`
  const lh = font.lineHeight
  if (lh === 'auto' || lh === '' || lh === undefined)
    s += 'line-height:1.25;'
  else if (lh === '-1') s += 'line-height:normal;'
  else if (!Number.isNaN(Number(lh))) s += `line-height:${Number(lh)}px;`
  else s += `line-height:${escAttr(lh)};`
  if (node.textColor) s += `color:${node.textColor};`
  if (node.textAlign) s += `text-align:${node.textAlign};`
  s +=
    'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center;'
  return s
}

function boxStyle(node, isRoot) {
  const L = node.layout || {}
  const x = L.x ?? 0
  const y = L.y ?? 0
  const w = L.w ?? 0
  const h = L.h ?? 0
  let s = 'box-sizing:border-box;'
  if (isRoot) {
    s += `position:relative;width:${w || 750}px;min-height:${h || 400}px;`
  } else {
    s += `position:absolute;left:${x}px;top:${y}px;`
    if (w > 0) s += `width:${w}px;`
    if (h > 0) s += `height:${h}px;`
    else s += 'min-height:1px;'
  }
  if (node.fill) s += fillToCss(node.fill)
  if (node.borderRadius != null && node.borderRadius !== '') {
    const br = String(node.borderRadius)
    if (/px|%|rem|em$/i.test(br)) s += `border-radius:${br};`
    else s += `border-radius:${br}px;`
  }
  if (node.stroke && node.stroke.color)
    s += `border:${node.stroke.width || '1px'} solid ${node.stroke.color};`
  if (node.flex)
    s += `display:flex;flex-direction:${node.flex === 'column' ? 'column' : 'row'};align-items:center;justify-content:flex-start;`
  return s
}

function renderNode(node, isRoot) {
  const type = node.type || 'NODE'
  const name = node.name || ''

  if (type === 'TEXT') {
    const st = boxStyle(node, isRoot) + fontToCss(node)
    return `<div class="dsl-text" data-type="TEXT" data-name="${escAttr(name)}" style="${st}">${escHtml(node.content || '')}</div>`
  }

  if (type === 'PATH') {
    const pf =
      (node.pathFills && node.pathFills[0] && node.pathFills[0].fill) || '#cccccc'
    const st =
      boxStyle(node, isRoot) +
      `background:${typeof pf === 'string' ? pf : '#ccc'};opacity:0.9;`
    const note = node._note ? escAttr(node._note) : 'PATH'
    return `<div class="dsl-path" data-type="PATH" data-name="${escAttr(name)}" title="${note}" style="${st}"></div>`
  }

  if (type === 'SVG_ELLIPSE') {
    let st = boxStyle(node, isRoot) + 'border-radius:50%;overflow:hidden;'
    st += fillToCss(node.fill)
    return `<div class="dsl-ellipse" data-type="SVG_ELLIPSE" data-name="${escAttr(name)}" style="${st}"></div>`
  }

  const kids = (node.children || []).map((c) => renderNode(c, false)).join('')
  const st = boxStyle(node, isRoot)
  return `<div class="dsl-${type}" data-type="${escAttr(type)}" data-name="${escAttr(name)}" style="${st}">${kids}</div>`
}

function resolveVisualInput(inputPath) {
  const dir = fs.statSync(inputPath).isDirectory()
    ? inputPath
    : path.dirname(inputPath)
  const visualPath = path.join(dir, 'dsl_visual.json')
  if (fs.existsSync(visualPath)) return visualPath
  return inputPath
}

function loadFilteredJson(inputPath) {
  const resolved = resolveVisualInput(inputPath)
  const raw = fs.readFileSync(resolved, 'utf8')
  let obj
  try {
    obj = JSON.parse(raw)
  } catch (e) {
    throw new Error(`JSON 解析失败: ${resolved}`)
  }
  const nodes = obj.nodes
  if (!Array.isArray(nodes)) {
    throw new Error('缺少顶层 nodes 数组（应为 filter-dsl 输出的 dsl_visual.json）')
  }
  return nodes
}

function buildHtml(nodes) {
  const bodies = nodes.map((n) => renderNode(n, true)).join('\n')
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=750, initial-scale=1, maximum-scale=1" />
  <title>DSL 预览（精简）</title>
  <style>
    body { margin: 0; background: #e8e8e8; font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif; }
    .dsl-wrap { padding: 16px 0; }
    .dsl-hint { max-width: 750px; margin: 0 auto 12px; padding: 0 16px; font-size: 13px; color: #666; }
  </style>
</head>
<body>
  <div class="dsl-hint">由精简 DSL 生成的静态预览：布局为绝对坐标嵌套；PATH 为色块占位；图片 fill 使用 background-image。</div>
  <div class="dsl-wrap">${bodies}</div>
</body>
</html>
`
}

function main() {
  const args = parseArgs(process.argv)
  const input =
    (args.input && args.input !== true && path.resolve(args.input)) ||
    path.resolve('d-d2c/temp/dsl/profile-dsl-filtered/dsl_visual.json')
  const output =
    (args.output && args.output !== true && path.resolve(args.output)) ||
    path.resolve('d-d2c/temp/dsl/dsl-preview.html')

  if (!fs.existsSync(input)) {
    console.error(`输入不存在: ${input}`)
    process.exit(1)
  }

  const nodes = loadFilteredJson(input)
  const html = buildHtml(nodes)
  fs.mkdirSync(path.dirname(output), { recursive: true })
  fs.writeFileSync(output, html, 'utf8')
  console.log(
    JSON.stringify({ ok: true, input, output, bytes: Buffer.byteLength(html) }, null, 2)
  )
}

main()
