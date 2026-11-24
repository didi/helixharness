#!/usr/bin/env node
'use strict'

// Explicit host gate: check this invocation's checkpoint, never read transcripts.
const fs = require('fs')
const path = require('path')
const crypto = require('crypto')
const { claimActiveWorkflow, decideStop, isHeadlessEnv, withWorkflowLock } = require('./d2c-workflow-guard-core.cjs')
const { validateHealthEvidence } = require('./d2c-runtime-health-core.cjs')
const { validatePngFile } = require('./d2c-png-evidence.cjs')
const { collectPhase2GateStatus } = require('../d2c-workflow-checkpoint.cjs')
const { extractJson } = require('../d2c-print-fidelity-report.cjs')

function within(root, candidate) {
  const relative = path.relative(root, candidate)
  return relative !== '' && relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)
}

function readInside(root, candidate, optional = false) {
  if (optional && !fs.existsSync(candidate)) return null
  const real = fs.realpathSync(candidate)
  if (!within(root, real)) throw new Error('checkpoint path escapes its state directory')
  const value = JSON.parse(fs.readFileSync(real, 'utf8'))
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('checkpoint must be an object')
  return value
}

function parseArgs(argv, options = {}) {
  const args = { headless: false }
  const values = new Set(['--repo-root', '--page-dir', '--started-at', '--action', '--session-id'])
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index]
    if (key === '--headless' && !args.headless) {
      args.headless = true
    } else if (values.has(key) && !Object.prototype.hasOwnProperty.call(args, key) && argv[index + 1] && !argv[index + 1].startsWith('--')) {
      args[key] = argv[++index]
    } else {
      throw new Error(`invalid or duplicate argument: ${key}`)
    }
  }
  for (const key of ['--repo-root', '--page-dir', '--started-at', '--action']) {
    if (!args[key]) throw new Error(`required argument: ${key}`)
  }
  const actions = options.allowClaim ? ['stop', 'ask', 'claim'] : ['stop', 'ask']
  if (!actions.includes(args['--action'])) throw new Error(`action must be ${actions.join(' or ')}`)
  if (!Number.isFinite(Date.parse(args['--started-at']))) throw new Error('started-at must be a timestamp')
  if (options.requireOwnership && !String(args['--session-id'] || '').trim()) throw new Error('required argument: --session-id from native Host Context')
  return args
}

function timestamp(value) {
  return typeof value === 'string' ? Date.parse(value) : NaN
}

function currentInvocationState(state, startedAt) {
  const phases = {}
  for (const phase of [0, 1, 2, 3, 4, 6]) {
    const entry = state.phases[phase]
    if (!entry || !Number.isFinite(timestamp(entry.enteredAt)) || timestamp(entry.enteredAt) < startedAt) continue
    if (entry.status === 'completed' && (!Number.isFinite(timestamp(entry.completedAt)) || timestamp(entry.completedAt) < timestamp(entry.enteredAt))) continue
    // The canonical checkpoint CLI retains previous phases on --enter 0.
    // A retained phase 6 must never outrank the new current phase.
    if (phase === 6 && Number(state.currentPhase) !== 6) continue
    phases[phase] = entry
  }
  return { ...state, phases }
}

function currentInvocationHandoff(handoff, startedAt) {
  if (!handoff || !Number.isFinite(timestamp(handoff.updatedAt)) || timestamp(handoff.updatedAt) < startedAt) return null
  if (handoff.status === 'awaiting-user' && (!Number.isFinite(timestamp(handoff.browserReadyAt)) || timestamp(handoff.browserReadyAt) < startedAt)) return null
  // The canonical guard prioritizes handoff over workflowPause. Old handoff
  // state must neither authorize this invocation nor hide its explicit pause.
  return handoff
}

function requireFresh(value, startedAt, label) {
  const time = timestamp(value)
  if (!Number.isFinite(time) || time < startedAt) throw new Error(`${label} is missing or belongs to an earlier invocation`)
  return time
}

function validateCompletedPhases(state, phases, startedAt) {
  let previousEnd = startedAt
  for (const phase of phases) {
    const entry = state.phases[phase]
    if (!entry || entry.status !== 'completed') throw new Error(`phase ${phase} has no completed evidence for this invocation`)
    const entered = requireFresh(entry.enteredAt, previousEnd, `phase ${phase} entry`)
    previousEnd = requireFresh(entry.completedAt, entered, `phase ${phase} completion`)
  }
  return previousEnd
}

function validateAllowedDecision(decision, state, handoff, startedAt) {
  if (!decision.allow) return
  const kind = decision.action && decision.action.kind
  if (state.workflowPause) {
    const pause = state.workflowPause
    requireFresh(pause.createdAt, startedAt, 'workflow pause')
    if (!['external-input', 'user-cancelled'].includes(pause.kind) || !pause.reason) throw new Error('invalid workflow pause')
    if (pause.kind === 'external-input' && !pause.detail && !pause.evidence) throw new Error('external-input pause needs evidence or detail')
    const pauseKind = pause.kind === 'external-input' ? 'wait-external-input' : 'aborted'
    if (kind === pauseKind && decision.reason === pause.reason) return
  }
  if (kind === 'done') {
    if (Number(state.currentPhase) !== 6) throw new Error('only the current phase 6 can complete this invocation')
    validateCompletedPhases(state, [0, 1, 2, 3, 4, 6], startedAt)
    if (state.phases[4].decision !== 'cancel') throw new Error('phase 4 must authorize finalization')
  } else if (kind === 'wait-user-decision') {
    const previousEnd = validateCompletedPhases(state, [0, 1, 2, 3], startedAt)
    const phase = state.phases[4]
    if (Number(state.currentPhase) !== 4 || !phase || phase.status !== 'in_progress') throw new Error('no current phase 4 decision')
    requireFresh(phase.enteredAt, previousEnd, 'phase 4 entry')
  } else if (handoff && ['wait-browser', 'wait-outer-runner', 'wait-external-input', 'aborted'].includes(kind)) {
    requireFresh(handoff.updatedAt, startedAt, 'browser handoff')
    if (kind === 'wait-browser') requireFresh(handoff.browserReadyAt, startedAt, 'browser readiness')
  } else {
    throw new Error('no current invocation evidence authorizes this decision')
  }
}

function freshFile(root, candidate, startedAt) {
  const real = fs.realpathSync(candidate)
  if (!within(root, real)) throw new Error('visual evidence path escapes its evidence directory')
  const stat = fs.statSync(real)
  if (!stat.isFile() || stat.size === 0) throw new Error(`visual evidence is empty or not a file: ${path.basename(candidate)}`)
  if (stat.mtimeMs + 1 < startedAt) throw new Error(`visual evidence belongs to an earlier invocation: ${path.basename(candidate)}`)
  return real
}

function nonemptyObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length > 0
}

function validateMeasurement(name, report, pageDir, meta) {
  const fail = () => { throw new Error(`${name} measurement evidence is incomplete or inconsistent with the current capture`) }
  if (report.version !== '1.0' || typeof report.url !== 'string' || !report.url || typeof report.pageDir !== 'string' || path.resolve(report.pageDir) !== pageDir || report.captureMode !== meta.captureMode) fail()
  if (!Number.isInteger(report.checkedCount) || report.checkedCount < 0 || !Number.isInteger(report.matchedCount) || report.matchedCount < 0 || report.matchedCount > report.checkedCount || !Array.isArray(report.items) || report.items.length !== report.checkedCount || !Array.isArray(report.violations) || !Number.isInteger(report.violationCount) || report.violationCount < 0) fail()
  if (report.items.filter((item) => item && item.matched === true).length !== report.matchedCount) fail()
  if (meta.captureMode === 'region' && (!report.scope || !Array.isArray(report.scope.layoutMarks) || report.scope.layoutMarks.length !== 1 || report.scope.layoutMarks[0] !== meta.designScope)) fail()
  if (name === 'vertical' && (report.tool !== 'd2c-vertical-check' || !['page', 'page-flow', 'component'].includes(report.mode))) fail()
  if (name === 'render' && (report.gate !== 'render-integrity' || report.checkedCount === 0)) fail()
  // Zero vertical anchors can be legitimate. It must still be a canonical
  // measurement report, not the same four empty placeholder fields.
  if (!report.checkedCount && (!Number.isFinite(report.designFrameWidth) || report.designFrameWidth <= 0 || !Number.isFinite(report.effectiveCssWidth) || report.effectiveCssWidth <= 0 || !Number.isFinite(report.scaleCssToDesign) || report.scaleCssToDesign <= 0 || report.violations.length)) fail()
  for (const item of report.items) {
    if (!item || typeof item.layoutMark !== 'string' || !item.layoutMark || typeof item.matched !== 'boolean' || !nonemptyObject(item.expected)) fail()
    if (!item.matched) {
      const canonicalUnresolved = name === 'vertical' && item.measured === null && item.method === null && item.expected.top === null && item.expected.bottom === null && Number.isFinite(item.expected.height) && item.expected.height >= 0 && Array.isArray(report.warnings) && report.warnings.some((warning) => warning && warning.kind === 'mark-unresolved' && warning.layoutMark === item.layoutMark && typeof warning.note === 'string' && warning.note.trim())
      if (!report.violations.some((violation) => violation.layoutMark === item.layoutMark) && !canonicalUnresolved) fail()
      continue
    }
    if (!nonemptyObject(item.measured)) fail()
    if (name === 'geometry' && (!Number.isFinite(item.expected.w) || !Number.isFinite(item.expected.absX) || !(Number.isFinite(item.measured.wDesign) && Number.isFinite(item.measured.leftDesign)) && !Number.isFinite(item.measured.fontSizeCss))) fail()
    if (name === 'vertical' && (!Number.isFinite(item.expected.top) || !Number.isFinite(item.expected.height) || !Number.isFinite(item.measured.top) || !Number.isFinite(item.measured.height))) fail()
    if (name === 'render') {
      const expected = item.expected
      const style = item.measured.style
      const hasContract = expected.text || expected.visualFill || expected.stroke || expected.effect || expected.textSpans && expected.textSpans.length || expected.assets && expected.assets.length || Number.isFinite(expected.borderRadius) || Number.isFinite(expected.opacity)
      if (!Array.isArray(expected.textSpans) || !Array.isArray(expected.assets) || !hasContract || item.measured.layoutMark !== item.layoutMark || item.measured.matched !== true || !style || !Number.isFinite(style.rectWidth) || style.rectWidth < 0 || !Number.isFinite(style.rectHeight) || style.rectHeight < 0) fail()
    }
  }
}

function validateFidelityReport(root, file, evaluation, round) {
  if (fs.statSync(file).size > 8 * 1024 * 1024) throw new Error('fidelity report exceeds the 8 MiB audit limit')
  const report = extractJson(fs.readFileSync(file, 'utf8'))
  if (!report || report.version !== '1.0' || report.round !== round || report.overallScore !== evaluation.score || report.scoreUnit !== 'percent' || typeof report.summary !== 'string' || !report.summary.trim() || typeof report.reportFile !== 'string' || freshFile(root, path.resolve(root, report.reportFile), 0) !== file) {
    throw new Error('fidelity report needs canonical JSON matching this file, round and checkpoint score')
  }
  const weights = { layoutStructure: .4, visualStyle: .25, contentAssets: .15, perceptualSimilarity: .2 }
  let weightedScore = 0
  for (const [name, weight] of Object.entries(weights)) {
    const dimension = report.dimensions && report.dimensions[name]
    if (!dimension || dimension.weight !== weight || !Number.isFinite(dimension.score) || dimension.score < 0 || dimension.score > 100 || !Number.isFinite(dimension.deduction) || Math.abs(dimension.deduction - (100 - dimension.score)) > 1e-6) throw new Error('fidelity report dimension scores are incomplete or inconsistent')
    weightedScore += dimension.score * weight
  }
  if (Math.abs(weightedScore - evaluation.score) > 1 || !Array.isArray(report.findings) || !Array.isArray(report.optimizationPlan) || !Array.isArray(report.unresolvedQuestions)) throw new Error('fidelity report score or structured findings are inconsistent')
}

function validateVisualEvidence(root, pageDir, state, startedAt) {
  const round = Number(state.round)
  if (!Number.isSafeInteger(round) || round < 1) throw new Error('invalid material round')
  const roundDir = fs.realpathSync(path.join(pageDir, `r${round}`))
  if (!within(pageDir, roundDir)) throw new Error('material round escapes page directory')
  const materialStart = Math.max(startedAt, timestamp(state.phases[2].enteredAt))
  const metaPath = freshFile(roundDir, path.join(roundDir, 'capture-meta.json'), materialStart)
  const meta = readInside(roundDir, metaPath)
  if (meta.materialStatus === 'invalid' || meta.invalidReason) throw new Error('capture metadata marks visual evidence invalid')
  if (!meta.runtimeHealth || typeof meta.runtimeHealth !== 'object') throw new Error('capture metadata has no runtime health evidence')
  const fingerprint = meta.sourceFingerprint || {}
  for (const file of [...(fingerprint.files || []), ...(fingerprint.explicitFiles || [])]) {
    if (typeof file !== 'string' || !within(root, path.resolve(root, file))) throw new Error('capture source path escapes repository')
    const candidate = path.resolve(root, file)
    if (fs.existsSync(candidate) && !within(root, fs.realpathSync(candidate))) throw new Error('capture source path escapes repository')
  }
  const healthProblems = validateHealthEvidence({ ...meta.runtimeHealth, sourceFingerprint: meta.sourceFingerprint }, { cwd: root, requireWithinRoot: true })
  if (healthProblems.length) throw new Error(`capture runtime health evidence is missing, failed or stale: ${healthProblems.join('; ')}`)
  if (!['full-page', 'region'].includes(meta.captureMode)) throw new Error('capture metadata has no valid capture mode')
  if (typeof meta.outPath !== 'string' || !meta.outPath) throw new Error('capture metadata has no actual image path')
  const actual = freshFile(roundDir, path.resolve(root, meta.outPath), materialStart)
  const image = validatePngFile(actual)
  const digest = crypto.createHash('sha256').update(image).digest('hex')
  if (meta.actualSha256 !== digest) throw new Error('actual screenshot does not match capture metadata actualSha256')
  const warnings = []
  let unresolvedVerticalAnchors = 0
  for (const name of ['geometry', 'vertical', 'render']) {
    const file = freshFile(roundDir, path.join(roundDir, `${name}-diff.json`), materialStart)
    const report = readInside(roundDir, file)
    validateMeasurement(name, report, pageDir, meta)
    if (name === 'vertical') unresolvedVerticalAnchors = report.items.filter((item) => !item.matched).length
    for (const warning of Array.isArray(report.warnings) ? report.warnings : []) {
      if (warning && typeof warning === 'object' && !Array.isArray(warning)) warnings.push({ ...warning, source: name })
    }
  }
  const gate = collectPhase2GateStatus(path.dirname(pageDir), path.basename(pageDir), round)
  if (gate.sourceMaterialFailing.length || gate.failing.length && round < gate.maxRounds) throw new Error('canonical geometry/vertical/render gate still requires repair before finalization')
  if (meta.captureMode === 'region') {
    if (!meta.designScope || !fs.readdirSync(roundDir).some((name) => {
      if (!name.endsWith('-design-source.png')) return false
      validatePngFile(freshFile(roundDir, path.join(roundDir, name), materialStart))
      return true
    })) throw new Error('region capture needs current scoped design-source evidence')
  }
  const evaluation = state.phases[3]
  if (typeof evaluation.report !== 'string' || !evaluation.report || !Number.isFinite(evaluation.score) || evaluation.score < 0 || evaluation.score > 100) throw new Error('fidelity evaluation needs report and valid score')
  const reportFile = freshFile(root, path.resolve(root, evaluation.report), Math.max(startedAt, timestamp(evaluation.enteredAt)))
  validateFidelityReport(root, reportFile, evaluation, round)
  return { warningCount: warnings.length, unresolvedVerticalAnchors, warnings: warnings.slice(0, 20), warningsTruncated: warnings.length > 20 }
}

function check(args, options = {}) {
  const root = fs.realpathSync(args['--repo-root'])
  const stateDir = fs.realpathSync(path.join(root, 'd-d2c/temp'))
  if (!within(root, stateDir)) throw new Error('state directory escapes repository')
  if (options.requireOwnership && args['--action'] === 'claim' && !options.claimLockHeld) {
    // Re-read marker AND state while holding the same lock as checkpoint writes.
    return withWorkflowLock(stateDir, () => check(args, { ...options, claimLockHeld: true }))
  }
  const marker = readInside(stateDir, path.join(stateDir, '.active-workflow.json'))
  if (marker.pageDir !== args['--page-dir'] || marker.workflowStartedAt !== args['--started-at']) {
    throw new Error('checkpoint does not belong to this invocation')
  }
  if (marker.ownerSessionId && marker.ownerSessionId !== args['--session-id']) {
    throw new Error('checkpoint belongs to another or unidentified session')
  }
  const pageDir = path.resolve(stateDir, marker.pageDir)
  if (!within(stateDir, pageDir)) throw new Error('invalid checkpoint page directory')
  const stateFile = fs.realpathSync(marker.stateFile || path.join(pageDir, 'workflow-state.json'))
  if (stateFile !== fs.realpathSync(path.join(pageDir, 'workflow-state.json'))) {
    throw new Error('checkpoint state does not match page directory')
  }
  const storedState = readInside(stateDir, stateFile)
  if (!storedState.phases || !storedState.phases[0] || storedState.phases[0].enteredAt !== args['--started-at']) {
    throw new Error('phase 0 does not match this invocation')
  }
  const startedAt = timestamp(args['--started-at'])
  const state = currentInvocationState(storedState, startedAt)
  if (options.requireOwnership) {
    const sessionId = String(args['--session-id'] || '').trim()
    if (!sessionId) throw new Error('native session identity is required')
    if (args['--action'] === 'claim') {
      const phase0 = state.phases[0]
      const age = Date.now() - startedAt
      if (marker.ownerSessionId) throw new Error('workflow ownership is already claimed')
      // The canonical CLI retains old phases on --enter 0, so its marker may
      // still say completed. Only this invocation's filtered phases count.
      if (age < 0 || age > 5 * 60 * 1000 || Number(state.currentPhase) !== 0 || !phase0 || phase0.status !== 'in_progress' || Object.keys(state.phases).some((phase) => phase !== '0')) {
        throw new Error('claim requires fresh unowned phase 0 before work begins; re-enter phase 0')
      }
      claimActiveWorkflow(path.join(stateDir, '.active-workflow.json'), marker, sessionId)
      return { allow: true, reason: 'workflow-claimed-for-native-session', action: { kind: 'claimed', askUser: false } }
    }
    if (!marker.ownerSessionId || marker.ownerSessionId !== sessionId) throw new Error('workflow must be claimed by this native session before work begins')
    requireFresh(marker.ownerClaimedAt, startedAt, 'workflow ownership claim')
  }
  const handoff = currentInvocationHandoff(readInside(stateDir, path.join(pageDir, 'browser-handoff.json'), true), startedAt)
  const decision = decideStop({ root, stateDir, marker, state, handoff }, {
    sessionId: args['--session-id'],
    headless: args.headless || isHeadlessEnv(process.env),
    scoreThreshold: process.env.D2C_SCORE_THRESHOLD,
  })
  validateAllowedDecision(decision, state, handoff, startedAt)
  if (options.requireVisualEvidence && decision.allow && decision.action && ['done', 'wait-user-decision'].includes(decision.action.kind)) {
    decision.evidenceAudit = validateVisualEvidence(root, fs.realpathSync(pageDir), state, startedAt)
  }
  if (args['--action'] === 'ask' && !(decision.allow && decision.action && decision.action.askUser)) {
    return { ...decision, allow: false, reason: 'user interaction is not permitted at this checkpoint' }
  }
  return decision
}

function main(argv = process.argv.slice(2), options = {}) {
  let result
  try {
    result = check(parseArgs(argv, options), options)
  } catch (error) {
    result = { allow: false, reason: `${options.host || 'cursor'}_d2c_gate_failed`, detail: error.message }
  }
  process.stdout.write(`${JSON.stringify(result)}\n`)
  return result.allow ? 0 : 2
}

module.exports = { check, main, parseArgs }
