/** Isolated real-server transport fixture. The model is scripted, never a live account. */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export interface ModelObservation {
  tag: string;
  tools: string[];
  results: string;
}

export async function eventually<T>(read: () => Promise<T>, ready: (value: T) => boolean): Promise<T> {
  const deadline = Date.now() + 15_000;
  while (true) {
    const value = await read();
    if (ready(value)) return value;
    if (Date.now() >= deadline) throw new Error("Synthetic native fixture did not reach the expected state");
    await Bun.sleep(50);
  }
}

export async function startNativeFixture(pluginRoot: string, depth = 2) {
  const executable = Bun.which(process.env.HELIXHARNESS_OPENCODE_BIN ?? "opencode");
  if (!executable) throw new Error("OpenCode E2E requires opencode or HELIXHARNESS_OPENCODE_BIN");
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "helix-opencode-e2e-"));
  const observations: ModelObservation[] = [];
  let removeBeforeSkill: string | undefined;
  const model = Bun.serve({
    hostname: "127.0.0.1",
    port: 0,
    async fetch(request) {
      const body = await request.json() as any;
      const tag = JSON.stringify(body.messages.filter((m: any) => m.role === "user"))
        .match(/HELIX_E2E_[A-Z_]+/)?.[0] ?? "command";
      const tools = (body.tools ?? []).map((tool: any) => tool.function.name) as string[];
      const results = JSON.stringify(body.messages.filter((m: any) => m.role === "tool"));
      observations.push({ tag, tools, results });
      const calls: Array<{ name: string; arguments: Record<string, unknown> }> = [];
      let text = results.includes("Subagent depth limit reached")
        ? "BLOCKED: native_depth_unavailable" : "Synthetic transport completed";
      if (/denied|not allowed|No tool named/i.test(results)) text = "BLOCKED: leaf_delegation_denied";
      if (results === "[]") {
        if (tag === "HELIX_E2E_SKILL" || tag === "HELIX_E2E_MISSING_ROOT") {
          if (removeBeforeSkill) {
            fs.rmSync(removeBeforeSkill, { recursive: true, force: true });
            removeBeforeSkill = undefined;
          }
          calls.push({ name: "skill", arguments: { id: "helix-tdd" } });
        } else if (tag === "HELIX_E2E_PARENT") {
          calls.push({ name: "subagent", arguments: {
            agent: "helix-worker", description: "Synthetic RepoJob",
            prompt: "HELIX_E2E_WORKER delegation_policy=may_delegate; dispatch two read-only leaf reviewers.",
          } });
        } else if (tag === "HELIX_E2E_WORKER") {
          for (const axis of ["STANDARDS", "SPEC"]) calls.push({ name: "subagent", arguments: {
            agent: "general", description: `Synthetic ${axis} review`,
            prompt: `HELIX_E2E_REVIEW_${axis} delegation_policy=leaf; read-only synthetic review.`,
          } });
        } else if (tag === "HELIX_E2E_GENERAL_PARENT" || tag === "HELIX_E2E_CANONICAL_PARENT") {
          const role = tag === "HELIX_E2E_GENERAL_PARENT" ? "general" : "helix-scan-tech";
          calls.push({ name: "subagent", arguments: {
            agent: role, description: "Synthetic leaf permission check",
            prompt: `HELIX_E2E_LEAF_${role === "general" ? "GENERAL" : "CANONICAL"} delegation_policy=leaf`,
          } });
        } else if (tag.startsWith("HELIX_E2E_LEAF_")) {
          calls.push({ name: "subagent", arguments: {
            agent: "general", description: "Must not run", prompt: "HELIX_E2E_FORBIDDEN_CHILD",
          } });
        }
      }
      const toolCalls = calls.map((call, index) => ({
        index, id: `synthetic-${observations.length}-${index}`, type: "function",
        function: { name: call.name, arguments: JSON.stringify(call.arguments) },
      }));
      const delta = calls.length ? { role: "assistant", tool_calls: toolCalls } : { role: "assistant", content: text };
      const chunks = [
        { choices: [{ index: 0, delta, finish_reason: null }] },
        { choices: [{ index: 0, delta: {}, finish_reason: calls.length ? "tool_calls" : "stop" }],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } },
      ].map((chunk) => ({ id: "synthetic", object: "chat.completion.chunk", model: "scripted", ...chunk }));
      return new Response(chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("") + "data: [DONE]\n\n", {
        headers: { "content-type": "text/event-stream" },
      });
    },
  });
  fs.writeFileSync(path.join(directory, "opencode.json"), JSON.stringify({
    plugins: [pluginRoot], model: "helix-test/scripted",
    providers: { "helix-test": {
      name: "Synthetic transport fixture", package: "@opencode/ai/providers/openai-compatible",
      settings: { baseURL: `http://127.0.0.1:${model.port}/v1`, apiKey: "synthetic" },
      models: { scripted: { name: "Synthetic", capabilities: { tools: true } } },
    } },
    permissions: [
      { action: "execute", resource: "*", effect: "deny" },
      { action: "skill", resource: "*", effect: "allow" },
    ],
    experimental: { subagent_depth: depth },
  }));
  const reservation = Bun.serve({ hostname: "127.0.0.1", port: 0, fetch: () => new Response() });
  const port = reservation.port;
  reservation.stop(true);
  const server = Bun.spawn([
    executable, "serve", "--port", String(port),
  ], {
    cwd: directory,
    env: {
      PATH: process.env.PATH ?? "", TMPDIR: os.tmpdir(), LANG: "en_US.UTF-8",
      ...(process.env.SystemRoot ? { SystemRoot: process.env.SystemRoot } : {}),
      OPENCODE_TEST_HOME: path.join(directory, "home"),
      XDG_CONFIG_HOME: path.join(directory, "config"), XDG_DATA_HOME: path.join(directory, "data"),
      XDG_CACHE_HOME: path.join(directory, "cache"), XDG_STATE_HOME: path.join(directory, "state"),
      OPENCODE_PASSWORD: "synthetic-e2e-password",
    },
    stdout: "ignore", stderr: "ignore",
  });
  async function request(route: string, body?: unknown) {
    return fetch(`http://127.0.0.1:${port}${route}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { authorization: `Basic ${btoa("opencode:synthetic-e2e-password")}`, "content-type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  }
  async function data(route: string, body?: unknown): Promise<any> {
    const response = await request(route, body);
    if (!response.ok) throw new Error(`Synthetic native request failed: ${response.status} ${route}`);
    return (await response.json() as any).data;
  }
  async function stop() {
    server.kill();
    await server.exited;
    model.stop(true);
    fs.rmSync(directory, { recursive: true, force: true });
  }
  try {
    await eventually(async () => {
      try {
        const agents = await data("/api/agent");
        return Array.isArray(agents) && agents.some((agent) => agent.id === "helix-worker");
      } catch { return false; }
    }, Boolean);
  } catch (error) {
    await stop();
    throw error;
  }
  return {
    directory, observations, request, data, stop,
    deleteBeforeSkill(target: string) { removeBeforeSkill = target; },
    async session(name: string, location?: string) {
      return data("/api/session", { title: name, model: { providerID: "helix-test", id: "scripted" },
        ...(location ? { location: { directory: location } } : {}) });
    },
    async prompt(sessionID: string, text: string) { await data(`/api/session/${sessionID}/prompt`, { text }); },
    async context(sessionID: string) { return data(`/api/session/${sessionID}/context`); },
  };
}
