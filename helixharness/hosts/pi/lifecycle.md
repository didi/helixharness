# Pi 生命周期适配

pi 包清单声明 `hosts/pi/extension.ts`，该扩展在 `session_start` 与 `session_shutdown` 调用
`hosts/pi/hook_entry.py`，入口固定绑定本宿主 manifest；不依赖另一宿主环境变量。

- `session_start` 注入当前安装的 Host Context，并按已有账本修复可选遥测运行时。
- `session_shutdown` 只执行当前宿主的授权关闭清理，不代表业务阶段完成。
- 不注册 Stop 或 Failure 业务钩子。阶段开始、完成与失败由公共命令显式记账。
- 本宿主 `project_hooks=unsupported`：init 不写入任何 pi 设置文件。
- 统计关闭不改变工作流完成门禁。
