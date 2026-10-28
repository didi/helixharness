# HelixHarness

面向 Codex 与 Claude Code 的 HelixHarness 研发工作流。通过项目约束、知识库、Spec、任务和验证证据完成研发。

## 命令

- `/helix:init`：初始化当前项目；`--workspace` 初始化多仓目录。
- `/helix:propose <name> <requirement-or-path>`：收敛需求并生成 Spec/Tasks。
- `/helix:propose <name> <requirement-or-path> --loop`：自动串联 Propose → Apply → Archive；同名 `propose <name> --loop` 续跑。
- `/helix:apply <name>`：逐项实现、测试和代码评审。
- `/helix:archive <name>`：核验收据、更新知识并归档。
- `/helix:bug-fix <name> <description>`：修订尚未归档的单仓需求。
- `/helix:ut`：扫描后选择包与并发，生成和维护单测；也支持显式参数。
- `/helix:update`：检查并更新当前宿主已配置的插件源。

## 安装

需要 Python 3.10+、Git，以及 Codex 或 Claude Code。将本目录作为本地 marketplace 添加。宿主具体命令见 `hosts/codex/installer.md` 或 `hosts/claude-code/installer.md`。

## 项目结构

`docs/harness/` 保存约束，`docs/knowledge/` 保存当前知识，`docs/specs/active/<name>/` 保存 Spec/Tasks/verify-report，归档后移至 completed。Workspace 根通过 `helixharness.workspace.yaml` 登记子仓，根 Spec 协调各仓完整 HelixHarness 生命周期。

## 可选配置

统计默认关闭，授权询问独立于异步地址。report 与 feedback 都由发布方在 `scripts/reporting_config.py` 设置默认地址，当前默认留空；用户可分别在 `~/.helixharness/telemetry/config.json` 与 `~/.helixharness/feedback/config.json` 中设置 `endpoint` 覆盖。用户文件优先，缺少或为空时使用发布默认值；无效配置不回退。地址环境变量不再生效。配置示例见 [README](README.md#可选配置)。未配置异步地址时静默跳过，授权后的同步账本仍正常记录；未授权时不新增同步账本，也不异步采集或投递。


前端保留设计解析、还原、预览和视觉验收能力。可用用户提供的设计素材；设计服务认证由用户配置。

## 验证

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p 'test_*.py'
python3 scripts/host_runtime_cli.py list
```

此发行版仅维护当前 HelixHarness 合同，不迁移其他工作流历史。循环不包含发布或部署。
