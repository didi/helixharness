---
user-invocable: false
name: code-review
description: 为 HelixHarness Apply 评审固定比较点之后的代码变化；并行执行 Standards 与 Spec 两个独立评审轴，分轴报告结果，并按 HelixHarness 合同完成修复和复审。
---

# HelixHarness Code Review

评审 `<fixed-point>` 与 `HEAD` 之间的代码变化，并保持两个独立评审轴：

- **Standards**：代码是否遵守仓库已经记录的工程规范？
- **Spec**：代码是否忠实实现当前需求 Spec？

两个轴使用并行、上下文隔离的只读 sub-agent，最后并排展示结果，不合并或跨轴重排 finding。

## 流程

### 1. 固定比较点和评审范围

仅评审当前未提交增量时，自动使用当前 `HEAD` 作为 `<fixed-point>`，不询问用户。只有需要纳入 `HEAD` 之前的已提交改动，或任务变化只存在于提交历史中时，才请用户指定 commit、branch 或 tag。将输入解析为完整 commit ID，不根据分支名称、时间或语义猜测其他起点。

执行并确认比较点有效、已提交差异非空或存在待评审的 working-tree delta：

```bash
git rev-parse <fixed-point>
git diff <fixed-point>...HEAD
git log <fixed-point>..HEAD --oneline
```

再执行 `workflow.py ... review-scope --feature "{feature}"`，取得 `changed_paths`、`excluded_preexisting_paths` 和 `review_scope_sha256`。只从 `changed_paths` 构造 staged、unstaged 与本任务新增 untracked 文件的 working-tree delta；`excluded_preexisting_paths` 只记录排除事实，不附带内容。执行前工作区基线缺失、范围重叠或脚本拒绝当前状态时，直接返回脚本给出的 `DECISION_NEEDED` 或 blocker，不自行重建范围。

冻结同一份 Review Bundle，供两个 Reviewer 使用：

- 当前预绑定 Host Context：准确的 `plugin_root`、`canonical_host_id` 与当前 Host Kit 路径；
- 完整 fixed-point 和 Review 时的 `HEAD` commit ID；
- committed diff、commit list 和 working-tree delta；
- `review_scope_sha256`、`changed_paths` 与 `excluded_preexisting_paths`；
- 当前 HelixHarness `spec.md`；
- Standards 来源文件列表。

Review Bundle 合计没有代码变化时默认停止。唯一例外是经 Workspace 合同验证的只读 RepoJob：

- 父 Coordinator 提供的当前 `workspace-plan --command apply` 必须包含本 job，且 `mode=read`、
  repo key、ownership_root 和 Single workflow 与当前仓完全一致；不能仅凭聊天声明或 Spec 中的
  “只读”文字启用例外。缺少受控 Workspace root / RepoJob 时停止，不扫描目录猜测来源。
- 同一 Workspace 的 `workspace-validate --stage apply` 必须通过，其 `spec_receipt_sha256` 与
  本仓 Spec 的 `workspace_contract_sha256` 一致；把实际命令输出、原始 RepoJob 与父概要设计附入 Bundle。
- 本次真实的只读检查命令、结果及业务文件相对 Apply 基线未变的证据必须附入 Bundle。
  `changed_paths=[]` 不能替代契约检查，也不能用旧报告或其他仓的成功声称本仓已验证。

满足这些条件时仍执行下面的两个独立原生评审，不豁免或预填 APPROVE：Standards 轴核查写域、
业务文件不变性及验证是否符合仓库规范；Spec 轴逐条核查本仓只读契约验收是否有真实证据。
两轴明确记录这是 Workspace `read` job 的契约验收、没有业务代码改动，不虚构 diff 或行为实现。
普通 Single Apply 和 `mode=write` RepoJob 的空代码变化仍停止。所有原有收据、Review Scope
及 Archive 门禁保持有效。评审期间不写工作区；`review-scope` 生成的 `review-state.json`
留给 Archive 复核范围。

### 2. 定位 Spec

Spec 固定为当前 feature 的 `docs/specs/active/{feature}/spec.md`。文件缺失、不可读或与当前 HelixHarness feature 不一致时停止，不使用聊天摘要代替。

#### 评审时的 Evidence 边界

Apply 在双轴 Review 完成后生成本轮 `verify-report.md`。恢复 Apply 或执行 Bug Fix 时，已有报告可能仍是上轮 Evidence，只供诊断；不得要求它提前包含尚未完成的本轮评审结论，也不得仅因旧报告尚未刷新就阻断当前代码 Review。旧报告不能作为本轮已验证或已获 APPROVE 的证明；本轮实际缺失的测试、错误行为或虚构证据仍须报告。

涉及收据一致性时，由父流程把当前 `"{plugin_root}/scripts/workflow.py" validate-tasks` 返回的 Spec/Tasks 规范化收据及对应阶段校验结果附入 Bundle；Reviewer 需要复核时只调用同一准确脚本路径。不得用 `sha256sum`、`shasum` 或自行对整份 Markdown 求哈希替代：文件字节摘要与合同收据不是同一口径，状态推进本身不代表合同变更。向两个 Reviewer 的 prompt 都传递这一边界。Host Context 或校验结果缺失时向父流程报告缺失输入，不扫描用户目录、临时目录或 plugin cache 寻找另一个脚本/版本。

本轮 Evidence 的生成、修订与当前收据绑定仍由 Apply 在 Review 后完成，并通过 `archive-preflight`；不得复用无效的旧 APPROVE、修改收据算法或跳过最终证据门禁。

### 3. 定位 Standards

使用 HelixHarness Apply 已确定的仓库入口、当前 Spec 命中的 `docs/harness/` 叶子、`docs/hooks/cr-checklist.md`（若存在）及其他已加载工程规范。仓库明确规范优先于下面的代码异味基线；跳过工具已经确定性检查的内容。

Standards 轴始终附带 Fowler 代码异味基线。每一项都只是需要结合上下文判断的启发式建议，不是仓库硬规则：

- **Mysterious Name**：名称不能揭示用途时重命名；无法诚实命名通常说明设计仍含混。
- **Duplicated Code**：相同逻辑形状出现在多个 hunk 或文件时，提取共享实现。
- **Feature Envy**：逻辑主要操作另一对象的数据时，把行为移到数据所有者附近。
- **Data Clumps**：同一组字段或参数反复同行时，形成一个明确类型。
- **Primitive Obsession**：原始字符串或数值承载领域概念时，使用小型领域类型。
- **Repeated Switches**：相同类型分支在多处重复时，收敛为一个映射或多态实现。
- **Shotgun Surgery**：一个逻辑变化要求分散修改许多位置时，收拢共同变化点。
- **Divergent Change**：一个模块因多个无关原因变化时，按职责拆分。
- **Speculative Generality**：当前 Spec 不需要的抽象、参数或 hook 应删除或内联。
- **Message Chains**：调用者依赖长导航链时，把导航隐藏在稳定接口后。
- **Middle Man**：只做转发的中间层应删除，让调用者直接访问真实能力。
- **Refused Bequest**：子类忽略大部分继承合同时，改用组合。

#### 测试代码分流

测试代码仍属于 `changed_paths` 和完整 Review Bundle；不得排除测试文件，也不得只审生产代码。Standards Reviewer 先区分生产代码与测试代码，再对测试代码执行下面的测试代码噪音过滤：

- 优先检查测试有效性：错误或缺失的断言、会造成假通过的 mock/fixture、绕过真实行为的测试机制、flaky 风险、共享可变状态、执行顺序依赖，以及仓库明确测试规范违反。证据充分且无需用户决策时，finding disposition 和 Standards 轴 verdict 都必须是 `MUST_FIX`；需要新的产品行为、测试策略或不可逆决策时才使用 `DECISION_NEEDED`。这类 correctness 或硬规范问题不得输出 `ADVISORY`、不得降级成纯风格建议。
- 只有存在具体、显著维护风险时才生成测试代码 `ADVISORY`，例如大段复杂 fixture 在多个测试文件复制且已经可能漂移、测试严重绑定私有实现导致正常重构大面积破坏，或测试数据构造复杂到掩盖被验证行为。finding 必须写出具体风险，不能只说“可以抽取”。
- 普通测试字面量、输入值与预期值分别写出的相同值、单个测试类内的简短重复，以及少量 Given/When/Then setup 不生成 `ADVISORY`；不得仅以 **Primitive Obsession** 或 **Duplicated Code** 名义报告这些内容。
- 为保持 test oracle 独立，不得因为消除重复而共享同一个常量，也不得建议让预期断言引用输入 fixture 或被测生产代码的常量。没有测试有效性、硬规范或显著维护风险时，完全省略该 test-only finding。

### 4. 并行执行两个评审轴

`Standards Reviewer` 与 `Spec Reviewer` 是写入两个 sub-agent prompt 的评审角色，不是固定的 agent type。必须使用当前运行环境原生可用的 sub-agent 能力并行创建两个独立实例；不得把 Reviewer 角色名臆造为专用 agent type。不得仅因某个专用 type 不存在就判定并行独立评审不可用或返回 `BLOCKED:lite_review_parallel_unavailable`。

同时启动两个上下文隔离、只读的 sub-agent，并向它们提供同一份冻结 Review Bundle。不得由 Implementer 角色扮演 Reviewer，也不得顺序复用同一上下文。运行环境无法完成并行独立评审时返回 `BLOCKED:lite_review_parallel_unavailable`。

向 Standards sub-agent 提供完整代码变化、commit list、Standards 来源和代码异味基线，要求它按文件或 hunk 报告：

1. 违反仓库明确规范的位置，并引用规范文件和规则；
2. 发现的代码异味，注明它是 judgement call 并引用代码 hunk；
3. 仓库规范与异味基线冲突时，以仓库规范为准；
4. 先区分生产代码与测试代码，对测试代码执行上述测试代码噪音过滤；
5. 跳过工具已经执行的检查。

向 Spec sub-agent 提供相同代码变化、commit list 和完整 Spec，要求它报告：

1. Spec 要求但缺失或只部分实现的行为；
2. Spec 未要求的行为或 scope creep；
3. 看似实现但实现机制错误的要求；
4. Spec 的 Testing Decisions 声明 `视觉还原验收: 是` 时检查 d2c 证据链是否兑现：hard violation = 0、叠图存在、豁免记录真实；声明了但缺证据按 MUST_FIX 报告，未声明的项目不审视觉还原。

每条 finding 都引用具体代码证据；Spec finding 同时引用对应 Spec 内容。两个报告都保持简洁，中文控制在约 800 字以内。

### 5. 分轴汇总

分别在 `Standards` 与 `Spec` 标题下原样或轻度清理两个报告。不得合并或跨轴重排 finding，也不得生成跨轴统一严重度。

最后输出一行分轴摘要：`Standards: <finding 数与最严重项>; Spec: <finding 数与最严重项>`。不得选出跨轴单一赢家。

### 6. HelixHarness 结果流转

finding disposition 只有 `ADVISORY`、`MUST_FIX`、`DECISION_NEEDED`；轴 verdict 只有 `APPROVE`、`MUST_FIX`、`DECISION_NEEDED`。`ADVISORY` 是 finding disposition，不是轴 verdict；它表示有具体证据和可选改进动作，但不影响当前 Spec 正确交付。Fowler 代码异味继续作为 Standards 报告中的非阻断 judgement call；Spec 轴的非阻断 Out of Scope 说明在确有可选改进时也归为 Advisory。Standards 使用稳定 ID `STD-A01...`，Spec 使用 `SPEC-A01...`，每条都给出证据、不阻断原因和可选动作。只含 Advisory 的轴仍为 `APPROVE`。`BLOCKED` 只表示 Apply 无法继续执行，不是 Reviewer verdict。

- `MUST_FIX`：Implementer 定向修复，运行覆盖测试和仓库完整测试套件，再以同一 fixed point 冻结新 Review Bundle 并重新执行完整双轴 Review。修复可能引入新问题，不得只复审原 finding；不设置固定 Review 轮次上限，并累计 `must_fix_rounds`。
- `BLOCKED:lite_review_no_progress`：同一阻断 finding 以相同轴、规则或 Spec 要求、代码路径和失败机制重复出现，且最新代码和验证没有改善时停止。不得只因轮次较多而阻断。
- `DECISION_NEEDED`：停止并交回 HelixHarness Apply，不在旧 Review Bundle 上继续。
- 两轴都没有 Must Fix 或 Decision Needed 时整体 `APPROVE`。

Reviewer 不得把 correctness、Spec 明确要求、当前任务引入的实际 scope creep、安全问题或仓库硬规范违反降级为 Advisory；这些必须按证据归为 `MUST_FIX` 或 `DECISION_NEEDED`。

输出 fixed-point、Review HEAD、`review_scope_sha256`、两个分轴报告、稳定 Advisory ID 和 verdict，交回 HelixHarness Apply 生成 Evidence。Reviewer 不修改代码、不生成 Evidence、不询问用户，也不自行启动修复循环。

## 为什么分成两个轴

一个变化可能通过其中一个轴，却在另一个轴失败：

- 完全遵守工程规范，但实现了错误需求：Standards 通过，Spec 失败。
- 完整实现需求，但违反仓库规范：Spec 通过，Standards 失败。

分开报告可以避免一个轴的问题掩盖另一个轴。
