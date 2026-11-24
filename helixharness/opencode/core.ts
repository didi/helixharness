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
import { guardUtStateWrite, type UtWriteEvent } from "./ut-state.ts";
import { runProjectChecks } from "./project-checks.ts";

export const HOST_ID = "opencode";
export const HOST_API_VERSION = "1.1";
export const PLUGIN_ID = "helixharness";
export const SESSION_ID_TOKEN = "@@HELIXHARNESS_SESSION_ID@@";
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

interface NativePreflight {
  registeredTools: string[] | null;
  toolCatalog: "available" | "unavailable";
  sessionMoveApi: "available" | "unavailable";
  delegationDepth: "unknown";
}

export interface HostContext {
  nativePreflight?: NativePreflight;
  sessionId?: string;
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
  input?: unknown;
  id?: string;
  result?: { content?: string | ReadonlyArray<ToolContent> };
}

export type SessionBindingHook = "context" | "compaction" | "generate" | "title";

interface RequestTextPart {
  type: string;
  text?: string;
  [key: string]: unknown;
}

export interface SessionBindingEvent {
  sessionID: string;
  system: Array<{ text: string; [key: string]: unknown }>;
  messages: Array<{
    role: string;
    content: string | ReadonlyArray<RequestTextPart>;
    [key: string]: unknown;
  }>;
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
    list?(input: { sessionID: string }): Promise<ReadonlyArray<{ id?: string; name?: string }>>;
    hook(name: "execute.before", callback: (event: UtWriteEvent) => Promise<void> | void): Promise<unknown>;
    hook(
      name: "execute.after",
      callback: (event: ToolAfterEvent) => Promise<void> | void,
    ): Promise<unknown>;
  };
  session: {
    move?: unknown;
    hook?(
      name: SessionBindingHook,
      callback: (event: SessionBindingEvent) => Promise<void> | void,
    ): Promise<unknown>;
    hook?(name: "experimental.idle", callback: (event: { sessionID: string; background: ReadonlyArray<unknown> }) => Promise<void> | void): Promise<unknown>;
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

/** Check both boundaries before reading a file or enumerating a directory. */
function resolvePluginPath(root: string, candidate: string, label: string): string {
  const lexical = assertInside(root, candidate, label);
  let resolved: string;
  try {
    resolved = realpath(lexical);
  } catch {
    throw new Error(`${label} does not exist or cannot be resolved: ${lexical}`);
  }
  return assertInside(root, resolved, label);
}

function requirePluginFile(root: string, candidate: string, label: string): string {
  return requireFile(resolvePluginPath(root, candidate, label), label);
}

function requirePluginDirectory(root: string, candidate: string, label: string): string {
  const resolved = resolvePluginPath(root, candidate, label);
  if (!fs.statSync(resolved).isDirectory()) throw new Error(`${label} is not a directory`);
  return resolved;
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
  const manifestPath = requirePluginFile(
    root, path.join(root, "hosts", HOST_ID, "host.json"), "OpenCode host manifest",
  );
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
    resources[name] = requirePluginFile(root, path.join(root, target), `OpenCode resource ${name}`);
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
    const filePath = requirePluginFile(
      root, path.join(root, "commands", `${name}.md`),
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
  const skillsRoot = requirePluginDirectory(root, path.join(root, "skills"), "HelixHarness skills");
  const entries = fs
    .readdirSync(skillsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() || entry.isSymbolicLink())
    .map((entry) => entry.name)
    .sort((left, right) => left.localeCompare(right));
  const skills: SkillSource[] = [];
  for (const directory of entries) {
    const skillDirectory = resolvePluginPath(root, path.join(skillsRoot, directory), `HelixHarness skill ${directory}`);
    if (!fs.statSync(skillDirectory).isDirectory()) continue;
    const candidate = path.join(skillDirectory, "SKILL.md");
    // A missing optional Skill is fine; a dangling link is an invalid resource.
    if (!fs.lstatSync(candidate, { throwIfNoEntry: false })) continue;
    const filePath = requirePluginFile(root, candidate, `HelixHarness skill ${directory}`);
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
  const agentsRoot = requirePluginDirectory(root, path.join(root, "agents"), "HelixHarness agents");
  const files = fs
    .readdirSync(agentsRoot, { withFileTypes: true })
    .filter((entry) => (entry.isFile() || entry.isSymbolicLink()) && entry.name.endsWith(".md"))
    .map((entry) => entry.name)
    .sort((left, right) => left.localeCompare(right));
  return files.map((file) => {
    const filePath = requirePluginFile(root, path.join(agentsRoot, file), `HelixHarness agent ${file}`);
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
  sessionId?: string,
): HostContext {
  return {
    sessionId,
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
  if (context.sessionId) lines.push(`- native_session_id: \`${context.sessionId}\``);
  lines.push(`- host_manifest: \`${context.hostManifest}\``);
  if (context.nativePreflight) {
    const observed = context.nativePreflight;
    lines.push(`- native_tool_catalog: \`${observed.toolCatalog}\``);
    lines.push(`- registered_native_tools: \`${JSON.stringify(observed.registeredTools)}\``);
    lines.push(`- native_session_move_api: \`${observed.sessionMoveApi}\``);
    lines.push(`- native_delegation_depth: \`${observed.delegationDepth}\``);
  }

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
    "- `spawn_job` uses the native `subagent` tool. Preserve the resolved `delegation_policy` from the command's Dispatch Contract. Generic `leaf` jobs use `general`; generic `may_delegate` jobs use `helix-worker` (currently Workspace Apply RepoJobs). Registered Init and UT roles use `helix-<agent>` and remain leaves.",
    "- Follow the exposed native `subagent` schema for execution and waiting; do not assume every OpenCode V2 release supports a foreground/background selector. `wait_job` waits for a real completed result or the native background-completion notice, then validates the returned result and artifacts. A returned `sessionID` alone is not completion.",
    "- `ask_user` uses the native `question` tool and waits for a real answer.",
    "- `update_plan` is emulated by `scripts/task_plan.py --repo-root <repo_root> --feature <feature> show` and `update --task <T-NN> --status <pending|in_progress|blocked|completed> --expected-receipt <receipt_sha256>`. Use the same canonical tasks.md; dependency and receipt failures require a fresh read. This progress update never substitutes for stage verification.",
    "- `workflow_transition` is `inline`: execute the routed workflow body in this same session instead of recursively invoking another slash command.",
    "- `stage_finish` calls the HelixHarness lifecycle scripts from the command body. Telemetry uses the existing explicit-consent protocol and records stage facts only; no native transcript or token usage is collected. Invoke the bound plugin's telemetry scripts by absolute path; native shell hooks bind the real session ID and directory to that subprocess, without changing process-global environment.",
    "- Successful native write/edit/patch notifications run existing project invariant/spec-sync linters when installed. Failure feedback does not undo edits or complete stages; shell-based changes still require the explicit workflow checks.",
    "- `/helix:update` uses the existing check/prepare/decide/install consent protocol for clean source checkouts with an explicit Git upstream. Targets are immutable commits and installation is fast-forward only. Dirty/detached/unregistered package copies are not modified. Restart OpenCode after an update.",
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
  const context = hostContextFor(plugin, REPO_ROOT_TOKEN, SESSION_ID_TOKEN);
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
    "- Preserve the resolved `delegation_policy` from the dispatch. When `delegation_policy=may_delegate`, delegate only the independent review roles required by the routed Single Apply through the native `subagent` tool; reviewer jobs remain read-only leaves with `delegation_policy=leaf`. Otherwise stay a leaf and do not spawn children.",
    "- Workspace Apply requires `experimental.subagent_depth >= 2`. If the native nesting depth or permissions do not permit the required delegation, return the concrete gap and `BLOCKED` reason to the parent. Never self-review or inline a required independent review, or invent a completed review.",
    "- Do not write shared indexes, plans, checkpoints or final reports owned by the parent.",
  ].join("\n");
}

export function replaceRepoRootToken(
  content: string,
  repoRoot: string,
  sessionId?: string,
): string {
  const bound = content.replaceAll(REPO_ROOT_TOKEN, () => repoRoot);
  return sessionId === undefined ? bound : bound.replaceAll(SESSION_ID_TOKEN, () => sessionId);
}

function bindOwnedSkillText(text: string, skillTexts: ReadonlyArray<string>, repoRoot: string, sessionId: string): string {
  return skillTexts.reduce(
    (result, skill) => result.replaceAll(skill, () => replaceRepoRootToken(skill, repoRoot, sessionId)), text,
  );
}

function hasOwnedSkillResult(result: ToolAfterEvent["result"], skillTexts: ReadonlyArray<string>): boolean {
  const owns = (text: string) => skillTexts.some(skill => text.includes(skill));
  const content = result?.content;
  if (typeof content === "string") return owns(content);
  return content?.some(item => item.type === "text" && typeof item.text === "string" && owns(item.text)) ?? false;
}

function replaceToolResultContent(
  result: ToolAfterEvent["result"],
  repoRoot: string,
  sessionId: string,
  skillTexts: ReadonlyArray<string>,
): void {
  if (!result || result.content === undefined) return;
  if (typeof result.content === "string") {
    result.content = bindOwnedSkillText(result.content, skillTexts, repoRoot, sessionId);
    return;
  }
  result.content = result.content.map((item) =>
    item.type === "text" && item.text !== undefined
      ? { ...item, text: bindOwnedSkillText(item.text, skillTexts, repoRoot, sessionId) }
      : item,
  );
}

type RepoRootErrorCode = "repo_root_lookup_failed" | "repo_root_invalid";

class RepoRootError extends Error {
  constructor(readonly code: RepoRootErrorCode) {
    super(`${code}: Cannot bind the requesting session directory.`);
  }
}

async function resolveRepoRoot(
  ctx: PluginContext,
  sessionID: string,
): Promise<string> {
  let session: Awaited<ReturnType<PluginContext["session"]["get"]>>;
  try {
    session = await ctx.session.get({ sessionID });
  } catch {
    throw new RepoRootError("repo_root_lookup_failed");
  }
  const directory = session?.location?.directory;
  if (typeof directory !== "string" || !directory.trim() || !path.isAbsolute(directory)) {
    throw new RepoRootError("repo_root_invalid");
  }
  try {
    const resolved = realpath(directory);
    if (!fs.statSync(resolved).isDirectory()) throw new Error();
    return resolved;
  } catch {
    throw new RepoRootError("repo_root_invalid");
  }
}

/** Observe registration only. This is not a permission/depth or execution probe. */
async function observeNativePreflight(ctx: PluginContext, sessionID: string): Promise<NativePreflight> {
  const result: NativePreflight = {
    registeredTools: null, toolCatalog: "unavailable",
    sessionMoveApi: typeof ctx.session.move === "function" ? "available" : "unavailable",
    delegationDepth: "unknown",
  };
  if (typeof ctx.tool?.list !== "function") return result;
  try {
    const tools = await ctx.tool.list({ sessionID });
    if (!Array.isArray(tools) || tools.some(tool => !tool || typeof tool.id !== "string" || !tool.id)) return result;
    const allowed = new Set(["read", "write", "edit", "patch", "shell", "question", "skill", "subagent", "opencode_session_move"]);
    result.registeredTools = [...new Set(tools.map(tool => tool.id).filter((id): id is string => typeof id === "string" && allowed.has(id)))].sort();
    result.toolCatalog = "available";
  } catch { /* Native config/error bodies can contain private values; keep only availability. */ }
  return result;
}

/** Bind only this installation's registered Skill bodies, never user literals. */
async function bindSkillRequest(
  ctx: PluginContext,
  event: SessionBindingEvent,
  skillTexts: ReadonlyArray<string>,
): Promise<void> {
  const owns = (text: string) => text.includes(REPO_ROOT_TOKEN) && skillTexts.some(skill => text.includes(skill));
  const eligible = (message: SessionBindingEvent["messages"][number]) =>
    message.role === "user" || message.role === "system";
  const containsSkill = event.system.some(part => owns(part.text)) || event.messages.some(message =>
    eligible(message) && (typeof message.content === "string"
      ? owns(message.content)
      : message.content.some(part => part.type === "text" && typeof part.text === "string" && owns(part.text))),
  );
  if (!containsSkill) return;

  // Do not mutate the projected source messages or reuse another request's root.
  // Throwing aborts delivery if lookup or directory validation fails.
  const repoRoot = await resolveRepoRoot(ctx, event.sessionID);
  const bind = (text: string) => bindOwnedSkillText(text, skillTexts, repoRoot, event.sessionID);
  event.system = event.system.map(part => owns(part.text) ? { ...part, text: bind(part.text) } : part);
  event.messages = event.messages.map(message => {
    if (!eligible(message)) return message;
    if (typeof message.content === "string") {
      return owns(message.content) ? { ...message, content: bind(message.content) } : message;
    }
    return { ...message, content: message.content.map(part =>
      part.type === "text" && typeof part.text === "string" && owns(part.text)
        ? { ...part, text: bind(part.text) } : part,
    ) };
  });
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

  // Neither tool results nor mentions may be exposed without their binding hooks.
  if (!ctx.tool || typeof ctx.tool.hook !== "function" || typeof ctx.session.hook !== "function") {
    throw new Error("repo_root_hook_unavailable: Session directory binding is required.");
  }
  const skillTexts = [...new Set(plugin.skills.map(skill => buildSkillContent(skill, plugin).trimEnd()))];
  await ctx.tool.hook("execute.after", async (event) => {
    await runProjectChecks(event, plugin.pluginRoot, () => resolveRepoRoot(ctx, event.sessionID));
    if (event.tool !== "skill" || event.status !== "completed" || !hasOwnedSkillResult(event.result, skillTexts)) return;
    try {
      const repoRoot = await resolveRepoRoot(ctx, event.sessionID);
      replaceToolResultContent(event.result, repoRoot, event.sessionID, skillTexts);
    } catch (error) {
      const failure = error instanceof RepoRootError
        ? error
        : new RepoRootError("repo_root_invalid");
      event.result = { content: `BLOCKED: ${failure.message}` };
    }
  });

  const utSessions = new Set<string>();

  // Publication checks run before native write/edit can expose partial UT state.
  await ctx.tool.hook("execute.before", async event => {
    await guardUtStateWrite(event, () => resolveRepoRoot(ctx, event.sessionID), utSessions.has(event.sessionID) || event.agent === "helix-unit-test-worker");
    const input = event.input as { command?: unknown } | undefined;
    if (event.tool === "shell" && typeof input?.command === "string" &&
        input.command.includes(plugin.pluginRoot) && /(?:metrics|telemetry_consent|telemetryctl)\.py\b/.test(input.command)) {
      const root = await resolveRepoRoot(ctx, event.sessionID);
      input.command = `export HELIXHARNESS_SESSION_ID=${shellQuote(event.sessionID)} HELIXHARNESS_SESSION_CWD=${shellQuote(root)};\n${input.command}`;
    }
  });

  for (const hook of ["context", "compaction", "generate", "title"] as const) {
    await ctx.session.hook(hook, event => bindSkillRequest(ctx, event, skillTexts));
  }

  await ctx.session.hook("experimental.idle", event => {
    if (event.background.length === 0) utSessions.delete(event.sessionID);
  });

  await ctx.command.transform((editor) => {
    for (const command of plugin.commands) {
      editor.add({
        name: `helix:${command.name}`,
        description: command.description,
        async execute(input) {
          const repoRoot = await resolveRepoRoot(ctx, input.sessionID);
          const context = hostContextFor(plugin, repoRoot, input.sessionID);
          if (["init", "propose", "apply", "archive", "ut", "bug-fix"].includes(command.name)) {
            context.nativePreflight = await observeNativePreflight(ctx, input.sessionID);
            if (context.nativePreflight.toolCatalog === "available") {
              const missing = ["read", "shell"].filter(name => !context.nativePreflight!.registeredTools!.includes(name));
              if (missing.length) throw new Error(`opencode_preflight_tools_unavailable: ${missing.join(",")}`);
            }
          }
          const text = buildCommandPrompt(
            command,
            context,
            input.prompt.text ?? "",
          );
          const wasUt = utSessions.has(input.sessionID);
          if (command.name === "ut") utSessions.add(input.sessionID);
          else utSessions.delete(input.sessionID);
          try {
            await ctx.session.prompt({
              sessionID: input.sessionID,
              text,
              ...(input.prompt.files ? { files: input.prompt.files } : {}),
              ...(input.prompt.agents ? { agents: input.prompt.agents } : {}),
              ...(input.prompt.skills ? { skills: input.prompt.skills } : {}),
              metadata: { "helixharness.command": command.name },
              ...(input.delivery ? { delivery: input.delivery } : {}),
            });
          } catch (error) {
            if (wasUt) utSessions.add(input.sessionID); else utSessions.delete(input.sessionID);
            throw error;
          }
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
}
