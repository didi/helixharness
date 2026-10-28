'use strict'

/**
 * isolated Vue preview 的「入口是否真的执行」归因。
 *
 * 背景：preview 物料只有在隔离入口真实执行并 mount 之后才可信。模板预置属性、
 * 静态快照、splitChunks 把入口代码拆到未加载的 chunk、dev server 未重启等情况
 * 都会让页面「看起来有内容」但入口从未跑起来。这里把归因做成纯函数，
 * 让上层拿到专用错误码与修法，而不是自己猜。
 */

/** 入口执行断言的专用错误码。 */
const PREVIEW_ENTRY_NOT_EXECUTED = 'D2C_PREVIEW_ENTRY_NOT_EXECUTED'

/**
 * @param {object} domProbe capture-meta.json 的 domProbe
 * @returns {{code: string, cause: string, detail: string, repair: string}|null}
 *   入口未执行时返回归因，已执行时返回 null。
 */
function classifyPreviewEntryFailure(domProbe) {
  const probe = domProbe || {}
  if (probe.previewEntryExecuted === true) return null

  // 入口自身守卫判定「当前不是预览页」→ 说明截的是主应用（或其他非预览）页面。
  // 这与「入口没执行」是两回事：入口执行了，只是主动不做任何副作用。
  if (probe.previewEntrySkipped) {
    return {
      code: PREVIEW_ENTRY_NOT_EXECUTED,
      cause: 'entry-skipped-not-a-preview-page',
      detail:
        `隔离预览入口已加载并主动跳过（${probe.previewEntrySkipped}）：当前页面的 <html> 上没有 ` +
        'data-d2c-template-preview-slug，不是预览模板页。',
      repair:
        '确认截图 URL 指向预览页（client/d2c-preview/<slug>.html，或 --slug 对应的 previewUrl），' +
        '而不是主应用 index.html。入口覆盖 Vue.prototype 与抢占 #app 的副作用已被守卫拦住，主应用不会被打挂。',
    }
  }

  const entryError = probe.previewEntryError ? String(probe.previewEntryError) : ''
  if (entryError) {
    return {
      code: PREVIEW_ENTRY_NOT_EXECUTED,
      cause: 'entry-mounted-with-error',
      detail: `隔离入口已加载，但 mount 抛出异常：${entryError}`,
      repair:
        '先修掉 mount 异常（通常是目标 .vue 的 props/store/router 依赖或全局注册缺失），再重跑截图与三轴校验。',
    }
  }

  const entryScripts = Array.isArray(probe.previewEntryScriptSrcs) ? probe.previewEntryScriptSrcs.filter(Boolean) : []
  if (!entryScripts.length) {
    return {
      code: PREVIEW_ENTRY_NOT_EXECUTED,
      cause: 'entry-chunk-not-injected',
      detail:
        '预览 HTML 中没有任何指向隔离入口 chunk 的 <script>，入口代码不可能被执行。' +
        `（已加载脚本：${Array.isArray(probe.scriptSrcs) && probe.scriptSrcs.length ? probe.scriptSrcs.join(', ') : '无'}）`,
      repair:
        '检查 HtmlWebpackPlugin 的 chunks 是否包含隔离入口 key，以及 optimization.splitChunks 是否把入口 chunk 拆成未注入的子 chunk；修正后重启 dev server 重跑。',
    }
  }

  if (probe.likelyStaticHtmlReplay) {
    return {
      code: PREVIEW_ENTRY_NOT_EXECUTED,
      cause: 'static-html-replay',
      detail: '页面只加载了 Vue runtime 而没有 module 脚本，属于静态快照/DOM replay，不是隔离入口执行的结果。',
      repair: '改用 d2c-isolated-vue-preview.cjs 生成的真实构建链入口，禁止用静态快照充当 preview 物料。',
    }
  }

  return {
    code: PREVIEW_ENTRY_NOT_EXECUTED,
    cause: 'entry-script-loaded-but-not-executed',
    detail:
      `入口 chunk 已注入（${entryScripts.join(', ')}）但未写入执行断言，` +
      '通常是被 splitChunks 拆到未加载的子 chunk，或 dev server 未加载补丁后的 webpack 配置。',
    repair:
      '重启 dev server 让补丁后的 webpack 配置生效；若仍失败，收窄 optimization.splitChunks（把隔离入口排除在拆分之外）后重跑。',
  }
}

/** 入口执行断言的证据摘要，供 capture-meta 与报告展示。 */
function summarizePreviewEntryEvidence(domProbe) {
  const probe = domProbe || {}
  return {
    previewEntryExecuted: probe.previewEntryExecuted === true,
    previewEntryError: probe.previewEntryError || null,
    previewEntrySkipped: probe.previewEntrySkipped || null,
    previewEntryScriptSrcs: Array.isArray(probe.previewEntryScriptSrcs) ? probe.previewEntryScriptSrcs : [],
    mountedNodeCount: Number.isFinite(probe.mountedNodeCount) ? probe.mountedNodeCount : null,
  }
}

module.exports = {
  PREVIEW_ENTRY_NOT_EXECUTED,
  classifyPreviewEntryFailure,
  summarizePreviewEntryEvidence,
}
