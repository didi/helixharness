# Pi 分发清单

pi 包根是仓库根目录的 `package.json`。该清单用 `pi.extensions` 装载 `helixharness/hosts/pi/extension.ts`，
用 `pi.skills` 装载 `helixharness/hosts/pi/skills/`（9 个公开命令入口）与 `helixharness/skills/`
（可复用方法 Skill）。

安装、更新与验收步骤见 `hosts/pi/installer.md`。pi 没有 marketplace 概念，因此本宿主不声明
marketplace 清单；正式来源就是当前包的配置来源。
