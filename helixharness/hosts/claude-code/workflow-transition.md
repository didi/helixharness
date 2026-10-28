# 工作流跳转

模式：`command`。只在 propose --loop 编排时按 Router 返回值依次进入 propose、apply、archive。每个阶段独立完成并写入真实证据；Apply 的双轴评审通过内部 code-review 技能执行。
