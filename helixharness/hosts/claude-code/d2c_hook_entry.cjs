#!/usr/bin/env node
'use strict'

// Skill-scoped Claude metadata calls this bound installation, never a user copy.
const path = require('path')
const event = process.argv[2]
const guards = {
  PreToolUse: 'd2c-user-question-guard.cjs',
  Stop: 'd2c-stop-guard.cjs',
}

try {
  if (!Object.hasOwn(guards, event)) throw new Error('unsupported skill hook event')
  require(path.resolve(__dirname, '../../skills/d-d2c/scripts', guards[event])).main()
} catch (error) {
  const reason = `d-d2c bound guard failed: ${error.message}; repair the current plugin installation.`
  const output = event === 'PreToolUse'
    ? { hookSpecificOutput: { hookEventName: event, permissionDecision: 'deny', permissionDecisionReason: reason } }
    : { decision: 'block', reason }
  process.stdout.write(`${JSON.stringify(output)}\n`)
}
