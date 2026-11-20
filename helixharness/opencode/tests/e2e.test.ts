import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import fs from "node:fs";
import path from "node:path";
import { eventually, startNativeFixture } from "./native-fixture.ts";

const PLUGIN_ROOT = path.resolve(import.meta.dir, "../..");
const ENABLED = process.env.HELIXHARNESS_OPENCODE_E2E === "1";
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
