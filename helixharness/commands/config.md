---
description: "查看或修改 HelixHarness 用户配置：统计授权、作用范围、统计与反馈上报地址。"
---
> HelixHarness 公共命令唯一契约：本文件承载 config 的全部业务步骤。所有宿主通过当前 `resources.command_projection` 加载本文件，使用预绑定 Host Context；完整读取 `reference/workflow-projection-protocol.md`、`reference/host-runtime-protocol.md`、当前 `resources.runtime` 和 `reference/eval-protocol.md`。

> 在任何 Python 调用前执行 `reference/runtime-preflight.md`。本命令不推进需求、不写 Stage、不刷新插件版本、不发送反馈或统计测试请求。

## User Input

```text
$ARGUMENTS
```

## 参数

| 用法                                                                                 | 行为                                                     |
| ------------------------------------------------------------------------------------ | -------------------------------------------------------- |
| `/helix:config`                                                              | 只读查看统计有效开关、作用范围、统计与反馈地址及配置来源 |
| `/helix:config --telemetry enabled --scope project`                          | 开启当前项目统计                                         |
| `/helix:config --telemetry disabled --scope project`                         | 关闭当前项目统计                                         |
| `/helix:config --telemetry enabled --scope user`                             | 开启当前宿主的本人统计；已有项目级设置仍参与优先级判断   |
| `/helix:config --telemetry disabled --scope user`                            | 关闭当前宿主的本人统计，优先于项目开启设置               |
| `/helix:config --report-endpoint https://platform.example/report`            | 更新本机统计上报地址                                     |
| `/helix:config --feedback-endpoint https://platform.example/feedback/report` | 更新本机反馈地址                                         |
| `/helix:config --reset-report-endpoint`                                      | 移除统计地址覆盖，恢复发布默认值                         |
| `/helix:config --reset-feedback-endpoint`                                    | 移除反馈地址覆盖，恢复发布默认值                         |

当前配置范围只有以上设置；模型、密钥、业务项目配置、平台服务部署不属于本入口。按 shell token 精确解析，URL 保留原文，不裁剪标点；未知参数、混合多项修改、`--telemetry` 与 `--scope` 不成对时先说明用法，不做部分写入。用户以自然语言提出多项设置时，先整理明确值和范围，再逐项执行并分别报告结果；失败时停止，不声称全部成功。

## 查看

```bash
python3 "{plugin_root}/scripts/user_config.py" --repo-root "{repo_root}" --platform "{canonical_host_id}"
```

`canonical_host_id` 来自预绑定 Host Context，不猜测平台或授权文件位置。脚本使用纯读取授权 API，默认查看不清理私有数据、不改提醒时间、不启动 Runtime。展示有效值、作用范围、配置来源与文件位置；无配置或损坏都如实说明，不伪造默认开启。不要打印完整配置 JSON、授权缓存、项目指纹或凭据。

## 修改

1. 用户已给出明确修改命令或明确自然语言目标、值和范围时，这就是该项修改的授权，不再重复要求确认。参数不完整时，先查看当前值，只询问缺失的必要信息。默认查看、推荐项和未提交的选择不构成授权。
2. 开启统计之前逐字展示 `reference/eval-protocol.md` 中 `helixharness-eval-data-notice-v1` 的数据说明。用户已明确要求开启时展示说明后执行；仅表达查看/配置意图时展示说明并由用户选择，不能自动开启。
3. 使用同一个 `user_config.py`，追加一项实际修改参数及内部 `--user-confirmed` 标记。该标记仅在已有明确请求或选择后由 Agent 传入。授权缓存始终通过捆绑 `telemetry_consent.py` 的 API 写入，不手改 JSON。项目开启被用户级关闭覆盖时展示 warning 和实际有效范围，不自动扩大授权。
4. 地址配置按当前 `HELIXHARNESS_TELEMETRY_HOME` / `HELIXHARNESS_FEEDBACK_HOME` 或默认本机目录定位，脚本只合并目标 `endpoint` 字段并原子写入，保留其他字段。非法地址或损坏 JSON 拒绝写入并保留原文件；不自行删除重建。URL 不允许凭据、查询参数和 fragment。
5. 根据返回的 `change.updated`、warning 和重新读取的有效值报告。`updated=false` 不能称修改成功。操作后显示实际作用范围和配置来源。

地址设置不代表统计授权；恢复默认地址不是关闭统计，发布默认值可能仍有接收方。停止统计必须显式关闭授权。开启本人范围仅影响当前 Host Kit 的授权缓存，不擅自改另一宿主。关闭后不删除历史账本或平台数据；未发送私有证据由既有授权协议清理。

配置检查只验证格式和实际生效值，不代表服务在线、统计已送达或反馈已发送。地址变更不重定向旧地址待发数据；不调用发送、补投、安装或后台 Worker 来“验证配置”。

宿主资源声明：`resources.runtime`。
