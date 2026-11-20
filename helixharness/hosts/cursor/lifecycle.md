# Cursor 生命周期适配

分发清单显式引用 `hosts/cursor/hooks.json`，宿主入口为 `hosts/cursor/hook_entry.py`。
它只接收本宿主原生事件，绑定当前 `cursor` Host Kit，不借用另一宿主配置或转录格式。

- `sessionStart` 注入当前安装 Host Context，输出使用 Cursor 的 `additional_context`。
  Hook 启动不构成业务授权；薄 Skill 仍显式执行当前安装的 runtime preflight 与 Host Context 解析。
- `beforeSubmitPrompt` 在单个 `continue=true` 响应中尽力附加本次原生身份。该兼容字段需按客户端
  版本和界面原生验证，不能仅凭 stdout 宣称模型可见。身份只做纯字段校验，不访问文件系统、
  Git、授权或 HostRegistry；最多 64 个 roots、上下文最多 8 KiB，超限或预算耗尽只放行。
  统计消费端仍规范化路径并验证真实仓库归属；采集在响应 flush 后继续使用同一截止时间。
- `sessionStart` 在启动上下文完整 flush 后，尽力派发一次现有本地清理任务；不等待完成。
  派发失败不抑制原有 lifecycle repair。它只使用本次原生 workspace roots，worker 解析到的 Git
  仓库才参与；不递归发现非 Git workspace 下的子仓，不访问未打开的其他仓库。
- `sessionEnd` 可能缺失或在退出时无法启动命令，不能承诺关窗、关聊天或归档必然清理。
  主动关闭授权仍立即清理当前仓库；遗漏的数据可由下次同仓 sessionStart 补扫。若仓库不再打开，
  不保证补扫发生；未知/损坏且无法证明归属的数据会保留。两次补扫均需重新确认授权仍为关闭。
- `sessionEnd` 不是阶段完成。不注册业务 `stop`/failure 自动续跑，不读取会话文本或伪造 token。
  它只启动一次性本地清理任务，按仓库去重并重新持锁确认当前 Cursor 授权为关闭；清理期间
  保持授权锁，重新开启后不会被旧任务误删。会话退出不等待清理完成；锁不可用或任务失败时
  本次清理可跳过，不创建持久重试队列。只删除明确归属本仓 Cursor 的数据，保留归属不明的
  异常文件；不拆除其他宿主可能仍使用的全局统计调度器，也不启动采集或发送。
- `telemetry=emulated`：统一授权开启后记录阶段账本；有有效上报地址时，原生
  `beforeSubmitPrompt`、`afterAgentResponse`、`postToolUse`、`postToolUseFailure`、
  `subagentStart`、`subagentStop` 与观察型 `stop`
  为当前活跃阶段提供私有事件 journal。它经过同一精确窗口、DLP、Archive 封口与 Outbox 投递。
  不读取 `transcript_path`、Cursor 私有数据库、Thinking、工具原始输入输出或附件。
  子代理仅保留原生 ID、类型、终态和时长，不保留 task/summary；按 subagent_id 去重，
  不用其 generation_id 冒充父 turn，也不把 Task 完成回调再算作一次派发。
- `stop` 只提供实际存在的 turn usage 快照，不返回 `followup_message`、不重试、不调用业务
  finish，因此 `stop_hook=unsupported` 仍表示没有业务停止门禁。
- 项目 hooks 由 `project-hooks.json` 定义，必须保留用户已有 hook 并幂等合并。
- 官方 Cloud Agents 缺少桌面 session 生命周期，Cloud Agents 不在当前支持范围；CLI 需独立验收。

SessionStart 输出真实 `conversation_id` 和原生 `workspace_roots`，分别绑定
`HELIXHARNESS_CURSOR_CONVERSATION_ID`、`HELIXHARNESS_CURSOR_WORKSPACE_ROOTS`（JSON 数组）。
官方仅保证 hook 的 env 对后续 hooks 可用，因此每次 HelixHarness Shell 调用都须显式传入这两项；
身份或项目归属缺失时统计受控跳过，不借用其他宿主环境或同仓库最近会话。

宿主 Hook 超时保持 3 秒。同步采集使用整次调用共享的 1.5 秒预算，授权锁、Git 查询、
journal 锁与多仓遍历消费同一截止时间。允许响应及启动上下文立即刷新到输出流；预算耗尽
时跳过本次可选采集，不无锁读授权、不沿用旧 epoch。竞争可能导致事件缺失，统计只表示实际
观察范围；该预算约束已知等待路径，不承诺任意文件系统或系统级卡死都能被强制中断。

授权缓存位于 `${HELIXHARNESS_CURSOR_HOME:-~/.cursor}/helixharness/telemetry-consent-cache.json`，
隔离覆写为 HelixHarness 自有变量。journal 位于当前仓库 `.helixharness/telemetry-v2/cursor-events/`，
按会话与授权 epoch 隔离、权限为私有；未授权或地址不可用时不采集事件正文。关闭授权清理私有
源，最终投递后无 Entry 再引用的自有 journal 会删除，其他宿主的日志和已提交账本不删除。

Cursor 3.15.6 的 `afterAgentResponse` 与 `stop` 可带同一 generation 的 token 总量快照。
按真实 conversation/generation 去重，`input_tokens` 已包含 cache，total 仅为 input + output。
只计入起止完整落在一个阶段窗口内的 generation，范围明确为 `parent_only`；跨阶段、缺字段、
客户端不提供或无法归属时保持 `partial/unavailable`，不填零或估算。初始 prompt 早于 start、
最终 response 晚于 finish 的常见顺序会使该 turn token 不可归属，基础阶段与工具事件仍可上报。
工具与交互统计表示实际观察到的 hook 事件，不宣称完整客户端会话覆盖。单 hook 输入上限 4 MiB、
单私有 journal 上限 128 MiB；超限事件不落盘，不用零值补齐。缺失 hook、生命周期竞态和多仓
未登记的子仓也可能减少可观察事件；多仓无 cwd 的回调仅访问原生 workspace 内已登记且具有
同一活跃 conversation 的子仓。有 cwd 时只记录该仓；没有 cwd 且多个子仓同时活跃时不复制
正文或数值，只记录 `ambiguous_workspace` 缺口，补充统计标为 partial/unavailable。
不扫描其他仓库或猜测最近会话。

Cursor 可导入 `.claude/settings.local.json`、`.claude/settings.json`、`~/.claude/settings.json`
中的 hooks，前提是第三方导入启用。所有匹配 hook 来源可能同时运行，应检测重复安装来源；同一仓库
存在 `.claude-plugin` 文件本身不等于已经双重安装。不得为消除重复而修改用户的全局第三方导入偏好。

参考：[Cursor Hooks](https://cursor.com/docs/hooks)、
[第三方 Hooks](https://cursor.com/docs/reference/third-party-hooks)。
