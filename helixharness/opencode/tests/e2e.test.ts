import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const PLUGIN_ROOT = path.resolve(import.meta.dir, "../..");
const ENABLED = process.env.HELIXHARNESS_OPENCODE_E2E === "1";
const PASSWORD = "helix-e2e-password";

function authorization(): string {
  return `Basic ${btoa(`opencode:${PASSWORD}`)}`;
}

async function request(
  port: number,
  route: string,
  init: RequestInit = {},
): Promise<Response> {
  return fetch(`http://127.0.0.1:${port}${route}`, {
    ...init,
    headers: {
      authorization: authorization(),
      "content-type": "application/json",
      ...(init.headers ?? {}),
    },
  });
}

async function waitForServer(port: number): Promise<void> {
  const deadline = Date.now() + 30_000;
  let lastError: unknown;
  while (Date.now() < deadline) {
    try {
      // The agent registry request also activates the location's plugins.
      const response = await request(port, "/api/agent");
      if (response.ok) return;
      lastError = new Error(`HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`OpenCode server did not become ready: ${String(lastError)}`);
}

describe.skipIf(!ENABLED)("installed OpenCode plugin", () => {
  let directory = "";
  let port = 0;
  let server: ReturnType<typeof Bun.spawn> | undefined;

  beforeAll(async () => {
    directory = fs.mkdtempSync(path.join(os.tmpdir(), "helix-opencode-e2e-"));
    fs.writeFileSync(
      path.join(directory, "opencode.json"),
      `${JSON.stringify(
        {
          $schema: "https://opencode.ai/config.json",
          plugins: [PLUGIN_ROOT],
        },
        null,
        2,
      )}\n`,
    );
    port = 5200 + Math.floor(Math.random() * 300);
    server = Bun.spawn(
      ["opencode", "serve", "--port", String(port), "--print-logs"],
      {
        cwd: directory,
        env: { ...process.env, OPENCODE_PASSWORD: PASSWORD },
        stdout: "ignore",
        stderr: "ignore",
      },
    );
    await waitForServer(port);
  }, 45_000);

  afterAll(() => {
    server?.kill();
    if (directory) fs.rmSync(directory, { recursive: true, force: true });
  });

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
});
