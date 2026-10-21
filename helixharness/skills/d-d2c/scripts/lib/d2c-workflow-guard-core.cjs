#!/usr/bin/env node
'use strict'

const fs = require('fs')
const path = require('path')

const GUARD_VERSION = 1
const ACTIVE_WORKFLOW_FILE = '.active-workflow.json'
const ALLOWED_PAUSE_KINDS = new Set(['external-input', 'user-cancelled'])

function nowIso() {
  return new Date().toISOString()
}

function readJsonIfExists(filePath) {
  if (!filePath || !fs.existsSync(filePath)) return null
  return JSON.parse(fs.readFileSync(filePath, 'utf8'))
}

function writeJsonAtomic(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true })
  const temp = `${filePath}.tmp-${process.pid}`
  fs.writeFileSync(temp, `${JSON.stringify(value, null, 2)}\n`, 'utf8')
  fs.renameSync(temp, filePath)
}

function activeWorkflowPath(stateDir = 'd-d2c/temp') {
  return path.resolve(stateDir, ACTIVE_WORKFLOW_FILE)
}

function workflowStatePath(stateDir, pageDir) {
  return path.resolve(stateDir, pageDir, 'workflow-state.json')
}

function canonicalPath(filePath) {
  try {
    return fs.realpathSync.native(filePath)
  } catch {
    return path.resolve(filePath)
  }
}

function phase(state, number) {
  return state && state.phases ? state.phases[number] || state.phases[String(number)] : null
}

function phaseStatus(state, number) {
  const value = phase(state, number)
  return value ? value.status : null
}

function workflowStartedAt(state) {
  return (phase(state, 0) && phase(state, 0).enteredAt) || null
}

function isHeadlessEnv(env = process.env) {
  return String(env.D2C_EVAL_LOOP || '') === '1' || String(env.D2C_HEADLESS || '') === '1'
}

function continueAction(reason, currentPhase, nextPhase, instruction, extra = {}) {
  return {
    kind: 'continue',
    reason,
    currentPhase: currentPhase === undefined ? null : currentPhase,
    nextPhase: nextPhase === undefined ? null : nextPhase,
    askUser: false,
    instruction,
    ...extra,
  }
}

function deriveWorkflowAction(state, options = {}) {
  const headless = Boolean(options.headless)
  const requestedThreshold = Number(options.scoreThreshold)
  const scoreThreshold = Number.isFinite(requestedThreshold) && requestedThreshold > 0
    ? requestedThreshold
    : 90
  if (!state) {
    return continueAction(
      'workflow-state-missing',
      null,
      0,
      'Initialize the d-d2c workflow and enter phase 0 before stopping.'
    )
  }

  if (state.workflowPause) {
    const pause = state.workflowPause
    if (pause.kind === 'user-cancelled') {
      return {
        kind: 'aborted',
        reason: pause.reason || 'user-cancelled',
        askUser: false,
        instruction: pause.detail || 'The user cancelled the workflow.',
      }
    }
    if (pause.kind === 'external-input') {
      return {
        kind: 'wait-external-input',
        reason: pause.reason || 'external-input-required',
        askUser: true,
        instruction: pause.detail || 'Wait for the required external input, then run --resume.',
      }
    }
  }

  if (phaseStatus(state, 6) === 'completed') {
    return {
      kind: 'done',
      reason: 'phase-6-completed',
      currentPhase: 6,
      nextPhase: null,
      askUser: false,
      instruction: 'The d-d2c workflow is complete.',
    }
  }

  if (state.phase2Block && state.phase2Block.allowEvaluator === false) {
    const block = state.phase2Block
    return continueAction(
      'phase-2-auto-fix-required',
      2,
      2,
      `Apply the deterministic phase-2 repair plan and generate ${block.consumeRound === false ? 'the same' : 'the next'} material round. Do not ask the user whether to continue.`,
      {
        round: Number(block.currentRound) || Number(state.round) || 1,
        nextRound: Number(block.nextRound) || Number(state.round) || 1,
      }
    )
  }

  const currentPhase = state.currentPhase !== null && state.currentPhase !== undefined && Number.isFinite(Number(state.currentPhase))
    ? Number(state.currentPhase)
    : null
  const current = currentPhase === null ? null : phase(state, currentPhase)

  if (currentPhase === 4 && current && current.status === 'in_progress') {
    if (headless) {
      const score = Number((phase(state, 3) || {}).score)
      const round = Number(state.round) || 1
      const shouldClose = (Number.isFinite(score) && score >= scoreThreshold) || round >= 8
      return continueAction(
        'headless-phase-4-must-finalize',
        4,
        shouldClose ? 6 : 2,
        shouldClose
          ? `The evaluator reached its stopping condition (score threshold ${scoreThreshold} or round 8). Record decision=cancel, complete phase 4, then enter and complete phase 6.`
          : 'Headless execution cannot wait for an interactive decision. Follow the evaluator evidence to continue repair, or record an evidence-bearing external-input pause.',
        {
          score: Number.isFinite(score) ? score : null,
          scoreThreshold,
          round,
        }
      )
    }
    return {
      kind: 'wait-user-decision',
      reason: 'phase-4-awaiting-user-decision',
      currentPhase: 4,
      nextPhase: null,
      askUser: true,
      instruction: 'Ask the user whether to continue optimization or accept the evaluated result.',
    }
  }

  if (current && current.status === 'in_progress') {
    return continueAction(
      `phase-${currentPhase}-in-progress`,
      currentPhase,
      currentPhase,
      `Complete phase ${currentPhase} and run the corresponding checkpoint before stopping.`
    )
  }

  if (phaseStatus(state, 4) === 'completed') {
    const decision = (phase(state, 4) || {}).decision
    if (decision === 'cancel') {
      return continueAction(
        'phase-4-completed',
        4,
        6,
        'Enter phase 6 and complete the final summary before stopping.'
      )
    }
  }

  if (phaseStatus(state, 3) === 'completed') {
    return continueAction(
      'phase-3-completed',
      3,
      4,
      'Enter phase 4 immediately. Interactive runs may then ask for the fidelity decision; headless runs must follow the automated closeout policy.'
    )
  }

  if (phaseStatus(state, 2) === 'completed') {
    return continueAction(
      'phase-2-completed',
      2,
      3,
      'Enter phase 3 and run the fidelity evaluator before stopping.'
    )
  }

  if (phaseStatus(state, 1) === 'completed') {
    return continueAction(
      'phase-1-completed',
      1,
      2,
      'Enter phase 2, start or restart the project dev server as needed, and generate browser materials. Do not ask the user whether to continue.'
    )
  }

  if (phaseStatus(state, 0) === 'completed') {
    return continueAction(
      'phase-0-completed',
      0,
      1,
      'Enter phase 1 and implement the design before stopping.'
    )
  }

  return continueAction(
    'workflow-not-started',
    currentPhase,
    0,
    'Enter phase 0 and execute the complete d-d2c workflow before stopping.'
  )
}

function markerStatus(action) {
  if (!action) return 'active'
  if (action.kind === 'done') return 'completed'
  if (action.kind === 'aborted') return 'aborted'
  if (action.kind === 'wait-external-input') return 'paused'
  if (action.kind === 'wait-user-decision') return 'waiting-user'
  return 'active'
}

function syncActiveWorkflow(options) {
  const stateDir = options.stateDir || 'd-d2c/temp'
  const pageDir = String(options.pageDir || '').trim()
  const state = options.state
  if (!pageDir || !state) return null

  const file = activeWorkflowPath(stateDir)
  const previous = readJsonIfExists(file)
  const startedAt = workflowStartedAt(state)
  const sameRun = Boolean(
    previous &&
    previous.pageDir === pageDir &&
    (previous.workflowStartedAt || null) === startedAt
  )
  const action = deriveWorkflowAction(state, {
    headless: Boolean(options.headless),
    scoreThreshold: options.scoreThreshold,
  })
  const marker = {
    version: GUARD_VERSION,
    pageDir,
    stateFile: path.resolve(options.stateFile || workflowStatePath(stateDir, pageDir)),
    mode: state.mode || null,
    workflowStartedAt: startedAt,
    ownerSessionId: sameRun ? previous.ownerSessionId || null : null,
    ownerClaimedAt: sameRun ? previous.ownerClaimedAt || null : null,
    status: markerStatus(action),
    currentPhase: state.currentPhase === undefined ? null : state.currentPhase,
    round: Number(state.round) || 1,
    nextAction: action,
    updatedAt: nowIso(),
  }
  writeJsonAtomic(file, marker)
  return { file, marker }
}

function claimActiveWorkflow(filePath, marker, sessionId) {
  if (!marker || !sessionId || marker.ownerSessionId) return marker
  const claimed = {
    ...marker,
    ownerSessionId: String(sessionId),
    ownerClaimedAt: nowIso(),
  }
  writeJsonAtomic(filePath, claimed)
  return claimed
}

function loadGuardContext(cwd, options = {}) {
  const root = canonicalPath(cwd || process.cwd())
  const stateDir = canonicalPath(path.resolve(root, options.stateDir || 'd-d2c/temp'))
  const markerFile = activeWorkflowPath(stateDir)
  const marker = readJsonIfExists(markerFile)
  if (!marker) return { root, stateDir, markerFile, marker: null, state: null, handoff: null }

  const stateFile = canonicalPath(marker.stateFile || workflowStatePath(stateDir, marker.pageDir || ''))
  const stateRootPrefix = `${stateDir}${path.sep}`
  if (!stateFile.startsWith(stateRootPrefix) || path.basename(stateFile) !== 'workflow-state.json') {
    return {
      root,
      stateDir,
      markerFile,
      marker,
      state: null,
      handoff: null,
      error: 'active workflow points outside d-d2c/temp',
    }
  }

  const state = readJsonIfExists(stateFile)
  const handoff = readJsonIfExists(path.join(path.dirname(stateFile), 'browser-handoff.json'))
  return { root, stateDir, markerFile, marker, stateFile, state, handoff }
}

function allow(reason, action = null) {
  return { allow: true, reason, action }
}

function block(reason, action) {
  return { allow: false, reason, action }
}

function decideStop(context, options = {}) {
  const marker = context && context.marker
  if (!marker) return allow('no-active-d2c-workflow')
  if (context.error) {
    return block(
      `d-d2c workflow guard state is invalid: ${context.error}. Repair or explicitly pause the workflow before stopping.`,
      { kind: 'continue', reason: 'guard-state-invalid', askUser: false }
    )
  }

  const sessionId = String(options.sessionId || '')
  if (marker.ownerSessionId && sessionId && marker.ownerSessionId !== sessionId) {
    return allow('workflow-owned-by-another-claude-session')
  }

  const state = context.state
  const action = deriveWorkflowAction(state, {
    headless: Boolean(options.headless),
    scoreThreshold: options.scoreThreshold,
  })
  const handoff = context.handoff

  if (handoff && handoff.active) {
    if (handoff.status === 'awaiting-user') {
      return allow('browser-handoff-awaiting-user', {
        kind: 'wait-browser',
        askUser: true,
        instruction: 'Wait for the user to navigate the managed Chrome tab, then verify and continue.',
      })
    }
    if (handoff.status === 'requested') {
      if (options.headless) {
        return allow('headless-browser-handoff-requested', {
          kind: 'wait-outer-runner',
          askUser: false,
          instruction: 'The outer runner must orchestrate browser handoff and resume this workflow.',
        })
      }
      return block(
        'd-d2c browser handoff was requested but the managed Chrome window has not been started. Start it before asking the user to act.',
        { kind: 'continue', reason: 'start-browser-handoff', askUser: false }
      )
    }
    if (handoff.status === 'browser-ready') {
      return block(
        'd-d2c managed Chrome is ready. Transition the handoff to awaiting-user and give the user the target-route instructions before stopping.',
        { kind: 'continue', reason: 'prepare-browser-handoff', askUser: false }
      )
    }
  }

  if (handoff && ['aborted', 'expired'].includes(handoff.status)) {
    return allow(`browser-handoff-${handoff.status}`, {
      kind: handoff.status === 'aborted' ? 'aborted' : 'wait-external-input',
      askUser: handoff.status === 'expired',
    })
  }

  if (['done', 'aborted', 'wait-external-input'].includes(action.kind)) {
    return allow(action.reason, action)
  }
  if (action.kind === 'wait-user-decision' && !options.headless) {
    return allow(action.reason, action)
  }

  return block(
    `d-d2c workflow is incomplete (${action.reason}). ${action.instruction} User confirmation is not permitted for this transition.`,
    action
  )
}

function signalForAction(action) {
  if (!action) return null
  if (action.kind === 'continue') {
    return `D2C_CONTINUE_REQUIRED currentPhase=${action.currentPhase ?? '?'} nextPhase=${action.nextPhase ?? '?'} reason=${action.reason || 'workflow-incomplete'} askUser=false`
  }
  if (action.kind === 'wait-user-decision') {
    return 'D2C_USER_INPUT_REQUIRED kind=fidelity-decision phase=4 askUser=true'
  }
  if (action.kind === 'wait-external-input') {
    return `D2C_USER_INPUT_REQUIRED kind=external-input reason=${action.reason || 'external-input-required'} askUser=true`
  }
  if (action.kind === 'aborted') return `D2C_WORKFLOW_ABORTED reason=${action.reason || 'user-cancelled'}`
  if (action.kind === 'done') return 'D2C_WORKFLOW_DONE phase=6'
  return null
}

module.exports = {
  ACTIVE_WORKFLOW_FILE,
  ALLOWED_PAUSE_KINDS,
  activeWorkflowPath,
  canonicalPath,
  claimActiveWorkflow,
  decideStop,
  deriveWorkflowAction,
  isHeadlessEnv,
  loadGuardContext,
  markerStatus,
  phaseStatus,
  readJsonIfExists,
  signalForAction,
  syncActiveWorkflow,
  workflowStatePath,
  writeJsonAtomic,
}
