'use strict'

const {
  loadMarkedDsl,
  resolveDesignScopeFrame,
} = require('./d2c-dom-measure-core.cjs')
const { designRectToRasterRect } = require('./d2c-visual-core.cjs')

function parseScopeMarks(value) {
  if (value === undefined || value === null || value === true) return []
  return String(value).split(',').map((mark) => mark.trim()).filter(Boolean)
}

function resolveDesignRegion(options = {}) {
  const scopeMarks = parseScopeMarks(options.scope)
  if (!scopeMarks.length) return null
  if (!options.pageDir) throw new Error('--scope 生成局部设计物料时必须同时传 --page-dir')

  const { nodes } = loadMarkedDsl(options.pageDir)
  const scopeFrame = resolveDesignScopeFrame(nodes, scopeMarks)
  const fullFrame = options.designFrameSize || {}
  const layoutShiftY = Math.max(0, Number(options.layoutShiftY || 0))
  const measurementFrameSize = {
    width: Number(fullFrame.width || 0),
    height: Math.max(1, Number(fullFrame.height || 0) - layoutShiftY),
  }
  const rasterRect = designRectToRasterRect(
    scopeFrame,
    measurementFrameSize,
    options.imageSize
  )
  return {
    layoutMarks: scopeMarks,
    scopeFrame,
    measurementFrameSize,
    rasterRect,
  }
}

module.exports = {
  parseScopeMarks,
  resolveDesignRegion,
}
