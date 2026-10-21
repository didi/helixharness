#!/usr/bin/env node
/* eslint-disable no-console */
/**
 * 设计稿（等比对齐到 actual 宽度）与代码截图按透明度叠图，输出 overlay。
 */
const fs = require('fs')
const path = require('path')
// 结构化运行日志（非侵入、可降级）
let getLoggerForArtifact = () => ({ enabled: false })
try { ({ getLoggerForArtifact } = require('./lib/d2c-run-log.cjs')) } catch { /* 静默降级 */ }
const {
  parseArgs,
  writePng,
  buildBlendOverlayPng,
  loadAlignedPair,
  getArtifactLabel,
  getArtifactInfix,
  buildArtifactStem,
  resolveDesignCropTop,
  DEFAULT_OUT,
  DEFAULT_OPACITY,
} = require('./lib/d2c-visual-core.cjs')
const { resolveDesignRegion } = require('./lib/d2c-design-region-core.cjs')

function main() {
  const args = parseArgs(process.argv)
  const designPath = args.design
  const actualPath = args.actual
  const outDir = args.out || DEFAULT_OUT
  const pageDir = args['page-dir'] && args['page-dir'] !== true ? args['page-dir'] : null
  const designScope = args.scope && args.scope !== true
    ? String(args.scope)
    : process.env.D2C_DESIGN_SCOPE || null
  const opacity = Number(args.opacity || DEFAULT_OPACITY)
  const actualOffsetX = Number(args['actual-offset-x'] || 0)
  const actualOffsetY = Number(args['actual-offset-y'] || 0)
  const hasExplicitCrop =
    args['design-crop-top'] !== undefined && args['design-crop-top'] !== true

  if (!designPath || !actualPath) {
    console.error(
      'Usage: node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-diff-overlay.cjs --design <design> --actual <actual.png> [--page-dir <page-dir> --scope <layoutMark>] [--out d-d2c/temp] [--name <页面标识>] [--artifact-infix <段>] [--design-crop-top <px>] [--actual-offset-y <px>] [--opacity 0.3]'
    )
    process.exit(1)
  }

  const designAbs = path.resolve(designPath)
  const actualAbs = path.resolve(actualPath)
  const outAbs = path.resolve(outDir)
  fs.mkdirSync(outAbs, { recursive: true })

  const cropResolved = resolveDesignCropTop({
    designAbs,
    explicitCropTop: hasExplicitCrop ? args['design-crop-top'] : undefined,
    layoutH5Path: args['layout-h5']
      ? path.resolve(args['layout-h5'])
      : pageDir || undefined,
  })
  const designCropTop = cropResolved.designCropTop
  if (cropResolved.fatalError) {
    console.error(cropResolved.fatalError)
    process.exit(2)
  }
  if (cropResolved.mismatchWarning) {
    console.error(cropResolved.mismatchWarning)
  } else if (!hasExplicitCrop && designCropTop > 0) {
    console.error(
      `[d2c] design-crop-top auto=${designCropTop} (layoutShiftY=${cropResolved.layoutShiftY}, policy=${cropResolved.hostLayoutPolicy}, image=${cropResolved.imageSourceSize.width}×${cropResolved.imageSourceSize.height})`
    )
  }

  const designRegion = resolveDesignRegion({
    pageDir,
    scope: designScope,
    designFrameSize: cropResolved.designFrameSize,
    layoutShiftY: cropResolved.layoutShiftY,
    imageSize: {
      width: Number(cropResolved.imageSourceSize?.width || 0),
      height: Math.max(
        1,
        Number(cropResolved.imageSourceSize?.height || 0) - designCropTop
      ),
    },
  })
  const { md5, design, actual } = loadAlignedPair(designAbs, actualAbs, {
    actualOffsetX,
    actualOffsetY,
    designCropTop,
    ignoredDesignRegions: cropResolved.ignoredDesignRegions,
    designFrameSize: cropResolved.designFrameSize,
    designRegionRaster: designRegion && designRegion.rasterRect,
  })
  const stem = buildArtifactStem(
    getArtifactLabel(args),
    md5,
    getArtifactInfix(args)
  )
  const blend = buildBlendOverlayPng(design, actual, opacity)
  const overlayPath = path.join(outAbs, `${stem}-overlay.png`)
  writePng(overlayPath, blend)

  // 结构化日志：叠图产物（page 目录由产物路径回溯）
  try {
    const olog = getLoggerForArtifact(overlayPath)
    if (olog.enabled) { olog.scriptComplete('d2c-diff-overlay', {}); olog.artifactWrite(overlayPath) }
  } catch { /* 日志失败不影响主流程 */ }

  const report = {
    step: 'diff-overlay',
    md5,
    design: designAbs,
    actual: actualAbs,
    overlayPath,
    opacity,
    originalDesignScale: design.scale,
    alignedDesignWidth: design.width,
    alignedDesignHeight: design.height,
    actualWidth: actual.width,
    actualHeight: actual.height,
    overlayWidth: blend.width,
    overlayHeight: blend.height,
    actualOffsetX,
    actualOffsetY,
    designCropTop,
    designCropTopRaster: cropResolved.designCropTopRaster,
    layoutShiftY: cropResolved.layoutShiftY,
    contentOffsetY: cropResolved.contentOffsetY,
    hostLayoutPolicy: cropResolved.hostLayoutPolicy,
    ignoredDesignRegions: cropResolved.ignoredDesignRegions,
    designFrameSize: cropResolved.designFrameSize,
    imageSourceSize: cropResolved.imageSourceSize,
    cropSource: cropResolved.source,
    designScope: designRegion || undefined,
  }
  console.log(JSON.stringify(report, null, 2))
}

main()
