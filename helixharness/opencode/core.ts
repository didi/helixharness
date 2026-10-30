/**
 * HelixHarness OpenCode plugin core.
 *
 * The canonical resources live at the plugin package root (`commands/`,
 * `skills/`, `agents/` and `hosts/`). This module reads them at startup and
 * renders deterministic registration payloads. It intentionally has no
 * dependency on the OpenCode runtime so it can be unit tested with plain Bun.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const HOST_ID = "opencode";
export const HOST_API_VERSION = "1.1";
export const PLUGIN_ID = "helixharness";
export const REPO_ROOT_TOKEN = "@@HELIXHARNESS_REPO_ROOT@@";
export const WORKER_AGENT_ID = "helix-worker";

export const COMMAND_NAMES = [
  "init",
  "propose",
  "apply",
  "archive",
  "bug-fix",
  "ut",
  "update",
  "status",
  "config",
] as const;

export type CommandName = (typeof COMMAND_NAMES)[number];

export const REQUIRED_RESOURCE_ADAPTERS = [
  "command_projection",
  "distribution",
  "installer",
  "lifecycle",
  "project_config",
  "project_hooks",
  "runtime",
  "subagent_protocol",
  "telemetry_protocol",
  "ut",
  "workflow_transition",
] as const;

const CAPABILITY_LEVELS = new Set(["native", "emulated", "unsupported"]);

export interface HostManifest {
  host_id: string;
  host_api_version: string;
  aliases: string[];
  capabilities: Record<string, string>;
  adapters: Record<string, string>;
  support?: {
    level?: string;
    display_name?: string;
    evidence?: string | null;
    limitations?: string[];
  };
}

export interface MarkdownDocument {
  path: string;
  frontmatter: Record<string, string | boolean>;
  body: string;
}

export interface CommandSource extends MarkdownDocument {
  name: CommandName;
  description: string;
}

export interface SkillSource extends MarkdownDocument {
  id: string;
  skillName: string;
  description: string;
  autoinvoke: boolean;
}

export interface AgentSource extends MarkdownDocument {
  id: string;
  agentName: string;
  description: string;
}

export interface LoadedPlugin {
  pluginRoot: string;
  manifest: HostManifest;
  resources: Record<string, string>;
  commands: CommandSource[];
  skills: SkillSource[];
  agents: AgentSource[];
  capabilities: Record<string, string>;
}

export interface HostContext {
  canonicalHostId: string;
  pluginRoot: string;
  repoRoot?: string;
  hostManifest: string;
  hostApiVersion: string;
  capabilities: Record<string, string>;
  resources: Record<string, string>;
}

export interface CommandInvocation {
  sessionID: string;
  prompt: {
    text: string;
    files?: unknown[];
    agents?: unknown[];
    skills?: unknown[];
    metadata?: Record<string, unknown>;
  };
  delivery?: "steer" | "queue";
}

export interface AgentDraft {
  name: string;
  description?: string;
  system?: string;
  mode: "subagent" | "primary" | "all";
  hidden: boolean;
  permissions: Array<{
    action: string;
    resource: string;
    effect: "allow" | "ask" | "deny";
  }>;
}

export interface ToolContent {
  type: "text" | "file";
  text?: string;
  uri?: string;
  mime?: string;
  name?: string;
}

export interface ToolAfterEvent {
  tool: string;
  sessionID: string;
  status: "completed" | "error";
  result?: { content?: string | ReadonlyArray<ToolContent> };
}

/**
 * The subset of the OpenCode plugin context used by this package. Keeping the
 * shape structural makes the registration logic testable without a server.
 */
export interface PluginContext {
  location: { directory: string };
  command: {
    transform(
      callback: (editor: {
        add(definition: {
          name: string;
          description?: string;
          execute(input: CommandInvocation): Promise<void>;
        }): void;
      }) => void,
    ): Promise<unknown>;
  };
  skill: {
    transform(
      callback: (editor: {
        add(skill: {
          id: string;
          name: string;
          description?: string;
          autoinvoke?: boolean;
          path: string;
          content: string;
        }): void;
      }) => void,
    ): Promise<unknown>;
  };
  agent: {
    transform(
      callback: (editor: {
        update(id: string, update: (agent: AgentDraft) => void): void;
      }) => void,
    ): Promise<unknown>;
  };
  tool?: {
    hook(
      name: "execute.after",
      callback: (event: ToolAfterEvent) => Promise<void> | void,
    ): Promise<unknown>;
  };
  session: {
    get(input: { sessionID: string }): Promise<{ location?: { directory?: string } }>;
    prompt(input: {
      sessionID: string;
      text: string;
      files?: unknown[];
      agents?: unknown[];
      skills?: unknown[];
      metadata?: Record<string, unknown>;
      delivery?: "steer" | "queue";
    }): Promise<unknown>;
  };
}

export interface RegisterOptions {
  pluginRoot?: string;
}

export function pluginRootFromModule(moduleUrl: string = import.meta.url): string {
  return path.resolve(path.dirname(fileURLToPath(moduleUrl)), "..");
}

function realpath(value: string): string {
  return fs.realpathSync(value);
}

function assertInside(root: string, candidate: string, label: string): string {
  const resolved = path.resolve(candidate);
  const relative = path.relative(root, resolved);
  if (
    relative === "" ||
    (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative))
  ) {
    return resolved;
  }
  throw new Error(`${label} escapes the plugin root: ${resolved}`);
}

function requireFile(filePath: string, label: string): string {
  let stat: fs.Stats;
  try {
    stat = fs.statSync(filePath);
  } catch {
    throw new Error(`${label} does not exist: ${filePath}`);
  }
  if (!stat.isFile()) {
    throw new Error(`${label} is not a file: ${filePath}`);
  }
  return filePath;
}

function unquote(value: string): string {
  const text = value.trim();
  if (text.length >= 2 && text.startsWith('"') && text.endsWith('"')) {
    try {
      const parsed = JSON.parse(text);
      if (typeof parsed === "string") return parsed;
    } catch {
      // Fall through to the simple quote stripping below.
    }
    return text.slice(1, -1);
  }
  if (text.length >= 2 && text.startsWith("'") && text.endsWith("'")) {
    return text.slice(1, -1).replaceAll("''", "'");
  }
  return text;
}

function parseScalar(value: string): string | boolean {
  const text = value.trim();
  if (text === "true") return true;
  if (text === "false") return false;
  if (text === "null" || text === "~") return "";
  return unquote(text);
}

/**
 * Parses the flat frontmatter keys used by HelixHarness commands, skills and
 * agents. Nested YAML is intentionally ignored; only top-level scalar fields
 * are part of the host projection contract.
 */
export function parseFrontmatter(frontmatter: string): Record<string, string | boolean> {
  const result: Record<string, string | boolean> = {};
  for (const line of frontmatter.split(/\r?\n/)) {
    if (!line || /^\s/.test(line) || line.trimStart().startsWith("#")) continue;
    const match = /^([A-Za-z0-9_-]+):(.*)$/.exec(line);
    if (!match) continue;
    const [, key, rawValue] = match;
    const value = rawValue.trim();
    if (!value || value === ">" || value === ">-" || value === "|" || value === "|-") {
      // Block scalars and nested maps are not used as scalar projection fields.
      continue;
    }
    result[key] = parseScalar(value);
  }
  return result;
}

export function parseMarkdown(source: string, filePath: string): MarkdownDocument {
  const text = source.replace(/^\uFEFF/, "");
  const match = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(text);
  if (!match) {
    return { path: filePath, frontmatter: {}, body: text };
  }
  return {
    path: filePath,
    frontmatter: parseFrontmatter(match[1]),
    body: text.slice(match[0].length),
  };
}

export function loadManifest(pluginRoot: string): {
  manifest: HostManifest;
  resources: Record<string, string>;
} {
  const root = realpath(pluginRoot);
  const manifestPath = path.join(root, "hosts", HOST_ID, "host.json");
  requireFile(manifestPath, "OpenCode host manifest");
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8")) as HostManifest;
  if (manifest.host_id !== HOST_ID) {
    throw new Error(
      `OpenCode host manifest ID mismatch: ${String(manifest.host_id)} != ${HOST_ID}`,
    );
  }
  if (manifest.host_api_version !== HOST_API_VERSION) {
    throw new Error(
      `Unsupported OpenCode Host API version: ${String(manifest.host_api_version)}`,
    );
  }
  if (!manifest.capabilities || typeof manifest.capabilities !== "object") {
    throw new Error("OpenCode host manifest is missing capabilities");
  }
  for (const [name, level] of Object.entries(manifest.capabilities)) {
    if (!CAPABILITY_LEVELS.has(level)) {
      throw new Error(`Invalid OpenCode capability level: ${name}=${level}`);
    }
  }
  if (!manifest.adapters || typeof manifest.adapters !== "object") {
    throw new Error("OpenCode host manifest is missing adapters");
  }

  const resources: Record<string, string> = {};
  for (const [name, reference] of Object.entries(manifest.adapters)) {
    if (!reference.startsWith("resource:")) continue;
    const target = reference.slice("resource:".length);
    if (!target || path.isAbsolute(target)) {
      throw new Error(`Invalid OpenCode resource adapter: ${name}`);
    }
    const resolved = assertInside(root, path.join(root, target), `OpenCode resource ${name}`);
    resources[name] = requireFile(resolved, `OpenCode resource ${name}`);
  }

  for (const name of REQUIRED_RESOURCE_ADAPTERS) {
    if (!resources[name]) {
      throw new Error(`OpenCode host manifest is missing required resource: ${name}`);
    }
  }

  return { manifest, resources };
}

export function loadCommands(pluginRoot: string): CommandSource[] {
  const root = realpath(pluginRoot);
  return COMMAND_NAMES.map((name) => {
    const filePath = requireFile(
      path.join(root, "commands", `${name}.md`),
      `HelixHarness command ${name}`,
    );
    const document = parseMarkdown(fs.readFileSync(filePath, "utf8"), filePath);
    const description =
      typeof document.frontmatter.description === "string" &&
      document.frontmatter.description.trim()
        ? document.frontmatter.description.trim()
        : `HelixHarness ${name}`;
    return { ...document, name, description };
  });
}

export function loadSkills(pluginRoot: string): SkillSource[] {
  const root = realpath(pluginRoot);
  const skillsRoot = path.join(root, "skills");
  const entries = fs
    .readdirSync(skillsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort((left, right) => left.localeCompare(right));
  const skills: SkillSource[] = [];
  for (const directory of entries) {
    const filePath = path.join(skillsRoot, directory, "SKILL.md");
    if (!fs.existsSync(filePath)) continue;
    const document = parseMarkdown(fs.readFileSync(filePath, "utf8"), filePath);
    const description =
      typeof document.frontmatter.description === "string" &&
      document.frontmatter.description.trim()
        ? document.frontmatter.description.trim()
        : `HelixHarness ${directory} skill`;
    skills.push({
      ...document,
      id: `helix-${directory}`,
      skillName: directory,
      description,
      autoinvoke: document.frontmatter["disable-model-invocation"] !== true,
    });
  }
  return skills;
}

export function loadAgents(pluginRoot: string): AgentSource[] {
  const root = realpath(pluginRoot);
  const agentsRoot = path.join(root, "agents");
  const files = fs
    .readdirSync(agentsRoot, { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith(".md"))
    .map((entry) => entry.name)
    .sort((left, right) => left.localeCompare(right));
  return files.map((file) => {
    const filePath = path.join(agentsRoot, file);
    const document = parseMarkdown(fs.readFileSync(filePath, "utf8"), filePath);
    const agentName = file.slice(0, -".md".length);
    const description =
      typeof document.frontmatter.description === "string" &&
      document.frontmatter.description.trim()
        ? document.frontmatter.description.trim()
        : `HelixHarness ${agentName} agent`;
    return {
      ...document,
      id: `helix-${agentName}`,
      agentName,
      description,
    };
  });
}

export function loadPluginResources(pluginRoot: string = pluginRootFromModule()): LoadedPlugin {
  const root = realpath(pluginRoot);
  const { manifest, resources } = loadManifest(root);
  return {
    pluginRoot: root,
    manifest,
    resources,
    capabilities: { ...manifest.capabilities },
    commands: loadCommands(root),
    skills: loadSkills(root),
    agents: loadAgents(root),
  };
}

export function hostContextFor(
  plugin: LoadedPlugin,
  repoRoot?: string,
): HostContext {
  return {
    canonicalHostId: plugin.manifest.host_id,
    pluginRoot: plugin.pluginRoot,
    repoRoot,
    hostManifest: path.join(plugin.pluginRoot, "hosts", HOST_ID, "host.json"),
    hostApiVersion: plugin.manifest.host_api_version,
    capabilities: plugin.capabilities,
    resources: plugin.resources,
  };
}

function renderCapabilities(capabilities: Record<string, string>): string[] {
  const lines = ["| capability | level |", "| --- | --- |"];
  for (const name of Object.keys(capabilities).sort()) {
    lines.push(`| \`${name}\` | ${capabilities[name]} |`);
  }
  return lines;
}

function renderResources(resources: Record<string, string>): string[] {
  const lines = ["| resource | absolute path |", "| --- | --- |"];
  for (const name of Object.keys(resources).sort()) {
    lines.push(`| \`${name}\` | \`${resources[name]}\` |`);
  }
  return lines;
}

export function renderHostContext(context: HostContext): string {
  const lines = [
    "# HelixHarness Host Context",
    "",
    "This context is pre-bound by the installed HelixHarness OpenCode plugin. Use these values directly; do not search for another plugin copy or another version.",
    "",
    `- canonical_host_id: \`${context.canonicalHostId}\``,
    `- host_api_version: \`${context.hostApiVersion}\``,
    `- plugin_root: \`${context.pluginRoot}\``,
  ];
  if (context.repoRoot) {
    lines.push(`- repo_root: \`${context.repoRoot}\``);
  }
  lines.push(`- host_manifest: \`${context.hostManifest}\``);
  lines.push("", "## Capabilities", "", ...renderCapabilities(context.capabilities));
  lines.push("", "## Resolved resources", "", ...renderResources(context.resources));
  lines.push(
    "",
    "## OpenCode runtime bindings",
    "",
    "- `read_file` uses the native `read` tool.",
    "- `edit_file` uses native `edit`, `write` or `patch` while preserving the current write scope and single-writer rule.",
    "- `run_command` uses the native `shell` tool and keeps the real exit status.",
    "- `invoke_skill(name)` loads the native skill ID `helix-<name>`. Direct method-skill invocation is also registered as `helix-<name>`.",
    "- `spawn_job` uses the native `subagent` tool. Registered roles use `helix-<agent>`; generic jobs use the built-in `general` agent. Set `background: true` only when the command needs parallel work.",
    "- `wait_job` uses the foreground subagent result, or the native background-completion notice for a background subagent, and then validates the returned result and artifacts.",
    "- `ask_user` uses the native `question` tool and waits for a real answer.",
    "- `update_plan` has no native OpenCode plan tool; update the current Feature `tasks.md` instead.",
    "- `workflow_transition` is `inline`: execute the routed workflow body in this same session instead of recursively invoking another slash command.",
    "- `stage_finish` calls the HelixHarness lifecycle scripts from the command body. OpenCode telemetry is `unsupported`, so telemetry steps return `unsupported_host` and business work continues.",
    "- OpenCode plugin update is `unsupported`; `/helix:update` reports `continue_silently` and the user updates through OpenCode plugin management.",
    "",
    "Follow `resources.runtime`, `resources.subagent_protocol`, `resources.workflow_transition`, `resources.ut`, `resources.project_config`, `resources.project_hooks` and `resources.telemetry_protocol` for the complete host contract.",
  );
  return lines.join("\n");
}

export function shellQuote(value: string): string {
  if (value === "") return "''";
  return `'${value.replaceAll("'", "'\\''")}'`;
}

export function substitutePlaceholders(
  text: string,
  context: HostContext,
  argumentsText: string,
): string {
  const replacements: Record<string, string> = {
    "{plugin_root}": context.pluginRoot,
    "{repo_root}": context.repoRoot ?? "{repo_root}",
    "{canonical_host_id}": context.canonicalHostId,
    "{current_platform}": context.canonicalHostId,
    "{SHELL_QUOTED_ARGUMENTS}": shellQuote(argumentsText),
    $ARGUMENTS: argumentsText,
  };
  return text.replace(
    /\{plugin_root\}|\{repo_root\}|\{canonical_host_id\}|\{current_platform\}|\{SHELL_QUOTED_ARGUMENTS\}|\$ARGUMENTS/g,
    (match) => replacements[match] ?? match,
  );
}

/**
 * Method skills are loaded by the native `skill` tool, which has no argument
 * channel. Keep `$ARGUMENTS` literal so the model can interpret the user's
 * message as the skill input, while still binding the plugin and host paths.
 */
export function substituteSkillPlaceholders(
  text: string,
  context: HostContext,
): string {
  const replacements: Record<string, string> = {
    "{plugin_root}": context.pluginRoot,
    "{repo_root}": context.repoRoot ?? "{repo_root}",
    "{canonical_host_id}": context.canonicalHostId,
    "{current_platform}": context.canonicalHostId,
  };
  return text.replace(
    /\{plugin_root\}|\{repo_root\}|\{canonical_host_id\}|\{current_platform\}/g,
    (match) => replacements[match] ?? match,
  );
}

export function buildCommandPrompt(
  command: CommandSource,
  context: HostContext,
  argumentsText: string,
): string {
  return [
    `# HelixHarness command /helix:${command.name}`,
    "",
    renderHostContext(context),
    "",
    "---",
    "",
    substitutePlaceholders(command.body, context, argumentsText).trimEnd(),
    "",
  ].join("\n");
}

export function buildSkillContent(skill: SkillSource, plugin: LoadedPlugin): string {
  const context = hostContextFor(plugin, REPO_ROOT_TOKEN);
  return [
    renderHostContext(context),
    "",
    "---",
    "",
    substituteSkillPlaceholders(skill.body, context).trimEnd(),
    "",
  ].join("\n");
}

export function buildAgentSystem(agent: AgentSource, plugin: LoadedPlugin): string {
  const context = hostContextFor(plugin);
  return [
    renderHostContext(context),
    "",
    "The dispatch prompt supplies the authoritative `repo_root` and the command-local Dispatch Contract.",
    "",
    "---",
    "",
    substituteSkillPlaceholders(agent.body, context).trimEnd(),
    "",
  ].join("\n");
}

/**
 * Host-only role for generic jobs that legitimately declare
 * `delegation_policy=may_delegate` (currently the Workspace Apply RepoJob).
 * The built-in `general` agent cannot spawn subagents; this role carries the
 * same complete dispatch prompt and only changes that host capability.
 */
export function buildWorkerSystem(plugin: LoadedPlugin): string {
  const context = hostContextFor(plugin);
  return [
    renderHostContext(context),
    "",
    "You execute one dispatched HelixHarness job described by the prompt.",
    "",
    "- Read the complete dispatch prompt before acting: `job_id`, `prompt_source`, `read_scope`, `write_scope`, `forbidden_scope`, `delegation_policy`, `interaction_mode`, expected artifacts and validation.",
    "- Follow the current command contract and `prompt_source`; the Host Context above and the routed workflow body are the only business sources.",
    "- When `delegation_policy=may_delegate`, delegate only the independent review roles required by the routed Single Apply through the native `subagent` tool. Otherwise stay a leaf and do not spawn children.",
    "- If the native nesting depth does not permit the required delegation, return the concrete gap and `BLOCKED` reason to the parent instead of inlining or inventing the missing review.",
    "- Do not write shared indexes, plans, checkpoints or final reports owned by the parent.",
  ].join("\n");
}

export function replaceRepoRootToken(
  content: string,
  repoRoot: string,
): string {
  return content.replaceAll(REPO_ROOT_TOKEN, repoRoot);
}

function replaceToolResultContent(
  result: ToolAfterEvent["result"],
  repoRoot: string,
): void {
  if (!result || result.content === undefined) return;
  if (typeof result.content === "string") {
    result.content = replaceRepoRootToken(result.content, repoRoot);
    return;
  }
  result.content = result.content.map((item) =>
    item.type === "text" && item.text !== undefined
      ? { ...item, text: replaceRepoRootToken(item.text, repoRoot) }
      : item,
  );
}

async function resolveRepoRoot(
  ctx: PluginContext,
  sessionID: string,
  fallback: string,
): Promise<string> {
  try {
    const session = await ctx.session.get({ sessionID });
    const directory = session.location?.directory;
    return typeof directory === "string" && directory ? directory : fallback;
  } catch {
    return fallback;
  }
}

/**
 * Registers every HelixHarness command, method skill and subagent role with an
 * OpenCode plugin context. Commands execute the canonical `commands/*.md`
 * contract with a pre-bound Host Context instead of copying Markdown into a
 * project `.opencode/` directory.
 */
export async function registerHelixHarness(
  ctx: PluginContext,
  options: RegisterOptions = {},
): Promise<void> {
  const plugin = loadPluginResources(options.pluginRoot ?? pluginRootFromModule());
  const fallbackRepoRoot = ctx.location.directory;

  await ctx.command.transform((editor) => {
    for (const command of plugin.commands) {
      editor.add({
        name: `helix:${command.name}`,
        description: command.description,
        async execute(input) {
          const repoRoot = await resolveRepoRoot(ctx, input.sessionID, fallbackRepoRoot);
          const context = hostContextFor(plugin, repoRoot);
          const text = buildCommandPrompt(
            command,
            context,
            input.prompt.text ?? "",
          );
          await ctx.session.prompt({
            sessionID: input.sessionID,
            text,
            ...(input.prompt.files ? { files: input.prompt.files } : {}),
            ...(input.prompt.agents ? { agents: input.prompt.agents } : {}),
            ...(input.prompt.skills ? { skills: input.prompt.skills } : {}),
            metadata: { "helixharness.command": command.name },
            ...(input.delivery ? { delivery: input.delivery } : {}),
          });
        },
      });
    }
  });

  await ctx.skill.transform((editor) => {
    for (const skill of plugin.skills) {
      editor.add({
        id: skill.id,
        name: `HelixHarness ${skill.skillName}`,
        description: skill.description,
        autoinvoke: skill.autoinvoke,
        path: skill.path,
        content: buildSkillContent(skill, plugin),
      });
    }
  });

  await ctx.agent.transform((editor) => {
    for (const agent of plugin.agents) {
      editor.update(agent.id, (draft) => {
        draft.name = `HelixHarness ${agent.agentName}`;
        draft.description = agent.description;
        draft.mode = "subagent";
        draft.hidden = false;
        draft.system = buildAgentSystem(agent, plugin);
        // Public contracts default to leaf delegation for canonical roles.
        // Only the host-only worker role may spawn the Workspace Apply review
        // agents when the dispatch declares may_delegate.
        draft.permissions.push({
          action: "subagent",
          resource: "*",
          effect: "deny",
        });
        if (agent.agentName === "unit-test-worker") {
          draft.permissions.push({
            action: "question",
            resource: "*",
            effect: "deny",
          });
        }
      });
    }
    editor.update(WORKER_AGENT_ID, (draft) => {
      draft.name = "HelixHarness Worker";
      draft.description =
        "Executes one dispatched HelixHarness job. Use for generic jobs that declare delegation_policy=may_delegate.";
      draft.mode = "subagent";
      draft.hidden = false;
      draft.system = buildWorkerSystem(plugin);
    });
  });

  if (ctx.tool) {
    await ctx.tool.hook("execute.after", async (event) => {
      if (event.tool !== "skill" || event.status !== "completed") return;
      const repoRoot = await resolveRepoRoot(ctx, event.sessionID, fallbackRepoRoot);
      replaceToolResultContent(event.result, repoRoot);
    });
  }
}
