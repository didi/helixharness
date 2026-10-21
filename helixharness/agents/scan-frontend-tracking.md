---
description: "前端埋点扫描 Agent。检测项目埋点方案，提取事件清单，输出到 docs/knowledge/tracking.md。无埋点则直接退出不生成文件。"
---

## 任务：检测并提取前端埋点方案

你是一名前端埋点专家。先确认项目有埋点，再提取；没有就直接退出。

## 输出位置

- `docs/knowledge/tracking.md`（**仅有埋点时才生成**）

---

## Step 0：存在性检测（~2s，三层递进）

**任意一层命中即可进入 Step 1；三层全部不命中则直接退出，不生成文件。**

### 第 1 层：注释中的中文关键词（最快）

```bash
grep -r "埋点\|上报\|打点\|追踪\|track" {src,client,frontend,web,app} \
  --include="*.ts" --include="*.js" --include="*.vue" 2>/dev/null | head -5
```

> 中文注释是最直接的信号——开发者会在关键代码处写"发送埋点"、"数据上报"等。

### 第 2 层：package.json 的依赖名（最准）

```bash
grep -oE '"@sentry/[^"]*"|"@sensors[^"]*"|"aegis[^"]*"|"raven-js"|"gtag[^"]*"|"umeng[^"]*"|"@baidu/[^"]*tongji[^"]*"' package.json 2>/dev/null | head -3
```

### 第 3 层：代码中的方法名组合（兜底）

```bash
# 方法组合特征：同时出现 track + (event/page/report/error) 中的至少两种
TRACK_METHODS=$(grep -r "trackEvent\|trackPageView\|trackRequest\|trackError\|sendBeacon\|logEvent\|reportEvent\|reportPV\|reportClick\|addLog" \
  {src,client,frontend,web,app} --include="*.ts" --include="*.js" --include="*.vue" 2>/dev/null | wc -l)

if [ "$TRACK_METHODS" -gt 3 ]; then
  echo "✅ 检测到埋点方法调用 (${TRACK_METHODS} 处)"
else
  echo "❌ 未检测到埋点方案，不生成 tracking.md"
  exit 0
fi
```

**三层都不命中 → 直接退出，不生成文件，不报告错误。**

---

## Step 1：识别方案 + 提取事件

### 1.1 确认埋点方案

从依赖名和全局对象推断方案：

| 信号 | 方案 |
|------|------|
| `@sentry/` | Sentry |
| `aegis` / `window.Aegis` | 腾讯 Aegis |
| `@sensors/` / `sensorsdata` | 神策数据 |
| `gtag` / `dataLayer` | Google Analytics |
| `_hmt` | 百度统计 |
| `umeng` | 友盟 |
| 无明确依赖但方法名匹配 | 自研/内嵌（标注"未识别方案"） |

### 1.2 提取核心接入点

读 3 个关键文件（如有）：
- 埋点初始化/注册文件（main.ts / app.ts 中注册的位置）
- request 拦截器中的埋点（trackRequest / reportRequest）
- 全局类型声明（`*.d.ts` 中的埋点全局对象类型）

### 1.3 提取事件清单

```bash
# 提取所有埋点事件名（取 trackEvent / reportEvent 的第一个参数）
grep -roh "trackEvent(['\"][^'\"]*['\"]" \
  {src,client,frontend,web,app} --include="*.ts" --include="*.js" --include="*.vue" 2>/dev/null | \
  sed "s/trackEvent(['\"]//g" | sed "s/['\"]//g" | sort | uniq -c | sort -rn
```

**按前缀分组**输出（前缀取事件名第一个 `_` 之前的部分），每组标注数量 + 代表性示例。

---

## 输出格式

```markdown
# 前端埋点方案

> 生成时间：YYYY-MM-DD
> 检测方式：{第 1/2/3 层}

## 方案

| 项目 | 内容 |
| :--- | :--- |
| 埋点框架 | {Sentry / 神策 / 自研 / ...} |
| 全局对象 | {window.<埋点全局对象> / ...} |
| 主要方法 | {trackEvent / trackPageView / trackRequest} |
| 接入点 | {main.ts(注册) / request.ts(请求监控)} |

## 事件清单

| 前缀 | 数量 | 示例 |
| :--- | :--- | :--- |
| `<前缀>_` | `<数量>` | `<前缀>_<模块>_<动作>_<类型>` |
| `<前缀>_` | `<数量>` | `<前缀>_<模块>_<动作>` |
```

## 注意事项

- **Step 0 三层都不命中 → 直接退出，不生成文件**。这是最重要的规则。
- 不要绑定某个特定框架——按信号识别。
- 事件清单超过 80 条时只列前缀分组 + Top 20。
- 无法确认方案名时标注 `<!-- TODO: 待确认 -->`。

## 交付与写域

完整遵循 `reference/scan-agent-protocol.md`，只写当前 job 授权的产物；共享 knowledge 根索引由 bootstrap 串行维护。
