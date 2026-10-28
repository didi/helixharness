#!/usr/bin/env node
'use strict'

const assert = require('assert')
const fs = require('fs')
const os = require('os')
const path = require('path')
const { spawnSync } = require('child_process')

const {
  normalizeWorkflowMode,
  previewAllowedForMode,
  requirePreviewWorkflowMode,
} = require('../lib/d2c-workflow-mode-policy.cjs')

const scriptsDir = path.resolve(__dirname, '..')
const isolatedPreviewScript = path.join(scriptsDir, 'd2c-isolated-vue-preview.cjs')
const isolatedPreviewBuildScript = path.join(scriptsDir, 'd2c-build-isolated-preview.cjs')
const previewHarnessScript = path.join(scriptsDir, 'd2c-preview-harness.cjs')

function tempDir(name) {
  return fs.mkdtempSync(path.join(os.tmpdir(), `${name}-`))
}

function write(file, content) {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, content, 'utf8')
}

function run(script, args, cwd) {
  return spawnSync(process.execPath, [script, ...args], {
    cwd,
    encoding: 'utf8',
  })
}

function createPreviewFixture(mode) {
  const root = tempDir(`d2c-preview-${mode}`)
  const webpackConfig = path.join(root, 'webpack/config/webpack.base.conf.js')
  write(path.join(root, 'client/Page.vue'), '<template><main>preview target</main></template>\n')
  write(path.join(root, 'client/public/index.html'), '<!doctype html><html><body><div id="app"></div></body></html>\n')
  write(webpackConfig, [
    "const HtmlWebpackPlugin = require('html-webpack-plugin')",
    "const paths = { resolveApp(value) { return value } }",
    'module.exports = {',
    '  entry: {',
    "    app: './client/main.js',",
    '  },',
    '  plugins: [',
    '    new HtmlWebpackPlugin({}),',
    '  ],',
    '}',
    '',
  ].join('\n'))
  return { root, webpackConfig, originalWebpack: fs.readFileSync(webpackConfig, 'utf8') }
}

function createNuxtPreviewFixture() {
  const root = tempDir('d2c-preview-nuxt')
  const nuxtConfig = path.join(root, 'nuxt.config.js')
  write(path.join(root, 'pages/Page.vue'), '<template><main>nuxt preview target</main></template>\n')
  write(path.join(root, 'pages/components/Child.vue'), '<template><span>child</span></template>\n')
  write(nuxtConfig, [
    'module.exports = {',
    '  build: {',
    '    extend(config, {',
    '      isClient,',
    '      loaders: { stylus },',
    '    }) {',
    "      config.devtool = 'source-map'",
    '    },',
    '  },',
    '}',
    '',
  ].join('\n'))
  return { root, nuxtConfig, originalNuxt: fs.readFileSync(nuxtConfig, 'utf8') }
}

function testModePolicy() {
  assert.strictEqual(normalizeWorkflowMode('existing-page'), 'update-page')
  assert.strictEqual(previewAllowedForMode('create-page'), true)
  assert.strictEqual(previewAllowedForMode('component'), true)
  assert.strictEqual(previewAllowedForMode('update-page'), false)
  assert.strictEqual(requirePreviewWorkflowMode('page'), 'create-page')
  assert.throws(
    () => requirePreviewWorkflowMode('update-page'),
    (error) => error.code === 'D2C_WORKFLOW_CAPABILITY_VIOLATION',
  )
  assert.throws(
    () => requirePreviewWorkflowMode(''),
    (error) => error.code === 'D2C_PREVIEW_MODE_REQUIRED',
  )
}

function testUpdatePageCannotInitializePreview() {
  const root = tempDir('d2c-update-preview-forbidden')
  const pageDir = path.join(root, 'd-d2c/temp/update-page')
  const target = path.join(root, 'client/Page.vue')
  write(target, '<template><main /></template>\n')

  const isolated = run(isolatedPreviewScript, [
    '--mode', 'update-page',
    '--page-dir', pageDir,
    '--target', target,
  ], root)
  assert.strictEqual(isolated.status, 1)
  assert.match(`${isolated.stdout}${isolated.stderr}`, /D2C_WORKFLOW_CAPABILITY_VIOLATION/)
  assert.strictEqual(fs.existsSync(path.join(pageDir, 'preview-harness.json')), false)
  assert.strictEqual(fs.existsSync(path.join(root, 'client/d2c-preview')), false)

  const harness = run(previewHarnessScript, [
    '--page-dir', pageDir,
    '--init',
    '--mode', 'update-page',
  ], root)
  assert.strictEqual(harness.status, 2)
  assert.match(`${harness.stdout}${harness.stderr}`, /D2C_WORKFLOW_CAPABILITY_VIOLATION/)
  assert.strictEqual(fs.existsSync(path.join(pageDir, 'preview-harness.json')), false)
}

function testAllowedModesKeepSharedPreviewWorking() {
  for (const mode of ['create-page', 'component']) {
    const fixture = createPreviewFixture(mode)
    const pageDir = path.join(fixture.root, 'd-d2c/temp/sample')
    const result = run(isolatedPreviewScript, [
      '--mode', mode,
      '--page-dir', pageDir,
      '--target', 'client/Page.vue',
      '--slug', `${mode}-sample`,
      '--base-url', 'http://127.0.0.1:8069',
      '--target-url', 'http://127.0.0.1:8069/target',
    ], fixture.root)

    assert.strictEqual(result.status, 0, `${mode}: ${result.stderr || result.stdout}`)
    const harnessPath = path.join(pageDir, 'preview-harness.json')
    const harness = JSON.parse(fs.readFileSync(harnessPath, 'utf8'))
    assert.strictEqual(harness.active, true)
    assert.strictEqual(harness.workflowMode, mode)
    assert.ok(fs.existsSync(path.join(fixture.root, `client/d2c-preview/${mode}-sample-entry.js`)))
    assert.match(fs.readFileSync(fixture.webpackConfig, 'utf8'), /D2C-PREVIEW-ISOLATED/)

    const cleanup = run(previewHarnessScript, [
      '--page-dir', pageDir,
      '--cleanup',
    ], fixture.root)
    assert.strictEqual(cleanup.status, 0, `${mode}: ${cleanup.stderr || cleanup.stdout}`)
    const cleaned = JSON.parse(fs.readFileSync(harnessPath, 'utf8'))
    assert.strictEqual(cleaned.active, false)
    assert.strictEqual(fs.readFileSync(fixture.webpackConfig, 'utf8'), fixture.originalWebpack)
    assert.strictEqual(
      fs.existsSync(path.join(fixture.root, `client/d2c-preview/${mode}-sample-entry.js`)),
      false,
    )
  }
}

function testNuxtConfigCanInitializePreview() {
  assert.ok(fs.existsSync(isolatedPreviewBuildScript))
  const fixture = createNuxtPreviewFixture()
  const pageDir = path.join(fixture.root, 'd-d2c/temp/nuxt-sample')
  const result = run(isolatedPreviewScript, [
    '--mode', 'create-page',
    '--page-dir', pageDir,
    '--target', 'pages/Page.vue',
    '--slug', 'nuxt-sample',
    '--base-url', 'http://127.0.0.1:8069',
    '--target-url', 'http://127.0.0.1:8069/target',
  ], fixture.root)

  assert.strictEqual(result.status, 0, result.stderr || result.stdout)
  assert.strictEqual(fs.readFileSync(fixture.nuxtConfig, 'utf8'), fixture.originalNuxt)
  const standaloneConfig = path.join(fixture.root, 'client/d2c-preview/nuxt-sample-webpack.config.cjs')
  assert.ok(fs.existsSync(standaloneConfig))
  const standalone = fs.readFileSync(standaloneConfig, 'utf8')
  assert.match(standalone, /D2C-PREVIEW-ISOLATED nuxt-sample: standalone/)
  assert.match(standalone, /VueLoaderPlugin/)
  assert.match(standalone, /HtmlWebpackPlugin/)
  assert.match(standalone, /nuxt-sample-entry\.js/)
  assert.match(standalone, /static\/d2c-preview-runtime\/nuxt-sample/)
  assert.ok(fs.existsSync(path.join(fixture.root, 'client/d2c-preview/nuxt-sample-entry.js')))
  const generatedTemplate = path.join(fixture.root, 'client/d2c-preview/nuxt-sample-template.html')
  assert.ok(fs.existsSync(generatedTemplate))
  assert.match(fs.readFileSync(generatedTemplate, 'utf8'), /D2C_PREVIEW_MODE__/)

  const runtimeDir = path.join(fixture.root, 'static/d2c-preview-runtime/nuxt-sample')
  const harness = JSON.parse(fs.readFileSync(path.join(pageDir, 'preview-harness.json'), 'utf8'))
  assert.deepStrictEqual(harness.sourceFiles.map((file) => path.basename(file)).sort(), [
    'Child.vue',
    'Page.vue',
  ])
  assert.ok(harness.files.some((entry) => (
    entry.kind === 'created' && entry.path.endsWith('/static/d2c-preview-runtime/nuxt-sample')
  )))
  assert.ok(harness.files.some((entry) => (
    entry.kind === 'created' && entry.path.endsWith('/client/d2c-preview/nuxt-sample-webpack.config.cjs')
  )))
  write(path.join(runtimeDir, 'runtime.js'), 'window.__previewRuntime = true\n')

  const cleanup = run(previewHarnessScript, [
    '--page-dir', pageDir,
    '--cleanup',
  ], fixture.root)
  assert.strictEqual(cleanup.status, 0, cleanup.stderr || cleanup.stdout)
  assert.strictEqual(fs.readFileSync(fixture.nuxtConfig, 'utf8'), fixture.originalNuxt)
  assert.strictEqual(fs.existsSync(standaloneConfig), false)
  assert.strictEqual(fs.existsSync(runtimeDir), false)
}

testModePolicy()
testUpdatePageCannotInitializePreview()
testAllowedModesKeepSharedPreviewWorking()
testNuxtConfigCanInitializePreview()
console.log('d2c preview mode policy tests: passed')
