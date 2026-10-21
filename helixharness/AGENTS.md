# HelixHarness 开发指南

仓库根是 Plugin。公开命令只允许 init、propose、apply、archive、ut、bug-fix、update、status、config；循环为 propose --loop。commands/*.md 保存公共入口和确定性路由消费，单仓完整正文在 workflows/single/*.md，多仓协调正文在 workflows/workspace/*.md；Router 返回唯一正文路径，禁止在 commands 重复业务步骤；skills 保留复用方法，agents 保留子任务角色。Single 与 Workspace 使用同一 HelixHarness 合同。status 是只读诊断入口，config 只管理用户配置；两者不进入研发阶段 Router。

修改后运行相关 Python unittest、Host Kit 装载和路径引用检查。不得加入凭据、真实会话、私有 URL 或运行数据。不要自动推送或部署。

测试文件统一放在 `scripts/tests/`，通过 `python3 scripts/run_tests.py` 运行完整测试集；`scripts/` 根目录不放 `test_*.py`。
