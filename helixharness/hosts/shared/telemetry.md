# HelixHarness telemetry

授权使用 `scripts/telemetry_consent.py resolve/choose`，未授权时关闭。当前阶段由 `scripts/metrics.py` 记录 start、trace-event、finish；异步 Runtime 仅收集授权窗口，经 DLP 后投递到`scripts/reporting_config.py` 的发布默认地址或用户私有 `config.json` 的覆盖地址。未配置地址时只跳过异步采集和投递，授权询问与 enabled 下的同步账本记录正常进行；未授权时两条统计通路都不新增数据。统计错误不阻塞业务。不得发送原始源码、凭据或未脱敏对话。

完整授权规则见 `reference/eval-protocol.md`；账本与异步采集、归档封口、重试清理及提交边界见 `reference/eval-supplement-protocol.md`。阶段生命周期遵循 `reference/telemetry-command-protocol.md`。

未配置或配置无效时，Runtime 不自动安装/唤醒，不采集会话、不投递补充数据或健康报告，也不把配置缺失计为采集失败。已安装 Worker 每轮执行前同样检查；历史待发送队列保留并暂停，不使用其中保存的地址绕过检查。配置恢复后按原授权、授权代次和队列地址继续处理。已有授权下的本地账本记录不受此检查影响。

未配置异步地址时静默跳过，不在正常命令、授权选择结果或最终摘要显示地址缺失、未异步上报或配置提醒；内部诊断保留，仅在用户主动查询或排障时解释。详见 `reference/telemetry-command-protocol.md`。
