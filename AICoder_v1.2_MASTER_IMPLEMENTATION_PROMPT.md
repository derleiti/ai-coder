# AICoder v1.2 — Master Implementation Prompt
## Agentic Core, Dynamic Tool Provisioning, Unified Settings, Plugins, Local OS MCP, Privilege Hardening, Provider Hygiene, GUI/CLI Parity

You are working on the **AICoder** repository and are responsible for preparing a production-quality **v1.2 release**.

The objective is NOT to turn AICoder into another wrapper around other coding CLIs. AICoder itself must become the native coding/system agent: a persistent agent loop with planning, tools, MCP, skills, subagents, settings, local execution, security boundaries, and first-class GUI/CLI behavior.

This task is a substantial architectural hardening and capability upgrade. Work incrementally, inspect before editing, preserve unrelated work, back up before changes, add tests, verify actual behavior, and do not claim success from code changes alone.

---


# 0A. VERIFIED IMPLEMENTATION STATUS — 2026-08-22

This section overrides stale status assumptions elsewhere in this document. The remainder of the file is still the architectural target/specification unless explicitly marked otherwise.

Verified repository:
- source of truth: `/home/zombie/ai-coder`
- verified HEAD during this review: `6b48ecc`
- working tree was clean before this roadmap-only update
- `pyproject.toml` version: `0.9.9`
- full test suite: **379/379 passing** (`python -m unittest discover tests`)
- do **not** call the product `1.2.0` yet

## Status legend

- ✅ **implemented and exercised** — real code exists and focused/full tests cover the critical path
- 🟡 **foundation implemented / incomplete product surface** — usable core exists, but important edge cases, integration, UX, trust, packaging, or real-world validation remain
- 🔴 **not release-ready / still required**
- ⚪ **deliberately deferred / only implement if evidence justifies it**

## What is genuinely working now

### ✅ Native agent runtime and reliability core
- `NativeLightRuntime` is the first-class native agent state machine.
- Persistent plans, pause/resume, continuation journals, model fallback, stall/duplicate-call guards, fresh-read-before-mutation after resume, mutation/verification tracking, and long-run safety pauses exist.
- HTTP 408/429 and transient backend/model failure handling are covered; 524-style upstream/proxy timeouts remain an infrastructure reality and cannot be fixed solely by increasing local client timeouts.
- Short continuation commands such as `retry`, `resume`, `continue`, `weiter`, and `fortfahren` reuse the persistent plan instead of silently creating a new task.

### ✅ Text-first tool protocol with optional native OpenRouter mode
- Provider-independent text Protocol v2 is the default:
  `TOOL_CALL <name>` + strict JSON arguments + `END_TOOL_CALL`.
- Semantic JSON repair is intentionally disabled. Malformed calls are not guessed into executable operations.
- Legacy valid tool-call formats remain readable for compatibility.
- Provider-native OpenRouter function calling exists only behind `native_openrouter_tool_calling=false/true`; default remains text-based.
- Final-response recovery no longer treats normal review prose containing `tool_call`, `TOOL_CALL`, `END_TOOL_CALL`, or parser function names as malformed protocol.
- Orphan protocol tails such as `}}\n</tool_call>` are still treated as protocol debris and repaired rather than shown as a valid final answer.

### ✅ Progressive `on_demand` capability selection
- Capability taxonomy, deterministic resolver, bounded initial working set, stable meta-tools, `toolbox_search`, `capability_request`, and bounded dynamic expansion exist.
- Resume capability selection is anchored to the original persistent `plan.task`, not merely the short continuation word.
- A resumed plan blocks new mutations until a fresh successful inspection/check has occurred.
- This is a working progressive system, but routing quality still needs real-world evals across more model families and task classes.

### ✅ Unified settings foundation
- Canonical settings registry and `SettingsStore` exist with validation/persistence.
- CLI supports `settings list/get/set/reset/explain/schema/doctor`.
- GUI settings are schema-backed for the implemented settings and share the same session/settings state.
- Typed LLM settings tools exist. Security-boundary reductions require host-side approval/policy rather than model self-authorization.
- `native_openrouter_tool_calling` is exposed in both GUI and CLI/settings state.

### ✅ Plugin/ToolProvider foundation
- Built-in ToolProvider abstraction, manifest parser, discovery scopes, deterministic override handling, enable/disable state, `plugin list/info/enable/disable/doctor/paths`, and security metadata exist.
- Built-in `settings` and `local-os` providers are executable.
- **External Python providers are deliberately not executed yet.** Their manifests may be discovered, but executable loading remains blocked until a real trust/install broker exists. This is a security feature, not a missing accidental import.

### ✅ Privilege/security broker foundation
- `PrivilegeBroker`, execution risk assessment, GUI/terminal/headless policy paths, destructive-command checks, protected-path checks, workspace escape approval, and typed tool security metadata exist.
- Host-side policy remains authoritative. Models cannot grant themselves privilege.
- Coding-only restrictions currently remain intentionally stricter than the broad long-term system-assistant vision.

### ✅ Subagents / skills / hooks foundation
- Bounded subagent profiles and `subagent_run` exist. Recursive subagent spawning is blocked by removing `subagent_run` from delegated tool sets.
- Native skills, guidelines, commands, and lifecycle hooks exist and have focused tests.
- Treat subagents primarily as context/capability isolation, not as an excuse to multiply models indiscriminately.

### ✅ Local OS + MCP read-only foundation
- Typed local OS diagnostics exist through `LocalOSToolProvider`.
- AICoder can expose an approved local provider through MCP stdio; `local-os` is the current default local provider.
- MCP client registry/management and verification tests exist.
- This does **not** mean arbitrary root/system administration over MCP is release-ready. Mutating OS capability must remain privilege-brokered and narrowly typed.

### ✅ Optimizer and change journal foundation
- Evidence-first optimizer inspect/plan flows exist.
- Structured change journal and rollback support exist for reversible supported actions.
- The safe target remains inspect → plan → explicitly approved apply → verify → rollback where possible.

### ✅ Provider/model hygiene foundation
- Model capability normalization, model transport helpers, provider/credential inspection commands, secret-presence reporting without printing values, and secret redaction tests exist.
- TriForce remains the default gateway. Do not duplicate backend secrets into the client.

### ✅ Chat continuity and minimal tool evidence
- SQLite chat history persists user/assistant sessions. GUI currently displays the 15 most recent sessions, but the DB retains older sessions unless explicitly deleted.
- Reopened chats now preserve the first historical user message correctly in model context.
- Per-session tool evidence stores only: timestamp, tool name, `ok/error/blocked`, iteration, and plan id.
- Tool arguments, paths, raw outputs, source snippets, credentials, and arbitrary tool content are **not** persisted as chat evidence.
- Up to the last 40 tool events are rendered as compact metadata context so a model can notice prior activity while still re-inspecting the live workspace for exact evidence.

## Known remaining weaknesses / realistic limits

### 🔴 First-turn / startup reliability still needs targeted reproduction
The GUI can occasionally appear to start an agent run but require a manual `retry` before useful tool execution begins. This is a real user-observed issue and should be reproduced with event/state logging before speculative fixes. Do not mask it with automatic infinite retries.

### 🔴 Real-world model compatibility is broader than unit tests
Qwen and Nemotron have completed real text-tool smoke/review runs, including normal final answers. More models/providers must be tested for:
- strict Protocol v2 compliance;
- malformed-call recovery;
- long context/tool loops;
- finalization after many tool calls;
- fallback transitions;
- short `retry`/resume behavior.

### 🔴 External plugin trust/install lifecycle is incomplete
Discovery is not the same as safe execution. Before external executable plugins are allowed, implement and test:
- explicit install/trust flow;
- immutable/identified plugin source or digest where practical;
- environment-variable allowlisting/sanitization;
- permission/capability declaration review;
- update/remove lifecycle;
- clear user approval for executable code.
Do not simply `import` user/workspace Python from discovered manifests.

### 🟡 GUI/CLI parity is strong at the settings core, not perfect everywhere
Do not claim every operational command has a GUI equivalent. v1.2 acceptance should require parity for canonical runtime/settings controls and critical agent behavior, not a GUI clone of every CLI subcommand.

### 🟡 Local OS/system-assistant vision must stay narrower than the marketing vision until hardened
The current repository guidance remains coding-oriented and blocks broad raw shell/admin behavior. That is acceptable for safety. Expand system operations only through typed capabilities, privilege policy, verification, and tests. Avoid turning v1.2 into unrestricted remote root automation.

### 🟡 Context durability must remain compact
Do not persist raw tool outputs merely to make resume “remember everything.” Conversation history is semantic context; plan/journal is crash state; live workspace is source of truth; compact tool metadata is evidence. Re-read files/state when exact details matter.

### 🟡 Agent/runtime modules are becoming large
`agent_runtime.py`, `executor.py`, and GUI widgets are sizeable. Refactor only when tests and responsibility boundaries justify it. Do not perform a cosmetic rewrite before release. Prefer extracting stable subsystems when a concrete maintenance/testability problem appears.

### ⚪ 600-second model timeouts are not a default solution
The client currently caps adaptive model attempts at 300 seconds, while upstream gateways/proxies may fail sooner (for example observed 524 behavior). Increasing a local cap cannot overcome an upstream timeout. Fix/request-streaming/infrastructure behavior at the correct layer instead of blindly raising constants.

## Immediate pre-1.2 priority order

1. Reproduce and fix the intermittent first-turn/manual-`retry` startup issue.
2. Run repeated end-to-end agent tasks across Qwen, Nemotron, and additional representative provider/model families.
3. Harden chat/session continuation and evidence behavior with GUI-level regression tests, including reopen → continue → tool use → final answer.
4. Finish external plugin trust/install lifecycle **or explicitly keep external executable providers out of v1.2**.
5. Validate privilege behavior on actual supported Linux desktop/headless paths without weakening the coding-only safety baseline.
6. Validate MCP local provider serving/client management end-to-end and document the read-only vs mutating boundary.
7. Complete packaging/install smoke tests (PyInstaller/Debian/installed binary) on clean systems, including version and dependency behavior.
8. Unify final version metadata only when release acceptance passes; then bump from `0.9.9` to `1.2.0`.
9. Update README/CHANGELOG/security/plugin/MCP/migration documentation to match what actually ships.
10. Avoid new large features unless they close a release criterion or a reproduced reliability/security gap.

---
# 0. ABSOLUTE OPERATING RULES

Follow these rules throughout the task.

1. **Understand before changing.**
   - Inspect the current implementation, call sites, tests, packaging, and live behavior.
   - Do not assume old architectural notes are correct.
   - Search the code before designing a replacement.

2. **Preserve unrelated user work.**
   - Start with `git status --short` and targeted diffs.
   - Do not reset, stash, delete, overwrite, or stage unrelated modifications.
   - Never use `git add .`.
   - Stage only explicit intended paths if a commit is requested.

3. **Create a timestamped backup before modifications.**
   - Prefer something like:
     `/home/zombie/ai-coder/.backups/v1.2-agentic-YYYYMMDD-HHMMSS/`
   - Back up every file you intend to modify before the first edit.
   - Report the backup location and rollback command.

4. **Smallest coherent change first.**
   - Do not rewrite the whole application at once.
   - Introduce clean interfaces and migrate existing behavior behind them.

5. **Test after each phase.**
   - Syntax/compile checks.
   - Focused unit tests.
   - Relevant integration tests.
   - GUI offscreen tests where possible.
   - PyInstaller packaged binary smoke tests before calling v1.2 releasable.

6. **If the same failure occurs twice, stop repeating the same hypothesis.**
   - Reinspect evidence and change approach.

7. **Do not trigger real sudo/password prompts unexpectedly.**
   - Mock privilege flows first.
   - Only perform an interactive terminal/GUI elevation test after telling the user exactly what prompt will appear and why.

8. **Never read, print, log, store, transmit, or expose raw API keys or sudo passwords.**
   - Credential discovery may inspect variable names/presence only.
   - Secret values must be redacted at every layer.

9. **Do not silently change providers/models.**
   - Routing may be configurable, suggested, or automatically enabled only under an explicit user policy.
   - A fallback must be distinct from the primary route.

10. **Do not weaken security by an LLM settings change without explicit user authorization.**
    - Security-reducing changes must require confirmation even if the current approval policy is permissive.

---

# 1. CURRENT REPOSITORY / WORKTREE FACTS — VERIFY THEM FIRST

Repository / source of truth:
`/home/zombie/ai-coder`

The self-test target may be copied to:
`/home/zombie/workspace/ai-coder`

Never patch a stale server copy and never treat the workspace copy as the source of truth. When testing AICoder against itself, first synchronize/copy the current source intentionally, then point AICoder at the workspace copy.

Verified on 2026-08-22:
- reviewed HEAD before this roadmap update: `6b48ecc`;
- full test suite: 379/379 passing;
- canonical package metadata currently reports `0.9.9` in `pyproject.toml`;
- `aicoder.__version__` resolves from source/distribution metadata rather than maintaining a second hard-coded release number;
- current `AGENTS.md` is AICoder-specific (the earlier stale TriForce backend layout problem has been corrected);
- current GUI history menu displays 15 recent sessions, while SQLite may retain more sessions;
- current default model gateway remains TriForce `/v1/client/chat`;
- OpenRouter native function calling is opt-in, not default.

Operating hygiene:
- Always inspect `git status` and diffs before editing.
- Preserve unrelated work and create targeted backups before risky changes.
- Do not overwrite `/home/zombie/ai-coder` from `/home/zombie/workspace/ai-coder`.
- Do not bump to `1.2.0` until release acceptance tests, packaging smoke tests, and documentation are complete.
- Keep backend-managed provider secrets on the backend/vault unless a deliberately scoped BYOK/local-provider feature is implemented.
- A clean unit test suite is necessary but not sufficient; real GUI/model/provider runs are part of acceptance.

---

# 2. PRODUCT VISION FOR V1.2

AICoder v1.2 should be able to do more than write code.

It should become a **purpose-driven agentic workstation/system assistant** capable of:

- coding and refactoring;
- repository analysis;
- web/research tasks;
- debugging;
- local system diagnosis;
- package/service/container management;
- goal-oriented Linux/system optimization;
- selecting relevant tools dynamically instead of loading the whole toolbox;
- delegating focused work to subagents;
- changing its own non-secret settings when the user asks;
- explaining every setting;
- sharing one settings source between CLI, REPL, GUI, and LLM tools;
- exposing/consuming MCP capabilities cleanly;
- being extensible through plugins rather than growing a giant hardcoded core;
- preserving strict privilege and destructive-action boundaries;
- verifying changes and supporting rollback.

The core principle is:

> **AICoder is the agent. Plugins, MCP servers, skills, models, and subagents are capabilities AICoder can acquire and use.**

Do not make AICoder a thin launcher around Claude Code, Codex, Gemini CLI, OpenCode, etc. Those may remain optional integrations or reference implementations, but the native AICoder agent loop must be first-class.

---

# 3. TARGET ARCHITECTURE

Aim for this conceptual structure:

```text
User
 │
 ├── CLI / REPL
 └── GUI
      │
      ▼
AICoder Agent Runtime
 ├── Conversation / Plan State
 ├── Intent + Capability Resolver
 ├── Dynamic Tool Provisioner
 ├── Agent Loop
 ├── Subagent Manager
 ├── Skill Registry
 ├── Plugin Registry
 ├── Settings Registry + Settings Store
 ├── Privilege Broker
 ├── Change Journal / Rollback
 ├── Provider / Model Capability Layer
 └── Telemetry / Evals
      │
      ├── Built-in Tool Providers
      │    ├── Local files/code
      │    ├── Local OS
      │    ├── Settings
      │    └── Toolbox/Capability discovery
      │
      ├── Plugins
      │    ├── skills
      │    ├── subagents
      │    ├── commands
      │    ├── hooks
      │    ├── policies
      │    ├── settings schema
      │    └── MCP servers/tool providers
      │
      └── MCP Clients
           ├── TriForce MCP
           └── user/project MCP servers
```

Also allow AICoder capabilities to be exposed externally:

```text
aicoder mcp serve
  └── expose approved local ToolProviders through MCP
```

This allows the Local OS provider to be both:
- a native AICoder plugin/tool provider; and
- an MCP server for other compatible agents.

Do not make AICoder talk to its own local MCP subprocess if direct in-process ToolProvider invocation is cleaner. The same provider implementation should be reusable behind an MCP adapter.

---

# 4. FIRST-CLASS PLUGIN / EXTENSION SYSTEM

AICoder currently has no real plugin layer. Build one.

## 4.1 Goals

Plugins should be able to contribute:

- tools / ToolProviders;
- MCP server definitions;
- settings schema entries;
- skills;
- subagents;
- commands;
- hooks;
- policy rules;
- capability tags/metadata;
- optional GUI settings sections generated from schema.

Avoid arbitrary GUI code execution for v1.2 unless necessary. Prefer declarative contributions.

## 4.2 Discovery scopes

Implement clear precedence, for example:

1. built-in plugins;
2. user plugins:
   `~/.config/ai-coder/plugins/`
3. workspace plugins:
   `<workspace>/.aicoder/plugins/`

Workspace should win over user, user over built-in when IDs conflict, but conflicts must be surfaced.

Consider interoperable skill aliases such as:
- `.agents/skills/`
- user `~/.agents/skills/`

Do not silently execute install scripts from an untrusted plugin.

## 4.3 Manifest

Use a simple versioned manifest, preferably TOML because Python 3.11+ has `tomllib`.

Example direction:

```toml
[plugin]
id = "local-os"
name = "Local OS"
version = "1.0.0"
api_version = "1"
description = "Typed local operating-system capabilities"

[capabilities]
groups = ["system", "processes", "packages", "services", "containers", "network", "storage"]

[security]
trusted_builtin = true
```

Support declarative references to:
- tools provider entrypoint;
- skills directory;
- agents directory;
- hooks;
- settings schema;
- MCP servers.

Validate manifests strictly.

## 4.4 CLI

Add:

```text
aicoder plugin list
aicoder plugin info ID
aicoder plugin enable ID
aicoder plugin disable ID
aicoder plugin doctor [ID]
aicoder plugin paths
```

Optional local-path install for v1.2:

```text
aicoder plugin install /path/to/plugin
```

Do not make remote marketplace installation mandatory for v1.2.

## 4.5 Trust model

A plugin must never bypass:
- tool enable/disable settings;
- capability policy;
- PrivilegeBroker;
- destructive-action checks;
- secret redaction;
- audit/change journal.

Every tool needs normalized security metadata:
- read-only vs mutating;
- destructive;
- requires elevation;
- network access;
- external side effect;
- user data sensitivity where applicable.

Unknown tools should default to the safer classification.

---

# 5. UNIFIED SETTINGS CORE

This is a required v1.2 foundation.

Current problems:
- settings definitions/defaults/choices are duplicated between `session_state.py`, CLI, GUI, and REPL;
- `state.json` uses a process-local cache, so a long-running GUI can keep stale data after a CLI process changes the file;
- current writes use direct `write_text`, without an atomic replace;
- `threading.Lock` is process-local and does not protect GUI/CLI concurrent processes;
- several important settings are not exposed through normal CLI help/status.

## 5.1 Canonical Settings Registry

Create one authoritative schema.

Each setting should define metadata such as:

```text
key
type
default
choices
min/max
description
group
aliases
sensitive
mutable
restart_required
security_impact
cli_parser
```

At minimum cover current runtime settings:

- `selected_model`
- `fallback_model`
- `swarm_mode`
- `workspace_root`
- `tool_mode`
- `enabled_tools`
- `request_timeout`
- `approval_mode`

Add agentic v1.2 settings where justified, e.g.:

- `tool_budget`
- `auto_expand_tools`
- `tool_discovery_strategy`
- `tool_recommendation_mode`
- `max_agent_turns`
- `planning_mode`
- `subagents_enabled`
- `max_parallel_subagents`
- `optimizer_mode`
- `change_journal_enabled`
- possibly `model_routing_mode`

Do not add settings merely because they sound interesting. Every setting needs a clear runtime consumer and test.

## 5.2 Persistence

Replace the fragile state writer with a proper `SettingsStore`:

- atomic temp-write + flush/fsync + `os.replace`;
- file mode `0600`;
- cross-process locking;
- external-change detection via revision/mtime/hash;
- no permanently stale in-memory GUI cache;
- migration/version field for future schema changes;
- corruption handling that preserves the broken file for diagnosis instead of silently destroying it.

Make it cross-platform where practical:
- POSIX locking and Windows equivalent, or use a small reliable lock dependency if justified.

## 5.3 Invariants

Keep central invariants, for example:
- fallback must not equal primary;
- timeout bounds;
- valid enum choices;
- enabled tools canonicalized;
- workspace normalized;
- security mode validation.

Validation belongs in the settings core, not independently in each UI.

---

# 6. COMPLETE SETTINGS CLI AND HELP

The user wants every setting discoverable, explainable, and settable from CLI.

Implement:

```text
aicoder settings
aicoder settings list
aicoder settings list --json
aicoder settings get KEY
aicoder settings set KEY VALUE
aicoder settings reset KEY
aicoder settings reset --all
aicoder settings explain KEY
aicoder settings schema --json
aicoder settings doctor
```

Requirements:

- `aicoder --help` must clearly expose the settings command and meaningful examples.
- `aicoder settings --help` must enumerate the complete schema or provide generated subcommand help that exposes:
  - valid values;
  - defaults;
  - descriptions;
  - aliases.
- Keep current `aicoder model`, `fallback`, and `swarm` commands as backward-compatible aliases.
- Do not expose tokens/secrets.
- `aicoder status` should show all effective non-sensitive settings, not only model/fallback/swarm/workspace.
- Support deterministic machine-readable JSON.

For `enabled_tools`, design unambiguous syntax:
- comma-separated list and/or repeated flags;
- `all`;
- `none`;
- JSON only if clearly documented.

Do not conflate LLM request timeout with unrelated shell/subprocess timeout flags.

---

# 7. GUI / CLI / REPL SETTINGS PARITY

The GUI must not maintain a parallel hardcoded settings model.

Refactor GUI Settings to use the same schema and SettingsStore.

Requirements:

- choices/defaults/descriptions/tooltips come from the schema;
- CLI changes become visible in a running GUI without restart where safe;
- GUI changes become visible to CLI immediately;
- model/fallback/tool mode/approval mode/tools/timeout all round-trip;
- plugin-defined settings can appear automatically in a plugin/settings section;
- validation errors are identical across GUI and CLI;
- security-sensitive changes are visibly marked.

REPL slash commands should call the same Settings API.

---

# 8. LLM-CONTROLLABLE SETTINGS

AICoder should be able to configure itself when the user asks in natural language.

Do NOT let the model edit `state.json` directly.

Create typed local tools, e.g.:

```text
settings_list
settings_describe
settings_get
settings_plan_patch
settings_apply_patch
settings_reset
```

Better if names are namespaced internally, e.g.:
`settings.list`, `settings.describe`, etc., while normalizing provider tool naming as needed.

Example user request:

> Make yourself more autonomous for coding, load tools only when needed, use 180 seconds for agent requests, but still ask before sudo and deletion.

The agent should:
1. inspect the relevant settings schema;
2. produce a structured proposed patch;
3. explain important behavioral/security changes;
4. apply safe settings if authorized;
5. require explicit approval for security reductions;
6. verify persisted values.

CRITICAL:
- the model may NEVER use a settings change to bypass a pending approval;
- changing `approval_mode`, destructive policy, trusted plugins, sandbox/elevation behavior, credential sources, or remote execution boundaries must require explicit user confirmation;
- changing a setting must be journaled with old/new values, excluding secrets.

---

# 9. AGENTIC `on_demand` TOOL PROVISIONING

The current mode is too coarse:

```text
simple greeting -> no tools
everything else -> full selected tool catalog
```

Replace this with **intent/capability-aware progressive disclosure**.

## 9.1 Capability model

Define capability groups independently from individual tool names.

Example:

```text
web
local_code_read
local_code_write
remote_code
debug
system_diagnostics
packages
services
containers
network
storage
memory
models
research
settings
git
testing
```

Each tool declares one or more capability tags.

Do not hardcode giant scattered lists in `executor.py`.

## 9.2 Hybrid resolver

Use a cheap deterministic first pass:

Signals can include:
- URL;
- file path;
- stack trace;
- code block;
- git URL/hash;
- package/service/docker terminology;
- “latest/current/search/find”;
- explicit action verbs;
- settings/configuration request;
- system performance/optimization request.

Example:
- URL -> `web`
- GitHub repository URL -> `web` initially, then expand to code/git if needed
- traceback -> `debug + local_code_read`
- “why is Docker slow” -> `system_diagnostics + containers`
- “change AICoder settings” -> `settings`
- greeting -> no tools

When deterministic confidence is low, an optional lightweight router model may classify intent, but do not add a mandatory extra LLM call to every prompt.

## 9.3 Initial tool budget

Add a configurable initial budget such as:
- 4–8 tools by default;
- do not dump 40–100 tool schemas into every agent turn.

Rank tools by:
- capability match;
- tool description;
- recent successful use;
- explicit user intent;
- provider/model tool capability.

## 9.4 Dynamic expansion

AICoder must be able to add more tools during the same task.

Provide an always-available lightweight meta capability such as:

```text
toolbox_search
capability_request
```

Example flow:

```text
User gives URL
 -> active tools: crawl, search, web_fetch
 -> agent discovers it is a Git repository
 -> capability_request("local_code_read/git")
 -> code/git tools become available on the next model turn
```

Do not let expansion bypass `enabled_tools` or plugin policy.

Set:
- max active tool count;
- max expansion rounds;
- clear stop rules.

## 9.5 MCP tool recommendation

TriForce may eventually expose server-side recommendation such as:
- `tools/recommend`
- `capabilities/resolve`

Design the client resolver so server-side ranking can be plugged in later.

For v1.2, a local resolver is acceptable if the backend lacks this endpoint.

If MCP list responses include cache metadata in the negotiated protocol, honor it. Keep a fallback TTL for older servers.

## 9.6 URL behavior

Explicitly test:
- bare URL;
- “summarize this URL”;
- “check whether this URL is safe”;
- GitHub URL;
- documentation URL;
- URL plus “do not browse”.

A URL should naturally seed web capabilities in `on_demand` mode unless the user explicitly forbids external access.

---

# 10. TOOL-CALL NORMALIZATION ACROSS PROVIDERS

Do not reduce tool calls to only `{name, arguments}` if the provider supplies an ID.

Introduce a normalized structure such as:

```python
@dataclass
class ToolCall:
    id: str | None
    name: str
    arguments: dict
    provider: str | None
    raw_type: str | None
    metadata: dict
```

Preserve the correlation identifier across:
- OpenAI-compatible tool calls;
- Anthropic;
- Gemini;
- Mistral;
- Groq;
- Ollama;
- backend-native formats.

This is important for provider APIs that require a tool result to reference the original tool/function call ID.

Support:
- multiple tool calls;
- parallel calls when safe and provider/model supports them;
- sequential/compositional calls;
- streamed partial tool-call argument assembly;
- strict JSON validation;
- no guessing a badly truncated destructive tool call.

Keep current repair behavior only for clearly recoverable formatting errors and test it.

---

# 11. MODEL / PROVIDER CAPABILITY LAYER

Do not route by brand-name assumptions.

The current working-tree change in `aicoder/client.py` prefers backend `model_details`. Preserve and build on that if valid.

Normalize model metadata:

```text
id
provider
capabilities:
  chat
  code
  tool_calling
  parallel_tools
  vision
  reasoning
  streaming
context_window
latency class / observed performance
availability
```

Use this for:
- deciding whether a selected model can run the requested agent loop;
- choosing a compatible fallback;
- optionally suggesting a better model;
- selecting a cheap router/subagent model;
- avoiding tool mode for models that cannot actually call tools.

Routing modes should be explicit, e.g.:
- `fixed`
- `suggest`
- `auto`

Never silently replace the user's fixed primary model under `fixed`.

A system-optimization task should be routable to a capable reasoning/tool model, not treated as “coding only”.

---

# 12. PROVIDER CREDENTIAL HYGIENE / DOCTOR

The development TriForce environment currently contains environment-variable names for many providers, including:
- OpenAI;
- Anthropic;
- Gemini/Google;
- Mistral/Codestral;
- Groq;
- Cerebras;
- OpenRouter;
- Cloudflare;
- Together;
- Cohere;
- Hugging Face;
- Fireworks;
- Jina;
- NVIDIA;
- Kimi;
- Ollama-related configuration;
and others.

Known aliases/legacy names include examples such as:
- `GOOGLE_AI_STUDIO_KEY`
- `GOOGLE_GEMINI_KEY`
- `GEMINI_API_KEY`
- `GOOGLE_API_KEY` may be the official SDK form even if not currently present
- `MIXTRAL_API_KEY`
- `MISTRAL_API_KEY`
- `CODESTRAL_API_KEY`

There may also be duplicate values across:
- `/home/zombie/triforce/.env`
- `/home/zombie/triforce/docker/.env`
- `/home/zombie/triforce/auth/.env.agents`
- other service-specific env files.

Do NOT print values.

Build or add a provider/credentials doctor that can report only:

```text
provider
credential source
present/missing
alias/legacy warning
backend model availability
last harmless health check status
```

Example CLI:

```text
aicoder providers list
aicoder providers doctor
aicoder credentials status
```

For normal released AICoder:
- prefer TriForce backend-managed credentials;
- never package provider secrets;
- never copy server `.env` secrets into the desktop client.

If BYOK is added:
- use OS keyring/secure store as canonical local storage where available;
- environment variables are a compatibility source;
- never commit `.env`;
- never expose keys in GUI/logs/tool output;
- support `getpass`/secure GUI entry;
- do not echo secrets;
- do not send a provider key to any other provider/model.

AICoder must be able to say:
> “Anthropic credential is present via backend”  
without revealing the credential.

Add legacy-key warnings rather than auto-deleting or auto-migrating secrets.

Special current Gemini concern:
- include a provider doctor rule that can surface current official key/auth migration requirements based on current backend/provider metadata or documentation;
- do not hardcode a warning that will become stale forever—make warnings versioned/documented.

The TriForce vault may be locked in the development environment. Do not try to bypass/unlock it or expose its contents.

---

# 13. LOCAL OS CAPABILITY / OS MCP

Build a **Local OS plugin/tool provider** as a flagship v1.2 extension.

Do not simply reuse the remote TriForce system tools and accidentally operate on the backend server when the user meant the local machine.

The same underlying local OS provider should be exposable through MCP.

## 13.1 Local OS capability groups

Start with typed, auditable capabilities:

### Read-only
- system overview;
- CPU;
- memory;
- disk/filesystem;
- load;
- processes;
- network interfaces/routes/connections/DNS/ports;
- package manager detection and upgradable packages;
- service status/logs;
- container list/status/stats/logs;
- kernel/OS release;
- GPU detection/status where available;
- power profile/governor where available;
- journal/kernel logs;
- failed systemd units.

### Mutating
- package install/upgrade with explicit package names;
- service start/stop/restart/enable/disable;
- container start/stop/restart;
- safe process termination with PID validation;
- power profile changes;
- managed sysctl/config changes only through explicit typed operations;
- other changes only after evidence and plan.

Avoid exposing “arbitrary root shell” as the primary OS API.

Keep generic local shell as a separate escape hatch with stricter approval.

## 13.2 Typed schemas

Prefer:

```text
os.system.overview
os.process.list
os.process.find
os.package.search
os.package.list_upgradable
os.package.install
os.service.status
os.service.logs
os.service.restart
os.container.list
os.container.stats
os.network.routes
os.network.ports
os.storage.overview
os.power.status
os.kernel.info
os.logs.kernel
```

Normalize actual provider/tool names to whatever the existing tool calling stack supports, but keep a clean logical namespace in code.

## 13.3 MCP exposure

Add:

```text
aicoder mcp serve --plugin local-os --transport stdio
```

Optional later:
```text
--transport http --bind 127.0.0.1
```

Prefer stdio for the local desktop/CLI use case.

If Streamable HTTP is implemented:
- bind to localhost by default;
- validate Origin;
- require appropriate authentication for non-local exposure;
- never default to `0.0.0.0`.

The OS provider must still pass through AICoder's central security/privilege policy when used inside AICoder.

---

# 14. PURPOSE-DRIVEN SYSTEM OPTIMIZER

AICoder should be able to optimize a machine for a user goal, but must not behave like a random “Linux tweak script”.

Example goals:
- coding;
- workstation;
- local AI;
- Docker host;
- server;
- battery;
- low latency;
- privacy;
- gaming;
- custom.

Natural language is primary:

> I use this laptop for Python, Docker, local LLMs and browser work. Battery matters more than peak CPU speed.

Convert this into a structured goal profile:

```yaml
priorities:
  development: high
  containers: high
  local_ai: high
  stability: very_high
  battery: medium_high
  peak_performance: medium
```

## 14.1 Optimizer loop

Use:

```text
Inspect
 -> Diagnose
 -> Build plan
 -> Explain evidence
 -> Snapshot / backup
 -> Apply approved changes
 -> Verify
 -> Benchmark/check
 -> Keep or rollback
```

Do not change settings merely because they are popular online.

Every optimization action needs:
- observed current state;
- reason;
- expected benefit;
- risk;
- scope;
- whether reboot/restart is needed;
- rollback plan;
- verification method.

Example:
- do not change swappiness if current behavior is already appropriate;
- do not disable services unless their purpose and impact are understood;
- do not write global sysctl values without a managed config file and rollback.

## 14.2 CLI

Potential commands:

```text
aicoder optimize inspect
aicoder optimize plan "coding + local AI, prioritize stability"
aicoder optimize apply PLAN_ID
aicoder optimize verify PLAN_ID
aicoder optimize rollback PLAN_ID
```

Natural-language agent use should call the same APIs.

---

# 15. CHANGE JOURNAL / SNAPSHOT / ROLLBACK

For system and agent configuration changes, add a structured journal.

Store non-secret records under the AICoder config directory.

Each entry should include:
- timestamp;
- session/task ID;
- tool/action;
- normalized arguments with secrets redacted;
- user/model reason;
- risk classification;
- approval decision;
- previous state when available;
- backup/snapshot path;
- result;
- verification;
- rollback metadata.

Do not store raw credentials.

For file/config changes:
- backup before write;
- preferably managed drop-in files instead of rewriting vendor files.

For optimization plans:
- every applied action must have a rollback status.

Add commands such as:

```text
aicoder changes list
aicoder changes show ID
aicoder changes rollback ID
```

Do not promise rollback when an operation is intrinsically irreversible; mark it accordingly.

---

# 16. PRIVILEGE BROKER — TERMINAL + GUI + HEADLESS

Centralize elevation logic.

Current architecture already has:
- risk classification in `aicoder/privileges.py`;
- terminal `sudo -v` validation;
- GUI executor behavior using `pkexec`/Polkit;
- tests for some policy modes.

However the design currently contains more than one elevation concept and has not been fully end-to-end verified interactively.

Create one coherent `PrivilegeBroker` (or equivalent) that owns:

```text
risk classification
 -> policy decision
 -> user confirmation if required
 -> local authentication
 -> execution strategy
 -> audit/journal
 -> result
```

## Terminal
- use sudo through the controlling TTY;
- password goes directly to sudo;
- AICoder never receives the password;
- support cached sudo credentials;
- fail clearly without an interactive TTY when auth is needed.

## GUI
- use Polkit/pkexec where appropriate;
- ensure a graphical authentication agent exists;
- if unavailable, fail closed with a clear message;
- optional explicit “open terminal authentication” fallback only if intentionally designed.

## Headless
- never hang waiting for a password;
- fail closed if elevation is required and no valid noninteractive authorization exists.

## Security invariants
- elevation always requires a concrete reason;
- deletion/destructive actions remain separately classified;
- generic `all`/autopilot must not silently bypass credential authentication;
- settings changes cannot downgrade these rules behind the user's back;
- plugin tools cannot bypass the broker.

Test:
- ask/autopilot/sudo_only/all;
- normal write;
- delete;
- sudo read/write;
- cached sudo;
- rejected password;
- cancel;
- timeout;
- missing Polkit agent;
- no DISPLAY;
- no TTY;
- shell redirection requiring root;
- pipe semantics;
- direct argv without shell when possible.

Do not perform the real interactive password test until unit/integration mocks pass and the user agrees.

---

# 17. SUBAGENTS

Add or improve first-class subagents.

Principles:
- separate context;
- focused purpose;
- restricted toolset;
- independent model selection;
- max turns / timeout;
- return concise structured findings to the parent;
- no infinite nested spawning;
- optional background/parallel execution only where safe.

Useful built-in subagents:
- Explore / Codebase Investigator — read-only;
- Research — web/docs;
- Debugger;
- Security Reviewer;
- Test Runner;
- System Diagnostician;
- Optimizer Planner.

Define agents declaratively, e.g. Markdown + YAML frontmatter.

Support project/user/plugin agent locations.

Allow tool capability lists, not only raw tool names.

Optional worktree isolation for code-changing subagents is desirable if it can be implemented cleanly.

Do not let background agents trigger interactive approvals. They should auto-deny an action that would require a prompt and report the blocker.

---

# 18. SKILLS / PROCEDURAL CAPABILITIES

Implement discoverable on-demand skills if not already first-class.

A skill should package:
- name;
- description/trigger semantics;
- instructions;
- optional scripts/references/assets;
- required capabilities;
- optional model preference.

Discovery tiers:
- built-in;
- plugin;
- user;
- workspace.

Support `.agents/skills/` compatibility where practical.

Skills should be progressive disclosure:
- their descriptions can be visible cheaply;
- full instructions load only when selected.

Examples:
- release checklist;
- security audit;
- WordPress deployment;
- Python package migration;
- Docker debugging;
- system optimization workflow.

AICoder should not stuff every skill body into every system prompt.

---

# 19. HOOKS / POLICY ENGINE

Add a restrained hook/policy layer for customization.

Useful lifecycle events:
- session start/end;
- before/after tool use;
- tool failure;
- before approval;
- after settings change;
- before/after subagent;
- before/after file modification;
- before final response.

Hooks may:
- add context;
- log;
- validate arguments;
- block an action;
- run a safe command;
- invoke a read-only verifier.

Do not allow a hook to bypass security policy.

Plugin hooks run under plugin trust/capability constraints.

Prefer deterministic policy rules for security over LLM-only approval classifiers.

---

# 20. MCP CLIENT MANAGEMENT

Current AICoder can call the TriForce MCP, but configuration should become first-class.

Add a clean MCP registry:

```text
aicoder mcp list
aicoder mcp add NAME ...
aicoder mcp remove NAME
aicoder mcp enable NAME
aicoder mcp disable NAME
aicoder mcp tools NAME
aicoder mcp doctor NAME
```

Support at least:
- stdio;
- Streamable HTTP.

Keep TriForce as a built-in/default server profile rather than hardcoding all behavior into the executor.

Per-server settings:
- enabled;
- transport;
- command/url;
- args;
- env variable names/references, never exposed values;
- tool allow/deny filters;
- trust classification;
- timeout;
- capability tags.

External MCP auth:
- use proper transport auth;
- do not pass one MCP server's token to another;
- never put access tokens in URL query strings;
- prefer secure secret storage.

Tool lists must be cached safely but refreshable.

---

# 21. SYSTEM PROMPT / AGENT LOOP CLEANUP

Do not grow one enormous static system prompt.

Move toward composable prompt sections:
- core identity;
- current permissions;
- workspace instructions / AGENTS;
- active capabilities/tools;
- current plan;
- relevant skill;
- current plugin guidance.

Avoid repeated instructions.

Only include tool descriptions actually active for the current turn.

Preserve repository/project instructions from `AGENTS.md`, but implement a correct discovery hierarchy and fix the stale AICoder repository file.

Planning:
- complex modifications should create a plan;
- plan state should persist through the task/session;
- a simple question should not pay the planning overhead.

Continue short confirmations (“yes”, “mach”, “continue”) using conversation/task state.

Keep current loop guard behavior and improve it with:
- repeated call detection;
- repeated error detection;
- progress detection;
- max turns;
- explicit blocked state.

---

# 22. TOOL SECURITY / APPROVAL METADATA

Move from tool-name-only assumptions to metadata-first security.

Every tool should normalize:

```text
read_only
mutating
destructive
requires_elevation
external_side_effect
network_access
```

Use provider/MCP annotations when trustworthy, but do not blindly trust an untrusted remote server's claim that a destructive tool is read-only.

Maintain local override policies.

Unknown/unclassified tools should require safer handling.

Tool selection and approval are separate:
- a tool may be relevant but still require confirmation.

---

# 23. PROVIDER API / MODERN TOOL USE COMPATIBILITY

The implementation should be designed against current official provider patterns, not old assumptions.

Support the common abstraction:
1. send model messages + tool schemas;
2. model emits one or more structured tool calls;
3. AICoder validates/authorizes;
4. AICoder executes;
5. tool results are correlated to original call IDs;
6. model continues;
7. repeat until final answer or stop condition.

Provider adapter concerns:
- OpenAI-style tool choice: none/auto/required/allowed subset where supported;
- Anthropic tool use;
- Gemini function/tool calls with call IDs and multi-step continuation;
- Mistral function calls and agents/connectors;
- Groq local tool calling / remote MCP;
- Ollama single, parallel and multi-turn tool calling;
- backend-native TriForce formats.

Do not force every provider to use exactly the same wire payload internally.
Normalize at the AICoder agent boundary.

If direct BYOK provider adapters would threaten the v1.2 scope, keep TriForce as the production model gateway and implement the adapter interfaces/capability metadata without duplicating the whole backend. Direct providers can be plugins.

---

# 24. GUI AGENT EXPERIENCE

GUI and terminal must share the same agent runtime semantics.

No separate “GUI agent architecture”.

GUI should show:
- active model;
- fallback;
- active capability/tool count;
- on-demand tool expansion events;
- subagent activity;
- plan state;
- privilege requests;
- settings changes;
- system optimizer plan;
- verification results.

Avoid flooding the chat with internal noise.

Useful concise events:
- `Web capability loaded: crawl, search`
- `Expanded tools: containers`
- `System change requires admin authentication`
- `3 settings updated`
- `Verification passed`

The GUI must not leak secret arguments in approval previews or logs.

---

# 25. OBSERVABILITY / TELEMETRY / EVALS

Add local non-secret telemetry sufficient to optimize AICoder itself.

Track:
- tool discovery latency;
- number of tools exposed per turn;
- tool-call success/error rate;
- expansion count;
- agent iterations;
- fallback use;
- model latency;
- subagent latency;
- approval requests;
- repeated-loop recoveries.

Do not record prompt contents by default if not necessary.
Never record secrets.

Use the telemetry to answer requests such as:
> “Why are you slow?”
or
> “Optimize your agent settings for faster coding.”

A self-optimization flow may recommend settings based on observed data, but security reductions still need explicit user approval.

Add benchmark/eval scenarios for:
- greeting;
- URL;
- coding question;
- code change;
- debug traceback;
- Docker issue;
- system optimization request;
- settings request;
- provider without tool support.

Compare:
- task success;
- active tool count;
- tokens/context overhead;
- latency;
- iterations;
- incorrect tool calls.

---

# 26. V1.2 IMPLEMENTATION PHASES — VERIFIED STATUS

Do not re-implement completed foundations. Inspect the code and extend the smallest missing layer.

## Phase 0 — Baseline — ✅ completed / repeat before release
- repository inspection, backups, test baseline, runtime/GUI inspection, and version checks are established;
- repeat clean-system/package smoke checks immediately before release.

## Phase 1 — Settings foundation — ✅ core completed, 🟡 polish remains
Implemented: canonical registry/store, validation, CLI settings commands, schema/doctor, GUI integration, LLM settings tools, fallback invariant, security-change gating tests.
Remaining: audit every newly added runtime setting for GUI/CLI discoverability and eliminate any one-off setting path that bypasses the registry.

## Phase 2 — Plugin / ToolProvider foundation — 🟡 foundation completed
Implemented: manifest, scopes, registry, deterministic override behavior, built-in providers, security metadata, enable/disable/doctor/paths, tests.
Remaining: executable **external** plugin trust/install/update/remove lifecycle. Do not load arbitrary discovered Python before this exists. This may be deferred beyond 1.2 if external executable plugins are explicitly documented as unsupported.

## Phase 3 — Capability resolver / on-demand tools — ✅ functional, 🟡 eval/hardening remains
Implemented: capability taxonomy, deterministic hints, bounded working set, stable meta layer, dynamic expansion, resume anchoring, URL/tool policy tests.
Remaining: model/task eval matrix, routing quality metrics, first-turn reliability investigation, cache/latency measurement.

## Phase 4 — Privilege broker — ✅ foundation completed, 🟡 real-environment validation remains
Implemented: central risk assessment, terminal/GUI/headless policy, workspace-boundary handling, destructive/security metadata tests.
Remaining: clean-machine GUI/Polkit/headless smoke tests and documentation of supported elevation strategies.

## Phase 5 — Local OS provider + MCP exposure — 🟡 read-only foundation completed
Implemented: typed Local OS provider, read-only diagnostics, MCP stdio serving, MCP registry/client management, optimizer inspect/plan, change journal/rollback foundations.
Remaining: narrowly typed mutating OS actions only where justified, privilege-broker integration per action, verification/rollback coverage, MCP boundary documentation. Do not add unrestricted shell/root MCP.

## Phase 6 — Subagents / skills / hooks — ✅ foundation completed, 🟡 behavior eval remains
Implemented: bounded profiles, recursion prevention, skills/guidelines/commands, hooks, tests.
Remaining: measure whether delegation improves context/reliability; add worktree isolation only for real concurrent mutation workflows.

## Phase 7 — Provider/model capability + credentials doctor — ✅ foundation completed, 🟡 compatibility matrix remains
Implemented: normalized model capability layer, text-first Protocol v2, optional native OpenRouter calling, provider/credential status, redaction, tool-call normalization/ID handling tests.
Remaining: real provider/model compatibility runs and backend timeout/streaming behavior validation.

## Phase 7.5 — Chat continuity / evidence — ✅ newly implemented, 🟡 GUI E2E remains
Implemented: persistent SQLite sessions, reopened-chat context fix, compact per-session tool metadata timeline without arguments/outputs, resume safety.
Remaining: GUI reopen/continue end-to-end tests and retention UX decision. Do not silently delete old sessions merely because the menu displays only 15.

## Phase 8 — Packaging / docs / release — 🔴 not complete
Required before `1.2.0`:
- clean README/CHANGELOG/security/migration/plugin/MCP docs;
- reconcile broad system-assistant vision with actual coding-only safety boundaries;
- PyInstaller build and installed-binary smoke test;
- Debian package build/install/upgrade smoke test on clean supported Linux environments;
- verify no secrets/local config/backups enter packages;
- final version source/reporting check;
- repeated GUI agent smoke runs;
- only then bump/release `1.2.0`.

---

# 27. REQUIRED TEST MATRIX

Add focused tests rather than one huge slow suite.

## Settings
- defaults;
- invalid value;
- aliases;
- model/fallback invariant;
- atomic write;
- concurrent CLI/GUI process simulation;
- stale cache invalidation;
- corrupted state recovery;
- permissions 0600;
- CLI JSON output;
- LLM settings patch;
- security downgrade confirmation.

## Capability resolver
- greeting -> none;
- URL -> web;
- GitHub URL -> web then expandable;
- traceback -> debug/read;
- Docker -> system/container;
- settings request -> settings tools;
- “do not browse” suppresses web;
- disabled tools stay disabled;
- tool budget respected;
- expansion max respected.

## Tool calls
- OpenAI-style;
- Mistral-style;
- Gemini-style;
- Groq-style;
- Ollama-style;
- IDs preserved;
- parallel calls;
- streamed partial args;
- malformed/truncated call safely rejected.

## Plugins
- discovery precedence;
- invalid manifest;
- disabled plugin;
- duplicate ID;
- capability metadata;
- settings contribution;
- untrusted plugin cannot bypass security.

## OS
- safe probes;
- package manager detection;
- systemd missing;
- Docker missing;
- unknown service;
- command timeout;
- no root required path;
- root-required path goes through broker;
- rollback metadata.

## Privileges
- ask;
- autopilot;
- sudo_only;
- all;
- destructive;
- GUI pkexec;
- missing Polkit;
- terminal sudo;
- no TTY;
- headless;
- reason required;
- cancellation.

## GUI
- settings round trip;
- CLI external settings refresh;
- tool expansion;
- approval preview redaction;
- worker exceptions do not kill app.

## Packaging
- `dist/aicoder --help`;
- `dist/aicoder settings --help`;
- plugin discovery in PyInstaller;
- GUI import/start smoke test;
- version output;
- no accidental secret files in bundle.

---

# 28. RELEASE ACCEPTANCE CRITERIA FOR 1.2.0 — CURRENT STATUS

Do NOT call it v1.2 until all **critical** items are green. A foundation existing in code is not the same as release validation.

1. ✅ CLI exposes native agent/settings/plugin/provider/optimizer/change/MCP capabilities clearly.
2. ✅ Canonical runtime settings are discoverable/settable through settings CLI; re-audit before release.
3. ✅ GUI uses the same canonical settings/session state for implemented settings.
4. ✅ Typed LLM settings inspection/change tools exist.
5. ✅ Security-boundary changes require host-side policy/approval.
6. ✅ `on_demand` uses progressive capability selection rather than all tools for every task.
7. ✅ Bare URL/tool relevance behavior has targeted policy coverage.
8. ✅ Tools can expand during a task without restarting the session.
9. ✅ Disabled tools/provider policy is reapplied host-side.
10. ✅ Tool-call normalization/identity handling has targeted tests; 🟡 continue real-provider validation.
11. ✅ Typed Local OS diagnostics work.
12. ✅ Mutating/destructive actions route through central security assessment; 🔴 broaden OS mutations only when individually hardened.
13. 🟡 Terminal/GUI privilege paths have tests; clean desktop/Polkit validation still required.
14. ✅ Headless privilege denial is designed to fail clearly rather than hang; 🟡 clean-host smoke test still required.
15. ✅ Plugin discovery and enable/disable work.
16. ✅ Built-in Local OS provider can be exposed via MCP stdio.
17. ✅ Optimizer can inspect and produce evidence-based plans.
18. ✅ Change journal/rollback foundation exists for supported reversible actions; 🟡 expand only with verified actions.
19. ✅ Provider/credential doctor reports presence/status without exposing secret values.
20. ✅ Secret redaction/hygiene tests exist; 🔴 package-content audit still required before release.
21. ✅ Current workflow preserves unrelated source-of-truth changes when discipline is followed.
22. ✅ Current full suite: 379/379 passing at the 2026-08-22 review point.
23. 🔴 PyInstaller/installed binary must pass final clean-system smoke tests.
24. 🔴 Version must report `1.2.0` only after acceptance; current project version remains `0.9.9`.
25. 🔴 CHANGELOG and migration/security/plugin/MCP docs must be updated to the shipping behavior.
26. ✅ Reopened GUI chats restore semantic conversation context correctly.
27. ✅ Tool evidence persists metadata only, never raw outputs/arguments; 🟡 GUI E2E continuation test still required.
28. 🔴 Intermittent first-turn/manual-`retry` issue must be reproduced, understood, or explicitly bounded before release.
29. 🔴 Run a representative real-model matrix (at minimum Qwen + Nemotron + additional provider/model families) through read → tool → multi-step → finalization and pause/resume scenarios.
30. 🔴 Decide external executable plugin scope for 1.2: either finish trust/install lifecycle or explicitly ship manifest/discovery-only external plugins.

Release rule:
> AICoder 1.2 should ship a smaller set of capabilities that are demonstrably reliable and safe rather than claim every ambitious roadmap item as complete.

---

# 29. RESEARCH / DESIGN PATTERNS TO USE AS REFERENCE

Use current official documentation as reference, but do not copy implementations blindly.

Patterns worth emulating:

- **OpenAI Codex / Agents SDK**
  - native agent loop;
  - MCP;
  - skills/progressive disclosure;
  - AGENTS.md;
  - shell/apply-patch style tools;
  - allowed/relevant tool subsets;
  - explicit autonomy/approval boundaries;
  - lean prompts and eval-driven tool selection.

- **Claude Code**
  - specialized subagents with independent context;
  - per-agent tools and permissions;
  - worktree isolation;
  - scoped MCP servers;
  - hooks and plugin components;
  - clear plan/read-only modes.

- **Gemini CLI**
  - extension manifest bundling MCP, skills, subagents, hooks, policies;
  - on-demand Agent Skills;
  - isolated subagent tools/context;
  - plugin/extension settings patterns.

- **JetBrains Junie**
  - AGENTS.md/guidelines;
  - MCP configuration;
  - custom subagents;
  - native coding-agent architecture rather than CLI-wrapper layering.

- **Mistral**
  - agents/conversations;
  - local function tools;
  - MCP connectors;
  - handoff patterns.

- **Groq**
  - local tool calling;
  - remote MCP;
  - explicit `tool_choice`;
  - local orchestration as a valid security/control model.

- **Gemini API**
  - custom function/tool execution loop;
  - provider-returned call IDs;
  - built-in vs client-side tools;
  - current credential/auth changes must be handled by provider doctor rather than stale assumptions.

- **Ollama**
  - local single/parallel/multi-turn tool calling;
  - streaming tool calls;
  - keep local models first-class.

- **MCP specification**
  - stdio is a strong default for local MCP;
  - Streamable HTTP for remote/server integrations;
  - localhost binding and Origin validation for local HTTP;
  - secure OAuth-based authorization for remote protected resources;
  - no token passthrough;
  - cache tool list metadata when supported.

---

# 30. IMPORTANT DESIGN DECISIONS

Use these unless current code evidence shows a better approach.

### Decision A — Plugin first, not more executor hardcoding
Implement a plugin/ToolProvider layer before adding many more tool-specific branches.

### Decision B — Local OS provider is transport-independent
The OS logic should be callable natively by AICoder and exposable through MCP, rather than duplicated.

### Decision C — Security stays in the host
Plugin/MCP tools cannot decide their own final approval/elevation policy.

### Decision D — Progressive tools, not full-tool prompt
`on_demand` means relevant tools now + capability expansion later.

### Decision E — Settings are schema-driven
CLI, GUI, REPL and LLM use one registry/store.

### Decision F — Secrets are NOT settings
Authentication/session/provider credentials use a separate secure credential subsystem.

### Decision G — TriForce remains the default model gateway
Do not duplicate all provider logic in the desktop client unless there is a clear BYOK requirement.
Provider adapters should be extensible, but v1.2 must stay maintainable.

### Decision H — System optimization is evidence-based
No “magic optimizer” or cargo-cult sysctl tweaks.

### Decision I — Plan/apply/verify/rollback
Especially for OS changes and broad code changes.

---

# 31. INITIAL COMMANDS / INSPECTION CHECKLIST

At the beginning of the new session:

```bash
cd /home/zombie/ai-coder
git status --short
git diff -- aicoder/__init__.py aicoder/client.py packaging/aur/aicoder
find tests -maxdepth 2 -type f -print | sort
sed -n '1,220p' pyproject.toml
sed -n '1,220p' aicoder/session_state.py
sed -n '1,260p' aicoder/privileges.py
grep -RIn "tool_mode\|enabled_tools\|load_tools\|is_simple_chat_message" aicoder
grep -RIn "approval_mode\|sudo\|pkexec\|polkit" aicoder tests
grep -RIn "mcp\|plugin\|extension\|provider" aicoder
```

Then:
- create the v1.2 backup;
- run existing tests;
- report baseline failures before editing.

Do not use `head` on `/usr/bin/aicoder`; it is a PyInstaller ELF binary.
Use:
```bash
file /usr/bin/aicoder
/usr/bin/aicoder --help
```

Remember:
running Python from the repo tests source code, but `/usr/bin/aicoder` is a packaged snapshot. Validate both source and packaged binary deliberately.

---

# 32. EXPECTED WORK STYLE / OUTPUT TO USER

Do not just produce a theoretical design document.

Proceed phase by phase.

For each phase report:

```text
Cause / current limitation
Files inspected
Backup
Change made
Tests run
Observed result
Remaining risk
Rollback
Next phase
```

Before large architectural changes, explain the call-site impact briefly.

Do not ask the user to repeat information already established in this prompt.

Only ask for user interaction when genuinely required, especially:
- actual sudo/password authentication test;
- GUI Polkit dialog test;
- entering a new secret;
- destructive or irreversible action;
- product decision that cannot be derived from the current architecture.

---

# 33. END GOAL

When v1.2 is complete, a user should be able to do things like:

```text
aicoder agent "Check this URL and tell me whether the project can replace our current parser"
```

and AICoder should begin with web tools, then dynamically add code/repository capabilities if needed.

Or:

```text
aicoder agent "Configure yourself for autonomous coding, but ask before sudo or deleting files"
```

and AICoder should use typed settings tools to safely update itself.

Or:

```text
aicoder agent "This machine is mostly for Python, Docker and local AI. Diagnose it and optimize for stability."
```

and AICoder should:
- inspect the local OS through typed capabilities;
- create a plan;
- explain evidence;
- request approvals only where necessary;
- apply changes through the PrivilegeBroker;
- verify the results;
- record rollback information.

Or:

```text
aicoder plugin list
aicoder mcp list
aicoder settings list
```

with CLI and GUI showing the same truth.

That is the v1.2 product: **a customizable native AI coding and system agent, not a pile of wrappers.**


---

# 34. AUGUST 2026 AGENTIC CODING BEST-PRACTICE RESEARCH ADDENDUM

This section captures additional conclusions from a fresh review of current official documentation for OpenAI Codex, Claude Code, Gemini CLI, GitHub Copilot, JetBrains Junie, Cursor, and MCP-style agent extension patterns.

These findings refine the architecture above and should be treated as implementation guidance for v1.2.

## 34.1 Progressive disclosure is now a clear cross-agent pattern

Current agents increasingly distinguish between:

- always-present project instructions;
- on-demand skills;
- isolated subagents;
- external MCP tools;
- lifecycle hooks;
- installable extensions/plugins.

Do not load every instruction, skill body, or tool schema into the base context.

Implement:
- small stable core instructions;
- scoped/project instructions;
- skill descriptions available for discovery;
- full skill content loaded only when selected;
- subagents for high-volume exploration/log/search work;
- tool capability bundles chosen per task.

This validates the AICoder `CapabilityResolver` and progressive `on_demand` direction.

## 34.2 Stable prompt prefixes and prompt-cache friendliness are an architectural concern

Modern agent harnesses benefit significantly from stable prompt prefixes.

Important consequence for AICoder:

Dynamic tools must NOT produce a randomly ordered or constantly changing tool schema list.

Implement:
- deterministic tool ordering;
- deterministic capability-bundle ordering;
- stable core prompt sections;
- stable tool schema serialization;
- avoid adding/removing individual tools on every iteration;
- perform capability expansion at deliberate phase boundaries;
- cap expansion rounds;
- prefer selecting the appropriate initial capability bundle before the first expensive model call.

A good policy is:

```text
user turn starts
 -> classify intent
 -> choose stable capability bundle
 -> run agent
 -> if genuinely blocked, request one capability expansion
 -> continue with new stable bundle
```

Do not continuously mutate the tool list after every tool result.

Consider recording whether the active provider benefits from prompt caching.

For providers where changing tool definitions invalidates prompt caching, weigh:
- reduced tool-schema context from dynamic provisioning;
against
- cache loss caused by tool-list churn.

This tradeoff must be included in benchmarks.

## 34.3 Keep dynamic expansion, but make the meta-layer stable

Keep one or two stable meta-tools available when agent mode is enabled:

```text
capability_search
capability_request
```

They should be tiny and schema-stable.

They do not execute arbitrary actions.

Their role is to:
- discover capabilities;
- explain why a capability is needed;
- request expansion.

Expansion is performed by the host runtime, which re-applies:
- enabled-tool filters;
- plugin policy;
- security classification;
- capability budget.

The model cannot directly bypass those controls.

## 34.4 Subagents are primarily a context-management primitive, not only “multiple AIs”

Official agent designs increasingly use subagents to keep the main conversation clean.

Use a subagent when:
- many files need to be explored;
- logs/search results will be large;
- a specialized review is needed;
- a task can run independently;
- a cheaper/faster model is sufficient.

The parent receives a concise structured result rather than the entire transcript.

AICoder built-ins should include at least:

```text
Explore
Task
Research
Debugger
SecurityReviewer
SystemDiagnostician
OptimizerPlanner
```

Recommended distinctions:

### Explore
- read-only;
- file tree/read/grep;
- no writes;
- concise source references returned.

### Task
- tests/build/lint/commands;
- return short success summary;
- include full relevant output only on failure.

### Research
- web/search/crawl;
- no local writes by default.

### SecurityReviewer
- read-only;
- restricted network;
- security-focused skill preload.

### SystemDiagnostician
- local OS read-only diagnostics;
- no optimization changes.

### OptimizerPlanner
- inspect + plan only;
- no mutation;
- produces a structured plan for parent/user approval.

## 34.5 Worktree isolation should be first-class for code-changing parallel agents

Current coding agents increasingly use git worktrees for parallel agents.

For AICoder:
- read-only subagents do not need worktrees;
- code-changing parallel subagents should default to isolated worktrees where possible;
- the parent should merge/reconcile results explicitly;
- never allow two agents to mutate the same working tree concurrently without coordination.

Add an optional subagent field:

```yaml
isolation: worktree
```

and implement lifecycle cleanup.

If changes remain, do not delete the worktree until the user/parent has accepted or rejected them.

## 34.6 Plugin/extension bundles should be declarative and composable

Current agent ecosystems package combinations of:
- skills;
- subagents;
- commands;
- MCP servers;
- hooks;
- policies.

This reinforces the proposed AICoder plugin manifest.

A v1.2 plugin should be able to contribute declaratively without monkey-patching core modules.

Plugin components should be namespaced.

Example:

```text
local-os:diagnose
local-os:optimize
wordpress:deploy
release:prepare
```

## 34.7 Extension environment variables must be explicitly allowlisted

A strong current security pattern is to NOT pass the user's full environment to plugins/MCP subprocesses.

Implement for AICoder plugins and stdio MCP servers:

- sanitize inherited environment;
- pass only safe baseline variables such as PATH/HOME/temp vars as needed;
- plugin manifest must declare additional required environment variable NAMES;
- secret values come from the credential broker/secure environment injection;
- never expose secret values to the LLM;
- never give a plugin all provider keys because it requested one credential.

Example manifest direction:

```toml
[[settings]]
name = "api_key"
secret = true
env_var = "EXAMPLE_API_KEY"
required = true
```

The plugin receives only that secret if the user authorized/configured it.

This is especially important for the Local OS MCP and third-party MCP servers.

## 34.8 Rules/instructions should be focused, composable, and scoped

Do not create a massive AGENTS.md containing every possible workflow.

Support layered instruction discovery:

```text
user/global instructions
 -> workspace root AGENTS.md
 -> nested/path-scoped instructions
 -> relevant rules
 -> relevant skill
```

Nearest/path-specific guidance should override broader project guidance where the formats allow.

Keep reusable rules:
- focused;
- actionable;
- concrete;
- small enough to understand;
- scoped to relevant paths/tasks.

AICoder should report which instruction sources were loaded when verbose/debug mode is enabled.

## 34.9 Hooks are best used for deterministic lifecycle policy and verification

Add a rich but controlled lifecycle.

Recommended events:

```text
SessionStart
UserPromptSubmit
BeforeCapabilityResolve
AfterCapabilityResolve
PreToolUse
PermissionRequest
PostToolUse
PostToolUseFailure
SubagentStart
SubagentStop
BeforeSettingsChange
AfterSettingsChange
BeforeFileWrite
AfterFileWrite
BeforeCompact
AfterCompact
Stop
SessionEnd
```

Security-critical rules should use deterministic hooks/policies rather than trusting another LLM call by default.

Examples:
- PreToolUse: reject forbidden paths;
- BeforeFileWrite: create backup/check git status;
- PostToolUse: validate mutation result;
- PostToolUseFailure: classify failure and prevent identical retries;
- BeforeSettingsChange: enforce security downgrade confirmation;
- Stop: verify requested task actually completed.

LLM/prompt hooks may be optional for semantic checks, but not the only security boundary.

## 34.10 Sandboxing and approval policy must be separate concepts

Do not use approval prompts as the only protection.

AICoder should ultimately distinguish:

```text
sandbox boundary = what the process CAN technically access
approval policy  = when the user must authorize crossing/using capabilities
```

For v1.2, even if a complete OS sandbox cannot be delivered cross-platform, structure the architecture so these are separate abstractions.

Possible future sandbox profiles:

```text
read_only
workspace_write
system_read
system_admin
custom
```

Network policy should also be explicit:
- no network;
- web/search only;
- known domains;
- unrestricted after authorization.

The Local OS provider should not imply unrestricted filesystem/network access.

## 34.11 Agent-native telemetry is part of the security model

Audit logs should preserve not only “what command ran” but:
- user intent/task;
- agent reason;
- tool selected;
- capability source/plugin/MCP;
- approval decision;
- execution result;
- verification result.

This is useful for:
- security auditing;
- debugging poor agent decisions;
- improving tool routing;
- self-optimization.

Support optional OpenTelemetry export later, but keep a simple local structured journal first.

## 34.12 Context compaction must be designed into the agent loop

Long-running agents need explicit context management.

Add:
- token/context usage estimates when provider metadata allows;
- compaction threshold;
- structured persistent task state outside the chat transcript;
- plan state;
- key decisions;
- changed files;
- unresolved blockers;
- important tool results.

When compacting:
- preserve user constraints;
- preserve security/approval state;
- preserve current plan;
- preserve changed-file/backup/rollback information;
- drop redundant logs and raw exploration.

The compacted representation should be testable.

## 34.13 Do not make main context the database

Durable state should live in structured host-side stores:

```text
SettingsStore
TaskState
ChangeJournal
Memory
PluginRegistry
ToolCatalog
CapabilityState
```

The model context is a working view, not the source of truth.

This avoids losing critical state during compaction/model changes.

## 34.14 Plan mode and execution mode should be distinguishable

Modern coding agents increasingly expose read-only/plan vs execution modes.

AICoder should support at least:

```text
plan
agent
```

Optional future modes:

```text
read_only
agent
autopilot
```

Plan mode:
- may inspect/read/search;
- may run safe diagnostics;
- cannot mutate;
- produces an implementation/system-change plan.

This fits system optimization particularly well:

```text
aicoder agent --mode plan "Optimize this machine for local AI"
```

Then:

```text
aicoder changes/apply PLAN_ID
```

## 34.15 MCP management needs installation verification, not only config editing

A modern MCP UX should include:

```text
discover
configure
start
verify
doctor
enable/disable
```

When adding an MCP server:
- validate its manifest/config;
- request only required env var names/secrets;
- start it;
- verify initialization;
- obtain tools/list;
- classify tools;
- show status;
- store the config only after successful validation or clearly mark it broken.

Do not merely write JSON and assume success.

## 34.16 Cross-agent interoperability is becoming valuable

Where practical, support open/shared conventions rather than AICoder-only formats:

- `AGENTS.md`
- `.agents/skills/`
- `SKILL.md`
- Markdown + YAML frontmatter agent definitions
- MCP
- stdio MCP
- Streamable HTTP MCP

AICoder-specific metadata can extend these standards without breaking basic compatibility.

This makes skills and agents portable among AICoder, Codex, Copilot, Gemini CLI, Junie, Claude Code, and other compatible runtimes.

## 34.17 Self-configuration is good; self-security-downgrade is not

LLM-controlled settings should follow a two-tier model.

### Normal behavioral settings
Can be changed on explicit user request, e.g.:
- timeout;
- tool budget;
- model;
- fallback;
- planning style;
- enabled safe plugins.

### Security-boundary settings
Always require explicit confirmation, e.g.:
- approval mode reduction;
- sandbox widening;
- network widening;
- trusted plugin list;
- arbitrary shell enablement;
- sudo/root automation;
- destructive operations;
- credential-source changes.

Never allow:
> “I need more permissions, so I changed myself to unrestricted.”

The host policy engine decides whether a settings patch requires authorization.

## 34.18 Measure the tool-routing tradeoff, don't guess

For the new `on_demand`, benchmark at least:

```text
A: full tool catalog every turn
B: one fixed minimal catalog
C: intent-selected bundle
D: intent-selected bundle + one expansion
```

Measure:
- first-token latency;
- total latency;
- input tokens;
- prompt cache hit/miss if provider reports it;
- tool selection accuracy;
- task success;
- number of model turns;
- number of tool schemas;
- expansion frequency.

It is possible for an overly dynamic tool system to save context but lose enough cache efficiency that it becomes slower.

Choose defaults from measured behavior.

## 34.19 The target v1.2 philosophy after this research

AICoder v1.2 should follow these principles:

1. **Small stable core.**
2. **Progressive disclosure of tools and instructions.**
3. **Intent-selected capabilities.**
4. **Isolated subagents for noisy/specialized work.**
5. **Worktree isolation for concurrent mutations.**
6. **Plugins bundle capabilities declaratively.**
7. **Plugins/MCP inherit a sanitized environment.**
8. **Settings/credentials/security are separate subsystems.**
9. **Sandbox boundaries and approvals are separate.**
10. **Host-side policy beats model self-policing.**
11. **Plan → act → verify → rollback.**
12. **Durable state lives outside the prompt.**
13. **Context is compacted deliberately.**
14. **Tool lists are stable and deterministic for cache efficiency.**
15. **Everything important is observable and auditable.**
16. **Open conventions are preferred where practical.**
17. **Benchmark agent architecture decisions instead of assuming them.**

These principles should guide implementation decisions when the earlier sections leave room for interpretation.
