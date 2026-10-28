#!/usr/bin/env node
/* eslint-disable no-console */
'use strict'

const fs = require('fs')
const path = require('path')
const net = require('net')
const http = require('http')
const crypto = require('crypto')
const { spawn } = require('child_process')

const {
  guessChrome,
  loadPuppeteer,
} = require('./lib/d2c-dom-measure-core.cjs')
const {
  createBrowserIssueCollector,
  probeRuntimePage,
  classifyRuntimeHealth,
  validateHealthEvidence,
  runtimeRepairSentinel,
} = require('./lib/d2c-runtime-health-core.cjs')

let appendUrlParams = (url) => url
let maskUrl = (url) => url
let readExtraParams = () => ''
try { ({ appendUrlParams, maskUrl, readExtraParams } = require('./lib/d2c-url-params.cjs')) } catch { /* optional */ }

const HANDOFF_FILE = 'browser-handoff.json'
const VALID_STATES = new Set([
  'requested',
  'browser-ready',
  'awaiting-user',
  'verified',
  'completed',
  'aborted',
  'expired',
])

function parseArgs(argv) {
  const args = {}
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i]
    if (!token.startsWith('--')) continue
    const key = token.slice(2)
    const next = argv[i + 1]
    args[key] = next && !next.startsWith('--') ? next : true
    if (args[key] !== true) i += 1
  }
  return args
}

function now() {
  return new Date().toISOString()
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true })
}

function resolvePageDir(args) {
  const value = args['page-dir'] && args['page-dir'] !== true ? args['page-dir'] : ''
  if (!value) throw new Error(`缺少 --page-dir；用法: node d2c-browser-handoff.cjs --request|--start|--verify|--status|--cleanup --page-dir d-d2c/temp/<page>`)
  return path.resolve(value)
}

function statePath(pageDir) {
  return path.join(pageDir, HANDOFF_FILE)
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'))
}

function readState(pageDir, required = true) {
  const file = statePath(pageDir)
  if (!fs.existsSync(file)) {
    if (required) throw new Error(`找不到 browser handoff 状态: ${file}`)
    return null
  }
  return readJson(file)
}

function writeState(pageDir, state) {
  ensureDir(pageDir)
  if (!VALID_STATES.has(state.status)) throw new Error(`非法 handoff 状态: ${state.status}`)
  state.version = 1
  state.updatedAt = now()
  const file = statePath(pageDir)
  const temp = `${file}.tmp-${process.pid}`
  fs.writeFileSync(temp, `${JSON.stringify(state, null, 2)}\n`, 'utf8')
  fs.renameSync(temp, file)
  return file
}

function safeUrl(value, args) {
  if (!value || value === true) return null
  const masked = maskUrl(String(value))
  try {
    const url = new URL(masked)
    const injectedKeys = new Set()
    for (const [key] of new URLSearchParams(readExtraParams(args))) injectedKeys.add(key)
    for (const key of injectedKeys) {
      if (url.searchParams.has(key)) url.searchParams.set(key, '***')
    }
    return url.toString()
  } catch {
    return masked
  }
}

function urlPath(value) {
  try { return new URL(String(value)).pathname }
  catch { return null }
}

function safeRoute(value, args) {
  const masked = safeUrl(value, args)
  if (!masked) return null
  try {
    const url = new URL(masked)
    const query = new URLSearchParams()
    for (const [key, item] of url.searchParams) {
      if (item !== '***') query.append(key, item)
    }
    const suffix = query.toString()
    return `${url.pathname}${suffix ? `?${suffix}` : ''}${url.hash || ''}`
  } catch {
    return urlPath(masked)
  }
}

function positiveInt(value, fallback) {
  const n = parseInt(String(value), 10)
  return Number.isFinite(n) && n > 0 ? n : fallback
}

function targetIdOf(page) {
  if (!page || !page.target) return null
  const target = page.target()
  return target._targetId || (target._targetInfo && target._targetInfo.targetId) || null
}

function sessionId(endpoint, targetId) {
  return crypto.createHash('sha256').update(`${endpoint}|${targetId || ''}`).digest('hex').slice(0, 20)
}

function portFree(port) {
  return new Promise((resolve) => {
    const server = net.createServer()
    server.once('error', () => resolve(false))
    server.once('listening', () => server.close(() => resolve(true)))
    server.listen(port, '127.0.0.1')
  })
}

async function findFreePort(start = 9223, span = 100) {
  for (let port = start; port < start + span; port += 1) {
    if (await portFree(port)) return port
  }
  throw new Error(`找不到可用 CDP 端口: ${start}-${start + span - 1}`)
}

function requestJson(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => {
      const chunks = []
      res.on('data', (chunk) => chunks.push(chunk))
      res.on('end', () => {
        if (!res.statusCode || res.statusCode >= 400) return reject(new Error(`HTTP ${res.statusCode || '?'}`))
        try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))) }
        catch (error) { reject(error) }
      })
    })
    req.setTimeout(1500, () => req.destroy(new Error('timeout')))
    req.on('error', reject)
  })
}

async function waitForCdp(endpoint, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs
  let lastError = null
  while (Date.now() < deadline) {
    try { return await requestJson(`${endpoint}/json/version`) }
    catch (error) { lastError = error }
    await sleep(250)
  }
  throw new Error(`Chrome CDP 未在 ${timeoutMs}ms 内就绪: ${endpoint} (${lastError ? lastError.message : 'unknown'})`)
}

async function waitForPageReady(page, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs
  let current = page.url()
  while (Date.now() < deadline) {
    current = page.url()
    if (/^https?:/i.test(current)) {
      try {
        const ready = await page.evaluate(() => document.readyState === 'complete')
        if (ready) return current
      } catch { /* target is still initializing */ }
    }
    await sleep(100)
  }
  throw new Error(`目标 tab 未在 ${timeoutMs}ms 内完成初始导航: ${safeUrl(current) || current}`)
}

async function selectPage(browser, targetId, targetUrl) {
  const pages = await browser.pages()
  if (!pages.length) throw new Error('CDP 浏览器中没有可用页面')
  if (targetId) {
    const exact = pages.find((page) => targetIdOf(page) === targetId)
    if (exact) return exact
  }
  const wantedPath = urlPath(targetUrl)
  if (wantedPath) {
    const route = pages.find((page) => urlPath(page.url()) === wantedPath)
    if (route) return route
  }
  return pages.find((page) => !/^chrome:|^devtools:/i.test(page.url())) || pages[0]
}

function requestHandoff({ pageDir, targetUrl, expectedSelector, captureMetaPath, reason }) {
  if (!captureMetaPath) {
    throw new Error('拒绝创建 handoff：必须传 --capture-meta，以证明当前源码对应的真实路由健康且发生重定向')
  }
  const captureMeta = readJson(path.resolve(captureMetaPath))
  if (captureMeta.accessMode !== 'real-route') {
    throw new Error(`拒绝创建 handoff：capture-meta.json accessMode=${captureMeta.accessMode || 'missing'}，只有 real-route 可升级为用户接管`)
  }
  const runtimeEvidence = {
    ...(captureMeta.runtimeHealth || {}),
    sourceFingerprint: captureMeta.sourceFingerprint || null,
  }
  const healthProblems = validateHealthEvidence(runtimeEvidence, { cwd: process.cwd() })
  if (captureMeta.materialStatus === 'invalid' || captureMeta.invalidReason) {
    healthProblems.push(`capture material invalid: ${captureMeta.invalidReason || 'materialStatus=invalid'}`)
  }
  if (healthProblems.length) {
    const health = captureMeta.runtimeHealth || { failureClass: captureMeta.failureClass || 'runtime-health-invalid' }
    throw new Error(
      `拒绝创建 handoff：${healthProblems.join('; ')}。${runtimeRepairSentinel(health)}；` +
      '请由 executor 修复并覆盖重采当前轮，用户无法修复编译/runtime/dev-server 路由错误。'
    )
  }
  if (!captureMeta.redirected && !captureMeta.realRouteBlockedByRedirect) {
    throw new Error('拒绝创建 handoff：capture-meta.json 未证明健康的真实路由发生重定向')
  }
  const previous = readState(pageDir, false)
  const state = {
    ...(previous || {}),
    version: 1,
    mode: 'update-page',
    status: 'requested',
    active: true,
    requestedAt: (previous && previous.requestedAt) || now(),
    targetUrl: safeUrl(targetUrl) || (previous && previous.targetUrl) || null,
    targetRoute: safeRoute(targetUrl) || (previous && previous.targetRoute) || null,
    targetPath: urlPath(targetUrl) || (previous && previous.targetPath) || null,
    expectedSelector: expectedSelector || (previous && previous.expectedSelector) || null,
    reason: reason || 'real-route-redirected-after-retry',
    captureMetaPath: captureMetaPath ? path.resolve(captureMetaPath) : null,
    runtimeHealth: captureMeta.runtimeHealth,
    sourceFingerprint: captureMeta.sourceFingerprint,
    viewport: captureMeta && captureMeta.viewport
      ? {
          width: positiveInt(captureMeta.viewport.width, 375),
          height: positiveInt(captureMeta.viewport.height, 900),
          dpr: Number(captureMeta.viewport.dpr || 2),
        }
      : (previous && previous.viewport) || null,
    verification: null,
  }
  const file = writeState(pageDir, state)
  return { file, state }
}

function prepareLaunchState(state) {
  if (!['requested', 'browser-ready', 'awaiting-user', 'aborted', 'expired'].includes(state.status)) {
    throw new Error(`handoff 当前状态 ${state.status} 不允许启动浏览器`)
  }
  if (!['aborted', 'expired'].includes(state.status)) return state
  return {
    ...state,
    status: 'requested',
    active: true,
    restartedAt: now(),
    cleanedAt: null,
    startError: null,
    verification: null,
    cdpEndpoint: null,
    browserPid: null,
    browserTargetId: null,
    browserSessionId: null,
    profileDir: null,
    currentUrl: null,
    currentPath: null,
  }
}

async function startBrowser(args, pageDir) {
  const state = readState(pageDir)
  const launchState = prepareLaunchState(state)
  const healthProblems = validateHealthEvidence({
    ...(launchState.runtimeHealth || {}),
    sourceFingerprint: launchState.sourceFingerprint || null,
  }, { cwd: process.cwd() })
  if (healthProblems.length) {
    const health = launchState.runtimeHealth || { failureClass: 'runtime-health-stale' }
    const error = new Error(
      `handoff 启动前健康证据无效：${healthProblems.join('; ')}。${runtimeRepairSentinel(health)}`
    )
    error.d2cRepairRequired = true
    error.failureClass = health.failureClass || 'runtime-health-stale'
    writeState(pageDir, {
      ...launchState,
      status: 'aborted',
      active: false,
      startError: error.message,
      failureClass: error.failureClass,
    })
    throw error
  }
  const chrome = guessChrome()
  if (!chrome) throw new Error('未找到 Chrome/Chromium，请设置 PUPPETEER_EXECUTABLE_PATH 或 CHROME_PATH')

  const rawUrl = args.url && args.url !== true ? appendUrlParams(String(args.url), args) : null
  if (!rawUrl) throw new Error('--start 必须提供 --url <真实目标 URL>；URL 只用于导航，不会原样写入状态文件')
  const requestedViewport = launchState.viewport || {}
  const width = positiveInt(args.width || args['viewport-width'] || requestedViewport.width, 375)
  const height = positiveInt(args.height || args['viewport-height'] || requestedViewport.height, 900)
  const dprRaw = Number(args.dpr || requestedViewport.dpr || 2)
  const dpr = dprRaw > 0 ? dprRaw : 2
  const port = await findFreePort(positiveInt(args.port, 9223), positiveInt(args['port-span'], 100))
  const endpoint = `http://127.0.0.1:${port}`
  const profileDir = path.join(pageDir, '.browser-handoff-profile')
  ensureDir(profileDir)

  const child = spawn(chrome, [
    `--remote-debugging-port=${port}`,
    '--remote-debugging-address=127.0.0.1',
    `--user-data-dir=${profileDir}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-background-networking',
    '--new-window',
    'about:blank',
  ], {
    detached: true,
    stdio: 'ignore',
  })
  child.unref()

  try {
    await waitForCdp(endpoint, positiveInt(args.timeout, 20000))
    const { puppeteer } = loadPuppeteer()
    const browser = await puppeteer.connect({ browserURL: endpoint, defaultViewport: null })
    try {
      const browserSession = await browser.target().createCDPSession()
      let targetId
      try {
        const created = await browserSession.send('Target.createTarget', {
          url: rawUrl,
          newWindow: true,
          width,
          height,
        })
        targetId = created.targetId
      } finally {
        await browserSession.detach()
      }
      const page = await selectPage(browser, targetId, rawUrl)
      const browserIssues = createBrowserIssueCollector(maskUrl)
      browserIssues.attach(page)
      await page.setViewport({ width, height, deviceScaleFactor: dpr })
      const currentUrl = await waitForPageReady(page, positiveInt(args['navigation-timeout'], 120000))
      await sleep(positiveInt(args['health-wait'], 800))
      const runtimeDomProbe = await probeRuntimePage(page)
      const runtimeHealth = classifyRuntimeHealth({
        requestedUrl: rawUrl,
        finalUrl: currentUrl,
        domProbe: runtimeDomProbe,
        collector: browserIssues.snapshot(),
      })
      if (runtimeHealth.status !== 'passed') {
        const error = new Error(
          `handoff 目标页运行时不可用（failureClass=${runtimeHealth.failureClass || 'unknown'}）。` +
          runtimeRepairSentinel(runtimeHealth)
        )
        error.d2cRepairRequired = true
        error.failureClass = runtimeHealth.failureClass || 'runtime-error'
        error.runtimeHealth = runtimeHealth
        throw error
      }
      targetId = targetIdOf(page)
      const next = {
        ...launchState,
        status: 'awaiting-user',
        active: true,
        browserReadyAt: now(),
        browserPid: child.pid,
        cdpEndpoint: endpoint,
        browserTargetId: targetId,
        browserSessionId: sessionId(endpoint, targetId),
        profileDir,
        viewport: { width, height, dpr },
        targetUrl: safeUrl(rawUrl, args),
        targetRoute: safeRoute(rawUrl, args),
        targetPath: urlPath(rawUrl),
        currentUrl: safeUrl(currentUrl, args),
        currentPath: urlPath(currentUrl),
        runtimeHealth,
      }
      writeState(pageDir, next)
      return next
    } finally {
      browser.disconnect()
    }
  } catch (error) {
    try { process.kill(-child.pid, 'SIGKILL') } catch { /* already stopped */ }
    if (error.d2cRepairRequired && fs.existsSync(profileDir)) {
      fs.rmSync(profileDir, { recursive: true, force: true })
    }
    writeState(pageDir, {
      ...launchState,
      status: error.d2cRepairRequired ? 'aborted' : 'requested',
      active: error.d2cRepairRequired ? false : true,
      startError: error.message,
      failureClass: error.failureClass || null,
      runtimeHealth: error.runtimeHealth || launchState.runtimeHealth || null,
      cdpEndpoint: null,
      browserPid: null,
      profileDir: null,
    })
    throw error
  }
}

async function verifyBrowser(args, pageDir) {
  const state = readState(pageDir)
  if (!state.cdpEndpoint) throw new Error('browser-handoff.json 缺少 cdpEndpoint，请先执行 --start')
  const { puppeteer } = loadPuppeteer()
  const browser = await puppeteer.connect({ browserURL: state.cdpEndpoint, defaultViewport: null })
  let browserClosed = false
  try {
    const page = await selectPage(browser, args['target-id'] || state.browserTargetId, args['target-url'] || state.targetUrl)
    const browserIssues = createBrowserIssueCollector(maskUrl)
    browserIssues.attach(page)
    const viewport = state.viewport || { width: 375, height: 900, dpr: 2 }
    await page.setViewport({
      width: positiveInt(viewport.width, 375),
      height: positiveInt(viewport.height, 900),
      deviceScaleFactor: Number(viewport.dpr || 2),
    })
    const waitMs = positiveInt(args.wait, 1200)
    await sleep(waitMs)
    const firstUrl = page.url()
    await sleep(500)
    const finalUrl = page.url()
    const expectedPath = urlPath(args['target-url'] || state.targetUrl) || state.targetPath
    const finalPath = urlPath(finalUrl)
    const selector = args.selector && args.selector !== true ? String(args.selector) : state.expectedSelector
    const runtimeDomProbe = await probeRuntimePage(page)
    const runtimeHealth = classifyRuntimeHealth({
      requestedUrl: args['target-url'] || state.targetUrl,
      finalUrl,
      domProbe: runtimeDomProbe,
      collector: browserIssues.snapshot(),
    })
    const dom = await page.evaluate((expectedSelector) => {
      const app = document.querySelector('#app')
      const target = expectedSelector ? document.querySelector(expectedSelector) : null
      const rect = target ? target.getBoundingClientRect() : null
      const style = target ? getComputedStyle(target) : null
      return {
        appChildElementCount: app ? app.childElementCount : null,
        selectorMatched: Boolean(target),
        selectorLayoutMark: target ? target.getAttribute('data-d2c-mark') : null,
        selectorVisible: Boolean(
          target && rect && rect.width > 0 && rect.height > 0 &&
          style && style.display !== 'none' && style.visibility !== 'hidden'
        ),
        selectorBBoxCss: rect ? {
          left: rect.left,
          top: rect.top,
          width: rect.width,
          height: rect.height,
        } : null,
        bodyTextLength: document.body && document.body.innerText ? document.body.innerText.trim().length : 0,
      }
    }, selector || null)
    const errors = []
    if (runtimeHealth.status !== 'passed') {
      errors.push(`运行时健康失败: failureClass=${runtimeHealth.failureClass || 'unknown'}`)
    }
    if (firstUrl !== finalUrl) errors.push('页面在验证窗口内仍发生跳转')
    if (expectedPath && expectedPath !== finalPath) errors.push(`目标路径不匹配: expected=${expectedPath} actual=${finalPath}`)
    if (!selector) errors.push('缺少 expected selector，无法证明目标组件已进入真实页面')
    else if (!dom.selectorMatched) errors.push(`目标 selector 未命中: ${selector}`)
    else if (!dom.selectorVisible) errors.push(`目标 selector 不可见或尺寸为 0: ${selector}`)
    if (dom.appChildElementCount === 0) errors.push('#app 已存在但没有子元素')

    const targetId = targetIdOf(page)
    const verification = {
      verifiedAt: now(),
      ok: errors.length === 0,
      expectedPath: expectedPath || null,
      finalUrl: safeUrl(finalUrl),
      finalPath,
      selector: selector || null,
      ...dom,
      errors,
    }
    const next = {
      ...state,
      status: runtimeHealth.status !== 'passed' ? 'aborted' : errors.length ? 'awaiting-user' : 'verified',
      active: runtimeHealth.status === 'passed',
      browserTargetId: targetId,
      browserSessionId: sessionId(state.cdpEndpoint, targetId),
      currentUrl: safeUrl(finalUrl),
      currentPath: finalPath,
      failureClass: runtimeHealth.failureClass || null,
      runtimeHealth,
      verification,
    }
    if (runtimeHealth.status !== 'passed') {
      try {
        await browser.close()
        browserClosed = true
      } catch { /* browser may already be gone */ }
      if (state.browserPid) {
        try { process.kill(-Number(state.browserPid), 'SIGKILL') } catch { /* already stopped */ }
      }
      const profileDir = safeProfileDir(pageDir, state.profileDir)
      if (profileDir && fs.existsSync(profileDir)) fs.rmSync(profileDir, { recursive: true, force: true })
      next.cdpEndpoint = null
      next.browserPid = null
      next.profileDir = null
    }
    writeState(pageDir, next)
    return next
  } finally {
    if (!browserClosed) browser.disconnect()
  }
}

function safeProfileDir(pageDir, profileDir) {
  if (!profileDir) return null
  const abs = path.resolve(profileDir)
  const root = path.resolve(pageDir) + path.sep
  if (!abs.startsWith(root) || path.basename(abs) !== '.browser-handoff-profile') return null
  return abs
}

async function cleanupBrowser(args, pageDir) {
  const state = readState(pageDir)
  if (state.cdpEndpoint) {
    try {
      const { puppeteer } = loadPuppeteer()
      const browser = await puppeteer.connect({ browserURL: state.cdpEndpoint, defaultViewport: null })
      await browser.close()
    } catch { /* browser may already be gone */ }
  }
  if (state.browserPid) {
    try { process.kill(-Number(state.browserPid), 'SIGKILL') } catch { /* already stopped */ }
  }
  const profileDir = safeProfileDir(pageDir, state.profileDir)
  if (profileDir && fs.existsSync(profileDir)) fs.rmSync(profileDir, { recursive: true, force: true })
  const result = args.result && args.result !== true ? String(args.result) : 'completed'
  const status = ['completed', 'aborted', 'expired'].includes(result) ? result : 'completed'
  const next = {
    ...state,
    status,
    active: false,
    cleanedAt: now(),
    cdpEndpoint: null,
    browserPid: null,
    profileDir: null,
  }
  writeState(pageDir, next)
  return next
}

function publicState(state, pageDir) {
  return {
    stateFile: statePath(pageDir),
    status: state.status,
    active: Boolean(state.active),
    mode: state.mode,
    targetUrl: state.targetUrl || null,
    targetRoute: state.targetRoute || null,
    targetPath: state.targetPath || null,
    expectedSelector: state.expectedSelector || null,
    currentUrl: state.currentUrl || null,
    currentPath: state.currentPath || null,
    cdpEndpoint: state.cdpEndpoint || null,
    browserTargetId: state.browserTargetId || null,
    browserSessionId: state.browserSessionId || null,
    viewport: state.viewport || null,
    verification: state.verification || null,
    failureClass: state.failureClass || null,
    runtimeHealth: state.runtimeHealth || null,
  }
}

async function main() {
  const args = parseArgs(process.argv)
  const pageDir = resolvePageDir(args)
  let state

  if (args.request) {
    const result = requestHandoff({
      pageDir,
      targetUrl: args['target-url'] || args.url,
      expectedSelector: args.selector && args.selector !== true ? String(args.selector) : null,
      captureMetaPath: args['capture-meta'] && args['capture-meta'] !== true ? args['capture-meta'] : null,
      reason: args.reason && args.reason !== true ? String(args.reason) : null,
    })
    state = result.state
    console.log(JSON.stringify(publicState(state, pageDir), null, 2))
    console.log(`D2C_USER_BROWSER_HANDOFF_REQUIRED page-dir=${path.basename(pageDir)}`)
    return
  }

  if (args.start) state = await startBrowser(args, pageDir)
  else if (args.verify) state = await verifyBrowser(args, pageDir)
  else if (args.cleanup || args.abort || args.expire) {
    if (args.abort) args.result = 'aborted'
    if (args.expire) args.result = 'expired'
    state = await cleanupBrowser(args, pageDir)
  } else if (args.status) state = readState(pageDir)
  else throw new Error('必须指定 --request、--start、--verify、--status、--cleanup、--abort 或 --expire')

  console.log(JSON.stringify(publicState(state, pageDir), null, 2))
  if (args.verify && state.runtimeHealth && state.runtimeHealth.status !== 'passed') {
    console.error(runtimeRepairSentinel(state.runtimeHealth))
    process.exitCode = 2
  } else if (args.verify && state.status !== 'verified') {
    console.error(`D2C_USER_BROWSER_NOT_READY page-dir=${path.basename(pageDir)}`)
    process.exitCode = 2
  }
}

if (require.main === module) {
  main().catch((error) => {
    console.error(`❌ ${error && error.message ? error.message : error}`)
    process.exit(1)
  })
}

module.exports = {
  HANDOFF_FILE,
  VALID_STATES,
  parseArgs,
  readState,
  writeState,
  requestHandoff,
  prepareLaunchState,
  safeUrl,
  safeRoute,
  targetIdOf,
  sessionId,
  selectPage,
  waitForPageReady,
  publicState,
}
