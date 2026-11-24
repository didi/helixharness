/** Native edit notifications reuse the generated project linters; no lifecycle inference. */
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";

export interface CheckEvent {
  tool: string;
  status: "completed" | "error";
  sessionID: string;
  id?: string;
  input?: unknown;
  result?: { content?: string | ReadonlyArray<{ type: "text" | "file"; text?: string }> };
}
interface CheckResult { code: number; output: string; }

export async function runBounded(command: string[], options: { timeoutMs?: number; maxBytes?: number } = {}): Promise<CheckResult> {
  return new Promise(resolve => {
    const limit = options.maxBytes ?? 12 * 1024;
    let size = 0;
    const chunks: Buffer[] = [];
    let reason = "";
    const child = spawn(command[0]!, command.slice(1), { detached: process.platform !== "win32", stdio: ["ignore", "pipe", "pipe"] });
    const stop = () => {
      try { if (process.platform !== "win32" && child.pid) process.kill(-child.pid, "SIGKILL"); else child.kill("SIGKILL"); } catch { /* already exited */ }
    };
    const timer = setTimeout(() => { reason = "timeout"; stop(); }, options.timeoutMs ?? 5000);
    const drain = (data: Buffer) => {
      const available = Math.max(0, limit - size);
      chunks.push(data.subarray(0, available));
      size += Math.min(data.length, available);
      if (data.length > available) { reason = "output_limit"; stop(); }
    };
    child.stdout.on("data", drain); child.stderr.on("data", drain);
    child.on("error", () => { clearTimeout(timer); resolve({ code: 1, output: "runtime_unavailable" }); });
    child.on("close", code => {
      clearTimeout(timer); stop();
      resolve({ code: reason ? 1 : code ?? 1, output: Buffer.concat(chunks).toString("utf8") + (reason ? `\n${reason}` : "") });
    });
  });
}

function append(event: CheckEvent, text: string): void {
  if (!event.result) return;
  const content = event.result.content;
  event.result.content = typeof content === "string" ? `${content}\n\n${text}` :
    [...(content ?? []), { type: "text", text }];
}

function contained(root: string, target: string): boolean {
  const relative = path.relative(root, target);
  return relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}

export async function runProjectChecks(event: CheckEvent, pluginRoot: string,
  resolveRoot: () => Promise<string>, run: (command: string[]) => Promise<CheckResult> = runBounded): Promise<void> {
  if (event.status !== "completed" || !["write", "edit", "patch"].includes(event.tool)) return;
  try {
    const root = await resolveRoot();
    const input = event.input as { path?: unknown } | undefined;
    if (typeof input?.path === "string" && !contained(root, path.resolve(root, input.path))) return;
    const folder = path.join(root, "docs/harness/linters");
    if (!fs.existsSync(folder)) return; // Uninitialized repositories are unaffected.
    if (!contained(root, fs.realpathSync(folder))) {
      append(event, "WARN:project_check_unavailable: linter directory escapes the repository."); return;
    }
    const failures: string[] = [];
    for (const name of ["invariant-check", "spec-sync-check"]) {
      const candidates = [path.join(folder, `${name}.py`), path.join(folder, `${name}.sh`)];
      const script = candidates.find(candidate => fs.existsSync(candidate));
      if (!script || !fs.statSync(script).isFile() || !contained(root, fs.realpathSync(script))) {
        failures.push(`${name}: missing or unsafe project linter`); continue;
      }
      const result = await run([process.env.HELIXHARNESS_OPENCODE_PYTHON || "python3", "-X", "utf8",
        path.join(pluginRoot, "scripts/project_linter.py"), "--repo-root", root, "--linter", name]);
      if (result.code !== 0) failures.push(`${name}: exit=${result.code}\n${result.output.slice(0, 6000)}`);
    }
    if (failures.length) append(event, `WARN:project_check_failed\n${failures.join("\n")}\nRepair the findings and rerun the project checks. This edit notification does not complete a workflow stage.`);
  } catch {
    // Optional diagnostics never expose native API errors or change edit success.
    append(event, "WARN:project_check_unavailable: project checks could not run; run them explicitly before stage completion.");
  }
}
