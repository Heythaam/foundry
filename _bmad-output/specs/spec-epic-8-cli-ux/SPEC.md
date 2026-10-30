---
id: SPEC-epic-8-cli-ux
companions: []
sources:
  - ../../brainstorming/brainstorm-foundry-next-phase-2026-10-29/epic-8-prd-addendum.md
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# Epic 8: CLI UX / Visuals

## Why

Foundry users feel like passengers: they tab away from the terminal and monitor for the sound of breaking. When a run fails, the error vocabulary is vague and spatial — "stopped," "got stuck" — describing an absent outcome, not a mechanism. When a run succeeds, they skim the diff and open the PR without ever feeling the project was theirs. The root cause is a transparency gap at the wrong moment: mid-run checkpoints surface after the machine has already committed to an interpretation of the spec, so the user is approving output rather than intent. This epic moves transparency upstream to the spec interpretation moment — before any code runs — and replaces vague failure exits with named, spatial error states. The target emotional state is authorship, not relief.

## Capabilities

- **CAP-1**
  - **intent:** User reviews Foundry's interpretation of their spec and explicitly approves it before any phase executes.
  - **success:** A structured readback appears on stdout before phase 1 begins; it includes inferred project name, language/framework, test runner, planned epic/story count, and any assumptions made to fill spec gaps, with assumptions visually distinguished from directly-parsed facts. User can abort and edit the spec before approving. `--yes` bypasses the approval gate but still prints the readback. No code runs until approval is given.

- **CAP-2**
  - **intent:** When Foundry stops due to an error, the failure output names the phase, the story or step, and the specific error condition.
  - **success:** The final line on stderr always matches `[<phase> / <story>] failed: <reason>` or is a success confirmation — never a bare stack trace. Agent timeout, non-zero exit, and output validation failure are reported as distinct conditions. Recoverable failures include a remediation hint naming the relevant spec section or config key.

- **CAP-3**
  - **intent:** At every phase boundary Foundry prints a brief summary of what just completed and what happens next, then waits for a single keypress before proceeding.
  - **success:** Every phase boundary — including the boundary before the final phase — shows: phase just completed (name + outcome count) and phase about to begin (name + what it will produce). `--yes` / non-interactive / CI skips the keypress wait; the summary is still printed to stdout.

## Constraints

- `--yes` / non-interactive mode bypasses both the readback approval gate (CAP-1) and the phase-signal keypress (CAP-3) without human intervention; readback is still printed to stdout so CI logs carry the interpretation.
- Once the run begins (after readback approval), it runs to completion or a named failure. No mid-run checkpoints are introduced by this epic.
- Quick mode (Epic 7) must not break. Phase signal fires at quick-mode phase boundaries; `--yes` behavior is identical.
- The readback parses the spec/idea string and emits to stdout. It does not modify `RunContext`, `foundry.lock`, or any mutable state.
- Failure output goes to stderr; the final line on stderr is always a named failure state or success confirmation.

## Non-goals

- Web dashboard, browser UI, or auto-open on `foundry pro`.
- Mid-run checkpoints after the run begins.
- Decorative progress indicators (spinners, progress bars that carry no named signal).
- Inline diff review at the readback gate. Diff review belongs in the PR workflow.

## Success signal

A user who ran `foundry pro` and hit a failure can name exactly which phase and story failed without opening any log file. A user who ran a successful `foundry pro` saw Foundry's interpretation of their spec — and approved it — before a single line of code was written.

## Open Questions

- **OQ-8.1** Readback format: structured key-value (YAML-ish) or prose? Structured is more scannable; prose handles complex multi-assumption specs more naturally. Does the format need to be stable enough for scripts to parse?
- **OQ-8.2** Assumption confidence: should readback distinguish high-confidence inferences ("inferred Flask from import pattern") from low-confidence assumptions ("no test runner specified, assuming pytest")? Or does any assumption get the same visual treatment?
- **OQ-8.3** Named failure state depth: for deeply-nested failures (a sub-step inside a story's implementation loop), how deep does the path go? Define the maximum path depth before it becomes noise.
- **OQ-8.4** Phase signal in quick mode: Epic 7 collapses phases. Does the signal fire at every quick-mode boundary, or only when more than one boundary exists? If only one boundary exists, is the signal still worth implementing for that case?
- **OQ-8.5** `--yes` flag: FR-8-01 and FR-8-03 both define bypass behavior. Is there a single flag governing both, or separate flags? Verify the flag name is consistent with Epic 7's non-interactive design.
