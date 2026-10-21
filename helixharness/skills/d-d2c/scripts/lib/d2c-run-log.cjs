'use strict'

/**
 * d2c-run-log — 结构化运行日志（供 d2c-eval-loop 等外层采集）。
 *
 * 门禁：日志仅服务于 d2c-eval-loop，**默认关闭**。仅当环境变量 D2C_EVAL_LOOP 置位时
 * 才打点存 log（loop 通过 executor 的 settings profile env 注入）；普通 d-d2c 运行不留日志。
 *
 * 设计原则：
 *  - 非侵入、可降级：任何写日志失败都被吞掉，绝不影响宿主脚本主流程。
 *  - 多进程安全：每个 d-d2c 脚本是独立进程，事件以 append-only 方式写入
 *    <pageArtifactDir>/d2c-run.jsonl；summary 每次从整份 jsonl 无状态重建。
 *
 * 用法：
 *   const { getLogger } = require('./lib/d2c-run-log.cjs')
 *   const log = getLogger(pageArtifactDir)   // 解析不到目录则返回 no-op logger
 *   log.phaseEnter(2, { round: 1 })
 *   log.scriptComplete('d2c-geometry-check', { ok: true })
 *   log.checkViolation('geometry', { violationCount: 28, checkedCount: 28 })
 *   log.artifactWrite('/abs/path/geometry-diff.json')
 *
 * 事件类型：phase.enter / phase.complete / script.start / script.complete /
 *   artifact.write / check.warning / check.violation / evaluation.complete /
 *   decision.recorded / error
 */

const fs = require('fs')
const path = require('path')
let maskUrl = (u) => u
try { ({ maskUrl } = require('./d2c-url-params.cjs')) } catch { /* 静默降级 */ }

const JSONL = 'd2c-run.jsonl'
const SUMMARY = 'd2c-run-summary.json'
const SENSITIVE_KEY = /^(token|auth|authorization|access[_-]?token|ticket|sid|session|secret|password|pwd)$/i
const SENSITIVE_QUERY_RE = /([?&](?:token|auth|authorization|access[_-]?token|ticket|sid|session|secret|password|pwd)=)[^&\s"'`]+/gi

function safe(fn) {
  try { return fn() } catch { return undefined }
}

function redactString(value) {
  return maskUrl(String(value)).replace(SENSITIVE_QUERY_RE, '$1***')
}

function redactForLog(value, key = '') {
  if (SENSITIVE_KEY.test(String(key || ''))) return '***'
  if (typeof value === 'string') return redactString(value)
  if (Array.isArray(value)) return value.map((item) => redactForLog(item))
  if (value && typeof value === 'object') {
    const out = {}
    for (const [k, v] of Object.entries(value)) out[k] = redactForLog(v, k)
    return out
  }
  return value
}

function getLogger(pageArtifactDir) {
  // 门禁：结构化日志仅服务于 d2c-eval-loop，默认关闭。未设 D2C_EVAL_LOOP 时退化为
  // no-op（不产生任何 jsonl/summary 文件），普通 d-d2c 运行不会留下日志。
  if (!process.env.D2C_EVAL_LOOP) pageArtifactDir = null
  // 解析不到有效目录就给 no-op，宿主脚本无感。
  const dir = pageArtifactDir ? safe(() => path.resolve(pageArtifactDir)) : undefined
  const jsonlPath = dir ? path.join(dir, JSONL) : null
  const summaryPath = dir ? path.join(dir, SUMMARY) : null

  function append(type, fields) {
    if (!jsonlPath) return
    safe(() => {
      fs.mkdirSync(dir, { recursive: true })
      const rec = redactForLog(Object.assign({ ts: new Date().toISOString(), type }, fields || {}))
      fs.appendFileSync(jsonlPath, JSON.stringify(rec) + '\n')
      rebuildSummary()
    })
  }

  function rebuildSummary() {
    if (!jsonlPath || !summaryPath) return
    safe(() => {
      if (!fs.existsSync(jsonlPath)) return
      const lines = fs.readFileSync(jsonlPath, 'utf8').split('\n').filter(Boolean)
      const events = []
      for (const l of lines) { const e = safe(() => JSON.parse(l)); if (e) events.push(e) }
      fs.writeFileSync(summaryPath, JSON.stringify(aggregate(events, dir), null, 2) + '\n')
    })
  }

  const api = {
    enabled: Boolean(jsonlPath),
    jsonlPath,
    summaryPath,
    event: append,
    phaseEnter: (phase, f) => append('phase.enter', Object.assign({ phase: Number(phase) }, f)),
    phaseComplete: (phase, f) => append('phase.complete', Object.assign({ phase: Number(phase) }, f)),
    scriptStart: (script, f) => append('script.start', Object.assign({ script }, f)),
    scriptComplete: (script, f) => append('script.complete', Object.assign({ script }, f)),
    artifactWrite: (file, f) => append('artifact.write', Object.assign({ file: safe(() => path.resolve(file)) || file }, f)),
    checkWarning: (check, f) => append('check.warning', Object.assign({ check }, f)),
    checkViolation: (check, f) => append('check.violation', Object.assign({ check }, f)),
    evaluationComplete: (f) => append('evaluation.complete', f),
    decisionRecorded: (decision, f) => append('decision.recorded', Object.assign({ decision }, f)),
    error: (message, f) => append('error', Object.assign({ message: String(message) }, f)),
  }
  return api
}

function aggregate(events, dir) {
  const summary = {
    pageDir: dir ? path.basename(dir) : null,
    pageArtifactDir: dir || null,
    round: null,
    phases: {},
    artifacts: [],
    warnings: 0,
    violations: 0,
    report: null,
    score: null,
    decision: null,
    completeRun: false,
    hardViolation: false,
    errors: 0,
    firstEventAt: events.length ? events[0].ts : null,
    lastEventAt: events.length ? events[events.length - 1].ts : null,
  }

  for (const e of events) {
    if (e.round != null) summary.round = e.round
    switch (e.type) {
      case 'phase.enter': {
        const p = (summary.phases[e.phase] = summary.phases[e.phase] || {})
        p.status = 'in_progress'; p.enteredAt = e.ts
        break
      }
      case 'phase.complete': {
        const p = (summary.phases[e.phase] = summary.phases[e.phase] || {})
        p.status = 'completed'; p.completedAt = e.ts
        if (e.score != null) p.score = e.score
        if (e.report) p.report = e.report
        if (e.decision) p.decision = e.decision
        if (Number(e.phase) === 6) summary.completeRun = true
        break
      }
      case 'artifact.write':
        if (e.file && !summary.artifacts.includes(e.file)) summary.artifacts.push(e.file)
        break
      case 'check.warning':
        summary.warnings += Number.isFinite(e.warningCount) ? e.warningCount : 1
        break
      case 'check.violation': {
        const n = Number.isFinite(e.violationCount) ? e.violationCount : 1
        summary.violations += n
        if (e.hard || n > 0) summary.hardViolation = summary.hardViolation || Boolean(e.hard)
        break
      }
      case 'evaluation.complete':
        if (e.score != null) summary.score = e.score
        if (e.report) summary.report = e.report
        break
      case 'decision.recorded':
        summary.decision = e.decision
        break
      case 'error':
        summary.errors += 1
        break
      default:
        break
    }
  }
  return summary
}

// 页目录标记：含其一即认定为 <page-dir>
const PAGE_DIR_MARKERS = ['workflow-state.json', 'dsl_full.json', JSONL]

/** 从产物路径向上回溯，定位 <page-dir>（含标记文件的目录）。找不到返回 null。 */
function resolvePageArtifactDir(startPath) {
  return safe(() => {
    let cur = path.resolve(startPath)
    if (fs.existsSync(cur) && fs.statSync(cur).isFile()) cur = path.dirname(cur)
    for (let i = 0; i < 6 && cur && cur !== path.dirname(cur); i++) {
      for (const marker of PAGE_DIR_MARKERS) {
        if (fs.existsSync(path.join(cur, marker))) return cur
      }
      cur = path.dirname(cur)
    }
    return null
  }) || null
}

/** 便捷：按产物路径回溯页目录后取 logger（适用于写到 r<N>/ 等子目录的脚本）。 */
function getLoggerForArtifact(artifactPath) {
  return getLogger(resolvePageArtifactDir(artifactPath))
}

module.exports = { getLogger, getLoggerForArtifact, resolvePageArtifactDir }
