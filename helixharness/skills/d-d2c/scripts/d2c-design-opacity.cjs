#!/usr/bin/env node
/* eslint-disable no-console */
const fs = require('fs')
const path = require('path')
// 结构化运行日志（非侵入、可降级）
let getLoggerForArtifact = () => ({ enabled: false })
try { ({ getLoggerForArtifact } = require('./lib/d2c-run-log.cjs')) } catch { /* 静默降级 */ }
const {
  parseArgs,
  decodeImage,
  readPng,
  writePng,
  getMd5,
  normalizePngBuffer,
  cropRgbaTop,
  cropRgbaRect,
  maskIgnoredDesignRegions,
  alignDesignToActualWidth,
  buildOpacityPng,
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
  const hasExplicitCrop =
    args['design-crop-top'] !== undefined && args['design-crop-top'] !== true

  if (!designPath) {
    console.error(
      'Usage: node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-design-opacity.cjs --design <design.png|jpg> [--actual <actual.png>] [--page-dir <page-dir> --scope <layoutMark>] [--out d-d2c/temp] [--name <页面标识>] [--artifact-infix <段>] [--design-crop-top <px>] [--opacity 0.3]'
    )
    process.exit(1)
  }

  const designAbs = path.resolve(designPath)
  const actualAbs = actualPath ? path.resolve(actualPath) : ''
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

  const designBuf = normalizePngBuffer(fs.readFileSync(designAbs))
  const md5 = getMd5(designBuf)
  const stem = buildArtifactStem(
    getArtifactLabel(args),
    md5,
    getArtifactInfix(args)
  )
  const rawDecoded = maskIgnoredDesignRegions(
    decodeImage(designAbs),
    cropResolved.ignoredDesignRegions,
    cropResolved.designFrameSize
  )
  const topCroppedDecoded = cropRgbaTop(
    rawDecoded.data,
    rawDecoded.width,
    rawDecoded.height,
    designCropTop
  )
  const designRegion = resolveDesignRegion({
    pageDir,
    scope: designScope,
    designFrameSize: cropResolved.designFrameSize,
    layoutShiftY: cropResolved.layoutShiftY,
    imageSize: { width: topCroppedDecoded.width, height: topCroppedDecoded.height },
  })
  const croppedDecoded = designRegion
    ? cropRgbaRect(
        topCroppedDecoded.data,
        topCroppedDecoded.width,
        topCroppedDecoded.height,
        designRegion.rasterRect
      )
    : topCroppedDecoded
  const scopedDesignPath = designRegion
    ? path.join(outAbs, `${stem}-design-source.png`)
    : null
  if (scopedDesignPath) writePng(scopedDesignPath, croppedDecoded)
  const actualPng = actualAbs ? readPng(actualAbs) : null
  const decoded = actualAbs
    ? alignDesignToActualWidth(croppedDecoded, actualPng.width)
    : { ...croppedDecoded, scale: 1 }
  const opacityPng = buildOpacityPng(
    decoded.data,
    decoded.width,
    decoded.height,
    opacity
  )
  const opacityPercent = Math.round(opacity * 100)
  const opacityPath = path.join(outAbs, `${stem}-design-${opacityPercent}.png`)
  writePng(opacityPath, opacityPng)

  // 结构化日志：透明设计稿产物（page 目录由产物路径回溯）
  try {
    const plog = getLoggerForArtifact(opacityPath)
    if (plog.enabled) { plog.scriptComplete('d2c-design-opacity', {}); plog.artifactWrite(opacityPath) }
  } catch { /* 日志失败不影响主流程 */ }

  const report = {
    step: 'design-opacity',
    md5,
    design: designAbs,
    actual: actualAbs || undefined,
    opacityPath,
    scopedDesignPath: scopedDesignPath || undefined,
    designScope: designRegion || undefined,
    opacity,
    originalWidth: rawDecoded.width,
    originalHeight: rawDecoded.height,
    designCropTop,
    designCropTopRaster: cropResolved.designCropTopRaster,
    layoutShiftY: cropResolved.layoutShiftY,
    contentOffsetY: cropResolved.contentOffsetY,
    hostLayoutPolicy: cropResolved.hostLayoutPolicy,
    ignoredDesignRegions: cropResolved.ignoredDesignRegions,
    designFrameSize: cropResolved.designFrameSize,
    imageSourceSize: cropResolved.imageSourceSize,
    cropSource: cropResolved.source,
    scale: decoded.scale,
    actualWidth: actualPng ? actualPng.width : undefined,
    actualHeight: actualPng ? actualPng.height : undefined,
    width: decoded.width,
    height: decoded.height,
  }
  console.log(JSON.stringify(report, null, 2))
}

main()
