# qoder 生命周期适配

插件分发清单（`.qoder-plugin/plugin.json`）显式加载 `hosts/qoder/hooks.json`，入口
`hosts/qoder/hook_entry.py` 固定绑定本宿主 manifest；不依赖另一宿主环境变量或根目录默认 hooks。

- `SessionStart` 注入当前安装的 Host Context。
- `SessionEnd` 只执行当前宿主的授权关闭清理，不代表业务阶段完成。
- 不注册 `Stop` 或 `PostToolUseFailure` 业务钩子。阶段开始、完成与失败由公共命令显式记账。
- 项目 hooks 由 `project-hooks.json` 独立声明，owner 为 `parent`。
- 统计能力为 `emulated`：SessionStart 会写入 `.helixharness/host-session/qoder.json` 供
  telemetry adapter 定位本次会话；阶段账本、异步采集与投递按统一 consent 合同执行，
  `SessionEnd` 的统计清理仅在授权关闭时触发。
- 命令资源绑定通过薄 Skill 的固定路径换算完成（见 `skill-entry.md`），不依赖 hook 注入。
