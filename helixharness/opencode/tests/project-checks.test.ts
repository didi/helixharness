import { afterEach, expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { runProjectChecks, runBounded } from "../project-checks.ts";

const roots: string[] = [];
afterEach(() => { for (const root of roots.splice(0)) fs.rmSync(root, { recursive: true, force: true }); });
function fixture() {
  const root = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), "helix-project-check-"));
  roots.push(root);
  const linters = path.join(root, "docs/harness/linters");
  fs.mkdirSync(linters, { recursive: true });
  for (const name of ["invariant-check", "spec-sync-check"]) fs.writeFileSync(path.join(linters, `${name}.py`), "print('synthetic')");
  return root;
}
const pluginRoot = path.resolve(import.meta.dir, "../..");

test("successful edit runs both linters against the requesting session root", async () => {
  const root = fixture();
  const commands: string[][] = [];
  const event: any = { tool: "edit", status: "completed", sessionID: "ses_A", input: { path: "src/a.ts" }, result: { content: "edited" } };
  await runProjectChecks(event, pluginRoot, async () => root, async command => {
    commands.push(command); return { code: 0, output: "synthetic passed" };
  });
  expect(commands).toHaveLength(2);
  expect(commands.every(command => command.includes(root))).toBe(true);
  expect(event.result.content).toBe("edited");
});

test("failed tools, non-edits and repositories without linters do not launch checks", async () => {
  let launches = 0;
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "helix-no-linters-")); roots.push(root);
  for (const event of [
    { tool: "edit", status: "error", sessionID: "ses_A" },
    { tool: "read", status: "completed", sessionID: "ses_A" },
    { tool: "write", status: "completed", sessionID: "ses_A", input: { path: "a.ts" } },
  ]) await runProjectChecks(event as any, pluginRoot, async () => root, async () => { launches++; return { code: 0, output: "" }; });
  expect(launches).toBe(0);
});

test("linter failure is attached without undoing the edit or completing a stage", async () => {
  const root = fixture();
  const event: any = { tool: "write", status: "completed", sessionID: "ses_A", input: { path: "a.ts" }, result: { content: [{ type: "text", text: "saved" }] } };
  await runProjectChecks(event, pluginRoot, async () => root, async () => ({ code: 1, output: "synthetic invariant violated" }));
  expect(event.status).toBe("completed");
  expect(event.result.content[0].text).toBe("saved");
  expect(event.result.content.at(-1).text).toContain("project_check_failed");
});

test("foreign file and external linter symlinks never execute", async () => {
  const root = fixture(); const foreign = fixture();
  for (const name of ["invariant-check", "spec-sync-check"]) {
    const target = path.join(root, `docs/harness/linters/${name}.py`);
    fs.unlinkSync(target); fs.symlinkSync(path.join(foreign, `docs/harness/linters/${name}.py`), target);
  }
  let launches = 0;
  for (const file of ["a.ts", path.join(foreign, "a.ts")]) {
    await runProjectChecks({ tool: "write", status: "completed", sessionID: "ses_A", input: { path: file }, result: { content: "saved" } } as any,
      pluginRoot, async () => root, async () => { launches++; return { code: 0, output: "" }; });
  }
  expect(launches).toBe(0);
});

test("bounded runner preserves nonzero exit, bounds output and terminates timeouts", async () => {
  const failed = await runBounded([process.execPath, "-e", "process.stdout.write('synthetic');process.exit(7)"], { timeoutMs: 3000 });
  expect(failed.code).toBe(7); expect(failed.output).toContain("synthetic");
  const oversized = await runBounded([process.execPath, "-e", "process.stdout.write('x'.repeat(50000))"], { maxBytes: 500, timeoutMs: 3000 });
  expect(oversized.code).not.toBe(0); expect(oversized.output.length).toBeLessThan(800);
  const slow = await runBounded([process.execPath, "-e", "setTimeout(()=>{}, 100000)"], { timeoutMs: 60 });
  expect(slow.code).not.toBe(0); expect(slow.output).toContain("timeout");
});
