/**
 * 从任意 MasterGo DSL 提取静态资源（通用，无业务硬编码规则）。
 *
 * 策略：
 * - fill.url（image-resource）→ 下载位图，按 URL 去重
 * - PATH + 纯色填充 → 导出 SVG，按 path data 去重
 * - PATH + 渐变/复杂填充 → needs_mastergo_export
 * - 文件名：节点名 / 祖先有意义名称 / 兄弟 TEXT / layoutMark / nodeId
 * - 可选覆盖：同目录 asset-aliases.json（nodeId → fileBase）
 */

const fs = require('fs')
const path = require('path')
const https = require('https')
const http = require('http')
const crypto = require('crypto')
const { URL } = require('url')
const { loadDslObject } = require('./dsl-layout-core.cjs')
const { buildStyleMaps, resolvePaint, paintToCssValue } = require('./dsl-style-core.cjs')

const DEFAULT_OPTIONS = {
  /** 短边 ≤ 该值（px，750 稿）的位图/矢量落 icons/，否则 images/ */
  iconMaxSide: 96,
  /** 单轴超过该值且无意义节点名的 PATH，视为大面积装饰，跳过 */
  skipPathMaxSide: 700,
}

/** 不宜直接作文件名的 MasterGo 默认命名 */
const MEANINGLESS_NAME = /^(位图|矩形|圆形|椭圆形|路径|蒙版|编组\s*\d*|容器\s*\d+|组\s*\d+|Frame\s*\d*|Group\s*\d+|Symbol|Symbols|Combined\s+Shape(\s+Copy\s*\d+)?|icon\d+\/|Slice\s*\d*|\d+)$/i

/** 装饰性 PATH：不参与导出（分割线用 CSS border 即可） */
function isDecorativePathName(name) {
  const n = String(name || '').trim()
  if (/^直线/.test(n) || /^分割线/.test(n)) return true
  if (/^(矩形|圆角矩形|蒙版)$/.test(n)) return true
  return false
}

const MEANINGLESS_PARENT = /^容器\s*\d+$/

function round1(n) {
  return Math.round(Number(n || 0) * 10) / 10
}

function resolveAssetsDirFromPageDir(pageDir, workspaceRoot) {
  const root = workspaceRoot || process.cwd()
  const m = String(pageDir || '').match(/^(.+)-d2c-(.+)$/)
  if (m) {
    return path.join(root, 'assets/images', m[1], `d2c-${m[2]}`)
  }
  return path.join(root, 'assets/images', pageDir)
}

function slugify(text) {
  return String(text || '')
    .trim()
    .toLowerCase()
    .replace(/\s+/g, '-')
    .replace(/[^a-z0-9\u4e00-\u9fa5-]/g, '')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 80)
}

function isMeaningfulName(name) {
  const n = String(name || '').trim()
  return n.length > 0 && !MEANINGLESS_NAME.test(n)
}

function hashKey(input) {
  return crypto.createHash('md5').update(String(input)).digest('hex').slice(0, 10)
}

function nodeIdSlug(id) {
  return String(id || 'unknown').replace(/:/g, '-')
}

function bboxMaxSide(layout) {
  const w = Number(layout?.width) || 0
  const h = Number(layout?.height) || 0
  return Math.max(w, h)
}

function resolveBucket(layout, opts) {
  return bboxMaxSide(layout) <= (opts.iconMaxSide || DEFAULT_OPTIONS.iconMaxSide)
    ? 'icons'
    : 'images'
}

function loadAssetAliases(inputPath) {
  const dir = path.dirname(path.resolve(inputPath))
  const aliasPath = path.join(dir, 'asset-aliases.json')
  if (!fs.existsSync(aliasPath)) return { path: null, map: {} }
  try {
    const raw = JSON.parse(fs.readFileSync(aliasPath, 'utf8'))
    const map = raw.aliases || raw
    return { path: aliasPath, map: map && typeof map === 'object' ? map : {} }
  } catch (_) {
    return { path: aliasPath, map: {} }
  }
}

function loadStrippedChromePaths(inputPath) {
  const dir = path.dirname(path.resolve(inputPath))
  const p = path.join(dir, 'stripped-chrome.json')
  if (!fs.existsSync(p)) return new Set()
  try {
    const raw = JSON.parse(fs.readFileSync(p, 'utf8'))
    return new Set((raw.items || []).map((i) => i.path).filter(Boolean))
  } catch (_) {
    return new Set()
  }
}

function collectContextLabels(node, ctx) {
  const labels = []
  for (const p of ctx.parentNames || []) {
    if (isMeaningfulName(p) && !MEANINGLESS_PARENT.test(p)) labels.push(p)
  }
  const nodesToScan = [...(ctx.ancestors || []), ctx.parentNode].filter(Boolean)
  for (const anc of nodesToScan) {
    for (const sib of anc.children || []) {
      if (sib.type === 'TEXT') {
        const t =
          (sib.text || []).map((x) => x.text).filter(Boolean).join('') || sib.name
        if (t && isMeaningfulName(t)) labels.push(t)
      }
    }
  }
  if (isMeaningfulName(node.name)) labels.push(node.name)
  return [...new Set(labels)]
}

function buildFileBase(node, ctx, aliases) {
  const override = aliases[node.id] || aliases[String(node.id)]
  if (override) return slugify(override)

  const labels = collectContextLabels(node, ctx)
  let base = ''

  if (isMeaningfulName(node.name)) {
    base = slugify(node.name)
  } else if (labels.length) {
    base = slugify(labels[labels.length - 1])
    if (labels.length > 1) {
      const parent = slugify(labels[labels.length - 2])
      if (parent && parent !== base) base = `${parent}-${base}`
    }
  } else if (node._layoutMark) {
    base = slugify(String(node._layoutMark).replace(/\./g, '-'))
  }

  if (!base) base = `node-${nodeIdSlug(node.id)}`
  return base
}

function extractImageUrl(node, paintMap) {
  const fill = resolvePaint(paintMap, node.fill)
  if (fill && typeof fill === 'object' && fill.url) return String(fill.url)
  if (Array.isArray(fill)) {
    for (const item of fill) {
      if (item && item.url) return String(item.url)
    }
  }
  return null
}

function pathFillIsSolid(node, paintMap) {
  const paths = node.path || []
  if (!paths.length) return null
  const fills = paths.map((p) => resolvePaint(paintMap, p.fill)).filter(Boolean)
  if (fills.length !== paths.length) return null
  const colors = fills.map((f) => paintToCssValue(f)).filter(Boolean)
  if (!colors.length || colors.some((c) => String(c).includes('gradient'))) return null
  return colors[0]
}

function pathDataHash(node) {
  const d = (node.path || []).map((p) => p.data).filter(Boolean).join('|')
  const t = (node.path || []).map((p) => p.transform || '').join('|')
  const fills = (node.path || []).map((p) => p.fill || '').join('|')
  const render = [
    node.opacity == null ? '' : node.opacity,
    node.fillRule || node.fillrule || '',
    node.clipRule || node.cliprule || '',
  ].join('|')
  return hashKey(`${d}::${t}::${fills}::${render}`)
}

function pathSubpathBBoxes(d) {
  return String(d || '')
    .split(/(?=[Mm])/)
    .map((part) => {
      if (!/[Zz]/.test(part)) return null
      const nums = part.match(/-?[\d.]+(?:e[-+]?\d+)?/gi)
      if (!nums || nums.length < 4) return null
      let minX = Infinity
      let minY = Infinity
      let maxX = -Infinity
      let maxY = -Infinity
      for (let i = 0; i + 1 < nums.length; i += 2) {
        const x = Number(nums[i])
        const y = Number(nums[i + 1])
        if (!Number.isFinite(x) || !Number.isFinite(y)) continue
        minX = Math.min(minX, x)
        minY = Math.min(minY, y)
        maxX = Math.max(maxX, x)
        maxY = Math.max(maxY, y)
      }
      return Number.isFinite(minX) ? { minX, minY, maxX, maxY } : null
    })
    .filter(Boolean)
}

function boxContains(outer, inner) {
  const eps = 0.01
  const contains =
    outer.minX <= inner.minX + eps &&
    outer.minY <= inner.minY + eps &&
    outer.maxX >= inner.maxX - eps &&
    outer.maxY >= inner.maxY - eps
  const strictlyLarger =
    outer.maxX - outer.minX > inner.maxX - inner.minX + eps ||
    outer.maxY - outer.minY > inner.maxY - inner.minY + eps
  return contains && strictlyLarger
}

function normalizeFillRule(value) {
  if (!value) return null
  const compact = String(value).toLowerCase().replace(/[-_\s]/g, '')
  if (compact === 'evenodd') return 'evenodd'
  if (compact === 'nonzero') return 'nonzero'
  return null
}

/**
 * MasterGo 的复合轮廓常以同一 PATH 内的多个闭合子路径表达孔洞，但部分 DSL
 * 不携带 fillRule。仅在轮廓存在几何包含关系时推导 evenodd；互不相交的图形
 * 不改变默认填充规则。
 */
function inferCompoundFillRule(node) {
  const explicit = normalizeFillRule(node.fillRule || node.fillrule)
  if (explicit) return explicit
  for (const item of node.path || []) {
    const boxes = pathSubpathBBoxes(item.data)
    for (let i = 0; i < boxes.length; i += 1) {
      for (let j = 0; j < boxes.length; j += 1) {
        if (i !== j && boxContains(boxes[i], boxes[j])) return 'evenodd'
      }
    }
  }
  return null
}

/** 从 path d 解析坐标范围（MasterGo 常大于 layoutStyle，避免 viewBox 裁切） */
function pathBBoxFromData(pathItems) {
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const item of pathItems) {
    const d = item.data || ''
    const nums = d.match(/-?[\d.]+(?:e[-+]?\d+)?/gi)
    if (!nums || nums.length < 2) continue
    for (let i = 0; i + 1 < nums.length; i += 2) {
      const x = parseFloat(nums[i])
      const y = parseFloat(nums[i + 1])
      if (!Number.isNaN(x)) {
        minX = Math.min(minX, x)
        maxX = Math.max(maxX, x)
      }
      if (!Number.isNaN(y)) {
        minY = Math.min(minY, y)
        maxY = Math.max(maxY, y)
      }
    }
  }
  if (!Number.isFinite(minX)) return null
  const pad = 0.5
  return {
    minX: minX - pad,
    minY: minY - pad,
    width: maxX - minX + pad * 2,
    height: maxY - minY + pad * 2,
  }
}

function pathDataToSvg(node, solidFill) {
  const paths = node.path || []
  const d = paths.map((p) => p.data).filter(Boolean).join(' ')
  if (!d) return null

  const fromPath = pathBBoxFromData(paths)
  const layoutW = Number(node.layoutStyle?.width) || 0
  const layoutH = Number(node.layoutStyle?.height) || 0
  const bbox =
    fromPath ||
    (layoutW > 0 && layoutH > 0
      ? { minX: 0, minY: 0, width: layoutW, height: layoutH }
      : { minX: 0, minY: 0, width: 1, height: 1 })

  const w = Math.max(1, Math.ceil(bbox.width))
  const h = Math.max(1, Math.ceil(bbox.height))
  const viewBox = `${round1(bbox.minX)} ${round1(bbox.minY)} ${round1(bbox.width)} ${round1(bbox.height)}`

  const transform = paths.find((p) => p.transform)?.transform
  const fillRule = inferCompoundFillRule(node)
  const clipRule = normalizeFillRule(node.clipRule || node.cliprule) || fillRule
  const opacity = Number(node.opacity)
  const attrs = [
    `d="${d}"`,
    `fill="${solidFill}"`,
    fillRule ? `fill-rule="${fillRule}"` : '',
    clipRule ? `clip-rule="${clipRule}"` : '',
    Number.isFinite(opacity) && opacity >= 0 && opacity < 1
      ? `opacity="${opacity}"`
      : '',
  ]
    .filter(Boolean)
    .join(' ')
  const inner = transform
    ? `<g transform="${transform}"><path ${attrs}/></g>`
    : `<path ${attrs}/>`
  return [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="${viewBox}" fill="none">`,
    inner,
    '</svg>',
  ].join('')
}

function downloadUrl(url, destPath, timeoutMs = 60000) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(url)
    const lib = parsed.protocol === 'https:' ? https : http
    const req = lib.get(
      url,
      { timeout: timeoutMs, headers: { 'User-Agent': 'd-d2c-extract-assets/1.0' } },
      (res) => {
        if (res.statusCode && res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          downloadUrl(res.headers.location, destPath, timeoutMs).then(resolve).catch(reject)
          return
        }
        if (!res.statusCode || res.statusCode >= 400) {
          reject(new Error(`HTTP ${res.statusCode} for ${url}`))
          return
        }
        const chunks = []
        res.on('data', (c) => chunks.push(c))
        res.on('end', () => {
          fs.mkdirSync(path.dirname(destPath), { recursive: true })
          fs.writeFileSync(destPath, Buffer.concat(chunks))
          resolve(destPath)
        })
      }
    )
    req.on('error', reject)
    req.on('timeout', () => {
      req.destroy(new Error(`timeout: ${url}`))
    })
  })
}

function extFromUrl(url, fallback = '.png') {
  try {
    const ext = path.extname(new URL(url).pathname).toLowerCase()
    if (ext && ext.length <= 5) return ext
  } catch (_) {
    /* ignore */
  }
  return fallback
}

function makeRef(node, ctx) {
  return {
    nodeId: node.id,
    nodeName: node.name,
    nodeType: node.type,
    layoutMark: node._layoutMark || null,
    path: [...(ctx.parentNames || []), node.name].join(' > '),
  }
}

function ensureUniqueFileBase(base, used) {
  let candidate = base
  let i = 2
  while (used.has(candidate)) {
    candidate = `${base}-${i}`
    i += 1
  }
  used.add(candidate)
  return candidate
}

/**
 * 遍历 DSL，收集所有可落盘资源（与具体业务无关）。
 */
function collectAssetCandidates(nodes, paintMap, opts = {}) {
  const options = { ...DEFAULT_OPTIONS, ...opts }
  const aliases = opts.aliases || {}
  const chromePaths = opts.chromePaths || new Set()
  const rasterByUrl = new Map()
  const vectorByHash = new Map()
  const usedFileBases = new Set()

  function isChromePath(ctx, node) {
    const p = [...(ctx.parentNames || []), node.name].join(' > ')
    return chromePaths.has(p)
  }

  function walk(node, parentNames, parentNode, ancestors) {
    if (!node) return
    const ctx = { parentNames, parentNode, ancestors }
    if (isChromePath(ctx, node)) return

    const nodePath = [...parentNames, node.name].join(' > ')
    const layout = node.layoutStyle || {}

    const isMaskInstruction = !!node.mask
    const imageUrl = isMaskInstruction ? null : extractImageUrl(node, paintMap)
    if (imageUrl) {
      if (!rasterByUrl.has(imageUrl)) {
        const fileBase = ensureUniqueFileBase(
          buildFileBase(node, ctx, aliases),
          usedFileBases
        )
        rasterByUrl.set(imageUrl, {
          assetId: `raster-${hashKey(imageUrl)}`,
          kind: 'raster',
          fileBase,
          bucket: resolveBucket(layout, options),
          url: imageUrl,
          ext: extFromUrl(imageUrl, '.png'),
          source: 'mastergo-image-url',
          primary: makeRef(node, ctx),
          refs: [],
          bbox: { w: round1(layout.width), h: round1(layout.height) },
        })
      }
      rasterByUrl.get(imageUrl).refs.push(makeRef(node, ctx))
    }

    if (node.type === 'PATH' && (node.path || []).length && !isMaskInstruction) {
      const maxSide = bboxMaxSide(layout)
      const minSide = Math.min(Number(layout.width) || 0, Number(layout.height) || 0)
      const solid = pathFillIsSolid(node, paintMap)
      const decorative = isDecorativePathName(node.name)

      if (decorative) {
        /* 直线/分割线/装饰矩形等，用 CSS 还原 */
      } else if (maxSide > options.skipPathMaxSide && !isMeaningfulName(node.name)) {
        /* 大面积无名 PATH */
      } else if (!solid && (decorative || !isMeaningfulName(node.name))) {
        /* 渐变矩形/直线等装饰，不进入待导出清单 */
      } else {
        const pHash = pathDataHash(node)
        if (!vectorByHash.has(pHash)) {
          const fileBase = ensureUniqueFileBase(
            buildFileBase(node, ctx, aliases),
            usedFileBases
          )
          const entry = {
            assetId: `vector-${pHash}`,
            kind: 'vector',
            fileBase,
            bucket: resolveBucket(layout, options),
            pathHash: pHash,
            primary: makeRef(node, ctx),
            refs: [],
            bbox: { w: round1(layout.width), h: round1(layout.height) },
          }
          if (solid) {
            entry.source = 'dsl-path-svg'
            entry.ext = '.svg'
            entry.svg = pathDataToSvg(node, solid)
          } else {
            entry.source = 'dsl-path-complex'
            entry.ext = '.png'
            entry.status = 'needs_mastergo_export'
            entry.note =
              'PATH 含渐变/多填充，需在 MasterGo 按 nodeId 导出 PNG/SVG，勿手写近似图标'
          }
          vectorByHash.set(pHash, entry)
        }
        vectorByHash.get(pHash).refs.push(makeRef(node, ctx))
      }
    }

    const children = node.children || []
    const nextParents = [...parentNames, node.name || '']
    const nextAncestors = [...(ancestors || []), node]
    for (const child of children) walk(child, nextParents, node, nextAncestors)
  }

  for (const node of nodes) walk(node, [], null, [])
  return [...rasterByUrl.values(), ...vectorByHash.values()]
}

async function materializeAssets(items, assetsDir) {
  const manifest = []

  for (const item of items) {
    const copy = {
      assetId: item.assetId,
      kind: item.kind,
      fileBase: item.fileBase,
      bucket: item.bucket,
      source: item.source,
      ext: item.ext,
      url: item.url || null,
      pathHash: item.pathHash || null,
      nodeId: item.primary?.nodeId,
      nodeName: item.primary?.nodeName,
      nodeType: item.primary?.nodeType,
      layoutMark: item.primary?.layoutMark,
      path: item.primary?.path,
      refs: item.refs,
      bbox: item.bbox,
      localPath: null,
      contentSha256: null,
      status: item.status || 'pending',
      note: item.note || null,
    }
    delete copy.svg

    const targetDir = path.join(assetsDir, item.bucket)

    try {
      if (item.source === 'mastergo-image-url' && item.url) {
        const dest = path.join(targetDir, `${item.fileBase}${item.ext}`)
        await downloadUrl(item.url, dest)
        copy.localPath = dest.replace(/\\/g, '/')
        copy.contentSha256 = crypto.createHash('sha256').update(fs.readFileSync(dest)).digest('hex')
        copy.status = 'downloaded'
      } else if (item.source === 'dsl-path-svg' && item.svg) {
        const dest = path.join(targetDir, `${item.fileBase}${item.ext}`)
        fs.mkdirSync(path.dirname(dest), { recursive: true })
        fs.writeFileSync(dest, item.svg, 'utf8')
        copy.localPath = dest.replace(/\\/g, '/')
        copy.contentSha256 = crypto.createHash('sha256').update(fs.readFileSync(dest)).digest('hex')
        copy.status = 'exported-svg'
      } else if (item.status === 'needs_mastergo_export') {
        copy.status = 'needs_mastergo_export'
      }
    } catch (err) {
      copy.status = 'failed'
      copy.error = String(err.message || err)
    }

    manifest.push(copy)
  }
  return manifest
}

async function extractDslAssets(inputPath, options = {}) {
  const { dsl } = loadDslObject(inputPath)
  const paintMap = buildStyleMaps(dsl.styles || {}).paintMap
  const nodes = dsl.nodes || []
  const pageDir =
    options.pageDir ||
    path.basename(path.dirname(path.resolve(inputPath)))

  const assetsDir = path.resolve(
    options.assetsDir ||
      resolveAssetsDirFromPageDir(pageDir, options.workspaceRoot)
  )

  const aliasInfo = loadAssetAliases(inputPath)
  const chromePaths = loadStrippedChromePaths(inputPath)

  const items = collectAssetCandidates(nodes, paintMap, {
    ...options,
    aliases: { ...aliasInfo.map, ...(options.aliases || {}) },
    chromePaths,
  })

  const manifest = await materializeAssets(items, assetsDir)

  const manifestPath = options.manifestPath
    ? path.resolve(options.manifestPath)
    : path.join(path.dirname(path.resolve(inputPath)), 'dsl-assets.json')

  const ok = manifest.filter(
    (m) => m.status === 'downloaded' || m.status === 'exported-svg'
  )

  const payload = {
    version: '2.0',
    generic: true,
    pageDir,
    assetsDir: assetsDir.replace(/\\/g, '/'),
    buckets: {
      icons: path.join(assetsDir, 'icons').replace(/\\/g, '/'),
      images: path.join(assetsDir, 'images').replace(/\\/g, '/'),
    },
    aliasFile: aliasInfo.path,
    count: manifest.length,
    materialized: ok.length,
    needsMasterGoExport: manifest.filter((m) => m.status === 'needs_mastergo_export'),
    items: manifest,
  }

  fs.mkdirSync(path.dirname(manifestPath), { recursive: true })
  fs.writeFileSync(manifestPath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8')

  return { assetsDir, manifestPath, manifest: payload }
}

module.exports = {
  DEFAULT_OPTIONS,
  resolveAssetsDirFromPageDir,
  collectAssetCandidates,
  materializeAssets,
  extractDslAssets,
  downloadUrl,
  slugify,
  buildFileBase,
  isMeaningfulName,
  pathDataHash,
  pathDataToSvg,
  pathSubpathBBoxes,
  normalizeFillRule,
  inferCompoundFillRule,
}
