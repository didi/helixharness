# 阶段统计协议

读取 Host Context 的 `resources.telemetry_protocol`。授权由 `scripts/telemetry_consent.py resolve/choose` 管理；未授权时统计关闭。不得替用户确认。

当前阶段使用 `scripts/metrics.py` 的 start、trace-event 和 finish。授权见 `reference/eval-protocol.md`；完整字段、Archive 封口、异步补充与脱敏约束见 `reference/eval-supplement-protocol.md`，宿主会话采集由 Host Kit 处理。先执行 start；返回 `status=started` 时保存真实 `entry_id`，然后开始业务。返回 `status=skipped`、宿主不支持或统计异常时，按关闭或 unavailable 继续业务，不要求 entry_id、不伪造 entry；finish 只写真实终态与证据，跳过统计时不声称已记账。等待用户不是终态。

`propose --loop` 为每个阶段分别记录 entry，trigger 为 `loop`，不创建编排层 entry。业务错误不因统计成功而变成成功。上报仅在授权且 `scripts/reporting_config.py` 的发布默认值或 Runtime 私有 `config.json` 的覆盖地址有效时进行；未配置或网络失败不能阻塞正常业务，也不能声称已经送达。

未配置异步地址是可选通路未启用，正常命令、授权选择结果、阶段收口和最终摘要都静默处理，不展示“地址仍未配置，未异步上报”等提示，不主动提醒配置，也不宣称已经送达。内部保留 `endpoint_unconfigured` 诊断；仅在用户主动询问上报状态或排查配置时解释。授权说明中的数据用途与 README 配置文档保留，地址无效或实际投递失败仍按错误规则处理。
