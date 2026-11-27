import { afterEach, expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { runProjectChecks, runBounded } from "../project-checks.ts";
import * as checks from "../project-checks.ts";

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

test("Windows project checks use the native shared launcher with literal arguments", () => {
  expect(typeof (checks as any).projectCheckCommand).toBe("function");
  const command = (checks as any).projectCheckCommand("C:\\Plugin Root", "C:\\Project Root", "invariant-check", "win32");
  expect(command[0]).toBe("powershell.exe");
  expect(command).toContain("-File"); expect(command.some((x: string) => x.endsWith("runtime_preflight.ps1"))).toBe(true);
  expect(command).toContain("exec"); expect(command).toContain("C:\\Project Root"); expect(command).not.toContain("sh");
});

test("successful edit runs both linters against the requesting session root", async () => {
  const root = fixture();
  const commands: string[][] = [];
  const event: any = { tool: "edit", status: "completed", sessionID: "ses_A", input: { path: "src/a.ts" }, result: { content: "edited" } };
  await runProjectChecks(event, pluginRoot, async () => root, async command => {
    commands.push(command); return { code: 0, output: "synthetic passed" };
  });
  expect(commands).toHaveLength(2);
  expect(commands.every(command => command.includes(root))).toBe(true);
  expect(commands.every(command => command.some(arg => /runtime_preflight\.(sh|ps1)$/.test(arg)) && command.includes("exec"))).toBe(true);
  expect(event.result.content).toBe("edited");
});

// POSIX fixture interpreters are deliberate test doubles; native acceptance uses
// actual 3.9 and 3.12 executables in a separate isolated OpenCode profile.
const posixTest = process.platform === "win32" ? test.skip : test;
function shellQuote(value: string): string { return "'" + value.replaceAll("'", "'\"'\"'") + "'"; }
async function mixedRuntime() {
  const root = fixture();
  const kit = path.join(root, "plugin"); const bin = path.join(root, "bin");
  fs.mkdirSync(path.join(kit, "scripts"), { recursive: true }); fs.mkdirSync(bin);
  for (const name of ["runtime_preflight.sh", "python_runtime.sh", "project_linter.py"]) {
    fs.copyFileSync(path.join(pluginRoot, "scripts", name), path.join(kit, "scripts", name));
  }
  const check = Bun.spawnSync(["sh", path.join(pluginRoot, "scripts/runtime_preflight.sh"), "check"]);
  const runtime = JSON.parse(check.stdout.toString()).python;
  expect(runtime.status).toBe("ready");
  const supported = "#!/bin/sh\nif [ \"$1\" = -c ]; then echo 3.12.13; exit 0; fi\nexec " +
    [runtime.executable, ...runtime.arguments].map(shellQuote).join(" ") + ' "$@"\n';
  const old = "#!/bin/sh\nif [ \"$1\" = -c ]; then echo 3.9.6; exit 0; fi\necho 'SyntaxError: synthetic old interpreter' >&2; exit 1\n";
  fs.writeFileSync(path.join(bin, "python3"), old, { mode: 0o755 });
  fs.writeFileSync(path.join(bin, "python3.12"), supported, { mode: 0o755 });
  fs.writeFileSync(path.join(bin, "uname"), "#!/bin/sh\necho Linux\n", { mode: 0o755 });
  for (const name of ["sh", "dirname", "git"]) fs.symlinkSync(Bun.which(name)!, path.join(bin, name));
  const log = path.join(root, "checked.jsonl");
  for (const name of ["invariant-check", "spec-sync-check"]) {
    fs.writeFileSync(path.join(root, `docs/harness/linters/${name}.py`),
      "import json,sys\nfrom pathlib import Path\nmatch 42:\n    case 42: pass\n" +
      `with Path(${JSON.stringify(log)}).open('a') as f: f.write(json.dumps(list(sys.version_info[:2]))+'\\n')\n`);
  }
  const event: any = { tool: "edit", status: "completed", sessionID: "ses_runtime", input: { path: "code.py" }, result: { content: "saved" } };
  const previousPath = process.env.PATH; const previousOverride = process.env.HELIXHARNESS_OPENCODE_PYTHON;
  process.env.PATH = bin; delete process.env.HELIXHARNESS_OPENCODE_PYTHON;
  return { root, kit, bin, log, event, restore() {
    process.env.PATH = previousPath;
    if (previousOverride === undefined) delete process.env.HELIXHARNESS_OPENCODE_PYTHON;
    else process.env.HELIXHARNESS_OPENCODE_PYTHON = previousOverride;
  } };
}

posixTest("old default Python is skipped for a supported versioned interpreter", async () => {
  const f = await mixedRuntime();
  try {
    await runProjectChecks(f.event, f.kit, async () => f.root);
    expect(f.event.result.content).toBe("saved"); expect(f.event.status).toBe("completed");
    const versions = fs.readFileSync(f.log, "utf8").trim().split("\n").map(line => JSON.parse(line));
    expect(versions).toHaveLength(2); expect(versions.every(v => v[0] >= 3 && v[1] >= 10)).toBe(true);
  } finally { f.restore(); }
});

posixTest("explicit supported interpreter with spaces is verified by the shared launcher", async () => {
  const f = await mixedRuntime();
  try {
    const preferred = path.join(f.bin, "Preferred Python 'quoted'");
    fs.copyFileSync(path.join(f.bin, "python3.12"), preferred); process.env.HELIXHARNESS_OPENCODE_PYTHON = preferred;
    await runProjectChecks(f.event, f.kit, async () => f.root);
    expect(f.event.result.content).toBe("saved"); expect(fs.readFileSync(f.log, "utf8").trim().split("\n")).toHaveLength(2);
  } finally { f.restore(); }
});

posixTest("explicit obsolete interpreter does not silently fall back or blame project syntax", async () => {
  const f = await mixedRuntime();
  try {
    process.env.HELIXHARNESS_OPENCODE_PYTHON = path.join(f.bin, "python3");
    await runProjectChecks(f.event, f.kit, async () => f.root);
    expect(f.event.result.content).toContain("project_check_unavailable");
    expect(f.event.result.content).not.toContain("project_check_failed");
    expect(f.event.result.content).not.toContain("SyntaxError");
    expect(fs.existsSync(f.log)).toBe(false); expect(f.event.status).toBe("completed");
  } finally { f.restore(); }
});

posixTest("no supported Python reports runtime unavailability and preserves the edit", async () => {
  const f = await mixedRuntime();
  try {
    fs.unlinkSync(path.join(f.bin, "python3.12"));
    await runProjectChecks(f.event, f.kit, async () => f.root);
    expect(f.event.result.content).toContain("project_check_unavailable");
    expect(f.event.result.content).not.toContain("project_check_failed");
    expect(f.event.status).toBe("completed"); expect(fs.existsSync(f.log)).toBe(false);
  } finally { f.restore(); }
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
