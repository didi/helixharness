#!/usr/bin/env node
/* eslint-disable no-console */
/**
 * d2c-complexity-plan.cjs — 页面复杂度量化判定 + 建议拆分点
 *
 * 读取 filter-dsl.cjs 产物（dsl_layout_marked.json / flex-groups.json / mixed-text.json），
 * 输出 complexity-plan.json：metrics + verdict（simple|complex）+ suggestedComponents。
 * verdict 以脚本结论为准，Agent 禁止口头推翻（见 rules/d-d2c-complex-split.md）。
 *
 * 判定标准（2026-06-12 用 3 个真实标注样本校准，file=188838996770122）：
 *   - 节点总数 / 深度 / 文本数区分不开简单与复杂（简单页"首页工作台"154 节点 vs
 *     复杂页"核保中"156 节点几乎同体量）；真正的复杂度信号是结构嵌套与多样性：
 *     重复卡片列表（一套结构 v-for 多次）对生成是简单的，嵌套表格/键值区/混排才是还原度杀手。
 *   - 命中任一阈值即 complex（漏判复杂代价是还原度差，误判简单只是多走流程，取敏感方向）：
 *       flexGroupCount  ≥ 10   （简单组 4 vs 复杂组 30）
 *       mixedTextCount  ≥ 4    （1 vs 8）
 *       uniqStructSigs  ≥ 30   （22 vs 36）
 *       nodeCount       ≥ 250  （体量兜底护栏，超出校准样本覆盖范围时宁可走拆解路径）
 *
 * 用法：
 *   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-complexity-plan.cjs --page-dir d-d2c/temp/<page-dir>
 * 可选阈值覆盖：--th-flex-groups 10 --th-mixed-text 4 --th-struct-sigs 30 --th-node-count 250
 */
const fs = require('fs')
const path = require('path')

const DEFAULT_THRESHOLDS = {
  flexGroupCount: 10,
  mixedTextCount: 4,
  uniqStructSigs: 30,
  nodeCount: 250,
}

/** 单组件节点预算：约等于已验证可一次性高保真生成的"简单页"体量 */
const DEFAULT_MAX_COMPONENT_NODES = 80

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

function readJsonSafe(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'))
  } catch (_) {
    return null
  }
}

function getBBoxLocal(node) {
  const ls = node.layoutStyle || {}
  return {
    x: Number(ls.relativeX || 0),
    y: Number(ls.relativeY || 0),
    w: Number(ls.width || 0),
    h: Number(ls.height || 0),
  }
}

/** 遍历 marked 节点树，统计体量/深度/结构多样性指标 */
function computeTreeMetrics(markedNodes) {
  let nodeCount = 0
  let textNodeCount = 0
  let maxDepth = 0
  const structSigs = new Set()

  function walk(node, depth) {
    if (!node) return
    nodeCount += 1
    maxDepth = Math.max(maxDepth, depth)
    if (node.type === 'TEXT') textNodeCount += 1
    const children = node.children || []
    if (children.length) {
      // 结构签名：节点类型 + 直接子节点类型序列；重复卡片列表只算一种结构
      structSigs.add(`${node.type}:${children.map((c) => c.type).join(',')}`)
    }
    for (const child of children) walk(child, depth + 1)
  }
  for (const n of markedNodes || []) walk(n, 1)

  return { nodeCount, textNodeCount, maxDepth, uniqStructSigs: structSigs.size }
}

function countNodesInSubtree(node) {
  let total = 0
  function walk(n) {
    if (!n) return
    total += 1
    for (const c of n.children || []) walk(c)
  }
  walk(node)
  return total
}

/**
 * 按顶层功能区生成建议拆分点：取 mark 深度为 2（L0.x）的节点，
 * 相邻且同属一个 _functionalRegion 的归为一个组件。
 * 单组件节点数超过预算（maxComponentNodes，默认 80，约等于已验证可一次性
 * 高保真生成的"简单页"体量）时自动下钻一层拆其子节点，直到不超或无法再拆。
 */
function buildSuggestedComponents(markedNodes, maxComponentNodes) {
  const sectionNodes = []
  for (const root of markedNodes || []) {
    for (const child of root.children || []) {
      if (child._layoutMark) sectionNodes.push(child)
    }
  }

  let groups = []
  let current = null
  for (const node of sectionNodes) {
    const region = node._functionalRegion || null
    if (!current || (region !== current.region || region === null)) {
      current = { region, nodes: [] }
      groups.push(current)
    }
    current.nodes.push(node)
  }

  // 超预算组件下钻：多节点组先拆成单节点组；单节点组再下钻其有 mark 的子节点
  function groupNodeCount(group) {
    return group.nodes.reduce((sum, n) => sum + countNodesInSubtree(n), 0)
  }
  let changed = true
  while (changed) {
    changed = false
    const next = []
    for (const group of groups) {
      if (groupNodeCount(group) <= maxComponentNodes) {
        next.push(group)
        continue
      }
      if (group.nodes.length > 1) {
        for (const node of group.nodes) {
          next.push({ region: group.region, nodes: [node] })
        }
        changed = true
        continue
      }
      const markedChildren = (group.nodes[0].children || []).filter(
        (c) => c._layoutMark
      )
      if (!markedChildren.length) {
        next.push(group) // 叶子级大块无法再拆，保留并由 Agent 微调
        continue
      }
      for (const child of markedChildren) {
        next.push({ region: child.name || group.region, nodes: [child] })
      }
      changed = true
    }
    groups = next
  }

  return groups.map((group, index) => {
    let bbox = null
    let nodeCount = 0
    for (const node of group.nodes) {
      const b = getBBoxLocal(node)
      nodeCount += countNodesInSubtree(node)
      if (!bbox) {
        bbox = { x: b.x, y: b.y, w: b.w, h: b.h }
      } else {
        const right = Math.max(bbox.x + bbox.w, b.x + b.w)
        const bottom = Math.max(bbox.y + bbox.h, b.y + b.h)
        bbox.x = Math.min(bbox.x, b.x)
        bbox.y = Math.min(bbox.y, b.y)
        bbox.w = right - bbox.x
        bbox.h = bottom - bbox.y
      }
    }
    return {
      id: `c${index + 1}`,
      name:
        group.region ||
        group.nodes.map((n) => n.name).filter(Boolean).join('+') ||
        `section-${index + 1}`,
      layoutMarks: group.nodes.map((n) => n._layoutMark),
      bbox,
      nodeCount,
    }
  })
}

/**
 * 复杂度判定主函数（filter-dsl.cjs 末尾自动调用；也可 CLI 单独执行）。
 * @param {string} pageDir filter-dsl 产物目录
 * @param {{thresholds?: object}} opts
 */
function runComplexityPlan(pageDir, opts = {}) {
  const markedPath = path.join(pageDir, 'dsl_layout_marked.json')
  if (!fs.existsSync(markedPath)) {
    throw new Error(`找不到 ${markedPath}（须先运行 filter-dsl.cjs）`)
  }
  const markedObj = JSON.parse(fs.readFileSync(markedPath, 'utf8'))
  const markedDsl = markedObj.dsl !== undefined ? markedObj.dsl : markedObj
  const markedNodes = markedDsl.nodes || []

  const flexGroups = readJsonSafe(path.join(pageDir, 'flex-groups.json'))
  const mixedText = readJsonSafe(path.join(pageDir, 'mixed-text.json'))

  const treeMetrics = computeTreeMetrics(markedNodes)
  const metrics = {
    ...treeMetrics,
    flexGroupCount:
      (flexGroups &&
        (flexGroups.count ??
          (flexGroups.items || flexGroups.groups || []).length)) ||
      0,
    mixedTextCount:
      (mixedText && (mixedText.count ?? (mixedText.items || []).length)) || 0,
  }

  const thresholds = { ...DEFAULT_THRESHOLDS, ...(opts.thresholds || {}) }
  const reasons = []
  for (const [key, limit] of Object.entries(thresholds)) {
    if (Number(metrics[key] || 0) >= limit) {
      reasons.push(`${key}=${metrics[key]} ≥ ${limit}`)
    }
  }
  const verdict = reasons.length ? 'complex' : 'simple'

  const plan = {
    version: '1.0',
    generatedAt: new Date().toISOString(),
    pageDir: path.resolve(pageDir),
    metrics,
    thresholds,
    verdict,
    reasons,
    note:
      verdict === 'complex'
        ? '复杂页：按 rules/d-d2c-complex-split.md 拆解为组件串行实现；Agent 禁止口头推翻 verdict'
        : '简单页：按 rules/d-d2c-generate.md 现有路径一次性生成；Agent 禁止口头推翻 verdict',
    suggestedComponents:
      verdict === 'complex'
        ? buildSuggestedComponents(
            markedNodes,
            Number(opts.maxComponentNodes) || DEFAULT_MAX_COMPONENT_NODES
          )
        : [],
  }

  const outPath = path.join(pageDir, 'complexity-plan.json')
  fs.writeFileSync(outPath, `${JSON.stringify(plan, null, 2)}\n`, 'utf8')
  return { plan, outPath }
}

function main() {
  const args = parseArgs(process.argv)
  const pageDir = args['page-dir'] && args['page-dir'] !== true ? args['page-dir'] : null
  if (!pageDir) {
    console.error(
      '用法: node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-complexity-plan.cjs --page-dir d-d2c/temp/<page-dir>'
    )
    process.exit(1)
  }

  const thresholds = {}
  const flagMap = {
    'th-flex-groups': 'flexGroupCount',
    'th-mixed-text': 'mixedTextCount',
    'th-struct-sigs': 'uniqStructSigs',
    'th-node-count': 'nodeCount',
  }
  for (const [flag, key] of Object.entries(flagMap)) {
    if (args[flag] && args[flag] !== true) thresholds[key] = Number(args[flag])
  }

  const { plan, outPath } = runComplexityPlan(path.resolve(pageDir), {
    thresholds,
    maxComponentNodes:
      args['max-component-nodes'] && args['max-component-nodes'] !== true
        ? Number(args['max-component-nodes'])
        : undefined,
  })
  console.log('===== d2c 复杂度判定 =====')
  console.log(`   指标      : ${JSON.stringify(plan.metrics)}`)
  console.log(`   判定      : ${plan.verdict}${plan.reasons.length ? `（${plan.reasons.join('; ')}）` : ''}`)
  if (plan.suggestedComponents.length) {
    console.log(`   建议拆分  : ${plan.suggestedComponents.length} 个组件`)
    for (const c of plan.suggestedComponents) {
      console.log(
        `     - ${c.id} ${c.name}（${c.layoutMarks.join(', ')}，${c.nodeCount} 节点）`
      )
    }
  }
  console.log(`   输出      : ${outPath}`)
}

if (require.main === module) {
  main()
}

module.exports = { runComplexityPlan, DEFAULT_THRESHOLDS }
