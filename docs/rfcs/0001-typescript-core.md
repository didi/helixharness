# RFC 0001: TypeScript Core Runtime for HelixHarness

- **Status:** Proposed
- **Scope:** HelixHarness plugin core and host adapters
- **Decision type:** Architecture
- **Target:** A single deterministic runtime shared by all supported coding agents

## 1. Summary

HelixHarness currently keeps its business workflow in Markdown contracts and its
deterministic behavior in Python scripts. Host plugins register commands, skills
and agents, then invoke the Python scripts through the host shell.

This RFC proposes moving the deterministic core to TypeScript/JavaScript and
shipping it with the host plugin package. Markdown remains the business source
of truth. The core becomes a typed library or an optional compiled binary, and
every host adapter calls the same core instead of maintaining a separate runtime.

## 2. Context

The current split is:

```text
Markdown contracts     commands/, workflows/, skills/, agents/
Deterministic core     scripts/*.py
Host adapters          host manifests, hooks, plugin entrypoints
```

The Python core covers more than glue:

- workflow routing and stage state;
- Spec/Tasks validation and atomic writes;
- workspace registry and multi-repo planning;
- project status and init output validation;
- version policy and update orchestration;
- telemetry consent, ledger, trace, async collection and feedback;
- host runtime discovery and project hooks.

It is a real state machine and policy layer, not a thin CLI wrapper.

## 3. Problem

### 3.1 Two runtimes

A coding agent already runs its own runtime (Node, Bun, or a native binary).
HelixHarness adds a second runtime for its core. A command may cross:

```text
host runtime -> plugin code -> shell -> Python -> shell -> project tools
```

This adds installation requirements, startup cost, quoting risk, error mapping
and a second dependency graph.

### 3.2 Scope ambiguity

The plugin is installed in a host/user scope, while the core is invoked through
an ambient `python3` from `PATH`. A project virtual environment, a shell profile
change, or a background host service can silently change which interpreter runs
HelixHarness. The boundary between user scope and project scope is therefore not
explicit.

### 3.3 Contract drift

The core emits JSON consumed by Markdown instructions and host adapters. With
two languages, schemas and types are duplicated. Changes can pass Python tests
while breaking a host adapter, or vice versa.

### 3.4 Distribution and testing

Users must install or discover Python separately from the plugin. CI needs a
second language matrix. Tests frequently use subprocesses and temporary
repositories instead of calling typed functions directly.

## 4. Goals

1. One deterministic core runtime for all supported hosts.
2. Explicitly bound, host-scoped runtime resolution; no ambient interpreter
   surprises.
3. Keep Markdown commands, workflows, skills and agents as the only business
   source of truth.
4. Preserve behavioral parity with the current Python core.
5. Make the plugin package self-contained: install the plugin, get the core.
6. Allow a compiled binary later for hosts without Node/Bun.
7. Keep host adapters thin: registration, Host Context, tool mapping and
   lifecycle only.

## 5. Non-goals

- Rewriting the business workflow into code.
- Turning HelixHarness into a standalone agent runtime.
- Reimplementing model calls, tool execution, permissions or sessions.
- A Rust rewrite in the first phase.
- Porting telemetry and feedback before the workflow core is stable.
- Changing the public command names or the Propose → Apply → Archive model.

## 6. Options considered

### Option A: Keep Python and bundle it

Bundle a Python runtime or build a single binary with a Python packager.

**Pros:** smallest migration; existing tests remain.

**Cons:** still a second runtime and ecosystem; packaging is fragile across
platforms; type sharing remains unsolved; the scope ambiguity remains unless a
host-level binding is added.

### Option B: TypeScript/JavaScript core (recommended)

Implement the deterministic core in TypeScript, ship it with the plugin, and run
it on the host's Node/Bun runtime. Optionally compile a single binary.

**Pros:** same ecosystem as modern coding-agent plugins; typed contracts;
in-process calls; one package; testable without subprocesses; optional binary.

**Cons:** large migration; Node/Bun availability must be defined; telemetry is a
substantial port.

### Option C: Rust core

A standalone binary with no runtime dependency.

**Pros:** single binary, fast startup, strong distribution story.

**Cons:** highest implementation and maintenance cost; FFI/JSON boundary remains;
the workload is mostly file, JSON/YAML, Git and state transitions, not CPU-bound;
the team and plugin ecosystem are primarily TypeScript/JavaScript.

**Decision:** Option B. Rust remains a possible later distribution target if a
single binary is required, but it is not the first migration.

## 7. Proposed architecture

```text
host plugin adapter
  - registers commands, skills and agents
  - builds Host Context
  - maps logical actions to native tools
  - binds lifecycle and permissions
        |
        v
TypeScript core
  - contracts and schemas
  - router and workflow state machine
  - validation and atomic writes
  - workspace and status
  - init and version policy
  - telemetry and feedback (later phase)
```

### 7.1 Core module boundaries

| Module | Responsibility |
|---|---|
| `contracts` | Host manifest, Host Context, router actions, stage state, receipt schemas |
| `router` | Argument normalization, feature/stage routing, workflow selection |
| `workflow` | Spec/Tasks validation, locks, atomic writes, review scope, archive |
| `workspace` | Registry, repo planning, workspace gates |
| `status` | Read-only project and feature diagnostics |
| `init` | Project type detection, greenfield/bootstrap validation |
| `version` | Version policy, installation discovery, update state machine |
| `telemetry` | Consent, ledger, trace, async collection, feedback (phase 2) |

### 7.2 Host adapters

Adapters must not reimplement business rules. They provide:

- command/skill/agent registration;
- Host Context and resource paths;
- logical action mapping;
- session, permission and lifecycle integration;
- a declared runtime binding for the core.

### 7.3 Runtime binding

The core must not resolve an interpreter through ambient `PATH` alone. A host
adapter should provide an explicit runtime binding:

```text
runtime.kind = node | bun | binary
runtime.command = absolute path or embedded binary
runtime.version = declared compatibility
```

The binding is host-scoped. Project virtual environments must not silently
change the core runtime.

## 8. Migration plan

### Phase 0: Contracts and fixtures

- Define TypeScript types for Host Context, router responses, stage state,
  receipts and workspace plans.
- Capture golden fixtures from the current Python implementation.
- Add a parity test harness that runs both implementations on the same inputs.

### Phase 1: Router and workflow state

- Port argument normalization and routing.
- Port Spec/Tasks validation, locks, atomic writes and stage transitions.
- Keep commands calling the existing entrypoint until parity is proven.

### Phase 2: Workspace, status, init and version

- Port workspace registry/planning and status.
- Port init output validation and project type detection.
- Port version policy and update state machine.

### Phase 3: Telemetry and feedback

- Port consent, ledger and trace first.
- Port async collection and feedback behind the same adapter interface.
- Keep a temporary Python adapter only if a host needs it during migration.

### Phase 4: Remove the compatibility layer

- Delete Python core paths after all hosts use the TypeScript core.
- Keep Markdown contracts unchanged.
- Update installation, troubleshooting and host preflight docs.

## 9. Testing strategy

- **Parity tests:** Python and TypeScript produce identical normalized outputs
  for the same fixtures.
- **Contract tests:** every host adapter consumes the same typed core response.
- **State-machine tests:** locks, atomic writes, resume, archive, workspace.
- **Failure tests:** missing files, invalid schemas, stale state, permission
  errors and interrupted writes.
- **Host integration tests:** command registration and Host Context injection.
- **CI matrix:** Node 20/22 and Bun; optional compiled-binary smoke test.
- **Performance baseline:** command startup and representative workflow calls.

## 10. Packaging

- The core is part of the plugin package.
- Node/Bun hosts import it directly.
- Hosts without Node/Bun can use an optional compiled binary.
- Version pinning follows the plugin version.
- No separate Python installation for the core command path.

## 11. Risks

| Risk | Mitigation |
|---|---|
| Behavior drift during port | Golden fixtures and parity tests before deleting Python |
| Host runtime unavailable | Declared runtime binding and optional compiled binary |
| Large migration surface | Phase by module; keep Markdown contracts unchanged |
| Telemetry complexity | Port workflow core first; telemetry as a separate phase |
| Duplicate implementations during migration | Time-boxed compatibility layer with explicit removal criteria |
| Public command changes | Freeze command names, arguments and JSON contracts during migration |

## 12. Open questions

1. Should the first release require Node, Bun, or allow either?
2. Is a compiled binary required for hosts without a JavaScript runtime?
3. Can telemetry remain on a Python adapter longer than the workflow core?
4. Which parity fixtures are authoritative when Python and TypeScript disagree?
5. How should a host declare runtime compatibility in its manifest?

## 13. Acceptance criteria

- The deterministic command path runs without Python.
- All supported hosts call the same core.
- Markdown contracts remain the only business source of truth.
- Parity tests pass for routing, state, validation, workspace and version.
- Host adapters contain no duplicated business rules.
- Installation no longer requires a separate core runtime discovery.
- The Python compatibility layer is removed or has a dated removal plan.

## 14. References

- `reference/host-api.md`
- `reference/host-runtime-protocol.md`
- `reference/workflow-projection-protocol.md`
- `reference/subagent-runtime-protocol.md`
- `docs/workflow-architecture.md`
