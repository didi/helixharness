# d-d2c 初始化（d-d2c-init）

## 目标
- 进入任何 d-d2c 页面开发前，先完成初始化盘点与映射基线。
- 初始化未完成时，禁止进入页面代码开发。

## 前置环境
- 需要可解析到这些依赖：`pngjs`、`jpeg-js`、`puppeteer-core`（可由 `NODE_PATH` 提供，或安装在仓库根 `devDependencies` 下；优先复用已有解析，不强制装进业务 repo）
- 完整自检评测流程强依赖本地 Agent 环境已安装 `d2c-fidelity-evaluator` skill
- 本机需安装 Chromium 内核浏览器（Chrome / Edge / Chromium），检测逻辑见下方
- 本地项目先启动
- 所有脚本在仓库根目录执行

## 阶段 0：依赖自检与自动安装
- 进入 d-d2c 初始化时，先检查本插件捆绑的 `skills/d2c-fidelity-evaluator/SKILL.md` 是否可读。若缺失，视为硬阻断，禁止继续进入页面开发、自检物料生成或评测阶段。

完整读取插件根目录下 `skills/d2c-fidelity-evaluator/SKILL.md`。该技能随插件分发；资源不存在则说明安装不完整，修复安装后继续。

- 再检查 d-d2c 截图与图片处理脚本依赖（`pngjs` / `jpeg-js` / `puppeteer-core`）是否可被 Node 解析（`require.resolve` 会搜 `NODE_PATH`）。可解析则跳过安装；不可解析才按需安装：

```bash
node -e "['pngjs','jpeg-js','puppeteer-core'].forEach(m=>require.resolve(m))" 2>/dev/null \
  || npm install -D pngjs jpeg-js puppeteer-core
```

- 含义：
  - 若三个脚本依赖已可被 Node 解析（含 `NODE_PATH` 提供的位置），则不安装、不写入业务 repo；
  - 仅当无法解析时，才按需 `npm install -D` 补齐到仓库根 `devDependencies`（兜底）。

## 阶段 1：初始化（硬阻断 - 必须完成全部三份文档）

> ⛔️ **硬阻断规则**：以下三份文档在 **`$D2C_ROOT`** 下全部存在且非空之前，禁止执行 MasterGo DSL 拉取、禁止编辑**业务页面目录**下任何文件、禁止运行截图脚本。任何跳过此阶段的行为都视为不合规。（`$D2C_ROOT` 未导出时按默认 `d-d2c` 理解。）

### 1.1 必须产出的文档清单

执行顺序与产出物（缺一不可；路径中 `d-d2c/` 表示 **`$D2C_ROOT`/**，默认即仓库根下 `d-d2c/`）：

| 序号 | 产出文件                       | 依据                                          |
| ---- | ------------------------------ | --------------------------------------------- |
| ①    | `d-d2c/sort-of-d2c.md`           | `d-d2c/rules/d-d2c-mapping-code-sort.md`          |
| ②    | `d-d2c/assets-mapping.md`        | `d-d2c/sort-of-d2c.md` 第 7.1 节候选项          |
| ③    | `d-d2c/components-mapping.md`    | `d-d2c/sort-of-d2c.md` 第 7.2 节候选项          |

### 1.2 强制执行步骤

1. **先校验**：每次任务开始时对三份映射文件执行存在性与非空校验（任选其一即可）：
   - `ls d-d2c/sort-of-d2c.md d-d2c/assets-mapping.md d-d2c/components-mapping.md 2>&1`
   - 或先 `export D2C_ROOT=d-d2c`（若自定义则改为实际目录），再 `ls "$D2C_ROOT/sort-of-d2c.md" "$D2C_ROOT/assets-mapping.md" "$D2C_ROOT/components-mapping.md" 2>&1`
   - 若三份文件**全部存在且非空**（>0 字节）：跳过 1.3 直接进入下一阶段。
   - 否则进入 1.3 补齐。
2. **不允许**仅勾选任务状态而不实际写文件；不允许"已知项目结构无需重做"等理由跳过。

### 1.3 生成步骤（严格串行，每步完成后立即 ls 验证）

```text
（可选）在 shell 中：export D2C_ROOT=d-d2c   # 自定义目录时改为实际路径；未设置则下文 R 一律按 d-d2c 理解
设 R=${D2C_ROOT:-d-d2c}
Step A: 按 $R/rules/d-d2c-mapping-code-sort.md 全部 7 节模板，扫描当前仓库，Write 落盘 $R/sort-of-d2c.md → ls -la 验证文件大小 > 0
Step B: 基于 sort-of-d2c.md 第 7.1 节，Write 落盘 $R/assets-mapping.md → ls -la 验证
Step C: 基于 sort-of-d2c.md 第 7.2 节，Write 落盘 $R/components-mapping.md → ls -la 验证
Step D: 三份齐全后，方可进入 d-d2c-generate.md 阶段（生成规则以**当前仓库内已安装的 d-d2c 技能** `rules/` 目录为准，与 `R` 所指业务映射目录无关）
```

#### --scope 参数（可选）

当只需要处理仓库中某个子目录的组件时，使用 `--scope <业务目录路径>` 限定本次扫描范围。该值是一个**目录路径**（不是 DSL layoutMark）——同名参数在 geometry / vertical / render 等检查脚本中含义不同，见 SKILL.md「易混参数」。

```
d-d2c --init --scope client/views/<your-module>
```

设置后：
- `sort-of-d2c.md` 只包含 scope 目录内的 .vue 文件
- `assets-mapping.md` 只包含 scope 目录内的资源
- `components-mapping.md` 同理

### 1.4 阻断点自检

进入下一阶段前，必须在对话中显式打印一次三份文档在 **`$D2C_ROOT`**（或默认 `d-d2c/`）下的 `ls -la` 结果作为放行证据。未打印即推进，视为违反 SOP。

## 另见
- [Brownfield 接入指南](d-d2c-brownfield.md) — 存量项目初始化建议
