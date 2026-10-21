/**
 * Classify how native status/navigation chrome relates to the H5 canvas.
 *
 * This runs on the raw MasterGo tree before chrome removal. It intentionally uses
 * only hierarchy, normalized geometry, clipping, and visual continuity so the
 * result is deterministic for text-only executors.
 */

const {
  getLayoutBox,
  isLikelyNativeWebViewShell,
  isLikelyStatusCluster,
  isNavTitleBarRemnantFrame,
  shouldDropChrome,
} = require('./dsl-chrome.cjs')

const POLICY_NONE = 'none'
const POLICY_IMMERSIVE = 'immersive-overlay'
const POLICY_STACKED = 'stacked-host'

function round1(value) {
  return Math.round(Number(value || 0) * 10) / 10
}

function getRoot(nodes) {
  return Array.isArray(nodes) && nodes.length === 1 ? nodes[0] : null
}

function frameSize(nodes) {
  const root = getRoot(nodes)
  if (root) {
    const box = getLayoutBox(root)
    if (box.w > 0 && box.h > 0) return { width: box.w, height: box.h }
  }
  let width = 0
  let height = 0
  for (const node of nodes || []) {
    const box = getLayoutBox(node)
    width = Math.max(width, box.x + box.w)
    height = Math.max(height, box.y + box.h)
  }
  return { width, height }
}

function subtreeHasText(node) {
  if (!node) return false
  if (node.type === 'TEXT') return true
  return (node.children || []).some(subtreeHasText)
}

function subtreeHasVisual(node) {
  if (!node) return false
  const hasFills = Array.isArray(node.fills)
    ? node.fills.length > 0
    : !!node.fills
  const hasPath = Array.isArray(node.path) ? node.path.length > 0 : !!node.path
  if (
    /^(?:PATH|LAYER|RECTANGLE|ELLIPSE|SVG_|IMAGE|BITMAP)/.test(
      String(node.type || '')
    ) ||
    node.fill ||
    hasFills ||
    hasPath ||
    node.strokeColor ||
    node.effect
  ) {
    return true
  }
  return (node.children || []).some(subtreeHasVisual)
}

function normalizeOverride(value) {
  const raw = String(value || 'auto').trim().toLowerCase()
  if (['immersive', 'immersive-overlay', 'preserve', 'preserve-space'].includes(raw)) {
    return POLICY_IMMERSIVE
  }
  if (['stacked', 'stacked-host', 'rebase', 'crop'].includes(raw)) {
    return POLICY_STACKED
  }
  return null
}

function isDirectHostNode(node, rootName, frame) {
  const box = getLayoutBox(node)
  const inTopHostBand =
    box.y <= Math.max(32, frame.height * 0.06) &&
    box.y + box.h <= Math.max(320, frame.height * 0.25)
  return (
    isLikelyNativeWebViewShell(node, frame) ||
    isLikelyStatusCluster(node, frame, 0, 0) ||
    isNavTitleBarRemnantFrame(node, [rootName]) ||
    (inTopHostBand && shouldDropChrome(node, [rootName], null))
  )
}

function isCrossBoundaryBackdrop(node, frame, hostBottomY) {
  const box = getLayoutBox(node)
  const broad = box.x <= frame.width * 0.06 && box.w >= frame.width * 0.85
  const startsBehindHost = box.y <= Math.max(8, hostBottomY * 0.2)
  const continuesIntoBusiness =
    box.y + box.h >= hostBottomY + Math.max(40, frame.height * 0.08)
  return (
    broad &&
    startsBehindHost &&
    continuesIntoBusiness &&
    subtreeHasVisual(node) &&
    !subtreeHasText(node)
  )
}

function isPostHostContentFrame(node, frame, hostBottomY) {
  const box = getLayoutBox(node)
  const nearBoundary =
    Math.abs(box.y - hostBottomY) <= Math.max(12, frame.height * 0.025)
  const pageSized = box.w >= frame.width * 0.85 && box.h >= frame.height * 0.4
  const container = /^(?:FRAME|GROUP|COMPONENT|INSTANCE)$/.test(
    String(node.type || '')
  )
  return (
    nearBoundary &&
    pageSized &&
    container &&
    (node.children || []).length > 0 &&
    (node.clipsContent === true || box.h >= frame.height * 0.65)
  )
}

function firstNestedBusiness(container) {
  const containerBox = getLayoutBox(container)
  const candidates = (container.children || []).filter((node) => {
    const box = getLayoutBox(node)
    const containerBackdrop =
      box.x <= containerBox.w * 0.06 &&
      box.y <= 8 &&
      box.w >= containerBox.w * 0.85 &&
      box.h >= containerBox.h * 0.5 &&
      subtreeHasVisual(node) &&
      !subtreeHasText(node)
    return !containerBackdrop
  })
  if (!candidates.length) {
    return { y: containerBox.y, name: String(container.name || '') }
  }
  const first = [...candidates].sort(
    (a, b) => getLayoutBox(a).y - getLayoutBox(b).y
  )[0]
  return {
    y: containerBox.y + getLayoutBox(first).y,
    name: String(first.name || ''),
  }
}

function confidenceFor(scores, policy, explicit) {
  if (explicit) return 1
  const lead = Math.abs(scores.immersive - scores.stacked)
  const winning = policy === POLICY_STACKED ? scores.stacked : scores.immersive
  if (winning >= 7 && lead >= 4) return 0.96
  if (winning >= 5 && lead >= 3) return 0.86
  return 0.45
}

function analyzeHostLayout(nodes, options = {}) {
  const frame = frameSize(nodes)
  const root = getRoot(nodes)
  const rootName = String(root?.name || '')
  const children = root && (root.children || []).length ? root.children : nodes || []
  const hostEntries = children
    .map((node, index) => ({ node, index, box: getLayoutBox(node) }))
    .filter((entry) => isDirectHostNode(entry.node, rootName, frame))

  if (!hostEntries.length || !frame.width || !frame.height) {
    return {
      version: 1,
      policy: POLICY_NONE,
      confidence: 1,
      source: 'dsl-structure',
      frame,
      hostChromeBottomY: 0,
      firstBusinessY: 0,
      firstBusinessName: '',
      businessTopGap: 0,
      layoutShiftY: 0,
      designCropTopDsl: 0,
      ignoredDesignRegions: [],
      evidence: ['no-native-host-shell-detected'],
      scores: { immersive: 0, stacked: 0 },
      hostNodes: [],
    }
  }

  const hostSet = new Set(hostEntries.map((entry) => entry.node))
  const hostChromeBottomY = round1(
    Math.max(...hostEntries.map((entry) => entry.box.y + entry.box.h))
  )
  const backdrops = children.filter(
    (node) =>
      !hostSet.has(node) &&
      isCrossBoundaryBackdrop(node, frame, hostChromeBottomY)
  )
  const backdropSet = new Set(backdrops)
  const business = children.filter(
    (node) => !hostSet.has(node) && !backdropSet.has(node)
  )
  const postHostFrames = business.filter((node) =>
    isPostHostContentFrame(node, frame, hostChromeBottomY)
  )
  const firstBusinessCandidates = postHostFrames.length
    ? postHostFrames.map(firstNestedBusiness)
    : business.map((node) => ({
        y: getLayoutBox(node).y,
        name: String(node.name || ''),
      }))
  const firstBusiness = firstBusinessCandidates.length
    ? [...firstBusinessCandidates].sort((a, b) => a.y - b.y)[0]
    : { y: hostChromeBottomY, name: '' }
  const firstBusinessY = round1(firstBusiness.y)
  const lastBusinessBottom = business.length
    ? round1(
        Math.max(
          ...business.map((node) => {
            const box = getLayoutBox(node)
            return box.y + box.h
          })
        )
      )
    : hostChromeBottomY
  const evidence = []
  const scores = { immersive: 0, stacked: 0 }
  if (backdrops.length) {
    scores.immersive += 5
    evidence.push('full-width-backdrop-crosses-host-boundary')
    const firstHostIndex = Math.min(...hostEntries.map((entry) => entry.index))
    const backdropBelowHost = backdrops.some(
      (node) => children.indexOf(node) < firstHostIndex
    )
    if (backdropBelowHost) {
      scores.immersive += 1
      evidence.push('cross-boundary-backdrop-is-below-host-shell')
    }
  }
  if (!postHostFrames.length && business.length > 1) {
    scores.immersive += 1
    evidence.push('business-blocks-are-root-siblings')
  }
  if (
    Math.abs(lastBusinessBottom - frame.height) <=
    Math.max(8, frame.height * 0.02)
  ) {
    scores.immersive += 1
    evidence.push('business-content-anchors-to-full-frame-bottom')
  }
  if (postHostFrames.length) {
    const clipped = postHostFrames.some((node) => node.clipsContent === true)
    scores.stacked += clipped ? 6 : 5
    evidence.push(
      clipped
        ? 'clipped-page-content-frame-starts-at-host-bottom'
        : 'page-sized-content-frame-starts-at-host-bottom'
    )
  }
  if (!backdrops.length && postHostFrames.length) {
    scores.stacked += 2
    evidence.push('no-visual-backdrop-crosses-host-boundary')
  }

  const explicitPolicy = normalizeOverride(options.override)
  let policy = explicitPolicy
  let source = explicitPolicy ? 'explicit-override' : 'dsl-structure'
  if (!policy) {
    if (scores.immersive >= 5 && scores.immersive - scores.stacked >= 3) {
      policy = POLICY_IMMERSIVE
    } else if (scores.stacked >= 5 && scores.stacked - scores.immersive >= 3) {
      policy = POLICY_STACKED
    } else {
      // Preserving raw coordinates is lossless and reversible. Rebasing is not.
      policy = POLICY_IMMERSIVE
      source = 'conservative-fallback'
      evidence.push('ambiguous-structure-preserves-raw-coordinate-space')
    }
  }

  const layoutShiftY = policy === POLICY_STACKED ? hostChromeBottomY : 0
  return {
    version: 1,
    policy,
    confidence: confidenceFor(scores, policy, !!explicitPolicy),
    source,
    frame,
    hostChromeBottomY,
    firstBusinessY,
    firstBusinessName: firstBusiness.name,
    businessTopGap: round1(Math.max(0, firstBusinessY - hostChromeBottomY)),
    layoutShiftY,
    designCropTopDsl: layoutShiftY,
    ignoredDesignRegions:
      policy === POLICY_IMMERSIVE
        ? [{ x: 0, y: 0, width: frame.width, height: hostChromeBottomY }]
        : [],
    evidence,
    scores,
    hostNodes: hostEntries.map((entry) => ({
      name: String(entry.node.name || ''),
      type: String(entry.node.type || ''),
      index: entry.index,
      bbox: {
        x: round1(entry.box.x),
        y: round1(entry.box.y),
        width: round1(entry.box.w),
        height: round1(entry.box.h),
      },
    })),
    crossBoundaryBackdrops: backdrops.map((node) => {
      const box = getLayoutBox(node)
      return {
        name: String(node.name || ''),
        type: String(node.type || ''),
        bbox: {
          x: round1(box.x),
          y: round1(box.y),
          width: round1(box.w),
          height: round1(box.h),
        },
      }
    }),
  }
}

module.exports = {
  POLICY_NONE,
  POLICY_IMMERSIVE,
  POLICY_STACKED,
  analyzeHostLayout,
  isCrossBoundaryBackdrop,
  isPostHostContentFrame,
  normalizeOverride,
}
