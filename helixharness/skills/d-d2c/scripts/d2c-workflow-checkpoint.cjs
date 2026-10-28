#!/usr/bin/env node
/* eslint-disable no-console */

/**
 * d2c-workflow-checkpoint.cjs
 *
 * 贯穿 d-d2c 自检评测工作流的阶段状态追踪脚本。
 * 每个阶段入口/出口必须调用此脚本，形成硬阻断。
 *
 * 用法：
 *   --enter <phase>                进入阶段（校验前置已完成）
 *   --complete <phase>             标记阶段完成
 *   --component-complete <id>      复杂页：标记单个组件小闭环完成（阶段 1 内）
 *   --ack <理由>                   组件 geometry-diff 有 violations 时的不修理由（配合 --component-complete）
 *   --status                       输出当前状态和下一步
 *   --state-dir <dir>              状态文件所在目录（默认 d-d2c/temp）
 *   --page-dir <name>              页面目录名
 *   --round <N>                    当前轮次
 *   --mode <create-page|update-page|component>
 *                                  工作流模式；update-page 会启用已有页源码落位闸
 *   --placement-check <path>       update-page 的 placement-check.json 路径（默认自动查找）
 *   --report <path>                评测报告路径（阶段 3 --complete 时必传）
 *   --score <N>                    还原度分数（阶段 3 --complete 时必传）
 *   --decision <continue|cancel>   用户决策（阶段 4 --complete 时必传）
 *   --pause <external-input|user-cancelled>
 *                                  记录允许停止的显式暂停（必须配合 --reason）
 *   --resume                       清除显式暂停并继续当前阶段
 *   --print-report                 阶段 3 complete 时自动打印中文摘要（默认开启）
 *   --no-print-report              跳过自动打印
 *
 * 阶段 1 --complete 产物校验（硬阻断）：
 *   d-d2c/temp/<page-dir>/ 下必须存在且非空：
 *   dsl_full.json, Layer.md, dom.md, styles.md,
 *   dsl_layout_marked.json, dsl_visual.json
 *   复杂页（complexity-plan.json verdict=complex）额外要求：
 *   component-plan.json 存在，且其全部组件均已 --component-complete
 */

const fs = require('fs')
const path = require('path')
const crypto = require('crypto')
const { execSync } = require('child_process')
const {
  ALLOWED_PAUSE_KINDS,
  isHeadlessEnv,
  signalForAction,
  syncActiveWorkflow,
} = require('./lib/d2c-workflow-guard-core.cjs')
const {
  validateHealthEvidence,
  runtimeRepairSentinel,
} = require('./lib/d2c-runtime-health-core.cjs')
const { classifyPreviewEntryFailure } = require('./lib/d2c-preview-entry-core.cjs')
// 结构化运行日志（非侵入、可降级；require 失败也不影响主流程）
let getLogger = () => ({ enabled: false })
try { ({ getLogger } = require('./lib/d2c-run-log.cjs')) } catch { /* 日志模块缺失时静默降级 */ }

function safe(fn) {
  try {
    return fn()
  } catch {
    return undefined
  }
}

/** 阶段 1（代码还原）--complete 前必须存在的 DSL 物料 */
const PHASE1_REQUIRED_ARTIFACTS = [
  'dsl_full.json',
  'Layer.md',
  'dom.md',
  'styles.md',
  'dsl_layout_marked.json',
  'dsl_visual.json',
  'mixed-text.json',
  'text-whitespace.json',
  'page-flow-plan.json',
  'host-layout.json',
  'layout-h5.json',
]

const PREVIEW_LEAK_PATTERNS = [
  {
    label: 'preview marker',
    re: /D2C[-_ ]?PREVIEW|__D2C_PREVIEW_BOOTSTRAP_BYPASS__|data-d2c-preview-bootstrap-bypass/i,
  },
  {
    label: 'preview route',
    re: /(?:^|[/"'`_\-\s])(d2c|d-d2c)-preview(?:[/"'`_\-\s]|$)/i,
  },
  {
    label: 'preview harness marker',
    re: /preview-harness|preview-bootstrap-bypass/i,
  },
]

function isIgnoredPreviewArtifactPath(filePath) {
  return /(^|\/)d-d2c\/temp\//.test(String(filePath || '').replace(/\\/g, '/'))
}

// ─── 参数解析 ───────────────────────────────────────
function parseArgs(argv) {
  const args = {}
  for (let i = 2; i < argv.length; i++) {
    const token = argv[i]
    if (token.startsWith('--')) {
      const key = token.slice(2)
      if (key === 'no-print-report') {
        args['print-report'] = false
        continue
      }
      const next = argv[i + 1]
      if (next && !next.startsWith('--')) {
        args[key] = next
        i++
      } else {
        args[key] = true
      }
    }
  }
  return args
}

// ─── 阶段定义 ───────────────────────────────────────
const PHASES = {
  0: { name: '初始化', prev: null },
  1: { name: '代码还原', prev: 0 },
  2: { name: '物料生成', prev: 1 },
  3: { name: '结果评测', prev: 2 },
  4: { name: '用户决策', prev: 3 },
  6: { name: '最终摘要', prev: 4 },
}

const NEXT_STEP = {
  0: '执行 --enter 1 进入代码还原阶段',
  1: '执行 --enter 2 进入物料生成阶段',
  2: '执行 --enter 3 进入结果评测阶段',
  3: '执行以下步骤：\n  1) 阅读上方中文评测摘要\n  2) 立即执行 --enter 4 进入用户决策阶段\n  3) 再使用 AskUserQuestion 工具向用户确认是否继续优化',
  4: '根据用户决策：continue → 修改代码后 --enter 2 开始下一轮；cancel → --enter 6 输出最终摘要',
  6: '输出最终完成摘要（阶段 6 格式）',
}

// ─── 状态文件读写 ────────────────────────────────────
function stateFilePath(stateDir, pageDir) {
  return path.join(stateDir, pageDir, 'workflow-state.json')
}

function readState(filePath) {
  if (!fs.existsSync(filePath)) {
    return { phases: {}, currentPhase: null, round: 1 }
  }
  return JSON.parse(fs.readFileSync(filePath, 'utf8'))
}

function writeState(filePath, state) {
  const dir = path.dirname(filePath)
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(filePath, JSON.stringify(state, null, 2), 'utf8')
}

function pageArtifactDir(stateDir, pageDir) {
  return path.join(stateDir, pageDir)
}

function syncWorkflowGuard(stateDir, pageDir, state, stateFile, printSignal = false, required = false) {
  if (!state || !pageDir) return null
  try {
    const result = syncActiveWorkflow({
      stateDir,
      pageDir,
      state,
      stateFile,
      headless: isHeadlessEnv(process.env),
      scoreThreshold: process.env.D2C_SCORE_THRESHOLD,
    })
    if (printSignal && result && result.marker) {
      const signal = signalForAction(result.marker.nextAction)
      if (signal) console.log(signal)
    }
    return result
  } catch (err) {
    console.error(`⚠️ d-d2c workflow guard 状态同步失败：${err.message}`)
    if (required) throw err
    return null
  }
}

function persistWorkflowState(stateFile, stateDir, pageDir, state, printSignal = false) {
  writeState(stateFile, state)
  syncWorkflowGuard(stateDir, pageDir, state, stateFile, printSignal, true)
}

function persistStateSnapshot(stateDir, pageDir, state) {
  if (!state || !pageDir) return
  try {
    const file = stateFilePath(stateDir, pageDir)
    persistWorkflowState(file, stateDir, pageDir, state)
  } catch (err) {
    console.error(`⚠️ workflow-state.json 写入失败：${err.message}`)
  }
}

function nextPhase2Round(currentRound, consumeRound = true) {
  return Number(currentRound) + (consumeRound ? 1 : 0)
}

function recordPhase2AutoFixBlock(state, gate, reason = 'coordinate-gate-failed', extra = {}) {
  if (!state) return null
  const {
    currentRound: requestedCurrentRound,
    nextRound: requestedNextRound,
    consumeRound: requestedConsumeRound,
    ...extraFields
  } = extra
  const currentRound = Number(gate && gate.roundNum) || Number(requestedCurrentRound) || Number(state.round) || 1
  const consumeRound = requestedConsumeRound !== false
  const explicitNextRound = Number(requestedNextRound)
  const nextRound = Number.isFinite(explicitNextRound) && explicitNextRound > 0
    ? explicitNextRound
    : nextPhase2Round(currentRound, consumeRound)
  const failing = gate && Array.isArray(gate.failing) ? gate.failing : []
  const top = gate && Array.isArray(gate.top) ? gate.top : []
  const maxDiffPx = failing.length ? Math.max(...failing.map((f) => Math.abs(Number(f.diffPx) || 0))) : 0
  state.phase2Block = {
    reason,
    currentRound,
    nextRound,
    tolerancePx: gate && gate.tolerancePx ? gate.tolerancePx : GEOMETRY_GATE_TOLERANCE_PX,
    maxRounds: gate && gate.maxRounds ? gate.maxRounds : GEOMETRY_GATE_MAX_ROUNDS,
    failingCount: failing.length,
    maxDiffPx,
    repairPlan: gate && gate.repairPlan ? gate.repairPlan : extra.repairPlan || null,
    top,
    askUser: false,
    allowAcceptCurrent: false,
    allowEvaluator: false,
    nextAction: 'auto-fix-and-regenerate-materials',
    updatedAt: new Date().toISOString(),
    ...extraFields,
    currentRound,
    nextRound,
    consumeRound,
  }
  return state.phase2Block
}

function clearPhase2AutoFixBlock(state) {
  if (state && state.phase2Block) delete state.phase2Block
}

function printPhase2AutoFixSentinel(block, printer = console.error) {
  if (!block) return
  printer(
    `D2C_AUTO_FIX_REQUIRED phase=2 reason=${block.reason || 'phase2-blocked'} currentRound=${block.currentRound || '?'} nextRound=${block.nextRound || '?'} consumeRound=${block.consumeRound !== false} askUser=false allowAccept=false allowEvaluator=false`
  )
  if (block.repairPlan && block.repairPlan.strategy) {
    printer(`D2C_REPAIR_PLAN strategy=${block.repairPlan.strategy} priority=${block.repairPlan.priority || 'normal'}`)
    for (const action of (block.repairPlan.actions || []).slice(0, 4)) printer(`  action: ${action}`)
    for (const forbidden of (block.repairPlan.forbiddenActions || []).slice(0, 3)) printer(`  forbidden: ${forbidden}`)
  }
}

function formatCoordinateRepairPlanSummary(plan) {
  if (!plan || !plan.strategy) return 'none'
  const secondary = (plan.secondaryStrategies || []).length ? ` secondary=${plan.secondaryStrategies.join(',')}` : ''
  return `${plan.strategy}${secondary}`
}

function printCoordinateRepairPlan(plan, printer = console.error) {
  if (!plan || !plan.strategy) return
  printer(`   repairPlan: ${formatCoordinateRepairPlanSummary(plan)} priority=${plan.priority || 'normal'}`)
  ;(plan.evidence || []).slice(0, 4).forEach((x) => printer(`     evidence: ${x}`))
  ;(plan.actions || []).slice(0, 5).forEach((x) => printer(`     action: ${x}`))
  ;(plan.forbiddenActions || []).slice(0, 4).forEach((x) => printer(`     forbidden: ${x}`))
}

function readJsonArtifact(filePath) {
  return safe(() => JSON.parse(fs.readFileSync(filePath, 'utf8')))
}

function readComponentPlan(stateDir, pageDir) {
  const base = pageArtifactDir(stateDir, pageDir)
  const planPath = path.join(base, 'component-plan.json')
  if (!fs.existsSync(planPath)) return null
  try {
    return JSON.parse(fs.readFileSync(planPath, 'utf8'))
  } catch {
    return null
  }
}

function countDslNodes(nodes) {
  if (!Array.isArray(nodes)) return 0
  let total = 0
  for (const node of nodes) {
    if (!node || typeof node !== 'object') continue
    total += 1
    total += countDslNodes(node.children)
  }
  return total
}

function dslNodeCount(payload) {
  if (!payload || typeof payload !== 'object') return 0
  const dsl = payload.dsl && typeof payload.dsl === 'object' ? payload.dsl : payload
  return countDslNodes(dsl.nodes)
}

function validatePhase1DslSemantics(dir) {
  const checks = [
    { file: 'dsl_full.json', label: '完整 DSL' },
    { file: 'dsl_visual.json', label: '视觉 DSL' },
    { file: 'dsl_layout_marked.json', label: '布局标记 DSL' },
  ]
  const failures = []
  for (const item of checks) {
    const filePath = path.join(dir, item.file)
    const payload = readJsonArtifact(filePath)
    const nodeCount = dslNodeCount(payload)
    if (!payload) {
      failures.push(`${item.file}: JSON 解析失败`)
    } else if (nodeCount <= 0) {
      const keys = payload && typeof payload === 'object' && !Array.isArray(payload) ? Object.keys(payload).join(',') : ''
      failures.push(`${item.file}: ${item.label}节点数为 0${keys ? `（top-level keys: ${keys}）` : ''}`)
    }
  }
  return failures
}

/** 阶段 1 完成前校验 DSL 物料是否齐全（见 d-d2c-generate.md §3） */
function validatePhase1Artifacts(stateDir, pageDir) {
  if (!pageDir) {
    console.error('❌ 阶段 1 --complete 必须传 --page-dir <name>')
    process.exit(1)
  }

  const dir = pageArtifactDir(stateDir, pageDir)
  const missing = []
  const empty = []

  PHASE1_REQUIRED_ARTIFACTS.forEach((file) => {
    const filePath = path.join(dir, file)
    if (!fs.existsSync(filePath)) {
      missing.push(file)
      return
    }
    if (fs.statSync(filePath).size === 0) {
      empty.push(file)
    }
  })

  const semanticFailures = !missing.length && !empty.length
    ? validatePhase1DslSemantics(dir)
    : []
  if (!missing.length && !empty.length) {
    try {
      const host = JSON.parse(
        fs.readFileSync(path.join(dir, 'host-layout.json'), 'utf8')
      )
      const layout = JSON.parse(
        fs.readFileSync(path.join(dir, 'layout-h5.json'), 'utf8')
      )
      const allowed = new Set(['none', 'immersive-overlay', 'stacked-host'])
      if (!allowed.has(host.policy) || layout.hostLayoutPolicy !== host.policy) {
        semanticFailures.push(
          `宿主策略无效或不一致（host-layout=${host.policy || 'missing'}, layout-h5=${layout.hostLayoutPolicy || 'missing'}）`
        )
      }
      if (Number(layout.schemaVersion || 0) < 2) {
        semanticFailures.push('layout-h5.json.schemaVersion 必须 >= 2')
      }
      const shiftY = Number(layout.layoutShiftY || 0)
      if (
        host.policy === 'immersive-overlay' &&
        (shiftY !== 0 ||
          Number(layout.designCropTopDsl || 0) !== 0 ||
          Number(layout.designCropTopRaster || 0) !== 0)
      ) {
        semanticFailures.push('immersive-overlay 必须保留原始坐标且不得裁顶')
      }
      if (
        host.policy === 'stacked-host' &&
        Math.abs(shiftY - Number(host.hostChromeBottomY || 0)) > 1
      ) {
        semanticFailures.push('stacked-host 必须按 hostChromeBottomY 平移')
      }
    } catch (err) {
      semanticFailures.push(`宿主布局产物解析失败：${err.message}`)
    }
  }

  if (!missing.length && !empty.length && !semanticFailures.length) {
    console.log(`✅ 阶段 1 产物校验通过（${PHASE1_REQUIRED_ARTIFACTS.length} 项）`)
    return
  }

  console.error('❌ 阶段 1（代码还原）产物校验失败，禁止标记完成。')
  console.error(`   目录: ${path.resolve(dir)}`)
  if (missing.length) {
    console.error(`   缺失: ${missing.join(', ')}`)
  }
  if (empty.length) {
    console.error(`   空文件: ${empty.join(', ')}`)
  }
  if (semanticFailures.length) {
    console.error(`   DSL 语义失败: ${semanticFailures.join('; ')}`)
  }
  console.error('')
  console.error('请先补齐 DSL 物料：')
  console.error('  # 1) 拉取完整 DSL（脚本优先；失败则 MCP 原始响应落盘后用脚本规范化/组装）')
  console.error(`  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/fetch-mastergo-dsl.cjs \\`)
  console.error(`    --mg-url '<MasterGo设计页链接>' \\`)
  console.error(`    --out d-d2c/temp/${pageDir}/dsl_full.json`)
  console.error('')
  console.error('  # 2) 若脚本退出码 2：按 .dsl-fetch-fallback.json 调用 MCP，不要手写 dsl_full.json')
  console.error(`  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/normalize-mastergo-dsl.cjs --input d-d2c/temp/${pageDir}/mcp-getDsl-response.json --out d-d2c/temp/${pageDir}/dsl_full.json`)
  console.error(`  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/assemble-mastergo-sections.cjs --overview d-d2c/temp/${pageDir}/mcp-sections/overview.json --sections-dir d-d2c/temp/${pageDir}/mcp-sections --out d-d2c/temp/${pageDir}/dsl_full.json`)
  console.error('')
  console.error('  # 3) 压缩 DSL / 生成布局与样式物料')
  console.error(`  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/filter-dsl.cjs \\`)
  console.error(`    --input d-d2c/temp/${pageDir}/dsl_full.json \\`)
  console.error(`    --output d-d2c/temp/${pageDir}`)
  process.exit(1)
}

function validatePhase1RuntimeHealth(stateDir, pageDir) {
  const file = path.join(pageArtifactDir(stateDir, pageDir), 'compile-check.json')
  if (!fs.existsSync(file)) {
    console.error('❌ 阶段 1 缺少真实 dev server 编译健康证据，禁止标记完成。')
    console.error(`   缺失: ${path.resolve(file)}`)
    console.error('   HTTP 200 或端口可连接不等于 webpack/vite 已编译成功。请先运行：')
    console.error('  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-runtime-health-check.cjs \\')
    console.error("    --url '<真实项目完整URL>' \\")
    console.error(`    --page-dir d-d2c/temp/${pageDir}`)
    console.error(runtimeRepairSentinel({ failureClass: 'compile-check-missing' }))
    process.exit(1)
  }

  let evidence
  try {
    evidence = JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch (error) {
    console.error(`❌ compile-check.json 解析失败：${error.message}`)
    console.error(runtimeRepairSentinel({ failureClass: 'compile-check-invalid' }))
    process.exit(1)
  }

  const problems = validateHealthEvidence(evidence, { cwd: process.cwd() })
  if (evidence.checkedBy !== 'd2c-runtime-health-check') {
    problems.push('compile-check.json 不是由 d2c-runtime-health-check.cjs 生成')
  }
  if (evidence.appMounted !== true) {
    problems.push('真实项目 SPA 根节点未成功挂载')
  }
  if (evidence.compileStatus !== 'passed') {
    problems.push(`compileStatus=${evidence.compileStatus || 'missing'}，未证明项目编译成功`)
  }

  if (!problems.length) {
    console.log(
      `✅ 阶段 1 编译健康闸通过（source=${evidence.sourceFingerprint.digest.slice(0, 12)} final=${evidence.finalUrl || '?'}）`
    )
    return evidence
  }

  console.error('❌ 阶段 1 编译健康闸未通过，禁止进入截图或 browser handoff。')
  problems.forEach((problem) => console.error(`   - ${problem}`))
  if (Array.isArray(evidence.signals) && evidence.signals.length) {
    console.error(`   - signals=${evidence.signals.map((item) => item.code).join(',')}`)
  }
  console.error(runtimeRepairSentinel(evidence))
  console.error('请修复编译/runtime/dev-server 路由问题后重新运行 health check；不得询问用户操作浏览器。')
  process.exit(1)
}

function readProjectPackageJson() {
  return safe(() => JSON.parse(fs.readFileSync(path.resolve('package.json'), 'utf8'))) || {}
}

function addStyleLangFromWebpackConfig(supported) {
  const configFiles = [
    'vue.config.js',
    'webpack.config.js',
    'webpack/config/utils.js',
    'webpack/config/webpack.base.conf.js',
    'webpack/config/webpack.dev.conf.js',
  ]
  const text = configFiles
    .map((file) => safe(() => fs.readFileSync(path.resolve(file), 'utf8')) || '')
    .join('\n')
    .toLowerCase()
  if (!text) return
  if (/\bstyl(us)?\b|stylus-loader/.test(text)) {
    supported.add('stylus')
    supported.add('styl')
  }
  if (/\bless\b|less-loader/.test(text)) supported.add('less')
  if (/\bsass\b|\bscss\b|sass-loader|node-sass/.test(text)) {
    supported.add('sass')
    supported.add('scss')
  }
}

function addStyleLangFromTrackedVueFiles(supported) {
  const out = safe(() =>
    execSync('git ls-files "*.vue"', {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    })
  ) || ''
  const counts = new Map()
  const styleTagRe = /<style\b([^>]*)>/gi
  for (const file of out.split('\n').map((s) => s.trim()).filter(Boolean)) {
    if (!fs.existsSync(file)) continue
    const text = safe(() => fs.readFileSync(file, 'utf8')) || ''
    let match
    while ((match = styleTagRe.exec(text))) {
      const attrs = match[1] || ''
      const langMatch = attrs.match(/\blang\s*=\s*["']?([^"'\s>]+)/i)
      const lang = langMatch ? String(langMatch[1]).trim().toLowerCase() : ''
      if (lang) counts.set(lang, (counts.get(lang) || 0) + 1)
    }
  }
  for (const [lang, count] of counts.entries()) {
    if (count >= 3) supported.add(lang)
  }
}

function detectSupportedVueStyleLangs() {
  const pkg = readProjectPackageJson()
  const deps = {
    ...(pkg.dependencies || {}),
    ...(pkg.devDependencies || {}),
  }
  const supported = new Set(['', 'css'])
  if (deps.stylus || deps['stylus-loader']) {
    supported.add('stylus')
    supported.add('styl')
  }
  if (deps.less || deps['less-loader']) supported.add('less')
  if (deps.sass || deps['sass-loader'] || deps['node-sass']) {
    supported.add('sass')
    supported.add('scss')
  }
  addStyleLangFromWebpackConfig(supported)
  addStyleLangFromTrackedVueFiles(supported)
  return supported
}

function changedVueFiles() {
  const out = safe(() =>
    execSync('git status --short -- "*.vue"', {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    })
  ) || ''
  return out
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const raw = line.slice(3).trim()
      const renamed = raw.includes(' -> ') ? raw.split(' -> ').pop().trim() : raw
      return renamed.replace(/^"|"$/g, '')
    })
    .filter((file) => file && fs.existsSync(file) && fs.statSync(file).isFile())
}

function decodeHtmlEntities(value) {
  return String(value || '')
    .replace(/&#x([0-9a-f]+);/gi, (_, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#([0-9]+);/g, (_, dec) => String.fromCodePoint(parseInt(dec, 10)))
    .replace(/&nbsp;/gi, '\u00a0')
    .replace(/&quot;/gi, '"')
    .replace(/&apos;|&#39;/gi, "'")
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&amp;/gi, '&')
}

function staticTemplateText(value) {
  return decodeHtmlEntities(String(value || '').replace(/<[^>]+>/g, '')).replace(/\r\n/g, '\n')
}

function findMarkedElementBodies(source, mark) {
  const bodies = []
  const escapedMark = escapeRegExp(mark)
  const openRe = new RegExp(
    `<([A-Za-z][\\w-]*)\\b[^>]*\\bdata-d2c-mark\\s*=\\s*["']${escapedMark}["'][^>]*>`,
    'gi'
  )
  let openMatch
  while ((openMatch = openRe.exec(String(source || '')))) {
    const tag = openMatch[1]
    const bodyStart = openRe.lastIndex
    const tokenRe = new RegExp(`<\\/?${escapeRegExp(tag)}\\b[^>]*>`, 'gi')
    tokenRe.lastIndex = bodyStart
    let depth = 1
    let token
    while ((token = tokenRe.exec(source))) {
      const raw = token[0]
      if (/^<\//.test(raw)) depth -= 1
      else if (!/\/\s*>$/.test(raw)) depth += 1
      if (depth === 0) {
        bodies.push(source.slice(bodyStart, token.index))
        openRe.lastIndex = tokenRe.lastIndex
        break
      }
    }
  }
  return bodies
}

function collectIndexedSpans(body) {
  const spans = []
  const spanRe = /<span\b([^>]*)>([\s\S]*?)<\/span\s*>/gi
  let match
  while ((match = spanRe.exec(String(body || '')))) {
    const attrs = match[1] || ''
    const indexMatch = attrs.match(/\bdata-span-index\s*=\s*(?:"(\d+)"|'(\d+)'|(\d+))/i)
    if (!indexMatch) continue
    spans.push({
      index: Number(indexMatch[1] || indexMatch[2] || indexMatch[3]),
      text: staticTemplateText(match[2]),
    })
  }
  return spans
}

function validateMixedTextContractsFromSources(manifest, sources) {
  if (!manifest || manifest.hasMixedText !== true) return []
  const normalizedSources = (sources || []).map((source, index) => (
    typeof source === 'string'
      ? { file: `source-${index + 1}`, text: source }
      : { file: source.file || `source-${index + 1}`, text: String(source.text || '') }
  ))
  const problems = []

  for (const item of manifest.items || []) {
    if (!item || !item.layoutMark) continue
    const expected = (item.spans || [])
      .slice()
      .sort((a, b) => Number(a.index) - Number(b.index))
      .map((span, index) => ({
        index: Number.isFinite(Number(span.index)) ? Number(span.index) : index,
        text: String(span.text || '').replace(/\r\n/g, '\n'),
      }))
    const declaredSpanCount = Number(item.spanCount)
    if (!expected.length || (Number.isFinite(declaredSpanCount) && declaredSpanCount !== expected.length)) {
      problems.push(`${item.layoutMark}: mixed-text.json spanCount/spans 无效，须重新运行 filter-dsl.cjs`)
      continue
    }
    const candidates = []
    for (const source of normalizedSources) {
      for (const body of findMarkedElementBodies(source.text, item.layoutMark)) {
        candidates.push({ file: source.file, spans: collectIndexedSpans(body) })
      }
    }
    if (!candidates.length) {
      problems.push(`${item.layoutMark}: changed Vue 中缺少静态 data-d2c-mark="${item.layoutMark}" 文本测量盒`)
      continue
    }

    const valid = candidates.some((candidate) => {
      if (candidate.spans.length !== expected.length) return false
      return candidate.spans.every((span, index) => (
        span.index === expected[index].index && span.text === expected[index].text
      ))
    })
    if (valid) continue

    const observed = candidates[0]
    const actualSummary = observed.spans.length
      ? observed.spans.map((span) => `${span.index}:${JSON.stringify(span.text)}`).join(', ')
      : '未找到带 data-span-index 的 <span>'
    const expectedSummary = expected.map((span) => `${span.index}:${JSON.stringify(span.text)}`).join(', ')
    problems.push(
      `${item.layoutMark}: 混排 span 结构/顺序/文案不一致（${observed.file}）；expected=[${expectedSummary}] actual=[${actualSummary}]`
    )
  }
  return problems
}

function validateMixedTextImplementation(stateDir, pageDir, designScope = '') {
  const manifestPath = path.join(pageArtifactDir(stateDir, pageDir), 'mixed-text.json')
  const manifest = readJsonArtifact(manifestPath)
  if (!manifest) {
    console.error(`❌ mixed-text.json 解析失败：${manifestPath}；请重新运行 filter-dsl.cjs。`)
    process.exit(1)
  }
  if (manifest.hasMixedText !== true) return
  const scope = String(designScope || '').trim()
  const scopedManifest = scope
    ? {
        ...manifest,
        items: (manifest.items || []).filter((item) => {
          const mark = String(item && item.layoutMark || '')
          return mark === scope || mark.startsWith(`${scope}.`)
        }),
      }
    : manifest
  if (!(scopedManifest.items || []).length) return
  const files = changedVueFiles()
  const sources = files.map((file) => ({ file, text: safe(() => fs.readFileSync(file, 'utf8')) || '' }))
  const problems = validateMixedTextContractsFromSources(scopedManifest, sources)
  if (!problems.length) {
    console.log(`✅ 阶段 1 混排文本结构校验通过（${(scopedManifest.items || []).length} 项${scope ? `，scope=${scope}` : ''}）`)
    return
  }

  console.error('❌ 阶段 1（代码还原）混排文本结构校验失败，禁止把可确定的 DSL 丢失带入截图循环。')
  console.error(`   mixed-text: ${manifestPath}`)
  problems.forEach((problem) => console.error(`   - ${problem}`))
  console.error('请按 mixed-text.json / dom.md 在真实 TEXT 测量盒上保留 data-d2c-mark，并按索引顺序实现全部 data-span-index <span>；禁止把混排节点拍平成一段普通文本。')
  process.exit(1)
}

function validateVueStyleLangs() {
  const files = changedVueFiles()
  if (!files.length) return
  const supported = detectSupportedVueStyleLangs()
  const unsupported = []
  const styleTagRe = /<style\b([^>]*)>/gi
  for (const file of files) {
    const text = safe(() => fs.readFileSync(file, 'utf8')) || ''
    let match
    while ((match = styleTagRe.exec(text))) {
      const attrs = match[1] || ''
      const langMatch = attrs.match(/\blang\s*=\s*["']?([^"'\s>]+)/i)
      const lang = langMatch ? String(langMatch[1]).trim().toLowerCase() : ''
      if (lang && !supported.has(lang)) unsupported.push({ file, lang })
    }
  }
  if (!unsupported.length) return

  console.error('❌ 阶段 1（代码还原）样式语言校验失败，禁止带着编译失败风险进入截图。')
  console.error(`   当前项目已识别支持的 Vue style lang: ${Array.from(supported).filter(Boolean).join(', ') || 'css'}`)
  unsupported.forEach((item) => {
    console.error(`   - ${item.file}: <style lang="${item.lang}"> 但项目未安装对应 loader`)
  })
  console.error('')
  console.error('请改用项目已支持的样式语言（本项目通常为 lang="stylus" 或普通 CSS），并通过 dev server 确认不再出现 Failed to compile / Module parse failed 后再执行 --complete 1。')
  process.exit(1)
}

function packageNameFromSpecifier(spec) {
  const s = String(spec || '').trim()
  if (!s || s.startsWith('.') || s.startsWith('/') || s.startsWith('@/') || s.startsWith('~@/')) return null
  if (/^[A-Za-z]:[\\/]/.test(s)) return null
  if (s.startsWith('@')) {
    const parts = s.split('/')
    return parts.length >= 2 ? `${parts[0]}/${parts[1]}` : null
  }
  return s.split('/')[0] || null
}

function readProjectDependencyNames() {
  const pkg = readProjectPackageJson()
  return new Set([
    ...Object.keys(pkg.dependencies || {}),
    ...Object.keys(pkg.devDependencies || {}),
    ...Object.keys(pkg.peerDependencies || {}),
    ...Object.keys(pkg.optionalDependencies || {}),
  ])
}

function isBarePackageAvailable(pkgName, dependencyNames) {
  if (!pkgName) return true
  if (dependencyNames.has(pkgName)) return true
  return fs.existsSync(path.resolve('node_modules', pkgName))
}

function collectBareImportSpecifiers(text) {
  const specs = []
  const importFromRe = /\bimport\s+(?:type\s+)?[\s\S]*?\bfrom\s*["']([^"']+)["']/g
  const sideEffectRe = /\bimport\s*["']([^"']+)["']/g
  const requireRe = /\brequire\s*\(\s*["']([^"']+)["']\s*\)/g
  let match
  while ((match = importFromRe.exec(text))) specs.push(match[1])
  while ((match = sideEffectRe.exec(text))) specs.push(match[1])
  while ((match = requireRe.exec(text))) specs.push(match[1])
  return specs
}

function validateBareScriptImports() {
  const files = changedVueFiles()
  if (!files.length) return
  const dependencyNames = readProjectDependencyNames()
  const missing = []
  for (const file of files) {
    const text = safe(() => fs.readFileSync(file, 'utf8')) || ''
    for (const spec of collectBareImportSpecifiers(text)) {
      const pkgName = packageNameFromSpecifier(spec)
      if (!pkgName || isBarePackageAvailable(pkgName, dependencyNames)) continue
      missing.push({ file, spec, pkgName })
    }
  }
  if (!missing.length) return

  console.error('❌ 阶段 1（代码还原）依赖引用校验失败，禁止带着 Module not found 风险进入截图。')
  missing.forEach((item) => {
    console.error(`   - ${item.file}: import "${item.spec}" 但项目未安装依赖 ${item.pkgName}`)
  })
  console.error('')
  console.error('请改用项目已有写法/已有依赖；静态还原页优先使用普通 Options API，例如 `export default { name: "..." }`，不要引入项目不存在的 decorator / class-component 依赖。')
  process.exit(1)
}

function escapeRegExp(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function classNamesFromAttr(value) {
  return String(value || '')
    .split(/\s+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .filter((s) => !/[{}()[\]:'"`]/.test(s))
}

function findClassesForD2cMark(vueText, mark) {
  const classes = new Set()
  const tagRe = new RegExp(`<[^>]*\\bdata-d2c-mark\\s*=\\s*["']${escapeRegExp(mark)}["'][^>]*>`, 'gi')
  let match
  while ((match = tagRe.exec(vueText))) {
    const tag = match[0]
    const classMatch = tag.match(/\bclass\s*=\s*["']([^"']+)["']/i)
    if (classMatch) {
      for (const cls of classNamesFromAttr(classMatch[1])) classes.add(cls)
    }
  }
  return Array.from(classes)
}

function collectStyleText(vueText) {
  const chunks = []
  const styleRe = /<style\b[^>]*>([\s\S]*?)<\/style>/gi
  let match
  while ((match = styleRe.exec(vueText))) chunks.push(match[1] || '')
  return chunks.join('\n')
}

function extractClassRule(styleText, className) {
  if (!styleText || !className) return ''
  const cssRe = new RegExp(`\\.${escapeRegExp(className)}(?:\\s|[.#:[{>+~])[^{}]*\\{([\\s\\S]*?)\\}`, 'i')
  const cssMatch = styleText.match(cssRe)
  if (cssMatch) return cssMatch[1]

  const lines = styleText.split(/\r?\n/)
  const selectorRe = new RegExp(`^\\s*\\.${escapeRegExp(className)}(?:\\b|$|[.#:[>+~\\s])`)
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i]
    if (!selectorRe.test(line)) continue
    const baseIndent = (line.match(/^(\s*)/) || [''])[0].length
    const collected = []
    for (let j = i + 1; j < lines.length; j += 1) {
      const next = lines[j]
      if (!next.trim()) {
        collected.push(next)
        continue
      }
      const indent = (next.match(/^(\s*)/) || [''])[0].length
      if (indent <= baseIndent) break
      collected.push(next)
    }
    if (collected.length) return collected.join('\n')
  }
  return ''
}

function hasAbsolutePosition(ruleText) {
  return /\bposition\s*:?\s*(absolute|fixed)\b/i.test(String(ruleText || ''))
}

function hasNormalFlowPosition(ruleText) {
  const text = String(ruleText || '')
  if (hasAbsolutePosition(text)) return false
  return true
}

function parseCssHeightPx(ruleText) {
  const match = String(ruleText || '').match(/\bheight\s*:?\s*([0-9.]+)px\b/i)
  return match ? Number(match[1]) : null
}

function validatePageFlowPlanLayering(stateDir, pageDir) {
  if (!pageDir) return
  const planPath = path.join(pageArtifactDir(stateDir, pageDir), 'page-flow-plan.json')
  if (!fs.existsSync(planPath) || fs.statSync(planPath).size === 0) return

  let plan = null
  try {
    plan = JSON.parse(fs.readFileSync(planPath, 'utf8'))
  } catch (err) {
    console.error(`❌ page-flow-plan.json 解析失败：${err.message}`)
    process.exit(1)
  }

  const backgroundItems = (plan.items || []).filter((item) => item && item.implementation === 'absolute-background-layer')
  if (!backgroundItems.length) return

  const files = changedVueFiles()
  if (!files.length) return
  const problems = []

  for (const file of files) {
    const vueText = safe(() => fs.readFileSync(file, 'utf8')) || ''
    const styleText = collectStyleText(vueText)
    for (const item of backgroundItems) {
      const mark = item.layoutMark
      const classes = findClassesForD2cMark(vueText, mark)
      if (!classes.length) continue
      const checked = []
      let hasAbsolute = false
      for (const cls of classes) {
        const rule = extractClassRule(styleText, cls)
        if (!rule) continue
        if (hasAbsolutePosition(rule)) {
          hasAbsolute = true
          break
        }
        checked.push({ cls, rule })
      }
      if (hasAbsolute || !checked.length) continue
      const largeNormalFlow = checked.some(({ rule }) => {
        const h = parseCssHeightPx(rule)
        return hasNormalFlowPosition(rule) && (h == null || h >= Math.max(120, Number(item.height || 0) * 0.5))
      })
      if (largeNormalFlow) {
        problems.push({
          file,
          mark,
          classes: checked.map((x) => `.${x.cls}`).join(', '),
          reason: item.reason || 'background layer must not occupy normal document flow',
        })
      }
    }
  }

  if (!problems.length) return
  console.error('❌ 阶段 1（代码还原）页面流/叠层校验失败，禁止进入截图。')
  console.error(`   page-flow-plan: ${planPath}`)
  for (const p of problems) {
    console.error(`   - ${p.file}: ${p.mark} (${p.classes}) 被识别为背景叠层，但实现仍像普通文档流块。${p.reason}`)
  }
  console.error('')
  console.error('请将该节点改为 position:absolute/fixed 的背景层、root 背景或 ::before 伪元素；不得用 position:relative/static + 大 height 占据文档流。')
  process.exit(1)
}

function isWholePageTextMissing(geometry) {
  if (!geometry || typeof geometry !== 'object') return false
  const checked = Number(geometry.checkedCount) || 0
  if (checked < 5) return false
  const matched = Number.isFinite(Number(geometry.matchedCount)) ? Number(geometry.matchedCount) : 0
  const violations = Array.isArray(geometry.violations) ? geometry.violations : []
  const textMissing = violations.filter((v) => v && v.kind === 'text-missing').length
  const dominantTextMissing = textMissing >= Math.ceil(checked * 0.8)
  return matched === 0 && dominantTextMissing
}

const WORKFLOW_MODES = new Set(['create-page', 'update-page', 'component'])

function normalizeWorkflowMode(value) {
  const raw = String(value || '').trim().toLowerCase()
  if (!raw) return ''
  if (raw === 'page' || raw === 'create' || raw === 'new-page') return 'create-page'
  if (raw === 'update' || raw === 'existing-page' || raw === 'updatepage') return 'update-page'
  if (raw === 'components') return 'component'
  return WORKFLOW_MODES.has(raw) ? raw : ''
}

function applyWorkflowMetadata(state, args = {}) {
  if (!state) return
  const mode = normalizeWorkflowMode(args.mode || args['workflow-mode'] || process.env.D2C_WORKFLOW_MODE)
  if (mode) state.mode = mode
  const targetVue = args['target-vue'] || args.target
  if (targetVue && targetVue !== true) state.targetVue = String(targetVue)
  const designScope = args['design-scope'] || args.scope || process.env.D2C_DESIGN_SCOPE
  if (designScope && designScope !== true) state.designScope = String(designScope).trim()
}

function isUpdatePageMode(args = {}, state = {}) {
  return normalizeWorkflowMode(args.mode || args['workflow-mode'] || (state && state.mode) || process.env.D2C_WORKFLOW_MODE) === 'update-page'
}

function isCreatePageMode(args = {}, state = {}) {
  return normalizeWorkflowMode(args.mode || args['workflow-mode'] || (state && state.mode) || process.env.D2C_WORKFLOW_MODE) === 'create-page'
}

function readJsonFile(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'))
  } catch (err) {
    err.message = `${filePath}: ${err.message}`
    throw err
  }
}

function resolveExpectedPageFlowMarks(stateDir, pageDir) {
  const base = pageArtifactDir(stateDir, pageDir)
  const flowPath = path.join(base, 'page-flow-plan.json')
  if (fs.existsSync(flowPath)) {
    const plan = readJsonFile(flowPath)
    const marks = (plan.items || [])
      .filter((item) => item && item.layoutMark && item.role !== 'background-layer')
      .map((item) => String(item.layoutMark))
    if (marks.length) return { source: 'page-flow-plan.json', marks: [...new Set(marks)] }
  }

  const componentPath = path.join(base, 'component-plan.json')
  if (!fs.existsSync(componentPath)) return null
  const plan = readJsonFile(componentPath)
  const components = plan.components || (Array.isArray(plan) ? plan : [])
  const marks = components
    .map((component) => {
      if (!component) return null
      if (component.layoutMark) return String(component.layoutMark)
      const candidates = Array.isArray(component.layoutMarks) ? component.layoutMarks.filter(Boolean) : []
      return candidates
        .slice()
        .sort((a, b) => String(a).split('.').length - String(b).split('.').length || String(a).localeCompare(String(b)))[0] || null
    })
    .filter(Boolean)
  return marks.length ? { source: 'component-plan.json', marks: [...new Set(marks)] } : null
}

/**
 * page-flow 覆盖检查应使用哪个 scope：
 * region 模式下报告会额外记录未被物料 scope 覆盖的 `targetScope`，此时目标 scope
 * 才是本次真正要核对的对象；没有 targetScope 时回落到 scope。
 * 同时兼容 scope 缺失的旧报告，避免直接读 scope.layoutMarks 抛异常。
 */
function resolvePageFlowReportMarks(vertical) {
  const targetMarks = vertical && vertical.targetScope && Array.isArray(vertical.targetScope.layoutMarks)
    ? vertical.targetScope.layoutMarks.map(String)
    : []
  if (targetMarks.length) return targetMarks
  return vertical && vertical.scope && Array.isArray(vertical.scope.layoutMarks)
    ? vertical.scope.layoutMarks.map(String)
    : []
}

function validateCreatePageFlowReport(stateDir, pageDir, args, state, vertical, problems) {
  if (!isCreatePageMode(args, state)) return true
  if (!vertical || vertical.mode !== 'page-flow' || !vertical.scope || vertical.scope.pageFlow !== true) {
    problems.push(
      `create-page 必须使用 d2c-vertical-check.cjs --page-flow 生成页面骨架报告；当前 vertical.mode=${vertical && vertical.mode ? vertical.mode : 'missing'}。普通 page 文本锚点报告不能替代顶层区块坐标闸。`
    )
    return false
  }

  let expected
  try {
    expected = resolveExpectedPageFlowMarks(stateDir, pageDir)
  } catch (err) {
    problems.push(`create-page page-flow 计划解析失败：${err.message}`)
    return false
  }
  if (!expected || !expected.marks.length) {
    problems.push('create-page 缺少可用 page-flow-plan.json/component-plan.json，无法核对顶层区块坐标。')
    return false
  }

  const scopeMarks = new Set(resolvePageFlowReportMarks(vertical))
  const itemByMark = new Map((vertical.items || []).map((item) => [String(item.layoutMark || ''), item]))
  const uncovered = expected.marks.filter((mark) => {
    const item = itemByMark.get(mark)
    return !scopeMarks.has(mark) || !item || item.matched !== true || item.method !== 'mark'
  })
  if (uncovered.length) {
    problems.push(
      `create-page page-flow 根节点覆盖不完整（来源 ${expected.source}）：${uncovered.slice(0, 12).join(', ')}。顶层根必须用 data-d2c-mark 精确命中，禁止文本回退。`
    )
    return false
  }
  return true
}

function resolvePlacementCheckPath(stateDir, pageDir, round, args = {}) {
  if (args['placement-check'] && args['placement-check'] !== true) {
    return path.resolve(String(args['placement-check']))
  }
  const base = pageArtifactDir(stateDir, pageDir)
  const candidates = [
    path.join(base, `r${round || 1}`, 'placement-check.json'),
    path.join(base, 'placement-check.json'),
  ]
  return candidates.find((p) => fs.existsSync(p)) || candidates[1]
}

function summarizePlacement(placement) {
  if (!placement || typeof placement !== 'object') return '无 placement-check'
  const expected = placement.expected || {}
  const evidence = placement.evidence || {}
  const comp = evidence.component || {}
  const anchor = evidence.anchor || {}
  return [
    `status=${placement.status || '?'}`,
    `targetVue=${placement.targetVue || '?'}`,
    `selector=${expected.selector || '?'}`,
    `anchor=${expected.anchorSelector || expected.anchorText || '?'}`,
    `relation=${expected.relation || '?'}`,
    `componentLine=${comp.line || '?'}`,
    `anchorLine=${anchor.line || '?'}`,
  ].join(' ')
}

function recordUpdatePagePlacementState(state, placement, captureMeta, placementPath) {
  if (!state) return
  const accessMode = captureMeta ? (captureMeta.accessMode || 'real-route') : 'unknown'
  const pageIntegrationVerified = accessMode === 'real-route' || (
    accessMode === 'user-browser-handoff' &&
    captureMeta &&
    captureMeta.browserAttached === true &&
    captureMeta.handoffVerification &&
    captureMeta.handoffVerification.ok === true
  )
  state.updatePagePlacement = {
    status: placement.status || 'unknown',
    placementCheck: placementPath,
    targetVue: placement.targetVue || null,
    selector: placement.expected && placement.expected.selector ? placement.expected.selector : null,
    anchor: placement.expected ? (placement.expected.anchorSelector || placement.expected.anchorText || null) : null,
    relation: placement.expected && placement.expected.relation ? placement.expected.relation : null,
    componentLine: placement.evidence && placement.evidence.component ? placement.evidence.component.line : null,
    anchorLine: placement.evidence && placement.evidence.anchor ? placement.evidence.anchor.line : null,
    accessMode,
    pageIntegrationVerified,
    scoreScope: pageIntegrationVerified ? 'existing-page-integrated' : 'component-visual-only',
    note: pageIntegrationVerified
      ? `${accessMode === 'user-browser-handoff' ? '用户接管的真实业务页' : '真实业务路由截图'} + 源码落位均已验证`
      : '真实业务路由未验证；本轮只能证明组件视觉与源码落位，不能宣称页面内运行时位置正确',
    updatedAt: new Date().toISOString(),
  }
}

function validateUserBrowserHandoff(stateDir, pageDir, captureMeta, reports, problems) {
  if (!captureMeta || captureMeta.accessMode !== 'user-browser-handoff') return
  const handoffPath = path.join(pageArtifactDir(stateDir, pageDir), 'browser-handoff.json')
  if (!fs.existsSync(handoffPath) || fs.statSync(handoffPath).size === 0) {
    problems.push(`user-browser-handoff 缺少 browser-handoff.json：${handoffPath}`)
    return
  }

  let handoff = null
  try { handoff = readJsonFile(handoffPath) }
  catch (err) {
    problems.push(`browser-handoff.json 解析失败：${err.message}`)
    return
  }

  if (handoff.mode !== 'update-page') {
    problems.push(`browser-handoff.json mode=${handoff.mode || 'missing'}；人工浏览器接管目前只允许 update-page。`)
  }
  if (!['verified', 'completed'].includes(handoff.status)) {
    problems.push(`browser-handoff.json status=${handoff.status || 'missing'}，必须先由用户进入目标页并通过 --verify。`)
  }
  const verification = handoff.verification || captureMeta.handoffVerification || null
  if (!verification || verification.ok !== true) {
    problems.push('user-browser-handoff 缺少 verification.ok=true 的目标路由与 selector 验证证据。')
  }
  if (captureMeta.browserAttached !== true) {
    problems.push('capture-meta.json browserAttached 不是 true；handoff 禁止退化为重新启动的无状态 Chrome。')
  }
  if (captureMeta.redirected) {
    problems.push(`user-browser-handoff 截图时已离开目标页：final=${captureMeta.finalUrl || '?'}`)
  }

  const sessionId = captureMeta.browserSessionId
  const targetId = captureMeta.browserTargetId
  if (!sessionId || !targetId) {
    problems.push('capture-meta.json 缺少 browserSessionId/browserTargetId，无法证明四项物料来自同一用户 tab。')
    return
  }
  if (handoff.browserSessionId && handoff.browserSessionId !== sessionId) {
    problems.push(`capture-meta browserSessionId=${sessionId} 与 browser-handoff.json=${handoff.browserSessionId} 不一致。`)
  }
  if (handoff.browserTargetId && handoff.browserTargetId !== targetId) {
    problems.push(`capture-meta browserTargetId=${targetId} 与 browser-handoff.json=${handoff.browserTargetId} 不一致。`)
  }

  for (const [name, report] of reports) {
    if (!report) continue
    if (report.browserAttached !== true) {
      problems.push(`${name}.browserAttached 不是 true；handoff 检测必须复用用户 tab。`)
    }
    if (report.browserSessionId !== sessionId || report.browserTargetId !== targetId) {
      problems.push(
        `${name} 未复用截图 tab：expected session=${sessionId} target=${targetId}，` +
        `actual session=${report.browserSessionId || 'missing'} target=${report.browserTargetId || 'missing'}。`
      )
    }
  }
}

function validateUpdatePagePlacement(stateDir, pageDir, round, args, state, captureMeta, problems) {
  if (!isUpdatePageMode(args, state)) return

  const placementPath = resolvePlacementCheckPath(stateDir, pageDir, round, args)
  if (!fs.existsSync(placementPath) || fs.statSync(placementPath).size === 0) {
    problems.push(
      `已有页模式缺失 placement-check.json。请先运行 d2c-code-placement-check.cjs 证明新增/改动块在目标 Vue 的指定宿主上下文内，再执行 --complete 2 --mode update-page。期望路径: ${placementPath}`
    )
    return
  }

  let placement = null
  try {
    placement = readJsonFile(placementPath)
  } catch (err) {
    problems.push(`placement-check.json 解析失败：${err.message}`)
    return
  }

  if (placement.status !== 'passed') {
    const failures = Array.isArray(placement.failures) && placement.failures.length
      ? placement.failures.join('；')
      : 'status 不是 passed'
    problems.push(`已有页源码落位检查未通过：${summarizePlacement(placement)}；failures=${failures}`)
  }

  recordUpdatePagePlacementState(state, placement, captureMeta, placementPath)

  if (captureMeta && (captureMeta.accessMode || 'real-route') === 'vue-route-preview') {
    problems.push(
      'D2C_WORKFLOW_CAPABILITY_VIOLATION: update-page 禁止使用 vue-route-preview；' +
      '请使用真实业务路由，持续重定向时进入 user-browser-handoff。'
    )
  }
}

function matchedPreviewLeak(value) {
  return PREVIEW_LEAK_PATTERNS.find((p) => p.re.test(value))
}

function collectPreviewHarnessLeaks() {
  const leaks = []
  const pushLeak = (file, label, line) => {
    if (!file || isIgnoredPreviewArtifactPath(file)) return
    leaks.push({
      file,
      label,
      line: String(line || '').trim().slice(0, 180),
    })
  }

  try {
    const diff = execSync('git diff --unified=0 -- .', {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
      maxBuffer: 20 * 1024 * 1024,
    })
    let currentFile = null
    for (const rawLine of diff.split(/\r?\n/)) {
      const header = rawLine.match(/^diff --git a\/(.+) b\/(.+)$/)
      if (header) {
        currentFile = header[2]
        continue
      }
      if (!rawLine.startsWith('+') || rawLine.startsWith('+++')) continue
      const added = rawLine.slice(1)
      const matched = matchedPreviewLeak(added)
      if (matched) pushLeak(currentFile, matched.label, added)
      if (leaks.length >= 8) return leaks
    }
  } catch (_) {
    // 非 git 仓或 diff 过大时不阻断；active/capture/meta 闸仍继续生效。
  }

  try {
    const status = execSync('git status --short --untracked-files=all', {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
      maxBuffer: 5 * 1024 * 1024,
    })
    for (const rawLine of status.split(/\r?\n/)) {
      const file = rawLine.slice(3).replace(/^"|"$/g, '')
      const matched = matchedPreviewLeak(file)
      if (matched) pushLeak(file, matched.label, 'status entry')
      if (leaks.length >= 8) return leaks
    }
  } catch (_) {
    // 同上，不因 git status 不可用而中断。
  }
  return leaks
}

function sha256File(filePath) {
  if (!filePath || !fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) return null
  return crypto.createHash('sha256').update(fs.readFileSync(filePath)).digest('hex')
}

function canonicalizeForDigest(value) {
  if (Array.isArray(value)) return value.map(canonicalizeForDigest)
  if (!value || typeof value !== 'object') return value
  return Object.keys(value)
    .sort()
    .reduce((out, key) => {
      out[key] = canonicalizeForDigest(value[key])
      return out
    }, {})
}

function measurementReportDigest(filePath) {
  const report = readJsonArtifact(filePath)
  if (!report) return null
  const payload = {
    schemaVersion: report.schemaVersion || null,
    mode: report.mode || null,
    scope: report.scope || null,
    hostLayoutPolicy: report.hostLayoutPolicy || null,
    layoutShiftY: report.layoutShiftY || 0,
    checkedCount: report.checkedCount ?? null,
    matchedCount: report.matchedCount ?? null,
    items: (report.items || []).map((item) => ({
      layoutMark: item.layoutMark || null,
      name: item.name || null,
      text: item.text || null,
      matched: item.matched === true,
      method: item.method || null,
      expected: item.expected || null,
      measured: item.measured || null,
    })),
    violations: (report.violations || []).map((violation) => ({
      layoutMark: violation.layoutMark || null,
      name: violation.name || null,
      text: violation.text || null,
      kind: violation.kind || null,
      severity: violation.severity || null,
      diffPx: Number.isFinite(Number(violation.diffPx)) ? Number(violation.diffPx) : null,
      expected: violation.expected ?? null,
      measured: violation.measured ?? null,
    })),
  }
  return crypto
    .createHash('sha256')
    .update(JSON.stringify(canonicalizeForDigest(payload)))
    .digest('hex')
}

function resolveRoundActualPng(roundDir, captureMeta) {
  const fromMeta = captureMeta && captureMeta.outPath ? path.resolve(String(captureMeta.outPath)) : null
  const roundAbs = path.resolve(roundDir)
  const metaRelative = fromMeta ? path.relative(roundAbs, fromMeta) : ''
  const metaInsideRound = fromMeta && metaRelative && !metaRelative.startsWith(`..${path.sep}`) && metaRelative !== '..' && !path.isAbsolute(metaRelative)
  if (metaInsideRound && fs.existsSync(fromMeta) && fs.statSync(fromMeta).isFile()) return fromMeta
  if (!fs.existsSync(roundDir) || !fs.statSync(roundDir).isDirectory()) return null
  const candidates = fs.readdirSync(roundDir)
    .filter((name) => /(?:^|-)actual\.png$/i.test(name))
    .map((name) => path.join(roundDir, name))
    .filter((filePath) => fs.statSync(filePath).isFile())
    .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs || a.localeCompare(b))
  return candidates[0] || null
}

function readRoundMaterialFingerprint(stateDir, pageDir, round) {
  const roundDir = path.join(pageArtifactDir(stateDir, pageDir), `r${round}`)
  const captureMeta = readJsonArtifact(path.join(roundDir, 'capture-meta.json'))
  const actualPath = resolveRoundActualPng(roundDir, captureMeta)
  const actualSha256 = sha256File(actualPath)
  const geometryMeasurementsSha256 = measurementReportDigest(path.join(roundDir, 'geometry-diff.json'))
  const verticalMeasurementsSha256 = measurementReportDigest(path.join(roundDir, 'vertical-diff.json'))
  const renderMeasurementsSha256 = measurementReportDigest(path.join(roundDir, 'render-diff.json'))
  const materialValid = !captureMeta || (captureMeta.materialStatus !== 'invalid' && !captureMeta.invalidReason)
  return {
    round: Number(round),
    actualPath,
    actualSha256,
    geometryMeasurementsSha256,
    verticalMeasurementsSha256,
    renderMeasurementsSha256,
    valid: Boolean(materialValid && actualSha256 && geometryMeasurementsSha256 && verticalMeasurementsSha256 && renderMeasurementsSha256),
  }
}

function compareRoundMaterialFingerprints(current, previous) {
  const fields = [
    'actualSha256',
    'geometryMeasurementsSha256',
    'verticalMeasurementsSha256',
    'renderMeasurementsSha256',
  ]
  const comparable = Boolean(current && previous && current.valid && previous.valid)
  const identical = comparable && fields.every((field) => current[field] === previous[field])
  return { comparable, identical, fields }
}

function findPreviousValidRoundMaterialFingerprint(stateDir, pageDir, round) {
  for (let previousRound = Number(round) - 1; previousRound >= 1; previousRound -= 1) {
    const fingerprint = readRoundMaterialFingerprint(stateDir, pageDir, previousRound)
    if (fingerprint.valid) return fingerprint
  }
  return null
}

/** 阶段 2 完成前校验当前轮物料，并在进入 evaluator 前执行 geometry + vertical + render 联合闸 */
function validatePhase2Artifacts(stateDir, pageDir, round, args = {}, state = null) {
  if (!pageDir) {
    console.error('❌ 阶段 2 --complete 必须传 --page-dir <name>')
    process.exit(1)
  }
  const roundDir = path.join(pageArtifactDir(stateDir, pageDir), `r${round || 1}`)
  const problems = []

  const geometryPath = path.join(roundDir, 'geometry-diff.json')
  if (!fs.existsSync(geometryPath)) {
    problems.push(`缺失 geometry-diff.json（须先运行 d2c-geometry-check.cjs）`)
  } else if (fs.statSync(geometryPath).size === 0) {
    problems.push('geometry-diff.json 为空文件')
  }
  let geometry = null
  let geometryParsed = false
  if (fs.existsSync(geometryPath) && fs.statSync(geometryPath).size > 0) {
    try {
      geometry = JSON.parse(fs.readFileSync(geometryPath, 'utf8'))
      geometryParsed = true
    } catch (err) {
      problems.push(`geometry-diff.json 解析失败：${err.message}`)
    }
  }

  const verticalPath = path.join(roundDir, 'vertical-diff.json')
  let vertical = null
  let verticalParsed = false
  if (!fs.existsSync(verticalPath)) {
    problems.push('缺失 vertical-diff.json（须先运行 d2c-vertical-check.cjs --page-flow）')
  } else if (fs.statSync(verticalPath).size === 0) {
    problems.push('vertical-diff.json 为空文件')
  } else {
    try {
      vertical = JSON.parse(fs.readFileSync(verticalPath, 'utf8'))
      verticalParsed = true
    } catch (err) {
      problems.push(`vertical-diff.json 解析失败：${err.message}`)
    }
  }

  const renderPath = path.join(roundDir, 'render-diff.json')
  let render = null
  let renderParsed = false
  if (!fs.existsSync(renderPath)) {
    problems.push('缺失 render-diff.json（须先运行 d2c-render-check.cjs）')
  } else if (fs.statSync(renderPath).size === 0) {
    problems.push('render-diff.json 为空文件')
  } else {
    try {
      render = JSON.parse(fs.readFileSync(renderPath, 'utf8'))
      renderParsed = true
    } catch (err) {
      problems.push(`render-diff.json 解析失败：${err.message}`)
    }
  }

  const layoutH5Path = path.join(pageArtifactDir(stateDir, pageDir), 'layout-h5.json')
  if (fs.existsSync(layoutH5Path)) {
    try {
      const layoutH5 = JSON.parse(fs.readFileSync(layoutH5Path, 'utf8'))
      if (Number(layoutH5.schemaVersion || 0) >= 2) {
        const policy = String(layoutH5.hostLayoutPolicy || '')
        const shiftY = Number(
          layoutH5.layoutShiftY ?? layoutH5.contentOffsetY ?? 0
        )
        const cropDsl = Number(layoutH5.designCropTopDsl || 0)
        const hostBottom = Number(layoutH5.hostChromeBottomY || 0)
        if (
          policy === 'immersive-overlay' &&
          (shiftY !== 0 ||
            cropDsl !== 0 ||
            Number(layoutH5.designCropTopRaster || 0) !== 0)
        ) {
          problems.push(
            `宿主布局策略冲突：immersive-overlay 必须 layoutShiftY=0 且 designCropTopDsl=0，当前 shift=${shiftY}, crop=${cropDsl}。沉浸式 H5 只能删除原生控件，禁止删除顶部坐标空间。`
          )
        }
        if (
          policy === 'immersive-overlay' &&
          !(layoutH5.ignoredDesignRegions || []).length
        ) {
          problems.push(
            'immersive-overlay 缺少 ignoredDesignRegions；设计侧原生控件必须从视觉评分排除，但完整画布不得裁顶。'
          )
        }
        if (
          policy === 'stacked-host' &&
          (shiftY <= 0 || Math.abs(shiftY - hostBottom) > 1)
        ) {
          problems.push(
            `宿主布局策略冲突：stacked-host 必须按 hostChromeBottomY 平移，当前 hostBottom=${hostBottom}, shift=${shiftY}。禁止按 firstBusinessY 删除业务顶部留白。`
          )
        }
        for (const [name, report] of [
          ['geometry-diff.json', geometry],
          ['vertical-diff.json', vertical],
          ['render-diff.json', render],
        ]) {
          if (!report) continue
          if (report.hostLayoutPolicy !== policy) {
            problems.push(
              `${name}.hostLayoutPolicy=${report.hostLayoutPolicy || 'missing'} 与 layout-h5.json=${policy} 不一致，须用当前 DSL 坐标策略重跑物料。`
            )
          }
          if (Number(report.layoutShiftY || 0) !== shiftY) {
            problems.push(
              `${name}.layoutShiftY=${Number(report.layoutShiftY || 0)} 与 layout-h5.json=${shiftY} 不一致，禁止复用旧轮次坐标报告。`
            )
          }
        }
      }
    } catch (err) {
      problems.push(`layout-h5.json 解析失败：${err.message}`)
    }
  }

  let usedPreviewHarness = false
  const previewStatePath = path.join(pageArtifactDir(stateDir, pageDir), 'preview-harness.json')
  let previewState = null
  let previewHarnessActiveProblem = ''
  if (fs.existsSync(previewStatePath)) {
    try {
      previewState = JSON.parse(fs.readFileSync(previewStatePath, 'utf8'))
      if (previewState && previewState.accessMode === 'preview-harness') usedPreviewHarness = true
      if (previewState && previewState.accessMode === 'preview-harness') {
        const previewWorkflowMode = normalizeWorkflowMode(previewState.workflowMode)
        if (!previewWorkflowMode) {
          problems.push('preview-harness.json 缺少 workflowMode；preview 入口必须明确属于 create-page 或 component。')
        } else if (previewWorkflowMode === 'update-page') {
          problems.push(
            'D2C_WORKFLOW_CAPABILITY_VIOLATION: preview-harness.json workflowMode=update-page；' +
            '已有页流程只能使用 real-route 或 user-browser-handoff。'
          )
        } else {
          const currentMode = normalizeWorkflowMode(args.mode || args['workflow-mode'] || state.mode)
          if (currentMode && currentMode !== previewWorkflowMode) {
            problems.push(
              `preview-harness.json workflowMode=${previewWorkflowMode} 与当前 workflow mode=${currentMode} 不一致。`
            )
          }
        }
      }
      if (previewState && previewState.accessMode === 'preview-harness' && previewState.active !== false) {
        previewHarnessActiveProblem =
          `preview harness 尚未清理（${previewStatePath} active=true）。联合闸可放行或达到最大轮次后，进入 evaluator 前必须先运行 d2c-preview-harness.cjs --cleanup`
      }
    } catch (err) {
      problems.push(`preview-harness.json 解析失败：${err.message}`)
    }
  }
  let browserHandoffActiveProblem = ''
  const browserHandoffPath = path.join(pageArtifactDir(stateDir, pageDir), 'browser-handoff.json')
  if (fs.existsSync(browserHandoffPath)) {
    try {
      const browserHandoff = JSON.parse(fs.readFileSync(browserHandoffPath, 'utf8'))
      if (browserHandoff && browserHandoff.active === true && browserHandoff.status === 'verified') {
        browserHandoffActiveProblem =
          `用户浏览器 handoff 尚未清理（${browserHandoffPath} active=true）。联合闸可放行或达到最大轮次后，进入 evaluator 前必须运行 d2c-browser-handoff.cjs --cleanup --result completed。`
      }
    } catch (err) {
      problems.push(`browser-handoff.json 解析失败：${err.message}`)
    }
  }
  // === 指纹复用：代码未变时复用上一轮 capture 物料 ===
  if (!fs.existsSync(path.join(roundDir, 'capture-meta.json')) && round > 1) {
    const prevRound = round - 1
    const currentFp = readRoundMaterialFingerprint(stateDir, pageDir, round)
    const previousFp = readRoundMaterialFingerprint(stateDir, pageDir, prevRound)

    if (currentFp.valid && previousFp.valid) {
      const comparison = compareRoundMaterialFingerprints(currentFp, previousFp)
      if (comparison.identical) {
        const prevRoundDir = path.join(pageArtifactDir(stateDir, pageDir), 'r' + prevRound)
        const roundDirFull = path.join(pageArtifactDir(stateDir, pageDir), 'r' + round)
        const filesToLink = ['capture-meta.json', 'actual.png']
        let linked = 0
        for (const file of filesToLink) {
          const src = path.join(prevRoundDir, file)
          const dst = path.join(roundDirFull, file)
          if (fs.existsSync(src) && !fs.existsSync(dst)) {
            fs.linkSync(src, dst)
            linked += 1
          }
        }
        if (linked > 0) {
          console.log('📋 复用上一轮截图（source fingerprint 未变，已链接 ' + linked + ' 个文件）')
        }
      }
    }
  }
  const captureMetaPath = path.join(roundDir, 'capture-meta.json')
  let captureMeta = null
  if (fs.existsSync(captureMetaPath) && fs.statSync(captureMetaPath).size > 0) {
    try {
      captureMeta = JSON.parse(fs.readFileSync(captureMetaPath, 'utf8'))
      if (captureMeta && captureMeta.accessMode === 'vue-route-preview') usedPreviewHarness = true
    } catch (err) {
      problems.push(`capture-meta.json 解析失败：${err.message}`)
    }
  } else if (usedPreviewHarness) {
    problems.push('使用 preview harness 但缺失 capture-meta.json，无法确认截图入口类型与真实 Vue DOM 命中情况')
  }

  let previewLeakProblem = ''
  if (usedPreviewHarness) {
    const previewLeaks = collectPreviewHarnessLeaks()
    if (previewLeaks.length) {
      const detail = previewLeaks
        .map((leak) => `${leak.file}: ${leak.label}: ${leak.line}`)
        .join('；')
      previewLeakProblem =
        'preview harness 已使用/清理，但当前业务 git diff 仍残留临时 preview-only 入口或 bootstrap bypass 标记。' +
        `必须先恢复这些 route/wrapper/bootstrap 改动，确保最终 diff 只保留真实业务页面实现。残留: ${detail}`
    }
  }

  if (captureMeta) {
    if (!captureMeta.runtimeHealth) {
      problems.push('capture-meta.json 缺少 runtimeHealth；必须用当前 d2c-capture-page.cjs 覆盖重采，禁止复用旧截图。')
    } else {
      const healthProblems = validateHealthEvidence({
        ...captureMeta.runtimeHealth,
        sourceFingerprint: captureMeta.sourceFingerprint || null,
      }, { cwd: process.cwd() })
      for (const problem of healthProblems) {
        problems.push(`capture runtime health: ${problem}`)
      }
    }
    if (captureMeta.materialStatus === 'invalid' || captureMeta.invalidReason) {
      problems.push(
        `capture-meta.json 标记当前截图材料无效：${captureMeta.invalidReason || 'materialStatus=invalid'}。` +
          '必须先修复截图入口/preview harness 并重跑 actual、geometry、vertical、render；禁止把无效材料送入 fidelity。'
      )
    }
    if (captureMeta.captureMode === 'region') {
      const r = captureMeta.regionBBoxCss || captureMeta.captureRegionCss || null
      const validRegion = r && [r.left, r.top, r.width, r.height].every((x) => Number.isFinite(Number(x))) && Number(r.width) > 0 && Number(r.height) > 0
      if (!validRegion) {
        problems.push(
          'capture-meta.json captureMode=region 但缺少有效 regionBBoxCss；局部材料必须记录实际 DOM 区域 bbox，才能与设计局部坐标系对齐。'
        )
      }
      const designScope = String(captureMeta.designScope || '').trim()
      if (!designScope || designScope.includes(',')) {
        problems.push('captureMode=region 必须记录唯一 capture-meta.json.designScope；缺失或多 scope 的局部材料无效。')
      }
      for (const [name, report] of [
        ['geometry-diff.json', geometry],
        ['vertical-diff.json', vertical],
        ['render-diff.json', render],
      ]) {
        if (!report) continue
        const marks = report.scope && Array.isArray(report.scope.layoutMarks)
          ? report.scope.layoutMarks.map(String)
          : []
        const measuredScope = report.designMeasurementFrame && report.designMeasurementFrame.scope
          ? String(report.designMeasurementFrame.scope.layoutMark || '')
          : ''
        if (marks.length !== 1 || marks[0] !== designScope || measuredScope !== designScope) {
          problems.push(
            `${name} 未复用 region designScope=${designScope || 'missing'}：` +
            `scope=[${marks.join(',') || 'missing'}], measurementScope=${measuredScope || 'missing'}。`
          )
        }
        if (report.captureMode !== 'region') {
          problems.push(`${name}.captureMode=${report.captureMode || 'missing'}，与 region actual 不一致。`)
        }
      }
      const scopedDesignExists = fs.existsSync(roundDir) && fs.readdirSync(roundDir).some((file) => /-design-source\.png$/.test(file))
      if (!scopedDesignExists) {
        problems.push('region 本轮缺少 *-design-source.png；必须按同一 scope 裁出设计输入后才能进入 fidelity。')
      }
    }
    const accessMode = captureMeta.accessMode || 'real-route'
    const allowedModes = new Set(['real-route', 'vue-route-preview', 'user-browser-handoff'])
    if (!allowedModes.has(accessMode)) {
      problems.push(
        `capture-meta.json accessMode=${accessMode} 不合法；必须是 real-route / vue-route-preview / user-browser-handoff 之一。禁止把静态快照或 DOM replay 作为阶段 2 物料来源。`
      )
    }
    if (isUpdatePageMode(args, state) && accessMode === 'vue-route-preview') {
      problems.push(
        'D2C_WORKFLOW_CAPABILITY_VIOLATION: update-page 的 capture-meta.json 不允许 accessMode=vue-route-preview；' +
        '请重新使用真实业务路由或验证后的 user-browser-handoff tab 生成物料。'
      )
    }
    if (usedPreviewHarness && accessMode !== 'vue-route-preview') {
      problems.push(
        `本轮使用 preview harness，但 capture-meta.json accessMode=${accessMode}；preview 成功物料必须通过项目 dev server 的 vue-route-preview 生成。`
      )
    }
    if (accessMode === 'vue-route-preview') {
      const domProbe = captureMeta.domProbe || {}
      const previewKind = previewState && previewState.previewKind ? String(previewState.previewKind) : ''
      const allowedPreviewKinds = new Set(['isolated-vue-mount', 'preview-bootstrap-bypass'])
      if (!allowedPreviewKinds.has(previewKind)) {
        problems.push(
          `vue-route-preview 缺少合格的 previewKind（当前: ${previewKind || '未声明'}）。必须用 d2c-preview-harness.cjs --preview-kind isolated-vue-mount 或 --preview-kind preview-bootstrap-bypass 登记；普通业务 SPA route 会继续跑全局 prepare/init-data，不能作为成功 preview。`
        )
      }
      if (captureMeta.previewRouteRedirected || captureMeta.redirected) {
        problems.push(
          `vue-route-preview 自身发生重定向：requested=${captureMeta.requestedUrl || '?'} final=${captureMeta.finalUrl || '?'}。这说明 preview 入口仍被业务 app bootstrap/init-data/router guard 带走；请改为 isolated Vue mount，或添加只命中 preview path 的临时 bootstrap/init-data/router bypass 后重跑。`
        )
      }
      if (domProbe.likelyStaticHtmlReplay) {
        problems.push(
          'vue-route-preview 疑似静态快照/DOM replay：capture-meta.json.domProbe.likelyStaticHtmlReplay=true。preview 成功物料必须通过项目 dev server 的真实 Vue 构建链 import 目标 .vue；请改用 isolated Vue entry 后重跑截图、geometry、vertical、render。'
        )
      }
      if (previewKind === 'preview-bootstrap-bypass' && previewState && previewState.bypassesBootstrap !== true && domProbe.hasPreviewBootstrapBypass !== true) {
        problems.push(
          'vue-route-preview 声明为 preview-bootstrap-bypass，但 preview-harness.json.bypassesBootstrap 与 capture-meta.json.domProbe.hasPreviewBootstrapBypass 均未确认。请在临时 preview path 分支中设置 window.__D2C_PREVIEW_BOOTSTRAP_BYPASS__=true 或 data-d2c-preview-bootstrap-bypass=\"true\"，并登记 --bypasses-bootstrap true。'
        )
      }
      if (domProbe.probeError) {
        problems.push(`vue-route-preview DOM probe 失败：${domProbe.probeError}`)
      } else if (!domProbe.hasD2cRoot && !domProbe.hasD2cComponent) {
        problems.push(
          'vue-route-preview 未命中目标 Vue DOM：capture-meta.json.domProbe 中 hasD2cRoot/hasD2cComponent 均为 false。请确认隔离 Vue entry 实际 import 并渲染目标 .vue，而不是空壳、截图壳或 DOM replay。'
        )
      }
      // 入口执行断言：只有 d2c-isolated-vue-preview.cjs 生成的隔离入口会写该标记，
      // 手写 preview path 分支不强制。失败时给出专用错误码与归因，避免上层猜测。
      if (previewKind === 'isolated-vue-mount') {
        const entryFailure = classifyPreviewEntryFailure(domProbe)
        if (entryFailure) {
          problems.push(
            `${entryFailure.code}（cause=${entryFailure.cause}）：${entryFailure.detail} 修法：${entryFailure.repair}`
          )
        }
      }
      if (isWholePageTextMissing(geometry)) {
        const consoleErrors = Number(captureMeta.browserConsoleErrorCount) ? `，browserConsoleErrorCount=${captureMeta.browserConsoleErrorCount}` : ''
        const pageErrors = Number(captureMeta.browserPageErrorCount) ? `，browserPageErrorCount=${captureMeta.browserPageErrorCount}` : ''
        problems.push(
          `vue-route-preview 未渲染目标 Vue DOM：geometry checked=${geometry.checkedCount}, matched=${geometry.matchedCount}, text-missing≈整页${consoleErrors}${pageErrors}。请先修复临时 route/wrapper 挂载失败、module import/runtime exception 或必要 fixture/env 缺失，重跑截图与 geometry；禁止把空白 preview 送入 fidelity 评测。`
        )
      }
    }
  }

  validateUserBrowserHandoff(stateDir, pageDir, captureMeta, [
    ['geometry-diff.json', geometry],
    ['vertical-diff.json', vertical],
    ['render-diff.json', render],
  ], problems)

  validateUpdatePagePlacement(stateDir, pageDir, round || 1, args, state, captureMeta, problems)
  const createPageFlowReady = validateCreatePageFlowReport(stateDir, pageDir, args, state, vertical, problems)

  // === 组件模式：只统计当前组件作用域内的 violation ===
  const mode = normalizeWorkflowMode(args.mode)
  if (mode === 'component' && state) {
    const componentPlan = readComponentPlan(stateDir, pageDir)
    if (componentPlan && componentPlan.components && componentPlan.components.length > 0) {
      const allScopeMarks = new Set()
      for (const comp of componentPlan.components) {
        if (comp.layoutMark) allScopeMarks.add(String(comp.layoutMark))
        if (comp.layoutMarks && Array.isArray(comp.layoutMarks)) {
          for (const m of comp.layoutMarks) allScopeMarks.add(String(m))
        }
      }

      if (allScopeMarks.size > 0) {
        const filterDiffViolations = (diff, name) => {
          if (!diff || !diff.violations || !Array.isArray(diff.violations)) return diff
          const scoped = diff.violations.filter(v => {
            const mark = v.layoutMark || v.component || ''
            return allScopeMarks.has(String(mark))
          })
          const outOfScope = diff.violations.length - scoped.length
          if (outOfScope > 0) {
            console.log('ℹ️  ' + name + '：组件作用域外 ' + outOfScope + ' 条 violation 已降级为 info（不参与闸门判定）')
          }
          return { ...diff, violations: scoped, _outOfScopeCount: outOfScope }
        }

        if (geometry) geometry = filterDiffViolations(geometry, 'geometry-diff')
        if (render) render = filterDiffViolations(render, 'render-diff')
        // vertical-diff 不过滤（布局检查与组件边界无关）
      }
    }
  }

  const coordinateGate = collectGeometryGateStatus(stateDir, pageDir, round || 1, args)
  const renderGate = collectRenderGateStatus(stateDir, pageDir, round || 1)
  const phase2Gate = combinePhase2Gate(coordinateGate, renderGate)
  let phase2ReadyForEvaluator = false
  const phase2InputsReady = coordinateGate.hasGeom && coordinateGate.hasVert && renderGate.hasRender && geometryParsed && verticalParsed && renderParsed && createPageFlowReady
  const materialReadyForPhase2Gate = phase2InputsReady && problems.length === 0
  if (materialReadyForPhase2Gate) {
    const currentFingerprint = readRoundMaterialFingerprint(stateDir, pageDir, phase2Gate.roundNum)
    const previousFingerprint = findPreviousValidRoundMaterialFingerprint(stateDir, pageDir, phase2Gate.roundNum)
    const fingerprintComparison = compareRoundMaterialFingerprints(currentFingerprint, previousFingerprint)
    const sourceMaterialFailed = phase2Gate.sourceMaterialFailing.length > 0
    const renderMadeNoProgress = phase2Gate.failing.length > 0 && !sourceMaterialFailed && fingerprintComparison.identical

    if (sourceMaterialFailed) {
      recordGeometryGateState(state, coordinateGate, coordinateGate.failing.length === 0)
      recordRenderGateState(state, renderGate, false)
      recordPhase2AutoFixBlock(state, phase2Gate, 'render-source-material-invalid', {
        consumeRound: false,
      })
      persistStateSnapshot(stateDir, pageDir, state)
      problems.push(
        `阶段 2 render 闸发现 ${phase2Gate.sourceMaterialFailing.length} 条 DSL/字体/静态资源源物料问题，禁止把它误算成页面代码修复轮次。请先按 render-diff.json 修复源物料并覆盖重跑 r${phase2Gate.roundNum}；top=${formatPhase2GateTop(phase2Gate.top)}`
      )
    } else if (renderMadeNoProgress) {
      recordGeometryGateState(state, coordinateGate, coordinateGate.failing.length === 0)
      recordRenderGateState(state, renderGate, renderGate.failing.length === 0)
      recordPhase2AutoFixBlock(state, phase2Gate, 'render-no-progress', {
        consumeRound: false,
        previousRound: previousFingerprint.round,
        actualSha256: currentFingerprint.actualSha256,
      })
      persistStateSnapshot(stateDir, pageDir, state)
      problems.push(
        `r${phase2Gate.roundNum} 与最近有效轮 r${previousFingerprint.round} 的最终 actual 像素、geometry、vertical 和 render 测量完全一致，未形成新的有效修复证据。请先确认代码改动已被当前 dev server 编译并重新截图，再覆盖重跑 r${phase2Gate.roundNum}；本次不消耗有效修复轮次。`
      )
    } else if (!phase2Gate.failing.length) {
      phase2ReadyForEvaluator = true
      recordGeometryGateState(state, coordinateGate, true)
      recordRenderGateState(state, renderGate, true)
      clearPhase2AutoFixBlock(state)
      console.log(
        `✅ 阶段 2 联合前置闸通过：r${phase2Gate.roundNum} geometry + vertical + render 均无硬违规，允许进入 d2c-fidelity-evaluator。`
      )
    } else {
      recordGeometryGateState(state, coordinateGate, coordinateGate.failing.length === 0)
      recordRenderGateState(state, renderGate, renderGate.failing.length === 0)
      if (phase2Gate.roundNum < phase2Gate.maxRounds) {
        const detail = formatPhase2GateTop(phase2Gate.top)
        const repairPlanSummary = formatCoordinateRepairPlanSummary(phase2Gate.repairPlan)
        const reason = coordinateGate.failing.length && renderGate.failing.length
          ? 'phase2-combined-gate-failed'
          : renderGate.failing.length
            ? 'render-gate-failed'
            : 'coordinate-gate-failed'
        recordPhase2AutoFixBlock(state, phase2Gate, reason)
        persistStateSnapshot(stateDir, pageDir, state)
        problems.push(
          `阶段 2 联合前置闸未通过，禁止进入 d2c-fidelity-evaluator / 阶段 3：r${phase2Gate.roundNum} 有 coordinate=${coordinateGate.failing.length}、render=${renderGate.failing.length} 条硬违规（最大有效修正轮次 ${phase2Gate.maxRounds}）。repairPlan=${repairPlanSummary}; top=${detail}`
        )
      } else {
        phase2ReadyForEvaluator = true
        clearPhase2AutoFixBlock(state)
        console.log(
          `⚠️ 阶段 2 联合前置闸：已达最大有效修正轮次 ${phase2Gate.maxRounds}，r${phase2Gate.roundNum} 仍有 coordinate=${coordinateGate.failing.length}、render=${renderGate.failing.length} 条硬违规。允许进入阶段 3 做最终评测/摘要，但必须披露未达标项：`
        )
        printPhase2GateTop(phase2Gate.top, console.log)
      }
    }
  }

  if (usedPreviewHarness && phase2ReadyForEvaluator) {
    if (previewHarnessActiveProblem) problems.push(previewHarnessActiveProblem)
    if (previewLeakProblem) problems.push(previewLeakProblem)
  }
  if (phase2ReadyForEvaluator && browserHandoffActiveProblem) {
    problems.push(browserHandoffActiveProblem)
  }

  if (!problems.length) {
    let summary = ''
    try {
      const g = JSON.parse(fs.readFileSync(geometryPath, 'utf8'))
      const lowConfidence = Array.isArray(g.lowConfidenceMatches) ? g.lowConfidenceMatches : []
      summary = `（checked=${g.checkedCount ?? '?'}, violations=${(g.violations || []).length}, 未验证节点=${lowConfidence.length}）`
      if ((g.violations || []).length) {
        console.log(
          `⚠️ geometry-diff 存在 ${g.violations.length} 条 violation —— 坐标类已由阶段 2 前置闸处理，其余项目须与 render-diff 和阶段 3 评测共同核对`
        )
      }
      if (lowConfidence.length) {
        console.log(
          `⚠️ geometry-diff 有 ${lowConfidence.length} 个节点由启发式回退匹配命中（fallback-strategy=${g.fallbackStrategy || '?'}），` +
          '仅确认文本存在、尺寸与边缘未验证；收尾摘要必须如实披露，禁止当成已通过。'
        )
      }
    } catch (_) {
      /* 摘要解析失败不阻断 */
    }
    console.log(`✅ 阶段 2 产物校验通过：${geometryPath}${summary}；${verticalPath}；${renderPath}`)
    return
  }

  if (state && !state.phase2Block) {
    const currentRound = Number(round) || Number(state.round) || 1
    const runtimeHealthFailed = captureMeta && captureMeta.runtimeHealth && captureMeta.runtimeHealth.status !== 'passed'
    recordPhase2AutoFixBlock(state, null, runtimeHealthFailed ? 'runtime-health-failed' : 'phase2-material-validation-failed', {
      currentRound,
      nextRound: currentRound,
      consumeRound: false,
      failureClass: runtimeHealthFailed ? captureMeta.runtimeHealth.failureClass || 'runtime-error' : null,
      askUser: false,
      problems: problems.slice(0, 8),
    })
    persistStateSnapshot(stateDir, pageDir, state)
  }

  console.error('❌ 阶段 2（物料生成）产物校验失败，禁止标记完成。')
  console.error(`   目录: ${path.resolve(roundDir)}`)
  problems.forEach((p) => console.error(`   - ${p}`))
  if (state && state.phase2Block) {
    console.error('')
    printPhase2AutoFixSentinel(state.phase2Block, console.error)
    printCoordinateRepairPlan(state.phase2Block.repairPlan, console.error)
    const retryScope = state.phase2Block.consumeRound === false ? `同轮 r${state.phase2Block.nextRound}` : `下一轮 r${state.phase2Block.nextRound}`
    console.error(`   这是阶段 2 自动修复信号：禁止询问用户是否继续/是否接受当前结果；必须修复实时页面/临时物料问题并重跑${retryScope}物料。`)
    if (captureMeta && captureMeta.runtimeHealth && captureMeta.runtimeHealth.status !== 'passed') {
      console.error(runtimeRepairSentinel(captureMeta.runtimeHealth))
    }
  }
  console.error('')
  console.error('请先运行横向、纵向与 render 自检（同一真实 URL / viewport / session）：')
  console.error('  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-geometry-check.cjs \\')
  console.error("    --url '<页面完整URL>' \\")
  console.error(`    --page-dir d-d2c/temp/${pageDir} \\`)
  console.error(`    --out d-d2c/temp/${pageDir}/r${round || 1}/`)
  console.error('')
  console.error('  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-render-check.cjs \\')
  console.error("    --url '<页面完整URL>' \\")
  console.error(`    --page-dir d-d2c/temp/${pageDir} \\`)
  console.error(`    --out d-d2c/temp/${pageDir}/r${round || 1}/`)
  console.error('')
  console.error('  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-vertical-check.cjs \\')
  console.error("    --url '<页面完整URL>' \\")
  console.error(`    --page-dir d-d2c/temp/${pageDir} \\`)
  if (isCreatePageMode(args, state)) console.error('    --page-flow \\')
  console.error(`    --out d-d2c/temp/${pageDir}/r${round || 1}/`)
  console.error('')
  if (captureMeta && captureMeta.accessMode === 'user-browser-handoff') {
    console.error('本轮使用 user-browser-handoff：自动修复轮次须保持 verified tab active；联合闸可放行或达到最大轮次后再清理：')
    console.error(`  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-browser-handoff.cjs --page-dir d-d2c/temp/${pageDir} --cleanup --result completed`)
  } else {
    console.error('若本轮使用 preview harness，联合闸可放行或达到最大轮次后，进入 evaluator 前再清理临时路由/包装文件；自动修复轮次须保持 active：')
    console.error(`  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-preview-harness.cjs --page-dir d-d2c/temp/${pageDir} --cleanup`)
  }
  console.error('')
  const retryRound = state && state.phase2Block && state.phase2Block.nextRound
    ? Number(state.phase2Block.nextRound)
    : (Number(round) || 1) + 1
  const retryNote = retryRound === (Number(round) || 1)
    ? '当前尝试属于无效/无进展物料，覆盖同轮重采，不消耗有效修复轮次'
    : '代码已形成新的有效修复尝试，进入下一轮并保留本轮证据'
  console.error(`按阻断原因修复实时页面/组件代码或物料链后，重新进入阶段 2：--enter 2 --round ${retryRound}（${retryNote}）；geometry + vertical + render 联合过闸或达到最大有效轮次后才进入阶段 3。`)
  process.exit(1)
}

function isComponentModePageDir(pageDir) {
  return String(pageDir || '')
    .replace(/^\/+|\/+$/g, '')
    .startsWith('component/')
}

/** 复杂页（verdict=complex）时校验组件拆解进度：component-plan.json + 全部组件已完成 */
function validateComplexSplitProgress(stateDir, pageDir, state) {
  if (isComponentModePageDir(pageDir)) {
    console.log(
      'ℹ️ 组件模式（page-dir=component/*）：跳过复杂整页拆解校验；组件模式仅使用根目录 DSL 物料完成组件级闭环。'
    )
    return
  }

  const dir = pageArtifactDir(stateDir, pageDir)
  const complexityPath = path.join(dir, 'complexity-plan.json')
  if (!fs.existsSync(complexityPath)) return // 旧产物/简单流程，不附加要求

  let verdict = null
  try {
    verdict = JSON.parse(fs.readFileSync(complexityPath, 'utf8')).verdict
  } catch (_) {
    return
  }
  if (verdict !== 'complex') return

  const planPath = path.join(dir, 'component-plan.json')
  if (!fs.existsSync(planPath)) {
    console.error('❌ 复杂页（complexity-plan.json verdict=complex）缺少 component-plan.json，禁止标记阶段 1 完成。')
    console.error('   请按 rules/d-d2c-complex-split.md：基于 suggestedComponents 微调边界后落盘 component-plan.json，再逐组件实现。')
    process.exit(1)
  }

  let components = []
  try {
    const plan = JSON.parse(fs.readFileSync(planPath, 'utf8'))
    components = plan.components || (Array.isArray(plan) ? plan : [])
  } catch (err) {
    console.error(`❌ component-plan.json 解析失败: ${err.message}`)
    process.exit(1)
  }
  if (!components.length) {
    console.error('❌ component-plan.json 中没有组件，禁止标记阶段 1 完成。')
    process.exit(1)
  }

  const done = state.components || {}
  const pending = components.filter(
    (c) => !done[c.id] || done[c.id].status !== 'completed'
  )
  if (pending.length) {
    console.error('❌ 复杂页存在未完成的组件小闭环，禁止标记阶段 1 完成。')
    console.error(`   未完成: ${pending.map((c) => `${c.id}（${c.name || '-'}）`).join(', ')}`)
    console.error('')
    console.error('每个组件须依次完成（见 rules/d-d2c-complex-split.md）：')
    console.error(`  1) node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-slice-dsl.cjs --page-dir ${dir} --component <id>`)
    console.error('  2) 只读切片实现组件 .vue 并挂载进页面')
    console.error(`  3a) node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-geometry-check.cjs --url <页面URL> --page-dir ${dir} --component <id>`)
    console.error(`  3b) node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-vertical-check.cjs --url <页面URL> --page-dir ${dir} --component <id>`)
    console.error(`  3c) node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-render-check.cjs --url <页面URL> --page-dir ${dir} --component <id>`)
    console.error(`  4) node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --component-complete <id> --page-dir ${pageDir}`)
    process.exit(1)
  }
  console.log(`✅ 复杂页组件小闭环校验通过（${components.length} 个组件全部完成）`)
}

/** 复杂页：标记单个组件小闭环完成（须在阶段 1 进行中调用） */
function completeComponent(componentId, state, args) {
  const stateDir = args['state-dir'] || 'd-d2c/temp'
  const pageDir = args['page-dir'] || ''
  if (!pageDir) {
    console.error('❌ --component-complete 必须传 --page-dir <name>')
    process.exit(1)
  }
  if (state.currentPhase !== 1 || !state.phases[1] || state.phases[1].status !== 'in_progress') {
    console.error(`❌ 组件小闭环属于阶段 1（代码还原），当前阶段: ${state.currentPhase}。请先 --enter 1。`)
    process.exit(1)
  }

  const diffPath = path.join(
    pageArtifactDir(stateDir, pageDir),
    'components',
    componentId,
    'geometry-diff.json'
  )
  if (!fs.existsSync(diffPath) || fs.statSync(diffPath).size === 0) {
    console.error(`❌ 缺失组件几何自检产物: ${diffPath}`)
    console.error('   组件挂载进页面后须先运行：')
    console.error(`   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-geometry-check.cjs --url <页面URL> --page-dir ${pageArtifactDir(stateDir, pageDir)} --component ${componentId}`)
    process.exit(1)
  }

  let violationCount = 0
  try {
    const g = JSON.parse(fs.readFileSync(diffPath, 'utf8'))
    violationCount = (g.violations || []).length
  } catch (err) {
    console.error(`❌ geometry-diff.json 解析失败: ${err.message}`)
    process.exit(1)
  }

  // 纵向几何自检（top/height/bottom），与横向同为组件过闸前置物料
  const verticalDiffPath = path.join(
    pageArtifactDir(stateDir, pageDir),
    'components',
    componentId,
    'vertical-diff.json'
  )
  if (!fs.existsSync(verticalDiffPath) || fs.statSync(verticalDiffPath).size === 0) {
    console.error(`❌ 缺失组件纵向自检产物: ${verticalDiffPath}`)
    console.error('   组件挂载进页面后须先运行：')
    console.error(`   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-vertical-check.cjs --url <页面URL> --page-dir ${pageArtifactDir(stateDir, pageDir)} --component ${componentId}`)
    process.exit(1)
  }
  // vertical-diff 的 violationCount 只计 primary（可定位根因；derived 级联派生不计）
  let verticalViolations = 0
  try {
    const v = JSON.parse(fs.readFileSync(verticalDiffPath, 'utf8'))
    verticalViolations = Number.isFinite(v.violationCount)
      ? v.violationCount
      : (v.violations || []).filter((x) => x.severity !== 'derived').length
  } catch (err) {
    console.error(`❌ vertical-diff.json 解析失败: ${err.message}`)
    process.exit(1)
  }

  const renderDiffPath = path.join(
    pageArtifactDir(stateDir, pageDir),
    'components',
    componentId,
    'render-diff.json'
  )
  if (!fs.existsSync(renderDiffPath) || fs.statSync(renderDiffPath).size === 0) {
    console.error(`❌ 缺失组件 render 自检产物: ${renderDiffPath}`)
    console.error('   组件挂载进页面后须先运行：')
    console.error(`   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-render-check.cjs --url <页面URL> --page-dir ${pageArtifactDir(stateDir, pageDir)} --component ${componentId}`)
    process.exit(1)
  }
  let renderViolations = 0
  try {
    const renderReport = JSON.parse(fs.readFileSync(renderDiffPath, 'utf8'))
    renderViolations = Number.isFinite(Number(renderReport.hardViolationCount))
      ? Number(renderReport.hardViolationCount)
      : (renderReport.violations || []).filter((item) => item.blocking !== false && item.severity !== 'warning').length
  } catch (err) {
    console.error(`❌ render-diff.json 解析失败: ${err.message}`)
    process.exit(1)
  }

  const ack = args.ack && args.ack !== true ? String(args.ack) : ''
  const totalViolations = violationCount + verticalViolations + renderViolations
  if (totalViolations > 0 && !ack) {
    console.error(
      `❌ 组件 ${componentId} 仍有 ${violationCount} 条横向、${verticalViolations} 条纵向(primary)、${renderViolations} 条 render hard violation。`
    )
    console.error('   要么修复后重跑 geometry / vertical / render 清零，要么逐条核对后用 --ack "<不修理由>" 显式记录；最终整页联合闸仍会重新检查。')
    process.exit(1)
  }

  state.components = state.components || {}
  state.components[componentId] = {
    status: 'completed',
    completedAt: new Date().toISOString(),
    violations: violationCount,
    verticalViolations,
    renderViolations,
    ...(ack ? { ack } : {}),
  }
  console.log(
    `✅ 组件 ${componentId} 小闭环完成（横向=${violationCount}，纵向 primary=${verticalViolations}，render hard=${renderViolations}${ack ? `，ack=${ack}` : ''}）`
  )
  console.log('\n⚠️ 下一步：继续下一个组件的 切片→实现→挂载→geometry/vertical/render→--component-complete；全部组件完成后执行 --complete 1')
  return state
}

// ─── 几何达标硬闸（收尾前强制；通用，换任何页面都成立）────────
/** 坐标/盒尺寸类 violation 的 kind（top/left 漂移、盒宽高偏差）；text-missing、font-size 等非坐标类不计入几何达标闸 */
const GEOMETRY_GATE_KINDS = new Set([
  'top-drift',
  'bottom-drift',
  'left-edge-drift',
  'right-edge-drift',
  'width-mismatch',
  'height-mismatch',
])

/**
 * 从 diff 产物收集坐标违规，并从 items.expected/measured 复算一次。
 * checkpoint 不能完全信任上游 violations；检查器漏报时原始测量仍须阻断。
 */
function collectGeometryGateViolations(filePath, tolerancePx) {
  const out = []
  const seen = new Set()
  if (!filePath || !fs.existsSync(filePath) || fs.statSync(filePath).size === 0) return out
  let data
  try {
    data = JSON.parse(fs.readFileSync(filePath, 'utf8'))
  } catch (_) {
    return out
  }
  const push = (entry) => {
    const key = `${entry.layoutMark || ''}|${entry.kind || ''}`
    if (seen.has(key)) return
    seen.add(key)
    out.push(entry)
  }
  for (const v of data.violations || []) {
    if (!GEOMETRY_GATE_KINDS.has(v.kind)) continue
    if (v.severity === 'derived') continue
    const d = Number(v.diffPx)
    if (!Number.isFinite(d) || Math.abs(d) <= tolerancePx) continue
    push({ layoutMark: v.layoutMark, name: v.name || v.text || '', kind: v.kind, diffPx: d })
  }

  const finite = (value) => {
    if (value === null || value === undefined || value === '') return null
    const n = Number(value)
    return Number.isFinite(n) ? n : null
  }
  const firstFinite = (...values) => {
    for (const value of values) {
      const n = finite(value)
      if (n !== null) return n
    }
    return null
  }
  const addRawDiff = (item, kind, expected, measured) => {
    if (expected === null || measured === null) return
    const diffPx = Math.round((measured - expected) * 10) / 10
    if (Math.abs(diffPx) <= tolerancePx) return
    push({
      layoutMark: item.layoutMark,
      name: item.name || item.text || '',
      kind,
      diffPx,
      recomputedFromItems: true,
    })
  }

  for (const item of data.items || []) {
    if (!item || item.matched !== true || !item.expected || !item.measured) continue
    const expected = item.expected
    const measured = item.measured

    addRawDiff(item, 'top-drift', finite(expected.top), finite(measured.top))
    addRawDiff(item, 'bottom-drift', finite(expected.bottom), finite(measured.bottom))
    addRawDiff(item, 'height-mismatch', finite(expected.height), finite(measured.height))

    const expectedLeft = firstFinite(expected.left, expected.absX)
    const measuredLeft = firstFinite(measured.left, measured.leftDesign)
    const expectedWidth = firstFinite(expected.width, expected.w)
    const measuredWidth = firstFinite(measured.width, measured.wDesign)
    addRawDiff(item, 'left-edge-drift', expectedLeft, measuredLeft)
    addRawDiff(item, 'width-mismatch', expectedWidth, measuredWidth)
    addRawDiff(
      item,
      'right-edge-drift',
      expectedLeft !== null && expectedWidth !== null ? expectedLeft + expectedWidth : null,
      measuredLeft !== null && measuredWidth !== null ? measuredLeft + measuredWidth : null
    )
  }
  return out
}

const GEOMETRY_GATE_TOLERANCE_PX = 8
const GEOMETRY_GATE_MAX_ROUNDS = 8
const DISALLOWED_GEOMETRY_GATE_FLAGS = ['geo-tolerance', 'max-rounds']

function geometryGateConfig(args = {}) {
  const disallowed = Object.keys(args).filter((key) =>
    DISALLOWED_GEOMETRY_GATE_FLAGS.some((flag) => key === flag || key.startsWith(`${flag}=`))
  )
  if (disallowed.length) {
    console.error(
      `❌ 坐标闸参数不允许命令行覆盖：${disallowed.map((flag) => `--${flag}`).join(', ')} 已废弃。固定规则为 |diff| ≤ ${GEOMETRY_GATE_TOLERANCE_PX} 设计px，最大修正轮次 ${GEOMETRY_GATE_MAX_ROUNDS}。`
    )
    process.exit(1)
  }
  return { tolerancePx: GEOMETRY_GATE_TOLERANCE_PX, maxRounds: GEOMETRY_GATE_MAX_ROUNDS }
}

function markParts(mark) {
  const matched = String(mark || '').match(/^L\d+(?:\.\d+)*/)
  return matched ? matched[0].split('.') : []
}

function markRoot(mark) {
  const parts = markParts(mark)
  return parts.slice(0, Math.min(2, parts.length)).join('.')
}

function markDepth(mark) {
  const parts = markParts(mark)
  return parts.length ? parts.length - 1 : 0
}

function markParent(mark) {
  const parts = markParts(mark)
  return parts.slice(0, Math.min(3, parts.length)).join('.')
}

function uniquePush(list, value) {
  if (value && !list.includes(value)) list.push(value)
}

function describeFailure(f) {
  return `${f.source || '?'}|${f.layoutMark || '?'}|${f.kind || '?'}|${f.diffPx}|${f.name || ''}`
}

/**
 * Convert raw geometry/vertical failures into a repair strategy for the next
 * automatic phase-2 round. The coordinate gate remains the source of truth for
 * pass/fail; this only tells the executor which class of fix to attempt first.
 */
function diagnoseCoordinateRepairPlan(gate) {
  const failing = Array.isArray(gate && gate.failing) ? gate.failing : []
  const verticalTop = failing.filter((f) => f.source === 'vertical' && f.kind === 'top-drift' && Number.isFinite(Number(f.diffPx)))
  const verticalHeight = failing.filter((f) => f.source === 'vertical' && f.kind === 'height-mismatch')
  const horizontal = failing.filter((f) => f.source === 'geometry' && /left-edge-drift|right-edge-drift|width-mismatch/.test(f.kind || ''))
  const tolerancePx = Number(gate && gate.tolerancePx) || GEOMETRY_GATE_TOLERANCE_PX
  const maxAbs = failing.length ? Math.max(...failing.map((f) => Math.abs(Number(f.diffPx) || 0))) : 0
  const maxVerticalAbs = verticalTop.length ? Math.max(...verticalTop.map((f) => Math.abs(Number(f.diffPx) || 0))) : 0
  const maxHorizontalAbs = horizontal.length ? Math.max(...horizontal.map((f) => Math.abs(Number(f.diffPx) || 0))) : 0
  const declaredTopLevelMarks = new Set((gate && gate.topLevelMarks || []).map(String))
  const topLevelVertical = verticalTop.filter((f) => (
    declaredTopLevelMarks.size
      ? declaredTopLevelMarks.has(String(f.layoutMark || ''))
      : markDepth(f.layoutMark) <= 1
  ))
  const deepVertical = verticalTop.filter((f) => !topLevelVertical.includes(f))
  const topLevelRoots = new Set(topLevelVertical.map((f) => markRoot(f.layoutMark)).filter(Boolean))
  const parentGroups = new Map()
  for (const f of deepVertical) {
    const key = markParent(f.layoutMark)
    if (!key) continue
    if (!parentGroups.has(key)) parentGroups.set(key, [])
    parentGroups.get(key).push(f)
  }

  let cumulativeGroup = null
  for (const [key, items] of parentGroups.entries()) {
    if (items.length < 3) continue
    const diffs = items.map((x) => Number(x.diffPx)).filter(Number.isFinite)
    if (!diffs.length) continue
    const range = Math.max(...diffs) - Math.min(...diffs)
    const sameDirection = diffs.every((d) => d > 0) || diffs.every((d) => d < 0)
    if (sameDirection && Math.abs(range) >= Math.max(24, tolerancePx * 3)) {
      cumulativeGroup = { key, items, range, maxAbs: Math.max(...diffs.map(Math.abs)) }
      break
    }
  }

  let largeLocalGroup = null
  for (const [key, items] of parentGroups.entries()) {
    if (items.length < 2) continue
    const diffs = items.map((x) => Number(x.diffPx)).filter(Number.isFinite)
    if (!diffs.length) continue
    const sameDirection = diffs.every((d) => d > 0) || diffs.every((d) => d < 0)
    const groupMaxAbs = Math.max(...diffs.map(Math.abs))
    const materialDriftCount = diffs.filter((d) => Math.abs(d) >= tolerancePx * 2).length
    if (sameDirection && materialDriftCount >= 2 && groupMaxAbs >= tolerancePx * 4) {
      largeLocalGroup = { key, items, maxAbs: groupMaxAbs }
      break
    }
  }

  const bottomControl = verticalTop.find((f) => {
    const name = String(f.name || '')
    return Number(f.diffPx) < -120 && /(继续|提交|确认|借款|下一步|按钮|button|submit|cta)/i.test(name)
  })

  const evidence = []
  const actions = []
  const forbiddenActions = []
  let strategy = 'text-horizontal-fix'
  let priority = 'normal'
  const secondaryStrategies = []
  const pageFlowPattern = topLevelVertical.length >= 2 && topLevelRoots.size >= 2
  const cardPattern = Boolean(cumulativeGroup || largeLocalGroup)
  const largestLocal = deepVertical
    .slice()
    .sort((a, b) => Math.abs(Number(b.diffPx)) - Math.abs(Number(a.diffPx)))[0]

  if (pageFlowPattern) {
    strategy = 'page-flow-skeleton-rebuild'
    priority = 'layout-model'
    evidence.push(`top-level anchors drift in ${topLevelRoots.size} sections; topLevelVertical=${topLevelVertical.length}; maxVerticalAbs=${maxVerticalAbs}`)
    evidence.push(`top-level failures: ${topLevelVertical.slice(0, 5).map(describeFailure).join(' ; ')}`)
    actions.push('先按 page-flow-plan / DSL 顶层 bbox 锁定页面主区块 top/height，再处理子文本横向问题')
    actions.push('检查顶层区块是否存在负 gap / overlap；禁止用普通正 margin 串行堆叠重叠区块')
    forbiddenActions.push('禁止只调单个元素 margin-top/line-height/text-align 后直接进入下一轮')
    if (cardPattern) uniquePush(secondaryStrategies, 'card-internal-rhythm-rebuild')
  } else if (bottomControl) {
    strategy = 'fixed-cta-misuse'
    priority = 'layout-model'
    evidence.push(`bottom operation/control negative drift: ${describeFailure(bottomControl)}`)
    actions.push('核对底部操作区/CTA 是否误用 fixed/sticky；除非 DSL 明确 sticky/fixed，否则改为文档流或 DSL bbox 定位')
    forbiddenActions.push('禁止把底部 CTA 巨大 top-drift 直接标记为检查器伪影')
    if (cardPattern) uniquePush(secondaryStrategies, 'card-internal-rhythm-rebuild')
  } else if (cardPattern) {
    strategy = 'card-internal-rhythm-rebuild'
    priority = 'layout-model'
    const group = cumulativeGroup || largeLocalGroup
    evidence.push(
      cumulativeGroup
        ? `cumulative sibling drift in ${group.key}; count=${group.items.length}; range=${Math.round(group.range)}px`
        : `multiple same-direction sibling drifts in ${group.key}; count=${group.items.length}; maxAbs=${Math.round(group.maxAbs)}px`
    )
    actions.push('重建命中卡片/列表内部纵向节奏：按 DSL row top/height/gap 使用 grid 或卡片内 CSS absolute')
    actions.push('禁止通用 .card padding + .row padding 叠加撑高多行 KV/list')
    forbiddenActions.push('禁止把同一卡片内逐行递增漂移当成多个独立 margin 问题')
  } else if (verticalTop.length) {
    strategy = 'local-anchor-position-fix'
    priority = 'targeted-layout'
    evidence.push(`no top-level/card-wide pattern; verticalTop=${verticalTop.length}; deepest maxAbs=${maxVerticalAbs}`)
    if (largestLocal) evidence.push(`largest local anchor: ${describeFailure(largestLocal)}; localGroup=${markParent(largestLocal.layoutMark)}`)
    actions.push('先修偏差最大的最小共同父容器或真实 TEXT 测量盒：按 DSL bbox 校正 top/height/line-height，再重跑坐标报告确认是否消除级联')
    actions.push('若多个失败项不共享卡片父级，按绝对偏差从大到小逐个修局部锚点，保留已过闸区块')
    forbiddenActions.push('禁止仅凭一个深层节点的大偏差重建整页 page-flow 或整张卡片')
  } else {
    evidence.push(`horizontal-only/no vertical top-drift; maxAbs=${maxAbs}`)
    actions.push('优先修复 geometry 横向宽度/左右缘：nowrap、字体、letter-spacing、右对齐和 data-d2c-mark 落点')
  }

  if (horizontal.length) {
    if (strategy !== 'text-horizontal-fix') uniquePush(secondaryStrategies, 'horizontal-geometry-fix')
    evidence.push(`horizontal geometry failures: count=${horizontal.length}; maxHorizontalAbs=${maxHorizontalAbs}`)
    actions.push('同步修复 geometry 横向宽度/左右缘：按 DSL bbox 核对容器 width、文本测量 mark、nowrap/white-space、右对齐与资源尺寸')
  }
  if (verticalHeight.length) actions.push('逐条核对 vertical height-mismatch；确认是容器/文本 mark 错位还是真实盒高错误')

  return {
    version: '2.0',
    strategy,
    secondaryStrategies,
    priority,
    evidence,
    actions,
    forbiddenActions,
    stats: {
      failingCount: failing.length,
      verticalTopCount: verticalTop.length,
      topLevelVerticalCount: topLevelVertical.length,
      deepVerticalCount: deepVertical.length,
      horizontalCount: horizontal.length,
      maxAbs,
      maxVerticalAbs,
      maxHorizontalAbs,
    },
  }
}

function collectGeometryGateStatus(stateDir, pageDir, round, args = {}) {
  const { tolerancePx, maxRounds } = geometryGateConfig(args)
  const roundNum = Number(round) || 1
  const roundDir = path.join(pageArtifactDir(stateDir, pageDir), `r${roundNum}`)
  const geomPath = path.join(roundDir, 'geometry-diff.json')
  const vertPath = path.join(roundDir, 'vertical-diff.json')
  const hasGeom = fs.existsSync(geomPath) && fs.statSync(geomPath).size > 0
  const hasVert = fs.existsSync(vertPath) && fs.statSync(vertPath).size > 0
  const verticalReport = hasVert ? readJsonArtifact(vertPath) : null
  const topLevelMarks = verticalReport && verticalReport.scope && Array.isArray(verticalReport.scope.layoutMarks)
    ? verticalReport.scope.layoutMarks.map(String)
    : []
  const failing = [
    ...collectGeometryGateViolations(hasGeom ? geomPath : null, tolerancePx).map((x) => ({ ...x, source: 'geometry' })),
    ...collectGeometryGateViolations(hasVert ? vertPath : null, tolerancePx).map((x) => ({ ...x, source: 'vertical' })),
  ]
  const top = failing
    .slice()
    .sort((a, b) => Math.abs(b.diffPx) - Math.abs(a.diffPx))
    .slice(0, 12)
  const status = {
    tolerancePx,
    maxRounds,
    roundNum,
    roundDir,
    geomPath,
    vertPath,
    hasGeom,
    hasVert,
    topLevelMarks,
    failing,
    top,
  }
  status.repairPlan = diagnoseCoordinateRepairPlan(status)
  return status
}

function collectRenderGateViolations(filePath) {
  if (!filePath || !fs.existsSync(filePath) || fs.statSync(filePath).size === 0) return []
  const report = readJsonArtifact(filePath)
  if (!report) return []
  const violations = (report.violations || []).filter((item) => (
    item && item.blocking !== false && item.severity !== 'warning'
  ))
  const declared = Number(report.hardViolationCount ?? report.violationCount)
  if (Number.isFinite(declared) && declared !== violations.length) {
    violations.push({
      layoutMark: 'report',
      name: 'render-diff.json',
      kind: 'render-report-count-mismatch',
      category: 'report-integrity',
      severity: 'hard',
      blocking: true,
      repairClass: 'source-material',
      expected: `hardViolationCount=${violations.length}`,
      actual: `declared=${declared}`,
    })
  }
  return violations
}

function diagnoseRenderRepairPlan(gate) {
  const failing = (gate && gate.failing) || []
  const source = failing.filter((item) => item.repairClass === 'source-material')
  const categories = new Set(failing.map((item) => item.category).filter(Boolean))
  const kinds = new Set(failing.map((item) => item.kind).filter(Boolean))
  let strategy = 'render-style-fix'
  let priority = 'high'
  const actions = []
  const forbiddenActions = [
    '不要通过删除 data-d2c-mark、expected style 或 render-diff violation 规避硬闸',
    '不要用截图/overlay 像素猜值替代 DSL 与 computed style 的确定性修复',
  ]

  if (source.length) {
    strategy = 'render-source-material-fix'
    priority = 'critical'
    actions.push('先修复 DSL 样式序列化、字体/资源清单或资源导出；覆盖同轮重采，不消耗有效修复轮次')
  }
  if (categories.has('asset')) actions.push('按 dsl-assets.json 的 nodeId/layoutMark 使用已导出资源，并确认浏览器实际加载成功；复杂 PATH 禁止降级为普通背景')
  if (kinds.has('asset-background-contamination')) actions.push('把已匹配的 SVG/透明资源作为该标记节点的唯一绘制源：移除同节点 CSS background/background-color，保留资源内部孔洞、描边和 alpha')
  if (kinds.has('opacity-mismatch')) actions.push('核对有效透明度乘积；SVG 已内置 opacity 时外层设为 1，未内置时才由承载节点应用 DSL opacity，禁止重复衰减')
  if (categories.has('text-integrity')) actions.push('修复文本固定宽、overflow、white-space 与真实字体加载，确保字形 Range 完整落在容器内')
  if (categories.has('typography')) actions.push('按 span 逐项修复 font-family/semantic weight/size/line-height/letter-spacing/color/decoration，避免专用 Black 字体再次叠加合成字重')
  if (categories.has('shape') || categories.has('stroke')) actions.push('按 DSL 显式或保守推导值修复 border-radius、border width/color；复杂轮廓优先使用 SVG')
  if (categories.has('paint') || categories.has('effect')) actions.push('修复 background/gradient/opacity/box-shadow；先用浏览器 CSS parser 验证声明有效且保留全部图层')
  if (!actions.length) actions.push('按 render-diff.json 的 expected/actual 逐项修复最高置信度视觉契约')

  return {
    strategy,
    priority,
    actions: [...new Set(actions)],
    forbiddenActions,
    evidence: [
      `hard render failures=${failing.length}`,
      `source-material=${source.length}`,
      `categories=${Array.from(categories).join(',') || 'unknown'}`,
      `kinds=${Array.from(kinds).slice(0, 8).join(',') || 'unknown'}`,
    ],
  }
}

function collectRenderGateStatus(stateDir, pageDir, round) {
  const roundNum = Number(round) || 1
  const roundDir = path.join(pageArtifactDir(stateDir, pageDir), `r${roundNum}`)
  const renderPath = path.join(roundDir, 'render-diff.json')
  const hasRender = fs.existsSync(renderPath) && fs.statSync(renderPath).size > 0
  const failing = collectRenderGateViolations(hasRender ? renderPath : null).map((item) => ({
    ...item,
    source: 'render',
  }))
  const top = failing.slice(0, 12)
  const status = {
    maxRounds: GEOMETRY_GATE_MAX_ROUNDS,
    roundNum,
    roundDir,
    renderPath,
    hasRender,
    failing,
    top,
    sourceMaterialFailing: failing.filter((item) => item.repairClass === 'source-material'),
  }
  status.repairPlan = diagnoseRenderRepairPlan(status)
  return status
}

function combinePhase2Gate(coordinateGate, renderGate) {
  const coordinateFailing = (coordinateGate && coordinateGate.failing) || []
  const renderFailing = (renderGate && renderGate.failing) || []
  const failing = [...coordinateFailing, ...renderFailing]
  const renderPlan = diagnoseRenderRepairPlan(renderGate || {})
  let repairPlan = renderPlan
  if (coordinateFailing.length) {
    const coordinatePlan = coordinateGate.repairPlan || diagnoseCoordinateRepairPlan(coordinateGate)
    repairPlan = {
      ...coordinatePlan,
      secondaryStrategies: [
        ...(coordinatePlan.secondaryStrategies || []),
        ...(renderFailing.length ? [renderPlan.strategy] : []),
      ],
      actions: [...new Set([
        ...(coordinatePlan.actions || []),
        ...(renderFailing.length ? renderPlan.actions : []),
      ])],
      forbiddenActions: [...new Set([
        ...(coordinatePlan.forbiddenActions || []),
        ...(renderFailing.length ? renderPlan.forbiddenActions : []),
      ])],
    }
  }
  return {
    tolerancePx: coordinateGate.tolerancePx,
    maxRounds: coordinateGate.maxRounds,
    roundNum: coordinateGate.roundNum,
    roundDir: coordinateGate.roundDir,
    hasGeom: coordinateGate.hasGeom,
    hasVert: coordinateGate.hasVert,
    hasRender: Boolean(renderGate && renderGate.hasRender),
    coordinateFailing,
    renderFailing,
    sourceMaterialFailing: renderFailing.filter((item) => item.repairClass === 'source-material'),
    failing,
    top: [
      ...coordinateGate.top,
      ...renderGate.top,
    ].slice(0, 16),
    repairPlan,
  }
}

function collectPhase2GateStatus(stateDir, pageDir, round, args = {}) {
  return combinePhase2Gate(
    collectGeometryGateStatus(stateDir, pageDir, round, args),
    collectRenderGateStatus(stateDir, pageDir, round)
  )
}

function recordGeometryGateState(state, gate, passed) {
  if (!state) return
  state.geometryGate = {
    round: gate.roundNum,
    tolerancePx: gate.tolerancePx,
    maxRounds: gate.maxRounds,
    passed,
    failingCount: gate.failing.length,
    maxDiffPx: gate.failing.length ? Math.max(...gate.failing.map((f) => Math.abs(f.diffPx))) : 0,
    top: gate.top,
    repairPlan: gate.repairPlan || null,
  }
}

function recordRenderGateState(state, gate, passed) {
  if (!state) return
  state.renderGate = {
    round: gate.roundNum,
    maxRounds: gate.maxRounds,
    passed,
    failingCount: gate.failing.length,
    sourceMaterialFailingCount: (gate.sourceMaterialFailing || []).length,
    top: gate.top,
    repairPlan: gate.repairPlan || null,
  }
}

function formatGeometryGateTop(top) {
  if (!top || !top.length) return '无'
  return top
    .map((f) => `${f.source}|${f.layoutMark}|${f.kind}|${f.diffPx}|${f.name}`)
    .join('；')
}

function printGeometryGateTop(top, printer = console.error) {
  printer('   偏差最大的若干项（source | mark | kind | diffPx 设计px | name）：')
  top.forEach((f) => printer(`     - ${f.source} | ${f.layoutMark} | ${f.kind} | ${f.diffPx} | ${f.name}`))
}

function formatPhase2GateTop(top) {
  if (!top || !top.length) return '无'
  return top.map((item) => {
    const metric = Number.isFinite(Number(item.diffPx))
      ? `${item.diffPx}px`
      : `${item.expected ?? '?'} -> ${item.actual ?? '?'}`
    return `${item.source || '?'}|${item.layoutMark || '?'}|${item.kind || '?'}|${metric}|${item.name || ''}`
  }).join('；')
}

function printPhase2GateTop(top, printer = console.error) {
  printer('   偏差最大的若干项（source | mark | kind | diff/expected→actual | name）：')
  for (const item of top || []) {
    const metric = Number.isFinite(Number(item.diffPx))
      ? `${item.diffPx}px`
      : `${JSON.stringify(item.expected ?? '?')} -> ${JSON.stringify(item.actual ?? '?')}`
    printer(`     - ${item.source || '?'} | ${item.layoutMark || '?'} | ${item.kind || '?'} | ${metric} | ${item.name || ''}`)
  }
}

/**
 * phase-2 联合硬闸（阶段 4 --decision cancel 时兜底）：
 * 读取当前轮 geometry-diff.json + vertical-diff.json + render-diff.json。
 *  - 未达标 且 未达最大修正轮次 → 拒绝 cancel（非 0 退出），要求 --decision continue 进入下一轮按实测 diff 修正。
 *  - 达标 或 已达最大修正轮次 → 放行；达上限仍不达标时分别写入 state.geometryGate/renderGate，禁止静默当"已通过"。
 * 通用：对任何多区块/多元素页面都成立，不针对单一页面。允差固定 8 设计px，最大修正轮次固定 8；禁止用命令行参数覆盖。
 */
function enforcePhase2Gate(stateDir, pageDir, round, args, state) {
  if (!pageDir) {
    console.error('❌ 联合达标闸：--complete 4 必须传 --page-dir <name>')
    process.exit(1)
  }
  const gate = collectPhase2GateStatus(stateDir, pageDir, round, args)
  const coordinateGate = collectGeometryGateStatus(stateDir, pageDir, round, args)
  const renderGate = collectRenderGateStatus(stateDir, pageDir, round)

  if (!gate.hasGeom || !gate.hasVert || !gate.hasRender) {
    console.error('❌ 联合达标闸：当前轮缺少 geometry-diff.json / vertical-diff.json / render-diff.json，禁止收尾。')
    console.error(`   轮次目录: ${path.resolve(gate.roundDir)}`)
    console.error('   请先在物料阶段运行 geometry、vertical、render 三个检查器后重试。')
    process.exit(1)
  }

  if (!gate.failing.length) {
    recordGeometryGateState(state, coordinateGate, true)
    recordRenderGateState(state, renderGate, true)
    console.log(`✅ 联合达标闸通过：r${gate.roundNum} geometry + vertical + render 均无硬违规，放行收尾。`)
    return
  }

  recordGeometryGateState(state, coordinateGate, coordinateGate.failing.length === 0)
  recordRenderGateState(state, renderGate, renderGate.failing.length === 0)

  if (gate.roundNum < gate.maxRounds) {
    console.error(`❌ 联合达标闸未通过：r${gate.roundNum} 仍有 coordinate=${gate.coordinateFailing.length}、render=${gate.renderFailing.length} 条硬违规。`)
    console.error(`   禁止 --decision cancel 收尾（当前轮 ${gate.roundNum} < 最大修正轮次 ${gate.maxRounds}）。`)
    printPhase2GateTop(gate.top, console.error)
    console.error('')
    console.error('   请改用 `--decision continue` 进入下一轮，按上列确定性证据修正坐标、样式、字体或资源后重跑三项检查；')
    console.error('   联合硬闸通过、或已达最大有效修正轮次，方可 `--decision cancel` 收尾。')
    process.exit(1)
  }

  console.log(`⚠️ 联合达标闸：已达最大有效修正轮次 ${gate.maxRounds}，r${gate.roundNum} 仍有 coordinate=${gate.coordinateFailing.length}、render=${gate.renderFailing.length} 条硬违规。放行收尾，但最终摘要（阶段 6）必须如实披露，禁止当"已通过"：`)
  printPhase2GateTop(gate.top, console.log)
}

function rejectContinueAfterMaxPhase2Rounds(stateDir, pageDir, round, args, state) {
  if (!pageDir) {
    console.error('❌ 联合达标闸：--complete 4 必须传 --page-dir <name>')
    process.exit(1)
  }
  const gate = collectPhase2GateStatus(stateDir, pageDir, round, args)

  if (!gate.hasGeom || !gate.hasVert || !gate.hasRender || !gate.failing.length) return

  recordGeometryGateState(state, collectGeometryGateStatus(stateDir, pageDir, round, args), gate.coordinateFailing.length === 0)
  recordRenderGateState(state, collectRenderGateStatus(stateDir, pageDir, round), gate.renderFailing.length === 0)

  if (gate.roundNum < gate.maxRounds) return

  console.error(`❌ 联合修复循环已结束：r${gate.roundNum} 已达到最大有效修正轮次 ${gate.maxRounds}，仍有 coordinate=${gate.coordinateFailing.length}、render=${gate.renderFailing.length} 条硬违规。`)
  printPhase2GateTop(gate.top, console.error)
  console.error('')
  console.error('   禁止 --decision continue 生成下一轮非终止状态；这会让 workflow-state 停在阶段 4，且 d2c-run-summary.json.completeRun=false。')
  console.error('   请改用：')
  console.error('     node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --complete 4 --page-dir <page-dir> --decision cancel')
  console.error('     node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --enter 6 --page-dir <page-dir>')
  console.error('     node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --complete 6 --page-dir <page-dir>')
  console.error('   阶段 6 摘要必须披露上述残余偏差；若仍需人工继续打磨，应在本次完整收尾后另起新任务。')
  process.exit(1)
}

function ensureWorkflowNotPaused(state) {
  if (!state.workflowPause) return
  const pause = state.workflowPause
  console.error(`❌ d-d2c workflow 当前处于暂停状态：kind=${pause.kind || '?'} reason=${pause.reason || '?'}`)
  console.error('   获得所需输入或确认继续后，先执行 --resume --page-dir <page-dir>。')
  process.exit(1)
}

function pauseWorkflow(state, args) {
  const kind = String(args.pause || '').trim()
  const reason = String(args.reason || '').trim()
  const detail = String(args.detail || '').trim()
  const evidence = String(args.evidence || '').trim()
  if (!ALLOWED_PAUSE_KINDS.has(kind)) {
    console.error(`❌ --pause 只允许 external-input 或 user-cancelled，当前: ${kind || 'missing'}`)
    process.exit(1)
  }
  if (!reason) {
    console.error('❌ --pause 必须提供 --reason <稳定原因码>。')
    process.exit(1)
  }
  if (kind === 'external-input' && !detail && !evidence) {
    console.error('❌ external-input 暂停必须提供 --detail 或 --evidence，禁止用无证据阻塞逃避工作流。')
    process.exit(1)
  }
  state.workflowPause = {
    kind,
    reason,
    detail: detail || null,
    evidence: evidence || null,
    createdAt: new Date().toISOString(),
  }
  console.log(`D2C_WORKFLOW_PAUSED kind=${kind} reason=${reason}`)
  return state
}

function resumeWorkflow(state) {
  if (!state.workflowPause) {
    console.log('ℹ️ d-d2c workflow 未处于显式暂停状态，无需恢复。')
    return state
  }
  const previous = state.workflowPause
  delete state.workflowPause
  state.workflowResumedAt = new Date().toISOString()
  console.log(`✅ d-d2c workflow 已恢复：kind=${previous.kind || '?'} reason=${previous.reason || '?'}`)
  return state
}

// ─── enter ───────────────────────────────────────────
function enterPhase(phase, state, args) {
  ensureWorkflowNotPaused(state)
  const phaseNum = Number(phase)
  const def = PHASES[phaseNum]
  if (!def) {
    console.error(`❌ 未知阶段: ${phase}`)
    process.exit(1)
  }

  // 校验前置阶段
  if (def.prev !== null) {
    const prevCompleted = state.phases[def.prev] && state.phases[def.prev].status === 'completed'
    if (!prevCompleted) {
      console.error(`❌ 阶段 ${phaseNum}（${def.name}）的前置阶段 ${def.prev}（${PHASES[def.prev].name}）未完成，禁止进入。`)
      console.error(`当前状态：${JSON.stringify(state.phases[def.prev] || { status: '未开始' })}`)
      process.exit(1)
    }
    // 阶段 6 额外校验：阶段 4 必须 decision=cancel
    if (phaseNum === 6) {
      const p4 = state.phases[4]
      if (!p4 || p4.decision !== 'cancel') {
        console.error(`❌ 进入阶段 6 要求阶段 4 的 decision=cancel，当前: ${p4 ? p4.decision : '无'}`)
        process.exit(1)
      }
    }
  }

  applyWorkflowMetadata(state, args)
  // 更新轮次
  if (phaseNum === 2 && args.round && state.phase2Block) {
    const enteringRound = Number(args.round)
    const blockedRound = Number(state.phase2Block.currentRound) || 0
    if (Number.isFinite(enteringRound) && enteringRound > blockedRound) {
      delete state.phase2Block
    }
  }
  if (args.round) state.round = Number(args.round)

  state.currentPhase = phaseNum
  state.phases[phaseNum] = {
    status: 'in_progress',
    enteredAt: new Date().toISOString(),
  }

  console.log(`✅ 进入阶段 ${phaseNum}（${def.name}）${state.round ? '，轮次 r' + state.round : ''}`)
  return state
}

// ─── complete ────────────────────────────────────────
function completePhase(phase, state, args) {
  ensureWorkflowNotPaused(state)
  const phaseNum = Number(phase)
  const def = PHASES[phaseNum]
  if (!def) {
    console.error(`❌ 未知阶段: ${phase}`)
    process.exit(1)
  }

  if (state.currentPhase !== phaseNum || !state.phases[phaseNum] || state.phases[phaseNum].status !== 'in_progress') {
    console.error(`❌ 当前不在阶段 ${phaseNum}（${def.name}），无法标记完成。当前阶段: ${state.currentPhase}`)
    process.exit(1)
  }

  // 阶段 1：DSL 物料齐全方可标记代码还原完成；复杂页另须全部组件小闭环完成
  if (phaseNum === 1) {
    const stateDir = args['state-dir'] || 'd-d2c/temp'
    const pageDir = args['page-dir'] || ''
    validatePhase1Artifacts(stateDir, pageDir)
    validateMixedTextImplementation(
      stateDir,
      pageDir,
      isUpdatePageMode(args, state) ? state.designScope : ''
    )
    validateVueStyleLangs()
    validateBareScriptImports()
    validatePageFlowPlanLayering(stateDir, pageDir)
    validateComplexSplitProgress(stateDir, pageDir, state)
    validatePhase1RuntimeHealth(stateDir, pageDir)
  }

  // 阶段 2：当前轮 geometry/vertical/render 自检存在且通过联合前置闸，方可标记物料生成完成
  if (phaseNum === 2) {
    const stateDir = args['state-dir'] || 'd-d2c/temp'
    const pageDir = args['page-dir'] || ''
    validatePhase2Artifacts(stateDir, pageDir, state.round || 1, args, state)
  }

  // 阶段 3 特殊处理：必须传 report + score，自动打印中文摘要
  if (phaseNum === 3) {
    if (!args.report || !args.score) {
      console.error('❌ 阶段 3 --complete 必须传 --report <报告路径> --score <分数>')
      process.exit(1)
    }
    state.phases[phaseNum].report = args.report
    state.phases[phaseNum].score = Number(args.score)

    // 自动打印中文摘要
    if (args['print-report'] !== false) {
      console.log('\n--- 中文评测摘要 ---')
      try {
        // 查找 d2c-print-fidelity-report.cjs 的位置
        const scriptDir = __dirname
        const printScript = path.join(scriptDir, 'd2c-print-fidelity-report.cjs')
        if (fs.existsSync(printScript)) {
          const output = execSync(`node "${printScript}" "${args.report}"`, {
            encoding: 'utf8',
            timeout: 10000,
          })
          console.log(output)
        } else {
          console.error(`⚠️ 未找到 d2c-print-fidelity-report.cjs，路径: ${printScript}`)
          console.log(`报告路径: ${args.report}`)
          console.log(`还原度分数: ${args.score}`)
        }
      } catch (err) {
        console.error(`⚠️ 打印中文摘要失败: ${err.message}`)
        console.log(`报告路径: ${args.report}`)
        console.log(`还原度分数: ${args.score}`)
      }
      console.log('--- 中文评测摘要结束 ---\n')
    }
  }

  // 阶段 4 特殊处理：必须传 decision
  if (phaseNum === 4) {
    const decision = args.decision
    if (!decision || !['continue', 'cancel'].includes(decision)) {
      console.error(`❌ 阶段 4 --complete 必须传 --decision <continue|cancel>，当前: ${decision}`)
      process.exit(1)
    }
    const stateDir = args['state-dir'] || 'd-d2c/temp'
    const pageDir = args['page-dir'] || ''
    // 联合达标硬闸：cancel 收尾前必须 geometry + vertical + render 达标或已达最大有效修正轮次
    if (decision === 'cancel') {
      enforcePhase2Gate(stateDir, pageDir, state.round || 1, args, state)
    } else {
      // 达到最大修正轮次后再 continue 会制造无法完整收尾的 workflow 状态。
      rejectContinueAfterMaxPhase2Rounds(stateDir, pageDir, state.round || 1, args, state)
    }
    state.phases[phaseNum].decision = decision
  }

  state.phases[phaseNum].status = 'completed'
  state.phases[phaseNum].completedAt = new Date().toISOString()

  console.log(`✅ 阶段 ${phaseNum}（${def.name}）完成。`)

  // 输出下一步指令
  const nextStep = NEXT_STEP[phaseNum]
  if (nextStep) {
    console.log(`\n⚠️ 下一步：${nextStep}`)
  }

  // 阶段 4 额外输出决策结果
  if (phaseNum === 4) {
    console.log(`\n决策结果: ${args.decision}`)
    if (args.decision === 'continue') {
      const nextRound = (state.round || 1) + 1
      state.round = nextRound
      console.log(`下一轮次: r${nextRound}`)
      // 重置阶段 2/3/4 状态以便下一轮
      delete state.phases[2]
      delete state.phases[3]
      delete state.phases[4]
    }
  }

  return state
}

// ─── status ──────────────────────────────────────────
function showStatus(state) {
  console.log('===== d-d2c 工作流状态 =====')
  console.log(`当前阶段: ${state.currentPhase !== null && state.currentPhase !== undefined ? state.currentPhase : '未开始'}`)
  console.log(`当前轮次: r${state.round || 1}`)
  console.log('')
  if (state.mode) {
    console.log(`工作流模式: ${state.mode}`)
    if (state.targetVue) console.log(`目标 Vue: ${state.targetVue}`)
    console.log('')
  }
  if (state.updatePagePlacement) {
    const p = state.updatePagePlacement
    console.log(`已有页落位: status=${p.status || '?'} scope=${p.scoreScope || '?'} accessMode=${p.accessMode || '?'} selector=${p.selector || '?'} anchor=${p.anchor || '?'}`)
    console.log('')
  }
  Object.entries(PHASES).forEach(([num, def]) => {
    const p = state.phases[num]
    const status = p ? p.status : '未开始'
    let extra = ''
    if (p && p.score !== undefined) extra += ` score=${p.score}`
    if (p && p.decision) extra += ` decision=${p.decision}`
    if (p && p.report) extra += ` report=${p.report}`
    console.log(`  阶段 ${num}（${def.name}）: ${status}${extra}`)
  })

  const components = state.components || {}
  if (Object.keys(components).length) {
    console.log('')
    console.log('  组件小闭环（复杂页拆解）:')
    for (const [id, c] of Object.entries(components)) {
      console.log(
        `    ${id}: ${c.status} violations=${c.violations ?? '?'}${c.ack ? ` ack=${c.ack}` : ''}`
      )
    }
  }

  if (state.phase2Block && state.phase2Block.allowEvaluator === false) {
    const b = state.phase2Block
    console.log('')
    console.log('  阶段 2 自动修复阻塞:')
    console.log(`    reason=${b.reason || '?'} currentRound=r${b.currentRound || '?'} nextRound=r${b.nextRound || '?'} consumeRound=${b.consumeRound !== false}`)
    console.log(`    askUser=false allowAcceptCurrent=false allowEvaluator=false failingCount=${b.failingCount ?? '?'}`)
    if (b.top && b.top.length) {
      console.log(`    top=${formatPhase2GateTop(b.top)}`)
    }
    console.log(`    下一步：自动修复实时页面/清理临时 preview 残留，重新生成${b.consumeRound === false ? '同轮' : '下一轮'}物料；禁止询问用户是否继续或接受当前结果。`)
  }

  // 输出下一步建议
  const current = state.currentPhase
  if (current !== null && current !== undefined) {
    const p = state.phases[current]
    if (p && p.status === 'in_progress') {
      console.log(`\n⚠️ 当前阶段 ${current}（${PHASES[current].name}）进行中，完成后执行 --complete ${current}`)
    } else if (p && p.status === 'completed') {
      const next = NEXT_STEP[current]
      if (next) console.log(`\n⚠️ 下一步：${next}`)
    }
  }
}

// ─── main ────────────────────────────────────────────
function main() {
  const args = parseArgs(process.argv)

  const stateDir = args['state-dir'] || 'd-d2c/temp'
  const pageDir = args['page-dir'] || ''

  if (!pageDir && !args.status) {
    // 尝试从环境或当前目录推断
    // 但对 --status 不强制要求
  }

  const sPath = pageDir ? stateFilePath(stateDir, pageDir) : path.join(stateDir, 'workflow-state.json')
  let state = readState(sPath)

  // 结构化日志：page 目录可解析时启用，否则 no-op。
  const log = getLogger(pageDir ? pageArtifactDir(stateDir, pageDir) : null)

  applyWorkflowMetadata(state, args)

  if (args.enter) {
    state = enterPhase(args.enter, state, args)
    persistWorkflowState(sPath, stateDir, pageDir, state, true)
    log.phaseEnter && log.phaseEnter(args.enter, { round: state.round || 1 })
  } else if (args['component-complete']) {
    state = completeComponent(String(args['component-complete']), state, args)
    persistWorkflowState(sPath, stateDir, pageDir, state, true)
    if (log.event) {
      const comp = (state.components || {})[String(args['component-complete'])] || {}
      log.event('script.complete', {
        script: 'component-complete', component: String(args['component-complete']),
        violations: comp.violations, verticalViolations: comp.verticalViolations, ack: comp.ack || null,
      })
    }
  } else if (args.complete) {
    state = completePhase(args.complete, state, args)
    persistWorkflowState(sPath, stateDir, pageDir, state, true)
    const phaseNum = Number(args.complete)
    log.phaseComplete && log.phaseComplete(phaseNum, { round: state.round || 1 })
    if (phaseNum === 3 && log.evaluationComplete) {
      log.evaluationComplete({ score: Number(args.score), report: args.report, round: state.round || 1 })
    }
    if (phaseNum === 4 && log.decisionRecorded) {
      log.decisionRecorded(args.decision, { round: state.round || 1 })
    }
  } else if (args.pause) {
    state = pauseWorkflow(state, args)
    persistWorkflowState(sPath, stateDir, pageDir, state, true)
  } else if (args.resume) {
    state = resumeWorkflow(state)
    persistWorkflowState(sPath, stateDir, pageDir, state, true)
  } else if (args.status) {
    syncWorkflowGuard(stateDir, pageDir, state, sPath)
    showStatus(state)
  } else {
    console.error('用法：')
    console.error('  --enter <phase>      进入阶段')
    console.error('  --complete <phase>   标记阶段完成')
    console.error('  --pause <kind>       记录 external-input / user-cancelled 合法暂停')
    console.error('  --resume             清除显式暂停并继续当前阶段')
    console.error('  --status             查看当前状态')
    console.error('')
    console.error('必须配合 --page-dir <name> 指定页面目录')
    process.exit(1)
  }
}

if (require.main === module) main()

module.exports = {
  resolvePageFlowReportMarks,
  collectRenderGateViolations,
  combinePhase2Gate,
  compareRoundMaterialFingerprints,
  diagnoseCoordinateRepairPlan,
  diagnoseRenderRepairPlan,
  findMarkedElementBodies,
  markDepth,
  markParent,
  markRoot,
  measurementReportDigest,
  nextPhase2Round,
  readRoundMaterialFingerprint,
  recordPhase2AutoFixBlock,
  validateMixedTextContractsFromSources,
  validatePhase1RuntimeHealth,
  validateUserBrowserHandoff,
}
