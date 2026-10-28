#!/usr/bin/env node
/* eslint-disable no-console */
/**
 * 用本机 Chrome（puppeteer-core）打开已运行的 Nuxt 页面并导出 PNG，供叠图脚本 --actual 使用。
 *
 * 前置：先在本机启动工程并确认真实可访问 URL，再执行本脚本。
 * 推荐传 `--url <完整URL>`；若只传 `--path`，必须同时传 `--host` 与 `--port`
 * 或设置 D2C_DEV_HOST / D2C_DEV_PORT。
 *
 * Chrome 路径：优先读 `PUPPETEER_EXECUTABLE_PATH` 或 `CHROME_PATH`，否则在常见路径探测。
 *
 * 与 `--design` + `--label` 联用时，输出形如 `<label>-<design-md5>-actual.png`。
 * 文件名里的短 md5 只用于关联同一设计稿；actual 内容哈希见 capture-meta.json。
 * `--page-id` 可作为 `--label` 的兼容别名，避免模型误传参数导致轮次目录被写成文件。
 */
const fs = require('fs')
const path = require('path')
const crypto = require('crypto')
const {
  loadPuppeteer,
  openBrowserSession,
  closeBrowserSession,
} = require('./lib/d2c-dom-measure-core.cjs')
const {
  createBrowserIssueCollector,
  probeRuntimePage,
  classifyRuntimeHealth,
  previewHarnessExcludedFiles,
  sourceFingerprint,
  runtimeRepairSentinel,
} = require('./lib/d2c-runtime-health-core.cjs')
const {
  normalizeWorkflowMode,
  requirePreviewWorkflowMode,
} = require('./lib/d2c-workflow-mode-policy.cjs')
// 结构化运行日志（非侵入、可降级）
let getLoggerForArtifact = () => ({ enabled: false })
try { ({ getLoggerForArtifact } = require('./lib/d2c-run-log.cjs')) } catch { /* 静默降级 */ }
// 登录态/额外 URL query 注入（--url-params 或 D2C_URL_PARAMS）
// 会话态注入（cookie/localStorage，--session-state 或 D2C_SESSION_STATE）：flow-gated SPA 常不认 URL token
let appendUrlParams = (u) => u
let maskUrl = (u) => u
let applySessionState = async () => ({ applied: false })
try { ({ appendUrlParams, maskUrl, applySessionState } = require('./lib/d2c-url-params.cjs')) } catch { /* 静默降级 */ }

function fileDigest(filePath, algorithm = 'sha256', length = 0) {
  const abs = path.resolve(filePath)
  const digest = crypto.createHash(algorithm).update(fs.readFileSync(abs)).digest('hex')
  return length > 0 ? digest.slice(0, length) : digest
}

function designMd5(designPath) {
  return fileDigest(designPath, 'md5', 12)
}

function buildCaptureHashes(designPath, actualPath) {
  return {
    designMd5: designPath ? designMd5(designPath) : null,
    actualMd5: fileDigest(actualPath, 'md5', 12),
    actualSha256: fileDigest(actualPath, 'sha256'),
  }
}

function readPngDimensions(imagePath) {
  try {
    const abs = path.resolve(imagePath)
    const buf = Buffer.alloc(24)
    const fd = fs.openSync(abs, 'r')
    try {
      const bytes = fs.readSync(fd, buf, 0, buf.length, 0)
      const isPng =
        bytes >= 24 &&
        buf[0] === 0x89 &&
        buf[1] === 0x50 &&
        buf[2] === 0x4e &&
        buf[3] === 0x47 &&
        buf[4] === 0x0d &&
        buf[5] === 0x0a &&
        buf[6] === 0x1a &&
        buf[7] === 0x0a
      if (!isPng) return null
      return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) }
    } finally {
      fs.closeSync(fd)
    }
  } catch (_) {
    return null
  }
}

function sanitizeArtifactLabel(raw) {
  if (raw === undefined || raw === null || raw === true) return ''
  const s = String(raw).trim()
  if (!s) return ''
  return s
    .replace(/[/\\:*?"<>|]/g, '-')
    .replace(/\s+/g, '-')
    .replace(/\.{2,}/g, '-')
    .replace(/-+/g, '-')
    .replace(/^[-_.]+|[-_.]+$/g, '')
}

function getArtifactInfix(args) {
  const fromArg = args['artifact-infix'] || args.infix
  if (fromArg && fromArg !== true) {
    const s = sanitizeArtifactLabel(fromArg)
    if (s) return s
  }
  return ''
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
  args.overlayOnly = Boolean(args['overlay-only'] || process.env.D2C_OVERLAY_ONLY)
  if (args['overlay-only'] === 'false' || args['overlay-only'] === '0') args.overlayOnly = false
  args.mock = null
  if (args.mock && args.mock !== true) {
    try { args.mock = JSON.parse(args.mock) } catch (_) { args.mock = null }
  }
  args.mockScript = args['mock-script'] && args['mock-script'] !== true ? args['mock-script'] : null
  return args
}

function normalizeAccessMode(raw) {
  const mode = String(raw || 'real-route').trim()
  if (!mode || mode === 'real-route') return 'real-route'
  if (mode === 'preview-harness') return 'vue-route-preview'
  if (mode === 'vue-route-preview') return 'vue-route-preview'
  return mode
}

async function captureDomProbe(page, appSelector) {
  try {
    return await page.evaluate((appSel) => {
      const selectors = Array.from(new Set([
        appSel,
        '#app',
        '#root',
        '[data-v-app]',
        '[data-server-rendered]',
        '#__nuxt',
        '#__next',
      ].filter(Boolean)))
      const app = selectors.reduce((found, sel) => found || document.querySelector(sel), null)
      const markRoot = document.querySelector('[data-d2c-mark="L0"]')
      const componentRoot = document.querySelector('[data-d2c-component]')
      const d2cRoot = markRoot || componentRoot
      const scripts = Array.from(document.scripts || [])
      const srcs = scripts.map((s) => s.src || '').filter(Boolean)
      const hasModuleScript = scripts.some((s) => s.type === 'module') || srcs.some((src) => /\/(assets|static|js)\//i.test(src))
      const hasVueRuntimeOnlyScript = srcs.some((src) => /vue\.runtime/i.test(src))
      const bodyText = document.body && document.body.innerText ? document.body.innerText.trim() : ''
      // 运行时状态只认运行时写法：模板预置的 data-d2c-template-* 是构建期事实，
      // 入口模块没执行时它依然存在，不能当成「预览已生效」的证据。
      const templatePreviewMode =
        document.documentElement.getAttribute('data-d2c-template-preview-mode') || null
      const previewSlug =
        document.documentElement.getAttribute('data-d2c-template-preview-slug') || null
      let previewModeSource = null
      let previewMode = null
      if (window.__D2C_PREVIEW_MODE__) {
        previewMode = window.__D2C_PREVIEW_MODE__
        previewModeSource = 'window.__D2C_PREVIEW_MODE__'
      } else if (document.documentElement.getAttribute('data-d2c-preview-mode')) {
        previewMode = document.documentElement.getAttribute('data-d2c-preview-mode')
        previewModeSource = 'html[data-d2c-preview-mode]'
      } else if (document.body && document.body.getAttribute('data-d2c-preview-mode')) {
        previewMode = document.body.getAttribute('data-d2c-preview-mode')
        previewModeSource = 'body[data-d2c-preview-mode]'
      }
      const previewEntryExecuted =
        Boolean(window.__D2C_PREVIEW_ENTRY_EXECUTED__) ||
        document.documentElement.getAttribute('data-d2c-preview-entry-executed') === 'true'
      const previewEntryError =
        window.__D2C_PREVIEW_ENTRY_ERROR__ ||
        document.documentElement.getAttribute('data-d2c-preview-entry-error') ||
        null
      // 入口在非预览页会被自身守卫跳过（项目 HtmlWebpackPlugin 未配 chunks 时会把它注入主应用 HTML）。
      // 这是「截错了页面」，不是「入口没执行」，必须区分开。
      const previewEntrySkipped = window.__D2C_PREVIEW_ENTRY_SKIPPED__ || null
      // Vue 2 的 $mount("#app") 会用渲染根替换 #app，之后再查 #app 恒为 null；
      // 兜底按 L0 标记（隔离入口渲染根）取，避免节点数恒 0 被读成「没挂上」。
      const mountRoot = document.querySelector('[data-d2c-mark="L0"]') || app
      const mountedNodeCount = Number.isFinite(window.__D2C_PREVIEW_MOUNTED_NODES__)
        ? window.__D2C_PREVIEW_MOUNTED_NODES__
        : (mountRoot ? mountRoot.querySelectorAll('*').length : null)
      const previewMountTag =
        window.__D2C_PREVIEW_MOUNT_TAG__ ||
        (mountRoot ? mountRoot.tagName : null)
      // 归因用：哪些 <script> 指向隔离入口（splitChunks 会把入口拆成多个 chunk）。
      const previewEntryScriptSrcs = previewSlug
        ? srcs.filter((src) => src.includes(previewSlug) || src.includes(previewSlug.replace(/-/g, '_')))
        : []
      const hasPreviewBootstrapBypass =
        Boolean(window.__D2C_PREVIEW_BOOTSTRAP_BYPASS__) || document.documentElement.getAttribute('data-d2c-preview-bootstrap-bypass') === 'true'
      return {
        hasD2cRoot: Boolean(d2cRoot),
        hasD2cMarkRoot: Boolean(markRoot),
        hasD2cComponent: Boolean(componentRoot),
        d2cRootTagName: d2cRoot ? d2cRoot.tagName : null,
        appChildElementCount: app ? app.childElementCount : null,
        bodyTextLength: bodyText.length,
        hasModuleScript,
        hasVueRuntimeOnlyScript,
        scriptSrcs: srcs,
        likelyStaticHtmlReplay: hasVueRuntimeOnlyScript && !hasModuleScript,
        previewMode,
        previewModeSource,
        templatePreviewMode,
        previewSlug,
        previewEntryExecuted,
        previewEntryError,
        previewEntrySkipped,
        previewEntryScriptSrcs,
        mountedNodeCount,
        previewMountTag,
        hasPreviewBootstrapBypass,
      }
    })
  } catch (err) {
    return {
      probeError: err && err.message ? err.message : String(err),
    }
  }
}

function parseNonNegativeFloat(value, fallback = 0) {
  const n = parseFloat(String(value))
  return Number.isFinite(n) && n >= 0 ? n : fallback
}

function round2(n) {
  return Math.round(Number(n || 0) * 100) / 100
}

function parseViewSize(raw) {
  if (!raw || raw === true) return null
  const match = String(raw).trim().match(/^(\d+(?:\.\d+)?)\s*[,xX]\s*(\d+(?:\.\d+)?)$/)
  if (!match) return null
  const width = parseInt(match[1], 10)
  const height = parseInt(match[2], 10)
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return null
  return { width, height }
}

/**
 * 解析 CSS 简写形式的截图留白（1~4 个值，语义同 CSS padding）。
 * 留白只影响**截图范围**，绝不能进入测量基准 bbox —— 否则视觉留白会被当成
 * 设计尺寸的一部分，导致 region 模式的量化比对整体偏移。
 */
function parseCapturePadding(raw) {
  const values = String(raw == null || raw === true ? '0' : raw)
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 4)
    .map((value) => parseNonNegativeFloat(value, 0))
  const [a = 0, b = a, c = a, d = b] = values
  if (values.length === 1) return { top: a, right: a, bottom: a, left: a }
  if (values.length === 2) return { top: a, right: b, bottom: a, left: b }
  if (values.length === 3) return { top: a, right: b, bottom: c, left: b }
  return { top: a, right: b, bottom: c, left: d }
}

/**
 * 由元素 rect 与留白推导截图几何。
 * - `clip`：实际截图范围（含留白，坐标夹到文档左上角且宽高至少 1px）
 * - `bboxCss` / `documentBBoxCss`：**测量基准**，始终是元素自身 bbox，不含留白
 */
function buildCaptureRegionGeometry(rect, paddingCss) {
  const pad = parseCapturePadding(paddingCss)
  const documentLeft = rect.left + rect.scrollX
  const documentTop = rect.top + rect.scrollY
  const clipLeft = Math.max(0, documentLeft - pad.left)
  const clipTop = Math.max(0, documentTop - pad.top)
  const clipRight = Math.max(clipLeft + 1, documentLeft + rect.width + pad.right)
  const clipBottom = Math.max(clipTop + 1, documentTop + rect.height + pad.bottom)
  const clip = {
    x: clipLeft,
    y: clipTop,
    width: clipRight - clipLeft,
    height: clipBottom - clipTop,
  }
  const bboxCss = {
    left: round2(rect.left),
    top: round2(rect.top),
    width: round2(rect.width),
    height: round2(rect.height),
    right: round2(rect.right),
    bottom: round2(rect.bottom),
  }
  const documentBBoxCss = {
    left: round2(documentLeft),
    top: round2(documentTop),
    width: round2(rect.width),
    height: round2(rect.height),
    right: round2(documentLeft + rect.width),
    bottom: round2(documentTop + rect.height),
  }
  return { paddingCss: pad, clip, bboxCss, documentBBoxCss }
}

async function resolveCaptureRegion(page, selector, paddingCss) {
  const handle = await page.$(selector)
  if (!handle) {
    throw new Error(`capture selector 未命中 DOM: ${selector}`)
  }
  const rect = await page.evaluate((el) => {
    const r = el.getBoundingClientRect()
    return {
      left: r.left,
      top: r.top,
      right: r.right,
      bottom: r.bottom,
      width: r.width,
      height: r.height,
      scrollX: window.scrollX || window.pageXOffset || 0,
      scrollY: window.scrollY || window.pageYOffset || 0,
    }
  }, handle)
  if (!rect || !Number.isFinite(rect.width) || !Number.isFinite(rect.height) || rect.width <= 0 || rect.height <= 0) {
    throw new Error(`capture selector 命中元素不可截图或尺寸为 0: ${selector}`)
  }
  const geometry = buildCaptureRegionGeometry(rect, paddingCss)
  return { selector, ...geometry }
}

async function main() {
  const args = parseArgs(process.argv)
  const appSelector =
    args['app-selector'] && args['app-selector'] !== true
      ? args['app-selector']
      : process.env.D2C_APP_SELECTOR || null
  const explicitUrl =
    args.url && args.url !== true ? args.url : process.env.D2C_CAPTURE_URL
  const host =
    args.host && args.host !== true ? args.host : process.env.D2C_DEV_HOST
  const port =
    args.port && args.port !== true ? args.port : process.env.D2C_DEV_PORT
  const routePath = args.path && args.path !== true ? args.path : ''

  let url = explicitUrl
  if (!url && routePath && routePath.startsWith('http')) {
    url = routePath
  } else if (!url && routePath && host && port) {
    url = `http://${host}:${port}${routePath.startsWith('/') ? '' : '/'}${routePath}`
  }

  if (!url) {
    console.error(
      [
        '缺少截图 URL。',
        '推荐传完整 URL：',
        '  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-capture-page.cjs --url <full-url> --design <image-source.png> --label <page-id> --out <round-dir>/ --dpr 2',
        '若只传 --path，则必须同时传 --host 与 --port，或设置 D2C_DEV_HOST / D2C_DEV_PORT。',
        '也可用环境变量 D2C_CAPTURE_URL 指定完整 URL。',
      ].join('\n')
    )
    process.exit(1)
  }

  // 注入登录态等额外 query 参数（需登录的项目靠它过鉴权页），日志中脱敏打印
  url = appendUrlParams(url, args)
  console.log(`截图 URL: ${maskUrl(url)}`)

  const outArg = args.out && args.out !== true ? String(args.out) : 'd-d2c/temp/capture-actual.png'
  const outAbs = path.resolve(outArg)
  const outLooksLikeImage = /\.(png|jpe?g|webp)$/i.test(outArg)
  const outputLabel = sanitizeArtifactLabel(
    args.label || args['page-id'] || args.pageId || args.name || args.slug
  )
  const outIsDir =
    outArg.endsWith('/') ||
    outArg.endsWith(path.sep) ||
    (fs.existsSync(outAbs) && fs.statSync(outAbs).isDirectory()) ||
    (!outLooksLikeImage && outputLabel)
  let outPath = outAbs
  let md5 = null
  if (args.design) {
    md5 = designMd5(args.design)
    const infix = getArtifactInfix(args)
    const mid = infix ? `${infix}-` : ''
    if (outputLabel && args.round) {
      const dir = outIsDir ? outPath : path.dirname(outPath)
      outPath = path.resolve(dir, `${outputLabel}-${md5}-${mid}r${args.round}-actual.png`)
    } else if (outputLabel) {
      const dir = outIsDir ? outPath : path.dirname(outPath)
      outPath = path.resolve(dir, `${outputLabel}-${md5}-${mid}actual.png`)
    }
  }
  if (outIsDir && outPath === outAbs) {
    outPath = path.resolve(outAbs, 'actual.png')
  }
  const viewSize = parseViewSize(args['view-size'] || args.viewSize)
  const designSize = args.design ? readPngDimensions(args.design) : null
  let width = parseInt(
    String(args.width || args['viewport-width'] || args.viewportWidth || (viewSize && viewSize.width) || (designSize && designSize.width) || '678'),
    10
  )
  let height = parseInt(
    String(args.height || args['viewport-height'] || args.viewportHeight || (viewSize && viewSize.height) || (designSize && designSize.height) || '900'),
    10
  )
  const waitMs = parseInt(String(args.wait || args['wait-for'] || args.waitFor || args['wait-ms'] || args.waitMs || '4000'), 10)
  let dpr = parseFloat(String(args.dpr || '2'), 10)
  const fullPage =
    args['full-page'] === 'false' || args.fullPage === 'false' ? false : true
  const captureSelector =
    (args['capture-selector'] && args['capture-selector'] !== true ? String(args['capture-selector']) : null) ||
    (args['region-selector'] && args['region-selector'] !== true ? String(args['region-selector']) : null)
  const designScope =
    (args['design-scope'] && args['design-scope'] !== true ? String(args['design-scope']) : null) ||
    (args.scope && args.scope !== true ? String(args.scope) : null) ||
    (process.env.D2C_DESIGN_SCOPE ? String(process.env.D2C_DESIGN_SCOPE) : null)
  const capturePaddingCss =
    args['capture-padding'] && args['capture-padding'] !== true ? args['capture-padding'] : '0'
  const accessModeRaw =
    (args['access-mode'] && args['access-mode'] !== true ? String(args['access-mode']) : process.env.D2C_ACCESS_MODE) ||
    'real-route'
  const accessMode = normalizeAccessMode(accessModeRaw)
  if (!new Set(['real-route', 'vue-route-preview', 'user-browser-handoff']).has(accessMode)) {
    console.error(
      `不支持的截图入口 accessMode=${accessModeRaw}。d-d2c 只允许 real-route、vue-route-preview 或 user-browser-handoff；禁止把静态快照或 DOM replay 作为物料来源。`
    )
    process.exit(1)
  }
  const workflowMode = normalizeWorkflowMode(
    args['workflow-mode'] || args.mode || process.env.D2C_WORKFLOW_MODE
  )
  if (accessMode === 'vue-route-preview') {
    try {
      requirePreviewWorkflowMode(workflowMode, 'd2c-capture-page vue-route-preview')
    } catch (error) {
      console.error(`❌ ${error.message}`)
      process.exit(2)
    }
  }
  const targetUrl =
    (args['target-url'] && args['target-url'] !== true ? String(args['target-url']) : process.env.D2C_TARGET_URL) ||
    url
  const metaOut = args['meta-out'] && args['meta-out'] !== true ? path.resolve(args['meta-out']) : null

  fs.mkdirSync(path.dirname(outPath), { recursive: true })

  // === Mock 数据预加载（必须在 page.goto 之前通过 evaluateOnNewDocument 注入） ===
  // 原来在 page.goto 之后用 page.evaluate 注入，对"挂载即 fetch"的 Vue App 太晚：
  // 首屏数据请求在 goto 完成时已发出，mock 拦截不到 → 组件渲染为空。
  const mockPreloads = []
  if (args.mock) {
    mockPreloads.push({ type: 'json', data: args.mock })
  }
  if (args.mockScript) {
    const mockCode = require('fs').readFileSync(args.mockScript, 'utf8')
    mockPreloads.push({ type: 'script', code: mockCode, path: args.mockScript })
  }

  const { chrome, puppeteer } = loadPuppeteer()
  const browserIssues = createBrowserIssueCollector(maskUrl)
  const session = await openBrowserSession({
    puppeteer,
    chrome,
    args,
    url,
    viewport: { width, height, deviceScaleFactor: dpr },
    waitMs,
    applySessionState,
    onPage: async (page) => {
      browserIssues.attach(page)
      // 在 page.goto 之前注册 mock，抢在 App 脚本执行前注入
      for (const preload of mockPreloads) {
        if (preload.type === 'json') {
          await page.evaluateOnNewDocument((mock) => { window.__MOCK__ = mock }, preload.data)
          console.log('🧪 已注册 window.__MOCK__（evaluateOnNewDocument）')
        } else {
          await page.evaluateOnNewDocument(preload.code)
          console.log('🧪 已注册 mock 脚本: ' + preload.path + '（evaluateOnNewDocument）')
        }
      }
    },
  })
  try {
    const page = session.page
    if (session.attached) {
      const liveViewport = await page.evaluate(() => ({
        width: document.documentElement.clientWidth || window.innerWidth,
        height: window.innerHeight,
        dpr: window.devicePixelRatio || 1,
      }))
      if (Number(liveViewport.width) > 0) width = Number(liveViewport.width)
      if (Number(liveViewport.height) > 0) height = Number(liveViewport.height)
      if (Number(liveViewport.dpr) > 0) dpr = Number(liveViewport.dpr)
    }

    // （mock 注入已移至 openBrowserSession 的 onPage 回调，
    //  通过 evaluateOnNewDocument 在 page.goto 前执行）

    // 目标页落地校验（通用，换任何页面都成立）：SPA 的路由守卫 / 鉴权 Flow / init-data 首屏决策
    // 可能在导航后把落地路由改写到另一个业务页（首页/登录页等）。此时盲目截图会让后续几何自检
    // 与还原度评测整条跑在「错页」上，得到无意义的极低分且掩盖真实失败模式（“没落地到目标页”被
    // 误读成“还原度低”）。这里记录导航稳定后的最终 URL 并与请求 URL 的 path 比对，不一致即判定
    // 发生重定向，输出 redirected 标志与告警，供工作流「目标页落地校验闸」在评测前拦截。
    let finalUrl = url
    let redirected = false
    try {
      finalUrl = page.url()
      const reqPath = new URL(url).pathname
      const finPath = new URL(finalUrl).pathname
      redirected = reqPath !== finPath
      if (redirected) {
        console.error(
          `⚠️ 疑似 SPA 重定向：请求路径 ${reqPath} → 最终路径 ${finPath}。` +
            '落地页可能不是目标业务页（常见于路由守卫 / 鉴权 Flow / init-data 首屏改写或登录态缺失）。' +
            '进入评测前必须核对：是否需要等待跳转完成、注入已有登录态或修正路由；勿对错页截图评测。'
        )
      }
    } catch (_) {
      /* 最终 URL 解析失败不阻断截图，仅跳过落地判定 */
    }

    const d2cDomProbe = await captureDomProbe(page, appSelector)
    const runtimeDomProbe = await probeRuntimePage(page, { appSelector })
    const domProbe = { ...d2cDomProbe, ...runtimeDomProbe }
    const browserIssueSnapshot = browserIssues.snapshot()
    const runtimeHealth = classifyRuntimeHealth({
      requestedUrl: url,
      finalUrl,
      redirected,
      domProbe,
      collector: browserIssueSnapshot,
    })
    const captureSourceFingerprint = sourceFingerprint(
      process.cwd(),
      [],
      previewHarnessExcludedFiles(metaOut || outPath, process.cwd())
    )
    const captureRegion = runtimeHealth.status === 'passed' && captureSelector
      ? await resolveCaptureRegion(page, captureSelector, capturePaddingCss)
      : null

    await page.screenshot({
      path: outPath,
      type: 'png',
      ...(captureRegion ? { clip: captureRegion.clip } : { fullPage }),
    })

    // 宽度钳制：fullPage 截图宽度 = 页面 scrollWidth；页面横向溢出时会大于设计视口宽，
    // 进而让叠图脚本（scale = actualW / designW）把设计稿纵向也等比放大，产生「假的累积纵向漂移」。
    // 这里把截图裁回设计视口宽（×dpr），并对横向溢出告警（溢出本身也是真机会横向滚动的真 bug）。
    let renderedScrollWidthCss = width
    let horizontalOverflowCss = 0
    const targetPx = Math.round((captureRegion ? captureRegion.bboxCss.width : width) * dpr)
    let outputWidthPx = targetPx
    let widthClamped = false
    try {
      const metrics = await page.evaluate(() => ({
        scrollWidth: Math.ceil(document.documentElement.scrollWidth),
      }))
      renderedScrollWidthCss = metrics.scrollWidth
      horizontalOverflowCss = Math.max(0, renderedScrollWidthCss - width)

      const { readPng, writePng, cropRgbaWidth } = require('./lib/d2c-visual-core.cjs')
      const { PNG } = require('pngjs')
      const src = readPng(outPath)
      outputWidthPx = src.width
      if (!captureRegion && src.width > targetPx + 2) {
        const cropped = cropRgbaWidth(src.data, src.width, src.height, targetPx)
        const out = new PNG({ width: cropped.width, height: cropped.height })
        cropped.data.copy(out.data)
        writePng(outPath, out)
        outputWidthPx = cropped.width
        widthClamped = true
        console.error(
          `⚠️ 横向溢出 ${horizontalOverflowCss}px：scrollWidth=${renderedScrollWidthCss} > 视口 ${width}；` +
            `已将截图宽度钳制到 ${width}（×dpr=${targetPx}px），请修复页面溢出元素（如绝对定位越界、固定宽超视口）。`
        )
      }
    } catch (e) {
      console.error(
        `⚠️ 宽度钳制步骤跳过（${e && e.message ? e.message : e}），截图按原始宽度输出。`
      )
    }

    // 结构化日志：截图产物（page 目录由产物路径回溯）
    try {
      const clog = getLoggerForArtifact(outPath)
      if (clog.enabled) { clog.scriptComplete('d2c-capture-page', { url }); clog.artifactWrite(outPath) }
    } catch { /* 日志失败不影响主流程 */ }

    // === overlay-only 模式：截图+叠图，跳过所有 checkpoint ===
    if (args.overlayOnly) {
      console.log('⚡ overlay-only 模式：截图 → 叠图 → 输出')
      const designPath = args.design
      if (designPath && fs.existsSync(designPath)) {
        try {
          const { buildBlendOverlayPng, writePng, loadAlignedPair } = require('./lib/d2c-visual-core.cjs')
          const overlayOut = outAbs.replace(/\.(png|jpe?g|webp)$/i, '-overlay.png')
          const pair = await loadAlignedPair(designPath, outAbs)
          const overlay = await buildBlendOverlayPng(pair.design, pair.actual, 0.5)
          await writePng(overlayOut, overlay)
          console.log('叠图输出: ' + overlayOut)
        } catch (e) {
          console.log('⚠️  叠图失败: ' + (e && e.message ? e.message : e))
        }
      } else {
        console.log('⚠️  未传 --design，仅截图，不叠图')
      }
      console.log('✅ overlay-only 完成（未写入 checkpoint）')
      try {
        const clog = getLoggerForArtifact(outPath)
        if (clog.enabled) { clog.scriptComplete('d2c-capture-page', { mode: 'overlay-only' }) }
      } catch (_) {}
      await closeBrowserSession(session)
      return
    }

    let invalidCaptureReason = ''
    let failureClass = null
    let askUser = false
    if (runtimeHealth.status !== 'passed') {
      failureClass = runtimeHealth.failureClass || 'runtime-error'
      invalidCaptureReason =
        `真实项目运行时健康检查失败（failureClass=${failureClass}，` +
        `signals=${runtimeHealth.signals.map((item) => item.code).join(',') || 'none'}）；` +
        '必须由 executor 修复编译/runtime/dev-server 路由后覆盖重采本轮，禁止发起 browser handoff。'
    } else if (accessMode === 'vue-route-preview') {
      if (redirected) {
        invalidCaptureReason =
          `vue-route-preview 自身发生重定向：requested=${maskUrl(url)} final=${maskUrl(finalUrl)}；preview 入口仍被业务 bootstrap/init-data/router guard 带走。请改用 d2c-isolated-vue-preview.cjs 生成独立 Vue entry。`
      } else if (domProbe && domProbe.probeError) {
        invalidCaptureReason = `vue-route-preview DOM probe 失败：${domProbe.probeError}`
      } else if (!domProbe || (!domProbe.hasD2cRoot && !domProbe.hasD2cComponent)) {
        invalidCaptureReason =
          'vue-route-preview 未命中目标 Vue DOM：domProbe.hasD2cRoot/hasD2cComponent 均为 false；请修复 isolated Vue entry/wrapper 后重跑。'
      }
    } else if (accessMode === 'user-browser-handoff') {
      if (!session.attached) {
        invalidCaptureReason = 'user-browser-handoff 未连接托管 CDP 浏览器，禁止退化为新启动的无状态 Chrome。'
      } else if (!session.handoff || !session.handoff.verification || !session.handoff.verification.ok) {
        invalidCaptureReason = 'user-browser-handoff 缺少通过的目标页验证证据。'
      } else if (redirected) {
        invalidCaptureReason = `user-browser-handoff 当前 tab 已离开目标路由：requested=${maskUrl(url)} final=${maskUrl(finalUrl)}`
      }
    }

    // 必须在截图宽度钳制完成后再计算 actual 哈希，确保哈希代表最终送评文件。
    const captureHashes = buildCaptureHashes(args.design && args.design !== true ? args.design : null, outPath)
    const result = {
      schemaVersion: 2,
      ok: true,
      accessMode,
      accessModeRaw,
      workflowMode: workflowMode || null,
      targetUrl: maskUrl(targetUrl),
      requestedUrl: maskUrl(url),
      finalUrl: maskUrl(finalUrl),
      redirected,
      previewRouteRedirected: accessMode === 'vue-route-preview' ? redirected : false,
      realRouteBlockedByRedirect: accessMode === 'real-route' && runtimeHealth.status === 'passed' ? redirected : false,
      browserSessionId: session.sessionId,
      browserTargetId: session.targetId,
      browserAttached: session.attached,
      handoffVerification: accessMode === 'user-browser-handoff' && session.handoff
        ? session.handoff.verification || null
        : null,
      domProbe,
      runtimeHealth,
      failureClass,
      askUser,
      sourceFingerprint: captureSourceFingerprint,
      outPath,
      designMd5: captureHashes.designMd5,
      actualMd5: captureHashes.actualMd5,
      actualSha256: captureHashes.actualSha256,
      hashSemantics: {
        filenameHash: 'designMd5',
        duplicateRenderHash: 'actualSha256',
      },
      viewport: { width, height, dpr, fullPage },
      browserConsoleErrorCount: browserIssueSnapshot.consoleErrorCount,
      browserConsoleErrors: browserIssueSnapshot.consoleErrors,
      browserPageErrorCount: browserIssueSnapshot.pageErrorCount,
      browserPageErrors: browserIssueSnapshot.pageErrors,
      viewportWidthCss: width,
      captureMode: captureRegion ? 'region' : 'full-page',
      captureSelector: captureRegion ? captureRegion.selector : null,
      designScope: captureRegion ? designScope : null,
      regionBBoxCss: captureRegion ? captureRegion.bboxCss : null,
      regionDocumentBBoxCss: captureRegion ? captureRegion.documentBBoxCss : null,
      regionClipCss: captureRegion ? captureRegion.clip : null,
      overlayBasis: captureRegion
        ? {
            mode: 'region-to-design-root',
            designScope: designScope || null,
            actualBBoxCss: captureRegion.bboxCss,
            note: 'actual 为 selector 局部截图；只能与同一 MasterGo 节点/根导出的设计图 0,0 对齐叠图。若设计图是整页，必须先裁出同一设计 bbox。',
          }
        : {
            mode: 'full-page-width-scale',
            note: 'actual 为整页/视口截图；design-30 与 overlay 按 actual 宽度等比缩放设计图。',
          },
      renderedScrollWidthCss,
      horizontalOverflowCss,
      outputWidthPx,
      widthClamped,
      waitMs,
      materialStatus: invalidCaptureReason ? 'invalid' : 'ok',
      invalidReason: invalidCaptureReason || null,
    }
    if (metaOut) {
      fs.mkdirSync(path.dirname(metaOut), { recursive: true })
      fs.writeFileSync(metaOut, JSON.stringify(result, null, 2), 'utf8')
    }
    console.log(JSON.stringify(result, null, 2))
    if (invalidCaptureReason) {
      console.error(`❌ ${invalidCaptureReason}`)
      if (runtimeHealth.status !== 'passed') console.error(runtimeRepairSentinel(runtimeHealth))
      process.exitCode = 2
    }
  } finally {
    await closeBrowserSession(session)
  }
}

if (require.main === module) {
  main().catch((err) => {
    console.error(err)
    process.exit(1)
  })
}

module.exports = {
  parseCapturePadding,
  buildCaptureRegionGeometry,
  buildCaptureHashes,
  designMd5,
  fileDigest,
}
