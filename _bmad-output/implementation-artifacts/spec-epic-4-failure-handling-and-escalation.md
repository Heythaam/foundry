---
title: 'Epic 4: Failure handling and human escalation'
type: 'feature'
created: '2026-10-29'
status: 'in-review'
baseline_commit: '38cfa6e889b3e10c001114fbb8767d202edc4138'
route: 'dispatch'
review_loop_iteration: 0
context: ['docs/epics.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Epic 3's loop stops dead on any red test, red CI, flaky API call or missing prerequisite (`HaltOnFailure`). Every failure needs a human even when a second try or a short wait would fix it, and no failure leaves a usable record or fix instructions.

**Approach:** Replace `HaltOnFailure` with a real `FailureHandler` (Stories 4.1-4.5, FR-21..24), plugged into the existing seam. Code failures get one automatic fresh-session retry; infrastructure failures get exponential backoff with jitter; what is still failing goes to the human in `$EDITOR`; environment failures stop at once, saved to the lock with the fix.

## Boundaries & Constraints

**Always:**
- Classification is `code` | `infrastructure` | `environment`; anything unclassifiable is `environment` (AD-3).
- All git/GitHub calls stay in `gitops.py` (AD-8); the lock stays append-only, each section written once; every lock write is committed (AD-2).
- A retry session gets its own `claude -p` and prompt file (AD-7). Infrastructure errors never reach the LLM.
- Commit subjects: `foundry: story E.S auto-fixed — title`, `foundry: story E.S — human intervention`.
- Lock stores hashes and counts, never output or story text (NFR-7).
- Tests green after each story; no commits unless asked; no Epic 5 work (`resume` stays a stub).

**Never:** a second automatic code retry; retrying `environment` errors; deleting a human's or session's uncommitted work (skip stashes it); reading `GITHUB_TOKEN` outside gitops.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Retry fixes it | gate red, 2nd session green | commit `auto-fixed`; `[failures.epic-E.story-S]` `retry_outcome="passed"` in the same commit | N/A |
| Retry fails | gate red twice | `retry_outcome="failed"` recorded, escalate | human decides |
| Transient infra | 429/5xx/timeout | wait `base*2^n` + jitter, retry the call; no lock writes while waiting | after 5 retries escalate |
| Escalation: retry | story text edited | fresh session on the edited story, then gate | red again: escalate again |
| Escalation: continue | human edited code | gate runs; green: commit `human intervention` | red: escalate again |
| Escalation: skip | — | work stashed, story not committed, listed in PR body | N/A |
| Escalation: cancel / no choice | — | run ends as cancelled | tree left as is |
| Environment failure | missing token, disk full, no claude | no retry; `[halts.halt-N]` appended; message says what to fix | halt record failing never masks the error |
| Red CI | PR checks failed | escalate (retry re-checks CI / cancel) | PR left open |

</frozen-after-approval>

## Code Map

- `foundry/context.py` -- error taxonomy, `classify_exception`: extend (OSError errnos, timeouts, `fix` hint on `FoundryError`).
- `foundry/failures.py` (new) -- `FailureClassifier`, `BackoffPolicy`/`with_backoff`, `StoryEscalation`, `RetryingFailureHandler`, env hints.
- `foundry/implementation.py` -- `FailureHandler` seam, `HaltOnFailure`, `implement_story`, `run_gate`, `finish_epic`: widen the seam (retry callback, escalation), wrap external calls, skipped stories in PR body.
- `foundry/provenance.py` -- `record_story` (message, failure/intervention sections in one commit), `record_failure`, `record_halt`.
- `foundry/gitops.py` -- add `staged_diff_text`, `stash`, `pull_request`; keep `create_pr` idempotent for backoff.
- `foundry/checkpoint.py`, `foundry/context.py::ArtifactType` -- reuse `EditorCheckpoint` for the escalation file (new `ESCALATION` type).
- `foundry/orchestrator.py` -- stage failures: classify, record halt for `environment`.
- `foundry/cli.py` -- wire the handler; print `fix` hint.
- `tests/` -- `test_failure_classifier.py`, `test_code_retry.py`, `test_infra_backoff.py`, `test_escalation.py`, `test_env_halt.py`; helpers in `tests/planned.py`.

## Tasks & Acceptance

**Execution:**
- [x] `foundry/context.py`, `foundry/failures.py` -- classifier covering subprocess, API, git, editor and OS errors -- 4.1
- [x] `foundry/failures.py`, `foundry/implementation.py`, `foundry/provenance.py` -- one-shot code retry, `auto-fixed` commit, failures section -- 4.2
- [x] `foundry/failures.py`, `foundry/implementation.py`, `foundry/gitops.py` -- backoff around session, push, PR, CI, merge -- 4.3
- [x] `foundry/failures.py`, `foundry/provenance.py`, `foundry/gitops.py` -- escalation file, four actions, interventions section, intervention commit -- 4.4
- [x] `foundry/orchestrator.py`, `foundry/failures.py`, `foundry/cli.py` -- environment hard stop and halt record -- 4.5

**Acceptance Criteria:**
- Given the epics' Story 4.1-4.5 criteria, when each story is done, then its tests pass and the whole suite stays green.
- Given a full run through the Orchestrator, when a story fails once and then passes, then the epic still merges.

## Implementation Notes

## Design Notes

Escalation file (opened via `EditorCheckpoint`): story text (editable), error, failing test output, `git diff`, then `action: ` with `retry | continue | skip | cancel` listed. Unchanged/empty `action` = cancel; an unknown word reopens the file (3 tries). Epic-level infra and CI failures offer only `retry | cancel`.

## Verification

**Commands:**
- `.venv/Scripts/python -m pytest -q -x` -- expected: all pass
