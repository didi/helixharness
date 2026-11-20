# OpenCode adapter verification / OpenCode 适配验收

Run these commands from the plugin directory (`helixharness/`).

```bash
bun install --ignore-scripts
bun test opencode/tests
bun run typecheck
```

The default suite skips native-server tests. To exercise an installed OpenCode V2 binary:

```bash
HELIXHARNESS_OPENCODE_E2E=1 bun test opencode/tests/e2e.test.ts
```

If it is not on `PATH`, select the executable explicitly:

```bash
HELIXHARNESS_OPENCODE_BIN=/absolute/path/to/opencode \
  HELIXHARNESS_OPENCODE_E2E=1 bun test opencode/tests/e2e.test.ts
```

`native-fixture.ts` starts the real OpenCode server with temporary project/home/config/data/cache/state
directories and a loopback OpenAI-compatible scripted model. It forwards no model-account environment
variables and uses only synthetic messages and fixture authentication. It stops both servers and removes
the temporary directories when each fixture finishes. An OpenCode account or paid model is not needed.

The tests verify registration and command admission, per-session directory binding, command rejection
after a directory disappears, successful Skill binding and complete replacement of failed Skill output
before the next model call. They also verify native worker → two independent reviewer dispatches,
leaf-role denial and depth-limit failures. The assertions observe actual native execution results;
a tool's presence in the advertised catalog is not proof that it can execute.

These are adapter integration tests. Scripted model responses do not establish that a real model can
complete Init → Propose → Apply → Archive, produce correct business code, or follow all ownership rules.
OpenCode remains Preview until that separate acceptance is completed.

## 中文说明

在插件目录运行上面的 Bun 单元测试与类型检查。默认跳过真实宿主测试；设置
`HELIXHARNESS_OPENCODE_E2E=1` 后，使用 `PATH` 上的 OpenCode，或通过
`HELIXHARNESS_OPENCODE_BIN` 指定实际可执行文件。最低验证版本为 OpenCode 2.2.2。

测试启动真实 OpenCode 服务，但模型是本机回环地址上的脚本模型；项目、宿主测试 home、配置、数据、缓存和
状态均使用临时目录，不传递模型账号环境变量，不需要真实账号或付费模型。测试结束后关闭
服务并清理临时目录。

验收覆盖注册、命令提交、跨会话目录绑定、目录失效后的阻塞、Skill 正文绑定与失败结果替换，
以及 worker 的两路独立派发、leaf 角色拒绝委派和深度不足拦截。必须检查真实调用结果，不能仅凭
工具出现在列表中判断权限有效。

这些结果证明宿主适配链路可用；真实模型执行完整需求、业务实现正确性及全部所有权规则的
验收仍需单独完成，因此支持级别保持 Preview。
