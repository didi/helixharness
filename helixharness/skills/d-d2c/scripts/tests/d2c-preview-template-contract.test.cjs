#!/usr/bin/env node
'use strict'

/**
 * 预览模板契约测试。
 *
 * 回归目标：模板（构建期静态 HTML）不得预置运行时状态。
 * 历史上 deriveTemplate 会把 data-d2c-preview-mode / -slug 直接写进 <html>，
 * 入口模块运行时再写一遍；一旦入口没执行（构建失败 / mount 异常），
 * capture 探针仍会读到这两个属性，把「预览已生效」当成事实。
 */

const assert = require('assert')
const { deriveTemplate, entrySource } = require('../d2c-isolated-vue-preview.cjs')

const SOURCE = '<html><head></head><body><div id="app"></div></body></html>'

function htmlTag(html) {
  const match = /<html\b[^>]*>/i.exec(html)
  assert.ok(match, 'generated template must contain an <html> tag')
  return match[0]
}

function testTemplateDoesNotBakeRuntimeState() {
  const html = deriveTemplate(SOURCE, 'demo-slug')
  const tag = htmlTag(html)
  assert.ok(!/\bdata-d2c-preview-mode=/.test(tag), `template baked runtime mode: ${tag}`)
  assert.ok(!/\bdata-d2c-preview-slug=/.test(tag), `template baked runtime slug: ${tag}`)
  assert.ok(!/\bdata-d2c-preview-bootstrap-bypass=/.test(tag), `template baked runtime bypass: ${tag}`)
}

function testTemplateDeclaresBuildTimeProvenance() {
  const html = deriveTemplate(SOURCE, 'demo-slug')
  const tag = htmlTag(html)
  assert.ok(/data-d2c-template-preview-mode="isolated-vue-mount"/.test(tag))
  assert.ok(/data-d2c-template-preview-slug="demo-slug"/.test(tag))
}

function testRuntimeMarkersStillEmittedAsScript() {
  const html = deriveTemplate(SOURCE, 'demo-slug')
  assert.ok(html.includes('__D2C_PREVIEW_MODE__'))
  assert.ok(html.includes('__D2C_PREVIEW_BOOTSTRAP_BYPASS__'))
  assert.ok(html.includes('document.documentElement.setAttribute("data-d2c-preview-mode"'))
}

function testTemplateDerivationIsIdempotent() {
  const once = deriveTemplate(SOURCE, 'demo-slug')
  const twice = deriveTemplate(once, 'demo-slug')
  assert.strictEqual((twice.match(/data-d2c-template-preview-mode=/g) || []).length, 1)
  assert.strictEqual((twice.match(/__D2C_PREVIEW_MODE__/g) || []).length, 1)
}

function testFragmentWithoutHtmlTagGetsTemplateProvenanceOnly() {
  const html = deriveTemplate('<body><div id="app"></div></body>', 'frag')
  const tag = htmlTag(html)
  assert.ok(/data-d2c-template-preview-slug="frag"/.test(tag))
  assert.ok(!/\bdata-d2c-preview-slug=/.test(tag))
}

function testExistingUserRuntimeAttributeIsPreserved() {
  const html = deriveTemplate('<html data-d2c-preview-mode="vue-route-preview"><body><div id="app"></div></body></html>', 'demo')
  const tag = htmlTag(html)
  assert.ok(/data-d2c-preview-mode="vue-route-preview"/.test(tag), 'hand-authored attribute must not be stripped')
  assert.ok(/data-d2c-template-preview-mode=/.test(tag))
}

function testEntrySourceWritesRuntimeStateOnly() {
  const source = entrySource('./client/Page.vue', 'demo-slug')
  assert.ok(source.includes('setAttribute("data-d2c-preview-mode"'))
  assert.ok(source.includes('setAttribute("data-d2c-preview-slug", "demo-slug")'))
  // 入口可以「读」构建期标记（用作「当前是否预览页」的判据），但绝不能「写」进 template- 命名空间：
  // 那个命名空间是模板的构建期事实，运行时写入会让它失去区分能力。
  assert.ok(
    !/setAttribute\(\s*["']data-d2c-template-preview/.test(source),
    'entry must not write into the template- namespace'
  )
  for (const attr of source.match(/setAttribute\(\s*["']([^"']+)["']/g) || []) {
    assert.ok(
      !attr.includes('data-d2c-template-preview'),
      `entry wrote template attribute: ${attr}`
    )
  }
}

function main() {
  testTemplateDoesNotBakeRuntimeState()
  testTemplateDeclaresBuildTimeProvenance()
  testRuntimeMarkersStillEmittedAsScript()
  testTemplateDerivationIsIdempotent()
  testFragmentWithoutHtmlTagGetsTemplateProvenanceOnly()
  testExistingUserRuntimeAttributeIsPreserved()
  testEntrySourceWritesRuntimeStateOnly()
  console.log('d2c preview template contract tests passed')
}

main()
