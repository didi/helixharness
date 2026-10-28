const fs = require('fs')
const path = require('path')

const ROUTES_FILE = process.env.HELIXHARNESS_FRONTEND_ROUTES_FILE || path.resolve(__dirname, '../../config/transponder-routes.json')
let cached

function loadTransponderRoutes() {
  if (cached !== undefined) return cached
  try {
    const parsed = JSON.parse(fs.readFileSync(ROUTES_FILE, 'utf8'))
    const transponderUrl = process.env.HELIXHARNESS_FRONTEND_ASSET_URL || (typeof parsed.transponderUrl === 'string'
      ? parsed.transponderUrl.trim()
      : '')
    const businessLines = Array.isArray(parsed.businessLines)
      ? parsed.businessLines.filter((item) => typeof item === 'string' && item.trim()).map((item) => item.trim())
      : []
    const aliases = parsed && typeof parsed.aliases === 'object' && parsed.aliases
      ? parsed.aliases
      : {}
    cached = { transponderUrl, businessLines, aliases }
  } catch {
    cached = { transponderUrl: '', businessLines: [], aliases: {} }
  }
  return cached
}

function listBusinessLines() {
  return loadTransponderRoutes().businessLines
}

function resolveTransponderUrlByBusinessLine(name) {
  const input = String(name || '').trim()
  if (!input) return null
  const config = loadTransponderRoutes()
  const businessLine = typeof config.aliases[input] === 'string'
    ? config.aliases[input].trim()
    : input
  const value = config.transponderUrl
  if (typeof value !== 'string' || !value.trim()) return null
  return {
    businessLine,
    url: value.trim().replace(/\/+$/, ''),
    source: ROUTES_FILE,
    known: config.businessLines.includes(businessLine),
  }
}

module.exports = {
  ROUTES_FILE,
  loadTransponderRoutes,
  listBusinessLines,
  resolveTransponderUrlByBusinessLine,
}
