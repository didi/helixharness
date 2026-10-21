#!/usr/bin/env node
/* eslint-disable no-console */
/**
 * 解析 image-source.png 叠图裁切高度（栅格像素，非 DSL 画板坐标）。
 *
 * Usage:
 *   node d2c-resolve-design-crop.cjs --design <image-source.png> [--layout-h5 <layout-h5.json>] [--design-crop-top <px>] [--print json|value|raster]
 */
const path = require('path')
const { parseArgs, resolveDesignCropTop } = require('./lib/d2c-visual-core.cjs')

function main() {
  const args = parseArgs(process.argv)
  const designPath = args.design
  if (!designPath) {
    console.error(
      'Usage: node d2c-resolve-design-crop.cjs --design <image-source.png> [--layout-h5 <path>] [--design-crop-top <px>] [--print json|value|raster]'
    )
    process.exit(1)
  }

  const hasExplicit =
    args['design-crop-top'] !== undefined && args['design-crop-top'] !== true
  const resolved = resolveDesignCropTop({
    designAbs: path.resolve(designPath),
    explicitCropTop: hasExplicit ? args['design-crop-top'] : undefined,
    layoutH5Path: args['layout-h5'] ? path.resolve(args['layout-h5']) : undefined,
  })

  if (resolved.mismatchWarning) {
    console.error(resolved.mismatchWarning)
  }
  if (resolved.fatalError) process.exit(2)

  const printMode = String(args.print || 'json')
  if (printMode === 'value' || printMode === 'raster') {
    process.stdout.write(String(resolved.designCropTop))
    return
  }

  console.log(JSON.stringify(resolved, null, 2))
}

main()
