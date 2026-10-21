#!/usr/bin/env node
'use strict'

const assert = require('assert')
const fs = require('fs')
const os = require('os')
const path = require('path')
const { spawnSync } = require('child_process')

const {
  browserTextSignals,
  classifyRuntimeHealth,
  probeRuntimePage,
  previewHarnessExcludedFiles,
  resolveAppRootSelectors,
  sourceFingerprint,
  validateHealthEvidence,
  runtimeRepairSentinel,
} = require('../lib/d2c-runtime-health-core.cjs')

const checkpointScript = path.resolve(__dirname, '../d2c-workflow-checkpoint.cjs')

function mountedProbe(overrides = {}) {
  return {
    appRootPresent: true,
    appMounted: true,
    appRootSelector: '#app',
    appChildElementCount: 1,
    bodyTextLength: 100,
    overlayPresent: false,
    textSignals: [],
    ...overrides,
  }
}

function emptyCollector(overrides = {}) {
  return {
    consoleErrorCount: 0,
    consoleErrors: [],
    pageErrorCount: 0,
    pageErrors: [],
    navigationResponses: [{ status: 200, url: 'http://127.0.0.1:8069/heranew/loan' }],
    navigationFailures: [],
    ...overrides,
  }
}

function runGit(cwd, args) {
  const result = spawnSync('git', args, { cwd, encoding: 'utf8' })
  assert.strictEqual(result.status, 0, result.stderr)
}

function testCompilerSignalsBeatHttp200AndRedirect() {
  const health = classifyRuntimeHealth({
    requestedUrl: 'http://127.0.0.1:8069/heranew/loan',
    finalUrl: 'http://127.0.0.1:8069/heranew/uncredit',
    domProbe: mountedProbe(),
    collector: emptyCollector({
      consoleErrorCount: 1,
      consoleErrors: ['Errors while compiling. Module build failed: RangeError'],
    }),
  })
  assert.strictEqual(health.status, 'failed')
  assert.strictEqual(health.failureClass, 'compile-error')
  assert.strictEqual(health.askUser, false)
  assert.match(runtimeRepairSentinel(health), /D2C_COMPILE_REPAIR_REQUIRED/)
}

function testCannotGetIsExecutorRepair() {
  const health = classifyRuntimeHealth({
    requestedUrl: 'http://127.0.0.1:8069/heranew/loan',
    finalUrl: 'http://127.0.0.1:8069/heranew/loan',
    domProbe: mountedProbe({
      appRootPresent: false,
      appMounted: false,
      appRootSelector: null,
      appChildElementCount: null,
      textSignals: browserTextSignals('Cannot GET /heranew/loan'),
    }),
    collector: emptyCollector(),
  })
  assert.strictEqual(health.status, 'failed')
  assert.strictEqual(health.failureClass, 'route-unreachable')
  assert.strictEqual(health.askUser, false)
  assert.match(runtimeRepairSentinel(health), /D2C_RUNTIME_REPAIR_REQUIRED/)
}

function testHealthyMountedRedirectStaysEligibleForHandoff() {
  const health = classifyRuntimeHealth({
    requestedUrl: 'http://127.0.0.1:8069/heranew/loan',
    finalUrl: 'http://127.0.0.1:8069/heranew/uncredit',
    domProbe: mountedProbe(),
    collector: emptyCollector(),
  })
  assert.strictEqual(health.status, 'passed')
  assert.strictEqual(health.routeAccess, 'redirected')
  assert.strictEqual(health.appMounted, true)
}

function testUnmountedAppFailsWithoutCompilerText() {
  const health = classifyRuntimeHealth({
    requestedUrl: 'http://127.0.0.1:8069/heranew/loan',
    finalUrl: 'http://127.0.0.1:8069/heranew/loan',
    domProbe: mountedProbe({ appMounted: false, appChildElementCount: 0 }),
    collector: emptyCollector(),
  })
  assert.strictEqual(health.status, 'failed')
  assert.strictEqual(health.failureClass, 'app-not-mounted')
}

async function testRootAppIsDetectedByDefault() {
  assert.deepStrictEqual(resolveAppRootSelectors('#rootApp').slice(0, 4), [
    '#rootApp',
    '#app',
    '#App',
    '#root',
  ])

  const previousDocument = global.document
  const root = {
    childElementCount: 1,
    childNodes: [{}],
    textContent: '',
  }
  global.document = {
    title: 'rootApp fixture',
    readyState: 'complete',
    body: { innerText: 'mounted page' },
    scripts: [],
    querySelector(selector) {
      return selector === '#rootApp' ? root : null
    },
  }
  try {
    const probe = await probeRuntimePage({
      evaluate: async (callback, input) => callback(input),
      frames: () => [],
    })
    assert.strictEqual(probe.appRootPresent, true)
    assert.strictEqual(probe.appMounted, true)
    assert.strictEqual(probe.appRootSelector, '#rootApp')
  } finally {
    if (previousDocument === undefined) delete global.document
    else global.document = previousDocument
  }
}

function testSourceFingerprintInvalidatesAfterEdit() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-runtime-source-'))
  try {
    runGit(root, ['init', '-q'])
    runGit(root, ['config', 'user.email', 'd2c@example.com'])
    runGit(root, ['config', 'user.name', 'd2c-test'])
    fs.writeFileSync(path.join(root, 'page.vue'), '<template><div /></template>\n', 'utf8')
    runGit(root, ['add', 'page.vue'])
    runGit(root, ['commit', '-qm', 'fixture'])

    fs.writeFileSync(path.join(root, 'page.vue'), '<template><main /></template>\n', 'utf8')
    const checked = sourceFingerprint(root)
    const evidence = { status: 'passed', askUser: false, sourceFingerprint: checked }
    assert.deepStrictEqual(validateHealthEvidence(evidence, { cwd: root }), [])

    fs.writeFileSync(path.join(root, 'page.vue'), '<template><section /></template>\n', 'utf8')
    assert.ok(validateHealthEvidence(evidence, { cwd: root }).some((item) => item.includes('已过期')))
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
}

function testPreviewHarnessCleanupDoesNotStaleBusinessEvidence() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-runtime-preview-'))
  try {
    runGit(root, ['init', '-q'])
    runGit(root, ['config', 'user.email', 'd2c@example.com'])
    runGit(root, ['config', 'user.name', 'd2c-test'])
    const page = path.join(root, 'page.vue')
    const webpack = path.join(root, 'webpack.config.js')
    const ignoredPage = path.join(root, 'pages/d2c/page.vue')
    const runtimeDir = path.join(root, 'static/d2c-preview-runtime/sample')
    fs.writeFileSync(page, '<template><div /></template>\n', 'utf8')
    fs.writeFileSync(webpack, 'module.exports = {}\n', 'utf8')
    fs.writeFileSync(path.join(root, '.gitignore'), 'pages/d2c\n', 'utf8')
    runGit(root, ['add', 'page.vue', 'webpack.config.js'])
    runGit(root, ['commit', '-qm', 'fixture'])

    fs.writeFileSync(page, '<template><main /></template>\n', 'utf8')
    fs.writeFileSync(webpack, 'module.exports = { d2cPreview: true }\n', 'utf8')
    fs.mkdirSync(path.dirname(ignoredPage), { recursive: true })
    fs.writeFileSync(ignoredPage, '<template><article /></template>\n', 'utf8')
    fs.mkdirSync(runtimeDir, { recursive: true })
    fs.writeFileSync(path.join(runtimeDir, 'bundle.js'), 'preview bundle\n', 'utf8')
    fs.writeFileSync(path.join(runtimeDir, 'index.html'), '<div id="app"></div>\n', 'utf8')
    const pageDir = path.join(root, 'd-d2c/temp/sample')
    const metaPath = path.join(pageDir, 'r1/capture-meta.json')
    fs.mkdirSync(path.dirname(metaPath), { recursive: true })
    fs.writeFileSync(path.join(pageDir, 'preview-harness.json'), JSON.stringify({
      active: true,
      sourceFiles: [ignoredPage],
      files: [
        { kind: 'modified', path: webpack },
        { kind: 'created', path: runtimeDir },
      ],
    }), 'utf8')
    const excluded = previewHarnessExcludedFiles(metaPath, root)
    assert.deepStrictEqual(excluded, [
      'static/d2c-preview-runtime/sample',
      'webpack.config.js',
    ])
    const checked = sourceFingerprint(root, [ignoredPage], excluded)
    assert.ok(checked.files.includes('pages/d2c/page.vue'))
    assert.ok(!checked.files.some((file) => file.startsWith('static/d2c-preview-runtime/sample/')))

    fs.writeFileSync(webpack, 'module.exports = {}\n', 'utf8')
    fs.rmSync(runtimeDir, { recursive: true, force: true })
    const evidence = { status: 'passed', askUser: false, sourceFingerprint: checked }
    assert.deepStrictEqual(validateHealthEvidence(evidence, { cwd: root }), [])

    fs.writeFileSync(ignoredPage, '<template><aside /></template>\n', 'utf8')
    assert.ok(validateHealthEvidence(evidence, { cwd: root }).some((item) => item.includes('已过期')))
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
}

function testPhase1CheckpointRequiresFreshPassingHealth() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-runtime-checkpoint-'))
  try {
    runGit(root, ['init', '-q'])
    runGit(root, ['config', 'user.email', 'd2c@example.com'])
    runGit(root, ['config', 'user.name', 'd2c-test'])
    const page = path.join(root, 'page.vue')
    fs.writeFileSync(page, '<template><div /></template>\n', 'utf8')
    runGit(root, ['add', 'page.vue'])
    runGit(root, ['commit', '-qm', 'fixture'])
    fs.writeFileSync(page, '<template><main /></template>\n', 'utf8')

    const pageDir = path.join(root, 'd-d2c/temp/sample')
    fs.mkdirSync(pageDir, { recursive: true })
    fs.writeFileSync(path.join(pageDir, 'compile-check.json'), JSON.stringify({
      checkedBy: 'd2c-runtime-health-check',
      status: 'passed',
      askUser: false,
      appMounted: true,
      compileStatus: 'passed',
      finalUrl: 'http://127.0.0.1:8069/app',
      sourceFingerprint: sourceFingerprint(root),
    }), 'utf8')

    const invoke = () => spawnSync(process.execPath, ['-e',
      `require(${JSON.stringify(checkpointScript)}).validatePhase1RuntimeHealth('d-d2c/temp', 'sample')`,
    ], { cwd: root, encoding: 'utf8' })
    const passing = invoke()
    assert.strictEqual(passing.status, 0, passing.stderr)
    assert.match(passing.stdout, /编译健康闸通过/)

    fs.writeFileSync(page, '<template><section /></template>\n', 'utf8')
    const stale = invoke()
    assert.strictEqual(stale.status, 1)
    assert.match(stale.stderr, /已过期/)
    assert.match(stale.stderr, /askUser=false/)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
}

async function main() {
  testCompilerSignalsBeatHttp200AndRedirect()
  testCannotGetIsExecutorRepair()
  testHealthyMountedRedirectStaysEligibleForHandoff()
  testUnmountedAppFailsWithoutCompilerText()
  await testRootAppIsDetectedByDefault()
  testSourceFingerprintInvalidatesAfterEdit()
  testPreviewHarnessCleanupDoesNotStaleBusinessEvidence()
  testPhase1CheckpointRequiresFreshPassingHealth()
  console.log('d2c runtime health gate tests: passed')
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
