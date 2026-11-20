---
name: helix-d-d2c
description: "用户提到 d-d2c、设计稿还原、MasterGo、贴图比对、design-to-code、页面开发，或使用 d-d2c --create-page / --update-page / --component 时，按这个 skill 要求执行；支持新建整页、更新已有页、组件/弹层/卡片三种流程入口，后两者分别读取 rules/d-d2c-update-page.md / rules/d-d2c-component.md 作为流程覆盖层"
disable-model-invocation: false
---

# Cursor method projection: d-d2c

入口类型：`method`。插件根固定为当前文件的 `../../../..`。完整读取 `hosts/cursor/skill-entry.md`，执行“公共绑定”后仅执行“方法入口”分支；不查询命令投影、不加载 commands 文件、不调用 Router。执行 runtime preflight 并以 `--platform cursor` 解析同一安装的 Host Context。完整读取 `reference/host-runtime-protocol.md`、`reference/tool-semantics.md` 和 `resources.runtime`；需要子 agent 时再读取公共子代理协议及 `resources.subagent_protocol`。

完整读取并执行唯一 canonical 方法 `skills/d-d2c/SKILL.md` 的正文。canonical frontmatter 不属于 Cursor 元数据，不装载其中的 Claude hooks、工具声明或调用标记。正文相对资源从 `skills/d-d2c/` 解析；不得改以投影目录为基准。保留实际步骤、输出工件与全部门禁，不复制或改写业务语义，不扫描全局同名 Skill。

D2C 必须执行 mode 对应的完整 canonical workflow、实际 runtime 健康与截图联合检查、fidelity 评测和 phase 6。新 phase 0 后记录本次 `pageDir` 与 `enteredAt`；在任何用户询问或结束前执行 `resources.runtime` 规定的 `hosts/cursor/d2c_gate.cjs`，分别使用 `--action ask` 或 `--action stop`。只有退出码 0 且 `allow=true` 可询问/结束；只有 `action.kind=done` 可宣称完成。不把缺少原生 skill hook 当作省略门禁的理由。
