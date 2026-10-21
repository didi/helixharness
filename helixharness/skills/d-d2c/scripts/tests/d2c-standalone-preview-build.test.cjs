#!/usr/bin/env node
'use strict'

/**
 * 独立预览构建链契约测试（Batch 3）。
 *
 * 覆盖已移植测试（d2c-preview-mode-policy.test.cjs）之外的细节：
 *   - 生成的 standalone 配置必须关闭 splitChunks/runtimeChunk
 *     （否则入口执行断言可能落在未被 HTML 加载的子 chunk 上，无法归因）
 *   - 可选 loader 缺失时跳过规则，而不是让整份配置加载失败
 *   - 构建脚本的参数校验与退出码
 *   - preview-harness 清理必须能删目录（否则 runtime 目录残留、harness 永远 active）
 */

const assert = require('assert')
const fs = require('fs')
const os = require('os')
const path = require('path')
const { spawnSync } = require('child_process')

const {
  standaloneWebpackConfigSource,
  collectPreviewSourceFiles,
  deriveTemplate,
} = require('../d2c-isolated-vue-preview.cjs')

const scriptsDir = path.resolve(__dirname, '..')
const buildScript = path.join(scriptsDir, 'd2c-build-isolated-preview.cjs')
const harnessScript = path.join(scriptsDir, 'd2c-preview-harness.cjs')

function tempDir(name) {
  return fs.mkdtempSync(path.join(os.tmpdir(), `${name}-`))
}

function write(file, content) {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, content, 'utf8')
}

function run(script, args, cwd) {
  return spawnSync(process.execPath, [script, ...args], { cwd, encoding: 'utf8' })
}

function generatedConfig() {
  return standaloneWebpackConfigSource(
    '/proj',
    '/proj/client/d2c-preview/demo-entry.js',
    '/proj/client/d2c-preview/demo-template.html',
    '/proj/static/d2c-preview-runtime/demo',
    'd2c_preview_demo',
    'demo',
  )
}

function testBuildScriptExistsAndExportsParseArgs() {
  assert.ok(fs.existsSync(buildScript))
  assert.strictEqual(typeof require(buildScript).parseArgs, 'function')
}

function testStandaloneConfigDisablesChunkSplitting() {
  const source = generatedConfig()
  assert.match(source, /optimization:\s*\{\s*splitChunks:\s*false,\s*runtimeChunk:\s*false\s*\}/)
}

function testStandaloneConfigLoadsOptionally() {
  const source = generatedConfig()
  // 不允许对可选 loader 直接 require（缺失即整份配置炸）
  for (const loader of ['vue-loader', 'ts-loader', 'stylus-loader', 'url-loader', 'file-loader', 'vue-style-loader']) {
    assert.ok(!new RegExp(`require\\('${loader}'\\)`).test(source), `${loader} must be resolved optionally`)
  }
  assert.match(source, /function optional\(name\)/)
  assert.match(source, /VueLoaderPlugin \? new VueLoaderPlugin\(\) : null/)
  assert.match(source, /D2C-PREVIEW-ISOLATED demo: standalone/)
}

function testStandaloneConfigIsParsableAndPointsAtTheRightFiles() {
  const dir = tempDir('d2c-standalone-config')
  try {
    const file = path.join(dir, 'demo-webpack.config.cjs')
    fs.writeFileSync(file, generatedConfig(), 'utf8')
    const check = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8' })
    assert.strictEqual(check.status, 0, check.stderr)
    const source = fs.readFileSync(file, 'utf8')
    assert.match(source, /HtmlWebpackPlugin/)
    assert.match(source, /demo-entry\.js/)
    assert.match(source, /demo-template\.html/)
    assert.match(source, /static\/d2c-preview-runtime\/demo/)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
}

function testBuildScriptArgValidation() {
  const dir = tempDir('d2c-build-args')
  try {
    const missing = run(buildScript, [], dir)
    assert.strictEqual(missing.status, 1)
    assert.match(`${missing.stdout}${missing.stderr}`, /--config/)

    // 参数不完整（--config 后无值）
    const noValue = run(buildScript, ['--config'], dir)
    assert.strictEqual(noValue.status, 1)
    assert.match(`${noValue.stdout}${noValue.stderr}`, /Missing value for --config/)

    // config 文件不存在
    const absent = run(buildScript, ['--config', 'nope.cjs'], dir)
    assert.strictEqual(absent.status, 1)
    assert.match(`${absent.stdout}${absent.stderr}`, /does not exist/)

    // 意外位置参数
    const stray = run(buildScript, ['oops'], dir)
    assert.strictEqual(stray.status, 1)
    assert.match(`${stray.stdout}${stray.stderr}`, /Unexpected argument/)

    // 项目没有 webpack 时给出可操作修法（而不是裸栈）
    const noWebpack = path.join(dir, 'proj')
    write(path.join(noWebpack, 'cfg.cjs'), 'module.exports = {}\n', 'utf8')
    const failed = run(buildScript, ['--config', 'cfg.cjs'], noWebpack)
    assert.strictEqual(failed.status, 1)
    assert.match(`${failed.stdout}${failed.stderr}`, /Cannot load project webpack\/config/)
    assert.match(`${failed.stdout}${failed.stderr}`, /修法/)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
}

function testCollectPreviewSourceFilesWalksSubdirectories() {
  const dir = tempDir('d2c-source-files')
  try {
    write(path.join(dir, 'Page.vue'), '<template><main /></template>\n', 'utf8')
    write(path.join(dir, 'helper.js'), 'export default 1\n', 'utf8')
    write(path.join(dir, 'components/Child.vue'), '<template><span /></template>\n', 'utf8')
    write(path.join(dir, 'notes.md'), '# not a source file\n', 'utf8')
    const files = collectPreviewSourceFiles(path.join(dir, 'Page.vue')).map((file) => path.basename(file))
    assert.deepStrictEqual(files.sort(), ['Child.vue', 'Page.vue', 'helper.js'])
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
}

function initHarnessFixture(root, pageDir) {
  return run(harnessScript, [
    '--page-dir', pageDir, '--init', '--mode', 'create-page',
    '--reason', 'test', '--preview-kind', 'isolated-vue-mount',
    '--bypasses-bootstrap', 'true', '--preview-url', '/index.html',
  ], root)
}

function testHarnessCleanupRemovesCreatedDirectories() {
  const root = tempDir('d2c-harness-dir-cleanup')
  try {
    const pageDir = path.join(root, 'd-d2c/temp/demo')
    const init = initHarnessFixture(root, pageDir)
    assert.strictEqual(init.status, 0, init.stderr || init.stdout)

    // 与真实 Nuxt 流程一致：目录在登记时还不存在，构建后才产生内容。
    const runtimeDir = path.join(root, 'static/d2c-preview-runtime/demo')
    const tracked = run(harnessScript, ['--page-dir', pageDir, '--created', runtimeDir], root)
    assert.strictEqual(tracked.status, 0, tracked.stderr || tracked.stdout)
    write(path.join(runtimeDir, 'bundle.js'), 'window.__built = true\n', 'utf8')

    const cleanup = run(harnessScript, ['--page-dir', pageDir, '--cleanup'], root)
    assert.strictEqual(cleanup.status, 0, cleanup.stderr || cleanup.stdout)
    assert.strictEqual(fs.existsSync(runtimeDir), false, 'runtime 目录必须被递归删除')

    const state = JSON.parse(fs.readFileSync(path.join(pageDir, 'preview-harness.json'), 'utf8'))
    assert.strictEqual(state.active, false)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
}

function testHarnessRestoresPreexistingDirectory() {
  const root = tempDir('d2c-harness-dir-restore')
  try {
    const pageDir = path.join(root, 'd-d2c/temp/demo')
    const init = initHarnessFixture(root, pageDir)
    assert.strictEqual(init.status, 0, init.stderr || init.stdout)

    // 目录已存在（重跑 / 上一次未清理）→ 必须备份并还原，不能 ENOTSUP 崩掉，
    // 也不能在清理时把用户原有内容删掉。
    const runtimeDir = path.join(root, 'static/d2c-preview-runtime/demo')
    write(path.join(runtimeDir, 'original.txt'), 'preexisting\n', 'utf8')
    const tracked = run(harnessScript, ['--page-dir', pageDir, '--created', runtimeDir], root)
    assert.strictEqual(tracked.status, 0, `跟踪已存在目录不得失败：${tracked.stderr || tracked.stdout}`)

    write(path.join(runtimeDir, 'original.txt'), 'clobbered\n', 'utf8')
    write(path.join(runtimeDir, 'new-from-build.js'), 'x\n', 'utf8')

    const cleanup = run(harnessScript, ['--page-dir', pageDir, '--cleanup'], root)
    assert.strictEqual(cleanup.status, 0, cleanup.stderr || cleanup.stdout)
    assert.strictEqual(
      fs.readFileSync(path.join(runtimeDir, 'original.txt'), 'utf8'),
      'preexisting\n',
      '原有内容必须被还原',
    )
    assert.strictEqual(fs.existsSync(path.join(runtimeDir, 'new-from-build.js')), false, '构建新增文件必须被清掉')
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
}

function testNuxtModeDoesNotLeakRuntimeAttributesIntoTemplate() {
  // Batch 1-⑥ 的约束同样适用于 Nuxt 独立构建链
  const html = deriveTemplate('<html><body><div id="app"></div></body></html>', 'nuxt-sample')
  const tag = /<html\b[^>]*>/i.exec(html)[0]
  assert.ok(!/\bdata-d2c-preview-mode=/.test(tag))
  assert.ok(/data-d2c-template-preview-slug="nuxt-sample"/.test(tag))
}

function main() {
  testBuildScriptExistsAndExportsParseArgs()
  testStandaloneConfigDisablesChunkSplitting()
  testStandaloneConfigLoadsOptionally()
  testStandaloneConfigIsParsableAndPointsAtTheRightFiles()
  testBuildScriptArgValidation()
  testCollectPreviewSourceFilesWalksSubdirectories()
  testHarnessCleanupRemovesCreatedDirectories()
  testHarnessRestoresPreexistingDirectory()
  testNuxtModeDoesNotLeakRuntimeAttributesIntoTemplate()
  console.log('d2c standalone preview build tests passed')
}

main()
