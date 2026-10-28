# HelixHarness Platform

当前版本：**1.0.0**。

FastAPI + Jinja2 + HTMX 单服务，包含使用数据看板、可信评测与证据驱动的自进化。`web_ui` 组合独立的 `eval`、`bench`、`evolution` 模块；各模块不互相导入、不跨模块查库。

## 安装和 Demo

以下命令在 `helixharness-platform/` 目录执行。

需要 Python 3.10+、Git。真实 Codex 评测还需要可用的 Codex 运行环境和模型凭据。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[server,dev]'
python -m web_ui.demo --port 7860
```

打开 http://127.0.0.1:7860。Demo 从 `bench/sample_data/task-filter/` 和 `eval/demo.py` 生成合成 Case、Mock Run 与项目账本，不随包分发数据库。默认写入临时运行目录，也可通过 `--data-dir /path/to/demo-runtime` 固定保存位置。Bench、遥测、反馈和自进化使用各自的数据库。页面明确标注 Demo；分数不代表真实模型能力。Demo 不开启后台自进化或模型分析。

Demo 首次启动会将随包提供的能力配置复制到运行目录的 `evolution/capabilities.yaml`，页面保存只修改该副本；复用同一运行目录时保留已保存的配置。Demo 不使用 `HELIXHARNESS_PLATFORM_EVOLUTION_CATALOG_PATH` 指向的外部配置文件。

## 接入自己的环境

配置项见 `.env.example`；服务从进程环境读取变量，不会自动加载该文件。可以通过部署工具注入环境变量。凭据留在部署环境，不写入版本库。

平台环境变量统一使用 `HELIXHARNESS_PLATFORM_` 前缀。

1. 看板的本地项目根用 `HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS`（逗号分隔）；也可在 `/config` 配置 Git 数据源。只接收当前 HelixHarness Ledger 与异步 Stage Supplement。
2. `HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_HELIXHARNESS_REPOSITORY` 指向源码 Git 仓库或其内部目录。平台按各自冻结提交自动定位插件，支持独立、组合及多层嵌套布局；存在多个副本时，通过 `HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_PLUGIN_PATH` 指定相对 Git 根目录的插件路径（`.` 表示根目录）。选中的路径随任务冻结。基准与候选通过明确 Git ref 冻结，评测命令使用当前 HelixHarness 命令；支持 `propose --loop`。
3. Codex 的 API 地址、Key 和模型通过 `HELIXHARNESS_PLATFORM_EVALUATION_CODEX_*` 配置。各角色模型及资源预算在 `/framework-evaluation/config` 保存。缺少模型配置时不得启动真实评测。
4. 看板 AI 分析直接调用 OpenAI 兼容的 Chat Completions API；配置 `HELIXHARNESS_PLATFORM_USAGE_AI_BASE_URL`、`HELIXHARNESS_PLATFORM_USAGE_AI_API_KEY`、`HELIXHARNESS_PLATFORM_USAGE_AI_MODEL` 并显式启用。评测 Judge 与自进化仍遵循其独立证据和门禁。
5. 自进化默认关闭。`evolution/catalog/capabilities.yaml` 是空目录模板；先定义自己的能力、受保护路径，准备 Published + Calibrated Case，再按需启用调度。平台不会自动发布 Candidate。

`HELIXHARNESS_PLATFORM_USAGE_SYNC_CONFIG` 可指定仓库同步与每日 AI 解读配置文件；`HELIXHARNESS_PLATFORM_USAGE_SYNC_CACHE` 可指定同步缓存目录。默认分别位于 `eval/configs/usage_repos.yaml` 和 `eval/.cache/`。配置示例见 [`eval/configs/usage_repos.example.yaml`](eval/configs/usage_repos.example.yaml)。每日解读沿用上述模型配置。

候选生成和评测统一使用 `HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_REFERENCE_REF` 配置的基准引用。普通候选生成前解析当前基准，修复候选继续使用来源 Run 中冻结的提交；候选审阅后才进入评测，不自动发布。

在第一个终端启动 Web 并保持运行：

```bash
python -m web_ui.main --port 7860 --usage-data-source auto
```

在另一个终端进入同一平台目录，激活相同虚拟环境并注入相同模型、数据库和源码配置，再启动 Worker 池：

```bash
export HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_REPOSITORIES="YOUR_REPOSITORY_ID=/path/to/business-repository"
python -m bench.worker_pool --adapter CODEX --poll-seconds 5
```

将 `YOUR_REPOSITORY_ID` 换成 Case 中的仓库 ID，路径指向该业务仓库；多个映射用逗号分隔。此映射用于执行 Case，不能用看板的 `HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS` 替代。

常驻池默认启动正式评测 2 个、自进化评测 2 个 Worker；在评测配置页面修改两个队列的单任务执行侧并发，等待任务结束后重启池生效。Web 与池必须共享配置文件、数据库、仓库映射及模型环境。池自动重启退出的 Worker，池本身应由系统进程管理器守护。设置 `HELIXHARNESS_PLATFORM_EVALUATION_TUTORIAL_VIDEO_PATH` 可接入本机 MP4/WebM 教学视频，视频文件不随公开包分发。详细步骤见[使用指南](../docs/user-guide.zh-CN.md)。

`auto` 在没有 Git 数据源及本地项目配置时，会生成带提示的看板合成示例；示例明细和统计投影单独存放，不进入真实统计或自动进化证据。已有 Git 配置但未同步时仍显示空数据。`sync` 只读取同步缓存。`web_ui.main --usage-data-source demo` 只控制看板来源；完整的 Case/Mock Run 演示请使用 `web_ui.demo`。详细区别见[启动方式](../docs/user-guide.zh-CN.md#1-启动非-demo-平台并读取业务仓库)。

看板 AI 支持只用环境变量配置，不要求存在 YAML 文件；结果从平台同步缓存目录读取，业务仓库可以位于其他位置。页面 `/config` 提供生成按钮，API 地址、Key 和模型仍由部署配置持有。普通用户下的 Candidate 生成使用 Codex 沙箱，root 部署默认要求 `nobody`/`setpriv` 隔离；可用 `HELIXHARNESS_PLATFORM_EVOLUTION_EXECUTION_USER` 指定专用用户。

HTTP API 的异步上报入口是 `/report`，由 HelixHarness 发布方在 `scripts/reporting_config.py` 中设置 `DEFAULT_REPORT_ENDPOINT`，或由用户在 `~/.helixharness/telemetry/config.json` 的 `endpoint` 中填写覆盖地址，并由用户授权后才开始统计。请按部署环境配置访问控制和网络入口。

数据库初始化只创建当前表结构，不迁移历史版本。对新的部署使用空数据库或 Demo 副本。运行目录、模型输出、Case 工作区、日志和凭据不属于发布源码。

## 验证

```bash
python -m pytest
python -m ruff check web_ui eval bench evolution tests
```

测试使用合成数据并 Mock 模型/外部执行器。正式模型调用、真实业务 Case 校准和部署需要使用者配置后执行。

## 问题反馈

feedback 辅助 Skill 可提交 HelixHarness 自身的使用问题，独立于研发命令和统计授权。**需先启动 helixharness-platform，并配置接收地址**：

在平台终端启动服务，并保持运行：

```bash
helixharness-server --port 7860
```

report 与 feedback 使用同一配置方式：发布方在 HelixHarness 的 `scripts/reporting_config.py` 中填写 `DEFAULT_REPORT_ENDPOINT` / `DEFAULT_FEEDBACK_ENDPOINT`；两个默认值当前为空，后续需要时再填写并发布。用户可通过本机配置文件覆盖默认值，例如 `~/.helixharness/feedback/config.json`：

```json
{
  "endpoint": "http://127.0.0.1:7860/feedback/report"
}
```

用户文件的非空 `endpoint` 优先；缺少文件、字段或值为空时使用发布默认值。配置损坏或非空地址无效时不回退。两条通路独立配置，地址环境变量不再生效；远程平台使用实际可访问地址。完整说明见[中文使用指南](../docs/user-guide.zh-CN.md#2-配置异步上报与-feedback-地址)。

在编码助手中要求使用 HelixHarness feedback Skill，先查看准备好的反馈内容和地址，确认后发送；可选附带脱敏后的当前会话摘录。平台 `/feedback` 页面可查看已接收反馈。连接失败时明确显示“待发送”，保存在本地私有队列，下次向同地址发送时补投，最长保留 30 天。不会写入业务仓库或自动开启统计。平台存储路径由 `HELIXHARNESS_PLATFORM_FEEDBACK_DB_PATH` 配置。

## 开源与贡献

项目使用 [Apache-2.0](LICENSE)。完整配置见[使用指南](../docs/user-guide.zh-CN.md)，贡献规则见[贡献指南](../CONTRIBUTING.md)。[安全政策](../SECURITY.md)和[社区行为准则](../CODE_OF_CONDUCT.md)在仓库根目录统一维护。普通社区问题在公开发布后通过 [GitHub Issues](https://github.com/didi/helixharness/issues) 提交。反馈 skill 默认没有平台地址，未配置时保留入口但不能发送。
