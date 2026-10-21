/* eslint-disable no-console */
'use strict'

const crypto = require('crypto')
const fs = require('fs')
const path = require('path')
const { execFileSync } = require('child_process')

const IMPLEMENTATION_EXTENSIONS = new Set([
  '.vue',
  '.js',
  '.jsx',
  '.ts',
  '.tsx',
  '.mjs',
  '.cjs',
  '.css',
  '.less',
  '.scss',
  '.sass',
  '.styl',
  '.stylus',
  '.html',
  '.pug',
])

const IMPLEMENTATION_BASENAMES = new Set([
  'package.json',
  'vue.config.js',
  'webpack.config.js',
  'vite.config.js',
  'vite.config.ts',
])

const SIGNAL_PATTERNS = [
  { code: 'failed-to-compile', failureClass: 'compile-error', re: /\bfailed to compile\b/i },
  { code: 'errors-while-compiling', failureClass: 'compile-error', re: /\berrors? while compiling\b/i },
  { code: 'module-build-failed', failureClass: 'compile-error', re: /\bmodule build failed\b/i },
  { code: 'module-not-found', failureClass: 'compile-error', re: /\bmodule not found\b|\bcannot find module\b|\bcan't resolve\b/i },
  { code: 'module-parse-failed', failureClass: 'compile-error', re: /\bmodule parse failed\b/i },
  { code: 'loader-error', failureClass: 'compile-error', re: /(?:vue-template-compiler|babel-loader|stylus-loader|sass-loader|less-loader|postcss-loader).*?(?:error|failed)/i },
  { code: 'cannot-get-route', failureClass: 'route-unreachable', re: /\bcannot\s+(?:get|post|put|patch|delete)\s+\//i },
  { code: 'uncaught-error', failureClass: 'runtime-error', re: /\buncaught(?:\s+\(in promise\))?\b|\bunhandled(?:rejection| promise rejection)\b/i },
  { code: 'javascript-error', failureClass: 'runtime-error', re: /\b(?:type|reference|range|syntax)error\s*:/i },
  { code: 'vue-runtime-error', failureClass: 'runtime-error', re: /\berror in (?:render|mounted|created|beforemount)\b|\bfailed to mount component\b/i },
  { code: 'property-access-error', failureClass: 'runtime-error', re: /\bcannot (?:read|set) propert(?:y|ies)\b/i },
]

const DEFAULT_APP_ROOT_SELECTORS = Object.freeze([
  '#app',
  '#App',
  '#rootApp',
  '#root',
  '[data-v-app]',
  '[data-server-rendered]',
  '#__nuxt',
  '#__next',
])

/**
 * 解析要探测的 SPA 根选择器：显式传入的 selector 优先，其后回落到默认清单。
 * 默认清单同时覆盖 Vue 2（#app / #App / #rootApp）与 Vue 3 / Nuxt / Next 的常见根节点，
 * 避免项目用了非标准挂载点、却必须先手动传 --app-selector 才能被识别。
 */
function resolveAppRootSelectors(requestedSelector) {
  const requested = requestedSelector && requestedSelector !== true
    ? String(requestedSelector).trim()
    : ''
  return Array.from(new Set([requested, ...DEFAULT_APP_ROOT_SELECTORS].filter(Boolean)))
}

function normalizeIssue(value, mask = (input) => input, limit = 800) {
  const raw = String(value || '').replace(/\s+/g, ' ').trim()
  if (!raw) return ''
  return String(mask(raw) || '').slice(0, limit)
}

function rememberIssue(list, value, mask, limit = 20) {
  const normalized = normalizeIssue(value, mask)
  if (!normalized || list.includes(normalized)) return
  list.push(normalized)
  if (list.length > limit) list.shift()
}

function createBrowserIssueCollector(mask = (input) => input) {
  const state = {
    consoleErrorCount: 0,
    consoleErrors: [],
    pageErrorCount: 0,
    pageErrors: [],
    navigationResponses: [],
    navigationFailures: [],
  }

  function attach(page) {
    page.on('console', (message) => {
      if (message.type && message.type() !== 'error') return
      state.consoleErrorCount += 1
      rememberIssue(state.consoleErrors, message.text ? message.text() : '', mask)
    })
    page.on('pageerror', (error) => {
      state.pageErrorCount += 1
      rememberIssue(
        state.pageErrors,
        error && (error.stack || error.message) ? error.stack || error.message : error,
        mask
      )
    })
    page.on('response', (response) => {
      try {
        const request = response.request()
        const frame = request.frame && request.frame()
        if (!request.isNavigationRequest() || (frame && frame.parentFrame())) return
        state.navigationResponses.push({
          status: Number(response.status()),
          url: normalizeIssue(response.url(), mask, 500),
        })
        if (state.navigationResponses.length > 10) state.navigationResponses.shift()
      } catch {
        /* Response metadata is best-effort. */
      }
    })
    page.on('requestfailed', (request) => {
      try {
        const frame = request.frame && request.frame()
        if (!request.isNavigationRequest() || (frame && frame.parentFrame())) return
        const failure = request.failure && request.failure()
        rememberIssue(
          state.navigationFailures,
          `${request.url()} ${failure && failure.errorText ? failure.errorText : 'navigation failed'}`,
          mask,
          10
        )
      } catch {
        /* Request metadata is best-effort. */
      }
    })
    return page
  }

  function snapshot() {
    return JSON.parse(JSON.stringify(state))
  }

  return { attach, snapshot, state }
}

function browserTextSignals(text) {
  const source = String(text || '')
  const signals = []
  for (const item of SIGNAL_PATTERNS) {
    if (item.re.test(source)) signals.push({ code: item.code, failureClass: item.failureClass })
  }
  return signals
}

async function probeRuntimePage(page, options = {}) {
  const selectors = resolveAppRootSelectors(options.appSelector)
  const main = await page.evaluate((appRootSelectors) => {
    let appRoot = null
    let appRootSelector = null
    for (const selector of appRootSelectors) {
      try {
        const candidate = document.querySelector(selector)
        if (candidate) {
          appRoot = candidate
          appRootSelector = selector
          break
        }
      } catch {
        /* Invalid custom selector is reported by the outer probe. */
      }
    }
    const bodyText = document.body && document.body.innerText ? document.body.innerText.trim() : ''
    const overlaySelectors = [
      '#webpack-dev-server-client-overlay',
      '.webpack-dev-server-client-overlay',
      'vite-error-overlay',
      'webpack-dev-server-client-overlay',
    ]
    const overlayPresent = overlaySelectors.some((selector) => document.querySelector(selector))
    return {
      documentTitle: document.title || '',
      readyState: document.readyState,
      appRootPresent: Boolean(appRoot),
      appRootSelector,
      appChildElementCount: appRoot ? appRoot.childElementCount : null,
      appChildNodeCount: appRoot ? appRoot.childNodes.length : null,
      appMounted: Boolean(
        appRoot && (appRoot.childElementCount > 0 || String(appRoot.textContent || '').trim().length > 0)
      ),
      bodyTextLength: bodyText.length,
      bodyTextSample: bodyText.slice(0, 5000),
      overlayPresent,
      scriptCount: document.scripts ? document.scripts.length : 0,
    }
  }, selectors)

  const textSignals = []
  const frames = page.frames ? page.frames() : []
  for (const frame of frames.slice(0, 12)) {
    try {
      const text = await frame.evaluate(() => {
        const body = document.body && document.body.innerText ? document.body.innerText.trim() : ''
        return body.slice(0, 5000)
      })
      for (const signal of browserTextSignals(text)) {
        if (!textSignals.some((item) => item.code === signal.code)) textSignals.push(signal)
      }
    } catch {
      /* Cross-origin or detached frames are not required for the health decision. */
    }
  }

  delete main.bodyTextSample
  return { ...main, textSignals }
}

function issueSignals(issues) {
  const found = []
  for (const issue of issues || []) {
    for (const signal of browserTextSignals(issue)) {
      if (!found.some((item) => item.code === signal.code)) found.push(signal)
    }
  }
  return found
}

function classifyRuntimeHealth(input = {}) {
  const domProbe = input.domProbe || {}
  const collector = input.collector || {}
  const requestedUrl = String(input.requestedUrl || '')
  const finalUrl = String(input.finalUrl || requestedUrl)
  let redirected = Boolean(input.redirected)
  if (!input.redirected && requestedUrl && finalUrl) {
    try { redirected = new URL(requestedUrl).pathname !== new URL(finalUrl).pathname }
    catch { /* Keep the explicit value. */ }
  }

  const signals = []
  const addSignals = (items, source) => {
    for (const item of items || []) {
      if (!item || !item.code) continue
      if (!signals.some((existing) => existing.code === item.code && existing.source === source)) {
        signals.push({ code: item.code, failureClass: item.failureClass, source })
      }
    }
  }
  addSignals(domProbe.textSignals, 'document')
  addSignals(issueSignals(collector.consoleErrors), 'console')
  addSignals(issueSignals(collector.pageErrors), 'pageerror')
  addSignals(issueSignals(collector.navigationFailures), 'navigation')

  const navigationResponses = Array.isArray(collector.navigationResponses)
    ? collector.navigationResponses
    : []
  const mainResponse = navigationResponses.length
    ? navigationResponses[navigationResponses.length - 1]
    : null
  if (mainResponse && Number(mainResponse.status) >= 400) {
    signals.push({
      code: `http-${Number(mainResponse.status)}`,
      failureClass: 'route-unreachable',
      source: 'navigation',
    })
  }
  if (domProbe.overlayPresent && !signals.some((signal) => signal.failureClass === 'compile-error')) {
    signals.push({ code: 'compiler-error-overlay', failureClass: 'compile-error', source: 'document' })
  }
  if (Number(collector.pageErrorCount || 0) > 0 && !signals.some((signal) => signal.source === 'pageerror')) {
    signals.push({ code: 'unhandled-page-error', failureClass: 'runtime-error', source: 'pageerror' })
  }
  if (Array.isArray(collector.navigationFailures) && collector.navigationFailures.length > 0) {
    signals.push({ code: 'navigation-failed', failureClass: 'route-unreachable', source: 'navigation' })
  }

  const priority = ['compile-error', 'runtime-error', 'route-unreachable']
  let failureClass = null
  for (const candidate of priority) {
    if (signals.some((signal) => signal.failureClass === candidate)) {
      failureClass = candidate
      break
    }
  }

  if (!failureClass && domProbe.probeError && !(collector.navigationFailures || []).length) {
    failureClass = 'runtime-error'
  }
  if (!failureClass && domProbe.appRootPresent && !domProbe.appMounted) failureClass = 'app-not-mounted'
  if (!failureClass && !domProbe.appRootPresent && !redirected) failureClass = 'app-not-mounted'

  const status = failureClass ? 'failed' : 'passed'
  return {
    schemaVersion: 1,
    status,
    failureClass,
    askUser: false,
    repairOwner: failureClass ? 'executor' : null,
    compileStatus: failureClass === 'compile-error' ? 'failed' : status === 'passed' ? 'passed' : 'unknown',
    routeAccess: redirected ? 'redirected' : 'target',
    redirected,
    appRootPresent: Boolean(domProbe.appRootPresent),
    appMounted: Boolean(domProbe.appMounted),
    appRootSelector: domProbe.appRootSelector || null,
    appChildElementCount: domProbe.appChildElementCount === undefined ? null : domProbe.appChildElementCount,
    appChildNodeCount: domProbe.appChildNodeCount === undefined ? null : domProbe.appChildNodeCount,
    bodyTextLength: Number(domProbe.bodyTextLength || 0),
    overlayPresent: Boolean(domProbe.overlayPresent),
    navigationStatus: mainResponse ? Number(mainResponse.status) : null,
    consoleErrorCount: Number(collector.consoleErrorCount || 0),
    pageErrorCount: Number(collector.pageErrorCount || 0),
    signals,
  }
}

function isImplementationPath(file) {
  const normalized = String(file || '').replace(/\\/g, '/')
  if (!normalized || normalized.startsWith('d-d2c/temp/') || normalized.includes('/node_modules/')) return false
  return IMPLEMENTATION_EXTENSIONS.has(path.extname(normalized).toLowerCase()) || IMPLEMENTATION_BASENAMES.has(path.basename(normalized))
}

function gitLines(cwd, args) {
  try {
    return execFileSync('git', args, {
      cwd,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).split('\n').map((item) => item.trim()).filter(Boolean)
  } catch {
    return []
  }
}

function changedImplementationFiles(cwd = process.cwd(), extraFiles = []) {
  const root = path.resolve(cwd)
  const tracked = gitLines(root, ['diff', '--name-only', '--diff-filter=ACMRD', 'HEAD', '--'])
  const untracked = gitLines(root, ['ls-files', '--others', '--exclude-standard'])
  const files = new Set([...tracked, ...untracked])
  for (const file of extraFiles || []) {
    if (!file || file === true) continue
    const abs = path.resolve(root, String(file))
    files.add(path.relative(root, abs))
  }
  return Array.from(files)
    .map((file) => String(file).replace(/\\/g, '/'))
    .filter(isImplementationPath)
    .sort()
}

function normalizeRelativeFiles(cwd, files) {
  const root = path.resolve(cwd)
  return Array.from(new Set((files || []).map((file) => {
    if (!file || file === true) return ''
    const abs = path.isAbsolute(String(file)) ? path.resolve(String(file)) : path.resolve(root, String(file))
    return path.relative(root, abs).replace(/\\/g, '/')
  }).filter(Boolean))).sort()
}

function previewHarnessExcludedFiles(startPath, cwd = process.cwd()) {
  if (!startPath) return []
  const root = path.resolve(cwd)
  let current = path.resolve(String(startPath))
  try {
    if (fs.existsSync(current) && fs.statSync(current).isFile()) current = path.dirname(current)
    else if (path.extname(current)) current = path.dirname(current)
  } catch {
    current = path.dirname(current)
  }
  for (let depth = 0; depth < 5; depth += 1) {
    const stateFile = path.join(current, 'preview-harness.json')
    if (fs.existsSync(stateFile)) {
      try {
        const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'))
        return normalizeRelativeFiles(root, (state.files || []).map((item) => item && item.path).filter(Boolean))
      } catch {
        return []
      }
    }
    const parent = path.dirname(current)
    if (parent === current) break
    current = parent
  }
  return []
}

/**
 * 预览 harness 自身源码（区别于它写入的临时产物）：健康检查需要把 harness 源码
 * 也纳入源码摘要，避免「改了 harness 却复用旧健康证据」。
 */
function previewHarnessSourceFiles(startPath, cwd = process.cwd()) {
  if (!startPath) return []
  const root = path.resolve(cwd)
  let current = path.resolve(String(startPath))
  try {
    if (fs.existsSync(current) && fs.statSync(current).isFile()) current = path.dirname(current)
    else if (path.extname(current)) current = path.dirname(current)
  } catch {
    current = path.dirname(current)
  }
  for (let depth = 0; depth < 5; depth += 1) {
    const stateFile = path.join(current, 'preview-harness.json')
    if (fs.existsSync(stateFile)) {
      try {
        const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'))
        return normalizeRelativeFiles(root, state.sourceFiles || [])
      } catch {
        return []
      }
    }
    const parent = path.dirname(current)
    if (parent === current) break
    current = parent
  }
  return []
}

/**
 * 排除项按「路径前缀」匹配：preview-harness.json 常记录整个临时目录，
 * 只用精确相等会把目录下的具体文件漏掉，导致清理预览后业务证据被误判失效。
 */
function isExcludedPath(file, excludedPaths) {
  return excludedPaths.some((excluded) => file === excluded || file.startsWith(`${excluded}/`))
}

function sourceFingerprint(cwd = process.cwd(), extraFiles = [], excludeFiles = []) {
  const root = path.resolve(cwd)
  const excludedFiles = normalizeRelativeFiles(root, excludeFiles)
  const explicitFiles = normalizeRelativeFiles(root, extraFiles).filter(isImplementationPath)
  const files = changedImplementationFiles(root, explicitFiles)
    .filter((file) => !isExcludedPath(file, excludedFiles))
  const hash = crypto.createHash('sha256')
  hash.update('d2c-runtime-source-v1\0')
  for (const file of files) {
    const abs = path.resolve(root, file)
    hash.update(file)
    hash.update('\0')
    if (fs.existsSync(abs) && fs.statSync(abs).isFile()) hash.update(fs.readFileSync(abs))
    else hash.update('<deleted>')
    hash.update('\0')
  }
  return {
    algorithm: 'sha256',
    digest: hash.digest('hex'),
    files,
    explicitFiles,
    excludedFiles,
  }
}

function validateHealthEvidence(evidence, options = {}) {
  const problems = []
  if (!evidence || typeof evidence !== 'object') return ['运行时健康证据缺失或 JSON 无效']
  if (evidence.status !== 'passed') {
    problems.push(`运行时健康检查未通过（failureClass=${evidence.failureClass || 'unknown'}）`)
  }
  if (evidence.askUser === true) problems.push('运行时健康失败不得标记 askUser=true')
  if (!evidence.sourceFingerprint || !evidence.sourceFingerprint.digest) {
    problems.push('运行时健康证据缺少 sourceFingerprint')
    return problems
  }
  const current = sourceFingerprint(
    options.cwd || process.cwd(),
    options.extraFiles || evidence.sourceFingerprint.explicitFiles || [],
    options.excludeFiles || evidence.sourceFingerprint.excludedFiles || []
  )
  if (current.digest !== evidence.sourceFingerprint.digest) {
    problems.push(
      `运行时健康证据已过期（checked=${evidence.sourceFingerprint.digest.slice(0, 12)} current=${current.digest.slice(0, 12)}）`
    )
  }
  return problems
}

function runtimeRepairSentinel(health) {
  const failureClass = health && health.failureClass ? health.failureClass : 'runtime-error'
  const name = failureClass === 'compile-error'
    ? 'D2C_COMPILE_REPAIR_REQUIRED'
    : 'D2C_RUNTIME_REPAIR_REQUIRED'
  return `${name} failureClass=${failureClass} askUser=false consumeRound=false`
}

module.exports = {
  SIGNAL_PATTERNS,
  DEFAULT_APP_ROOT_SELECTORS,
  resolveAppRootSelectors,
  normalizeIssue,
  rememberIssue,
  createBrowserIssueCollector,
  browserTextSignals,
  probeRuntimePage,
  classifyRuntimeHealth,
  changedImplementationFiles,
  previewHarnessExcludedFiles,
  previewHarnessSourceFiles,
  sourceFingerprint,
  validateHealthEvidence,
  runtimeRepairSentinel,
}
