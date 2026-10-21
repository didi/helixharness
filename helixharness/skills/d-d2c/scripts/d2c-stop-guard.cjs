#!/usr/bin/env node
/* eslint-disable no-console */
'use strict'

const fs = require('fs')

const {
  claimActiveWorkflow,
  decideStop,
  isHeadlessEnv,
  loadGuardContext,
} = require('./lib/d2c-workflow-guard-core.cjs')

function readHookInput() {
  const raw = fs.readFileSync(0, 'utf8').trim()
  if (!raw) return {}
  try {
    return JSON.parse(raw)
  } catch (error) {
    return { parseError: error.message }
  }
}

function hookBlock(reason) {
  return {
    decision: 'block',
    reason,
    systemMessage: 'd-d2c workflow guard: the active workflow must reach a legal pause or terminal state before this turn can stop.',
  }
}

function textContent(content) {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content
    .filter((item) => item && item.type === 'text' && typeof item.text === 'string')
    .map((item) => item.text)
    .join('\n')
}

function readTranscriptTail(filePath, maxBytes = 1024 * 1024) {
  if (!filePath || !fs.existsSync(filePath)) return ''
  const size = fs.statSync(filePath).size
  const length = Math.min(size, maxBytes)
  const buffer = Buffer.alloc(length)
  const fd = fs.openSync(filePath, 'r')
  try {
    fs.readSync(fd, buffer, 0, length, size - length)
  } finally {
    fs.closeSync(fd)
  }
  const text = buffer.toString('utf8')
  return size > length ? text.slice(text.indexOf('\n') + 1) : text
}

function latestExplicitInvocation(transcriptPath) {
  const lines = readTranscriptTail(transcriptPath).split(/\r?\n/)
  let latest = null
  for (const line of lines) {
    if (!line.trim()) continue
    let entry
    try { entry = JSON.parse(line) } catch { continue }
    const message = entry.message || entry
    if (!message || message.role !== 'user') continue
    const text = textContent(message.content)
    if (!/\bd-d2c\s+--(?:create-page|update-page|component)\b/i.test(text)) continue
    latest = {
      text,
      timestamp: entry.timestamp || message.timestamp || null,
    }
  }
  return latest
}

function invocationAfterMarker(invocation, marker) {
  if (!invocation || !marker || !invocation.timestamp || !marker.updatedAt) return false
  const invokedAt = Date.parse(invocation.timestamp)
  const markerAt = Date.parse(marker.updatedAt)
  return Number.isFinite(invokedAt) && Number.isFinite(markerAt) && invokedAt > markerAt
}

function main() {
  const input = readHookInput()
  if (input.parseError) {
    process.stdout.write(`${JSON.stringify(hookBlock(`Unable to parse Claude Stop hook input: ${input.parseError}`))}\n`)
    return
  }

  const cwd = input.cwd || process.cwd()
  const context = loadGuardContext(cwd, {
    stateDir: process.env.D2C_STATE_DIR || 'd-d2c/temp',
  })
  const invocation = latestExplicitInvocation(input.transcript_path)
  if (!context.marker) {
    if (invocation) {
      process.stdout.write(`${JSON.stringify(hookBlock('A d-d2c command is active, but workflow checkpoint state was never initialized. Enter phase 0 and continue the complete workflow before stopping.'))}\n`)
    }
    return
  }
  if (context.marker.status === 'completed' && invocationAfterMarker(invocation, context.marker)) {
    process.stdout.write(`${JSON.stringify(hookBlock('A new d-d2c command was submitted after the previous workflow completed, but no new checkpoint state was initialized. Enter phase 0 for the new workflow before stopping.'))}\n`)
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
  if (decision.allow) return

  process.stdout.write(`${JSON.stringify(hookBlock(decision.reason))}\n`)
}

if (require.main === module) {
  try {
    main()
  } catch (error) {
    const message = error && error.message ? error.message : String(error)
    process.stdout.write(`${JSON.stringify(hookBlock(`d-d2c workflow guard failed: ${message}`))}\n`)
  }
}

module.exports = {
  hookBlock,
  invocationAfterMarker,
  latestExplicitInvocation,
  main,
  readHookInput,
  readTranscriptTail,
  textContent,
}
