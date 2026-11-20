import { afterAll, describe, expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  REPO_ROOT_TOKEN,
  type PluginContext,
  registerHelixHarness,
} from "../core.ts";

const PLUGIN_ROOT = path.resolve(import.meta.dir, "../..");
const FIXTURE_ROOT = fs.mkdtempSync(path.join(os.tmpdir(), "helix-directory-binding-"));
const SESSION_ROOT = fs.mkdirSync(path.join(FIXTURE_ROOT, "session repo's directory"), { recursive: true })!;
const LOCATION_ROOT = fs.mkdirSync(path.join(FIXTURE_ROOT, "plugin-location"), { recursive: true })!;
afterAll(() => fs.rmSync(FIXTURE_ROOT, { recursive: true, force: true }));

interface Captured {
  commands: Array<Record<string, any>>;
  skills: Array<Record<string, any>>;
  agents: Array<{ id: string; draft: Record<string, any> }>;
  prompts: Array<Record<string, any>>;
  toolHooks: Array<(event: any) => Promise<void> | void>;
  sessionGets: string[];
  registrationOrder: string[];
}

function createContext(options: {
  location?: string;
  sessionDirectory?: string;
  getSession?: (sessionID: string) => Promise<any>;
  hookError?: Error;
} = {}): { ctx: PluginContext; captured: Captured } {
  const captured: Captured = {
    commands: [],
    skills: [],
    agents: [],
    prompts: [],
    toolHooks: [],
    sessionGets: [],
    registrationOrder: [],
  };
  const ctx = {
    location: { directory: options.location ?? LOCATION_ROOT },
    command: {
      async transform(callback: (editor: any) => void) {
        captured.registrationOrder.push("command");
        callback({ add: (command: any) => captured.commands.push(command) });
      },
    },
    skill: {
      async transform(callback: (editor: any) => void) {
        captured.registrationOrder.push("skill");
        callback({ add: (skill: any) => captured.skills.push(skill) });
      },
    },
    agent: {
      async transform(callback: (editor: any) => void) {
        captured.registrationOrder.push("agent");
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
        captured.registrationOrder.push("hook");
        if (options.hookError) throw options.hookError;
        captured.toolHooks.push(callback);
      },
    },
    session: {
      async get({ sessionID }: { sessionID: string }) {
        captured.sessionGets.push(sessionID);
        if (options.getSession) return options.getSession(sessionID);
        return {
          location: { directory: options.sessionDirectory ?? SESSION_ROOT },
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
      prompt: { text: "--workspace --knowledge", files: [{ uri: "file:///x" }], agents: [{ id: "synthetic-agent" }], skills: [{ id: "synthetic-skill" }] },
      delivery: "queue",
    });

    expect(captured.prompts).toHaveLength(1);
    const prompt = captured.prompts[0];
    expect(prompt.sessionID).toBe("ses_test");
    expect(prompt.delivery).toBe("queue");
    expect(prompt.files).toEqual([{ uri: "file:///x" }]);
    expect(prompt.agents).toEqual([{ id: "synthetic-agent" }]);
    expect(prompt.skills).toEqual([{ id: "synthetic-skill" }]);
    expect(prompt.metadata).toEqual({ "helixharness.command": "init" });
    expect(prompt.text).toContain("# HelixHarness command /helix:init");
    expect(prompt.text).toContain("canonical_host_id: `opencode`");
    expect(prompt.text).toContain(`repo_root: \`${fs.realpathSync(SESSION_ROOT)}\``);
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
    expect(result.content).toBe(`repo_root: \`${fs.realpathSync(SESSION_ROOT)}\``);

    const arrayResult = {
      content: [{ type: "text" as const, text: `x ${REPO_ROOT_TOKEN}` }],
    };
    await captured.toolHooks[0]({
      tool: "skill",
      sessionID: "ses_test",
      status: "completed",
      result: arrayResult,
    });
    expect(arrayResult.content[0].text).toBe(`x ${fs.realpathSync(SESSION_ROOT)}`);

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

describe("session directory binding fails closed", () => {
  const filePath = path.join(FIXTURE_ROOT, "ordinary-file");
  fs.writeFileSync(filePath, "synthetic");
  const deletedDirectory = fs.mkdtempSync(path.join(FIXTURE_ROOT, "deleted-"));
  fs.rmdirSync(deletedDirectory);
  const invalidSessions = [
    ["missing session", undefined],
    ["missing location", {}],
    ["missing directory", { location: {} }],
    ["empty directory", { location: { directory: "" } }],
    ["blank directory", { location: { directory: "   " } }],
    ["relative directory", { location: { directory: "." } }],
    ["deleted directory", { location: { directory: deletedDirectory } }],
    ["file directory", { location: { directory: filePath } }],
  ] as const;

  for (const [name, session] of invalidSessions) {
    test(`commands reject ${name} before prompting`, async () => {
      const { ctx, captured } = createContext({ getSession: async () => session });
      await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
      const init = captured.commands.find((command) => command.name === "helix:init")!;
      await expect(init.execute({ sessionID: "invalid", prompt: { text: "--synthetic" } }))
        .rejects.toThrow("repo_root_invalid");
      expect(captured.prompts).toHaveLength(0);
    });
  }

  test("commands hide lookup errors and never fall back to the plugin location", async () => {
    const { ctx, captured } = createContext({
      getSession: async () => { throw new Error("private synthetic lookup details /secret/path"); },
    });
    await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
    const init = captured.commands.find((command) => command.name === "helix:init")!;
    let failure: unknown;
    try { await init.execute({ sessionID: "failed", prompt: { text: "" } }); } catch (error) { failure = error; }
    expect(failure).toBeInstanceOf(Error);
    expect((failure as Error).message).toContain("repo_root_lookup_failed");
    for (const forbidden of ["private synthetic", "/secret/path", LOCATION_ROOT]) {
      expect((failure as Error).message).not.toContain(forbidden);
    }
    expect(captured.prompts).toHaveLength(0);
  });

  for (const shape of ["string", "blocks"] as const) {
    for (const failure of ["lookup", "invalid"] as const) {
      test(`skill ${shape} ${failure} failure discards the whole result`, async () => {
        const { ctx, captured } = createContext({
          getSession: async () => {
            if (failure === "lookup") throw new Error("private synthetic details");
            return { location: { directory: filePath } };
          },
        });
        await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
        const original = `unsafe original ${REPO_ROOT_TOKEN}`;
        const event = {
          tool: "skill", sessionID: "failed", status: "completed",
          result: {
            content: shape === "string" ? original : [
              { type: "text", text: original },
              { type: "file", uri: "file:///synthetic-private-attachment" },
            ],
            metadata: { original: "synthetic metadata" },
          },
        };
        await captured.toolHooks[0](event);
        const result = JSON.stringify(event.result);
        expect(result).toContain("BLOCKED");
        expect(result).toContain(failure === "lookup" ? "repo_root_lookup_failed" : "repo_root_invalid");
        for (const forbidden of [REPO_ROOT_TOKEN, "unsafe original", "synthetic-private-attachment", "metadata", "private synthetic", filePath]) {
          expect(result).not.toContain(forbidden);
        }
      });
    }
  }

  test("unrelated tool results skip session lookup and remain unchanged", async () => {
    const { ctx, captured } = createContext({ getSession: async () => { throw new Error("must not look up"); } });
    await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
    const events = [
      { tool: "read", status: "completed", result: { content: REPO_ROOT_TOKEN } },
      { tool: "skill", status: "error", result: { content: REPO_ROOT_TOKEN } },
      { tool: "skill", status: "completed", result: { content: "unrelated skill" } },
      { tool: "skill", status: "completed", result: { content: [{ type: "file", uri: REPO_ROOT_TOKEN }] } },
      { tool: "skill", status: "completed", result: {} },
      { tool: "skill", status: "completed" },
    ];
    for (const event of events) {
      const invocation = { ...event, sessionID: "unrelated" };
      const before = JSON.stringify(invocation);
      await captured.toolHooks[0](invocation);
      expect(JSON.stringify(invocation)).toBe(before);
    }
    expect(captured.sessionGets).toEqual([]);
  });

  test("interleaved command and skill invocations resolve their own directories without a cache", async () => {
    const repoB = fs.mkdirSync(path.join(FIXTURE_ROOT, 'repo B "quoted"'), { recursive: true })!;
    const linkA = path.join(FIXTURE_ROOT, "repo A symlink");
    fs.symlinkSync(SESSION_ROOT, linkA, "dir");
    let currentA = linkA;
    let releaseA!: () => void;
    const gate = new Promise<void>((resolve) => { releaseA = resolve; });
    const { ctx, captured } = createContext({ getSession: async (sessionID) => {
      if (sessionID === "A") { await gate; return { location: { directory: currentA } }; }
      return { location: { directory: repoB } };
    } });
    await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
    const init = captured.commands.find((command) => command.name === "helix:init")!;
    const commandA = init.execute({ sessionID: "A", prompt: { text: "A" } });
    const fileBlock = { type: "file", uri: "file:///synthetic-attachment" };
    const resultB = { content: [fileBlock, { type: "text", text: `B ${REPO_ROOT_TOKEN}` }] };
    await captured.toolHooks[0]({ tool: "skill", sessionID: "B", status: "completed", result: resultB });
    releaseA();
    await commandA;
    expect(captured.prompts[0].text).toContain(`repo_root: \`${fs.realpathSync(SESSION_ROOT)}\``);
    expect(resultB.content[1]).toEqual({ type: "text", text: `B ${fs.realpathSync(repoB)}` });
    expect(resultB.content[0]).toBe(fileBlock);
    currentA = repoB;
    await init.execute({ sessionID: "A", prompt: { text: "A moved" } });
    expect(captured.prompts[1].text).toContain(`repo_root: \`${fs.realpathSync(repoB)}\``);
    expect(captured.sessionGets).toEqual(["A", "B", "A"]);
  });

  test("skill binding keeps replacement metacharacters in directory names literal", async () => {
    const directory = fs.mkdirSync(path.join(FIXTURE_ROOT, "repo $& $$ $` $'"), { recursive: true })!;
    const { ctx, captured } = createContext({ sessionDirectory: directory });
    await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
    const result = { content: `before ${REPO_ROOT_TOKEN} after` };
    await captured.toolHooks[0]({ tool: "skill", sessionID: "literal", status: "completed", result });
    expect(result.content).toBe(`before ${fs.realpathSync(directory)} after`);
    expect(result.content).not.toContain(REPO_ROOT_TOKEN);
  });

  test("required hook is registered before command, skill or agent surfaces", async () => {
    const { ctx, captured } = createContext();
    await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
    expect(captured.registrationOrder).toEqual(["hook", "command", "skill", "agent"]);
  });

  for (const missing of [true, false]) {
    test(`${missing ? "missing" : "rejecting"} required hook prevents all surface registration`, async () => {
      const { ctx, captured } = createContext({ hookError: missing ? undefined : new Error("synthetic unsupported hook") });
      if (missing) delete ctx.tool;
      await expect(registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT })).rejects.toThrow();
      expect(captured.commands).toEqual([]);
      expect(captured.skills).toEqual([]);
      expect(captured.agents).toEqual([]);
    });
  }
});
