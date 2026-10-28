#!/usr/bin/env node
/* eslint-disable no-console */

const fs = require('fs')
const path = require('path')

const DIMENSION_LABELS = {
  layoutStructure: '布局结构',
  visualStyle: '视觉样式',
  contentAssets: '内容与素材',
  perceptualSimilarity: '整体相似度',
}

const SEVERITY_LABELS = {
  critical: '严重',
  major: '主要',
  minor: '轻微',
}

function usage() {
  console.error(
    '用法：node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-print-fidelity-report.cjs <report-absolute-path>'
  )
}

function extractJson(markdown) {
  const match = markdown.match(/```json\s*([\s\S]*?)\s*```/i)
  if (!match) return null
  return JSON.parse(match[1])
}

function formatScore(score) {
  if (score === null || score === undefined || Number.isNaN(Number(score))) {
    return 'N/A'
  }
  return `${score}/100`
}

function formatGain(gain) {
  if (gain === null || gain === undefined || gain === '') return ''
  return `（预计提升 +${gain}）`
}

function printFromJson(report, reportPath) {
  const round =
    report.round === null || report.round === undefined ? '未知' : `r${report.round}`
  const reportFile = report.reportFile || reportPath

  console.log('===== D2C 还原度评测报告 =====')
  console.log(`报告路径：${reportFile}`)
  console.log(`评测轮次：${round}`)
  console.log(`总还原度：${formatScore(report.overallScore)}`)
  if (report.summary) console.log(`摘要：${report.summary}`)

  console.log('\n维度分数：')
  const dimensions = report.dimensions || {}
  Object.entries(DIMENSION_LABELS).forEach(([key, label]) => {
    const item = dimensions[key]
    if (!item) return
    const weight =
      item.weight === null || item.weight === undefined
        ? ''
        : `，权重 ${Math.round(Number(item.weight) * 100)}%`
    const deduction =
      item.deduction === null || item.deduction === undefined
        ? ''
        : `，扣分 ${item.deduction}`
    console.log(`- ${label}：${formatScore(item.score)}${weight}${deduction}`)
    if (item.notes) console.log(`  说明：${item.notes}`)
  })

  const plans = Array.isArray(report.optimizationPlan)
    ? report.optimizationPlan
    : []
  console.log('\n优化建议：')
  if (plans.length) {
    plans.slice(0, 7).forEach((item, index) => {
      const action = item.action || item.rationale || '未提供具体动作'
      console.log(
        `${index + 1}. ${action}${formatGain(item.expectedGain)}`
      )
      if (Array.isArray(item.findingIds) && item.findingIds.length) {
        console.log(`   对应问题：${item.findingIds.join(', ')}`)
      }
      if (item.acceptanceCriteria) {
        console.log(`   验收标准：${item.acceptanceCriteria}`)
      }
    })
  } else {
    console.log('- 暂无明确优化建议。')
  }

  const findings = Array.isArray(report.findings) ? report.findings : []
  console.log('\n主要扣分项：')
  if (findings.length) {
    findings.slice(0, 10).forEach((item) => {
      const id = item.id || '未编号'
      const dimension = DIMENSION_LABELS[item.dimension] || item.dimension || '未知维度'
      const severity = SEVERITY_LABELS[item.severity] || item.severity || '未知级别'
      const target = item.target ? `｜目标：${item.target}` : ''
      console.log(
        `- ${id}｜${dimension}｜${severity}${target}${formatGain(item.expectedGain)}`
      )
      if (item.evidence) console.log(`  证据：${item.evidence}`)
      if (item.suggestedChange) console.log(`  建议：${item.suggestedChange}`)
      if (item.acceptanceCriteria) {
        console.log(`  验收：${item.acceptanceCriteria}`)
      }
    })
  } else {
    console.log('- 暂无主要扣分项。')
  }
}

function printFallback(markdown, reportPath) {
  const humanPart = markdown.split(/##\s+JSON Report/i)[0].trim()
  console.log('===== D2C 还原度评测报告 =====')
  console.log(`报告路径：${reportPath}`)
  console.log('提示：未能解析 JSON Report，以下为报告正文摘录。')
  console.log('')
  console.log(humanPart || markdown.trim())
}

function main() {
  const reportArg = process.argv[2]
  if (!reportArg) {
    usage()
    process.exit(1)
  }

  const reportPath = path.resolve(reportArg)
  if (!fs.existsSync(reportPath)) {
    console.error(`报告文件不存在：${reportPath}`)
    process.exit(1)
  }

  const markdown = fs.readFileSync(reportPath, 'utf8')
  try {
    const report = extractJson(markdown)
    if (report) {
      printFromJson(report, reportPath)
      return
    }
  } catch (err) {
    // Fall through to markdown excerpt.
  }
  printFallback(markdown, reportPath)
}

main()
