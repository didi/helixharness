/**
 * 从本机各编辑器 MCP 配置解析 MasterGo 令牌与 API 地址。
 * 仅供 Node 子进程（fetch-mastergo-dsl.cjs）使用；Agent 勿 Read 这些配置文件进对话。
 */
const fs = require('fs')
const os = require('os')
const path = require('path')

const MASTERGO_SERVER_HINTS = ['mastergo', 'magic-mcp']

function tokenFromArgString(arg) {
  if (typeof arg !== 'string') return ''
  const eq = arg.match(/^--token=(.+)$/i)
  if (eq) return eq[1].trim()
  return ''
}

function urlFromArgString(arg) {
  if (typeof arg !== 'string') return ''
  const eq = arg.match(/^--url=(.+)$/i)
  if (eq) return eq[1].trim().replace(/\/$/, '')
  return ''
}

function pickFromArgvStyleArgs(args) {
  if (!Array.isArray(args)) return { token: '', baseUrl: '' }
  let token = ''
  let baseUrl = ''
  for (let i = 0; i < args.length; i += 1) {
    const a = args[i]
    if (typeof a !== 'string') continue
    const t = tokenFromArgString(a)
    if (t) token = t
    const u = urlFromArgString(a)
    if (u) baseUrl = u
    if (/^--token$/i.test(a) && args[i + 1]) {
      token = String(args[i + 1]).trim()
      i += 1
    }
    if (/^--url$/i.test(a) && args[i + 1]) {
      baseUrl = String(args[i + 1]).trim().replace(/\/$/, '')
      i += 1
    }
  }
  return { token, baseUrl }
}

function isMasterGoMcpEntry(name, entry) {
  const n = String(name || '').toLowerCase()
  if (MASTERGO_SERVER_HINTS.some((h) => n.includes(h))) return true
  const cmd = String(entry?.command || '').toLowerCase()
  const joined = Array.isArray(entry?.args) ? entry.args.join(' ').toLowerCase() : ''
  return (
    cmd.includes('mastergo') ||
    joined.includes('@mastergo/magic-mcp') ||
    joined.includes('magic-mcp')
  )
}

function extractFromServerEntry(entry) {
  const env = entry?.env && typeof entry.env === 'object' ? entry.env : {}
  const fromArgs = pickFromArgvStyleArgs(entry?.args)
  const token =
    fromArgs.token ||
    String(env.MG_MCP_TOKEN || env.MASTERGO_API_TOKEN || '').trim()
  const baseUrl = (
    fromArgs.baseUrl ||
    String(env.API_BASE_URL || '').trim() ||
    'https://mastergo.com'
  ).replace(/\/$/, '')
  return { token, baseUrl }
}

function extractFromServersMap(servers, configPath) {
  if (!servers || typeof servers !== 'object') return null
  for (const [name, entry] of Object.entries(servers)) {
    if (!entry || typeof entry !== 'object') continue
    if (!isMasterGoMcpEntry(name, entry)) continue
    const { token, baseUrl } = extractFromServerEntry(entry)
    if (token) return { token, baseUrl, source: configPath, server: name }
  }
  return null
}

function extractFromConfigObject(json, configPath) {
  const hit = extractFromServersMap(json?.mcpServers || json?.mcp_servers, configPath)
  if (hit) return hit
  // Claude Code（~/.claude.json）的项目级配置：projects.<path>.mcpServers
  const projects = json?.projects
  if (projects && typeof projects === 'object') {
    for (const projectConfig of Object.values(projects)) {
      const projectHit = extractFromServersMap(
        projectConfig?.mcpServers,
        configPath
      )
      if (projectHit) return projectHit
    }
  }
  return null
}

/** 各编辑器常见 MCP 配置路径（按优先级） */
function defaultMcpConfigCandidates() {
  const home = os.homedir()
  const paths = []

  if (process.env.MASTERGO_MCP_CONFIG) {
    paths.push(process.env.MASTERGO_MCP_CONFIG)
  }

  paths.push(
    path.join(home, '.claude.json'),
    path.join(home, '.cursor', 'mcp.json'),
    path.join(home, '.config', 'cursor', 'mcp.json')
  )

  if (process.platform === 'darwin') {
    paths.push(
      path.join(
        home,
        'Library',
        'Application Support',
        'Claude',
        'claude_desktop_config.json'
      )
    )
  } else if (process.platform === 'win32') {
    paths.push(
      path.join(process.env.APPDATA || '', 'Claude', 'claude_desktop_config.json')
    )
  } else {
    paths.push(path.join(home, '.config', 'Claude', 'claude_desktop_config.json'))
  }

  paths.push(
    path.join(home, '.codeium', 'windsurf', 'mcp_config.json'),
    path.join(home, '.windsurf', 'mcp_config.json'),
    path.join(home, '.config', 'windsurf', 'mcp.json'),
    path.join(home, '.vscode', 'mcp.json'),
    path.join(home, '.config', 'Code', 'User', 'mcp.json')
  )

  const seen = new Set()
  return paths.filter((p) => {
    if (!p || seen.has(p)) return false
    seen.add(p)
    return true
  })
}

function readMcpConfig(configPath) {
  const abs = path.resolve(configPath)
  if (!fs.existsSync(abs)) return null
  try {
    const text = fs.readFileSync(abs, 'utf8')
    return { json: JSON.parse(text), abs }
  } catch {
    return null
  }
}

/**
 * @param {{ explicitPaths?: string[], skipScan?: boolean }} opts
 * @returns {{ token: string, baseUrl: string, source?: string, server?: string } | null}
 */
function resolveFromEditorMcp(opts = {}) {
  const candidates = opts.explicitPaths?.length
    ? opts.explicitPaths
    : defaultMcpConfigCandidates()

  for (const configPath of candidates) {
    const parsed = readMcpConfig(configPath)
    if (!parsed) continue
    const hit = extractFromConfigObject(parsed.json, parsed.abs)
    if (hit?.token) return hit
  }
  return null
}

module.exports = {
  defaultMcpConfigCandidates,
  resolveFromEditorMcp,
  extractFromConfigObject,
}
