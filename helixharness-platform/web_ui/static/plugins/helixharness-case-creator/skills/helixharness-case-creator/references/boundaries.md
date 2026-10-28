# 边界与阻断规则

- 根目录不可读、非 Git、shallow/LFS/submodule 事实不完整时停止自动生成并标记 `NEEDS_REVIEW`。
- 所有业务事实必须来自冻结 commit 或相关历史需求；不得读取 `.env`、凭据、缓存和未提交工作区内容。
- Case ID 冲突且 source hash 不同，必须取得用户对该 ID 的更新授权；不得自动加随机后缀。
- 目标 `evaluation.db`、`case-sources-v2` 或 repository registry 任一不可验证，返回 `TARGET_STATE_UNVERIFIED`。
- 同一 Case 参与 `QUEUED/RUNNING` 的 Scenario v2 Run 时阻止更新。
- 已存在 Source 目录但 DB 无当前 Scenario 时，阻止发布并要求人工核对孤儿目录。
- 更新已发布 Case 后必须重新确定性校准；校准前不得用于正式评测。
- 发布、校准、创建正式 Run、提交代码、推送和部署是独立授权。
