#!/usr/bin/env node
'use strict'

/**
 * d2c-build-isolated-preview.cjs — 用项目自己的 webpack 构建独立的预览配置。
 *
 * 用于 Nuxt 等把 webpack 配置封在框架内部、无法就地打补丁的构建链：
 * d2c-isolated-vue-preview.cjs 会生成一份 standalone 配置，
 * 本脚本负责在项目上下文里执行它（复用项目 node_modules 的 webpack 与 loader）。
 *
 * 用法：
 *   node d2c-build-isolated-preview.cjs --config client/d2c-preview/<slug>-webpack.config.cjs
 *
 * 退出码：构建失败（含 stats.hasErrors()）时为 1，便于上层自动修复回路归因。
 */

const fs = require('fs')
const path = require('path')

function parseArgs(argv) {
  const args = {}
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index]
    if (!token.startsWith('--')) throw new Error(`Unexpected argument: ${token}`)
    const value = argv[index + 1]
    if (!value || value.startsWith('--')) throw new Error(`Missing value for ${token}`)
    args[token.slice(2)] = value
    index += 1
  }
  return args
}

function fail(message) {
  console.error(`❌ ${message}`)
  process.exit(1)
}

function main() {
  let args
  try {
    args = parseArgs(process.argv.slice(2))
  } catch (error) {
    fail(error.message)
  }

  if (!args.config) fail('Usage: d2c-build-isolated-preview.cjs --config <webpack-config.cjs>')
  const projectRoot = process.cwd()
  const configPath = path.resolve(projectRoot, args.config)
  if (!fs.existsSync(configPath)) fail(`Webpack config does not exist: ${configPath}`)

  let webpack
  let config
  try {
    webpack = require(path.join(projectRoot, 'node_modules/webpack'))
    delete require.cache[require.resolve(configPath)]
    config = require(configPath)
  } catch (error) {
    fail(
      `Cannot load project webpack/config: ${error.message}\n` +
      `   修法：确认项目已安装 webpack；若配置里引用了项目未安装的 loader，` +
      `该 loader 对应的规则会被跳过，但 webpack 本体与 html-webpack-plugin 必须存在。`
    )
  }

  webpack(config, (error, stats) => {
    if (error) fail(error.stack || error.message)
    const output = stats.toString({ colors: false, chunks: false, modules: false })
    if (output) console.log(output)
    if (stats.hasErrors()) {
      console.error('❌ 独立预览构建失败：上面的 errors 必须由 executor 修复后重跑，禁止跳过预览直接进入评测。')
      process.exitCode = 1
    }
  })
}

if (require.main === module) main()

module.exports = { parseArgs }
