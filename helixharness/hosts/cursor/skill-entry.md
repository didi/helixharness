# Cursor Skill Entry Protocol

当前 Skill 的实际文件位于 `hosts/cursor/skills/{name}/SKILL.md`。以该文件向上四级为唯一
`plugin_root`，所有资源都固定在这一安装内，不搜索全局同名 Skill 或其他版本缓存。

## 公共绑定（command 与 method 都执行）

1. 完整读取 `reference/runtime-preflight.md` 并执行其中的 shell preflight；SessionStart 给出的
   `require && ... resolve ...` 已先执行同一检查，只有 `ok=true` 才会继续来源解析。Python 不可用时
   遵循该协议恢复；不绕过 preflight 直接执行 Python，不以 `check` 的退出码 0 代替 JSON 就绪判定。
2. 执行 `sh "{plugin_root}/scripts/runtime_preflight.sh" exec "{plugin_root}/scripts/host_runtime_cli.py" resolve --platform cursor --expected-plugin-root "{plugin_root}"`。
   仅接受 `status=resolved`、`host_id=cursor` 与当前安装一致的回执。资源缺失或来源不符时停止，
   不回退到另一宿主。不依赖 session hook 环境变量传播到终端。
3. 完整读取 `reference/host-runtime-protocol.md`、`reference/tool-semantics.md` 与 `resources.runtime`。
   后续每次 HelixHarness Python 调用按 runtime 显式绑定当前原生会话的
   `HELIXHARNESS_CURSOR_CONVERSATION_ID` 和 `HELIXHARNESS_CURSOR_WORKSPACE_ROOTS`；
   不假设 hook 环境会自动传到 Shell，不使用其他会话身份补缺。
4. 只读解析本次实际 Skill 路径：

   ```text
   sh "{plugin_root}/scripts/runtime_preflight.sh" exec "{plugin_root}/scripts/cursor_skill_projection.py" --root "{plugin_root}" --resolve-entry "{当前SKILL.md绝对路径}"
   ```

   回执 `entry_kind` 必须与薄入口声明的类型一致，`entry`、`canonical` 必须绑定当前安装。
   `command` 仅进入下方“命令入口”；`method` 仅进入“方法入口”。未知类型、失败或路径不符时停止。

以上 `exec` 与预检使用同一解释器解析规则；后续插件 Python 调用继续使用该启动器或预检回执中
已验证的解释器参数。不能在预检成功后改回 PATH 中未验证的 `python3` 或 `python`。

## 命令入口（仅 entry_kind=command）

1. 完整读取 `reference/workflow-projection-protocol.md`。入口解析器已核验
   `resources.command_projection` 的对应 command 条目；只使用回执中的唯一 `commands/{command}.md`。
2. 将用户本次调用附带的参数文本作为公共命令的 `$ARGUMENTS`，保留原始标点、引号、换行和
   `--` 后字面参数；这是一项逻辑参数绑定，不假定 Cursor 会展开 `$ARGUMENTS`。
   同一条用户消息中 Skill 名后的所有文本都是本次输入，包括首行参数后的换行需求正文；
   不能只取 `/helix-propose name --loop` 首行而丢弃后续描述。第一次调用 Router 前先绑定完整输入，
   不先用名称/开关试探路由，再补正文重跑；未能取得完整输入时先澄清，不猜测或截断。
   按公共命令要求安全传参，禁止 `eval` 或拼接未经引用的用户文本。确实未提供参数时才使用空串。
3. 完整读取回执中的 canonical 命令并执行；按需读取它声明的宿主资源。
   Router 返回唯一 workflow 后只执行该正文，不在投影层重复路由、拆解需求或补写业务步骤。

本次 Cursor 的公开入口统一使用 `/helix-init`、`/helix-propose` 等 `/helix-{command}` Skill 名。
`/helix:*` 是 canonical 文档的逻辑名称，由 `runtime.md` 映射为 Cursor 的连字符入口；
用户可见的命令指引与下一步建议使用映射后的写法。本次不提供原生冒号别名或冒号前缀补全。
参数按上述协议显式绑定，不依赖宿主自动展开。

## 方法入口（仅 entry_kind=method）

1. 方法解析独立于九个 command：不查询 `command_projection` 条目，不要求
   `commands/{method}.md` 存在，也不调用 Router 或命令入口分支。
2. 完整读取回执中的唯一 `skills/{method}/SKILL.md`，只执行其正文，忽略 Claude 专属 frontmatter。
   原样保留方法本次输入；正文中的相对资源以该 canonical 方法目录为基准。
   正文中的 `HELIXHARNESS_PLUGIN` 必须绑定本次已验证的 `plugin_root`，每次 Shell 调用遵循
   `resources.runtime` 的独立绑定与完整路径安全引用规则，不继承旧变量或搜索其他安装。
3. 按方法需要读取公共子代理协议、`resources.subagent_protocol` 及其它已声明资源，保留实际
   步骤和门禁。D2C 的显式 checkpoint 门禁按 `resources.runtime` 执行。

参考：[Cursor Skills](https://cursor.com/docs/skills)。
