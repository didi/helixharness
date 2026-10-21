#!/usr/bin/env node
'use strict'

/**
 * d-d2c 确定性脚本的回归测试入口。
 *
 * 用法：node scripts/tests/run-tests.cjs [--verbose]
 *
 * - 逐个在独立子进程里执行同目录的 `*.test.cjs`，任一失败即非 0 退出。
 * - 可选运行时依赖（pngjs / jpeg-js / puppeteer-core）缺失时记为 SKIP 而不是失败：
 *   这些依赖按设计由业务仓库或 NODE_PATH 提供，测试环境不强制安装。
 */

const fs = require('fs')
const path = require('path')
const { spawnSync } = require('child_process')

const OPTIONAL_DEPS = ['pngjs', 'jpeg-js', 'puppeteer-core']

/**
 * 已知待实现项：测试已移植，但被测能力尚未落地。
 * 只有失败输出仍匹配 `expect` 时才记为 PENDING；一旦失败原因变了（说明是别的回归），
 * 仍然按失败处理，避免这个清单把真正的破坏掩盖掉。
 * 能力落地后必须删除对应条目，测试随即恢复为强制通过。
 */
const PENDING = {}
const testsDir = __dirname
const verbose = process.argv.includes('--verbose')

const files = fs
  .readdirSync(testsDir)
  .filter((name) => name.endsWith('.test.cjs'))
  .sort()

if (!files.length) {
  console.error('未找到任何 *.test.cjs')
  process.exit(1)
}

const missingDeps = OPTIONAL_DEPS.filter((dep) => {
  try {
    require.resolve(dep)
    return false
  } catch {
    return true
  }
})

const passed = []
const failed = []
const skipped = []
const pending = []

for (const file of files) {
  const result = spawnSync(process.execPath, [path.join(testsDir, file)], {
    encoding: 'utf8',
    cwd: testsDir,
  })
  const output = `${result.stdout || ''}${result.stderr || ''}`

  if (result.status === 0) {
    passed.push(file)
    process.stdout.write(`✅ ${file}\n`)
    continue
  }

  const missing = OPTIONAL_DEPS.find((dep) => (
    missingDeps.includes(dep) &&
    new RegExp(`Cannot find module '${dep.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}'`).test(output)
  ))
  if (missing) {
    skipped.push({ file, reason: `缺少可选依赖 ${missing}（可由 NODE_PATH 或业务仓库提供）` })
    process.stdout.write(`⏭️  ${file}  (SKIP: 缺少可选依赖 ${missing})\n`)
    continue
  }

  const pendingSpec = PENDING[file]
  if (pendingSpec && pendingSpec.expect.test(output)) {
    pending.push({ file, reason: pendingSpec.reason })
    process.stdout.write(`⏳ ${file}  (PENDING: ${pendingSpec.reason})\n`)
    continue
  }
  if (pendingSpec) {
    process.stdout.write(`❌ ${file}  (PENDING 清单命中，但失败原因与预期不符 —— 视为真实回归)\n`)
  }

  failed.push({ file, output })
  process.stdout.write(`❌ ${file}\n`)
}

process.stdout.write(`\n通过 ${passed.length} / 跳过 ${skipped.length} / 待实现 ${pending.length} / 失败 ${failed.length}（共 ${files.length}）\n`)

if (missingDeps.length) {
  process.stdout.write(`未安装的可选依赖：${missingDeps.join(', ')}\n`)
}

if (skipped.length) {
  process.stdout.write('\n跳过原因：\n')
  for (const item of skipped) process.stdout.write(`  - ${item.file}: ${item.reason}\n`)
}

if (pending.length) {
  process.stdout.write('\n待实现（测试已就位，能力落地后删除 PENDING 条目）：\n')
  for (const item of pending) process.stdout.write(`  - ${item.file}: ${item.reason}\n`)
}

if (failed.length) {
  process.stdout.write('\n失败详情：\n')
  for (const item of failed) {
    const lines = item.output.split('\n').filter(Boolean)
    const head = verbose ? lines : lines.slice(0, 12)
    process.stdout.write(`\n--- ${item.file} ---\n${head.join('\n')}\n`)
  }
  process.exit(1)
}
