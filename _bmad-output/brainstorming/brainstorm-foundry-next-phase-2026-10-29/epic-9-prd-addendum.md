# PRD Addendum — Epic 9: Multi-tool / AgentRunner Compatibility

**Project:** Foundry
**Epic:** 9 — Multi-tool / AgentRunner Compatibility
**Status:** Draft
**Date:** 2026-10-29
**Author:** Wajdi Addin (wajdi404@gmail.com)

---

## 1. Problem Statement

Foundry's AgentRunner abstraction already exists as a seam in the codebase. Claude Code is currently the only implementation behind it. This is a hidden single point of failure: anyone running Foundry on a machine without Claude Code installed, or mid-flow in a different agent tool (Aider, OpenAI Codex CLI, a custom LLM wrapper), hits a wall that has nothing to do with their spec or their project.

The failure mode isn't an error in the abstraction — the abstraction is correct. The failure mode is that the seam has only one plug. When the plug is missing, the user gets either a cryptic crash or silence, and the momentum that Foundry is supposed to provide evaporates.

The reframe from the design session is precise: Epic 9 is not about "compatibility." It is about not becoming the gap. The spec is the control surface — it is not interchangeable. The agent tool is interchangeable. Foundry must behave as though that is true.

---

## 2. Goals

1. Foundry runs end-to-end with at least two agent tools beyond Claude Code, so no single tool installation is a hard dependency.
2. When the configured runner is not found, Foundry surfaces a clear, actionable error — not a traceback — so the user recovers in under a minute.
3. Adding a new AgentRunner is documented well enough that a contributor who has never read the internals can do it without asking.
4. The escape hatch is universal: any tool, internal wrapper, or vim macro can be wired in via a shell script convention, making the abstraction a Unix pipe rather than a class hierarchy.

---

## 3. Functional Requirements

### Must

**FR-9.1 — Second AgentRunner implementation**
Foundry ships a concrete AgentRunner implementation for at least one additional agent tool. Priority candidates, in order: OpenAI Codex CLI, Aider. The implementation must pass the same integration test harness used for ClaudeCodeRunner.

**FR-9.2 — Third AgentRunner implementation**
Foundry ships a second additional implementation for a distinct agent tool from FR-9.1. Together, FR-9.1 and FR-9.2 ensure Claude Code is never the sole option and the pattern is proven across tool families (CLI-wrapped LLM, autonomous agent, etc.).

**FR-9.3 — Runner-not-found error message**
When the configured AgentRunner binary or entry point is not found on PATH or at the configured path, Foundry exits with a human-readable error that names the missing tool, explains which config key controls runner selection, and shows the exact install command (or a link to it) for the missing tool. No raw Python traceback reaches the user for this case.

**FR-9.4 — AgentRunner contributor documentation**
A single document (or clearly bounded section in the existing developer guide) explains: the AgentRunner interface contract, how to register a new implementation, how to run the integration tests for it, and the review checklist. A contributor must be able to ship a new runner without reading source beyond what the doc references.

### Should

**FR-9.5 — Universal shell-script runner (`foundry-agent.sh`)**
Foundry recognizes a `foundry-agent.sh` file at the repo root (or at a path set in project-local config) as a valid AgentRunner. When present, Foundry invokes it with the story file path as its first argument and waits for exit code 0 (success) or non-zero (failure). The script is a black box: it may call any tool, LLM, or local automation. This makes the AgentRunner abstraction a Unix pipe — any agent that can be scripted can be wired in without writing Python.

**FR-9.6 — Runner selection via config**
The active AgentRunner is selectable via a documented config key (e.g. `agent.runner` in `foundry.toml` or equivalent). The default remains Claude Code for backward compatibility. The runner name maps to the registered implementation or, if set to `shell`, to the `foundry-agent.sh` convention in FR-9.5.

**FR-9.7 — Runner capability advertisement**
Each AgentRunner implementation declares a minimal capability set (e.g. supports streaming output, supports interactive checkpoints, supports dry-run). Foundry uses this to emit a one-line warning — not an error — when a selected runner lacks a capability that the current pipeline phase would use, so the user can make an informed choice rather than discover the gap mid-run.

---

## 4. Non-Goals

**Self-correcting agent loop.** The idea of combining AgentRunner execution with the test gate — running tests mid-implementation, having the agent self-correct on failure, and cycling until green — is explicitly deferred. It is architecturally adjacent to this epic but requires a separate control-flow design (retry budget, loop termination, partial-state handling) that would expand scope significantly. It belongs in a later epic once multiple runner implementations have stabilized.

**Agent output normalization.** Foundry does not attempt to parse, normalize, or reinterpret agent output across runner implementations. Each runner is responsible for mapping its tool's output to the AgentRunner contract. Foundry treats runner output as opaque beyond exit code and any structured fields the contract defines.

**Runner auto-detection.** Foundry does not scan PATH to discover available runners and auto-select one. The configured runner is always explicit. Discovery logic creates surprising behavior when multiple tools are installed; the error message in FR-9.3 is the right response to a missing runner, not silent fallback.

**Plugin marketplace or runner registry.** Third-party runner distribution, versioning, or a central registry are out of scope. The contributor documentation in FR-9.4 covers adding runners to the Foundry codebase; external distribution is a separate concern.

---

## 5. Open Questions

**OQ-9.1 — Which tool ships as FR-9.1 vs FR-9.2?**
OpenAI Codex CLI and Aider are the priority candidates. The choice depends on which has a stable enough CLI contract to write a reliable integration test against. If Codex CLI's interface is still in flux, Aider may be the safer FR-9.1. Needs a quick spike (30 min) against each tool's current release before the epic is scheduled.

**OQ-9.2 — Where does `foundry-agent.sh` live when Foundry is used outside a git repo?**
The convention in FR-9.5 assumes a repo root. For users running Foundry against a directory that is not yet a git repo (common at project inception), the fallback lookup path is undefined. Options: (a) skip shell runner if no repo root found and emit a clear message, (b) also check `$HOME/.config/foundry/foundry-agent.sh` as a user-level default. Decision needed before FR-9.5 is implemented.

**OQ-9.3 — AgentRunner interface contract: what is the minimum?**
The existing seam defines the interface. Before FR-9.1 is implemented, the contract should be reviewed and locked: what arguments does a runner receive (story path only, or also context like project root and phase name), what exit codes are meaningful, and what structured output (if any) does the runner return to Foundry. Locking this before writing the second implementation prevents the interface from drifting to fit only one tool's shape.

**OQ-9.4 — Integration test harness: real tool or mock?**
Running the full integration test suite against real Claude Code, Codex CLI, and Aider adds external dependencies and API costs to CI. Options: (a) mock runner harness for unit tests, real-tool smoke test gated behind an env flag, (b) record-and-replay, (c) require contributors to certify manually. The existing Claude Code test pattern should be inspected to see what it already does before deciding the standard for new runners.

---

*This addendum covers Epic 9 scope only. Epic 8 (spec interpretation readback and CLI phase signal) is covered in a separate addendum.*
