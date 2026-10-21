#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const { requirePreviewWorkflowMode } = require('./lib/d2c-workflow-mode-policy.cjs');

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (!key.startsWith('--')) {
      throw new Error(`Unexpected argument: ${key}`);
    }
    const name = key.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith('--')) {
      args[name] = true;
    } else {
      args[name] = next;
      i += 1;
    }
  }
  return args;
}

function usage() {
  return [
    'Usage:',
    '  node d2c-isolated-vue-preview.cjs --mode <create-page|component> --page-dir d-d2c/temp/<page> --target <path/to/page.vue> [options]',
    '',
    'Options:',
    '  --mode <create-page|component> Workflow mode. update-page is not allowed.',
    '  --slug <slug>                 Preview slug. Defaults to page-dir basename.',
    '  --webpack-config <path>       Webpack config to patch. Auto-detected when omitted.',
    '  --html-template <path>        HTML template to clone. Auto-detected when omitted.',
    '  --base-url <url>              Dev-server base URL used for printed previewUrl.',
    '  --target-url <url>            Original target URL, recorded in preview-harness.json.',
    '',
    'This creates a real Vue/webpack preview entry and HtmlWebpackPlugin page.',
    'It must not be used to snapshot or replay DOM.',
  ].join('\n');
}

function fail(message) {
  console.error(`❌ ${message}`);
  process.exit(1);
}

function posixPath(value) {
  return value.split(path.sep).join('/');
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function readText(file) {
  return fs.readFileSync(file, 'utf8');
}

function writeText(file, text) {
  ensureDir(path.dirname(file));
  fs.writeFileSync(file, text);
}

function findFirst(root, candidates) {
  for (const candidate of candidates) {
    const abs = path.resolve(root, candidate);
    if (fs.existsSync(abs)) return abs;
  }
  return null;
}

function sanitizeSlug(value) {
  return String(value || 'preview')
    .replace(/[^a-zA-Z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80) || 'preview';
}

function jsString(value) {
  return JSON.stringify(value);
}

function makeRelativeImport(fromFile, toFile) {
  let rel = posixPath(path.relative(path.dirname(fromFile), toFile));
  if (!rel.startsWith('.')) rel = `./${rel}`;
  return rel;
}

function harnessScript() {
  return path.join(__dirname, 'd2c-preview-harness.cjs');
}

function runHarness(args) {
  const script = harnessScript();
  if (!fs.existsSync(script)) {
    fail(`Missing preview harness helper: ${script}`);
  }
  const result = spawnSync(process.execPath, [script, ...args], {
    stdio: 'inherit',
    cwd: process.cwd(),
  });
  if (result.status !== 0) {
    fail(`preview harness update failed: node ${script} ${args.join(' ')}`);
  }
}

function recordInit(pageDir, targetUrl, previewUrl, workflowMode, sourceFiles) {
  const args = [
    '--page-dir', pageDir,
    '--init',
    '--mode', workflowMode,
    '--reason', 'route-or-bootstrap-redirect',
    '--preview-kind', 'isolated-vue-mount',
    '--bypasses-bootstrap', 'true',
    '--preview-url', previewUrl,
  ];
  if (targetUrl) args.push('--target-url', targetUrl);
  runHarness(args);

  // 记录进入隔离入口的源文件，供 runtime-health 与后续轮次判断「预览链是否被改动」。
  if (sourceFiles && sourceFiles.length) {
    const statePath = path.join(pageDir, 'preview-harness.json');
    const state = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    state.sourceFiles = Array.from(new Set(sourceFiles.map((file) => path.resolve(file))));
    fs.writeFileSync(statePath, `${JSON.stringify(state, null, 2)}\n`, 'utf8');
  }
}

/**
 * 目标 .vue 同目录下的源文件（含子目录）——隔离入口会 import 它们，
 * 因此它们的内容变化必须能使预览物料失效。
 */
function collectPreviewSourceFiles(targetFile) {
  const allowed = new Set(['.vue', '.js', '.jsx', '.ts', '.tsx', '.css', '.less', '.scss', '.sass', '.styl']);
  const files = [];
  const visit = (current) => {
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const absolute = path.join(current, entry.name);
      if (entry.isDirectory()) visit(absolute);
      else if (entry.isFile() && allowed.has(path.extname(entry.name).toLowerCase())) files.push(absolute);
    }
  };
  visit(path.dirname(targetFile));
  return files.sort();
}

function recordFile(pageDir, flag, file) {
  runHarness(['--page-dir', pageDir, flag, file]);
}

function deriveTemplate(sourceHtml, slug) {
  let html = sourceHtml || [
    '<!DOCTYPE html>',
    '<html>',
    '<head>',
    '  <meta charset="utf-8">',
    '  <meta name="viewport" content="width=device-width,initial-scale=1">',
    '  <title>d2c isolated preview</title>',
    '</head>',
    '<body>',
    '  <div id="app"></div>',
    '</body>',
    '</html>',
  ].join('\n');

  html = html
    .replace(/<!--\s*INJECT_START\s*-->/g, '')
    .replace(/<!--\s*INJECT_BODY_TOP\s*-->/g, '')
    .replace(/<!--\s*INJECT_BODY\s*-->/g, '');

  // 模板只登记构建期事实（data-d2c-template-*），不得预置运行时状态。
  // 运行时状态（data-d2c-preview-mode / -slug / -bootstrap-bypass）一律由
  // 下方 marker script 与入口模块在浏览器里写入；否则入口未执行时，
  // 模板属性会让 capture 探针误判成「预览已生效」。
  if (/<html\b[^>]*>/i.test(html)) {
    html = html.replace(/<html\b([^>]*)>/i, (_match, attrs) => {
      if (/data-d2c-template-preview-mode=/.test(attrs)) return `<html${attrs}>`;
      return `<html${attrs} data-d2c-template-preview-mode="isolated-vue-mount" data-d2c-template-preview-slug="${slug}">`;
    });
  } else {
    html = `<!DOCTYPE html>\n<html data-d2c-template-preview-mode="isolated-vue-mount" data-d2c-template-preview-slug="${slug}">\n${html}\n</html>`;
  }

  const markerScript = [
    '<script>',
    'window.__D2C_PREVIEW_MODE__ = "isolated-vue-mount";',
    'window.__D2C_PREVIEW_BOOTSTRAP_BYPASS__ = true;',
    'document.documentElement.setAttribute("data-d2c-preview-mode", "isolated-vue-mount");',
    'document.documentElement.setAttribute("data-d2c-preview-bootstrap-bypass", "true");',
    '</script>',
  ].join('');
  if (!html.includes('__D2C_PREVIEW_MODE__')) {
    if (/<\/head>/i.test(html)) {
      html = html.replace(/<\/head>/i, `${markerScript}\n</head>`);
    } else {
      html = `${markerScript}\n${html}`;
    }
  }

  if (!/id=["']app["']/.test(html)) {
    const appRoot = '<div id="app"></div>';
    if (/<\/body>/i.test(html)) {
      html = html.replace(/<\/body>/i, `${appRoot}\n</body>`);
    } else {
      html += `\n${appRoot}\n`;
    }
  }

  return html;
}

function entrySource(targetImport, slug) {
  const body = [
    'window.__D2C_PREVIEW_MODE__ = "isolated-vue-mount";',
    'window.__D2C_PREVIEW_BOOTSTRAP_BYPASS__ = true;',
    'document.documentElement.setAttribute("data-d2c-preview-mode", "isolated-vue-mount");',
    'document.documentElement.setAttribute("data-d2c-preview-bootstrap-bypass", "true");',
    `document.documentElement.setAttribute("data-d2c-preview-slug", ${jsString(slug)});`,
    '',
    'const noop = () => {};',
    'const store = window.$store || {',
    '  state: {},',
    '  getters: {},',
    '  commit: noop,',
    '  dispatch: () => Promise.resolve(),',
    '  subscribe: noop,',
    '  watch: noop,',
    '};',
    'const route = { path: window.location.pathname, query: {}, params: {}, name: "d2c-isolated-preview" };',
    'const router = window.$router || {',
    '  currentRoute: route,',
    '  history: { current: route },',
    '  init: noop,',
    '  push: noop,',
    '  replace: noop,',
    '  go: noop,',
    '  back: noop,',
    '  beforeEach: noop,',
    '  afterEach: noop,',
    '};',
    'window.$store = store;',
    'window.$router = router;',
    '',
    'function installPreviewGetter(key, value) {',
    '  const descriptor = Object.getOwnPropertyDescriptor(Vue.prototype, key);',
    '  if (descriptor && descriptor.configurable === false && !descriptor.set) return;',
    '  try {',
    '    Object.defineProperty(Vue.prototype, key, {',
    '      configurable: true,',
    '      get() { return value; },',
    '    });',
    '  } catch (error) {',
    '    try { Vue.prototype[key] = value; } catch (ignored) {}',
    '  }',
    '}',
    '',
    'installPreviewGetter("$store", store);',
    'installPreviewGetter("$router", router);',
    'installPreviewGetter("$route", route);',
    'Vue.config.productionTip = false;',
    '',
    '// 执行断言：只有真实执行到这里的入口才会写这两个标记。',
    '// 模板预置属性、静态快照、被拆到未加载 chunk 的入口都不会写。',
    'window.__D2C_PREVIEW_ENTRY_EXECUTED__ = false;',
    'try {',
    '  const app = new Vue({',
    '    store,',
    '    router,',
    '    beforeCreate() {',
    '      this._routerRoot = this;',
    '      this._router = router;',
    '      this._route = route;',
    '    },',
    '    render(h) {',
    '      return h(',
    '        "div",',
    '        {',
    '          attrs: {',
    '            "data-d2c-mark": "L0",',
    '            "data-d2c-component": "isolated-vue-preview",',
    '            "data-d2c-preview-mode": "isolated-vue-mount",',
    '          },',
    '        },',
    '        [h(TargetPage)]',
    '      );',
    '    },',
    '  }).$mount("#app");',
    '  window.__D2C_PREVIEW_APP__ = app;',
    '  // Vue 2 的 $mount("#app") 会用渲染根元素「替换」#app，之后 querySelector("#app") 恒为 null。',
    '  // 必须从 $mount 的返回值取渲染根，否则节点数永远是 0，',
    '  // 会被误读成「入口跑了但一个节点都没挂上」——与卡片其实渲染正常相反。',
    '  window.__D2C_PREVIEW_MOUNTED_NODES__ = app && app.$el',
    '    ? app.$el.querySelectorAll("*").length',
    '    : 0;',
    '  window.__D2C_PREVIEW_MOUNT_TAG__ = app && app.$el ? app.$el.tagName : null;',
    '  window.__D2C_PREVIEW_ENTRY_EXECUTED__ = true;',
    '  document.documentElement.setAttribute("data-d2c-preview-entry-executed", "true");',
    '} catch (error) {',
    '  window.__D2C_PREVIEW_ENTRY_ERROR__ = String((error && error.message) || error);',
    '  document.documentElement.setAttribute("data-d2c-preview-entry-error", window.__D2C_PREVIEW_ENTRY_ERROR__);',
    '  throw error;',
    '}',
    '',
  ];

  // 本模块可能被「主应用 HTML」一并注入：项目自身的 HtmlWebpackPlugin 未配 chunks 时，
  // 它会把所有 entry（含本预览入口）都注入 index.html。而本入口在顶层就会
  // 覆盖 Vue.prototype.$store/$router/$route 并抢占 #app —— 在主应用页面执行即打挂主应用，
  // 且健康闸报的是「真实项目 SPA 根节点未成功挂载」，矛头指向使用者的应用。
  // 因此先确认自己在预览页，否则不做任何副作用。
  // 判据用模板的构建期标记 data-d2c-template-preview-slug（只有预览模板带，主应用 index.html 不带）。
  return [
    "import Vue from 'vue';",
    `import TargetPage from ${jsString(targetImport)};`,
    '',
    '(function bootstrapD2cPreview() {',
    '  if (!document.documentElement.hasAttribute("data-d2c-template-preview-slug")) {',
    '    window.__D2C_PREVIEW_ENTRY_SKIPPED__ = "not-a-preview-page";',
    '    console.warn("[d2c] 隔离预览入口被非预览页面加载，已跳过全部副作用（未覆盖 Vue.prototype，未挂载）。");',
    '    return;',
    '  }',
    ...body.map((line) => (line ? `  ${line}` : line)),
    '})();',
    '',
  ].join('\n');
}

/**
 * 生成独立的 webpack 配置（不修改项目自身的构建配置）。
 *
 * 适用场景：项目用 Nuxt 等把 webpack 配置封在框架内部、无法就地打补丁的构建链。
 * 关键约束：
 *   - splitChunks / runtimeChunk 必须关闭 —— 入口代码集中在单文件里，
 *     否则入口执行断言可能落在未被 HTML 加载的子 chunk 上（无法归因）。
 *   - 可选 loader 缺失时跳过对应规则，而不是让整份配置 require 失败。
 */
function standaloneWebpackConfigSource(projectRoot, entryAbs, templateAbs, outputAbs, entryKey, slug) {
  return [
    `'use strict';`,
    `// D2C-PREVIEW-ISOLATED ${slug}: standalone`,
    "const path = require('path');",
    "const HtmlWebpackPlugin = require('html-webpack-plugin');",
    '',
    '// 可选 loader：项目没装就跳过对应规则，避免整份配置加载失败。',
    "function optional(name) {",
    '  try { return require.resolve(name) } catch (_) { return null }',
    '}',
    'const loaders = {',
    "  vue: optional('vue-loader'),",
    "  vuePlugin: optional('vue-loader/lib/plugin'),",
    "  ts: optional('ts-loader'),",
    "  style: optional('vue-style-loader'),",
    "  css: optional('css-loader'),",
    "  stylus: optional('stylus-loader'),",
    "  url: optional('url-loader'),",
    "  file: optional('file-loader'),",
    '};',
    'const VueLoaderPlugin = loaders.vuePlugin ? require(loaders.vuePlugin) : null;',
    '',
    'module.exports = {',
    "  mode: 'development',",
    `  context: ${jsString(projectRoot)},`,
    `  entry: { ${entryKey}: ${jsString(entryAbs)} },`,
    '  output: {',
    `    path: ${jsString(outputAbs)},`,
    "    filename: '[name].js',",
    "    publicPath: './',",
    '  },',
    "  devtool: 'source-map',",
    '  resolve: {',
    "    extensions: ['.js', '.ts', '.vue', '.json'],",
    `    alias: { vue$: 'vue/dist/vue.esm.js', '@': ${jsString(projectRoot)}, '~': ${jsString(projectRoot)} },`,
    '  },',
    '  module: {',
    '    rules: [',
    "      loaders.vue && { test: /\\.vue$/, loader: loaders.vue },",
    "      loaders.ts && { test: /\\.tsx?$/, loader: loaders.ts, options: { transpileOnly: true, appendTsSuffixTo: [/\\.vue$/] } },",
    "      loaders.css && { test: /\\.css$/, use: [loaders.style, { loader: loaders.css, options: { esModule: false } }].filter(Boolean) },",
    "      loaders.stylus && loaders.css && { test: /\\.styl(us)?$/, use: [loaders.style, { loader: loaders.css, options: { esModule: false } }, loaders.stylus].filter(Boolean) },",
    "      loaders.url && { test: /\\.(png|jpe?g|gif|svg|webp)$/i, loader: loaders.url, options: { limit: 8192, esModule: false } },",
    "      loaders.file && { test: /\\.(woff2?|eot|ttf|otf)$/i, loader: loaders.file, options: { esModule: false } },",
    '    ].filter(Boolean),',
    '  },',
    '  // 入口代码必须集中在单文件：入口执行断言不能被拆到未加载的子 chunk。',
    '  optimization: { splitChunks: false, runtimeChunk: false },',
    '  performance: { hints: false },',
    '  plugins: [',
    '    VueLoaderPlugin ? new VueLoaderPlugin() : null,',
    '    new HtmlWebpackPlugin({',
    "      title: 'd2c isolated preview',",
    `      template: ${jsString(templateAbs)},`,
    "      filename: 'index.html',",
    `      chunks: ['${entryKey}'],`,
    '      inject: true,',
    '    }),',
    '  ].filter(Boolean),',
    '};',
    '',
  ].join('\n');
}

function patchWebpackConfig(configPath, entryKey, entryRel, templateRel, outputFilename, slug) {
  let text = readText(configPath);
  const entryMarker = `D2C-PREVIEW-ISOLATED ${slug}: entry`;
  const htmlMarker = `D2C-PREVIEW-ISOLATED ${slug}: html`;

  if (!text.includes(entryMarker)) {
    const entryInsert = [
      `    // ${entryMarker} start`,
      `    ${entryKey}: './${entryRel}',`,
      `    // ${entryMarker} end`,
    ].join('\n') + '\n';
    const next = text.replace(/entry\s*:\s*{\s*\n/, (match) => `${match}${entryInsert}`);
    if (next === text) {
      fail(`Cannot find object-form webpack entry in ${configPath}. Pass a supported webpack base config.`);
    }
    text = next;
  }

  if (!text.includes(htmlMarker)) {
    if (!/HtmlWebpackPlugin/.test(text)) {
      fail(`Cannot find HtmlWebpackPlugin in ${configPath}; isolated Vue preview needs a webpack-managed HTML page.`);
    }
    const pluginInsert = [
      `    // ${htmlMarker} start`,
      '    new HtmlWebpackPlugin({',
      "      title: 'd2c isolated preview',",
      `      template: paths.resolveApp('${templateRel}'),`,
      `      filename: '${outputFilename}',`,
      `      chunks: ['${entryKey}'],`,
      '      inject: true,',
      '    }),',
      `    // ${htmlMarker} end`,
    ].join('\n') + '\n';

    const injectPluginIndex = text.indexOf('    new InjectHTMLPlugin');
    if (injectPluginIndex >= 0) {
      text = `${text.slice(0, injectPluginIndex)}${pluginInsert}${text.slice(injectPluginIndex)}`;
    } else {
      const next = text.replace(/plugins\s*:\s*\[\s*\n/, (match) => `${match}${pluginInsert}`);
      if (next === text) {
        fail(`Cannot find webpack plugins array in ${configPath}.`);
      }
      text = next;
    }
  }

  writeText(configPath, text);
}

function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error(usage());
    fail(error.message);
  }

  if (args.help || !args['page-dir'] || !args.target) {
    console.error(usage());
    process.exit(args.help ? 0 : 1);
  }

  let workflowMode;
  try {
    workflowMode = requirePreviewWorkflowMode(args.mode || args['workflow-mode'], 'd2c-isolated-vue-preview');
  } catch (error) {
    fail(error.message);
  }

  const projectRoot = process.cwd();
  const pageDir = path.resolve(projectRoot, args['page-dir']);
  const targetAbs = path.resolve(projectRoot, args.target);
  if (!fs.existsSync(targetAbs)) {
    fail(`Target Vue file does not exist: ${targetAbs}`);
  }

  const slug = sanitizeSlug(args.slug || path.basename(pageDir));
  const entryKey = `d2c_preview_${slug.replace(/-/g, '_')}`;
  const previewDir = path.resolve(projectRoot, 'client/d2c-preview');
  const entryAbs = path.join(previewDir, `${slug}-entry.js`);
  const templateAbs = path.join(previewDir, `${slug}-template.html`);
  const outputFilename = `d2c-preview/${slug}.html`;

  const explicitConfig = args['webpack-config']
    ? path.resolve(projectRoot, args['webpack-config'])
    : null;
  const webpackConfig = explicitConfig || findFirst(projectRoot, [
    'webpack/config/webpack.base.conf.js',
    'build/webpack.base.conf.js',
    'webpack.base.conf.js',
    'config/webpack.base.conf.js',
  ]);
  // Nuxt 把 webpack 配置封在框架内部，无法就地打补丁 → 走独立构建链。
  const nuxtConfig = !webpackConfig ? findFirst(projectRoot, ['nuxt.config.js']) : null;
  const buildConfig = webpackConfig || nuxtConfig;
  const configKind = nuxtConfig ? 'nuxt' : 'webpack';
  if (!buildConfig) {
    fail('Cannot auto-detect webpack base config or nuxt.config.js. Pass --webpack-config <path>.');
  }

  const standaloneConfigAbs = path.join(previewDir, `${slug}-webpack.config.cjs`);
  const runtimeOutputRel = `static/d2c-preview-runtime/${slug}`;
  const runtimeOutputAbs = path.resolve(projectRoot, runtimeOutputRel);
  const previewPath = configKind === 'nuxt' ? '/index.html' : `/${outputFilename}`;
  const baseUrl = args['base-url'] ? String(args['base-url']).replace(/\/+$/, '') : '';
  const previewUrl = baseUrl ? `${baseUrl}${previewPath}` : previewPath;

  const htmlTemplate = args['html-template']
    ? path.resolve(projectRoot, args['html-template'])
    : findFirst(projectRoot, [
      'client/public/index.html',
      'public/index.html',
      'index.html',
    ]);

  recordInit(
    pageDir,
    args['target-url'] || '',
    previewUrl,
    workflowMode,
    collectPreviewSourceFiles(targetAbs),
  );
  // webpack 就地打补丁 → 原配置被修改；Nuxt → 只新增文件，项目配置零改动。
  if (configKind === 'webpack') recordFile(pageDir, '--modified', buildConfig);
  recordFile(pageDir, '--created', entryAbs);
  recordFile(pageDir, '--created', templateAbs);
  if (configKind === 'nuxt') {
    recordFile(pageDir, '--created', standaloneConfigAbs);
    recordFile(pageDir, '--created', runtimeOutputAbs);
  }

  const targetImport = makeRelativeImport(entryAbs, targetAbs);
  writeText(entryAbs, entrySource(targetImport, slug));

  const sourceHtml = htmlTemplate ? readText(htmlTemplate) : '';
  writeText(templateAbs, deriveTemplate(sourceHtml, slug));

  const entryRel = posixPath(path.relative(projectRoot, entryAbs));
  const templateRel = posixPath(path.relative(projectRoot, templateAbs));
  if (configKind === 'nuxt') {
    writeText(
      standaloneConfigAbs,
      standaloneWebpackConfigSource(projectRoot, entryAbs, templateAbs, runtimeOutputAbs, entryKey, slug),
    );
  } else {
    patchWebpackConfig(buildConfig, entryKey, entryRel, templateRel, outputFilename, slug);
  }

  const buildScriptAbs = path.join(__dirname, 'd2c-build-isolated-preview.cjs');
  // 插件通常位于业务仓库之外：此时用 $HELIXHARNESS_PLUGIN 占位（与 rules 中的命令写法一致），
  // 避免把 ../.. 相对路径写进输出、换个工作目录就失效。
  const skillDir = path.dirname(__dirname);
  const buildScriptRef = skillDir.startsWith(projectRoot + path.sep)
    ? posixPath(path.relative(projectRoot, buildScriptAbs))
    : `$HELIXHARNESS_PLUGIN/skills/${path.basename(skillDir)}/scripts/d2c-build-isolated-preview.cjs`;
  const result = {
    ok: true,
    mode: 'isolated-vue-mount',
    workflowMode,
    previewPath,
    previewUrl,
    entry: posixPath(path.relative(projectRoot, entryAbs)),
    template: posixPath(path.relative(projectRoot, templateAbs)),
    buildConfigKind: configKind,
    buildConfig: posixPath(path.relative(projectRoot, buildConfig)),
    webpackConfig: posixPath(path.relative(
      projectRoot,
      configKind === 'nuxt' ? standaloneConfigAbs : buildConfig,
    )),
    runtimeOutput: configKind === 'nuxt' ? runtimeOutputRel : null,
    buildCommand: configKind === 'nuxt'
      ? `node ${buildScriptRef} --config ${posixPath(path.relative(projectRoot, standaloneConfigAbs))}`
      : null,
    requiresDevServerRestart: configKind === 'webpack',
    requiresPreviewBuild: configKind === 'nuxt',
    note: configKind === 'nuxt'
      ? 'Run buildCommand, serve the generated runtime directory at base-url, then capture previewUrl with --access-mode vue-route-preview.'
      : 'Capture this URL with d2c-capture-page.cjs --access-mode vue-route-preview after the dev server has loaded the patched webpack config.',
  };
  console.log(JSON.stringify(result, null, 2));
}

module.exports = { deriveTemplate, entrySource, standaloneWebpackConfigSource, collectPreviewSourceFiles }

if (require.main === module) main();
