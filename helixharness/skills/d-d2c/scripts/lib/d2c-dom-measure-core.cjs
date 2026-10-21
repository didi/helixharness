/* eslint-disable no-console */
/**
 * d2c-dom-measure-core.cjs — DOM 实测自检的共用核心
 *
 * d2c-geometry-check.cjs（X 轴/尺寸/字号）、d2c-vertical-check.cjs（Y 轴/高度）
 * 与 d2c-render-check.cjs（computed style/font/assets）
 * 共用：命令行解析、Chrome 探测、puppeteer 启动、DSL bbox 读取、组件作用域 marks
 * 解析、marked DSL 加载、设计画板宽度读取、按 layoutMark 反查原始节点。
 * 抽到一处，避免两份脚本逻辑漂移。
 */
const fs = require('fs')
const path = require('path')

function round1(n) {
  return Math.round(Number(n || 0) * 10) / 10
}

function parseArgs(argv) {
  const args = {}
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i]
    if (token.startsWith('--')) {
      const key = token.slice(2)
      const value =
        argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : true
      args[key] = value
      if (value !== true) i += 1
    }
  }
  return args
}

function parsePositiveInt(value) {
  const n = parseInt(String(value), 10)
  return Number.isFinite(n) && n > 0 ? n : null
}

function parsePositiveFloat(value) {
  const n = parseFloat(String(value))
  return Number.isFinite(n) && n > 0 ? n : null
}

function readCaptureMeta(outDir) {
  if (!outDir) return null
  const metaPath = path.join(path.resolve(outDir), 'capture-meta.json')
  if (!fs.existsSync(metaPath)) return null
  try {
    return JSON.parse(fs.readFileSync(metaPath, 'utf8'))
  } catch (err) {
    console.error(`❌ capture-meta.json 解析失败：${err.message}`)
    process.exit(2)
  }
}

function captureRegionFromMeta(meta) {
  if (!meta || meta.captureMode !== 'region') return null
  const region = meta.regionBBoxCss || meta.captureRegionCss || null
  if (!region) return null
  const left = Number(region.left)
  const top = Number(region.top)
  const width = Number(region.width)
  const height = Number(region.height)
  if (![left, top, width, height].every(Number.isFinite) || width <= 0 || height <= 0) return null
  return { left, top, width, height }
}

function allowInvalidMaterial(args) {
  return !!(args && (args['allow-invalid-material'] || args.allowInvalidMaterial))
}

function assertUsableCaptureMaterial(args, outDir, toolName = 'd2c-measure') {
  const meta = readCaptureMeta(outDir)
  if (!meta || allowInvalidMaterial(args)) return meta
  if (meta.materialStatus === 'invalid' || meta.invalidReason) {
    const reason = meta.invalidReason || 'capture-meta.json materialStatus=invalid'
    console.error(
      `❌ ${toolName} 拒绝基于无效截图继续生成量化 diff：${reason}\n` +
        '   请先修复截图入口/preview harness 并重跑 d2c-capture-page.cjs；' +
        '不要把空 DOM、错页或未命中目标 Vue DOM 的材料送进 geometry/vertical/render。'
    )
    process.exit(2)
  }
  if (meta.captureMode === 'region' && !captureRegionFromMeta(meta)) {
    console.error(
      `❌ ${toolName} 发现 captureMode=region，但 capture-meta.json 缺少有效 regionBBoxCss。` +
        '局部截图必须记录实际 DOM 区域 bbox，才能把 DOM 坐标换算到设计局部坐标系。'
    )
    process.exit(2)
  }
  return meta
}

function readCaptureViewportWidth(outDir) {
  const meta = readCaptureMeta(outDir)
  if (!meta) return null
  try {
    return (
      parsePositiveInt(meta.viewportWidthCss) ||
      parsePositiveInt(meta.viewport && meta.viewport.width) ||
      null
    )
  } catch (_) {
    return null
  }
}

function resolveMeasurementContext(args, outDir, defaultWidth = 678) {
  const meta = readCaptureMeta(outDir)
  const explicit = args && args.width && args.width !== true ? parsePositiveInt(args.width) : null
  const viewportWidthCss =
    explicit ||
    (meta && (parsePositiveInt(meta.viewportWidthCss) || parsePositiveInt(meta.viewport && meta.viewport.width))) ||
    parsePositiveInt(process.env.D2C_VIEWPORT_WIDTH) ||
    defaultWidth
  const regionCss = captureRegionFromMeta(meta)
  // region 模式下 capture-meta.json 记录的 designScope 就是测量必须对齐的设计根；
  // 解析出来供 geometry/vertical/render 与 checkpoint 共用同一口径。
  const designScopeMarks = regionCss && meta && meta.designScope
    ? String(meta.designScope).split(',').map((mark) => mark.trim()).filter(Boolean)
    : null
  return {
    captureMeta: meta,
    viewportWidthCss,
    captureMode: regionCss ? 'region' : 'full-page',
    regionCss,
    designScopeMarks,
    effectiveCssWidth: regionCss ? parsePositiveFloat(regionCss.width) : viewportWidthCss,
  }
}

function resolveMeasurementViewportWidth(args, outDir, defaultWidth = 678) {
  if (args && args.width && args.width !== true) {
    const explicit = parsePositiveInt(args.width)
    if (explicit) return explicit
  }
  const fromCapture = readCaptureViewportWidth(outDir)
  if (fromCapture) return fromCapture
  return parsePositiveInt(process.env.D2C_VIEWPORT_WIDTH) || defaultWidth
}

/** 浏览器可执行文件候选路径（Chromium 内核：Chrome / Chromium / Edge / Brave） */
function browserExecutableCandidates() {
  const candidates = [
    // macOS
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
    // macOS Homebrew (Apple Silicon)
    '/opt/homebrew/bin/google-chrome',
    '/opt/homebrew/bin/microsoft-edge',
    '/opt/homebrew/bin/chromium',
    // Linux
    '/usr/bin/google-chrome-stable',
    '/usr/bin/google-chrome',
    '/usr/bin/microsoft-edge-stable',
    '/usr/bin/microsoft-edge',
    '/usr/bin/chromium-browser',
    '/usr/bin/chromium',
    '/snap/bin/chromium',
    '/opt/microsoft/msedge/msedge',
    // Windows
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  ]
  if (process.env.LOCALAPPDATA) {
    candidates.push(
      path.win32.join(
        process.env.LOCALAPPDATA,
        'Microsoft',
        'Edge',
        'Application',
        'msedge.exe'
      )
    )
  }
  return candidates
}

function guessChrome() {
  if (process.env.PUPPETEER_EXECUTABLE_PATH)
    return process.env.PUPPETEER_EXECUTABLE_PATH
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH
  if (process.env.EDGE_PATH) return process.env.EDGE_PATH
  for (const p of browserExecutableCandidates()) {
    try {
      if (fs.existsSync(p)) return p
    } catch (_) {
      /* ignore */
    }
  }
  // 兜底：尝试 PATH 中的命令
  for (const cmd of ['google-chrome-stable', 'google-chrome', 'microsoft-edge-stable', 'microsoft-edge', 'chromium-browser', 'chromium', 'brave-browser', 'chrome', 'edge']) {
    try {
      const resolved = require('child_process').execSync(`which ${cmd} 2>/dev/null || where ${cmd} 2>nul`, { encoding: 'utf8', timeout: 3000 }).trim()
      if (resolved) return resolved
    } catch (_) { /* ignore */ }
  }
  return null
}

/** 解析 Chrome 与 puppeteer-core；任一缺失打印一致提示并退出 */
function loadPuppeteer() {
  const chrome = guessChrome()
  if (!chrome) {
    console.error(
      '未找到 Chrome/Chromium/Edge，请设置 PUPPETEER_EXECUTABLE_PATH、CHROME_PATH 或 EDGE_PATH。'
    )
    process.exit(1)
  }
  let puppeteer
  try {
    puppeteer = require('puppeteer-core')
  } catch (e) {
    console.error(
      '无法加载 puppeteer-core。请在仓库根执行，并确保已安装（必要时 NODE_PATH=$(pwd)/node_modules）。'
    )
    process.exit(1)
  }
  return { chrome, puppeteer }
}

/** 启动 headless 浏览器（参数与历史 geometry-check 一致） */
function launchBrowser(puppeteer, chrome) {
  return puppeteer.launch({
    executablePath: chrome,
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  })
}

function readBrowserHandoff(args) {
  const file = args && args['handoff-state'] && args['handoff-state'] !== true
    ? path.resolve(String(args['handoff-state']))
    : null
  if (!file) return null
  if (!fs.existsSync(file)) throw new Error(`找不到 browser handoff 状态: ${file}`)
  const state = JSON.parse(fs.readFileSync(file, 'utf8'))
  if (!['verified', 'completed'].includes(state.status)) {
    throw new Error(`browser handoff 尚未验证（status=${state.status || 'missing'}）`)
  }
  return { file, state }
}

function browserTargetId(page) {
  if (!page || !page.target) return null
  const target = page.target()
  return target._targetId || (target._targetInfo && target._targetInfo.targetId) || null
}

function browserSessionId(endpoint, targetId) {
  const crypto = require('crypto')
  return crypto.createHash('sha256').update(`${endpoint}|${targetId || ''}`).digest('hex').slice(0, 20)
}

async function selectAttachedPage(browser, targetId) {
  const pages = await browser.pages()
  if (!pages.length) throw new Error('CDP 浏览器中没有可用页面')
  if (targetId) {
    const exact = pages.find((page) => browserTargetId(page) === targetId)
    if (exact) return exact
    throw new Error(`CDP 浏览器中找不到目标 tab: ${targetId}`)
  }
  return pages.find((page) => !/^chrome:|^devtools:/i.test(page.url())) || pages[0]
}

/**
 * Open either the historical owned headless page or a verified user handoff tab.
 * Attached mode never navigates, reloads, closes the tab, or closes Chrome.
 */
async function openBrowserSession(options) {
  const {
    puppeteer,
    chrome,
    args = {},
    url,
    viewport,
    waitMs = 0,
    applySessionState = async () => ({ applied: false }),
    onPage = null,
  } = options
  const handoff = readBrowserHandoff(args)
  const endpointArg = args['cdp-endpoint'] && args['cdp-endpoint'] !== true
    ? String(args['cdp-endpoint'])
    : process.env.D2C_CDP_ENDPOINT
  const endpoint = endpointArg || (handoff && handoff.state.cdpEndpoint)
  const targetIdArg = args['target-id'] && args['target-id'] !== true
    ? String(args['target-id'])
    : process.env.D2C_BROWSER_TARGET_ID
  const targetId = targetIdArg || (handoff && handoff.state.browserTargetId)

  if (endpoint) {
    if (handoff && handoff.state.active === false) {
      throw new Error('browser handoff 已 cleanup，无法继续连接')
    }
    const browser = await puppeteer.connect({ browserURL: endpoint, defaultViewport: null })
    try {
      const page = await selectAttachedPage(browser, targetId)
      if (onPage) await onPage(page)
      if (waitMs > 0) await new Promise((resolve) => setTimeout(resolve, waitMs))
      const actualTargetId = browserTargetId(page)
      const expectedPath = handoff && handoff.state.verification && handoff.state.verification.finalPath
      let actualPath = null
      try { actualPath = new URL(page.url()).pathname } catch { /* handled below */ }
      if (expectedPath && actualPath !== expectedPath) {
        throw new Error(`用户浏览器已离开验证页面: expected=${expectedPath} actual=${actualPath || page.url()}`)
      }
      return {
        browser,
        page,
        attached: true,
        endpoint,
        targetId: actualTargetId,
        sessionId: browserSessionId(endpoint, actualTargetId),
        handoff: handoff && handoff.state,
      }
    } catch (error) {
      browser.disconnect()
      throw error
    }
  }

  const browser = await launchBrowser(puppeteer, chrome)
  try {
    const page = await browser.newPage()
    if (onPage) await onPage(page)
    if (viewport) await page.setViewport(viewport)
    await applySessionState(page, url, args)
    await page.goto(url, { waitUntil: 'load', timeout: 120000 })
    if (waitMs > 0) await new Promise((resolve) => setTimeout(resolve, waitMs))
    const actualTargetId = browserTargetId(page)
    return {
      browser,
      page,
      attached: false,
      endpoint: null,
      targetId: actualTargetId,
      sessionId: null,
      handoff: null,
    }
  } catch (error) {
    await browser.close()
    throw error
  }
}

async function closeBrowserSession(session) {
  if (!session || !session.browser) return
  if (session.attached) session.browser.disconnect()
  else await session.browser.close()
}

function getBBoxLocal(node) {
  const ls = node.layoutStyle || {}
  return {
    x: Number(ls.relativeX || 0),
    y: Number(ls.relativeY || 0),
    w: Number(ls.width || 0),
    h: Number(ls.height || 0),
  }
}

function getNodeText(node) {
  return (node.text || [])
    .map((t) => (t && t.text ? t.text : ''))
    .filter(Boolean)
    .join('')
}

/** 解析组件的整页 layoutMarks：component-meta.json 优先，回退组件清单 */
function resolveComponentMarks(pageDir, componentId) {
  const candidates = [
    {
      file: path.join(pageDir, 'components', componentId, 'component-meta.json'),
      pick: (j) => j.pageLayoutMarks,
    },
    {
      file: path.join(pageDir, 'component-plan.json'),
      pick: (j) => {
        const c = (j.components || j || []).find?.((x) => x.id === componentId)
        return c && (c.layoutMarks || (c.layoutMark ? [c.layoutMark] : null))
      },
    },
    {
      file: path.join(pageDir, 'complexity-plan.json'),
      pick: (j) => {
        const c = (j.suggestedComponents || []).find((x) => x.id === componentId)
        return c && c.layoutMarks
      },
    },
  ]
  for (const { file, pick } of candidates) {
    if (!fs.existsSync(file)) continue
    try {
      const marks = pick(JSON.parse(fs.readFileSync(file, 'utf8')))
      if (Array.isArray(marks) && marks.length) return { marks, source: file }
    } catch (_) {
      /* 尝试下一个来源 */
    }
  }
  return null
}

function loadMarkedDsl(pageDir) {
  const markedPath = path.join(pageDir, 'dsl_layout_marked.json')
  if (!fs.existsSync(markedPath)) {
    console.error(`❌ 找不到 ${markedPath}（须先运行 filter-dsl.cjs）`)
    process.exit(1)
  }
  const obj = JSON.parse(fs.readFileSync(markedPath, 'utf8'))
  const dsl = obj.dsl !== undefined ? obj.dsl : obj
  return { dsl, nodes: dsl.nodes || [] }
}

function loadDesignFrameWidth(pageDir) {
  const p = path.join(pageDir, 'layout-h5.json')
  try {
    const j = JSON.parse(fs.readFileSync(p, 'utf8'))
    const w = j.designFrameSize && Number(j.designFrameSize.width)
    if (Number.isFinite(w) && w > 0) return w
  } catch (_) {
    /* fallthrough */
  }
  return 750
}

/** 按 layoutMark 在 marked DSL 树中反查原始节点 */
function findRawNode(roots, layoutMark) {
  if (!layoutMark) return null
  let found = null
  function walk(node) {
    if (found || !node) return
    if (node._layoutMark === layoutMark) {
      found = node
      return
    }
    for (const c of node.children || []) walk(c)
  }
  for (const r of roots || []) walk(r)
  return found
}

/** Resolve one marked DSL node to an absolute design-space bbox. */
function findNodeAbsoluteFrame(roots, layoutMark) {
  if (!layoutMark) return null
  let found = null
  function walk(node, parentX, parentY) {
    if (found || !node) return
    const bbox = getBBoxLocal(node)
    const x = parentX + bbox.x
    const y = parentY + bbox.y
    if (node._layoutMark === layoutMark) {
      found = {
        layoutMark,
        nodeId: node.id || null,
        name: node.name || '',
        x,
        y,
        width: bbox.w,
        height: bbox.h,
      }
      return
    }
    for (const child of node.children || []) walk(child, x, y)
  }
  for (const root of roots || []) walk(root, 0, 0)
  return found
}

/** Region materials are meaningful only against one unambiguous DSL subtree. */
function resolveDesignScopeFrame(roots, scopeMarks) {
  const marks = Array.isArray(scopeMarks)
    ? scopeMarks.map((mark) => String(mark || '').trim()).filter(Boolean)
    : []
  if (marks.length !== 1) {
    throw new Error(
      `region material 需要且只允许一个 design scope，当前收到 ${marks.length} 个`
    )
  }
  const frame = findNodeAbsoluteFrame(roots, marks[0])
  if (!frame) throw new Error(`design scope 未命中 marked DSL: ${marks[0]}`)
  if (!(frame.width > 0) || !(frame.height > 0)) {
    throw new Error(
      `design scope bbox 无效: ${marks[0]} (${frame.width}x${frame.height})`
    )
  }
  return frame
}

/**
 * Select the design coordinate frame used by DOM measurements. Full-page capture
 * keeps the page frame; region capture uses the scoped root width and origin.
 */
function resolveDesignMeasurementFrame(options = {}) {
  const fullWidth = Number(options.fullDesignFrameWidth || 0)
  if (!(fullWidth > 0)) throw new Error('fullDesignFrameWidth 必须大于 0')
  if (options.captureMode !== 'region') {
    return {
      mode: 'full-page',
      width: fullWidth,
      originX: 0,
      originY: 0,
      scope: null,
    }
  }
  const scope = resolveDesignScopeFrame(options.nodes, options.scopeMarks)
  return {
    mode: 'region',
    width: scope.width,
    originX: scope.x,
    originY: scope.y,
    scope,
  }
}

module.exports = {
  round1,
  parseArgs,
  readCaptureMeta,
  assertUsableCaptureMaterial,
  resolveMeasurementContext,
  resolveMeasurementViewportWidth,
  browserExecutableCandidates,
  guessChrome,
  loadPuppeteer,
  launchBrowser,
  readBrowserHandoff,
  browserTargetId,
  browserSessionId,
  openBrowserSession,
  closeBrowserSession,
  getBBoxLocal,
  getNodeText,
  resolveComponentMarks,
  loadMarkedDsl,
  loadDesignFrameWidth,
  findRawNode,
  findNodeAbsoluteFrame,
  resolveDesignScopeFrame,
  resolveDesignMeasurementFrame,
}
