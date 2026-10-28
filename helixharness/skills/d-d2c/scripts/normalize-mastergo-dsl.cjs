#!/usr/bin/env node
/* eslint-disable no-console */

/**
 * Normalize a raw mastergo-magic-mcp getDsl response into dsl_full.json.
 *
 * The executor is allowed to write only the raw MCP response to disk. This
 * script unwraps common MCP envelopes, extracts the real DSL, validates that it
 * has semantic nodes, and writes the normalized payload for filter-dsl.cjs.
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
      if (hasDslShape(unwrapped)) return unwrapped
    }
    return current
  }

  if (current.type === 'text' && typeof current.text === 'string') {
    return unwrapMcpPayload(current.text, seen)
  }

  if (Array.isArray(current.content)) {
    const unwrapped = unwrapMcpPayload(current.content, seen)
    if (hasDslShape(unwrapped)) return unwrapped
  }

  for (const key of ['data', 'result', 'response']) {
    if (current[key] !== undefined) {
      const unwrapped = unwrapMcpPayload(current[key], seen)
      if (hasDslShape(unwrapped)) return unwrapped
    }
  }

  return current
}

function hasDslShape(value) {
  if (!value || typeof value !== 'object') return false
  if (Array.isArray(value.nodes)) return true
  if (value.dsl && typeof value.dsl === 'object' && Array.isArray(value.dsl.nodes)) return true
  return false
}

function dslObject(value) {
  if (!value || typeof value !== 'object') return null
  if (value.dsl && typeof value.dsl === 'object') return value.dsl
  return value
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

function fail(message, details = {}) {
  console.error(`❌ ${message}`)
  if (Object.keys(details).length) {
    console.error(JSON.stringify(details, null, 2))
  }
  process.exit(1)
}

function main() {
  const args = parseArgs(process.argv)
  const inputPath = args.input && args.input !== true ? path.resolve(args.input) : ''
  const outPath = args.out && args.out !== true ? path.resolve(args.out) : ''

  if (!inputPath || !outPath) {
    fail('Usage: node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/normalize-mastergo-dsl.cjs --input <raw-mcp-response.json> --out <dsl_full.json>')
  }
  if (!fs.existsSync(inputPath)) fail(`输入文件不存在: ${inputPath}`)

  const rawText = fs.readFileSync(inputPath, 'utf8')
  let rawJson
  try {
    rawJson = JSON.parse(rawText)
  } catch (error) {
    fail(`MCP 原始响应不是合法 JSON: ${error.message}`)
  }

  const normalized = unwrapMcpPayload(rawJson)
  const dsl = dslObject(normalized)
  const nodeCount = countNodes(dsl?.nodes)

  if (!nodeCount) {
    fail('MCP getDsl 响应未包含非空 DSL nodes，禁止写入 dsl_full.json。', {
      inputPath,
      topLevelKeys: normalized && typeof normalized === 'object' && !Array.isArray(normalized) ? Object.keys(normalized) : [],
      hasDsl: Boolean(normalized?.dsl),
      hasSectionsOnly: Boolean(normalized?.sections && !normalized?.nodes && !normalized?.dsl),
    })
  }

  fs.mkdirSync(path.dirname(outPath), { recursive: true })
  fs.writeFileSync(outPath, `${JSON.stringify(normalized, null, 2)}\n`, 'utf8')
  console.log(JSON.stringify({ ok: true, inputPath, outPath, nodeCount }, null, 2))
}

main()
