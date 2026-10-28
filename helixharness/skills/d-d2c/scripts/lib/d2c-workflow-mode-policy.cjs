'use strict'

const WORKFLOW_MODES = new Set(['create-page', 'update-page', 'component'])
const PREVIEW_WORKFLOW_MODES = new Set(['create-page', 'component'])

function normalizeWorkflowMode(value) {
  const raw = String(value || '').trim().toLowerCase()
  if (!raw) return ''
  if (raw === 'page' || raw === 'create' || raw === 'new-page') return 'create-page'
  if (raw === 'update' || raw === 'existing-page' || raw === 'updatepage') return 'update-page'
  if (raw === 'components') return 'component'
  return WORKFLOW_MODES.has(raw) ? raw : ''
}

function previewAllowedForMode(value) {
  return PREVIEW_WORKFLOW_MODES.has(normalizeWorkflowMode(value))
}

function requirePreviewWorkflowMode(value, context = 'isolated preview') {
  const mode = normalizeWorkflowMode(value)
  if (!mode) {
    const error = new Error(
      `D2C_PREVIEW_MODE_REQUIRED context=${context}; pass --mode create-page or --mode component`
    )
    error.code = 'D2C_PREVIEW_MODE_REQUIRED'
    throw error
  }
  if (!previewAllowedForMode(mode)) {
    const error = new Error(
      `D2C_WORKFLOW_CAPABILITY_VIOLATION mode=${mode} capability=isolated-preview; ` +
      'update-page must use real-route then browser handoff'
    )
    error.code = 'D2C_WORKFLOW_CAPABILITY_VIOLATION'
    throw error
  }
  return mode
}

module.exports = {
  WORKFLOW_MODES,
  PREVIEW_WORKFLOW_MODES,
  normalizeWorkflowMode,
  previewAllowedForMode,
  requirePreviewWorkflowMode,
}
