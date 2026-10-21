#!/usr/bin/env node
/* eslint-disable no-console */
/**
 * d2c-slice-dsl.cjs — 复杂页组件 DSL 切片
 *
 * 从整页 dsl_layout_marked.json 中按组件的 layoutMarks 取出子树，
 * 复用 generateLayoutArtifacts 在 components/<id>/ 下重新生成该组件专属的
 * Layer.md / dom.md / styles.md / mixed-text.json / text-whitespace.json /
 * flex-groups.json / flex-nesting.md。实现组件时只读本目录切片，
 * 禁止读整页 dom.md / styles.md（省 token + 避免上下文稀释）。
 *
 * 组件来源（按优先级）：
 *   1. <page-dir>/component-plan.json   — Agent 微调后的最终拆解方案
 *   2. <page-dir>/complexity-plan.json  — 脚本建议拆分（suggestedComponents）
 *
 * 注意：切片重跑后组件内 layoutMark 从 L0 重新编号，与整页 mark 不同。
 * 根节点对应关系写在 components/<id>/component-meta.json 的 rootMarkMap，
 * 整页校验（geometry-check --component）的 violations 用整页 mark，按此映射回切片。
 *
 * 用法：
 *   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-slice-dsl.cjs --page-dir d-d2c/temp/<page-dir> --component c1
 *   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-slice-dsl.cjs --page-dir d-d2c/temp/<page-dir> --all
 */
const fs = require('fs')
const path = require('path')
const { generateLayoutArtifacts } = require('./lib/dsl-layout-core.cjs')

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

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'))
}

/** 读取组件清单：component-plan.json（Agent 最终方案）优先，缺省回退脚本建议 */
function loadComponents(pageDir) {
  const planPath = path.join(pageDir, 'component-plan.json')
  if (fs.existsSync(planPath)) {
    const plan = readJson(planPath)
    const components = plan.components || plan
    if (Array.isArray(components) && components.length) {
      return { components, source: planPath }
    }
  }
  const complexityPath = path.join(pageDir, 'complexity-plan.json')
  if (fs.existsSync(complexityPath)) {
    const plan = readJson(complexityPath)
    if ((plan.suggestedComponents || []).length) {
      return { components: plan.suggestedComponents, source: complexityPath }
    }
  }
  throw new Error(
    `找不到组件清单：${planPath} 或 complexity-plan.json 的 suggestedComponents（须先运行 d2c-complexity-plan.cjs，复杂页由 Agent 落盘 component-plan.json）`
  )
}

function findSubtreeByMark(roots, layoutMark) {
  let found = null
  function walk(node) {
    if (found || !node) return
    if (node._layoutMark === layoutMark) {
      found = node
      return
    }
    for (const c of node.children || []) walk(c)
  }
  for (const r of roots || []) walk(r)
  return found
}

function sliceComponent(pageDir, component, markedNodes, dslFullPath) {
  const marks = component.layoutMarks || (component.layoutMark ? [component.layoutMark] : [])
  if (!marks.length) {
    throw new Error(`组件 ${component.id} 缺少 layoutMarks`)
  }

  const subtrees = []
  for (const mark of marks) {
    const node = findSubtreeByMark(markedNodes, mark)
    if (!node) {
      throw new Error(`组件 ${component.id} 的 layoutMark ${mark} 在 dsl_layout_marked.json 中不存在`)
    }
    subtrees.push({ mark, node })
  }

  const outDir = path.join(pageDir, 'components', component.id)
  fs.mkdirSync(outDir, { recursive: true })

  // 子树作为根节点重跑产物生成（深拷贝防止 generateLayoutArtifacts 改动原树）
  const preStrippedNodes = JSON.parse(JSON.stringify(subtrees.map((s) => s.node)))
  const result = generateLayoutArtifacts(dslFullPath, outDir, {
    stripChrome: false,
    preStrippedNodes,
  })

  // 切片 mark（重编号）↔ 整页 mark 的根级映射，按节点 id 匹配
  const sliceMarked = readJson(path.join(outDir, 'dsl_layout_marked.json'))
  const sliceDsl = sliceMarked.dsl !== undefined ? sliceMarked.dsl : sliceMarked
  const rootMarkMap = {}
  for (const sliceRoot of sliceDsl.nodes || []) {
    const origin = subtrees.find((s) => s.node.id && s.node.id === sliceRoot.id)
    if (origin) rootMarkMap[sliceRoot._layoutMark] = origin.mark
  }

  const meta = {
    id: component.id,
    name: component.name || '',
    pageDir: path.resolve(pageDir),
    pageLayoutMarks: marks,
    rootMarkMap,
    markMapNote:
      '切片内 mark 从 L0 重编号；整页 mark = 将切片 mark 的根前缀按 rootMarkMap 替换（如 rootMarkMap 中 L0→L0.1.0，则切片 L0.2.1 对应整页 L0.1.0.2.1）',
    bbox: component.bbox || null,
    generatedAt: new Date().toISOString(),
  }
  const metaPath = path.join(outDir, 'component-meta.json')
  fs.writeFileSync(metaPath, `${JSON.stringify(meta, null, 2)}\n`, 'utf8')

  return { outDir, result, metaPath, rootMarkMap }
}

function main() {
  const args = parseArgs(process.argv)
  const pageDir = args['page-dir'] && args['page-dir'] !== true ? path.resolve(args['page-dir']) : null
  const componentId = args.component && args.component !== true ? args.component : null

  if (!pageDir || (!componentId && !args.all)) {
    console.error(
      '用法: node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-slice-dsl.cjs --page-dir d-d2c/temp/<page-dir> --component <id>（或 --all）'
    )
    process.exit(1)
  }

  const dslFullPath = path.join(pageDir, 'dsl_full.json')
  const markedPath = path.join(pageDir, 'dsl_layout_marked.json')
  for (const p of [dslFullPath, markedPath]) {
    if (!fs.existsSync(p)) {
      console.error(`❌ 找不到 ${p}（须先运行 fetch-mastergo-dsl.cjs + filter-dsl.cjs）`)
      process.exit(1)
    }
  }

  const markedObj = readJson(markedPath)
  const markedDsl = markedObj.dsl !== undefined ? markedObj.dsl : markedObj
  const markedNodes = markedDsl.nodes || []

  const { components, source } = loadComponents(pageDir)
  const targets = args.all
    ? components
    : components.filter((c) => c.id === componentId)
  if (!targets.length) {
    console.error(`❌ 组件 ${componentId} 不在清单中（${source}）`)
    console.error(`   可用组件: ${components.map((c) => c.id).join(', ')}`)
    process.exit(1)
  }

  console.log(`===== d2c 组件 DSL 切片（清单: ${source}） =====`)
  for (const component of targets) {
    const { outDir, rootMarkMap } = sliceComponent(
      pageDir,
      component,
      markedNodes,
      dslFullPath
    )
    console.log(`✅ ${component.id} ${component.name || ''}`)
    console.log(`   切片目录  : ${outDir}`)
    console.log(`   mark 映射 : ${JSON.stringify(rootMarkMap)}`)
  }
  console.log('')
  console.log('⚠️ 实现组件时只读该组件 components/<id>/ 下的切片产物 + 公共映射表；禁止读整页 dom.md / styles.md。')
}

main()
