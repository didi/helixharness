/** Test-only probe: import a real managed installation, never the checkout's runtime. */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { eventually, startNativeFixture } from "./native-fixture.ts";

const [root, workspace, mode] = process.argv.slice(2);
assert(root && workspace && ["setup", "native"].includes(mode!));
assert(!fs.existsSync(path.join(root, "node_modules")));
assert(!fs.existsSync(path.join(root, "opencode/tests")));

if (mode === "setup") {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
  assert.equal(manifest.exports["."], "./opencode/index.ts");
  const plugin = (await import(pathToFileURL(path.join(root, manifest.exports["."])).href)).default;
  const commands: any[] = [], skills: any[] = [], agents: string[] = [];
  const before: any[] = [], after: any[] = [], hooks: string[] = [], prompts: any[] = [];
  await plugin.setup({
    location: { directory: workspace },
    command: { async transform(callback: any) { callback({ add: (item: any) => commands.push(item) }); } },
    skill: { async transform(callback: any) { callback({ add: (item: any) => skills.push(item) }); } },
    agent: { async transform(callback: any) {
      callback({ update(id: string, update: any) {
        update({ name: id, mode: "primary", hidden: false, permissions: [] }); agents.push(id);
      } });
    } },
    tool: { async hook(name: string, callback: any) { (name === "execute.before" ? before : after).push(callback); } },
    session: {
      async hook(name: string) { hooks.push(name); },
      async get() { return { location: { directory: workspace } }; },
      async prompt(input: any) { prompts.push(input); },
    },
  });
  assert.equal(commands.length, 9); assert.equal(skills.length, 8); assert.equal(agents.length, 17);
  assert(agents.includes("helix-worker")); assert(hooks.includes("experimental.idle"));
  await commands.find(command => command.name === "helix:status").execute({ sessionID: "ses_bundle", prompt: { text: "--help" } });
  assert(prompts[0].text.includes(`plugin_root: \`${fs.realpathSync(root)}\``));
  let guarded = false;
  try {
    await before[0]({ tool: "write", sessionID: "ses_bundle", input: { path: "docs/specs/active/synthetic/unittest-summary.md" } });
  } catch (error) { guarded = String(error).includes("ut_atomic_write_required"); }
  assert(guarded);
  await after[0]({ tool: "write", sessionID: "ses_bundle", status: "completed", input: { path: "ordinary.txt" }, result: { content: "synthetic saved" } });
  console.log(JSON.stringify({ mode, commands: commands.length, skills: skills.length, agents: agents.length, ut_guard: true }));
} else {
  const native = await startNativeFixture(root, 2, undefined, true);
  try {
    const commands = await native.data("/api/command");
    const skills = await native.data("/api/skill");
    const agents = await native.data("/api/agent");
    assert.equal(commands.filter((item: any) => item.name.startsWith("helix:")).length, 9);
    assert.equal(skills.filter((item: any) => item.id.startsWith("helix-")).length, 8);
    assert(agents.some((item: any) => item.id === "helix-worker"));
    const folder = path.join(native.directory, "docs/harness/linters");
    fs.mkdirSync(folder, { recursive: true });
    fs.writeFileSync(path.join(folder, "invariant-check.py"), "print('synthetic bundle invariant');raise SystemExit(1)\n");
    fs.writeFileSync(path.join(folder, "spec-sync-check.py"), "print('synthetic bundle spec passed')\n");
    const project = await native.session("Synthetic managed bundle project check");
    await native.prompt(project.id, "HELIX_E2E_PROJECT_CHECK");
    const result = await eventually(async () => native.observations.find(item => item.tag === "HELIX_E2E_PROJECT_CHECK" && item.results.includes("project_check_failed")), Boolean);
    assert(result!.results.includes("synthetic bundle invariant"));
    const ut = await native.session("Synthetic managed bundle UT guard");
    await native.prompt(ut.id, "HELIX_E2E_UT_DIRECT_WRITE");
    await eventually(async () => native.observations.find(item => item.tag === "HELIX_E2E_UT_DIRECT_WRITE" && item.results.includes("ut_atomic_write_required")), Boolean);
    assert(!fs.existsSync(path.join(native.directory, "docs/specs/active/acceptance/unittest-summary.md")));
    console.log(JSON.stringify({ mode, native_registration: true, project_check: true, ut_guard: true }));
  } finally { await native.stop(); }
}
