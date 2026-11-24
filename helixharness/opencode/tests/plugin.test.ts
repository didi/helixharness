import { afterAll, describe, expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  REPO_ROOT_TOKEN,
  type PluginContext,
  type SessionBindingEvent,
  registerHelixHarness,
} from "../core.ts";

const PLUGIN_ROOT = path.resolve(import.meta.dir, "../..");
const FIXTURE_ROOT = fs.mkdtempSync(path.join(os.tmpdir(), "helix-directory-binding-"));
const SESSION_ROOT = fs.mkdirSync(path.join(FIXTURE_ROOT, "session repo's directory"), { recursive: true })!;
const LOCATION_ROOT = fs.mkdirSync(path.join(FIXTURE_ROOT, "plugin-location"), { recursive: true })!;
afterAll(() => fs.rmSync(FIXTURE_ROOT, { recursive: true, force: true }));

test("owned telemetry shell calls receive per-call native identity without global environment mutation", async () => {
  const { ctx, captured } = createContext();
  await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
  const input = { command: `python3 '${PLUGIN_ROOT}/scripts/metrics.py' --help` };
  const before = process.env.HELIXHARNESS_SESSION_ID;
  await captured.beforeHooks[0]({ tool: "shell", sessionID: "ses_boundA", input });
  expect(input.command).toContain("HELIXHARNESS_SESSION_ID='ses_boundA'");
  expect(input.command).toContain("HELIXHARNESS_SESSION_CWD=");
  expect(input.command).toContain("'\\''");
  expect(process.env.HELIXHARNESS_SESSION_ID).toBe(before);
});

test("ordinary shell calls are unchanged and telemetry identity lookup fails closed", async () => {
  const { ctx, captured } = createContext({ getSession: async () => { throw new Error("synthetic unavailable"); } });
  await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
  const ordinary = { command: "git status --short" };
  await captured.beforeHooks[0]({ tool: "shell", sessionID: "ses_A", input: ordinary });
  expect(ordinary.command).toBe("git status --short");
  await expect(captured.beforeHooks[0]({ tool: "shell", sessionID: "ses_A", input: { command: `python3 '${PLUGIN_ROOT}/scripts/metrics.py' --help` } })).rejects.toThrow("repo_root_lookup_failed");
});

interface Captured {
  commands: Array<Record<string, any>>;
  skills: Array<Record<string, any>>;
  agents: Array<{ id: string; draft: Record<string, any> }>;
  prompts: Array<Record<string, any>>;
  beforeHooks: Array<(event: any) => Promise<void> | void>;
  toolHooks: Array<(event: any) => Promise<void> | void>;
  sessionGets: string[];
  sessionHooks: Array<{ name: string; callback: (event: any) => Promise<void> | void }>;
  registrationOrder: string[];
}

function createContext(options: {
  location?: string;
  sessionDirectory?: string;
  getSession?: (sessionID: string) => Promise<any>;
  hookError?: Error;
  sessionHookError?: string;
} = {}): { ctx: PluginContext; captured: Captured } {
  const captured: Captured = {
    commands: [],
    skills: [],
    agents: [],
    prompts: [],
    toolHooks: [],
    beforeHooks: [],
    sessionGets: [],
    sessionHooks: [],
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
      async hook(name: string, callback: (event: any) => Promise<void> | void) {
        captured.registrationOrder.push(name === "execute.before" ? "before-hook" : "hook");
        if (options.hookError) throw options.hookError;
        (name === "execute.before" ? captured.beforeHooks : captured.toolHooks).push(callback);
      },
    },
    session: {
      async hook(name: string, callback: (event: any) => Promise<void> | void) {
        captured.registrationOrder.push(`session-hook:${name}`);
        if (options.sessionHookError === name) throw new Error("Synthetic request hook unavailable");
        captured.sessionHooks.push({ name, callback });
      },
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

    const body = captured.skills.find(s => s.id === "helix-d-d2c")!.content;
    const result = { content: body };
    await captured.toolHooks[0]({
      tool: "skill",
      sessionID: "ses_test",
      status: "completed",
      result,
    });
    expect(result.content).toContain(`repo_root: \`${fs.realpathSync(SESSION_ROOT)}\``);

    const arrayResult = {
      content: [{ type: "text" as const, text: body }],
    };
    await captured.toolHooks[0]({
      tool: "skill",
      sessionID: "ses_test",
      status: "completed",
      result: arrayResult,
    });
    expect(arrayResult.content[0].text).toContain(`repo_root: \`${fs.realpathSync(SESSION_ROOT)}\``);

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
        const original = `unsafe original ${captured.skills.find(s => s.id === "helix-d-d2c")!.content}`;
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
    const body = captured.skills.find(s => s.id === "helix-d-d2c")!.content;
    const resultB = { content: [fileBlock, { type: "text", text: body }] };
    await captured.toolHooks[0]({ tool: "skill", sessionID: "B", status: "completed", result: resultB });
    releaseA();
    await commandA;
    expect(captured.prompts[0].text).toContain(`repo_root: \`${fs.realpathSync(SESSION_ROOT)}\``);
    expect((resultB.content[1] as any).text).toContain(`repo_root: \`${fs.realpathSync(repoB)}\``);
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
    const body = captured.skills.find(s => s.id === "helix-d-d2c")!.content;
    const result = { content: body };
    await captured.toolHooks[0]({ tool: "skill", sessionID: "literal", status: "completed", result });
    expect(result.content).toContain(`repo_root: \`${fs.realpathSync(directory)}\``);
    expect(result.content).not.toContain(REPO_ROOT_TOKEN);
  });

  test("required hook is registered before command, skill or agent surfaces", async () => {
    const { ctx, captured } = createContext();
    await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
    expect(captured.registrationOrder).toEqual(["hook", "before-hook", "session-hook:context", "session-hook:compaction", "session-hook:generate", "session-hook:title", "session-hook:experimental.idle", "command", "skill", "agent"]);
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


describe("native Skill mention request binding", () => {
  const hookNames = ["context", "compaction", "generate", "title"] as const;
  async function context(options: Parameters<typeof createContext>[0] = {}) {
    const result = createContext(options);
    await registerHelixHarness(result.ctx, { pluginRoot: PLUGIN_ROOT });
    expect(result.captured.sessionHooks.map(h => h.name)).toEqual([...hookNames, "experimental.idle"]);
    return result;
  }

  for (const name of hookNames) {
    test(`binds owned skill text in ${name} requests without changing source messages`, async () => {
      const { captured } = await context();
      const skill = captured.skills.find(s => s.id === "helix-tdd")!;
      const source = { role: "user", content: `<skill_content>\n${skill.content.trimEnd()}\n</skill_content>` };
      const system = { type: "text", text: skill.content, metadata: { synthetic: true } };
      const sourceText = source.content;
      const event = { sessionID: "mention", messages: [source], system: [system] };
      await captured.sessionHooks.find(h => h.name === name)!.callback(event);
      expect(event.messages[0].content).toContain(`repo_root: \`${fs.realpathSync(SESSION_ROOT)}\``);
      expect(event.messages[0].content).not.toContain(REPO_ROOT_TOKEN);
      expect(event.system[0].text).not.toContain(REPO_ROOT_TOKEN);
      expect(event.system[0].metadata).toBe(system.metadata);
      expect(source.content).toBe(sourceText);
      expect(system.text).toContain(REPO_ROOT_TOKEN);
      expect(captured.sessionGets).toEqual(["mention"]);
    });
  }

  test("preserves user literals, unrelated skills, media and metadata", async () => {
    const { captured } = await context();
    const skill = captured.skills.find(s => s.id === "helix-feedback")!;
    const media = { type: "media", media: { synthetic: true } };
    const userLiteral = `User literal: ${REPO_ROOT_TOKEN} $ARGUMENTS`;
    const textPart = { type: "text", text: `${userLiteral}\n${skill.content}` };
    const source = { role: "user", content: [textPart, media], metadata: { synthetic: true } };
    const unrelated = { role: "user", content: `Unrelated skill: ${REPO_ROOT_TOKEN}` };
    const event: SessionBindingEvent = { sessionID: "mention", messages: [source, unrelated], system: [] };
    await captured.sessionHooks.find(h => h.name === "context")!.callback(event);
    const parts = event.messages[0].content as ReadonlyArray<{ type: string; text?: string }>;
    expect(parts[0].text).toContain(userLiteral);
    expect(parts[0].text).toContain(`repo_root: \`${fs.realpathSync(SESSION_ROOT)}\``);
    expect(parts[1]).toBe(media);
    expect(event.messages[0].metadata).toBe(source.metadata);
    expect(event.messages[1]).toBe(unrelated);
    expect(textPart.text).toContain(skill.content);
  });

  test("re-resolves moved and interleaved sessions without caching", async () => {
    const b = fs.mkdirSync(path.join(FIXTURE_ROOT, "mention B $& $$ $` $'"), { recursive: true })!;
    let a = SESSION_ROOT;
    const { captured } = await context({ getSession: async id => ({ location: { directory: id === "A" ? a : b } }) });
    const text = captured.skills.find(s => s.id === "helix-tdd")!.content;
    const hook = captured.sessionHooks.find(h => h.name === "context")!.callback;
    for (const [id, expected] of [["A", SESSION_ROOT], ["B", b], ["A", b]]) {
      if (id === "A" && expected === b) a = b;
      const event = { sessionID: id, messages: [{ role: "user", content: text }], system: [] };
      await hook(event);
      expect(event.messages[0].content).toContain(`repo_root: \`${fs.realpathSync(expected)}\``);
    }
    expect(captured.sessionGets).toEqual(["A", "B", "A"]);
  });

  for (const failure of ["lookup", "invalid"] as const) {
    test(`${failure} failure aborts the model request without mutating it`, async () => {
      const { captured } = await context({ getSession: async () => {
        if (failure === "lookup") throw new Error("private synthetic detail");
        return { location: { directory: "relative" } };
      } });
      const event = { sessionID: "failed", messages: [{ role: "user", content: captured.skills[0].content }], system: [] };
      const before = JSON.stringify(event);
      const hook = captured.sessionHooks.find(h => h.name === "context")!.callback;
      let error: unknown;
      try { await hook(event); } catch (caught) { error = caught; }
      expect(error).toBeInstanceOf(Error);
      expect((error as Error).message).toContain(failure === "lookup" ? "repo_root_lookup_failed" : "repo_root_invalid");
      expect((error as Error).message).not.toContain("private synthetic");
      expect(JSON.stringify(event)).toBe(before);
    });
  }

  test("unrelated model text does not query the session", async () => {
    const { captured } = await context({ getSession: async () => { throw new Error("must not query"); } });
    const event = { sessionID: "unrelated", messages: [{ role: "user", content: REPO_ROOT_TOKEN }], system: [] };
    const before = JSON.stringify(event);
    await captured.sessionHooks.find(h => h.name === "context")!.callback(event);
    expect(JSON.stringify(event)).toBe(before);
    expect(captured.sessionGets).toEqual([]);
  });

  for (const name of hookNames) {
    test(`rejecting ${name} hook prevents all surface registration`, async () => {
      const { ctx, captured } = createContext({ sessionHookError: name });
      await expect(registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT })).rejects.toThrow();
      expect(captured.commands).toEqual([]);
      expect(captured.skills).toEqual([]);
      expect(captured.agents).toEqual([]);
    });
  }

  test("missing request hook prevents all surface registration", async () => {
    const { ctx, captured } = createContext();
    delete (ctx.session as any).hook;
    await expect(registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT })).rejects.toThrow("repo_root_hook_unavailable");
    expect(captured.commands).toEqual([]);
    expect(captured.skills).toEqual([]);
    expect(captured.agents).toEqual([]);
  });
});


test("registers UT direct-write guard before public entry points", async () => {
  const {ctx,captured}=createContext();
  await registerHelixHarness(ctx,{pluginRoot:PLUGIN_ROOT});
  expect(captured.beforeHooks).toHaveLength(1);
  await expect(Promise.resolve(captured.beforeHooks[0]({tool:"write",sessionID:"synthetic",input:{path:"docs/specs/active/sample/unittest-session.md"}}))).rejects.toThrow("ut_atomic_write_required");
  expect(captured.registrationOrder.indexOf("before-hook")).toBeLessThan(captured.registrationOrder.indexOf("command"));
});

test('UT scope protects aliases without making ordinary editing depend on session lookup',async()=>{
 let broken=false;
 const {ctx,captured}=createContext({getSession:async()=>{if(broken)throw Error('synthetic API failure');return {location:{directory:SESSION_ROOT}};}});
 await registerHelixHarness(ctx,{pluginRoot:PLUGIN_ROOT});
 const event={tool:'write',sessionID:'active-ut',input:{path:'ordinary.txt'}};
 broken=true;await captured.beforeHooks[0](event);expect(captured.sessionGets).toEqual([]);
 broken=false;await captured.commands.find(c=>c.name==='helix:ut')!.execute({sessionID:'active-ut',prompt:{text:''}});
 broken=true;await expect(Promise.resolve(captured.beforeHooks[0](event))).rejects.toThrow('repo_root');
 const idle=captured.sessionHooks.find(h=>h.name==='experimental.idle')!;expect(idle).toBeDefined();
 await idle.callback({sessionID:'active-ut',background:[{kind:'subagent'}]});
 await expect(Promise.resolve(captured.beforeHooks[0](event))).rejects.toThrow('repo_root');
 await idle.callback({sessionID:'another',background:[]});
 await expect(Promise.resolve(captured.beforeHooks[0](event))).rejects.toThrow('repo_root');
 await idle.callback({sessionID:'active-ut',background:[]});await captured.beforeHooks[0](event);
 await expect(Promise.resolve(captured.beforeHooks[0]({...event,agent:'helix-unit-test-worker'}))).rejects.toThrow('repo_root');
 broken=false;await captured.commands.find(c=>c.name==='helix:ut')!.execute({sessionID:'active-ut',prompt:{text:''}});
 await captured.commands.find(c=>c.name==='helix:status')!.execute({sessionID:'active-ut',prompt:{text:''}});
 broken=true;await captured.beforeHooks[0](event);
});

describe("native session identity binding", () => {
  test("commands expose their actual requesting identity while preserving literal arguments", async () => {
    const { ctx, captured } = createContext();
    await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
    await captured.commands.find(c => c.name === "helix:status")!.execute({
      sessionID: "native-owner-alpha", prompt: { text: "literal @@HELIXHARNESS_SESSION_ID@@" },
    });
    expect(captured.prompts[0].text).toContain("native_session_id: `native-owner-alpha`");
    expect(captured.prompts[0].text).toContain("literal @@HELIXHARNESS_SESSION_ID@@");
  });

  test("native skill results bind each request's identity without caching", async () => {
    const { ctx, captured } = createContext();
    await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
    const body = captured.skills.find(s => s.id === "helix-d-d2c")!.content;
    for (const sessionID of ["native-owner-alpha", "native-owner-beta"]) {
      const event: any = { tool: "skill", sessionID, status: "completed", result: { content: body } };
      for (const hook of captured.toolHooks) await hook(event);
      expect(event.result.content).toContain(`native_session_id: \`${sessionID}\``);
      expect(event.result.content).not.toContain("@@HELIXHARNESS_SESSION_ID@@");
    }
    expect(body).toContain("@@HELIXHARNESS_SESSION_ID@@");
  });

  for (const shape of ["string", "blocks"] as const) {
    test(`native skill ${shape} binds only complete owned bodies and preserves surrounding literals`, async () => {
      const { ctx, captured } = createContext();
      await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
      const body = captured.skills.find(s => s.id === "helix-d-d2c")!.content;
      const literal = `literal ${REPO_ROOT_TOKEN} @@HELIXHARNESS_SESSION_ID@@`;
      const text = `${literal}\n${body}\n${literal}`;
      const event: any = {tool:"skill",sessionID:"owned-native",status:"completed",
        result:{content:shape === "string" ? text : [{type:"text",text},{type:"text",text:literal}]}};
      await captured.toolHooks[0](event);
      const bound = shape === "string" ? event.result.content : event.result.content[0].text;
      expect(bound).toContain("native_session_id: `owned-native`");
      expect(bound.startsWith(literal)).toBe(true);
      expect(bound.endsWith(literal)).toBe(true);
      if (shape === "blocks") expect(event.result.content[1].text).toBe(literal);
    });
    test(`unrelated skill ${shape} token literals do not trigger lookup or mutation`, async () => {
      const { ctx, captured } = createContext({getSession:async()=>{throw new Error("must not resolve");}});
      await registerHelixHarness(ctx, {pluginRoot:PLUGIN_ROOT});
      const literal = `other skill ${REPO_ROOT_TOKEN} @@HELIXHARNESS_SESSION_ID@@`;
      const event:any={tool:"skill",sessionID:"unrelated",status:"completed",
        result:{content:shape === "string" ? literal : [{type:"text",text:literal}]}};
      const before=JSON.stringify(event);
      await captured.toolHooks[0](event);
      expect(JSON.stringify(event)).toBe(before);
      expect(captured.sessionGets).toEqual([]);
    });
  }

  test("mention binding changes only owned bodies and keeps source messages intact", async () => {
    const { ctx, captured } = createContext();
    await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
    const body = captured.skills.find(s => s.id === "helix-d-d2c")!.content;
    const source = { role: "user", content: body + "\nliteral @@HELIXHARNESS_SESSION_ID@@" };
    for (const sessionID of ["native-mention-alpha", "native-mention-beta"]) {
      const event: any = { sessionID, system: [], messages: [source] };
      await captured.sessionHooks.find(h => h.name === "context")!.callback(event);
      expect(event.messages[0].content).toContain(`native_session_id: \`${sessionID}\``);
      expect(event.messages[0].content).toContain("literal @@HELIXHARNESS_SESSION_ID@@");
    }
    expect(source.content).toBe(body + "\nliteral @@HELIXHARNESS_SESSION_ID@@");
  });
});

describe("allowlisted native preflight observations", () => {
  test("mutating commands observe actual tool metadata without exposing unrelated configuration", async () => {
    const { ctx, captured } = createContext();
    const queries: unknown[] = [];
    (ctx.tool as any).list = async (input: unknown) => {
      queries.push(input);
      return [{ id: "shell", headers: { token: "synthetic-do-not-expose" } }, { id: "read" }, { id: "subagent" }, { id: "unrelated_private_tool" }];
    };
    (ctx.session as any).move = async () => { throw new Error("preflight must not move a session"); };
    await registerHelixHarness(ctx, { pluginRoot: PLUGIN_ROOT });
    await captured.commands.find(c => c.name === "helix:apply")!.execute({sessionID:"preflight-current",prompt:{text:"demo"}});
    expect(queries).toEqual([{sessionID:"preflight-current"}]);
    const prompt = captured.prompts[0].text;
    expect(prompt).toContain('registered_native_tools: `["read","shell","subagent"]`');
    expect(prompt).toContain('native_session_move_api: `available`');
    expect(prompt).toContain('native_delegation_depth: `unknown`');
    expect(prompt).not.toContain("synthetic-do-not-expose");
    expect(prompt).not.toContain("unrelated_private_tool");
  });
  test("an unavailable catalogue stays unknown and does not leak native error details", async () => {
    const {ctx,captured}=createContext();
    (ctx.tool as any).list=async()=>{throw new Error("synthetic-do-not-expose");};
    await registerHelixHarness(ctx,{pluginRoot:PLUGIN_ROOT});
    await captured.commands.find(c=>c.name==="helix:ut")!.execute({sessionID:"preflight-current",prompt:{text:""}});
    expect(captured.prompts[0].text).toContain('native_tool_catalog: `unavailable`');
    expect(captured.prompts[0].text).not.toContain("synthetic-do-not-expose");
  });
  test("Status and config do not depend on the capability lookup", async () => {
    const {ctx,captured}=createContext();let calls=0;
    (ctx.tool as any).list=async()=>{calls++;throw new Error("must not query");};
    await registerHelixHarness(ctx,{pluginRoot:PLUGIN_ROOT});
    for(const name of ["status","config"])await captured.commands.find(c=>c.name==="helix:"+name)!.execute({sessionID:"read-only",prompt:{text:""}});
    expect(calls).toBe(0);expect(captured.prompts).toHaveLength(2);
  });
});

test("known missing mandatory execution tools reject mutation commands before prompt admission", async () => {
  const {ctx,captured}=createContext();
  (ctx.tool as any).list=async()=>[{id:"read"}];
  await registerHelixHarness(ctx,{pluginRoot:PLUGIN_ROOT});
  await expect(captured.commands.find(c=>c.name==="helix:apply")!.execute({sessionID:"missing-shell",prompt:{text:"demo"}})).rejects.toThrow("opencode_preflight_tools_unavailable: shell");
  expect(captured.prompts).toHaveLength(0);
});

test("unrecognized native catalogue shapes are unknown rather than falsely missing tools", async () => {
  const {ctx,captured}=createContext();
  (ctx.tool as any).list=async()=>[{displayName:"future-tool-shape"}];
  await registerHelixHarness(ctx,{pluginRoot:PLUGIN_ROOT});
  await captured.commands.find(c=>c.name==="helix:apply")!.execute({sessionID:"future-catalogue",prompt:{text:"demo"}});
  expect(captured.prompts[0].text).toContain('native_tool_catalog: `unavailable`');
});
