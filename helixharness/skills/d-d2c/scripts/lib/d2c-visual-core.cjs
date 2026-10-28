/* eslint-disable no-console */
const fs = require('fs')
const path = require('path')
const crypto = require('crypto')
const { PNG } = require('pngjs')
const jpeg = require('jpeg-js')

const DEFAULT_OUT = 'd-d2c/temp'
const DEFAULT_OPACITY = 0.3

/**
 * 叠图产物文件名前缀中的「页面标识」（可与路由对应），允许中文。
 * 非法路径字符会替换为 `-`；未传 `--name` / `--tag` / `--page-tag` 时默认 `img`（兼容旧文件名）。
 */
function sanitizeArtifactLabel(raw) {
  if (raw === undefined || raw === null || raw === true) return ''
  const s = String(raw).trim()
  if (!s) return ''
  return s
    .replace(/[/\\:*?"<>|]/g, '-')
    .replace(/\s+/g, '-')
    .replace(/\.{2,}/g, '-')
    .replace(/-+/g, '-')
    .replace(/^[-_.]+|[-_.]+$/g, '')
}

function getArtifactLabel(args) {
  const fromArg = args.name || args.tag || args['page-tag']
  if (fromArg && fromArg !== true) {
    const s = sanitizeArtifactLabel(fromArg)
    if (s) return s
  }
  return 'img'
}

/**
 * 产物名中间段，如 `self-check`，生成 `page-md5-self-check-actual.png`。
 * 不传或为空则保持旧格式 `page-md5-actual.png`。
 */
function getArtifactInfix(args) {
  const fromArg = args['artifact-infix'] || args.infix
  if (fromArg && fromArg !== true) {
    const s = sanitizeArtifactLabel(fromArg)
    if (s) return s
  }
  return ''
}

function buildArtifactStem(label, md5, infix = '') {
  const mid = infix ? `-${infix}` : ''
  return `${label}-${md5}${mid}`
}

function parseArgs(argv) {
  const args = {}
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i]
    if (token.startsWith('--')) {
      const key = token.slice(2)
      const value =
        argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : true
      args[key] = value
      if (value !== true) i += 1
    }
  }
  return args
}

function normalizePngBuffer(buffer) {
  const iendSignature = Buffer.from([
    0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
  ])
  const idx = buffer.indexOf(iendSignature)
  if (idx === -1) return buffer
  const end = idx + iendSignature.length
  return end >= buffer.length ? buffer : buffer.slice(0, end)
}

function decodeImage(filePath) {
  const buf = fs.readFileSync(filePath)
  const ext = path.extname(filePath).toLowerCase()
  if (buf[0] === 0xff && buf[1] === 0xd8) {
    const j = jpeg.decode(buf, { useTArray: true })
    return { width: j.width, height: j.height, data: j.data }
  }
  if (
    buf[0] === 0x89 &&
    buf[1] === 0x50 &&
    buf[2] === 0x4e &&
    buf[3] === 0x47
  ) {
    const p = PNG.sync.read(normalizePngBuffer(buf))
    return { width: p.width, height: p.height, data: p.data }
  }
  if (ext === '.png') {
    const p = PNG.sync.read(normalizePngBuffer(buf))
    return { width: p.width, height: p.height, data: p.data }
  }
  if (ext === '.jpg' || ext === '.jpeg') {
    const j = jpeg.decode(buf, { useTArray: true })
    return { width: j.width, height: j.height, data: j.data }
  }
  const p = PNG.sync.read(normalizePngBuffer(buf))
  return { width: p.width, height: p.height, data: p.data }
}

function readPng(filePath) {
  const buf = fs.readFileSync(filePath)
  return PNG.sync.read(normalizePngBuffer(buf))
}

function writePng(filePath, png) {
  fs.writeFileSync(filePath, PNG.sync.write(png))
}

function getMd5(buffer) {
  return crypto.createHash('md5').update(buffer).digest('hex').slice(0, 12)
}

function getDesignSessionMd5(designAbsPath) {
  const designBuf = normalizePngBuffer(fs.readFileSync(designAbsPath))
  return getMd5(designBuf)
}

function resizeRgbaNearest(srcData, srcW, srcH, dstW, dstH) {
  const out = Buffer.alloc(dstW * dstH * 4)
  for (let y = 0; y < dstH; y += 1) {
    const sy = Math.min(srcH - 1, Math.floor((y / dstH) * srcH))
    for (let x = 0; x < dstW; x += 1) {
      const sx = Math.min(srcW - 1, Math.floor((x / dstW) * srcW))
      const s = (sy * srcW + sx) << 2
      const d = (y * dstW + x) << 2
      out[d] = srcData[s]
      out[d + 1] = srcData[s + 1]
      out[d + 2] = srcData[s + 2]
      out[d + 3] = srcData[s + 3]
    }
  }
  return out
}

function cropRgbaTop(srcData, width, height, cropTop) {
  const cut = Math.max(0, Math.min(height - 1, Math.floor(cropTop || 0)))
  if (!cut) return { width, height, data: srcData }
  const dstH = height - cut
  const out = Buffer.alloc(width * dstH * 4)
  for (let y = 0; y < dstH; y += 1) {
    const s = ((y + cut) * width) << 2
    const d = (y * width) << 2
    out.set(srcData.subarray(s, s + width * 4), d)
  }
  return { width, height: dstH, data: out }
}

function cropRgbaRect(srcData, width, height, rect) {
  if (!rect) return { width, height, data: srcData }
  const left = Math.max(0, Math.min(width - 1, Math.floor(Number(rect.left || 0))))
  const top = Math.max(0, Math.min(height - 1, Math.floor(Number(rect.top || 0))))
  const right = Math.max(left + 1, Math.min(width, Math.ceil(Number(rect.right ?? width))))
  const bottom = Math.max(top + 1, Math.min(height, Math.ceil(Number(rect.bottom ?? height))))
  const dstW = right - left
  const dstH = bottom - top
  const out = Buffer.alloc(dstW * dstH * 4)
  for (let y = 0; y < dstH; y += 1) {
    const srcStart = (((top + y) * width) + left) << 2
    const dstStart = (y * dstW) << 2
    out.set(srcData.subarray(srcStart, srcStart + dstW * 4), dstStart)
  }
  return { width: dstW, height: dstH, data: out }
}

/** Map a DSL design-space bbox into an image's raster coordinates. */
function designRectToRasterRect(rect, designFrameSize, imageSize) {
  const frameW = Number(designFrameSize?.width || 0)
  const frameH = Number(designFrameSize?.height || 0)
  const imageW = Number(imageSize?.width || 0)
  const imageH = Number(imageSize?.height || 0)
  if (!(frameW > 0) || !(frameH > 0) || !(imageW > 0) || !(imageH > 0)) {
    throw new Error('design scope 栅格换算缺少有效的画板或图片尺寸')
  }
  const x = Number(rect?.x || 0)
  const y = Number(rect?.y || 0)
  const width = Number(rect?.width || 0)
  const height = Number(rect?.height || 0)
  if (!(width > 0) || !(height > 0)) throw new Error('design scope bbox 无效')
  const left = Math.max(0, Math.floor((x * imageW) / frameW))
  const top = Math.max(0, Math.floor((y * imageH) / frameH))
  const right = Math.min(imageW, Math.ceil(((x + width) * imageW) / frameW))
  const bottom = Math.min(imageH, Math.ceil(((y + height) * imageH) / frameH))
  if (right <= left || bottom <= top) throw new Error('design scope 映射后的栅格 bbox 为空')
  return { left, top, right, bottom, width: right - left, height: bottom - top }
}

/**
 * 把 RGBA 位图裁到目标宽度（保留左侧 targetW 列，高度不变）。
 * 用于钳制渲染截图：页面横向溢出时 fullPage 截图宽度会大于设计视口宽，
 * 裁掉右侧溢出列，使叠图缩放（actualW/designW）不再把设计稿纵向放大。
 * 与设计画板自身的横向裁切语义一致（LTR 页面保留左侧）。
 */
function cropRgbaWidth(srcData, width, height, targetW) {
  const tw = Math.max(1, Math.min(width, Math.floor(targetW || width)))
  if (tw === width) return { width, height, data: srcData }
  const out = Buffer.alloc(tw * height * 4)
  for (let y = 0; y < height; y += 1) {
    const s = (y * width) << 2
    const d = (y * tw) << 2
    out.set(srcData.subarray(s, s + tw * 4), d)
  }
  return { width: tw, height, data: out }
}

/**
 * 将设计稿对齐到实际截图的宽度坐标系。
 * 叠图必须先统一宽度，否则不同画布宽度会导致比例错位。
 */
function alignDesignToActualWidth(designDecoded, actualW) {
  if (!actualW || actualW <= 0) {
    throw new Error('actual width must be greater than 0')
  }
  if (designDecoded.width === actualW) {
    return {
      width: designDecoded.width,
      height: designDecoded.height,
      data: designDecoded.data,
      scale: 1,
    }
  }
  const scale = actualW / designDecoded.width
  const outH = Math.max(1, Math.round(designDecoded.height * scale))
  return {
    width: actualW,
    height: outH,
    data: resizeRgbaNearest(
      designDecoded.data,
      designDecoded.width,
      designDecoded.height,
      actualW,
      outH
    ),
    scale,
  }
}

/** Make native-only design regions transparent before design/actual blending. */
function maskIgnoredDesignRegions(decoded, regions, designFrameSize) {
  const frameW = Number(designFrameSize?.width || 0)
  const frameH = Number(designFrameSize?.height || 0)
  if (!frameW || !frameH || !(regions || []).length) return decoded
  const data = Buffer.from(decoded.data)
  for (const region of regions || []) {
    const left = Math.max(
      0,
      Math.floor((Number(region.x || 0) * decoded.width) / frameW)
    )
    const top = Math.max(
      0,
      Math.floor((Number(region.y || 0) * decoded.height) / frameH)
    )
    const right = Math.min(
      decoded.width,
      Math.ceil(
        ((Number(region.x || 0) + Number(region.width || 0)) * decoded.width) /
          frameW
      )
    )
    const bottom = Math.min(
      decoded.height,
      Math.ceil(
        ((Number(region.y || 0) + Number(region.height || 0)) *
          decoded.height) /
          frameH
      )
    )
    for (let y = top; y < bottom; y += 1) {
      for (let x = left; x < right; x += 1) {
        data[((y * decoded.width + x) << 2) + 3] = 0
      }
    }
  }
  return { ...decoded, data }
}

function buildOpacityPng(designRgba, width, height, opacity) {
  const opacityPng = new PNG({ width, height })
  for (let i = 0; i < designRgba.length; i += 4) {
    const effectiveOpacity = opacity * (Number(designRgba[i + 3]) / 255)
    opacityPng.data[i] = Math.round(
      designRgba[i] * effectiveOpacity + 255 * (1 - effectiveOpacity)
    )
    opacityPng.data[i + 1] = Math.round(
      designRgba[i + 1] * effectiveOpacity + 255 * (1 - effectiveOpacity)
    )
    opacityPng.data[i + 2] = Math.round(
      designRgba[i + 2] * effectiveOpacity + 255 * (1 - effectiveOpacity)
    )
    opacityPng.data[i + 3] = 255
  }
  return opacityPng
}

function buildBlendOverlayPng(design, actualPng, opacity) {
  const { width, height, data: actualData } = actualPng
  const designRgba = design.data || design
  const designWidth = design.width || width
  const designHeight = design.height || height
  const diff = new PNG({ width, height })
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const idx = (width * y + x) << 2
      if (x < designWidth && y < designHeight) {
        const didx = (designWidth * y + x) << 2
        const effectiveOpacity =
          opacity * (Number(designRgba[didx + 3]) / 255)
        diff.data[idx] = Math.round(
          designRgba[didx] * effectiveOpacity +
            actualData[idx] * (1 - effectiveOpacity)
        )
        diff.data[idx + 1] = Math.round(
          designRgba[didx + 1] * effectiveOpacity +
            actualData[idx + 1] * (1 - effectiveOpacity)
        )
        diff.data[idx + 2] = Math.round(
          designRgba[didx + 2] * effectiveOpacity +
            actualData[idx + 2] * (1 - effectiveOpacity)
        )
      } else {
        diff.data[idx] = actualData[idx]
        diff.data[idx + 1] = actualData[idx + 1]
        diff.data[idx + 2] = actualData[idx + 2]
      }
      diff.data[idx + 3] = 255
    }
  }
  return diff
}

/**
 * 在不改动原始截图文件的前提下，对 actual 做像素级平移（用于含状态栏稿的人工对齐）。
 * offsetY > 0 表示实际截图整体“下移”，顶部会补白。
 */
function shiftPngRgba(src, offsetX, offsetY, fill = [255, 255, 255, 255]) {
  const out = new PNG({ width: src.width, height: src.height })
  for (let i = 0; i < out.data.length; i += 4) {
    out.data[i] = fill[0]
    out.data[i + 1] = fill[1]
    out.data[i + 2] = fill[2]
    out.data[i + 3] = fill[3]
  }
  for (let y = 0; y < src.height; y += 1) {
    for (let x = 0; x < src.width; x += 1) {
      const nx = x + offsetX
      const ny = y + offsetY
      if (nx < 0 || ny < 0 || nx >= src.width || ny >= src.height) continue
      const s = (y * src.width + x) << 2
      const d = (ny * src.width + nx) << 2
      out.data[d] = src.data[s]
      out.data[d + 1] = src.data[s + 1]
      out.data[d + 2] = src.data[s + 2]
      out.data[d + 3] = src.data[s + 3]
    }
  }
  return out
}

/**
 * 将 DSL 画板坐标的 layoutShiftY 换算为 image-source.png 栅格裁切高度。
 * 导出 PNG 尺寸常与 MasterGo 画板（如 750×1602）不一致，禁止直接把 layoutShiftY 当 --design-crop-top。
 */
function computeRasterDesignCropTop(layoutShiftY, designFrameHeight, imageSourceHeight) {
  const offset = Math.max(0, Number(layoutShiftY || 0))
  const frameH = Number(designFrameHeight || 0)
  const imgH = Number(imageSourceHeight || 0)
  if (!offset || !frameH || !imgH) return 0
  return Math.round((offset * imgH) / frameH)
}

function readLayoutH5Json(layoutH5PathOrPageDir) {
  const base = path.resolve(layoutH5PathOrPageDir)
  const layoutPath = base.endsWith('.json')
    ? base
    : path.join(base, 'layout-h5.json')
  if (!fs.existsSync(layoutPath)) return { path: layoutPath, data: null }
  try {
    return { path: layoutPath, data: JSON.parse(fs.readFileSync(layoutPath, 'utf8')) }
  } catch (e) {
    return { path: layoutPath, data: null, error: e.message }
  }
}

/**
 * 解析叠图裁切像素（仅用于 image-source 栅格，非 DSL/H5 坐标）。
 */
function resolveDesignCropTop(options = {}) {
  const designAbs = path.resolve(options.designAbs)
  const raw = decodeImage(designAbs)
  const imageSourceSize = { width: raw.width, height: raw.height }

  const layoutArg = options.layoutH5Path
  const { path: layoutPath, data: layout } = layoutArg
    ? readLayoutH5Json(layoutArg)
    : readLayoutH5Json(path.dirname(designAbs))

  const layoutShiftY = Number(
    layout?.layoutShiftY ?? 0
  )
  const contentOffsetY = layoutShiftY
  const hostLayoutPolicy = String(layout?.hostLayoutPolicy || 'none')
  const ignoredDesignRegions = Array.isArray(layout?.ignoredDesignRegions)
    ? layout.ignoredDesignRegions
    : []
  const designFrameSize = layout?.designFrameSize || null
  const frameH = Number(designFrameSize?.height || 0)

  let designCropTopRaster = 0
  if (layout?.designCropTopRaster != null && layout?.imageSourceSize) {
    const sameImage =
      Number(layout.imageSourceSize.width) === raw.width &&
      Number(layout.imageSourceSize.height) === raw.height
    if (sameImage) {
      designCropTopRaster = Math.max(
        0,
        Math.floor(Number(layout.designCropTopRaster))
      )
    }
  }
  if (!designCropTopRaster && layoutShiftY && frameH) {
    designCropTopRaster = computeRasterDesignCropTop(
      layoutShiftY,
      frameH,
      raw.height
    )
  }
  const storedPolicyError =
    hostLayoutPolicy === 'immersive-overlay' && designCropTopRaster > 0
      ? `⛔ layout-h5.json 冲突：immersive-overlay 的 designCropTopRaster 必须为 0，当前为 ${designCropTopRaster}。请重新运行 filter-dsl。`
      : null

  const explicitProvided =
    options.explicitCropTop !== undefined && options.explicitCropTop !== null
  const explicit = explicitProvided
    ? Math.max(0, Math.floor(Number(options.explicitCropTop)))
    : null

  let source = 'layout-h5'
  let designCropTop = designCropTopRaster
  let mismatchWarning = null
  let fatalError = storedPolicyError
  if (storedPolicyError) mismatchWarning = storedPolicyError

  if (explicit !== null) {
    designCropTop = explicit
    source = 'explicit'
    if (hostLayoutPolicy === 'immersive-overlay' && explicit > 0) {
      fatalError =
        '⛔ 当前宿主策略为 immersive-overlay，必须保留完整 H5 坐标空间；design-crop-top 应为 0。'
      mismatchWarning = fatalError
    } else if (
      designCropTopRaster > 0 &&
      explicit !== designCropTopRaster &&
      (explicit === contentOffsetY ||
        Math.abs(explicit - contentOffsetY) <= 2)
    ) {
      mismatchWarning = [
        '⛔ design-crop-top 疑似使用了 DSL 画板坐标 layoutShiftY=',
        layoutShiftY,
        'px；当前 image-source 为 ',
        `${raw.width}×${raw.height}`,
        '，应使用栅格裁切 designCropTopRaster=',
        designCropTopRaster,
        'px（见 layout-h5.json）。叠图可能错位。',
      ].join('')
    } else if (
      designCropTopRaster > 0 &&
      Math.abs(explicit - designCropTopRaster) > 2
    ) {
      mismatchWarning = [
        '⚠️ 显式 design-crop-top=',
        explicit,
        ' 与 layout-h5 推算的 designCropTopRaster=',
        designCropTopRaster,
        ' 不一致（>2px）。',
      ].join('')
    }
  } else if (!designCropTopRaster && !layoutShiftY) {
    source = 'default'
    designCropTop = 0
  }

  return {
    designCropTop,
    designCropTopRaster,
    layoutShiftY,
    contentOffsetY,
    hostLayoutPolicy,
    ignoredDesignRegions,
    designFrameSize,
    imageSourceSize,
    layoutH5Path: layout ? layoutPath : null,
    source,
    explicitCropTop: explicit,
    mismatchWarning,
    fatalError,
  }
}

function loadAlignedPair(designAbs, actualAbs, options = {}) {
  const rawDesign = maskIgnoredDesignRegions(
    decodeImage(designAbs),
    options.ignoredDesignRegions,
    options.designFrameSize
  )
  const designCropTop = Number(options.designCropTop || 0)
  const topCroppedDesign = cropRgbaTop(
    rawDesign.data,
    rawDesign.width,
    rawDesign.height,
    designCropTop
  )
  const designDecoded = options.designRegionRaster
    ? cropRgbaRect(
        topCroppedDesign.data,
        topCroppedDesign.width,
        topCroppedDesign.height,
        options.designRegionRaster
      )
    : topCroppedDesign
  const rawActual = readPng(actualAbs)
  const offsetX = Number(options.actualOffsetX || 0)
  const offsetY = Number(options.actualOffsetY || 0)
  const actual =
    offsetX || offsetY
      ? shiftPngRgba(rawActual, offsetX, offsetY)
      : rawActual

  const alignedDesign = alignDesignToActualWidth(designDecoded, actual.width)

  const md5 = getDesignSessionMd5(designAbs)
  return {
    md5,
    design: alignedDesign,
    actual,
  }
}

module.exports = {
  parseArgs,
  decodeImage,
  readPng,
  writePng,
  getMd5,
  normalizePngBuffer,
  getDesignSessionMd5,
  resizeRgbaNearest,
  cropRgbaTop,
  cropRgbaRect,
  designRectToRasterRect,
  cropRgbaWidth,
  alignDesignToActualWidth,
  maskIgnoredDesignRegions,
  buildOpacityPng,
  buildBlendOverlayPng,
  shiftPngRgba,
  loadAlignedPair,
  computeRasterDesignCropTop,
  readLayoutH5Json,
  resolveDesignCropTop,
  sanitizeArtifactLabel,
  getArtifactLabel,
  getArtifactInfix,
  buildArtifactStem,
  DEFAULT_OUT,
  DEFAULT_OPACITY,
}
