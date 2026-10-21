#!/usr/bin/env node
/* eslint-disable no-console */

/**
 * Assemble getDesignSections fallback responses into a filter-dsl-compatible DSL.
 *
 * Expected files:
 *   --overview     raw getDesignSections response without sectionIndex
 *   --sections-dir directory containing raw section-<N>.json responses
 *   --svgs         optional raw getDesignSvgs response
 *   --texts        optional raw getDesignTexts response
 *   --out          dsl_full.json
 */

const fs = require('fs')
const path = require('path')

function parseArgs(argv) {
  const args = {}
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i]
    if (!token.startsWith('--')) continue
    const key = token.slice(2)
    const next = argv[i + 1]
    if (next && !next.startsWith('--')) {
      args[key] = next
      i += 1
    } else {
      args[key] = true
    }
  }
  return args
}

function tryParseJsonString(value) {
  if (typeof value !== 'string') return value
  const text = value.trim()
  if (!text) return value
  if (!/^[\[{]/.test(text)) return value
  try {
    return JSON.parse(text)
  } catch {
    return value
  }
}

function unwrapMcpPayload(value, seen = new Set()) {
  let current = tryParseJsonString(value)
  if (!current || typeof current !== 'object') return current
  if (seen.has(current)) return current
  seen.add(current)

  if (Array.isArray(current)) {
    for (const item of current) {
      const unwrapped = unwrapMcpPayload(item, seen)
      if (unwrapped && typeof unwrapped === 'object' && !Array.isArray(unwrapped)) return unwrapped
    }
    return current
  }

  if (current.type === 'text' && typeof current.text === 'string') {
    return unwrapMcpPayload(current.text, seen)
  }

  if (Array.isArray(current.content)) return unwrapMcpPayload(current.content, seen)

  for (const key of ['data', 'result', 'response']) {
    if (current[key] !== undefined) {
      const unwrapped = unwrapMcpPayload(current[key], seen)
      if (unwrapped && typeof unwrapped === 'object') return unwrapped
    }
  }

  return current
}

function readJsonFile(filePath, { optional = false } = {}) {
  if (!filePath) return null
  if (!fs.existsSync(filePath)) {
    if (optional) return null
    throw new Error(`文件不存在: ${filePath}`)
  }
  const raw = fs.readFileSync(filePath, 'utf8')
  return unwrapMcpPayload(JSON.parse(raw))
}

function countNodes(nodes) {
  if (!Array.isArray(nodes)) return 0
  let total = 0
  for (const node of nodes) {
    if (!node || typeof node !== 'object') continue
    total += 1
    total += countNodes(node.children)
  }
  return total
}

function deepClone(value) {
  return JSON.parse(JSON.stringify(value))
}

function offsetTopLevelNode(node, dx, dy) {
  const copy = deepClone(node)
  if (copy.layoutStyle && typeof copy.layoutStyle === 'object') {
    copy.layoutStyle.relativeX = Number(copy.layoutStyle.relativeX || 0) + dx
    copy.layoutStyle.relativeY = Number(copy.layoutStyle.relativeY || 0) + dy
  }
  return copy
}

function extractTexts(rawTexts) {
  if (!rawTexts || typeof rawTexts !== 'object') return {}
  if (rawTexts.texts && typeof rawTexts.texts === 'object') return rawTexts.texts
  return rawTexts
}

function hydrateTextRefs(node, textMap) {
  if (!node || typeof node !== 'object') return
  if (node.type === 'TEXT') {
    if (typeof node.text === 'string' && textMap[node.text]) {
      node.text = [{ text: String(textMap[node.text]) }]
    } else if (Array.isArray(node.text)) {
      node.text = node.text.map((span) => {
        if (span && typeof span === 'object' && typeof span.text === 'string' && textMap[span.text]) {
          return { ...span, text: String(textMap[span.text]) }
        }
        return span
      })
    }
  }
  for (const child of node.children || []) hydrateTextRefs(child, textMap)
}

function sectionFiles(sectionsDir) {
  return fs.readdirSync(sectionsDir)
    .filter((file) => /\.json$/i.test(file))
    .filter((file) => !/^(overview|svgs|texts)\.json$/i.test(file))
    .map((file) => path.join(sectionsDir, file))
    .sort((a, b) => a.localeCompare(b, 'en', { numeric: true }))
}

function mergeComponents(target, components) {
  if (!components) return
  if (Array.isArray(components)) {
    target.push(...components)
    return
  }
  if (typeof components === 'object') {
    target.push(...Object.values(components))
  }
}

function fail(message, details = {}) {
  console.error(`❌ ${message}`)
  if (Object.keys(details).length) console.error(JSON.stringify(details, null, 2))
  process.exit(1)
}

function main() {
  const args = parseArgs(process.argv)
  const overviewPath = args.overview && args.overview !== true ? path.resolve(args.overview) : ''
  const sectionsDir = args['sections-dir'] && args['sections-dir'] !== true ? path.resolve(args['sections-dir']) : ''
  const outPath = args.out && args.out !== true ? path.resolve(args.out) : ''
  const svgsPath = args.svgs && args.svgs !== true ? path.resolve(args.svgs) : ''
  const textsPath = args.texts && args.texts !== true ? path.resolve(args.texts) : ''

  if (!overviewPath || !sectionsDir || !outPath) {
    fail('Usage: node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/assemble-mastergo-sections.cjs --overview <overview.json> --sections-dir <dir> --out <dsl_full.json> [--svgs svgs.json] [--texts texts.json]')
  }
  if (!fs.existsSync(sectionsDir) || !fs.statSync(sectionsDir).isDirectory()) {
    fail(`sections-dir 不存在或不是目录: ${sectionsDir}`)
  }

  let overview
  try {
    overview = readJsonFile(overviewPath)
  } catch (error) {
    fail(`读取 overview 失败: ${error.message}`)
  }
  const overviewSections = Array.isArray(overview?.sections) ? overview.sections : []
  if (!overviewSections.length) fail('overview 中没有 sections，无法组装 section DSL。', { overviewPath })

  const textMap = extractTexts(readJsonFile(textsPath, { optional: true }))
  const svgs = readJsonFile(svgsPath, { optional: true })
  const styles = {}
  const nodes = []
  const components = []
  const includedSections = []

  for (const filePath of sectionFiles(sectionsDir)) {
    let sectionPayload
    try {
      sectionPayload = readJsonFile(filePath)
    } catch (error) {
      fail(`读取 section 文件失败: ${error.message}`, { filePath })
    }
    const sectionIndex = Number(sectionPayload?.sectionIndex)
    const dsl = sectionPayload?.dsl
    if (!Number.isInteger(sectionIndex) || !dsl || !Array.isArray(dsl.nodes)) {
      fail('section 文件不是 getDesignSections(sectionIndex) 的 DSL 响应。', {
        filePath,
        keys: sectionPayload && typeof sectionPayload === 'object' ? Object.keys(sectionPayload) : [],
      })
    }

    const bbox = overviewSections[sectionIndex] || {}
    const dx = Number(bbox.x || 0)
    const dy = Number(bbox.y || 0)
    Object.assign(styles, dsl.styles || {})
    mergeComponents(components, dsl.components)

    for (const node of dsl.nodes) {
      const moved = offsetTopLevelNode(node, dx, dy)
      hydrateTextRefs(moved, textMap)
      nodes.push(moved)
    }
    includedSections.push({
      sectionIndex,
      id: sectionPayload.section?.id || bbox.id || '',
      name: sectionPayload.section?.name || bbox.name || '',
      nodeCount: countNodes(dsl.nodes),
      x: dx,
      y: dy,
    })
  }

  const nodeCount = countNodes(nodes)
  if (!nodeCount) {
    fail('section fallback 组装结果没有任何节点，禁止写入 dsl_full.json。', {
      overviewPath,
      sectionsDir,
      sectionFileCount: sectionFiles(sectionsDir).length,
    })
  }

  const result = {
    fileId: overview.fileId,
    layerId: overview.layerId,
    shortLink: overview.shortLink,
    rootMetadata: overview.rootMetadata,
    rootContainer: overview.rootContainer,
    sections: overviewSections,
    styles,
    nodes,
    components,
    _sectionFallback: {
      assembledAt: new Date().toISOString(),
      overviewPath,
      sectionsDir,
      sectionCount: includedSections.length,
      nodeCount,
      includedSections,
      svgs: svgs?.svgs || undefined,
      textKeyCount: Object.keys(textMap).length,
    },
  }

  fs.mkdirSync(path.dirname(outPath), { recursive: true })
  fs.writeFileSync(outPath, `${JSON.stringify(result, null, 2)}\n`, 'utf8')
  console.log(JSON.stringify({ ok: true, outPath, sectionCount: includedSections.length, nodeCount }, null, 2))
}

main()
