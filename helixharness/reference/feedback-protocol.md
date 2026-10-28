# 显式反馈协议

客户端与平台使用 `helixharness-feedback/1.0` envelope，`document` 遵循同目录的 `feedback-schema-v1.json`；平台 schema 必须与客户端一致。反馈与 eval-log 及统计 consent 完全独立。

## 配置与数据

公开版默认没有平台地址。未配置目的地时仅保留反馈入口，`prepare` 不采集、不保存草稿、不发送；普通社区问题可直接提交到 GitHub Issues。

- 发布方在 `scripts/reporting_config.py` 配置 `DEFAULT_FEEDBACK_ENDPOINT`（当前为空）；用户私有 `config.json` 的非空 `endpoint` 优先覆盖该默认值。默认目录为 `~/.helixharness/feedback`，可通过 `HELIXHARNESS_FEEDBACK_HOME` 修改。地址环境变量不再参与解析。缺少文件、字段或值为空时使用发布默认值；文件损坏、字段类型错误或非空无效地址不回退，与 report 共用同一解析器。
- 接收端为用户已启动的 helixharness-platform `/feedback/report`。本机示例为 `http://127.0.0.1:7860/feedback/report`；地址不能含凭据、查询参数或 fragment。
- 反馈包含分类、正文、插件/宿主版本、匿名项目/会话标识，以及用户选择关联的 feature 和最近 entry ID。用户名留空，不读取需求地址。
- 正文经过通用 DLP；可选当前 Claude 会话摘录排除工具原始输出、thinking 和宿主注入内容，处理代码块、密钥、联系方式和本地路径。仅使用 Host Adapter 给出的当前会话路径，不搜索其他会话；超出大小限制时不附带。
- `prepare` 在私有草稿目录以 0600 权限保存脱敏 payload，不联网、不写业务仓库。返回的 SHA-256 绑定完整 payload 和接收地址；`send` 要求匹配的摘要和明确确认。任何修改都需重新审核。

## 送达

POST 的 JSON 为 `{"items": [item]}`，支持 gzip。每条 item 包含 schema、feedback_id、document，及可选 transcript_md/transcript_stats。服务端逐条返回 feedback_id 与 accepted / duplicate / rejected / retryable；同 ID 同内容幂等，不同内容拒绝覆盖。没有匹配反馈编号的成功确认，不算送达。

连接失败或临时错误进入私有 outbox；只有相同接收地址的已确认记录可以补投。待发记录保留 30 天，下次向同地址发送或显式 retry 时处理；不因配置变化而改投其他服务。永久拒绝保留草稿供检查，不自动修改反馈重发。

平台通过 `HELIXHARNESS_PLATFORM_FEEDBACK_DB_PATH` 配置 SQLite 存储，默认 `eval/.cache/feedback.db`；`/feedback` 提供列表、分类筛选、分页和详情。模板按文本转义反馈与会话摘录，不执行其中的 HTML 或指令。
