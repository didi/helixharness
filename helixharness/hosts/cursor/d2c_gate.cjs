#!/usr/bin/env node
'use strict'

// Explicit Cursor gate: check this invocation's checkpoint, never read transcripts.
const fs = require('fs')
const path = require('path')
const { decideStop, isHeadlessEnv } = require('../../skills/d-d2c/scripts/lib/d2c-workflow-guard-core.cjs')

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

function parseArgs(argv) {
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
  if (!['stop', 'ask'].includes(args['--action'])) throw new Error('action must be stop or ask')
  if (!Number.isFinite(Date.parse(args['--started-at']))) throw new Error('started-at must be a timestamp')
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

function check(args) {
  const root = fs.realpathSync(args['--repo-root'])
  const stateDir = fs.realpathSync(path.join(root, 'd-d2c/temp'))
  if (!within(root, stateDir)) throw new Error('state directory escapes repository')
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
  const handoff = currentInvocationHandoff(readInside(stateDir, path.join(pageDir, 'browser-handoff.json'), true), startedAt)
  const decision = decideStop({ root, stateDir, marker, state, handoff }, {
    sessionId: args['--session-id'],
    headless: args.headless || isHeadlessEnv(process.env),
    scoreThreshold: process.env.D2C_SCORE_THRESHOLD,
  })
  validateAllowedDecision(decision, state, handoff, startedAt)
  if (args['--action'] === 'ask' && !(decision.allow && decision.action && decision.action.askUser)) {
    return { ...decision, allow: false, reason: 'user interaction is not permitted at this checkpoint' }
  }
  return decision
}

function main(argv = process.argv.slice(2)) {
  let result
  try {
    result = check(parseArgs(argv))
  } catch (error) {
    result = { allow: false, reason: 'cursor_d2c_gate_failed', detail: error.message }
  }
  process.stdout.write(`${JSON.stringify(result)}\n`)
  return result.allow ? 0 : 2
}

if (require.main === module) process.exitCode = main()
module.exports = { check, main, parseArgs }
