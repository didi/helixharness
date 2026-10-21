# claude-code 生命周期适配

插件分发清单显式加载 `hosts/claude-code/hooks.json`，入口 `hosts/claude-code/hook_entry.py`
固定绑定本宿主 manifest；不依赖另一宿主环境变量或根目录默认 hooks。

- SessionStart 注入当前安装的 Host Context，并按已有账本修复可选遥测运行时。
- SessionEnd 只执行当前宿主的授权关闭清理，不代表业务阶段完成。
- 插件生命周期不注册全局 Stop 或 Failure 业务钩子。阶段开始、完成与失败由公共命令显式记账。
- 工作流调用 d-d2c 时必须通过原生 Skill 工具激活，不以 Read 正文替代原生加载。d-d2c 的原生技能元数据只在技能激活期间注册 PreToolUse/Stop guard，命令调用本 Host Kit 的 `d2c_hook_entry.cjs`。入口按自身位置解析当前安装的共享 guard，不扫描项目或用户技能副本。Codex 薄入口不消费这些 Claude frontmatter；业务正文和 guard 判定保持共享。
- 项目 hooks 由 `project-hooks.json` 独立声明。
- 统计关闭不改变工作流完成门禁。
- CwdChanged 由宿主适配器更新 CLAUDE_ENV_FILE 会话字段并触发可选运行时修复。
