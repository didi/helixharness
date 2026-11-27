# UT 分类与工件发布协议

本资源由公共 `commands/ut.md` 与 `agents/unit-test-worker.md` 共同消费，不替代扫描、6 Loop、
真实 worker 终态、重试、源外构建或主项目统一验证。首次 UT 工件写入前必须完整读取。

## 原子发布入口

UT 的 queue、session、results、summary、experience 和 worker result 的**每次创建/更新**，
包括空表初始化、失败结果、最终完成状态，都使用绑定插件的 `scripts/ut_artifacts.py`：

1. 用当前宿主 Write 将完整内容写入目标同目录的 `<name>.tmp`（不是目标文件）。
2. 在原项目目录执行（示例 name 可替换为允许的 UT 状态名）：

```bash
{helix_python} "{plugin_root}/scripts/ut_artifacts.py" --repo-root "{repo_root}" --feature "{feature}" write --name unittest-session.md --source "{repo_root}/docs/specs/active/{feature}/unittest-session.md.tmp"
```

脚本只接受 UT 状态白名单、当前 feature 目录和同名 `.tmp` 源；拒绝越界/符号链接，
用独立同目录临时文件、flush/fsync、原子 replace 发布，再清除输入 staging。失败时旧目标
保持完整、输入 staging 保留。替换已有普通文件保留其 POSIX mode，新文件默认 0644；不承诺 ACL/xattr 等其他元数据。成功输出 `atomic:true`；失败不得假称已写入。

不得直接 Write/Edit 最终状态，也不得用 `cat > target`、`echo > target`、`tee target` 或 Python
直接覆盖目标来绕过入口。rename 归档已有 worker 工件不属于内容发布，可保持原协议执行。
`worker-*-defects.md`、`worker-*-findings.json` 和 `unittest-defects.md` 由以下分类入口生成，
不接受普通 write 子命令，也不由模型手写覆盖。业务测试文件仍按原 Loop 4 同步。

## 分类依据与计数

每条观察必须区分：

- `confirmed_defect`：违反已确认要求，或有可复现的机械失败，且有实际反例证据。
- `unconfirmed_risk`：风险依赖尚未确认的产品期望、边界或没有足够证据。
- `optional_improvement`：兼容的 API/诊断/风格/文档改进建议。

只有第一类计入缺陷总数与 High/Medium/Low。风险和建议分别统计、完整保留，不混入缺陷，
不能因“发现缺陷数为 0”隐藏观察。没有相关需求时，不自行发明 Unicode 空白处理、舍入方式、
null 必须抛 NPE、错误消息粒度或 Javadoc 要求。现有代码仅证明当前行为，不自动成为规范性
来源；测试按某个猜测失败也不能把猜测升级成已确认要求。

### Worker 结构化记录

每个 worker 在最后写 result 前，准备自己的 `worker-{ID}-findings.json.tmp`：

```json
{
  "worker_id": 1,
  "class_name": "example.SomeClass",
  "observations": [
    {
      "id": "O-01",
      "classification": "optional_improvement",
      "title": "可进一步细化诊断信息",
      "actual_behavior": "非法入参已正确拒绝，但共用一条异常消息"
    }
  ]
}
```

无观察使用 `observations:[]`，不省略文件。ID 在当前 worker 内唯一。已证实缺陷还必须提供：

- `severity`：`High|Medium|Low`；`actual_behavior` 和具体影响应说明真实错误。
- `basis=contract`：`normative_ref={path,quote}`，path 是当前 repo README/AGENTS 或 docs 下
  Markdown 的规范性来源，quote 必须逐字存在；明确指出它如何约束当前方法，不能引用实现正文
  充当未确认需求，也不能引用本协议作为具体产品规则。
- 或 `basis=mechanical`：用于无法作为合法预期解释的机械失败，给出 `code_ref={path,quote}`
  与 `mechanical_kind`：`inevitable_exception|nontermination|data_corruption|control_flow_failure|security_violation|misleading_success`。
  异常类还需 `observed_exception`（例如 ClassCastException、ZeroDivisionError 或 Go panic），必须逐字出现在反例日志；不使用异常名称白名单。
  源码引用支持 Java/Kotlin/Groovy、Python、JS/TS（含 JSX/TSX/MJS/CJS）、Go，不接受配置文件充当源码依据。不能把正常的参数拒绝、异常类型偏好、舍入政策或风格归为机械失败。
- `reproducer={command,expected,actual,evidence_path,evidence_quote}`：真实执行过的反例，
  expected/actual 必须不同；日志路径在本 repo 或本 worker 的系统临时目录内，quote 逐字存在。
  不得构造/改写日志来证明猜测。该 command 只作记录，脚本不会执行它。

```bash
{helix_python} "{plugin_root}/scripts/ut_artifacts.py" --repo-root "{repo_root}" --feature "{feature}" findings --worker {ID} --source "{repo_root}/docs/specs/active/{feature}/worker-{ID}-findings.json.tmp"
```

脚本校验当前 worker 身份、引用和反例结构，原子生成规范化 JSON 与 `worker-{ID}-defects.md`。
缺少支持的 confirmed 提议自动降为 risk，保留原提议、proposed_severity 和原因；不得以原提议继续计数、提前高危退出
或禁用测试。worker result 的缺陷/风险/建议数以脚本返回的 counts 为准，随后最后原子发布 result。
对已证实 High 保持原高危退出协议，仍先输出 findings 和分类报告，再发布失败 result。

脚本校验的是来源与证据存在性，不是对所有业务语义的机械证明。父流程仍须对照实际合同复核；
不能仅凭 JSON 通过就把不合理的推理当成事实。

## 父流程汇总

先等待原生终态并校验 result、6 Loop、测试/覆盖率和写域。按原重试去重规则选择每个类的最终
Worker ID，然后执行（优先在清理临时证据前）（下面 ID 为示例）：

```bash
{helix_python} "{plugin_root}/scripts/ut_artifacts.py" --repo-root "{repo_root}" --feature "{feature}" aggregate --workers 1,2
```

只聚合显式选择的最终 workers，支持当前或 archived-worker 文件，单个 ID 只选同一组；存在当前工件时不混用旧归档，归档中断须先按恢复协议还原整组。新发布的 schema_version=2 记录保存发布时已校验的分类与完整性摘要；聚合检查身份、结构、counts 和摘要，不因临时日志已清理而重新降级。摘要仅检查完整性，不是签名或业务正确性证明；这些是发布时的观察，不能冒充当前源码的重新验收。损坏/非法 JSON 仍阻断。
旧会话只有 result/defects，或 schema_version=1 尚未重新核验时，保留原件和状态并输出 legacy_workers；分类总数 `counts` 为 null、`classification_complete=false`，`known_counts` 只含已核验部分。超时代写 result 同样处理，不能编造空 findings 或把未知算零；缺少 result 且无有效新分类仍阻断。正常新 worker 仍必须先发布 findings 再 result。
命令原子生成 `unittest-defects.md`；results/summary/最终回复数字须一致，分类不全时显示“未核验”，列出原报告，不宣称全量零缺陷或分类验收通过。
不得重新把风险/建议数称为缺陷数。其余状态和结果报告通过 write 子命令发布。
归档 worker 文件时包含 `.md` 与 `-findings.json`，保留 ID/扩展名与重试关联；共享经验写入所有权
仍服从宿主 adapter，worker 不得借此写共享汇总或经验文件。
