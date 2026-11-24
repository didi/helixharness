import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { REPO_ROOT_TOKEN } from "../core.ts";
import { eventually, startNativeFixture } from "./native-fixture.ts";

const PLUGIN_ROOT = path.resolve(import.meta.dir, "../..");
const ENABLED = process.env.HELIXHARNESS_OPENCODE_E2E === "1";

describe.skipIf(!ENABLED)("native optional project checks and stage identity", () => {
  let native: Awaited<ReturnType<typeof startNativeFixture>>;
  beforeAll(async () => { native = await startNativeFixture(PLUGIN_ROOT, 2, undefined, true); }, 30_000);
  afterAll(async () => { await native?.stop(); });

  test("real edit completes and returns generated linter failure to the model", async () => {
    const folder = path.join(native.directory, "docs/harness/linters");
    fs.mkdirSync(folder, { recursive: true });
    fs.writeFileSync(path.join(folder, "invariant-check.py"), "print('synthetic invariant finding');raise SystemExit(1)\n");
    fs.writeFileSync(path.join(folder, "spec-sync-check.py"), "print('synthetic spec passed')\n");
    const session = await native.session("Synthetic project checks");
    await native.prompt(session.id, "HELIX_E2E_PROJECT_CHECK");
    const row = await eventually(async () => native.observations.find(item => item.tag === "HELIX_E2E_PROJECT_CHECK" && item.results.includes("project_check_failed")), Boolean);
    expect(row!.results).toContain("synthetic invariant finding");
    expect(fs.readFileSync(path.join(native.directory, "synthetic-edited.txt"), "utf8")).toBe("synthetic edit");
  }, 30_000);

  test("real shell hook supplies only its requesting native session and directory", async () => {
    const session = await native.session("Synthetic telemetry identity");
    await native.prompt(session.id, "HELIX_E2E_TELEMETRY_BINDING");
    const row = await eventually(async () => native.observations.find(item => item.tag === "HELIX_E2E_TELEMETRY_BINDING" && item.results.includes("HELIXHARNESS_SESSION_CWD")), Boolean);
    expect(row!.results).toContain(session.id);
    expect(row!.results).toContain(fs.realpathSync(native.directory));
    expect(row!.results).toContain(path.join(native.directory, "synthetic-host-state"));
  }, 30_000);

  test("real native shell rechecks consent and records only the authorized stage", async () => {
    expect(Bun.spawnSync(["git", "-c", "core.hooksPath=/dev/null", "init", "-q", native.directory]).exitCode).toBe(0);
    const home = path.join(native.directory, "synthetic-telemetry");
    fs.mkdirSync(home, { recursive: true });
    fs.writeFileSync(path.join(home, "config.json"), JSON.stringify({ endpoint: "disabled-for-synthetic-test" }));
    const session = await native.session("Synthetic stage lifecycle");
    await native.prompt(session.id, "HELIX_E2E_TELEMETRY_LIFECYCLE");
    const row = await eventually(async () => native.observations.find(item => item.tag === "HELIX_E2E_TELEMETRY_LIFECYCLE" && item.results !== "[]"), Boolean);
    expect(row!.results).toContain("recorded");
    expect(row!.results).toContain("telemetry_disabled");
    expect(row!.results).toContain("started");
    const ledger = JSON.parse(fs.readFileSync(path.join(native.directory, "docs/eval/native-telemetry/eval-log.json"), "utf8"));
    expect(ledger.entries).toHaveLength(1);
    expect(ledger.entries[0].status).toBe("completed");
    expect(ledger.entries[0].session_hash).toBe("session-" + createHash("sha256").update(session.id).digest("hex").slice(0, 32));
  }, 30_000);
});
let fixture: Awaited<ReturnType<typeof startNativeFixture>>;

async function request(_port: number, route: string, init: RequestInit = {}): Promise<Response> {
  return fixture.request(route, init.body ? JSON.parse(String(init.body)) : undefined);
}

describe.skipIf(!ENABLED)("installed OpenCode plugin", () => {
  let directory = "";
  const port = 0;

  beforeAll(async () => {
    fixture = await startNativeFixture(PLUGIN_ROOT);
    directory = fixture.directory;
  }, 30_000);

  afterAll(async () => { await fixture?.stop(); });

  test("registers commands, method skills and subagent roles", async () => {
    const commands = (await (await request(port, "/api/command")).json()) as {
      data: Array<{ name: string }>;
    };
    expect(commands.data.map((command) => command.name)).toEqual(
      expect.arrayContaining([
        "helix:init",
        "helix:propose",
        "helix:apply",
        "helix:archive",
        "helix:bug-fix",
        "helix:ut",
        "helix:update",
        "helix:status",
        "helix:config",
      ]),
    );

    const skills = (await (await request(port, "/api/skill")).json()) as {
      data: Array<{ id: string; path: string; content: string }>;
    };
    const feedback = skills.data.find((skill) => skill.id === "helix-feedback");
    expect(feedback).toBeDefined();
    expect(feedback?.path).toBe(path.join(PLUGIN_ROOT, "skills/feedback/SKILL.md"));
    expect(feedback?.content).toContain("canonical_host_id: `opencode`");

    const agents = (await (await request(port, "/api/agent")).json()) as {
      data: Array<{ id: string; mode: string }>;
    };
    const agentIds = agents.data.map((agent) => agent.id);
    expect(agentIds).toEqual(
      expect.arrayContaining([
        "helix-scan-tech",
        "helix-scan-db",
        "helix-init-service-bootstrap",
        "helix-unit-test-worker",
        "helix-worker",
      ]),
    );
    expect(
      agents.data.filter((agent) => agent.id.startsWith("helix-")).every(
        (agent) => agent.mode === "subagent",
      ),
    ).toBe(true);
  }, 20_000);

  test("a native command admits the canonical contract with Host Context", async () => {
    const sessionResponse = await request(port, "/api/session", {
      method: "POST",
      body: JSON.stringify({
        title: "helix-e2e",
        model: { providerID: "mock", id: "none" },
      }),
    });
    const session = (await sessionResponse.json()) as {
      data: { id: string };
    };

    const commandResponse = await request(
      port,
      `/api/session/${session.data.id}/command`,
      {
        method: "POST",
        body: JSON.stringify({ name: "helix:status", text: "--help" }),
      },
    );
    expect(commandResponse.ok).toBe(true);

    const context = (await (
      await request(port, `/api/session/${session.data.id}/context`)
    ).json()) as {
      data: Array<{
        type?: string;
        metadata?: Record<string, unknown>;
        text?: string;
      }>;
    };
    const user = context.data.find(
      (message) => message.metadata?.["helixharness.command"] === "status",
    );
    expect(user?.text).toBeDefined();
    expect(user?.text).toContain("# HelixHarness command /helix:status");
    expect(user?.text).toContain("canonical_host_id: `opencode`");
    expect(user?.text).toContain(`plugin_root: \`${PLUGIN_ROOT}\``);
    expect(user?.text).toContain(`repo_root: \`${fs.realpathSync(directory)}\``);
    expect(user?.text).toContain("--help");
    expect(user?.text).toContain("HelixHarness 公共命令唯一契约");
    expect(user?.text).not.toContain("$ARGUMENTS");
    expect(user?.text).not.toContain("{plugin_root}");
    expect(user?.text).not.toContain("{repo_root}");
    expect(user?.text).not.toContain("{SHELL_QUOTED_ARGUMENTS}");
  }, 30_000);

  test("commands bind the requesting session rather than the plugin location", async () => {
    const target = path.join(directory, "repo with spaces");
    fs.mkdirSync(target);
    const session = await fixture.session("Synthetic second repo", target);
    const response = await fixture.request(`/api/session/${session.id}/command`, { name: "helix:status", text: "--help" });
    expect(response.ok).toBe(true);
    const context = JSON.stringify(await fixture.context(session.id));
    expect(context).toContain(`repo_root: \`${fs.realpathSync(target)}\``);
    expect(context).not.toContain(`repo_root: \`${fs.realpathSync(directory)}\``);
  }, 20_000);

  test("a vanished session directory rejects the command without admission", async () => {
    const target = path.join(directory, "deleted-command-repo");
    fs.mkdirSync(target);
    const session = await fixture.session("Synthetic deleted repo", target);
    // Activate this session's location before removing its directory.
    await fixture.request(`/api/session/${session.id}/command`, { name: "helix:status", text: "--help" });
    fs.rmSync(target, { recursive: true });
    const before = JSON.stringify(await fixture.context(session.id));
    const response = await fixture.request(`/api/session/${session.id}/command`, { name: "helix:apply", text: "synthetic-must-not-admit" });
    expect(response.ok).toBe(false);
    expect(await response.text()).toContain("repo_root_invalid");
    const after = JSON.stringify(await fixture.context(session.id));
    expect(after).not.toContain("synthetic-must-not-admit");
    expect(before).not.toContain("synthetic-must-not-admit");
  }, 20_000);

  test("native skill tool results bind the requesting repo before the next model call", async () => {
    const target = path.join(directory, "skill-repo");
    fs.mkdirSync(target);
    const session = await fixture.session("Synthetic skill", target);
    await fixture.prompt(session.id, "HELIX_E2E_SKILL");
    const observation = await eventually(async () => fixture.observations.find((x) => x.tag === "HELIX_E2E_SKILL" && x.results !== "[]"), Boolean);
    expect(observation!.results).toContain("HelixHarness Host Context");
    expect(observation!.results).toContain(`native_session_id: \`${session.id}\``);
    expect(observation!.results).toContain(fs.realpathSync(target));
    expect(observation!.results).not.toContain("@@HELIXHARNESS_REPO_ROOT@@");
  }, 20_000);

  test("native skill failure replaces the result instead of returning its original body", async () => {
    const target = path.join(directory, "deleted-skill-repo");
    fs.mkdirSync(target);
    const session = await fixture.session("Synthetic skill failure", target);
    fixture.deleteBeforeSkill(target);
    await fixture.prompt(session.id, "HELIX_E2E_MISSING_ROOT");
    const observation = await eventually(async () => fixture.observations.find((x) => x.tag === "HELIX_E2E_MISSING_ROOT" && x.results !== "[]"), Boolean);
    expect(observation!.results).toContain("BLOCKED: repo_root_invalid");
    expect(observation!.results).not.toContain("HelixHarness Host Context");
    expect(observation!.results).not.toContain("@@HELIXHARNESS_REPO_ROOT@@");
    expect(observation!.results).not.toContain("红灯");
  }, 20_000);

  test("the native worker can launch two independent leaf reviewers at depth two", async () => {
    const session = await fixture.session("Synthetic nested review");
    await fixture.prompt(session.id, "HELIX_E2E_PARENT");
    await eventually(async () => fixture.observations, (items) =>
      ["STANDARDS", "SPEC"].every((axis) => items.some((item) => item.tag === `HELIX_E2E_REVIEW_${axis}`)),
    );
    const worker = fixture.observations.find((x) => x.tag === "HELIX_E2E_WORKER")!;
    expect(worker.tools).toContain("subagent");
    for (const axis of ["STANDARDS", "SPEC"]) {
      const reviewer = fixture.observations.find((x) => x.tag === `HELIX_E2E_REVIEW_${axis}`)!;
      expect(reviewer).toBeDefined();
      const agents = await fixture.data("/api/agent");
      const general = agents.find((agent: any) => agent.id === "general");
      expect(general.permissions.some((rule: any) => rule.action === "subagent" && rule.effect === "deny")).toBe(true);
    }
  }, 20_000);

  test("leaf roles refuse further delegation even when nesting depth permits it", async () => {
    for (const role of ["GENERAL", "CANONICAL"]) {
      const session = await fixture.session(`Synthetic ${role} leaf`);
      await fixture.prompt(session.id, `HELIX_E2E_${role}_PARENT`);
      const leaf = await eventually(async () => fixture.observations.find((x) => x.tag === `HELIX_E2E_LEAF_${role}` && x.results !== "[]"), Boolean);
      expect(leaf!.results).toMatch(/denied|not allowed|No tool named/i);
    }
    expect(fixture.observations.some((x) => x.tag === "HELIX_E2E_FORBIDDEN_CHILD")).toBe(false);
  }, 20_000);

});


describe.skipIf(!ENABLED)("OpenCode default nesting limit", () => {
  test("depth one does not let the RepoJob start reviewer grandchildren", async () => {
    const limited = await startNativeFixture(PLUGIN_ROOT, 1);
    try {
      const session = await limited.session("Synthetic depth-one blocker");
      await limited.prompt(session.id, "HELIX_E2E_PARENT");
      const worker = await eventually(async () => limited.observations.find((x) => x.tag === "HELIX_E2E_WORKER" && x.results !== "[]"), Boolean);
      expect(worker!.results).toContain("Subagent depth limit reached (1)");
      const context = await eventually(async () => JSON.stringify(await limited.context(session.id)), (x) => x.includes("native_depth_unavailable"));
      expect(context).toContain("BLOCKED");
      expect(limited.observations.some((x) => x.tag.startsWith("HELIX_E2E_REVIEW_"))).toBe(false);
    } finally { await limited.stop(); }
  }, 30_000);
});


describe.skipIf(!ENABLED)("native Skill mention binding", () => {
  let native: Awaited<ReturnType<typeof startNativeFixture>>;
  beforeAll(async () => { native = await startNativeFixture(PLUGIN_ROOT); }, 30_000);
  afterAll(async () => { await native?.stop(); });

  async function observed(tag: string) {
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
      const result = native.observations.find(row => row.tag === tag);
      if (result) return result;
      await Bun.sleep(50);
    }
    throw new Error(`Synthetic model did not receive ${tag}`);
  }

  test("direct mentions bind each requesting session instead of the plugin location", async () => {
    const other = fs.mkdirSync(path.join(native.directory, "mention other $&"), { recursive: true })!;
    const a = await native.session("Synthetic mention A");
    const b = await native.session("Synthetic mention B", other);
    for (const [session, tag, expected] of [
      [a, "HELIX_E2E_MENTION_ALPHA", native.directory],
      [b, "HELIX_E2E_MENTION_BETA", other],
    ] as const) {
      await native.data(`/api/session/${session.id}/prompt`, { text: tag, skills: [{ id: "helix-tdd" }] });
      const row = await observed(tag);
      expect(row.messages.includes(REPO_ROOT_TOKEN)).toBe(false);
      expect(row.messages.includes(`repo_root: \`${fs.realpathSync(expected)}\``)).toBe(true);
      expect(row.results).toBe("[]");
    }
  }, 30_000);

  test("mention binding preserves literal markers in the user input", async () => {
    const session = await native.session("Synthetic literal mention");
    const tag = "HELIX_E2E_MENTION_LITERAL";
    await native.data(`/api/session/${session.id}/prompt`, {
      text: `${tag} User literal ${REPO_ROOT_TOKEN}`, skills: [{ id: "helix-tdd" }],
    });
    const row = await observed(tag);
    expect(row.messages.split(REPO_ROOT_TOKEN)).toHaveLength(2);
    expect(row.messages.includes(`repo_root: \`${fs.realpathSync(native.directory)}\``)).toBe(true);
  }, 20_000);

  test("command Skill attachments are bound before the model request", async () => {
    const session = await native.session("Synthetic command mention");
    const response = await native.request(`/api/session/${session.id}/command`, { name: "helix:status", text: "HELIX_E2E_MENTION_COMMAND", skills: [{ id: "helix-tdd" }] });
    expect(response.ok).toBe(true);
    const row = await observed("HELIX_E2E_MENTION_COMMAND");
    expect(row.messages.includes(REPO_ROOT_TOKEN)).toBe(false);
    expect(row.messages.includes(`repo_root: \`${fs.realpathSync(native.directory)}\``)).toBe(true);
  }, 20_000);

  test("native skill activation is bound without invoking the skill tool", async () => {
    const session = await native.session("Synthetic native activation");
    await native.data(`/api/session/${session.id}/prompt`, { text: "HELIX_E2E_MENTION_ACTIVATION", resume: false });
    const response = await native.request(`/api/experimental/session/${session.id}/skill`, { id: "helix-tdd" });
    expect(response.ok).toBe(true);
    const row = await observed("HELIX_E2E_MENTION_ACTIVATION");
    expect(row.messages.includes(REPO_ROOT_TOKEN)).toBe(false);
    expect(row.messages.includes(`repo_root: \`${fs.realpathSync(native.directory)}\``)).toBe(true);
    expect(row.results).toBe("[]");
  }, 20_000);
});


for (const fault of ["lookup", "invalid"] as const) {
  describe.skipIf(!ENABLED)(`native mention ${fault} failure`, () => {
    let native: Awaited<ReturnType<typeof startNativeFixture>>;
    beforeAll(async () => { native = await startNativeFixture(PLUGIN_ROOT, 2, fault); }, 30_000);
    afterAll(async () => { await native?.stop(); });

    test("binding failure aborts before the model receives the Skill", async () => {
      const session = await native.session("Synthetic mention lookup failure");
      await native.data(`/api/session/${session.id}/prompt`, {
        text: "HELIX_E2E_MENTION_FAILURE", skills: [{ id: "helix-tdd" }],
      });
      const deadline = Date.now() + 10_000;
      let info: any;
      while (Date.now() < deadline) {
        info = await native.data(`/api/session/${session.id}`);
        if (info.time.idle || native.observations.length) break;
        await Bun.sleep(50);
      }
      expect(info.outcome).toBe("failed");
      expect(native.observations).toHaveLength(0);
    }, 15_000);
  });
}


describe.skipIf(!ENABLED)("native UT atomic publication", () => {
  let stateFixture: Awaited<ReturnType<typeof startNativeFixture>>;
  beforeAll(async () => { stateFixture = await startNativeFixture(PLUGIN_ROOT, 2, undefined, true); }, 30_000);
  afterAll(async () => { await stateFixture?.stop(); });
  test("blocks first native direct write before it changes the target", async () => {
    const dir = path.join(stateFixture.directory, "docs/specs/active/acceptance");
    fs.mkdirSync(dir, {recursive:true});
    const target = path.join(dir, "unittest-summary.md");
    fs.writeFileSync(target, "previous complete report");
    const session = await stateFixture.session("Synthetic direct UT write");
    await stateFixture.request(`/api/session/${session.id}/prompt`, {text:"HELIX_E2E_UT_DIRECT_WRITE"});
    await eventually(async () => stateFixture.observations.some(x=>x.tag==="HELIX_E2E_UT_DIRECT_WRITE" && x.results!=="[]"), Boolean);
    expect(fs.readFileSync(target,"utf8")).toBe("previous complete report");
    expect(stateFixture.observations.some(x=>x.results.includes("ut_atomic_write_required"))).toBe(true);
  });
  test("blocks a native write through a file-link alias", async () => {
    const target = path.join(stateFixture.directory,"docs/specs/active/acceptance/unittest-summary.md");
    fs.writeFileSync(target,"complete alias target");
    fs.symlinkSync(target,path.join(stateFixture.directory,"ut-report-link.md"));
    const session=await stateFixture.session("Synthetic aliased UT write");
    await stateFixture.request(`/api/session/${session.id}/command`,{name:"helix:ut",text:"HELIX_E2E_UT_ALIAS_WRITE"});
    await eventually(async()=>stateFixture.observations.some(x=>x.tag==="HELIX_E2E_UT_ALIAS_WRITE" && x.results.includes("ut_atomic_write_required")),Boolean);
    expect(fs.readFileSync(target,"utf8")).toBe("complete alias target");
  });
  test("publishes staged content through the real helper", async () => {
    const session = await stateFixture.session("Synthetic atomic UT write");
    await stateFixture.request(`/api/session/${session.id}/prompt`, {text:"HELIX_E2E_UT_STAGED_WRITE"});
    await eventually(async () => stateFixture.observations.some(x=>x.tag==="HELIX_E2E_UT_STAGED_WRITE" && x.results.includes("atomic")), Boolean);
    const target = path.join(stateFixture.directory,"docs/specs/active/acceptance/unittest-summary.md");
    expect(fs.readFileSync(target,"utf8")).toBe("Synthetic UTF-8 report 验证\n");
    expect(fs.existsSync(target+".tmp")).toBe(false);
  });
});

// Actual filesystem transport regression: casing must never evade the reserved namespace.
describe.skipIf(!ENABLED)('native UT casing regression',()=>{
 let fixture:Awaited<ReturnType<typeof startNativeFixture>>;
 beforeAll(async()=>{fixture=await startNativeFixture(PLUGIN_ROOT,2,undefined,true);},30_000);
 afterAll(async()=>{await fixture?.stop();});
 test('blocks casing variants before write on the host filesystem',async()=>{
  const dir=path.join(fixture.directory,'docs/specs/active/acceptance');fs.mkdirSync(dir,{recursive:true});
  const target=path.join(dir,'unittest-summary.md');fs.writeFileSync(target,'private complete report',{mode:0o600});
  const caseAlias=path.join(fixture.directory,'DOCS/SPECS/ACTIVE/acceptance/UNITTEST-SUMMARY.MD');
  const caseInsensitive=fs.existsSync(caseAlias);
  const session=await fixture.session('Synthetic casing write');
  await fixture.request(`/api/session/${session.id}/prompt`,{text:'HELIX_E2E_UT_CASE_WRITE'});
  await eventually(async()=>fixture.observations.some(x=>x.tag==='HELIX_E2E_UT_CASE_WRITE' && x.results.includes('ut_atomic_write_required')),Boolean);
  expect(fs.readFileSync(target,'utf8')).toBe('private complete report');
  expect(fs.statSync(target).mode & 0o777).toBe(0o600);
  if(caseInsensitive)expect(fs.readFileSync(caseAlias,'utf8')).toBe('private complete report');
 });
});

for(const fault of ['lookup','invalid'] as const)describe.skipIf(!ENABLED)(`ordinary native write with ${fault} session API`,()=>{
 let fixture:Awaited<ReturnType<typeof startNativeFixture>>;
 beforeAll(async()=>{fixture=await startNativeFixture(PLUGIN_ROOT,2,fault,true);},30_000);
 afterAll(async()=>{await fixture?.stop();});
 test('keeps ordinary editor usable despite plugin directory lookup failure',async()=>{
  const session=await fixture.session('Synthetic ordinary editor');
  await fixture.request(`/api/session/${session.id}/prompt`,{text:'HELIX_E2E_ORDINARY_WRITE'});
  await eventually(async()=>fs.existsSync(path.join(fixture.directory,'ordinary.txt')),Boolean);
  expect(fs.readFileSync(path.join(fixture.directory,'ordinary.txt'),'utf8')).toBe('Synthetic UTF-8 report 验证\n');
 });
});


describe.skipIf(!ENABLED)("native entry preflight metadata", () => {
  test("mutation prompt has actual session identity and native catalogue observations", async () => {
    const native=await startNativeFixture(PLUGIN_ROOT,2,undefined,true);
    try {
      const session=await native.session("Synthetic preflight observation");
      const response=await native.request(`/api/session/${session.id}/command`,{name:"helix:apply",text:"synthetic"});
      expect(response.ok).toBe(true);
      const ctx=JSON.stringify(await native.context(session.id));
      expect(ctx).toContain(`native_session_id: \`${session.id}\``);
      expect(ctx).toContain("native_tool_catalog: `available`");
      expect(ctx).toContain("native_delegation_depth: `unknown`");
      expect(ctx).not.toContain("@@HELIXHARNESS_SESSION_ID@@");
    } finally {await native.stop();}
  },30000);
  test("missing required native tools prevent command admission and model delivery", async () => {
    const native=await startNativeFixture(PLUGIN_ROOT,2,undefined,true,"missing-shell");
    try {
      const session=await native.session("Synthetic unavailable shell");
      const response=await native.request(`/api/session/${session.id}/command`,{name:"helix:apply",text:"must-not-admit"});
      expect(response.ok).toBe(false);
      expect(await response.text()).toContain("opencode_preflight_tools_unavailable: shell");
      expect(JSON.stringify(await native.context(session.id))).not.toContain("must-not-admit");
      expect(native.observations).toHaveLength(0);
    } finally {await native.stop();}
  },30000);
});
