import { describe, expect, test } from "bun:test";
import path from "node:path";

import {
  REPO_ROOT_TOKEN,
  type PluginContext,
  registerHelixHarness,
} from "../core.ts";

const PLUGIN_ROOT = path.resolve(import.meta.dir, "../..");

interface Captured {
  commands: Array<Record<string, any>>;
  skills: Array<Record<string, any>>;
  agents: Array<{ id: string; draft: Record<string, any> }>;
  prompts: Array<Record<string, any>>;
  toolHooks: Array<(event: any) => Promise<void> | void>;
}

function createContext(options: {
  location?: string;
  sessionDirectory?: string;
} = {}): { ctx: PluginContext; captured: Captured } {
  const captured: Captured = {
    commands: [],
    skills: [],
    agents: [],
    prompts: [],
    toolHooks: [],
  };
  const ctx = {
    location: { directory: options.location ?? "/project" },
    command: {
      async transform(callback: (editor: any) => void) {
        callback({ add: (command: any) => captured.commands.push(command) });
      },
    },
    skill: {
      async transform(callback: (editor: any) => void) {
        callback({ add: (skill: any) => captured.skills.push(skill) });
      },
    },
    agent: {
      async transform(callback: (editor: any) => void) {
        callback({
          update: (id: string, update: (draft: any) => void) => {
            const draft: Record<string, any> = {
              name: id,
              mode: "primary",
              hidden: false,
              permissions: [],
            };
            update(draft);
            captured.agents.push({ id, draft });
          },
        });
      },
    },
    tool: {
      async hook(_name: string, callback: (event: any) => Promise<void> | void) {
        captured.toolHooks.push(callback);
      },
    },
    session: {
      async get() {
        return {
          location: { directory: options.sessionDirectory ?? "/session-repo" },
        };
      },
      async prompt(input: Record<string, any>) {
        captured.prompts.push(input);
      },
    },
  };
  return { ctx: ctx as unknown as PluginContext, captured };
}

describe("OpenCode registration", () => {
  test("registers nine commands, all method skills and all subagent roles", async () => {
    const { ctx, captured } = createContext();
    await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });

    expect(captured.commands.map((command) => command.name)).toEqual([
      "helix:init",
      "helix:propose",
      "helix:apply",
      "helix:archive",
      "helix:bug-fix",
      "helix:ut",
      "helix:update",
      "helix:status",
      "helix:config",
    ]);
    expect(captured.commands.every((command) => command.description)).toBe(true);
    expect(captured.skills.map((skill) => skill.id)).toContain("helix-code-review");
    expect(captured.skills.every((skill) => path.isAbsolute(skill.path))).toBe(true);
    expect(captured.agents.map((agent) => agent.id)).toContain("helix-scan-tech");
    expect(captured.agents.map((agent) => agent.id)).toContain("helix-worker");
    expect(captured.agents.every((agent) => agent.draft.mode === "subagent")).toBe(
      true,
    );
    const canonicalAgents = captured.agents.filter(
      (agent) => agent.id !== "helix-worker",
    );
    expect(
      canonicalAgents.every((agent) =>
        agent.draft.permissions.some(
          (rule: Record<string, string>) =>
            rule.action === "subagent" &&
            rule.resource === "*" &&
            rule.effect === "deny",
        ),
      ),
    ).toBe(true);
    const worker = captured.agents.find((agent) => agent.id === "helix-worker")!;
    expect(
      worker.draft.permissions.some(
        (rule: Record<string, string>) => rule.action === "subagent",
      ),
    ).toBe(false);
    expect(captured.toolHooks).toHaveLength(1);
  });

  test("command execution injects Host Context and the canonical contract", async () => {
    const { ctx, captured } = createContext();
    await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
    const init = captured.commands.find((command) => command.name === "helix:init")!;

    await init.execute({
      sessionID: "ses_test",
      prompt: { text: "--workspace --knowledge", files: [{ uri: "file:///x" }] },
      delivery: "queue",
    });

    expect(captured.prompts).toHaveLength(1);
    const prompt = captured.prompts[0];
    expect(prompt.sessionID).toBe("ses_test");
    expect(prompt.delivery).toBe("queue");
    expect(prompt.files).toEqual([{ uri: "file:///x" }]);
    expect(prompt.metadata).toEqual({ "helixharness.command": "init" });
    expect(prompt.text).toContain("# HelixHarness command /helix:init");
    expect(prompt.text).toContain("canonical_host_id: `opencode`");
    expect(prompt.text).toContain("repo_root: `/session-repo`");
    expect(prompt.text).toContain("--workspace --knowledge");
    expect(prompt.text).toContain("HelixHarness 公共命令唯一契约");
    expect(prompt.text).not.toContain("$ARGUMENTS");
  });

  test("the skill result hook binds repo_root to the requesting session", async () => {
    const { ctx, captured } = createContext();
    await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });

    const result = {
      content: `repo_root: \`${REPO_ROOT_TOKEN}\``,
    };
    await captured.toolHooks[0]({
      tool: "skill",
      sessionID: "ses_test",
      status: "completed",
      result,
    });
    expect(result.content).toBe("repo_root: `/session-repo`");

    const arrayResult = {
      content: [{ type: "text" as const, text: `x ${REPO_ROOT_TOKEN}` }],
    };
    await captured.toolHooks[0]({
      tool: "skill",
      sessionID: "ses_test",
      status: "completed",
      result: arrayResult,
    });
    expect(arrayResult.content[0].text).toBe("x /session-repo");

    const untouched = { content: "no marker" };
    await captured.toolHooks[0]({
      tool: "read",
      sessionID: "ses_test",
      status: "completed",
      result: untouched,
    });
    expect(untouched.content).toBe("no marker");
  });
});
