#!/usr/bin/env node
/* eslint-disable no-console */
'use strict'

const fs = require('fs')
const path = require('path')

const {
  parseArgs,
  loadPuppeteer,
  openBrowserSession,
  closeBrowserSession,
} = require('./lib/d2c-dom-measure-core.cjs')
const {
  createBrowserIssueCollector,
  probeRuntimePage,
  classifyRuntimeHealth,
  sourceFingerprint,
  runtimeRepairSentinel,
} = require('./lib/d2c-runtime-health-core.cjs')

let appendUrlParams = (url) => url
let maskUrl = (url) => url
let applySessionState = async () => ({ applied: false })
try { ({ appendUrlParams, maskUrl, applySessionState } = require('./lib/d2c-url-params.cjs')) } catch { /* optional */ }

function resolveUrl(args) {
  const explicit = args.url && args.url !== true ? String(args.url) : process.env.D2C_CAPTURE_URL
  if (explicit) return appendUrlParams(explicit, args)
  const host = args.host && args.host !== true ? String(args.host) : process.env.D2C_DEV_HOST
  const port = args.port && args.port !== true ? String(args.port) : process.env.D2C_DEV_PORT
  const route = args.path && args.path !== true ? String(args.path) : ''
  if (route && /^https?:/i.test(route)) return appendUrlParams(route, args)
  if (host && port && route) {
    return appendUrlParams(`http://${host}:${port}${route.startsWith('/') ? '' : '/'}${route}`, args)
  }
  throw new Error('缺少 --url <真实项目 URL>；也可传 --host/--port/--path 或设置 D2C_CAPTURE_URL')
}

function resolveOutput(args) {
  if (args.out && args.out !== true) return path.resolve(String(args.out))
  if (args['page-dir'] && args['page-dir'] !== true) {
    return path.resolve(String(args['page-dir']), 'compile-check.json')
  }
  throw new Error('缺少 --out <compile-check.json> 或 --page-dir d-d2c/temp/<page-dir>')
}

function writeJsonAtomic(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  const temp = `${file}.tmp-${process.pid}`
  fs.writeFileSync(temp, `${JSON.stringify(value, null, 2)}\n`, 'utf8')
  fs.renameSync(temp, file)
}

function pathRedirected(requestedUrl, finalUrl) {
  try { return new URL(requestedUrl).pathname !== new URL(finalUrl).pathname }
  catch { return false }
}

function buildResult(options) {
  const health = classifyRuntimeHealth({
    requestedUrl: options.requestedUrl,
    finalUrl: options.finalUrl,
    redirected: options.redirected,
    domProbe: options.domProbe,
    collector: options.collector,
  })
  return {
    schemaVersion: 1,
    checkedAt: new Date().toISOString(),
    checkedBy: 'd2c-runtime-health-check',
    requestedUrl: maskUrl(options.requestedUrl),
    finalUrl: maskUrl(options.finalUrl || options.requestedUrl),
    ...health,
    sourceFingerprint: sourceFingerprint(process.cwd()),
    domProbe: options.domProbe,
    browserIssues: {
      consoleErrorCount: Number(options.collector.consoleErrorCount || 0),
      consoleErrors: options.collector.consoleErrors || [],
      pageErrorCount: Number(options.collector.pageErrorCount || 0),
      pageErrors: options.collector.pageErrors || [],
      navigationResponses: options.collector.navigationResponses || [],
      navigationFailures: options.collector.navigationFailures || [],
    },
  }
}

async function main() {
  const args = parseArgs(process.argv)
  const url = resolveUrl(args)
  const out = resolveOutput(args)
  const width = parseInt(String(args.width || args['viewport-width'] || '678'), 10)
  const height = parseInt(String(args.height || args['viewport-height'] || '900'), 10)
  const dpr = parseFloat(String(args.dpr || '2'))
  const waitMs = parseInt(String(args.wait || args['wait-ms'] || '4000'), 10)
  const appSelector = args['app-selector'] && args['app-selector'] !== true
    ? String(args['app-selector'])
    : '#app'
  const { chrome, puppeteer } = loadPuppeteer()
  const issues = createBrowserIssueCollector(maskUrl)
  let session = null
  let finalUrl = url
  let domProbe = null

  try {
    session = await openBrowserSession({
      puppeteer,
      chrome,
      args,
      url,
      viewport: { width, height, deviceScaleFactor: dpr },
      waitMs,
      applySessionState,
      onPage: async (page) => issues.attach(page),
    })
    finalUrl = session.page.url()
    domProbe = await probeRuntimePage(session.page, { appSelector })
  } catch (error) {
    issues.state.navigationFailures.push(maskUrl(error && error.message ? error.message : String(error)))
    domProbe = {
      probeError: maskUrl(error && error.message ? error.message : String(error)),
      appRootPresent: false,
      appMounted: false,
      appChildElementCount: null,
      bodyTextLength: 0,
      overlayPresent: false,
      textSignals: [],
    }
  } finally {
    if (session) await closeBrowserSession(session)
  }

  const result = buildResult({
    requestedUrl: url,
    finalUrl,
    redirected: pathRedirected(url, finalUrl),
    domProbe,
    collector: issues.snapshot(),
  })
  writeJsonAtomic(out, result)
  console.log(JSON.stringify({ ...result, output: out }, null, 2))
  if (result.status !== 'passed') {
    console.error(runtimeRepairSentinel(result))
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
  resolveUrl,
  resolveOutput,
  pathRedirected,
  buildResult,
}
