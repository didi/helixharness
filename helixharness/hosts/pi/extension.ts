/**
 * HelixHarness pi Host Kit.
 *
 * Forwards pi session lifecycle to `hosts/pi/hook_entry.py` and exposes the nine
 * public HelixHarness commands as `/helix-<name>`.
 */
import { spawn } from "node:child_process";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const COMMANDS: Record<string, string> = {
  init: "HelixHarness 初始化入口",
  propose: "HelixHarness 需求提案入口",
  apply: "HelixHarness 实施入口",
  archive: "HelixHarness 归档入口",
  "bug-fix": "HelixHarness 缺陷修复入口",
  ut: "HelixHarness 单元测试入口",
  update: "HelixHarness 版本更新入口",
  status: "HelixHarness 只读诊断入口",
  config: "HelixHarness 用户配置入口",
};

const HOOK_TIMEOUT_MS = 3000;
const HOST_KIT_DEPTH = 5; // <root>/hosts/pi/skills/<name>/SKILL.md

type HookOutput = { hookSpecificOutput?: { additionalContext?: string } };

function findPluginRoot(pi: ExtensionAPI): string | null {
  for (const command of pi.getCommands()) {
    if (command.source !== "skill" || !command.name.startsWith("skill:helix-")) continue;
    let root = command.sourceInfo.path;
    for (let depth = 0; depth < HOST_KIT_DEPTH; depth += 1) root = path.dirname(root);
    if (root !== path.dirname(root)) return root;
  }
  return null;
}

function runHook(
  root: string,
  event: "SessionStart" | "SessionEnd",
  payload: Record<string, unknown>,
): Promise<HookOutput | null> {
  return new Promise((resolve) => {
    const child = spawn(
      "sh",
      [
        path.join(root, "scripts", "python_hook_bootstrap.sh"),
        event,
        path.join(root, "hosts", "pi", "hook_entry.py"),
      ],
      { stdio: ["pipe", "pipe", "ignore"] },
    );
    let stdout = "";
    let settled = false;
    const settle = (value: HookOutput | null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      settle(null);
    }, HOOK_TIMEOUT_MS);
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk;
    });
    child.on("error", () => settle(null));
    child.on("close", () => {
      try {
        settle(JSON.parse(stdout) as HookOutput);
      } catch {
        settle(null);
      }
    });
    child.stdin.on("error", () => undefined);
    child.stdin.end(JSON.stringify(payload));
  });
}

export default function (pi: ExtensionAPI) {
  let root: string | null = null;
  let pendingContext: string | null = null;

  const pluginRoot = (): string | null => (root ??= findPluginRoot(pi));

  const payloadFor = (ctx: ExtensionContext, event: string) => ({
    hook_event_name: event,
    cwd: ctx.cwd,
    session_id: ctx.sessionManager.getSessionId(),
    session_file: ctx.sessionManager.getSessionFile() ?? null,
  });

  const startSession = async (ctx: ExtensionContext): Promise<void> => {
    const plugin = pluginRoot();
    if (!plugin) return;
    const output = await runHook(plugin, "SessionStart", payloadFor(ctx, "SessionStart"));
    pendingContext = output?.hookSpecificOutput?.additionalContext ?? null;
  };

  pi.on("session_start", async (_event, ctx) => {
    await startSession(ctx);
  });

  pi.on("before_agent_start", async (_event, ctx) => {
    if (!pendingContext && !root) await startSession(ctx);
    if (!pendingContext) return;
    const content = pendingContext;
    pendingContext = null;
    return { message: { customType: "helixharness-host-context", content, display: false } };
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    const plugin = pluginRoot();
    if (plugin) await runHook(plugin, "SessionEnd", payloadFor(ctx, "SessionEnd"));
  });

  for (const [name, description] of Object.entries(COMMANDS)) {
    pi.registerCommand(`helix-${name}`, {
      description,
      handler: async (args, ctx) => {
        if (!ctx.isIdle()) {
          ctx.ui.notify("Agent 忙，稍后再执行 HelixHarness 命令。", "warning");
          return;
        }
        pi.sendUserMessage(`/skill:helix-${name}${args.trim() ? ` ${args.trim()}` : ""}`, {
          expandPromptTemplates: true,
        });
      },
    });
  }
}
