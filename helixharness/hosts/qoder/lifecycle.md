# qoder 生命周期适配

插件 hook 的源文件是 `hosts/qoder/hooks.json`；打包脚本把它原样复制到 ZIP 根的
`hooks/hooks.json`，因为 Qoder 只按插件根约定目录装载 hook，不解析插件自定义路径。
两处内容必须逐字节一致，不得手工维护第二份。

- SessionStart 注入当前安装的 Host Context。本宿主 `telemetry=unsupported`，因此不修复遥测
  运行时、不读写 consent 与账本。
- 不注册 SessionEnd、Stop 或 Failure 业务钩子：没有授权态需要收尾，阶段开始、完成与失败全部由
  公共命令显式记账，会话结束不代表阶段完成。
- 不注册 PreToolUse 守卫。Host Context 已在 SessionStart 绑定并在会话内不可变，命令与 Skill
  复用同一绑定；额外的 Skill 前置注入只会重复同一份上下文。
- 项目 hooks 由 `project-hooks.json` 独立声明，合并目标 `.qoder/settings.json`。该路径是否被
  宿主在真实会话中装载尚未验证，Init 后必须单独检查，重复 Init 应保留用户条目且不增加重复命令。
- 统计关闭不改变工作流完成门禁。
