# codex 生命周期适配

插件分发清单显式加载 `hosts/codex/hooks.json`，入口 `hosts/codex/hook_entry.py`
固定绑定本宿主 manifest；不依赖另一宿主环境变量或根目录默认 hooks。

- SessionStart 注入当前安装的 Host Context，并按已有账本修复可选遥测运行时。
- SessionEnd 只执行当前宿主的授权关闭清理，不代表业务阶段完成。
- 不注册 Stop 或 Failure 业务钩子。阶段开始、完成与失败由公共命令显式记账。
- 项目 hooks 由 `project-hooks.json` 独立声明。
- 统计关闭不改变工作流完成门禁。
