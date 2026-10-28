#!/usr/bin/env node
/* eslint-disable no-console */
'use strict'

/**
 * Manage temporary preview-harness files used only for d-d2c screenshots.
 *
 * The harness is intentionally framework-agnostic: the agent still creates the
 * actual route/wrapper according to the target repo's routing conventions, but
 * every temporary file must be tracked here before it is edited. Cleanup then
 * deletes newly-created files and restores modified files from backups before
 * the workflow may leave the materials phase.
 */

const fs = require('fs')
const path = require('path')
const crypto = require('crypto')
const { requirePreviewWorkflowMode } = require('./lib/d2c-workflow-mode-policy.cjs')

function parseArgs(argv) {
  const args = { created: [], modified: [] }
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i]
    if (!token.startsWith('--')) continue
    const key = token.slice(2)
    const next = argv[i + 1]
    const value = next && !next.startsWith('--') ? next : true
    if (key === 'created' || key === 'track-created') args.created.push(value)
    else if (key === 'modified' || key === 'track-modified') args.modified.push(value)
    else args[key] = value
    if (value !== true) i += 1
  }
  return args
}

function now() {
  return new Date().toISOString()
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true })
}

function pageDirFromArgs(args) {
  const pageDir = args['page-dir'] && args['page-dir'] !== true ? args['page-dir'] : ''
  if (!pageDir) {
    console.error('Usage: d2c-preview-harness.cjs --page-dir d-d2c/temp/<page-dir> [--init --mode <create-page|component>|--cleanup|--status]')
    process.exit(1)
  }
  return path.resolve(pageDir)
}

function statePath(pageDir) {
  return path.join(pageDir, 'preview-harness.json')
}

function backupDir(pageDir) {
  return path.join(pageDir, '.preview-harness-backups')
}

function readState(pageDir) {
  const p = statePath(pageDir)
  if (!fs.existsSync(p)) {
    return {
      version: 1,
      accessMode: 'preview-harness',
      active: false,
      files: [],
    }
  }
  return JSON.parse(fs.readFileSync(p, 'utf8'))
}

function writeState(pageDir, state) {
  ensureDir(pageDir)
  fs.writeFileSync(statePath(pageDir), JSON.stringify(state, null, 2), 'utf8')
}

function hashPath(filePath) {
  return crypto.createHash('sha1').update(path.resolve(filePath)).digest('hex').slice(0, 16)
}

function findEntry(state, filePath) {
  const abs = path.resolve(filePath)
  return (state.files || []).find((f) => path.resolve(f.path) === abs)
}

function addEntry(state, entry) {
  state.files = state.files || []
  const existing = findEntry(state, entry.path)
  if (existing) return existing
  state.files.push(entry)
  return entry
}

function backupFile(pageDir, filePath) {
  const abs = path.resolve(filePath)
  const bdir = backupDir(pageDir)
  ensureDir(bdir)
  const base = `${hashPath(abs)}-${path.basename(abs)}.bak`
  const dest = path.join(bdir, base)
  // 目录也要能备份（独立预览的 runtime 输出目录）：
  // copyFileSync 对目录会抛 ENOTSUP/ENOTDIR，导致跟踪失败、清理链断裂。
  if (fs.statSync(abs).isDirectory()) {
    fs.rmSync(dest, { recursive: true, force: true })
    fs.cpSync(abs, dest, { recursive: true })
  } else {
    fs.copyFileSync(abs, dest)
  }
  return dest
}

function initHarness(args, pageDir, state) {
  let workflowMode
  try {
    workflowMode = requirePreviewWorkflowMode(
      args.mode || args['workflow-mode'],
      'd2c-preview-harness --init'
    )
  } catch (error) {
    console.error(`❌ ${error.message}`)
    process.exit(2)
  }
  const targetUrl = args['target-url'] && args['target-url'] !== true ? String(args['target-url']) : null
  const previewUrl = args['preview-url'] && args['preview-url'] !== true ? String(args['preview-url']) : null
  const reason = args.reason && args.reason !== true ? String(args.reason) : null
  const previewKind = args['preview-kind'] && args['preview-kind'] !== true ? String(args['preview-kind']) : null
  const bypassesBootstrapRaw = args['bypasses-bootstrap'] !== undefined ? String(args['bypasses-bootstrap']) : null
  state.version = 2
  state.accessMode = 'preview-harness'
  state.workflowMode = workflowMode
  state.active = true
  state.createdAt = state.createdAt || now()
  state.updatedAt = now()
  state.cleanedAt = null
  if (targetUrl) state.targetUrl = targetUrl
  if (previewUrl) state.previewUrl = previewUrl
  if (reason) state.reason = reason
  if (previewKind) state.previewKind = previewKind
  if (bypassesBootstrapRaw !== null) state.bypassesBootstrap = /^(1|true|yes)$/i.test(bypassesBootstrapRaw)
  state.files = state.files || []
  writeState(pageDir, state)
  console.log(`preview harness initialized: ${statePath(pageDir)}`)
}

function trackCreated(pageDir, state, filePath) {
  const abs = path.resolve(filePath)
  if (findEntry(state, abs)) return
  if (fs.existsSync(abs)) {
    const backupPath = backupFile(pageDir, abs)
    addEntry(state, {
      kind: 'modified',
      path: abs,
      backupPath,
      trackedAt: now(),
      note: 'tracked as created but file already existed; backed up and will restore',
    })
    console.log(`tracked modified existing file: ${abs}`)
    return
  }
  addEntry(state, { kind: 'created', path: abs, trackedAt: now() })
  console.log(`tracked created file: ${abs}`)
}

function trackModified(pageDir, state, filePath) {
  const abs = path.resolve(filePath)
  if (findEntry(state, abs)) return
  if (!fs.existsSync(abs)) {
    addEntry(state, {
      kind: 'created',
      path: abs,
      trackedAt: now(),
      note: 'tracked as modified but file did not exist; will delete if created',
    })
    console.log(`tracked missing file as created: ${abs}`)
    return
  }
  const backupPath = backupFile(pageDir, abs)
  addEntry(state, { kind: 'modified', path: abs, backupPath, trackedAt: now() })
  console.log(`tracked modified file with backup: ${abs}`)
}

function cleanupHarness(pageDir, state) {
  const files = state.files || []
  const restored = []
  const deleted = []
  const missing = []

  for (const f of files.slice().reverse()) {
    const abs = path.resolve(f.path)
    if (f.kind === 'modified') {
      if (f.backupPath && fs.existsSync(f.backupPath)) {
        ensureDir(path.dirname(abs))
        // 目录备份同样要递归还原：先清掉当前内容再拷回，避免残留新增文件。
        if (fs.statSync(f.backupPath).isDirectory()) {
          fs.rmSync(abs, { recursive: true, force: true })
          fs.cpSync(f.backupPath, abs, { recursive: true })
        } else {
          fs.copyFileSync(f.backupPath, abs)
        }
        restored.push(abs)
      } else {
        missing.push({ path: abs, reason: 'backup missing' })
      }
    } else if (f.kind === 'created') {
      if (fs.existsSync(abs)) {
        // created 也可能是目录（如独立预览的 runtime 输出目录），必须递归删除；
        // 用 unlinkSync 会抛 EISDIR，导致清理静默失败、harness 永远 active。
        const stat = fs.statSync(abs)
        if (stat.isDirectory()) {
          fs.rmSync(abs, { recursive: true, force: true })
          deleted.push(`${abs}/`)
        } else {
          fs.unlinkSync(abs)
          deleted.push(abs)
        }
      }
    }
  }

  state.active = false
  state.cleanedAt = now()
  state.updatedAt = state.cleanedAt
  state.cleanup = { restored, deleted, missing }
  writeState(pageDir, state)

  console.log(`preview harness cleaned: restored=${restored.length} deleted=${deleted.length}`)
  if (missing.length) {
    console.error(`warning: ${missing.length} preview harness entries could not be fully cleaned`)
  }
}

function statusHarness(pageDir, state) {
  console.log(JSON.stringify({
    stateFile: statePath(pageDir),
    active: !!state.active,
    accessMode: state.accessMode || 'preview-harness',
    workflowMode: state.workflowMode || null,
    targetUrl: state.targetUrl || null,
    previewUrl: state.previewUrl || null,
    previewKind: state.previewKind || null,
    bypassesBootstrap: state.bypassesBootstrap === undefined ? null : !!state.bypassesBootstrap,
    fileCount: (state.files || []).length,
    cleanedAt: state.cleanedAt || null,
  }, null, 2))
}

function main() {
  const args = parseArgs(process.argv)
  const pageDir = pageDirFromArgs(args)
  let state = readState(pageDir)

  if (args.setup) {
    console.error(
      '❌ d2c-preview-harness.cjs 不支持 --setup，也不会创建或重建 preview entry。' +
        '请使用 d2c-isolated-vue-preview.cjs --mode <create-page|component> --page-dir <dir> --target <target.vue> 创建 isolated Vue preview；' +
        '若 preview-harness.json 已被 cleanup，必须重新运行 d2c-isolated-vue-preview.cjs 并由 loop 受控重启 dev server。'
    )
    process.exit(1)
  }

  if (args.init) initHarness(args, pageDir, state)
  state = readState(pageDir)

  if ((args.created && args.created.length) || (args.modified && args.modified.length)) {
    try {
      requirePreviewWorkflowMode(state.workflowMode, 'd2c-preview-harness file tracking')
    } catch (error) {
      console.error(`❌ ${error.message}`)
      process.exit(2)
    }
  }

  for (const p of args.created || []) {
    if (p && p !== true) trackCreated(pageDir, state, p)
  }
  for (const p of args.modified || []) {
    if (p && p !== true) trackModified(pageDir, state, p)
  }
  if ((args.created && args.created.length) || (args.modified && args.modified.length)) {
    state.active = true
    state.updatedAt = now()
    writeState(pageDir, state)
  }

  if (args.cleanup) {
    state = readState(pageDir)
    cleanupHarness(pageDir, state)
  } else if (args.status || (!args.init && !(args.created || []).length && !(args.modified || []).length)) {
    state = readState(pageDir)
    statusHarness(pageDir, state)
  }
}

main()
