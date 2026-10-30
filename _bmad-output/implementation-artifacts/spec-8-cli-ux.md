---
title: 'Epic 8: CLI UX / Visuals'
type: 'feature'
created: '2026-10-30'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '4d090a291b3d22e6a48564715893bb885b57c0e2'
context:
  - _bmad-output/implementation-artifacts/epic-8-context.md
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Foundry users feel like passengers — they rubber-stamp mid-run checkpoints after the machine has already committed to an interpretation, vague failure output tells them the run "stopped" without saying where or why, and silence between phases gives no signal about what happens next.

**Approach:** Three additive changes: (1) a spec interpretation readback before phase 1 so users approve Foundry's intent-parse upfront; (2) named, spatial failure output so every error exit names the phase, story, and condition; (3) a phase-boundary signal that fills every pause with what completed and what comes next.

## Boundaries & Constraints

**Always:**
- `--yes` flag bypasses all new keypresses but never suppresses printed output (readback and signal still print to stdout)
- All three features apply equally to quick mode (`RunContext.mode == "quick"`)
- Final line on stderr is always `[<phase> / <story>] failed: <reason>` or a success confirmation — never a bare traceback
- `foundry/agent.py` is not modified
- All new modules in `foundry/`; tests for every new module and every modified behavior

**Never:**
- Mid-run checkpoints after readback approval (run goes to completion or named failure)
- Decorative output (spinners, progress bars without named signal)
- Web dashboard or browser UI

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Readback, interactive | `foundry pro "idea"`, TTY, no `--yes` | Structured readback printed, keypress prompt | Ctrl-C → clean exit, no phase executed, no lock mutation |
| Readback, `--yes` | `--yes` flag set | Readback printed to stdout, run proceeds without waiting | N/A |
| Named failure, agent timeout | `InfraFailure` from `asyncio.timeout`, phase/story set | `[brainstorm / -] failed: agent 'brainstorm' timed out after 1800s` | Last stderr line matches format |
| Named failure, non-zero exit | Exit code ≠ 0, `_INFRA_PATTERN` not matched | `[planning / story 1.2] failed: agent exited with status 1` | Distinct from timeout and empty-output |
| Named failure, empty output | `CodeFailure("agent returned no output")` | `[planning / story 1.2] failed: agent returned no output` | Distinct message |
| Phase signal, TTY | Between phases, interactive | Summary of completed + upcoming phase printed, keypress wait | N/A |
| Phase signal, no TTY / CI | `sys.stdout.isatty()` false OR `--yes` | Summary printed, no wait | N/A |
| Recoverable failure | `EnvFailure` with `fix` set | Format line + `To fix: <hint>` | Hint must reference config key or spec section |

</frozen-after-approval>

## Code Map

- `foundry/context.py:45-57` — `FoundryError` dataclass; add `phase: str | None` and `story: str | None` fields; add `format_failure_line(err) -> str` helper here
- `foundry/cli.py:89-135` — `pro` and `quick` commands; add `--yes` option; thread into `RunContext`; update `_fail_with` to call `format_failure_line`
- `foundry/context.py:168-185` — `RunContext` dataclass; add `yes: bool = False` field
- `foundry/orchestrator.py:67-80` — `Orchestrator.run()`; inject readback call after `record_run_start`, before phase loop; inject phase signal between phases
- `foundry/readback.py` — **new**: `SpecReadback` class; parses `ctx.idea` for project name, likely language/framework, test runner signal, assumptions; prints structured readback; waits for keypress unless `ctx.yes`
- `foundry/signal.py` — **new**: `PhaseSignal` class; prints completed+upcoming summary; waits for keypress unless `ctx.yes` or no TTY
- `tests/test_readback.py` — **new**: readback content, assumption distinction, `--yes` bypass, abort path, no lock mutation
- `tests/test_signal.py` — **new**: output format, `--yes` bypass, no-TTY bypass, quick mode boundary
- `tests/test_named_failures.py` — **new**: `format_failure_line` for timeout/non-zero/empty; env failure with fix hint; phase/story propagation; stderr final-line invariant
- `tests/test_cli.py` — extend: `--yes` flag present on `pro` and `quick`

## Tasks & Acceptance

**Execution:**
- [x] `foundry/context.py` — add `phase: str | None = None` and `story: str | None = None` to `FoundryError.__init__`; add `format_failure_line(err: FoundryError) -> str`; include `fix` hint on next line when set — format always shows both fields, `-` when None
- [x] `foundry/context.py` — add `yes: bool = False` to `RunContext`
- [x] `foundry/cli.py` — add `yes: bool = typer.Option(False, "--yes", ...)` to `pro` and `quick`; pass `yes=yes` into `RunContext`; update `_fail_with` to call `format_failure_line`
- [x] `foundry/readback.py` — `SpecReadback.present(ctx)` implemented; keyword detection, `~` inference markers, `--yes` bypass, Ctrl-C raises `EnvFailure("run aborted at readback")`
- [x] `foundry/signal.py` — `PhaseSignal.present(...)` implemented; `--yes` and no-TTY bypass
- [x] `foundry/orchestrator.py` — readback injected after `record_run_start`, phase signal injected at each boundary except after the last phase
- [x] `foundry/agent.py` (read-only context) — protocol untouched per constraint
- [x] `tests/test_readback.py` — 14 tests, all passing
- [x] `tests/test_signal.py` — 12 tests, all passing
- [x] `tests/test_named_failures.py` — 12 tests, all passing; stderr final-line regex verified
- [x] `tests/test_cli.py` — 6 new tests for `--yes` flag on `pro` and `quick`

**Acceptance Criteria:**
- Given `foundry pro "build a todo app"` on a TTY, when the run initializes, then a structured readback prints before any phase executes and the run waits for keypress
- Given `--yes` is passed, when the readback runs, then it prints to stdout and the run proceeds without waiting
- Given Ctrl-C during readback, when the user aborts, then the run exits cleanly with no foundry.lock and no git commits created
- Given any agent timeout, non-zero exit, or empty-output failure, when the run exits, then the final stderr line matches `[<phase>] failed: <distinct reason>` for each condition
- Given an `EnvFailure` with `fix` set, when formatting, then the fix hint appears on the line after the named failure line
- Given a phase completes, when the next phase is about to begin, then stdout shows what completed and what's next before proceeding
- Given `--yes` or no TTY, when the phase signal runs, then it prints and continues without waiting
- Given quick mode, when a phase boundary is crossed, then the phase signal fires identically to pro mode

## Implementation Notes

## Spec Change Log

- **2026-10-30**: `format_failure_line` format updated per user decision during matrix audit — always shows `[phase / story]` with `-` placeholder when either field is None (matches frozen matrix row 3 example `[brainstorm / -]`). Tasks section had said "omitting story segment when None" — corrected to match frozen intent.

## Review Triage Log

| # | Finding | Verdict | Route | Evidence |
|---|---------|---------|-------|----------|
| 1 | `format_failure_line` doesn't sanitize `err.message` — multiline message breaks the "first line only" guarantee | medium | patch | `f"[...] failed: {err.message}"` — no `splitlines()` guard; real if any agent error has `\n` in message |
| 2 | Double "To fix:" in `_fail_with` halted branch — when `hint != GENERIC_FIX` AND `err.phase` is not None AND `err.fix` is set, hint appears twice | medium | patch | Halted branch appends `format_failure_line(err)` which itself appends `To fix: {err.fix}`; currently dead code since `err.phase` is never set, but will manifest once phase annotation is wired |
| 3 | `PhaseSignal` uses `sys.stdout.isatty()` but `SpecReadback` uses `sys.stdin.isatty()` — inconsistent and wrong stream for `input()` | medium | patch | `input()` reads stdin; gating on stdout breaks when stdin and stdout have different TTY status |
| 4 | `PhaseSignal.present` has no `KeyboardInterrupt`/`EOFError` handler — crashes uncleanly on Ctrl-C or piped stdin | medium | patch | `SpecReadback.present` handles both; `signal.py:45` lets exceptions propagate raw through `asyncio.to_thread` |
| 5 | `err.phase` is never set anywhere in the diff — `_fail_with`'s named failure path is dead code | medium | patch | Searched all `foundry/` source: only `context.py` initialises `phase = None`, nothing sets it; orchestrator needs to set `err.phase = phase.name` when catching a phase exception |
| 6 | `--yes` → `ctx.yes` propagation not verified under TTY conditions — CliRunner is non-TTY so removing `yes=yes` doesn't fail any test | medium | patch | Verification-gap analysis: remove `yes=yes` from `RunContext(...)` and all six `TestYesFlag` tests still pass because CliRunner's isatty returns False |
| 7 | `PhaseSignal` orchestrator integration not verified via output — removing the phase signal block from `orchestrator.py` fails no tests | medium | patch | Verification-gap analysis: no test captures stdout and asserts `"✓"` or `"→"` in the output |
| 8 | `tests/test_signal.py` missing 2 abort path tests (Ctrl-C and EOFError) — spec claims 12 tests but diff has 10 | medium | patch | Same root cause as finding #4; count will be correct after adding the 2 tests |
| 9 | Wrong type annotation `pytest.TemporaryDirectory` on `tmp_path` in `test_readback.py` | low | patch | `tmp_path` is a `pathlib.Path` pytest fixture; `pytest.TemporaryDirectory` is not a valid annotation |
| 10 | Dead `monkeypatch` parameter in `_present` helper in `test_signal.py` | low | patch | `monkeypatch` declared but never used inside `_present` |
| 11 | `_extract_project_name` always returns `inferred=True` — correct for this context; user's idea text is never a literal project name declaration | false | — | Marking name as `~inferred` is the correct UX behaviour; design notes say "story count is always TBD" as the same principle |
| 12 | `\bgo\b` false positive language detection | low | reject | Complex heuristic required; user sees `~go` and can correct via abort; fix adds branches beyond simple correction |
| 13 | `PhaseSignal` uses `input("")` with no visible prompt | false | — | Spec's Design Notes explicitly specify `input("")` to avoid echoing a second newline; intentional |
| 14 | Orchestrator hardcodes `"completed"` as outcome — Phase Protocol has no outcome-reporting surface | low | defer | Pre-existing Phase Protocol limitation; adding outcome data to Phase Protocol is out of scope for this story |
| 15 | `phase`/`story` as post-construction attributes (no IDE enforcement) | false | — | Spec mandates these as optional fields attached at raise sites, by design; constructor params would require all existing raise sites to change |

## Design Notes

`SpecReadback` heuristics: scan `ctx.idea` for language/framework keywords case-insensitively. Mark any field that was not explicitly stated in the idea with `~` to distinguish inference from fact. Story count is not knowable before planning runs — output `~TBD (determined after spec phase)` consistently so the readback is honest about what is unknown.

`PhaseSignal` uses `input("")` (not `sys.stdin.read()`) so it works on Windows without a terminal workaround. The empty-string prompt avoids echoing a second newline.

`format_failure_line` must produce a single line (no embedded newlines in the `[...] failed: reason` part); the `fix` hint goes on a second line when present. The caller (`_fail_with`) is responsible for writing to stderr.

## Verification

**Commands:**
- `uv run pytest tests/test_readback.py tests/test_signal.py tests/test_named_failures.py -v` — expected: all pass
- `uv run pytest --tb=short -q` — expected: ≥950 passing, 0 failing
