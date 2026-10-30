# Epic 8 Context: CLI UX / Visuals

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Make Foundry feel like a tool that gives the developer authorship over their project, not just relief that the tool didn't crash. The core mechanism: move transparency upstream to the spec interpretation moment (before any code runs), replace vague spatial failure vocabulary with named, spatial error states, and fill every phase boundary with signal so the next step feels inevitable rather than opaque.

## Stories

- Story 8.1: Spec Interpretation Readback
- Story 8.2: Named Failure States
- Story 8.3: Phase Signal

## Requirements & Constraints

- **Readback before run:** Foundry must display a structured interpretation of the spec/idea before phase 1 executes — inferred project name, language/framework, test runner, story count, and any gap-filling assumptions. Assumptions are visually distinguished from directly-parsed facts (e.g. `~` prefix). User approves with a single keypress or aborts with Ctrl-C.
- **`--yes` bypass:** All new human gates (readback approval, phase signal keypress) must be bypassed when `--yes` / non-interactive mode is active. Readback and signal output are still printed to stdout in that mode.
- **Named failure format:** The final line on stderr is always `[<phase> / <story>] failed: <reason>` or a success confirmation. Never a bare stack trace. Agent timeout, non-zero exit, and empty-output validation are distinct conditions.
- **Phase signal:** At every phase boundary — including before the final phase — print what just completed (name + outcome count) and what comes next (name + what it will produce), then wait for a single keypress.
- **No new mid-run checkpoints:** Once the run starts (after readback approval), it runs to completion or a named failure. This epic does not add implementation-phase gates.
- **No decoration:** No spinners, animated bars, or output that carries no named signal. Text only.
- **Quick mode compatibility:** Phase signal and readback apply to quick mode (`RunContext.mode == "quick"`) with identical behavior.

## Technical Decisions

- **New modules:** `foundry/readback.py` (SpecReadback), `foundry/signal.py` (PhaseSignal). Do not put CLI-presentation logic in existing modules.
- **FoundryError extension:** Add optional `phase: str | None` and `story: str | None` fields to `FoundryError` in `foundry/context.py`. Add a `format_failure_line(error: FoundryError) -> str` helper. These are additive — no existing fields change.
- **Orchestrator integration:** Readback is invoked after `RunContext` is built, before phase 1. PhaseSignal is invoked at every phase boundary by the orchestrator. The orchestrator's top-level exception handler formats and writes the final stderr line.
- **TTY detection:** `sys.stdout.isatty()` gates the keypress wait in PhaseSignal. In non-TTY environments (CI), print the summary and continue.
- **AgentRunner protocol untouched:** `foundry/agent.py` is not modified by this epic.
- **Failure classification in `ClaudeCodeRunner`:** Distinguish timeout (`InfraFailure`), non-zero exit (`CodeFailure`/`InfraFailure` depending on `_INFRA_PATTERN`), and empty output (`CodeFailure`) in the error messages so they surface distinctly through the named failure format.

## Cross-Story Dependencies

- Story 8.2 (FoundryError extension) should land before 8.3 (PhaseSignal) since the orchestrator exception handler change in 8.2 is foundational to how failures surface during phase execution. Story 8.1 (Readback) is independent of 8.2 and 8.3 and can be implemented first.
- All three stories share the `--yes` flag — verify the flag name with `foundry/cli.py` before implementing any story to avoid introducing a new flag that conflicts with Epic 7.
