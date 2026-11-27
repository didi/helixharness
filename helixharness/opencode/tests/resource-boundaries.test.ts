import { afterAll, describe, expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { COMMAND_NAMES, loadPluginResources } from "../core.ts";

const PACKAGE = path.resolve(import.meta.dir, "../..");
const TEMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "helix-resource-boundaries-")));
afterAll(() => fs.rmSync(TEMP, { recursive: true, force: true }));
let sequence = 0;

function fixture() {
  const parent = path.join(TEMP, String(++sequence));
  const root = path.join(parent, "plugin");
  const outside = path.join(parent, "plugin-sidecar");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(outside);
  const manifest = JSON.parse(fs.readFileSync(path.join(PACKAGE, "hosts/opencode/host.json"), "utf8"));
  function write(relative: string, text: string) {
    const target = path.join(root, relative);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, text);
  }
  write("hosts/opencode/host.json", JSON.stringify(manifest));
  for (const ref of Object.values(manifest.adapters) as string[]) {
    if (ref.startsWith("resource:")) write(ref.slice("resource:".length), "Synthetic resource\n");
  }
  for (const command of COMMAND_NAMES) write(`commands/${command}.md`, "---\ndescription: Synthetic command\n---\nSynthetic body\n");
  write("skills/demo/SKILL.md", "---\nname: demo\n---\nSynthetic skill\n");
  write("agents/demo.md", "---\ndescription: Synthetic agent\n---\nSynthetic agent body\n");
  return { root, outside };
}

function moveToLink(root: string, relative: string, destination: string) {
  const source = path.join(root, relative);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  const directory = fs.statSync(source).isDirectory();
  fs.renameSync(source, destination);
  fs.symlinkSync(destination, source, directory ? "dir" : "file");
}

describe("canonical plugin resource containment", () => {
  for (const relative of [
    "hosts/opencode/host.json", "hosts/opencode/runtime.md", "commands/init.md",
    "skills/demo/SKILL.md", "agents/demo.md", "commands", "skills", "skills/demo", "agents",
  ]) {
    test(`rejects external links at ${relative} before loading resources`, () => {
      const { root, outside } = fixture();
      moveToLink(root, relative, path.join(outside, "target"));
      expect(() => loadPluginResources(root)).toThrow("escapes the plugin root");
    });
  }

  for (const relative of ["hosts/opencode/runtime.md", "commands/init.md", "skills/demo/SKILL.md", "agents/demo.md"]) {
    test(`allows an internal file link at ${relative}`, () => {
      const { root } = fixture();
      moveToLink(root, relative, path.join(root, "internal/target.md"));
      const plugin = loadPluginResources(root);
      expect(plugin.commands).toHaveLength(9);
      expect(plugin.skills.map(s => s.id)).toEqual(["helix-demo"]);
      expect(plugin.agents.map(a => a.id)).toEqual(["helix-demo"]);
    });
  }

  for (const relative of ["commands", "skills", "skills/demo", "agents"]) {
    test(`allows internal directory links at ${relative}`, () => {
      const { root } = fixture();
      moveToLink(root, relative, path.join(root, "internal/target"));
      const plugin = loadPluginResources(root);
      expect(plugin.commands).toHaveLength(9);
      expect(plugin.skills).toHaveLength(1);
      expect(plugin.agents).toHaveLength(1);
    });
  }

  for (const relative of ["hosts/opencode/runtime.md", "skills/demo/SKILL.md", "agents/demo.md"]) {
    test(`rejects a dangling link at ${relative}`, () => {
      const { root } = fixture();
      const file = path.join(root, relative);
      fs.unlinkSync(file);
      fs.symlinkSync(path.join(root, "missing.md"), file);
      expect(() => loadPluginResources(root)).toThrow();
    });
  }

  test("rejects link cycles", () => {
    const { root } = fixture();
    const first = path.join(root, "hosts/opencode/runtime.md");
    const second = path.join(root, "cycle.md");
    fs.unlinkSync(first);
    fs.symlinkSync(second, first);
    fs.symlinkSync(first, second);
    expect(() => loadPluginResources(root)).toThrow();
  });

  test("rejects a lexical traversal even when it resolves back inside", () => {
    const { root } = fixture();
    const manifestPath = path.join(root, "hosts/opencode/host.json");
    const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    fs.symlinkSync(root, path.join(path.dirname(root), "back"), "dir");
    manifest.adapters.runtime = "resource:../back/hosts/opencode/runtime.md";
    fs.writeFileSync(manifestPath, JSON.stringify(manifest));
    expect(() => loadPluginResources(root)).toThrow("escapes the plugin root");
  });
});
