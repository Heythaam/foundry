# PRD Addendum — Epic 8: CLI UX / Visuals

**Project:** Foundry
**Branch:** epic-8-cli-ux (planned)
**Author:** Heythaam
**Date:** 2026-10-29
**Source:** Brainstorming session — `brainstorm-foundry-next-phase-2026-10-29`

---

## 1. Problem Statement

Foundry users currently feel like passengers, not authors. They tab away from the terminal and check back every 30 seconds listening for the sound of breaking. When something fails, the error vocabulary is vague and spatial — "stopped," "got stuck," "lost track" — describing the absence of an outcome rather than its mechanism. When something succeeds, they skim the diff for obvious wrongs and open the PR without ever feeling the project was theirs.

The root cause is a transparency gap at the wrong moment. Foundry surfaces checkpoints mid-run, after the machine has already committed to an interpretation. By then, the user is approving output, not intent — rubber-stamping a direction they never consciously set. Accountability never fully transfers; it only distributes labor. If Foundry fails, it is still on them.

The opportunity: move transparency upstream to the spec interpretation moment, before any code runs. Let users see how Foundry parsed their intent, approve that interpretation, and then let the run proceed uninterrupted. The PR that results feels authored, not generated.

**Design north star:** authorship, not relief. The spec is the control surface.

---

## 2. Goals

1. Users approve Foundry's interpretation of their spec before the run starts, not after code has been written.
2. When Foundry fails, the user knows exactly where it stopped and why — not that it stopped.
3. Between phases, silence is replaced with signal: users know what just happened and what happens next before they decide to proceed.

---

## 3. Functional Requirements

### Must

**FR-8-01 — Spec interpretation readback**
Before any phase begins executing, Foundry displays a structured summary of how it parsed the spec: inferred project type, detected constraints, planned phase sequence, and any assumptions it made to fill gaps. The user must explicitly approve this interpretation (single keypress or `--yes` flag for non-interactive mode) before the run proceeds. This is not a mid-run checkpoint — it runs once, before the first phase, and the run is uninterrupted thereafter.

*Acceptance criteria:*
- Readback output includes: inferred project name, language/framework, test runner, number of planned epics/stories, and any spec ambiguities Foundry resolved by assumption.
- Ambiguities resolved by assumption are visually distinguished from facts parsed directly from the spec (e.g., a prefix marker or indentation).
- User can abort at this point and edit the spec before retrying; the run does not begin until explicit approval is given.
- In `--yes` / non-interactive mode, the readback is printed to stdout and the run proceeds automatically; no approval gate.

---

**FR-8-02 — Named failure states**
When Foundry stops due to an error, the failure output must name: (a) which phase failed, (b) which story or step within that phase, (c) the specific error condition. Generic messages like "Foundry stopped" or "agent did not complete" are not acceptable exit states.

*Acceptance criteria:*
- Exit message format: `[phase-name / story-title] failed: <reason>` with enough detail to locate the failure without re-reading logs.
- If the failure is recoverable (e.g., spec ambiguity, missing env var), the message includes a remediation hint pointing to the relevant spec section or config key.
- If the failure is an agent error, the message distinguishes between agent timeout, non-zero exit, and output validation failure.
- Failure output is written to stderr; the final line on stderr is always a named failure state or a success confirmation — never a stack trace alone.

---

### Should

**FR-8-03 — Phase signal**
At the boundary between phases, Foundry prints a brief summary of what just completed and what happens next, then waits for a single keypress before proceeding. This fills the silence between phases with signal and makes the next step feel inevitable rather than opaque.

*Acceptance criteria:*
- Summary covers: phase just completed (name + outcome count, e.g., "Architecture — 1 doc written"), phase about to begin (name + what it will produce, e.g., "Implementation — 4 stories, estimated 12 agent calls").
- Wait is a single keypress (any key, or Enter); `--yes` / `--non-interactive` skips the wait.
- If the phase boundary occurs inside a non-interactive CI run, no wait is inserted and the summary is printed to stdout.
- The signal is printed even when the next phase is the last one; the boundary before completion is as important as any other.

---

## 4. Non-Goals

- **Web dashboard.** No browser UI, no auto-open on `foundry pro`, no live streaming pane. The terminal is the interface.
- **Mid-run checkpoints.** The spec interpretation readback (FR-8-01) is the one human gate. Once the run begins, it runs to completion or named failure. No "approve this story before continuing" prompts during execution.
- **Decorative progress indicators.** Spinners and progress bars that convey no information about what Foundry is doing are out of scope. Any progress output must carry signal (phase name, story title, step count) — not motion.
- **Inline diff review at checkpoint.** The approval moment is for interpretation of intent, not review of generated output. Diff review belongs in the PR workflow, not inside Foundry's run loop.

---

## 5. Open Questions

1. **Readback format.** Should the spec interpretation readback be structured (key-value, YAML-ish) or prose? Structured is more scannable; prose may feel more natural for complex specs with many inferred assumptions. Does the format need to be stable enough for scripts to parse it?

2. **Assumption confidence.** When Foundry resolves a spec ambiguity by assumption, should it surface a confidence signal (e.g., "assuming pytest because no test runner specified" vs. "inferred Flask from import pattern in spec snippet")? Or does any assumption, regardless of basis, warrant the same visual treatment?

3. **Named failure state granularity.** FR-8-02 requires naming the failing story and phase. For deeply nested failures (e.g., a sub-step inside a story's implementation loop), how deep does the path go before it becomes noise? Define the maximum path depth for failure messages.

4. **Phase signal content in quick mode.** Epic 7 introduced quick mode, which collapses phases. Does the phase signal still fire at quick-mode phase boundaries, or only in full-mode runs? If quick mode has fewer boundaries, the signal may appear only once — is that still worth implementing?

5. **`--yes` completeness.** FR-8-01 and FR-8-03 both define `--yes` / `--non-interactive` bypass behavior. Is there a single flag that governs both, or separate flags? Ensure the flag name is consistent with Epic 7's non-interactive mode design.
