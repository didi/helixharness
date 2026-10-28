#!/usr/bin/env node
'use strict'

const assert = require('assert')
const fs = require('fs')
const os = require('os')
const path = require('path')
const { spawnSync } = require('child_process')

const {
  decideStop,
  deriveWorkflowAction,
  loadGuardContext,
  syncActiveWorkflow,
  writeJsonAtomic,
} = require('../lib/d2c-workflow-guard-core.cjs')

const hookScript = path.resolve(__dirname, '../d2c-stop-guard.cjs')
const userQuestionHookScript = path.resolve(__dirname, '../d2c-user-question-guard.cjs')
const checkpointScript = path.resolve(__dirname, '../d2c-workflow-checkpoint.cjs')
const skillRoot = path.resolve(__dirname, '../..')
const pluginRoot = path.resolve(skillRoot, '../..')
const skillFile = path.join(skillRoot, 'SKILL.md')

// 本发行版把两个 guard 统一路由到 `${CLAUDE_PLUGIN_ROOT}/hosts/claude-code/d2c_hook_entry.cjs`，
// 由它按 hook 事件名分派；不再使用 `hook="<script>"` 的多根查找写法。
const HOOK_ENTRY_EVENTS = {
  'd2c-stop-guard.cjs': 'Stop',
  'd2c-user-question-guard.cjs': 'PreToolUse',
}

function configuredHookCommands() {
  const source = fs.readFileSync(skillFile, 'utf8')
  const sections = source.split(/^---\s*$/m)
  assert.ok(sections.length >= 3, 'SKILL.md frontmatter is missing')
  const lines = sections[1].split(/\r?\n/)
  const commands = []

  for (let index = 0; index < lines.length; index += 1) {
    const match = lines[index].match(/^(\s*)command:\s*>-\s*$/)
    if (!match) continue
    const commandIndent = match[1].length
    const block = []

    for (index += 1; index < lines.length; index += 1) {
      const line = lines[index]
      if (!line.trim()) {
        block.push(line)
        continue
      }
      const lineIndent = line.match(/^\s*/)[0].length
      if (lineIndent <= commandIndent) {
        index -= 1
        break
      }
      block.push(line)
    }

    const contentIndent = Math.min(...block
      .filter((line) => line.trim())
      .map((line) => line.match(/^\s*/)[0].length))
    commands.push(block.map((line) => line.slice(contentIndent)).join('\n').trim())
  }

  return { commands, source }
}

function configuredHookCommand(scriptName) {
  const { commands } = configuredHookCommands()
  const event = HOOK_ENTRY_EVENTS[scriptName]
  const command = commands.find((candidate) => (
    event
      ? candidate.includes('d2c_hook_entry.cjs') && candidate.includes(event)
      : candidate.includes(`hook="${scriptName}"`)
  ))
  assert.ok(command, `SKILL.md hook command for ${scriptName} is missing`)
  return command
}

function isolatedHookEnv(root, overrides = {}) {
  const env = {
    ...process.env,
    HOME: path.join(root, '.home'),
    CLAUDE_PROJECT_DIR: root,
    ...overrides,
  }
  delete env.CLAUDE_SKILL_DIR
  if (!Object.prototype.hasOwnProperty.call(overrides, 'CLAUDE_CONFIG_DIR')) {
    delete env.CLAUDE_CONFIG_DIR
  }
  return env
}

function runConfiguredHook(command, root, eventName, env = {}) {
  const input = eventName === 'Stop'
    ? {
        session_id: 'configured-session',
        cwd: root,
        hook_event_name: 'Stop',
        stop_hook_active: false,
      }
    : {
        session_id: 'configured-session',
        cwd: root,
        hook_event_name: 'PreToolUse',
        tool_name: 'AskUserQuestion',
        tool_input: { questions: [] },
      }
  return spawnSync('/bin/sh', ['-c', command], {
    cwd: root,
    input: JSON.stringify(input),
    encoding: 'utf8',
    env: isolatedHookEnv(root, env),
  })
}

function completed(at = '2026-07-13T00:00:00.000Z') {
  return { status: 'completed', completedAt: at }
}

function inProgress(at = '2026-07-13T00:00:00.000Z') {
  return { status: 'in_progress', enteredAt: at }
}

function phase1State() {
  return {
    currentPhase: 1,
    round: 1,
    phases: {
      0: { ...completed(), enteredAt: '2026-07-13T00:00:00.000Z' },
      1: completed(),
    },
  }
}

function fixture(state, handoff = null) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-stop-guard-'))
  const stateDir = path.join(root, 'd-d2c/temp')
  const pageDir = 'fixture-page'
  const stateFile = path.join(stateDir, pageDir, 'workflow-state.json')
  writeJsonAtomic(stateFile, state)
  syncActiveWorkflow({ stateDir, pageDir, state, stateFile })
  if (handoff) writeJsonAtomic(path.join(stateDir, pageDir, 'browser-handoff.json'), handoff)
  return {
    root,
    context: () => loadGuardContext(root),
    cleanup: () => fs.rmSync(root, { recursive: true, force: true }),
  }
}

function runHook(root, sessionId = 'session-a', env = {}, transcriptPath = null) {
  return spawnSync(process.execPath, [hookScript], {
    cwd: root,
    input: JSON.stringify({
      session_id: sessionId,
      cwd: root,
      hook_event_name: 'Stop',
      stop_hook_active: false,
      transcript_path: transcriptPath,
    }),
    encoding: 'utf8',
    env: { ...process.env, ...env },
  })
}

function runUserQuestionHook(root, sessionId = 'session-a', env = {}, transcriptPath = null) {
  return spawnSync(process.execPath, [userQuestionHookScript], {
    cwd: root,
    input: JSON.stringify({
      session_id: sessionId,
      cwd: root,
      hook_event_name: 'PreToolUse',
      tool_name: 'AskUserQuestion',
      tool_input: { questions: [] },
      transcript_path: transcriptPath,
    }),
    encoding: 'utf8',
    env: { ...process.env, ...env },
  })
}

function writeTranscript(root, text, timestamp = '2026-07-13T00:00:00.000Z') {
  const file = path.join(root, 'transcript.jsonl')
  fs.writeFileSync(file, `${JSON.stringify({
    timestamp,
    message: { role: 'user', content: [{ type: 'text', text }] },
  })}\n`, 'utf8')
  return file
}

function testPhase1BlocksStop() {
  const f = fixture(phase1State())
  try {
    const action = deriveWorkflowAction(phase1State())
    assert.strictEqual(action.kind, 'continue')
    assert.strictEqual(action.reason, 'phase-1-completed')
    assert.strictEqual(action.nextPhase, 2)
    assert.strictEqual(action.askUser, false)

    const result = runHook(f.root)
    assert.strictEqual(result.status, 0, result.stderr)
    const output = JSON.parse(result.stdout)
    assert.strictEqual(output.decision, 'block')
    assert.match(output.reason, /phase-1-completed/)
    assert.match(output.reason, /Do not ask the user/)
  } finally {
    f.cleanup()
  }
}

function testPhase2AutoFixBlocksStop() {
  const state = {
    ...phase1State(),
    currentPhase: 2,
    round: 3,
    phases: { ...phase1State().phases, 2: inProgress() },
    phase2Block: {
      reason: 'combined-gate-failed',
      currentRound: 3,
      nextRound: 4,
      consumeRound: true,
      allowEvaluator: false,
    },
  }
  const action = deriveWorkflowAction(state)
  assert.strictEqual(action.reason, 'phase-2-auto-fix-required')
  assert.strictEqual(action.nextRound, 4)
  assert.strictEqual(action.askUser, false)
}

function testBrowserHandoffStates() {
  const awaiting = fixture(phase1State(), { status: 'awaiting-user', active: true })
  try {
    assert.strictEqual(decideStop(awaiting.context(), { sessionId: 'session-a' }).allow, true)
  } finally {
    awaiting.cleanup()
  }

  const requested = fixture(phase1State(), { status: 'requested', active: true })
  try {
    const interactive = decideStop(requested.context(), { sessionId: 'session-a' })
    assert.strictEqual(interactive.allow, false)
    assert.match(interactive.reason, /has not been started/)
    assert.strictEqual(decideStop(requested.context(), { sessionId: 'session-a', headless: true }).allow, true)
  } finally {
    requested.cleanup()
  }
}

function phase4State(score = 95, round = 2) {
  return {
    currentPhase: 4,
    round,
    phases: {
      0: { ...completed(), enteredAt: '2026-07-13T00:00:00.000Z' },
      1: completed(),
      2: completed(),
      3: { ...completed(), score },
      4: inProgress(),
    },
  }
}

function testInteractiveAndHeadlessDecision() {
  const interactive = deriveWorkflowAction(phase4State(), { headless: false })
  assert.strictEqual(interactive.kind, 'wait-user-decision')

  const passing = deriveWorkflowAction(phase4State(95, 2), { headless: true, scoreThreshold: 90 })
  assert.strictEqual(passing.kind, 'continue')
  assert.strictEqual(passing.nextPhase, 6)

  const repairing = deriveWorkflowAction(phase4State(82, 2), { headless: true, scoreThreshold: 90 })
  assert.strictEqual(repairing.nextPhase, 2)

  const maxed = deriveWorkflowAction(phase4State(82, 8), { headless: true, scoreThreshold: 90 })
  assert.strictEqual(maxed.nextPhase, 6)
}

function testTerminalAndExplicitPauseAllowStop() {
  const done = phase4State()
  done.currentPhase = 6
  done.phases[4] = { ...completed(), decision: 'cancel' }
  done.phases[6] = completed()
  assert.strictEqual(deriveWorkflowAction(done).kind, 'done')

  const paused = phase1State()
  paused.workflowPause = {
    kind: 'external-input',
    reason: 'missing-design-image',
    detail: 'The user must provide the exported design image.',
  }
  assert.strictEqual(deriveWorkflowAction(paused).kind, 'wait-external-input')

  const f = fixture(done)
  try {
    const result = runHook(f.root)
    assert.strictEqual(result.status, 0)
    assert.strictEqual(result.stdout, '')
  } finally {
    f.cleanup()
  }
}

function testSessionIsolation() {
  const f = fixture(phase1State())
  try {
    const first = runHook(f.root, 'session-owner')
    assert.ok(first.stdout)
    const context = f.context()
    assert.strictEqual(context.marker.ownerSessionId, 'session-owner')
    assert.strictEqual(decideStop(context, { sessionId: 'another-session' }).allow, true)
  } finally {
    f.cleanup()
  }
}

function testCheckpointCreatesMarkerAndSignal() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-checkpoint-guard-'))
  try {
    const result = spawnSync(process.execPath, [
      checkpointScript,
      '--enter', '0',
      '--page-dir', 'fixture-page',
    ], {
      cwd: root,
      encoding: 'utf8',
    })
    assert.strictEqual(result.status, 0, result.stderr)
    assert.match(result.stdout, /D2C_CONTINUE_REQUIRED/)
    assert.match(result.stdout, /currentPhase=0/)
    assert.ok(fs.existsSync(path.join(root, 'd-d2c/temp/.active-workflow.json')))

    const pause = spawnSync(process.execPath, [
      checkpointScript,
      '--pause', 'external-input',
      '--reason', 'missing-required-input',
      '--detail', 'A required external fixture is unavailable.',
      '--page-dir', 'fixture-page',
    ], {
      cwd: root,
      encoding: 'utf8',
    })
    assert.strictEqual(pause.status, 0, pause.stderr)
    assert.match(pause.stdout, /D2C_WORKFLOW_PAUSED/)

    const stop = runHook(root)
    assert.strictEqual(stop.status, 0)
    assert.strictEqual(stop.stdout, '')

    const resume = spawnSync(process.execPath, [
      checkpointScript,
      '--resume',
      '--page-dir', 'fixture-page',
    ], {
      cwd: root,
      encoding: 'utf8',
    })
    assert.strictEqual(resume.status, 0, resume.stderr)
    assert.match(resume.stdout, /D2C_CONTINUE_REQUIRED/)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
}

function testExplicitInvocationCannotStopBeforeCheckpoint() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-no-checkpoint-'))
  try {
    const invocation = writeTranscript(root, '请执行 d-d2c --update-page client/views/page.vue https://mastergo.example/design')
    const blocked = runHook(root, 'session-a', {}, invocation)
    assert.strictEqual(blocked.status, 0)
    assert.match(JSON.parse(blocked.stdout).reason, /never initialized/)

    const question = writeTranscript(root, 'd-d2c 的 checkpoint 是什么？')
    const allowed = runHook(root, 'session-a', {}, question)
    assert.strictEqual(allowed.stdout, '')
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
}

function questionDecision(result) {
  assert.strictEqual(result.status, 0, result.stderr)
  return JSON.parse(result.stdout || '{}').hookSpecificOutput || null
}

function testUserQuestionGuard() {
  const active = fixture(phase1State())
  try {
    const output = questionDecision(runUserQuestionHook(active.root))
    assert.strictEqual(output.hookEventName, 'PreToolUse')
    assert.strictEqual(output.permissionDecision, 'deny')
    assert.match(output.permissionDecisionReason, /phase-1-completed/)
  } finally {
    active.cleanup()
  }

  const browser = fixture(phase1State(), { status: 'awaiting-user', active: true })
  try {
    assert.strictEqual(questionDecision(runUserQuestionHook(browser.root)), null)
  } finally {
    browser.cleanup()
  }

  const interactive = fixture(phase4State())
  try {
    assert.strictEqual(questionDecision(runUserQuestionHook(interactive.root)), null)
    const headless = questionDecision(runUserQuestionHook(interactive.root, 'session-a', { D2C_HEADLESS: '1' }))
    assert.strictEqual(headless.permissionDecision, 'deny')
    assert.match(headless.permissionDecisionReason, /headless-phase-4-must-finalize/)
  } finally {
    interactive.cleanup()
  }

  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'd2c-question-no-workflow-'))
  try {
    assert.strictEqual(questionDecision(runUserQuestionHook(root)), null)
    const invocation = writeTranscript(root, '请执行 d-d2c --create-page https://mastergo.example/design')
    const denied = questionDecision(runUserQuestionHook(root, 'session-a', {}, invocation))
    assert.strictEqual(denied.permissionDecision, 'deny')
    assert.match(denied.permissionDecisionReason, /initialize phase 0/)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
}

function testConfiguredHookResolution() {
  const { source } = configuredHookCommands()
  assert.ok(!source.includes('${CLAUDE_SKILL_DIR}'))
  const stopCommand = configuredHookCommand('d2c-stop-guard.cjs')
  const questionCommand = configuredHookCommand('d2c-user-question-guard.cjs')

  // 本发行版只通过 ${CLAUDE_PLUGIN_ROOT} 定位 hook 入口，不依赖安装目录探测。
  const f = fixture(phase1State())
  try {
    const stop = runConfiguredHook(stopCommand, f.root, 'Stop', { CLAUDE_PLUGIN_ROOT: pluginRoot })
    assert.strictEqual(stop.status, 0, stop.stderr)
    assert.strictEqual(JSON.parse(stop.stdout).decision, 'block')

    const question = runConfiguredHook(questionCommand, f.root, 'PreToolUse', { CLAUDE_PLUGIN_ROOT: pluginRoot })
    assert.strictEqual(question.status, 0, question.stderr)
    const output = JSON.parse(question.stdout).hookSpecificOutput
    assert.strictEqual(output.permissionDecision, 'deny')
  } finally {
    f.cleanup()
  }
}

function testConfiguredHooksFailClosed() {
  const stopCommand = configuredHookCommand('d2c-stop-guard.cjs')
  const questionCommand = configuredHookCommand('d2c-user-question-guard.cjs')

  // 1) ${CLAUDE_PLUGIN_ROOT} 缺失：命令无法定位 hook 入口，必须走 SKILL.md 内的
  //    printf 兜底，两个事件都要 fail-closed 且给出可操作的修复提示。
  const missing = fixture(phase1State())
  try {
    const stop = runConfiguredHook(stopCommand, missing.root, 'Stop')
    assert.strictEqual(stop.status, 0, stop.stderr)
    assert.match(JSON.parse(stop.stdout).reason, /could not start/)

    const question = runConfiguredHook(questionCommand, missing.root, 'PreToolUse')
    assert.strictEqual(question.status, 0, question.stderr)
    const output = JSON.parse(question.stdout).hookSpecificOutput
    assert.strictEqual(output.permissionDecision, 'deny')
    assert.match(output.permissionDecisionReason, /could not start/)
  } finally {
    missing.cleanup()
  }

  // 2) 入口存在但自身异常退出：同样必须 fail-closed，而不是放行。
  const failing = fixture(phase1State())
  try {
    const fakePluginRoot = path.join(failing.root, 'broken-plugin')
    const entryDir = path.join(fakePluginRoot, 'hosts/claude-code')
    fs.mkdirSync(entryDir, { recursive: true })
    fs.writeFileSync(path.join(entryDir, 'd2c_hook_entry.cjs'), 'process.exit(9)\n', 'utf8')

    const stop = runConfiguredHook(stopCommand, failing.root, 'Stop', {
      CLAUDE_PLUGIN_ROOT: fakePluginRoot,
    })
    assert.strictEqual(stop.status, 0, stop.stderr)
    assert.match(JSON.parse(stop.stdout).reason, /could not start/)

    const question = runConfiguredHook(questionCommand, failing.root, 'PreToolUse', {
      CLAUDE_PLUGIN_ROOT: fakePluginRoot,
    })
    assert.strictEqual(question.status, 0, question.stderr)
    const output = JSON.parse(question.stdout).hookSpecificOutput
    assert.match(output.permissionDecisionReason, /could not start/)
  } finally {
    failing.cleanup()
  }
}

testPhase1BlocksStop()
testPhase2AutoFixBlocksStop()
testBrowserHandoffStates()
testInteractiveAndHeadlessDecision()
testTerminalAndExplicitPauseAllowStop()
testSessionIsolation()
testCheckpointCreatesMarkerAndSignal()
testExplicitInvocationCannotStopBeforeCheckpoint()
testUserQuestionGuard()
testConfiguredHookResolution()
testConfiguredHooksFailClosed()

console.log('d2c workflow stop guard tests: passed')
