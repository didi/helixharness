#!/usr/bin/env node
'use strict'

const fs = require('fs')

const {
  claimActiveWorkflow,
  decideStop,
  isHeadlessEnv,
  loadGuardContext,
} = require('./lib/d2c-workflow-guard-core.cjs')
const {
  invocationAfterMarker,
  latestExplicitInvocation,
} = require('./d2c-stop-guard.cjs')

function deny(message) {
  return {
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: `d-d2c workflow guard: ${message}`,
    },
  }
}

function main() {
  const raw = fs.readFileSync(0, 'utf8').trim()
  let input = {}
  try { input = raw ? JSON.parse(raw) : {} } catch {
    process.stdout.write(`${JSON.stringify(deny('cannot parse user-question hook input; do not ask the user yet.'))}\n`)
    return
  }

  const context = loadGuardContext(input.cwd || process.cwd(), {
    stateDir: process.env.D2C_STATE_DIR || 'd-d2c/temp',
  })
  const invocation = latestExplicitInvocation(input.transcript_path)
  if (!context.marker) {
    if (invocation) {
      process.stdout.write(`${JSON.stringify(deny('initialize phase 0 before asking the user anything about an explicitly invoked d-d2c workflow.'))}\n`)
      return
    }
    process.stdout.write('{}\n')
    return
  }
  if (context.marker.status === 'completed' && invocationAfterMarker(invocation, context.marker)) {
    process.stdout.write(`${JSON.stringify(deny('initialize phase 0 for the new d-d2c invocation before asking the user anything.'))}\n`)
    return
  }

  const sessionId = String(input.session_id || '')
  if (!context.marker.ownerSessionId && sessionId && !['completed', 'aborted'].includes(context.marker.status)) {
    context.marker = claimActiveWorkflow(context.markerFile, context.marker, sessionId)
  }
  const decision = decideStop(context, {
    sessionId,
    headless: isHeadlessEnv(process.env),
    scoreThreshold: process.env.D2C_SCORE_THRESHOLD,
  })
  if (decision.allow) {
    process.stdout.write('{}\n')
    return
  }

  process.stdout.write(`${JSON.stringify(deny(`${decision.reason} Continue automatically; user interaction is not legal in this state.`))}\n`)
}

if (require.main === module) {
  try {
    main()
  } catch (error) {
    process.stdout.write(`${JSON.stringify(deny(error && error.message ? error.message : String(error)))}\n`)
  }
}

module.exports = { deny, main }
