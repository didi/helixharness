#!/usr/bin/env node
/**
 * 不经过 Claude / MCP 对话，直接用 MasterGo 开放 HTTP 接口拉取 DSL 并落盘。
 * 与 @mastergo/magic-mcp 使用的接口一致：GET {API_BASE_URL}/mcp/dsl
 *
 * 令牌（按优先级，命中即停）：
 * 1. --token <令牌>
 * 2. 环境变量 MG_MCP_TOKEN / MASTERGO_API_TOKEN（及 API_BASE_URL，可选）
 * 3. 本机编辑器 MCP 配置（默认开启）：Cursor / Claude Desktop / Windsurf 等，见 lib/resolve-mastergo-credentials.cjs
 *    - 显式：--from-mcp（仅扫描 MCP，不再尝试其它来源）
 *    - 指定文件：--mcp-config ~/.cursor/mcp.json
 *    - 覆盖路径列表：MASTERGO_MCP_CONFIG=/path/to/mcp.json
 * 4. --env-file <路径>（显式指定 KEY=VAL 文件，不自动读仓库内任何 .env）
 * 5. --no-mcp：禁用第 3 步，仅用 1/2/4
 *
 * Agent 勿 Read ~/.cursor/mcp.json 等进对话；由本脚本在子进程内解析，stdout 不打印 token。
 *
 * 前置：MasterGo 个人访问令牌 + 团队版且文件在团队项目。
 *
 * 用法示例：
 *   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/fetch-mastergo-dsl.cjs \
 *     --mg-url 'https://mastergo.com/goto/xxx?...&layer_id=...&file=...' \
 *     --out d-d2c/temp/<page-dir>/dsl_full.json
 *
 *   node .../fetch-mastergo-dsl.cjs --file 188838996770122 --layer '24:324938' --out d-d2c/temp/<page-dir>/dsl_full.json
 *
 * 拉取失败回退：HTTP/超时等导致未写出 dsl_full.json 时，退出码 2，并在 --out 同目录写入
 * `.dsl-fetch-fallback.json`，stdout 输出 JSON 指引 Agent 调用 mastergo-magic-mcp 的 mcp__getDsl，
 * 将结果落盘到 outPath 后执行 filter-dsl.cjs（勿在对话中编造 DSL）。
 *
 * 参考：mastergo-design/mastergo-magic-mcp `src/utils/api.ts`
 */

const EXIT_OK = 0
const EXIT_ERR = 1
const EXIT_FALLBACK_LLM = 2

/* eslint-disable no-console */
const fs = require('fs')
const path = require('path')
const https = require('https')
// 结构化运行日志（非侵入、可降级）
let getLogger = () => ({ enabled: false })
try { ({ getLogger } = require('./lib/d2c-run-log.cjs')) } catch { /* 静默降级 */ }
const http = require('http')
const { URL } = require('url')
const { resolveFromEditorMcp } = require('./lib/resolve-mastergo-credentials.cjs')
const {
  resolveTransponderUrlByBusinessLine,
} = require('./lib/load-transponder-routes.cjs')

/** 轻量 KEY=VAL，不依赖 dotenv；仅当 process.env[key] 未设置时写入 */
function loadEnvFile(filePath) {
  const abs = path.resolve(filePath)
  if (!fs.existsSync(abs)) return
  const text = fs.readFileSync(abs, 'utf8')
  for (const line of text.split('\n')) {
    const t = line.trim()
    if (!t || t.startsWith('#')) continue
    const eq = t.indexOf('=')
    if (eq === -1) continue
    const k = t.slice(0, eq).trim()
    let v = t.slice(eq + 1).trim()
    if (
      (v.startsWith('"') && v.endsWith('"')) ||
      (v.startsWith("'") && v.endsWith("'"))
    ) {
      v = v.slice(1, -1)
    }
    if (k && process.env[k] === undefined) process.env[k] = v
  }
}

function parseArgs(argv) {
  const args = {}
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i]
    if (token.startsWith('--')) {
      const key = token.slice(2)
      const val =
        argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : true
      args[key] = val
      if (val !== true) i += 1
    }
  }
  return args
}

function normalizeFileId(fileId) {
  if (!fileId) return fileId
  return String(fileId).replace(/^file\//, '')
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function toPositiveInt(value, fallback) {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return fallback
  return Math.floor(n)
}

function transientStatus(statusCode) {
  return statusCode === 408 || statusCode === 409 || statusCode === 425 || statusCode === 429 || statusCode >= 500
}

function isRetryableFetchError(error) {
  const message = String(error?.message || error || '')
  if (error?.statusCode && transientStatus(Number(error.statusCode))) return true
  if (/\b(ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND)\b/i.test(message)) return true
  if (/request timeout|服务器繁忙|稍后重试|HTTP\s+(?:429|5\d\d)|"code"\s*:\s*"?(?:10015|429)"?/i.test(message)) return true
  return false
}

/**
 * 解析设计页 URL，得到 fileId / layerId（与 magic-mcp extractIdsFromUrl 对齐思路）
 */
function extractIdsFromResolvedUrl(targetUrl) {
  const urlObj = new URL(targetUrl)
  const pathSegments = urlObj.pathname.split('/').filter(Boolean)
  const searchParams = urlObj.searchParams

  let fileId = pathSegments.find((segment) => /^\d+$/.test(segment))
  if (!fileId) {
    const f = searchParams.get('file')
    if (f && /^\d+$/.test(f)) fileId = f
  }

  const layerId = searchParams.get('layer_id')
  const sourceLayerId = searchParams.get('source_layer_id') || undefined

  if (!fileId) throw new Error(`无法从 URL 解析 fileId: ${targetUrl}`)
  if (!layerId) throw new Error(`无法从 URL 解析 layer_id: ${targetUrl}`)

  return {
    fileId: normalizeFileId(fileId),
    layerId,
    sourceLayerId,
  }
}

function httpGet(urlStr, opts = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(urlStr)
    const lib = u.protocol === 'https:' ? https : http
    const agent =
      u.protocol === 'https:' && process.env.MG_TLS_INSECURE === '1'
        ? new https.Agent({ rejectUnauthorized: false })
        : undefined

    const req = lib.request(
      urlStr,
      {
        method: opts.method || 'GET',
        agent,
        headers: {
          'User-Agent': 'fetch-mastergo-dsl/1.0',
          ...(opts.headers || {}),
        },
        timeout: opts.timeout || 60000,
      },
      (res) => {
        const chunks = []
        res.on('data', (c) => chunks.push(c))
        res.on('end', () => {
          const buf = Buffer.concat(chunks)
          resolve({
            statusCode: res.statusCode,
            headers: res.headers,
            body: buf,
          })
        })
      }
    )
    req.on('error', reject)
    req.on('timeout', () => {
      req.destroy()
      reject(new Error('request timeout'))
    })
    if (opts.body) req.write(opts.body)
    req.end()
  })
}

async function httpPostJson(urlStr, payload) {
  const body = JSON.stringify(payload)
  return httpGet(urlStr, {
    method: 'POST',
    body,
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
      'Content-Length': Buffer.byteLength(body),
    },
  })
}

async function resolveMasterGoUrl(startUrl, maxRedirects = 8) {
  let current = startUrl
  for (let i = 0; i < maxRedirects; i += 1) {
    const { statusCode, headers, body } = await httpGet(current, {
      method: 'GET',
    })
    if (statusCode >= 300 && statusCode < 400 && headers.location) {
      current = new URL(headers.location, current).href
      continue
    }
    if (statusCode === 200) {
      const ct = (headers['content-type'] || '').toLowerCase()
      if (ct.includes('text/html')) {
        const html = body.toString('utf8')
        const candidates = [
          ...html.matchAll(/https:\/\/mastergo\.com\/file\/\d+[^\s"'<>]*/gi),
          ...html.matchAll(/https:\/\/mastergo\.com\/goto\/[^\s"'<>]*/gi),
        ]
          .map((x) => x[0])
          .filter(Boolean)
        const best =
          candidates.find((u) => /[?&]layer_id=/.test(u)) || candidates[0]
        if (best) {
          current = best
          continue
        }
      }
      return current
    }
    throw new Error(`解析跳转失败 HTTP ${statusCode} ${current}`)
  }
  throw new Error('跳转次数过多')
}

async function fetchDsl({ baseUrl, token, fileId, layerId, sourceLayerId }) {
  const u = new URL('/mcp/dsl', baseUrl.endsWith('/') ? baseUrl : `${baseUrl}/`)
  u.searchParams.set('fileId', fileId)
  u.searchParams.set('layerId', layerId)
  if (sourceLayerId) u.searchParams.set('sourceLayerId', sourceLayerId)

  const { statusCode, body } = await httpGet(u.href, {
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
      'X-MG-UserAccessToken': token,
    },
  })

  const text = body.toString('utf8')
  if (statusCode !== 200) {
    const err = new Error(`GET /mcp/dsl 失败 HTTP ${statusCode}: ${text.slice(0, 500)}`)
    err.statusCode = statusCode
    err.body = text
    throw err
  }
  return text
}

async function fetchDslWithRetry(params, opts = {}) {
  const attempts = toPositiveInt(opts.retries, 4)
  const baseDelayMs = toPositiveInt(opts.baseDelayMs, 1200)
  const maxDelayMs = toPositiveInt(opts.maxDelayMs, 10000)
  let lastError = null

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      if (attempt > 1) {
        console.error(`MasterGo DSL 拉取重试 ${attempt}/${attempts}...`)
      }
      return await fetchDsl(params)
    } catch (error) {
      lastError = error
      if (attempt >= attempts || !isRetryableFetchError(error)) {
        throw error
      }
      const jitter = Math.floor(Math.random() * Math.min(500, baseDelayMs))
      const delay = Math.min(maxDelayMs, baseDelayMs * 2 ** (attempt - 1)) + jitter
      console.error(`MasterGo DSL 拉取失败（${error.message || error}），${delay}ms 后重试。`)
      await sleep(delay)
    }
  }

  throw lastError || new Error('MasterGo DSL 拉取失败')
}

function extractShortLink(mgUrl) {
  if (!mgUrl || typeof mgUrl !== 'string') return ''
  try {
    const u = new URL(mgUrl)
    return /^\/(?:goto|file)\//i.test(u.pathname) ? u.href : ''
  } catch {
    return ''
  }
}

function resolveTransponder(args) {
  if (args['no-transponder'] === true) return null
  const businessLine =
    args['business-line'] && args['business-line'] !== true
      ? String(args['business-line']).trim()
      : ''
  if (!businessLine) return null

  const configured = resolveTransponderUrlByBusinessLine(businessLine)
  const url = (configured?.url || '').replace(/\/+$/, '')
  if (!url) {
    throw new Error(
      '未配置设计资源转发地址：请在 config/transponder-routes.json 设置 transponderUrl，或设置环境变量 HELIXHARNESS_FRONTEND_ASSET_URL；不需要转发时请去掉 --business-line'
    )
  }
  return {
    businessLine: configured?.businessLine || businessLine,
    url,
  }
}

function parseJsonResponse(body, label) {
  const text = body.toString('utf8')
  try {
    return JSON.parse(text)
  } catch {
    throw new Error(`${label} 返回了非 JSON 响应: ${text.slice(0, 500)}`)
  }
}

function extractTransponderDsl(payload) {
  let value = payload?.result
  if (value && typeof value === 'object' && 'content' in value) value = value.content
  if (Array.isArray(value)) {
    const textItem = value.find((item) => item && typeof item.text === 'string')
    if (textItem) value = textItem.text
  }
  if (value && typeof value === 'object' && typeof value.dsl === 'string') value = value.dsl
  if (typeof value === 'string') return value.trim()
  if (value && typeof value === 'object') return JSON.stringify(value)
  return ''
}

function assertDslResponse(raw, label) {
  const text = String(raw || '').trim()
  if (!text) throw new Error(`${label} 返回空 DSL`)
  try {
    const parsed = JSON.parse(text)
    if (
      parsed &&
      typeof parsed === 'object' &&
      Object.prototype.hasOwnProperty.call(parsed, 'code') &&
      Object.prototype.hasOwnProperty.call(parsed, 'message')
    ) {
      throw new Error(`${label} 返回 MasterGo 错误: ${parsed.code} ${parsed.message}`)
    }
  } catch (error) {
    if (/MasterGo 错误/.test(error.message)) throw error
  }
  return text
}

async function fetchDslViaTransponder({ transponder, mgUrl, fileId, layerId, sourceLayerId }) {
  const sessionResponse = await httpPostJson(`${transponder.url}/session`, {
    businessLine: transponder.businessLine,
  })
  if (sessionResponse.statusCode !== 200) {
    throw new Error(`POST /session 失败 HTTP ${sessionResponse.statusCode}: ${sessionResponse.body.toString('utf8').slice(0, 500)}`)
  }
  const session = parseJsonResponse(sessionResponse.body, 'POST /session')
  if (!session.sessionId) throw new Error('POST /session 响应缺少 sessionId')

  const toolArgs = {}
  if (fileId && layerId) {
    toolArgs.fileId = String(fileId)
    toolArgs.layerId = String(layerId)
    if (sourceLayerId) toolArgs.sourceLayerId = String(sourceLayerId)
  } else {
    const shortLink = extractShortLink(mgUrl)
    if (!shortLink) throw new Error('中转拉取缺少 fileId/layerId 或可用的 MasterGo shortLink')
    toolArgs.shortLink = shortLink
  }

  const callResponse = await httpPostJson(`${transponder.url}/call`, {
    sessionId: session.sessionId,
    tool: 'mcp__getDsl',
    args: toolArgs,
  })
  if (callResponse.statusCode !== 200) {
    throw new Error(`POST /call 失败 HTTP ${callResponse.statusCode}: ${callResponse.body.toString('utf8').slice(0, 500)}`)
  }
  const call = parseJsonResponse(callResponse.body, 'POST /call')
  return assertDslResponse(extractTransponderDsl(call), 'POST /call')
}

/**
 * 脚本拉取失败时，生成供 Agent / 大模型走 MCP 的回退说明（不打印 token）
 */
function buildLlmFallbackPayload({
  mgUrl,
  fileId,
  layerId,
  sourceLayerId,
  outPath,
  error,
}) {
  const mcpArgs = {}
  const shortLink = extractShortLink(mgUrl)
  if (shortLink) mcpArgs.shortLink = shortLink
  if (fileId) mcpArgs.fileId = String(fileId)
  if (layerId) mcpArgs.layerId = String(layerId)
  if (sourceLayerId) mcpArgs.sourceLayerId = String(sourceLayerId)

  const outDir = path.dirname(outPath)
  const filterOutputDir = outDir
  const rawMcpPath = path.join(outDir, 'mcp-getDsl-response.json')
  const sectionsDir = path.join(outDir, 'mcp-sections')
  const overviewPath = path.join(sectionsDir, 'overview.json')
  const svgsPath = path.join(sectionsDir, 'svgs.json')
  const textsPath = path.join(sectionsDir, 'texts.json')

  return {
    ok: false,
    fallback: 'llm-mcp',
    error: String(error || 'unknown'),
    mgUrl: mgUrl || undefined,
    fileId: fileId || undefined,
    layerId: layerId || undefined,
    outPath,
    mcp: {
      server: 'user-mastergo-magic-mcp',
      tool: 'mcp__getDsl',
      arguments: mcpArgs,
    },
    rawMcpPath,
    sectionsDir,
    nextSteps: [
      `最多重试 3 次调用 MCP ${'user-mastergo-magic-mcp'}/${'mcp__getDsl'}，参数见 mcp.arguments；每次失败若为 10015/繁忙/超时，等待后重试。`,
      `将成功返回的 MCP 原始 JSON 原封不动写入 ${rawMcpPath}，禁止手写/摘抄/伪造 dsl_full.json。`,
      `执行 node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/normalize-mastergo-dsl.cjs --input ${rawMcpPath} --out ${outPath}`,
      `若 getDsl 仍不可用但 getDesignSections 可用：调用 getDesignSections 概览并写入 ${overviewPath}；按 sectionIndex 写入 ${sectionsDir}/section-<N>.json；调用 getDesignSvgs 写入 ${svgsPath}、getDesignTexts 写入 ${textsPath}（若工具可用）。`,
      `执行 node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/assemble-mastergo-sections.cjs --overview ${overviewPath} --sections-dir ${sectionsDir} --svgs ${svgsPath} --texts ${textsPath} --out ${outPath}`,
      `node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/filter-dsl.cjs --input ${outPath} --output ${filterOutputDir}/`,
    ],
  }
}

function writeFallbackManifest(payload, outPath) {
  const manifestPath = path.join(path.dirname(outPath), '.dsl-fetch-fallback.json')
  fs.mkdirSync(path.dirname(manifestPath), { recursive: true })
  fs.writeFileSync(manifestPath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8')
  return manifestPath
}

function exitWithLlmFallback(payload) {
  const outPath = payload.outPath
  if (!outPath || typeof outPath !== 'string') {
    console.error('exitWithLlmFallback: 缺少 outPath')
    process.exit(EXIT_ERR)
  }
  const full = buildLlmFallbackPayload(payload)
  const manifestPath = writeFallbackManifest(full, outPath)
  console.log(JSON.stringify({ ...full, manifestPath }, null, 2))
  console.error(
    `\n脚本拉取 DSL 失败，已写入回退说明: ${manifestPath}\n` +
      '请由 Agent 调用 mastergo-magic-mcp 的 mcp__getDsl 拉取并落盘，再执行 filter-dsl.cjs。\n'
  )
  process.exit(EXIT_FALLBACK_LLM)
}

function collectMcpConfigPaths(args) {
  const paths = []
  const raw = args['mcp-config']
  if (raw === true) return paths
  if (Array.isArray(raw)) {
    for (const p of raw) paths.push(String(p))
  } else if (raw) {
    paths.push(String(raw))
  }
  return paths
}

function resolveCredentials(args) {
  const cliToken = args.token && args.token !== true ? String(args.token) : ''
  const cliBase =
    args['api-base'] && args['api-base'] !== true ? String(args['api-base']) : ''

  if (cliToken) {
    return {
      token: cliToken,
      baseUrl: (cliBase || process.env.API_BASE_URL || 'https://mastergo.com').replace(
        /\/$/,
        ''
      ),
      source: 'cli:--token',
    }
  }

  const envToken =
    process.env.MG_MCP_TOKEN || process.env.MASTERGO_API_TOKEN || ''
  if (envToken) {
    return {
      token: envToken,
      baseUrl: (
        cliBase ||
        process.env.API_BASE_URL ||
        'https://mastergo.com'
      ).replace(/\/$/, ''),
      source: 'env',
    }
  }

  const skipMcp = args['no-mcp'] === true
  const fromMcpOnly = args['from-mcp'] === true
  if (!skipMcp) {
    const explicitPaths = collectMcpConfigPaths(args)
    const mcpHit = resolveFromEditorMcp({
      explicitPaths: explicitPaths.length ? explicitPaths : undefined,
    })
    if (mcpHit?.token) {
      return {
        token: mcpHit.token,
        baseUrl: (cliBase || mcpHit.baseUrl || 'https://mastergo.com').replace(/\/$/, ''),
        source: `mcp:${mcpHit.source}${mcpHit.server ? `#${mcpHit.server}` : ''}`,
      }
    }
    if (fromMcpOnly) {
      return { token: '', baseUrl: 'https://mastergo.com', source: 'mcp:miss' }
    }
  }

  if (!fromMcpOnly && args['env-file'] && args['env-file'] !== true) {
    loadEnvFile(String(args['env-file']))
    const afterEnv =
      process.env.MG_MCP_TOKEN || process.env.MASTERGO_API_TOKEN || ''
    if (afterEnv) {
      return {
        token: afterEnv,
        baseUrl: (
          cliBase ||
          process.env.API_BASE_URL ||
          'https://mastergo.com'
        ).replace(/\/$/, ''),
        source: `env-file:${path.resolve(String(args['env-file']))}`,
      }
    }
  }

  return { token: '', baseUrl: 'https://mastergo.com', source: '' }
}

async function main() {
  const args = parseArgs(process.argv)

  let transponder
  try {
    transponder = resolveTransponder(args)
  } catch (error) {
    console.error(error.message || error)
    process.exit(EXIT_ERR)
  }

  const outPath =
    (args.out && args.out !== true && path.resolve(args.out)) ||
    path.resolve('d-d2c/temp/dsl/mastergo-dsl.json')

  const mgUrl = args['mg-url'] && args['mg-url'] !== true ? String(args['mg-url']) : ''

  let fileId = args.file && args.file !== true ? String(args.file) : ''
  let layerId = args.layer && args.layer !== true ? String(args.layer) : ''
  let sourceLayerId =
    args['source-layer'] && args['source-layer'] !== true
      ? String(args['source-layer'])
      : undefined

  const canTryLlmFallback = () =>
    Boolean(mgUrl || (fileId && layerId))

  const doLlmFallback = (error) => {
    if (!canTryLlmFallback()) {
      console.error(error.message || error)
      process.exit(EXIT_ERR)
    }
    exitWithLlmFallback({
      mgUrl,
      fileId: fileId ? normalizeFileId(fileId) : '',
      layerId,
      sourceLayerId,
      outPath,
      error: error.message || error,
    })
  }

  if (mgUrl) {
    try {
      let resolved = mgUrl
      if (mgUrl.includes('/goto/')) {
        resolved = await resolveMasterGoUrl(mgUrl)
      }
      const ids = extractIdsFromResolvedUrl(resolved)
      fileId = ids.fileId
      layerId = ids.layerId
      sourceLayerId = ids.sourceLayerId || sourceLayerId
    } catch (e) {
      if (!transponder) doLlmFallback(e)
      console.error(`MasterGo URL 本地解析失败，将交由中转 shortLink 解析: ${e.message || e}`)
    }
  }

  if ((!fileId || !layerId) && !transponder) {
    console.error(
      '用法: --mg-url <MasterGo 设计链接> [--out 路径]\n' +
        '  或: --file <fileId> --layer <layer_id> [--source-layer id] [--out 路径]\n' +
        '  令牌: --token；环境变量 MG_MCP_TOKEN；默认扫描 MCP；--mcp-config；--env-file <路径>；--no-mcp\n' +
        '  环境: MG_MCP_TOKEN, API_BASE_URL, MASTERGO_MCP_CONFIG\n' +
        '  拉取失败且已解析 fileId/layerId 或提供 --mg-url 时，退出码 2 并生成 MCP 回退说明。'
    )
    process.exit(EXIT_ERR)
  }

  fileId = normalizeFileId(fileId)

  const { token, baseUrl, source } = resolveCredentials(args)
  let credSource = source

  if (!token) {
    if (transponder) {
      try {
        const raw = await fetchDslViaTransponder({ transponder, mgUrl, fileId, layerId, sourceLayerId })
        return writeSuccess({ raw, outPath, fileId, layerId, credSource: `transponder:${transponder.url}#${transponder.businessLine}` })
      } catch (error) {
        doLlmFallback(error)
      }
    }
    if (canTryLlmFallback()) {
      exitWithLlmFallback({
        mgUrl,
        fileId,
        layerId,
        sourceLayerId,
        outPath,
        error:
          '缺少 MasterGo 令牌（脚本 HTTP 拉取不可用）；改由编辑器 MCP mastergo-magic-mcp 拉取 DSL',
      })
    }
    console.error(
      '缺少 MasterGo 令牌。任选其一：\n' +
        '  - 环境变量 MG_MCP_TOKEN / MASTERGO_API_TOKEN\n' +
        '  - --token <令牌>\n' +
        '  - 本机 MCP：在编辑器中配置 mastergo-magic-mcp（脚本默认自动读取）\n' +
        '  - --mcp-config <path/to/mcp.json> 指定 MCP 配置\n' +
        '  - --env-file <path/to/keys.env>（显式路径，需含 MG_MCP_TOKEN）\n' +
        '  - --no-mcp：跳过 MCP 扫描，仅用环境变量 / --token / --env-file'
    )
    process.exit(EXIT_ERR)
  }

  let raw
  try {
    raw = await fetchDslWithRetry({
      baseUrl,
      token,
      fileId,
      layerId,
      sourceLayerId,
    }, {
      retries: args.retries && args.retries !== true ? args.retries : process.env.MG_DSL_FETCH_RETRIES,
      baseDelayMs: args['retry-base-ms'] && args['retry-base-ms'] !== true ? args['retry-base-ms'] : process.env.MG_DSL_RETRY_BASE_MS,
      maxDelayMs: args['retry-max-ms'] && args['retry-max-ms'] !== true ? args['retry-max-ms'] : process.env.MG_DSL_RETRY_MAX_MS,
    })
  } catch (e) {
    if (!transponder) doLlmFallback(e)
    try {
      raw = await fetchDslViaTransponder({ transponder, mgUrl, fileId, layerId, sourceLayerId })
      credSource = `transponder:${transponder.url}#${transponder.businessLine}`
    } catch (transponderError) {
      doLlmFallback(transponderError)
    }
  }

  raw = assertDslResponse(raw, 'GET /mcp/dsl')
  return writeSuccess({ raw, outPath, fileId, layerId, credSource })
}

function writeSuccess({ raw, outPath, fileId, layerId, credSource }) {

  fs.mkdirSync(path.dirname(outPath), { recursive: true })
  fs.writeFileSync(outPath, raw, 'utf8')

  // 结构化日志：DSL 落盘（page 目录 = outPath 所在目录）
  try {
    const dlog = getLogger(path.dirname(outPath))
    if (dlog.enabled) { dlog.scriptComplete('fetch-mastergo-dsl', { fileId, layerId }); dlog.artifactWrite(outPath) }
  } catch { /* 日志失败不影响主流程 */ }

  const manifestPath = path.join(path.dirname(outPath), '.dsl-fetch-fallback.json')
  if (fs.existsSync(manifestPath)) {
    try {
      fs.unlinkSync(manifestPath)
    } catch {
      /* ignore */
    }
  }

  const size = Buffer.byteLength(raw, 'utf8')
  console.log(
    JSON.stringify(
      {
        ok: true,
        outPath,
        bytes: size,
        fileId,
        layerId,
        credentialsFrom: credSource || undefined,
      },
      null,
      2
    )
  )
}

main().catch((e) => {
  const args = parseArgs(process.argv)
  const outPath =
    (args.out && args.out !== true && path.resolve(args.out)) ||
    path.resolve('d-d2c/temp/dsl/mastergo-dsl.json')
  const mgUrl = args['mg-url'] && args['mg-url'] !== true ? String(args['mg-url']) : ''
  const fileId = args.file && args.file !== true ? String(args.file) : ''
  const layerId = args.layer && args.layer !== true ? String(args.layer) : ''

  if (mgUrl || (fileId && layerId)) {
    exitWithLlmFallback({
      mgUrl,
      fileId: fileId ? normalizeFileId(fileId) : '',
      layerId,
      outPath,
      error: e.message || e,
    })
  }
  console.error(e.message || e)
  process.exit(EXIT_ERR)
})
