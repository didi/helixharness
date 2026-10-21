#!/usr/bin/env node
/**
 * 从 dsl_full.json 提取 MasterGo 图标/位图，语义化命名落盘。
 *
 * 用法:
 *   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/extract-dsl-assets.cjs \
 *     --input d-d2c/temp/<page-dir>/dsl_full.json \
 *     --assets-dir assets/images/<page-dir> \
 *     --manifest d-d2c/temp/<page-dir>/dsl-assets.json
 *
 * 通常由 filter-dsl.cjs 在过滤完成后自动调用，无需单独执行。
 */

/* eslint-disable no-console */
const fs = require('fs')
const path = require('path')
const { extractDslAssets, resolveAssetsDirFromPageDir } = require('./lib/dsl-asset-core.cjs')

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

async function main() {
  const args = parseArgs(process.argv)
  const inputPath = args.input
  const outputDir = args['output-dir'] || args.output

  if (!inputPath) {
    console.error(
      'Usage: node extract-dsl-assets.cjs --input <dsl_full.json> [--assets-dir assets/images/<page-dir>] [--manifest <path>]'
    )
    process.exit(1)
  }

  if (!fs.existsSync(inputPath)) {
    console.error(`❌ 输入不存在: ${inputPath}`)
    process.exit(1)
  }

  const pageDir = outputDir
    ? path.basename(path.resolve(outputDir))
    : path.basename(path.dirname(path.resolve(inputPath)))

  const assetsDir =
    args['assets-dir'] && args['assets-dir'] !== true
      ? path.resolve(args['assets-dir'])
      : resolveAssetsDirFromPageDir(pageDir)

  const manifestPath =
    args.manifest && args.manifest !== true
      ? path.resolve(args.manifest)
      : path.join(path.dirname(path.resolve(inputPath)), 'dsl-assets.json')

  const result = await extractDslAssets(path.resolve(inputPath), {
    assetsDir,
    manifestPath,
  })

  console.log('✅ DSL 图标/位图提取完成')
  console.log(`   落盘目录  : ${result.assetsDir}`)
  console.log(`   清单文件  : ${result.manifestPath}`)
  console.log(
    `   已落盘    : ${result.manifest.materialized}/${result.manifest.count}`
  )
  if (result.manifest.needsMasterGoExport.length) {
    console.log(
      `   待 MasterGo 导出: ${result.manifest.needsMasterGoExport.length}（见清单 status=needs_mastergo_export）`
    )
    for (const item of result.manifest.needsMasterGoExport) {
      console.log(`     - ${item.fileBase} (${item.nodeName}, ${item.nodeId})`)
    }
  }
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
