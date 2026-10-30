---
id: SPEC-epic-9-multiagent
companions: []
sources:
  - ../../brainstorming/brainstorm-foundry-next-phase-2026-10-29/epic-9-prd-addendum.md
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# Epic 9: Multi-tool / AgentRunner Compatibility

## Why

The AgentRunner abstraction seam already exists in `foundry/agent.py`. Claude Code is the only implementation behind it. The failure mode is not the abstraction — it is that the seam has only one plug. When a user is mid-flow in Aider, on a machine without Claude Code installed, or using a company-internal LLM wrapper, Foundry becomes the gap it was built to close. This epic is not about compatibility in the abstract; it is about not becoming the thing that breaks momentum when someone's environment differs from the assumed default. The spec is the control surface — it is not interchangeable. The agent tool is interchangeable. This epic makes Foundry behave as though that is true.

## Capabilities

- **CAP-1**
  - **intent:** Foundry runs end-to-end with at least two agent tools beyond Claude Code.
  - **success:** A full `foundry pro` run completes successfully using `AiderRunner`; a full run also completes using a second distinct runner (priority: OpenAI Codex CLI). Both pass the same integration test harness used for `ClaudeCodeRunner`.

- **CAP-2**
  - **intent:** When the configured runner binary or entry point is not found, Foundry exits with a human-readable, actionable error — not a Python traceback.
  - **success:** When the runner binary is missing from PATH or the configured path, stderr names: the missing tool, the config key that controls runner selection, and the exact install command (or a documentation link). No raw Python traceback reaches the user for this case. User can recover in under a minute.

- **CAP-3**
  - **intent:** A shell script at a well-known path (`foundry-agent.sh`) acts as a valid AgentRunner, making any scriptable tool wire-compatible without writing Python.
  - **success:** When `agent.runner = "shell"` is configured and `foundry-agent.sh` exists at the repo root (or configured path), Foundry invokes it with the story file path as `$1` and treats exit 0 as success, non-zero as failure. Any tool, LLM wrapper, or local automation that can be expressed as a shell script becomes a valid runner.

- **CAP-4**
  - **intent:** A contributor with no prior Foundry knowledge can add a new AgentRunner implementation by following a single document.
  - **success:** The document covers: the AgentRunner interface contract, how to register a new implementation, how to run the integration tests for it, and the review checklist. A contributor ships a working runner without reading source beyond what the document references.

## Constraints

- The `AgentRunner` protocol in `foundry/agent.py` must not change. New runners implement the existing interface. This epic adds implementations; it does not redesign the seam.
- Runner selection is always explicit. Foundry never scans PATH to discover or auto-select a runner.
- The default runner remains `ClaudeCodeRunner` for full backward compatibility.
- New runner implementations must pass the same integration test harness as `ClaudeCodeRunner` before merging.
- The `foundry.lock` runner field records the runner name used for the run; this is a new field, not a change to existing sections.

## Non-goals

- Self-correcting agent loop (combining AgentRunner execution with the test gate for mid-implementation self-correction). Deferred to a later epic.
- Agent output normalization across runner implementations. Each runner maps its tool's output to the AgentRunner contract; Foundry treats runner output as opaque beyond exit code.
- Runner auto-detection from PATH.
- Plugin marketplace, versioning, or central registry for third-party runners.

## Success signal

`foundry pro` completes a run using `AiderRunner` without touching `ClaudeCodeRunner`. A contributor reads the AgentRunner doc and ships a working third runner without asking anyone for help. When Claude Code is not installed and `--runner claude-code` is configured, the error message tells the user exactly what to install and which config key to change.

## Assumptions

- Aider's CLI interface is stable enough to write a reliable integration test against. If the spike (OQ-9.1) shows otherwise, Aider and Codex CLI swap priority order.
- `foundry.toml` or an equivalent project-local config file is available as the config surface for `agent.runner`. If no such file exists yet, it will be introduced by this epic as a minimal addition.

## Open Questions

- **OQ-9.1** Which tool ships as CAP-1's first runner? Aider and OpenAI Codex CLI are priority candidates. A 30-minute spike against each tool's current release should determine which has a stable enough CLI contract to write a reliable integration test. Resolve before the epic is scheduled.
- **OQ-9.2** Where does `foundry-agent.sh` live when Foundry is used outside a git repo? Options: (a) skip shell runner if no repo root is found and emit a clear message; (b) also check `$HOME/.config/foundry/foundry-agent.sh` as a user-level fallback. Needs a decision before CAP-3 is implemented.
- **OQ-9.3** AgentRunner interface contract lock: before the second implementation is written, the contract should be reviewed and frozen — what arguments does a runner receive (story path only, or also project root and phase name?), what exit codes are meaningful, and what structured output (if any) does the runner return? Locking this prevents the interface from drifting to fit only one tool's shape.
- **OQ-9.4** Integration test harness strategy: running the full harness against real tools adds external dependencies and API costs to CI. Options: (a) mock harness for unit tests, real-tool smoke test gated behind an env flag; (b) record-and-replay; (c) manual contributor certification. Inspect the existing Claude Code test pattern before deciding the standard.
