import { describe, expect, test } from "bun:test";
import path from "node:path";

import {
  COMMAND_NAMES,
  HOST_ID,
  REPO_ROOT_TOKEN,
  buildAgentSystem,
  buildCommandPrompt,
  buildSkillContent,
  buildWorkerSystem,
  hostContextFor,
  loadPluginResources,
  parseFrontmatter,
  parseMarkdown,
  replaceRepoRootToken,
  shellQuote,
  substitutePlaceholders,
  substituteSkillPlaceholders,
} from "../core.ts";

const PLUGIN_ROOT = path.resolve(import.meta.dir, "../..");

describe("frontmatter", () => {
  test("parses flat scalar fields and ignores nested maps", () => {
    const source = [
      "---",
      'name: "example"',
      "description: 'single quoted'",
      "disable-model-invocation: true",
      "hooks:",
      "  PreToolUse:",
      "    - matcher: AskUserQuestion",
      "---",
      "",
      "# Body",
      "",
    ].join("\n");
    const document = parseMarkdown(source, "/tmp/example.md");
    expect(document.frontmatter).toEqual({
      name: "example",
      description: "single quoted",
      "disable-model-invocation": true,
    });
    expect(document.body).toStartWith("\n# Body");
  });

  test("returns the whole document when frontmatter is absent", () => {
    expect(parseFrontmatter("description: plain")).toEqual({
      description: "plain",
    });
  });
});

describe("plugin resources", () => {
  test("loads every public command, skill and agent from the package root", () => {
    const plugin = loadPluginResources(PLUGIN_ROOT);

    expect(plugin.pluginRoot).toBe(PLUGIN_ROOT);
    expect(plugin.manifest.host_id).toBe(HOST_ID);
    expect(plugin.manifest.host_api_version).toBe("1.1");
    expect(plugin.commands.map((command) => command.name)).toEqual([
      ...COMMAND_NAMES,
    ]);
    expect(plugin.commands.every((command) => command.description.length > 0)).toBe(
      true,
    );
    expect(plugin.skills.map((skill) => skill.id)).toContain("helix-code-review");
    expect(plugin.skills.map((skill) => skill.id)).toContain("helix-feedback");
    expect(plugin.agents.map((agent) => agent.id)).toContain("helix-scan-tech");
    expect(plugin.agents.map((agent) => agent.id)).toContain(
      "helix-unit-test-worker",
    );
    expect(plugin.skills.length).toBeGreaterThanOrEqual(8);
    expect(plugin.agents.length).toBeGreaterThanOrEqual(16);
  });

  test("resolves required resources to absolute files inside the plugin", () => {
    const plugin = loadPluginResources(PLUGIN_ROOT);
    for (const [name, resource] of Object.entries(plugin.resources)) {
      expect(path.isAbsolute(resource), name).toBe(true);
      expect(resource.startsWith(PLUGIN_ROOT), name).toBe(true);
    }
    expect(plugin.resources.runtime).toBe(
      path.join(PLUGIN_ROOT, "hosts/opencode/runtime.md"),
    );
    expect(plugin.resources.telemetry_protocol).toBe(
      path.join(PLUGIN_ROOT, "hosts/shared/telemetry.md"),
    );
    expect(plugin.capabilities.command_surface).toBe("native");
    expect(plugin.capabilities.subagents).toBe("native");
    expect(plugin.capabilities.telemetry).toBe("unsupported");
    expect(plugin.capabilities.plugin_update).toBe("unsupported");
  });

  test("maps disable-model-invocation to OpenCode autoinvoke=false", () => {
    const plugin = loadPluginResources(PLUGIN_ROOT);
    const brainstorming = plugin.skills.find(
      (skill) => skill.id === "helix-brainstorming",
    );
    const codeReview = plugin.skills.find(
      (skill) => skill.id === "helix-code-review",
    );
    expect(brainstorming?.autoinvoke).toBe(false);
    expect(codeReview?.autoinvoke).toBe(true);
  });
});

describe("rendering", () => {
  test("renders a pre-bound context with every capability and resource", () => {
    const plugin = loadPluginResources(PLUGIN_ROOT);
    const context = hostContextFor(plugin, "/repo");
    const text = buildCommandPrompt(
      plugin.commands.find((command) => command.name === "apply")!,
      context,
      "--feature demo",
    );

    expect(text).toContain("# HelixHarness Host Context");
    expect(text).toContain("canonical_host_id: `opencode`");
    expect(text).toContain(`plugin_root: \`${PLUGIN_ROOT}\``);
    expect(text).toContain("repo_root: `/repo`");
    expect(text).toContain("`helix-<agent>`");
    expect(text).toContain("`workflow_transition` is `inline`");
    expect(text).toContain("`update_plan` has no native OpenCode plan tool");
    expect(text).toContain("HelixHarness 公共命令唯一契约");
    expect(text).toContain("--arguments='--feature demo'");
  });

  test("substitutes command placeholders once and keeps user arguments literal", () => {
    const plugin = loadPluginResources(PLUGIN_ROOT);
    const context = hostContextFor(plugin, "/repo");
    const literal = "--note {plugin_root} $ARGUMENTS";

    expect(substitutePlaceholders("$ARGUMENTS", context, literal)).toBe(literal);
    expect(
      substitutePlaceholders(
        "python3 {plugin_root}/scripts/x.py --repo-root {repo_root} {SHELL_QUOTED_ARGUMENTS}",
        context,
        literal,
      ),
    ).toBe(
      `python3 ${PLUGIN_ROOT}/scripts/x.py --repo-root /repo ${shellQuote(literal)}`,
    );
  });

  test("shell quoting is single-argument safe", () => {
    expect(shellQuote("")).toBe("''");
    expect(shellQuote("plain")).toBe("'plain'");
    expect(shellQuote("it's")).toBe("'it'\\''s'");
  });

  test("method skills carry a repo-root marker until the tool hook binds the session", () => {
    const plugin = loadPluginResources(PLUGIN_ROOT);
    const skill = plugin.skills.find((item) => item.id === "helix-feedback")!;
    const content = buildSkillContent(skill, plugin);

    expect(content).toContain(REPO_ROOT_TOKEN);
    expect(content).toContain("canonical_host_id: `opencode`");
    expect(content).toContain("$ARGUMENTS");
    expect(content).toContain("`opencode`");
    expect(replaceRepoRootToken(content, "/work/repo")).toContain(
      "repo_root: `/work/repo`",
    );
    expect(replaceRepoRootToken(content, "/work/repo")).not.toContain(
      REPO_ROOT_TOKEN,
    );
  });

  test("skill placeholder substitution keeps the skill argument convention", () => {
    const plugin = loadPluginResources(PLUGIN_ROOT);
    const context = hostContextFor(plugin, "/repo");
    expect(
      substituteSkillPlaceholders(
        "$ARGUMENTS {plugin_root} {repo_root} {current_platform}",
        context,
      ),
    ).toBe(`$ARGUMENTS ${PLUGIN_ROOT} /repo opencode`);
  });

  test("agent systems bind the plugin and resources but leave repo_root to dispatch", () => {
    const plugin = loadPluginResources(PLUGIN_ROOT);
    const agent = plugin.agents.find((item) => item.id === "helix-scan-tech")!;
    const system = buildAgentSystem(agent, plugin);

    expect(system).toContain("canonical_host_id: `opencode`");
    expect(system).toContain("The dispatch prompt supplies the authoritative `repo_root`");
    expect(system).not.toContain("repo_root: `");
    expect(system).toContain("任务：生成项目技术栈与架构全景文档");
  });

  test("the delegating worker role stays leaf unless the dispatch allows delegation", () => {
    const plugin = loadPluginResources(PLUGIN_ROOT);
    const system = buildWorkerSystem(plugin);

    expect(system).toContain("canonical_host_id: `opencode`");
    expect(system).toContain("`delegation_policy=may_delegate`");
    expect(system).toContain("Do not write shared indexes");
  });
});
