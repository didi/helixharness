#!/usr/bin/env node
'use strict'

const assert = require('assert')
const fs = require('fs')
const os = require('os')
const path = require('path')

const {
  requestHandoff,
  readState,
  writeState,
  prepareLaunchState,
} = require('../d2c-browser-handoff.cjs')
const {
  browserExecutableCandidates,
  guessChrome,
  openBrowserSession,
  closeBrowserSession,
} = require('../lib/d2c-dom-measure-core.cjs')
const {
  validateUserBrowserHandoff,
} = require('../d2c-workflow-checkpoint.cjs')
const {
  sourceFingerprint,
} = require('../lib/d2c-runtime-health-core.cjs')

function tempDir(name) {
  return fs.mkdtempSync(path.join(os.tmpdir(), `${name}-`))
}

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`, 'utf8')
}

function verifiedState(overrides = {}) {
  return {
    version: 1,
    mode: 'update-page',
    status: 'verified',
    active: true,
    targetUrl: 'http://127.0.0.1:8068/heranew/loan?token=***',
    targetPath: '/heranew/loan',
    expectedSelector: '[data-d2c-component="banner"]',
    cdpEndpoint: 'http://127.0.0.1:9333',
    browserTargetId: 'target-1',
    browserSessionId: 'session-1',
    verification: {
      ok: true,
      finalPath: '/heranew/loan',
      selector: '[data-d2c-component="banner"]',
      selectorMatched: true,
      selectorVisible: true,
    },
    ...overrides,
  }
}

function testRequestRequiresRedirectAndMasksUrl() {
  const pageDir = tempDir('d2c-handoff-request')
  const captureMeta = path.join(pageDir, 'r1', 'capture-meta.json')
  const healthyCapture = {
    accessMode: 'real-route',
    redirected: true,
    realRouteBlockedByRedirect: true,
    materialStatus: 'ok',
    runtimeHealth: { status: 'passed', askUser: false },
    sourceFingerprint: sourceFingerprint(process.cwd()),
  }
  writeJson(captureMeta, healthyCapture)
  requestHandoff({
    pageDir,
    targetUrl: 'http://127.0.0.1:8068/heranew/loan?token=secret-token&foo=bar',
    expectedSelector: '[data-d2c-component="banner"]',
    captureMetaPath: captureMeta,
  })
  const state = readState(pageDir)
  assert.strictEqual(state.status, 'requested')
  assert.strictEqual(state.targetPath, '/heranew/loan')
  assert.strictEqual(state.targetRoute, '/heranew/loan?foo=bar')
  assert.strictEqual(state.expectedSelector, '[data-d2c-component="banner"]')
  assert.ok(!JSON.stringify(state).includes('secret-token'))

  const noRedirect = path.join(pageDir, 'r2', 'capture-meta.json')
  writeJson(noRedirect, { ...healthyCapture, redirected: false, realRouteBlockedByRedirect: false })
  assert.throws(() => requestHandoff({ pageDir, captureMetaPath: noRedirect }), /未证明.*真实路由发生重定向/)

  const unhealthy = path.join(pageDir, 'r3', 'capture-meta.json')
  writeJson(unhealthy, {
    ...healthyCapture,
    runtimeHealth: { status: 'failed', failureClass: 'compile-error', askUser: false },
    materialStatus: 'invalid',
    invalidReason: 'Errors while compiling',
  })
  assert.throws(
    () => requestHandoff({ pageDir, captureMetaPath: unhealthy }),
    /D2C_COMPILE_REPAIR_REQUIRED/
  )
}

function testAbortedAndExpiredHandoffsAreRestartable() {
  for (const status of ['aborted', 'expired']) {
    const restarted = prepareLaunchState(verifiedState({
      status,
      active: false,
      cleanedAt: '2026-07-13T00:00:00.000Z',
      profileDir: '/tmp/old-profile',
    }))
    assert.strictEqual(restarted.status, 'requested')
    assert.strictEqual(restarted.active, true)
    assert.strictEqual(restarted.verification, null)
    assert.strictEqual(restarted.cdpEndpoint, null)
    assert.strictEqual(restarted.profileDir, null)
  }
  assert.throws(() => prepareLaunchState(verifiedState({ status: 'completed' })), /不允许启动浏览器/)
}

async function testAttachedSessionNeverNavigatesOrClosesChrome() {
  const pageDir = tempDir('d2c-handoff-attached')
  const handoffPath = path.join(pageDir, 'browser-handoff.json')
  writeState(pageDir, verifiedState())
  const calls = { goto: 0, close: 0, disconnect: 0 }
  const page = {
    url: () => 'http://127.0.0.1:8068/heranew/loan',
    target: () => ({ _targetId: 'target-1' }),
    goto: async () => { calls.goto += 1 },
  }
  const browser = {
    pages: async () => [page],
    close: async () => { calls.close += 1 },
    disconnect: () => { calls.disconnect += 1 },
  }
  const session = await openBrowserSession({
    puppeteer: { connect: async () => browser },
    chrome: '/unused',
    args: { 'handoff-state': handoffPath },
    url: 'http://127.0.0.1:8068/heranew/loan',
  })
  assert.strictEqual(session.attached, true)
  assert.strictEqual(session.targetId, 'target-1')
  assert.strictEqual(calls.goto, 0)
  await closeBrowserSession(session)
  assert.strictEqual(calls.close, 0)
  assert.strictEqual(calls.disconnect, 1)
}

function testCheckpointRequiresOneTab() {
  const stateDir = tempDir('d2c-handoff-gate')
  const pageDir = 'sample-page'
  const pageRoot = path.join(stateDir, pageDir)
  fs.mkdirSync(pageRoot, { recursive: true })
  writeJson(path.join(pageRoot, 'browser-handoff.json'), verifiedState())
  const captureMeta = {
    accessMode: 'user-browser-handoff',
    browserAttached: true,
    browserSessionId: 'session-1',
    browserTargetId: 'target-1',
    redirected: false,
    handoffVerification: { ok: true },
  }
  const validReport = {
    browserAttached: true,
    browserSessionId: 'session-1',
    browserTargetId: 'target-1',
  }
  const problems = []
  validateUserBrowserHandoff(stateDir, pageDir, captureMeta, [
    ['geometry-diff.json', validReport],
    ['vertical-diff.json', validReport],
    ['render-diff.json', validReport],
  ], problems)
  assert.deepStrictEqual(problems, [])

  const mismatch = []
  validateUserBrowserHandoff(stateDir, pageDir, captureMeta, [
    ['geometry-diff.json', { ...validReport, browserTargetId: 'other-target' }],
  ], mismatch)
  assert.ok(mismatch.some((item) => item.includes('未复用截图 tab')))
}

function testMicrosoftEdgeExecutableDiscovery() {
  const candidates = browserExecutableCandidates()
  assert.ok(candidates.includes('/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'))
  assert.ok(candidates.includes('/usr/bin/microsoft-edge-stable'))
  assert.ok(candidates.includes('C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'))

  const names = ['PUPPETEER_EXECUTABLE_PATH', 'CHROME_PATH', 'EDGE_PATH']
  const previous = Object.fromEntries(
    names.map((name) => [name, {
      present: Object.prototype.hasOwnProperty.call(process.env, name),
      value: process.env[name],
    }])
  )
  try {
    for (const name of names) delete process.env[name]
    process.env.EDGE_PATH = '/custom/msedge'
    assert.strictEqual(guessChrome(), '/custom/msedge')
  } finally {
    for (const name of names) {
      if (previous[name].present) process.env[name] = previous[name].value
      else delete process.env[name]
    }
  }
}

async function main() {
  testRequestRequiresRedirectAndMasksUrl()
  testAbortedAndExpiredHandoffsAreRestartable()
  await testAttachedSessionNeverNavigatesOrClosesChrome()
  testCheckpointRequiresOneTab()
  testMicrosoftEdgeExecutableDiscovery()
  console.log('d2c-browser-handoff tests: passed')
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
