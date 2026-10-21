#!/usr/bin/env node
'use strict'

/**
 * 预览入口执行断言测试。
 *
 * 回归目标：preview 物料必须能证明「隔离入口真的执行并 mount 了」。
 * 历史上没有任何执行断言，模板属性/静态快照/splitChunks 拆包都会让
 * 页面看起来正常，而入口从未跑起来。
 */

const assert = require('assert')
const fs = require('fs')
const os = require('os')
const path = require('path')
const { spawnSync } = require('child_process')
const {
  PREVIEW_ENTRY_NOT_EXECUTED,
  classifyPreviewEntryFailure,
  summarizePreviewEntryEvidence,
} = require('../lib/d2c-preview-entry-core.cjs')
const { entrySource, deriveTemplate } = require('../d2c-isolated-vue-preview.cjs')

function testExecutedEntryHasNoFailure() {
  assert.strictEqual(classifyPreviewEntryFailure({ previewEntryExecuted: true }), null)
}

function testMountErrorIsAttributedFirst() {
  const failure = classifyPreviewEntryFailure({
    previewEntryExecuted: false,
    previewEntryError: 'Cannot read property \'$store\' of undefined',
    previewEntryScriptSrcs: ['/static/d2c-preview/demo.js'],
  })
  assert.strictEqual(failure.code, PREVIEW_ENTRY_NOT_EXECUTED)
  assert.strictEqual(failure.cause, 'entry-mounted-with-error')
  assert.match(failure.detail, /\$store/)
  assert.ok(failure.repair.includes('mount'))
}

function testMissingEntryChunkIsAttributed() {
  const failure = classifyPreviewEntryFailure({
    previewEntryExecuted: false,
    previewEntryScriptSrcs: [],
    scriptSrcs: ['/static/js/vendors.js'],
  })
  assert.strictEqual(failure.cause, 'entry-chunk-not-injected')
  assert.match(failure.detail, /vendors\.js/)
  assert.match(failure.repair, /HtmlWebpackPlugin/)
}

function testStaticReplayIsAttributed() {
  const failure = classifyPreviewEntryFailure({
    previewEntryExecuted: false,
    previewEntryScriptSrcs: ['/static/js/vue.runtime.js'],
    likelyStaticHtmlReplay: true,
  })
  assert.strictEqual(failure.cause, 'static-html-replay')
  assert.match(failure.repair, /d2c-isolated-vue-preview/)
}

function testLoadedButNotExecutedIsAttributedToSplitChunks() {
  const failure = classifyPreviewEntryFailure({
    previewEntryExecuted: false,
    previewEntryScriptSrcs: ['/static/d2c-preview/demo.js'],
  })
  assert.strictEqual(failure.cause, 'entry-script-loaded-but-not-executed')
  assert.match(failure.detail, /splitChunks/)
  assert.match(failure.repair, /splitChunks/)
}

function testEveryFailureCarriesCodeAndRepair() {
  const cases = [
    { previewEntryExecuted: false },
    { previewEntryExecuted: false, previewEntryError: 'boom' },
    { previewEntryExecuted: false, previewEntryScriptSrcs: ['/x.js'], likelyStaticHtmlReplay: true },
  ]
  for (const probe of cases) {
    const failure = classifyPreviewEntryFailure(probe)
    assert.strictEqual(failure.code, PREVIEW_ENTRY_NOT_EXECUTED)
    assert.ok(failure.cause && failure.detail && failure.repair)
  }
}

function testGeneratedEntryCarriesTheExecutionAssertion() {
  const source = entrySource('./client/Page.vue', 'demo-slug')
  assert.ok(source.includes('window.__D2C_PREVIEW_ENTRY_EXECUTED__ = false;'), 'marker must start false')
  assert.ok(source.includes('window.__D2C_PREVIEW_ENTRY_EXECUTED__ = true;'), 'marker set only after mount')
  assert.ok(source.includes('data-d2c-preview-entry-executed'))
  assert.ok(source.includes('window.__D2C_PREVIEW_ENTRY_ERROR__'))
  // 断言必须在 $mount 之后
  assert.ok(
    source.indexOf('$mount("#app")') < source.indexOf('window.__D2C_PREVIEW_ENTRY_EXECUTED__ = true;'),
    'execution assertion must come after mount'
  )
}

function testMountedNodeCountReadsTheMountResult() {
  // 回归：$mount("#app") 会用渲染根替换 #app，之后 querySelector("#app") 恒为 null，
  // 旧实现因此永远记 0，会被读成「入口跑了但一个节点都没挂上」。
  const source = entrySource('./client/Page.vue', 'demo-slug')
  assert.ok(
    !/document\.querySelector\("#app"\)/.test(source),
    'must not re-query #app after mount (it no longer exists)'
  )
  assert.match(source, /window\.__D2C_PREVIEW_MOUNTED_NODES__ = app && app\.\$el/)
  assert.match(source, /app\.\$el\.querySelectorAll\("\*"\)\.length/)
  assert.match(source, /window\.__D2C_PREVIEW_MOUNT_TAG__ = app && app\.\$el \? app\.\$el\.tagName : null;/)
}

function testEntryRefusesToRunOutsideAPreviewPage() {
  // 回归：项目 HtmlWebpackPlugin 未配 chunks 时，预览 entry 会被注入主应用 index.html。
  // 它在顶层覆盖 Vue.prototype.$store/$router/$route 并抢占 #app → 打挂主应用。
  const source = entrySource('./client/Page.vue', 'demo-slug')
  assert.match(source, /\(function bootstrapD2cPreview\(\) \{/)
  assert.match(source, /if \(!document\.documentElement\.hasAttribute\("data-d2c-template-preview-slug"\)\)/)
  assert.match(source, /window\.__D2C_PREVIEW_ENTRY_SKIPPED__ = "not-a-preview-page";/)
  assert.match(source, /return;/)
  // 守卫必须排在所有副作用之前
  const guardAt = source.indexOf('hasAttribute("data-d2c-template-preview-slug")')
  for (const sideEffect of [
    'installPreviewGetter(',
    'window.__D2C_PREVIEW_MODE__ =',
    'document.documentElement.setAttribute("data-d2c-preview-mode"',
    '$mount("#app")',
  ]) {
    assert.ok(
      guardAt >= 0 && guardAt < source.indexOf(sideEffect),
      `guard must precede ${sideEffect}`
    )
  }
  // 括号配平（生成物可解析）
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-entry-parse-'))
  try {
    const file = path.join(dir, 'entry.mjs')
    fs.writeFileSync(file, source, 'utf8')
    const check = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8' })
    assert.strictEqual(check.status, 0, check.stderr)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
}

function testSkippedEntryIsAttributedSeparately() {
  const failure = classifyPreviewEntryFailure({
    previewEntryExecuted: false,
    previewEntrySkipped: 'not-a-preview-page',
    previewEntryScriptSrcs: ['/static/d2c-preview/demo.js'],
  })
  assert.strictEqual(failure.code, PREVIEW_ENTRY_NOT_EXECUTED)
  assert.strictEqual(failure.cause, 'entry-skipped-not-a-preview-page')
  assert.match(failure.detail, /data-d2c-template-preview-slug/)
  assert.match(failure.repair, /previewUrl|预览页/)
  // 跳过优先于「已加载但未执行」——两者成因完全不同
  assert.notStrictEqual(failure.cause, 'entry-script-loaded-but-not-executed')
}

function testEvidenceSummaryCarriesSkipFlag() {
  const summary = summarizePreviewEntryEvidence({ previewEntrySkipped: 'not-a-preview-page' })
  assert.strictEqual(summary.previewEntrySkipped, 'not-a-preview-page')
  assert.strictEqual(summary.previewEntryExecuted, false)
  assert.strictEqual(summarizePreviewEntryEvidence({}).previewEntrySkipped, null)
}

function testTemplateAloneCannotSatisfyTheAssertion() {
  const html = deriveTemplate('<html><body><div id="app"></div></body></html>', 'demo-slug')
  assert.ok(!html.includes('ENTRY_EXECUTED'), 'template must not pre-set the execution assertion')
  // 只有模板属性、没有入口执行证据时，断言依然判定为失败。
  const failure = classifyPreviewEntryFailure({ templatePreviewMode: 'isolated-vue-mount', previewMode: 'isolated-vue-mount' })
  assert.strictEqual(failure.code, PREVIEW_ENTRY_NOT_EXECUTED)
  assert.strictEqual(failure.cause, 'entry-chunk-not-injected')
}

function testEvidenceSummaryIsSerializable() {
  const summary = summarizePreviewEntryEvidence({
    previewEntryExecuted: true,
    previewEntryError: null,
    previewEntryScriptSrcs: ['/a.js'],
    mountedNodeCount: 3,
  })
  assert.deepStrictEqual(JSON.parse(JSON.stringify(summary)), {
    previewEntryExecuted: true,
    previewEntryError: null,
    previewEntrySkipped: null,
    previewEntryScriptSrcs: ['/a.js'],
    mountedNodeCount: 3,
  })
  assert.strictEqual(summarizePreviewEntryEvidence({}).mountedNodeCount, null)
}

function main() {
  testExecutedEntryHasNoFailure()
  testMountErrorIsAttributedFirst()
  testMissingEntryChunkIsAttributed()
  testStaticReplayIsAttributed()
  testLoadedButNotExecutedIsAttributedToSplitChunks()
  testEveryFailureCarriesCodeAndRepair()
  testGeneratedEntryCarriesTheExecutionAssertion()
  testMountedNodeCountReadsTheMountResult()
  testEntryRefusesToRunOutsideAPreviewPage()
  testSkippedEntryIsAttributedSeparately()
  testEvidenceSummaryCarriesSkipFlag()
  testTemplateAloneCannotSatisfyTheAssertion()
  testEvidenceSummaryIsSerializable()
  console.log('d2c preview entry assertion tests passed')
}

main()
