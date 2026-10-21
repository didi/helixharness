# 已校准 Scenario v2 的参考方式

只从目标平台当前 `case-sources-v2` 中选择 `PUBLISHED + CALIBRATED` 的 REPOSITORY_CASE 作为结构参考。可以借鉴：

- 如何冻结 Base/Solution commit 与 patch SHA256；
- 如何把需求拆成可追溯 requirement/source evidence；
- 如何编写 Base 失败、Solution 通过的 F2P 和两侧均通过的 P2P；
- 如何准备不泄露到执行工作区的隐藏校验。

不得复制其他 Case 的业务事实、commit、路径、断言或澄清答案。Case Source 不保存工作流命令、阶段产物来源或评分轴；这些由每次 Run 冻结。

本地缓存不能证明远端当前状态。冲突检查必须读取同一目标快照下的 `evaluation.db`、`case-sources-v2` 和 repository registry。
