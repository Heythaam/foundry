---
title: Foundry CLI
status: final
created: 2026-10-28
updated: 2026-10-28
---

# PRD: Foundry CLI

## 0. Document Purpose

This PRD defines Foundry, a Python CLI that orchestrates BMad agents to turn a one-line idea into a fully implemented GitHub project with provenance tracking and human checkpoints. It is written for the developer building Foundry and for downstream architecture, epic, and story generation. Features are grouped with globally numbered FRs nested under each. Assumptions are tagged inline as `[ASSUMPTION]` and indexed in §9. The brainstorm intent document (`docs/brainstorming/brainstorm-foundry-cli-2026-10-28/brainstorm-intent.md`) is the upstream input; this PRD builds on it.

## 1. Vision

Foundry turns a single sentence into a deployed software project — not by generating a file dump, but by running the same planning and implementation process a disciplined team would: brainstorm the idea, write a PRD, design the architecture, break it into epics and stories, implement each story with tests, and ship a containerized artifact.

The difference is that every decision along the way is recorded. Foundry produces two things: the software, and a complete, machine-readable provenance trail of how that software came to exist. Every checkpoint approval, every architecture choice, every failed test and its resolution lives in the git history and a structured lock file. The repository tells the full story of how it was built, not just what was built.

This matters because AI-assisted development is becoming the norm, but the process is invisible. Foundry makes AI development legible — inspectable, replayable, and auditable. A developer who clones a Foundry-built repo can trace any decision back to the conversation that produced it.

## 2. Target User

### 2.1 Jobs To Be Done

- **Turn an idea into running software without losing the "why."** I have an idea for a tool. I want to go from concept to deployed code, but I also want to understand (and be able to explain) every decision that was made along the way.
- **Get a structured planning process without the overhead.** I know I should write a PRD and think about architecture before coding, but doing it alone feels like busywork. I want an AI collaborator that runs the process with me, not for me.
- **Resume a project after interruption without losing context.** I started a Foundry run yesterday, my laptop crashed mid-build. I want to pick up exactly where I left off.
- **Produce auditable AI-assisted software.** I need to show stakeholders (or myself) that the AI didn't just "vibe code" this — there was a process, there were checkpoints, there were human decisions.

### 2.2 Non-Users (v1)

- **Teams with existing CI/CD and deployment infrastructure.** Foundry outputs a Dockerfile; it does not manage deployment. Teams looking for a deployment orchestrator should look elsewhere.
- **Non-technical users.** Foundry is a CLI tool that expects the user to understand software development concepts, review PRDs, and evaluate architecture decisions.
- **Users who want fully autonomous code generation.** Foundry has mandatory human checkpoints. Users who want zero-touch generation are not the audience.

### 2.3 Key User Journeys

- **UJ-1. Sam builds a side project from scratch.**
  - **Persona + context:** Sam, a backend developer with a weekend idea for a habit tracker.
  - **Entry state:** Terminal open, Foundry installed, GitHub credentials configured.
  - **Path:** Runs `foundry pro "a daily habit tracker with streaks and reminders"`. Foundry streams the BMad brainstorm output to terminal. Sam's `$EDITOR` opens with the brainstorm doc — Sam trims 3 of 13 directions, saves, closes. PRD phase runs, editor opens again — Sam edits two requirements, saves. Architecture phase opens with tech stack and UX choices — Sam changes the database from Postgres to SQLite, saves. Epics generate automatically. Foundry prompts for repo name ("daily-habit-tracker"), Sam confirms. Scaffold is committed. Implementation begins — terminal streams progress per story. Story 2.3 fails tests, auto-retries once, passes on retry. All stories complete. Dockerfile is generated and committed. Sam sees the final commit log and the repo URL.
  - **Climax:** Sam opens the GitHub repo and sees a clean commit history that reads like a development journal — every planning decision, every implementation step, every retry.
  - **Resolution:** A working containerized app with full provenance. Sam can `docker build` and run it locally, or deploy the Dockerfile anywhere.

- **UJ-2. Sam's laptop crashes mid-build and resumes.**
  - **Persona + context:** Same Sam, 6 stories into a 14-story build.
  - **Entry state:** Laptop rebooted, terminal open, in the project directory.
  - **Path:** Runs `foundry resume`. Foundry reads git state and foundry.lock, determines story 6 was the last completed. Reports: "Resuming from story 7/14. Model pinned to claude-sonnet-5-5 (same as original run)." Implementation continues from story 7.
  - **Climax:** The build completes as if the crash never happened.
  - **Resolution:** No lost work, no duplicate stories, no style discontinuity.

- **UJ-3. Sam uses Quick mode for a small utility.**
  - **Persona + context:** Sam wants a URL shortener in 20 minutes, doesn't need full planning.
  - **Entry state:** Terminal open.
  - **Path:** Runs `foundry quick "a URL shortener with click tracking"`. Foundry asks the LLM to produce a one-page spec (what it's building, what it's not, what it assumes). Editor opens — Sam tweaks one assumption, saves. Implementation begins with incremental commits. Tests are required before each commit. Dockerfile generated at the end.
  - **Climax:** A working URL shortener with a clean git history and a lock file, built in minutes.
  - **Resolution:** Less provenance than Pro mode, but still structured, incremental, and verified.

## 3. Glossary

- **Foundry Run** — A single end-to-end execution of Foundry, from idea input to final Dockerfile commit. Identified by the git repository it produces.
- **Planning Pipeline** — The sequence of BMad agent phases: brainstorm, PRD, architecture, epics. Produces planning artifacts that feed implementation.
- **Implementation Loop** — The per-story cycle of: pass story to Claude Code agent → run tests → retry or escalate → commit on green.
- **Checkpoint** — A mandatory pause where the human reviews and may edit a planning artifact before Foundry proceeds. Implemented by opening the artifact in `$EDITOR`.
- **foundry.lock** — A TOML file at repository root that records provenance metadata for every phase of a Foundry run: model versions, prompt hashes, artifact hashes, timestamps, and human intervention records.
- **Forge Log** — The git commit history of a Foundry-built repository, where every commit message follows a structured format recording the Foundry decision or action that produced it.
- **Scaffold** — The initial repository structure committed after architecture approval: README stub, LICENSE (MIT), .gitignore, and foundry.lock seed.
- **Phase** — One stage of the planning pipeline (brainstorm, PRD, architecture, epics) or implementation (per-story, Dockerfile generation).
- **Failure Category** — One of three classifications for errors during implementation: code failure, infrastructure failure, or environment failure. Each has a distinct handling strategy.
- **Pre-flight Analysis** — An automated check on planning artifacts (contradiction detection, completeness, traceability) that runs before the architecture phase begins.
- **Consistency Pass** — An optional final implementation phase that normalizes code style across all stories, triggered when the model version changes mid-run.

## 4. Features

### 4.1 CLI Interface

**Description:** Foundry exposes a Python CLI with subcommands for its operating modes. The CLI is the sole entry point for all Foundry operations. Realizes UJ-1, UJ-2, UJ-3.

**Functional Requirements:**

#### FR-1: Pro mode invocation

User can run `foundry pro "<idea>"` to start a full planning-pipeline run with mandatory human checkpoints at brainstorm, PRD, and architecture phases. Realizes UJ-1.

**Consequences (testable):**
- Foundry starts the brainstorm phase within 5 seconds of invocation.
- The idea string is passed verbatim to the BMad brainstorm agent.
- Foundry exits with a non-zero code if the idea string is empty.

#### FR-2: Quick mode invocation

User can run `foundry quick "<idea>"` to start a Quick mode run that skips the planning pipeline and proceeds directly to spec generation and implementation. Realizes UJ-3.

**Consequences (testable):**
- No BMad agents are invoked during a Quick mode run.
- The LLM produces a one-page spec as the first artifact.
- The spec is opened in `$EDITOR` as a checkpoint before implementation begins.

#### FR-3: Resume invocation

User can run `foundry resume` to continue a previously interrupted run from the last completed story. Realizes UJ-2.

**Consequences (testable):**
- Foundry determines the last completed story by reading git state and foundry.lock.
- Foundry reports the resume point (story N of M) and the pinned model version before continuing.
- If no incomplete run is detected, Foundry exits with an informative error message.

#### FR-4: Status invocation

User can run `foundry status` to view the current state of an in-progress or completed Foundry run.

**Consequences (testable):**
- Output includes: current phase, completed stories, total stories, model version, elapsed time.
- Works on any Foundry-built repository (reads foundry.lock and git state).

#### FR-5: Cancel invocation

User can run `foundry cancel` to cleanly stop an in-progress run. `[ASSUMPTION: cancel writes a "cancelled" entry to foundry.lock and leaves the repository in a resumable state rather than cleaning up.]`

**Consequences (testable):**
- The current Claude Code session is terminated.
- A cancellation entry is written to foundry.lock with timestamp.
- The repository remains in a valid state; `foundry resume` can continue from the last completed story.

#### FR-6: Epic review flag

User can pass `--review-epics` to `foundry pro` to enable human checkpoints before each epic's implementation begins.

**Consequences (testable):**
- Without the flag, implementation proceeds automatically after story generation.
- With the flag, Foundry opens each epic's story list in `$EDITOR` before starting that epic's implementation.

### 4.2 BMad Planning Pipeline

**Description:** The core of Foundry Pro mode. Orchestrates four BMad agents in sequence — brainstorm, PRD, architecture, epics — with human checkpoints between phases. Each agent receives the output of the previous phase as input. The planning pipeline runs entirely locally; no GitHub repository exists until after architecture approval. Realizes UJ-1.

**Functional Requirements:**

#### FR-7: Brainstorm phase

Foundry invokes the BMad brainstorm agent with the user's idea string, streams output to the terminal, and opens the brainstorm artifact in `$EDITOR` for human review.

**Consequences (testable):**
- BMad brainstorm agent is called via Claude Code SDK with the idea string as input.
- Agent output is streamed to stdout in real time.
- On agent completion, the brainstorm document is written to a temp workspace and opened in `$EDITOR`.
- Foundry blocks until the editor process exits.
- The saved document (potentially edited by the human) is used as input to the next phase.
- A checkpoint entry is written to foundry.lock recording: phase name, timestamp, artifact hash, and the human's time spent in editor.

#### FR-8: PRD phase

Foundry invokes the BMad PRD agent with the approved brainstorm document and opens the result in `$EDITOR`.

**Consequences (testable):**
- The brainstorm document is passed as input context to the BMad PRD agent.
- The PRD artifact is opened in `$EDITOR` for human review and editing.
- The approved PRD (as saved by the human) feeds the architecture phase.
- A checkpoint entry is written to foundry.lock.

#### FR-9: Architecture phase

Foundry invokes the BMad architecture agent with the approved PRD and opens the result in `$EDITOR`. This is the most critical checkpoint — the human reviews and may modify technology choices, database selection, API design, and UX decisions.

**Consequences (testable):**
- The PRD is passed as input context to the BMad architecture agent.
- The architecture document is opened in `$EDITOR`.
- The approved architecture document defines the tech stack, test runner, and project structure for implementation.
- The test runner command is extracted from the architecture document and written to foundry.lock.
- A checkpoint entry is written to foundry.lock.

#### FR-10: Epics and stories phase

Foundry invokes the BMad epics/stories agent with the approved architecture document. Stories are generated and (unless `--review-epics` is set) proceed directly to implementation.

**Consequences (testable):**
- The architecture document is passed as input context to the BMad epics/stories agent.
- The output is a structured set of epics, each containing ordered stories with acceptance criteria.
- If `--review-epics` is set, each epic's stories are opened in `$EDITOR` before implementation.
- Epic and story metadata (names, count, acceptance criteria hashes) are written to foundry.lock.

### 4.3 Pre-flight Analysis

**Description:** An automated analysis pass that runs on the PRD artifact before the architecture phase begins. Checks for internal contradictions, completeness gaps, and ambiguities. Surfaces findings as a hard-stop checkpoint — the human must resolve flagged issues before architecture can proceed. Realizes UJ-1.

**Functional Requirements:**

#### FR-11: Contradiction detection

Foundry analyzes the approved PRD for internally contradictory requirements before passing it to the architecture agent.

**Consequences (testable):**
- If contradictions are detected, Foundry opens a findings document in `$EDITOR` listing each contradiction with the conflicting requirement IDs.
- The human must resolve each contradiction (edit the PRD or mark as "accepted conflict") before the architecture phase begins.
- Resolutions are recorded in foundry.lock as first-class decisions.

#### FR-12: Completeness check

Foundry checks the PRD for common omissions: authentication, error handling, data persistence, input validation.

**Consequences (testable):**
- Omissions are surfaced as questions, not assumptions: "Your PRD does not mention authentication. Is this intentional?"
- The human's response (intentional omission or added requirement) is recorded in foundry.lock.

#### FR-13: Traceability validation

Foundry verifies that every brainstorm direction either maps to a PRD requirement or was explicitly excluded.

**Consequences (testable):**
- Unmapped brainstorm directions are listed for human review.
- The human marks each as "intentionally excluded" or adds it to the PRD.
- Exclusion decisions are recorded in foundry.lock.

### 4.4 Repository Creation and Scaffold

**Description:** After architecture approval, Foundry creates a GitHub repository and commits the initial scaffold. The planning phase is entirely local; the repo is the transition point to implementation. Realizes UJ-1.

**Functional Requirements:**

#### FR-14: Repository creation

Foundry derives a repository name from the idea (slugified), prompts the user to confirm or edit the name, and creates a public GitHub repository.

**Consequences (testable):**
- The suggested name is a URL-safe slug derived from the idea string.
- The user can edit the name before creation.
- The repository is created via the GitHub API (PyGitHub).
- `[ASSUMPTION: The repository is created under the authenticated user's personal account, not an organization. Org support is a v1.x addition.]`

#### FR-15: Initial scaffold commit

Foundry commits an initial scaffold to the main branch: README.md (stub with project name and one-line description), LICENSE (MIT), .gitignore (language-appropriate, derived from architecture), a seeded foundry.lock, and `.github/workflows/ci.yml` (FR-37).

**Consequences (testable):**
- The scaffold commit message follows the forge log format: `foundry: scaffold — README, LICENSE, .gitignore, ci.yml, foundry.lock seeded`.
- foundry.lock contains: model version, run start timestamp, idea string hash, and all planning-phase checkpoint records.
- The .gitignore is appropriate for the tech stack defined in the architecture document.

### 4.5 Implementation Loop

**Description:** For each epic, Foundry cuts a feature branch, implements each story via a Claude Code SDK session, runs tests, handles failures per the failure taxonomy, and commits on green. One PR per epic, auto-merged when CI passes. Realizes UJ-1, UJ-3.

**Functional Requirements:**

#### FR-16: Feature branch management

Foundry creates a feature branch per epic, named after the epic slug.

**Consequences (testable):**
- Branch name format: `epic/<epic-slug>` (e.g., `epic/user-authentication`).
- The branch is created from the current state of main.
- All story commits for that epic land on the feature branch.

#### FR-17: Story implementation

Foundry passes each story's text and acceptance criteria to a new Claude Code SDK session, along with the full codebase context.

**Consequences (testable):**
- Each story gets its own Claude Code session (no context leakage between stories).
- The session receives: story text, acceptance criteria, architecture document, and the current codebase state.
- The Claude Code agent is responsible for writing code and installing dependencies.

#### FR-18: Test execution

After each story implementation, Foundry runs the test runner command defined in foundry.lock.

**Consequences (testable):**
- The test runner command is read from foundry.lock (written during architecture phase via FR-9).
- Green = zero exit code from the test runner.
- `[ASSUMPTION: Linting is not included in the green check for v1. The test runner is the sole arbiter.]`

#### FR-19: Story commit on green

When tests pass, Foundry commits the story's changes with a structured forge-log commit message.

**Consequences (testable):**
- Commit message format: `foundry: story <epic>.<story> implemented — <N> tests passing`.
- The commit includes all files modified or created by the Claude Code session.
- An implementation entry is written to foundry.lock: story ID, artifact hash, test count, duration.

#### FR-20: Pull request and merge

After all stories in an epic are committed, Foundry opens a PR from the epic branch to main and merges it when CI passes.

**Consequences (testable):**
- PR title format: `[Foundry] Epic: <epic name>`.
- PR body includes a summary of implemented stories and test results.
- Foundry merges the PR automatically when the CI workflow passes.
- If CI fails on the PR (integration-level failures not caught by per-story tests), Foundry surfaces the failure to the human via the escalation flow (FR-23) rather than auto-fixing at the PR level.
- A merge entry is written to foundry.lock.

### 4.6 Failure Taxonomy

**Description:** Foundry classifies every error during implementation into one of three categories, each with a distinct handling strategy. This prevents the critical failure mode of handing infrastructure problems to the LLM for "fixing." Realizes UJ-1, UJ-2.

**Functional Requirements:**

#### FR-21: Code failure handling

When the test runner returns a non-zero exit code after story implementation, Foundry classifies this as a code failure and auto-retries once.

**Consequences (testable):**
- Foundry passes the test output back to a new Claude Code session with instructions to fix the failing tests.
- The retry gets one attempt. If tests pass, the story is committed normally.
- If the retry fails, Foundry escalates to the human (FR-23).
- Retry attempts are recorded in foundry.lock: original failure output hash, retry artifact hash, outcome.

#### FR-22: Infrastructure failure handling

When a GitHub API call, Claude Code SDK call, or network request fails with a recognizable infrastructure error (rate limit, timeout, connection refused, 5xx), Foundry pauses and retries with exponential backoff.

**Consequences (testable):**
- Infrastructure failures are never passed to the LLM.
- Backoff starts at 30 seconds, doubles each retry, caps at 10 minutes. `[ASSUMPTION: Max infrastructure retries is 5 before escalating to the human.]`
- Each pause and retry is logged to foundry.lock with: error type, timestamp, retry count.
- The codebase is never modified during infrastructure failure handling.

#### FR-23: Escalate-to-human on persistent failure

When a code failure persists after one auto-retry, or an infrastructure failure exhausts retries, Foundry pauses and opens a failure report in `$EDITOR`.

**Consequences (testable):**
- The failure report includes: failing test output, the generated code, and (for code failures) a diff of what the auto-fix attempted.
- The human can: edit the story description, edit the generated code directly, or mark the story as "deferred."
- The human's choice and any edits are recorded in foundry.lock as a human intervention entry.
- If the story is deferred, Foundry skips it and continues with the next story. Deferred stories are listed in `foundry status` output.

#### FR-24: Environment failure handling

When Foundry detects a fatal environment error (disk full, missing credentials, model unavailable, invalid API key), it stops immediately with a clear error message.

**Consequences (testable):**
- Foundry does not retry environment failures.
- The error message includes: what failed, why, and what the user should fix.
- The run state is preserved; `foundry resume` can continue after the environment issue is resolved.
- An environment failure entry is written to foundry.lock.

### 4.7 Provenance Engine (foundry.lock)

**Description:** A single TOML file at repository root that grows through the run, recording provenance metadata for every phase and decision. The lock file is both a machine-readable audit trail and the source of truth for resume operations. Realizes UJ-1, UJ-2.

**Functional Requirements:**

#### FR-25: Lock file structure

foundry.lock is a TOML file with sections per phase, each recording provenance metadata.

**Consequences (testable):**
- Top-level keys: `[run]` (idea hash, start time, model version, agent versions), `[checkpoints.*]` (per-phase checkpoint records), `[implementation.*]` (per-story records), `[failures.*]` (failure and retry records), `[interventions.*]` (human intervention records).
- Every entry includes a UTC timestamp.
- The file is valid TOML at all times; writes are atomic (write to temp, rename).

#### FR-26: Model version pinning

foundry.lock records the exact model ID used at run start. On resume, Foundry checks whether the same model is still available.

**Consequences (testable):**
- If the model is available, Foundry continues with the same model without prompting.
- If the model is unavailable, Foundry reports the situation and asks the human to choose a replacement model. A consistency pass (FR-30) is automatically scheduled as the final step.
- The model version is never silently changed.

#### FR-27: Artifact hashing

Every planning document and implementation output is hashed (SHA-256 of file content) and recorded in foundry.lock.

**Consequences (testable):**
- The hash is computed on the artifact as saved by the human (post-edit, not pre-edit).
- Hashes are content-addressable: the same content always produces the same hash.
- `foundry status` can detect if a tracked artifact has been modified outside of Foundry by comparing current hash to lock file hash.

#### FR-28: Checkpoint timestamp recording

Every human checkpoint records wall-clock time: when the editor opened, when the editor closed.

**Consequences (testable):**
- Duration is computed and stored alongside the checkpoint entry.
- `[ASSUMPTION: Checkpoint duration is informational only in v1. Anti-rubber-stamp enforcement (minimum read times, acknowledgment flags) is deferred to COULD scope.]`

### 4.8 Forge Log (Structured Git History)

**Description:** Every Foundry action that modifies the repository is committed with a structured message that follows a consistent format. The git log is the human-readable forge log. Realizes UJ-1, UJ-2.

**Functional Requirements:**

#### FR-29: Commit message format

All Foundry-generated commits follow the format: `foundry: <action> — <detail>`.

**Consequences (testable):**
- Planning commits: `foundry: <phase> approved — <summary>` (e.g., `foundry: brainstorm approved — 10 directions chosen`).
- Implementation commits: `foundry: story <epic>.<story> implemented — <N> tests passing`.
- Retry commits: `foundry: story <epic>.<story> auto-fixed — retry <N>/<max>, <reason>`.
- Infrastructure commits: `foundry: scaffold — README, LICENSE, .gitignore, foundry.lock seeded`.
- Dockerfile commits: `foundry: dockerfile generated — <base image>, <port>`.
- Human intervention commits: `foundry: story <epic>.<story> — human intervention, <action taken>`.
- Resume commits: `foundry: resumed — from story <N>/<M>, model <version>`.
- The format is machine-parseable: `git log --format="%s" | grep "^foundry:"` extracts all Foundry actions.

### 4.9 Dockerfile Generation

**Description:** After all epics are implemented and merged, Foundry generates a Dockerfile as a post-implementation step. The Dockerfile is the universal deployment interface — Foundry has no opinions about where the container runs. Realizes UJ-1, UJ-3.

**Functional Requirements:**

#### FR-30: Dockerfile generation

Foundry generates a Dockerfile appropriate to the tech stack defined in the architecture document, plus a health check endpoint and port declaration.

**Consequences (testable):**
- The Dockerfile uses a standard base image appropriate to the language/framework.
- A health check endpoint (`/health` or equivalent) is included or verified to exist.
- The port is declared via `EXPOSE`.
- The Dockerfile is committed to main with a forge-log commit message.
- No deployment-target-specific configuration is generated (no fly.toml, no render.yaml, no docker-compose.yml).

### 4.10 CI Workflow Generation

**Description:** Foundry generates a minimal GitHub Actions CI workflow that runs the test command from foundry.lock on every pull request. This is the only CI file Foundry generates — no CD, no deployment pipeline. The workflow is committed as part of the scaffold step so that PR auto-merge (FR-20) has a gate to pass. Realizes UJ-1.

**Functional Requirements:**

#### FR-37: Minimal CI workflow

Foundry generates `.github/workflows/ci.yml` during the scaffold step (FR-15). The workflow runs the test runner command extracted from the architecture document.

**Consequences (testable):**
- The workflow triggers on pull requests to main.
- The workflow runs a single step: install dependencies and execute the test runner command from foundry.lock.
- The workflow uses a language-appropriate GitHub Actions setup (e.g., `actions/setup-python` for Python projects).
- No deployment steps, release steps, or CD configuration is included.
- The workflow file is committed with the scaffold commit.

### 4.11 Checkpoint Web UI (SHOULD)

**Description:** An alternative to `$EDITOR`-based checkpoints. Foundry spins up a local web server and opens a browser tab for reviewing and editing planning documents with rich text rendering. The terminal continues streaming progress. Realizes UJ-1.

**Functional Requirements:**

#### FR-31: Local checkpoint server

When the web UI is enabled, Foundry starts a local HTTP server for checkpoint interactions instead of opening `$EDITOR`.

**Consequences (testable):**
- `[ASSUMPTION: Web UI is activated via a --web flag on foundry pro / foundry quick. It is not the default; $EDITOR is the default.]`
- The server binds to localhost on an available port.
- The browser is opened automatically to the checkpoint page.
- The checkpoint page renders the planning artifact with markdown formatting.
- The human can edit the document in the browser and click "Approve & Continue."
- The server shuts down after the checkpoint is resolved.

### 4.12 Resume and Recovery

**Description:** Foundry can resume an interrupted run from the last completed story by reading git state and foundry.lock. No separate run database is required. Realizes UJ-2.

**Functional Requirements:**

#### FR-32: Resume from git state

`foundry resume` determines the last completed story by parsing foundry.lock and the git log.

**Consequences (testable):**
- Foundry reads foundry.lock for the story manifest (epic/story list from FR-10) and the last recorded implementation entry.
- The next uncompleted story is identified and implementation continues from there.
- All planning artifacts are read from the repository (committed during planning phases).

#### FR-33: Model continuity check

On resume, Foundry checks whether the pinned model version is still available.

**Consequences (testable):**
- If available, Foundry continues silently with the same model.
- If unavailable, Foundry prompts the human to choose a replacement and schedules a consistency pass.
- The model decision is recorded in foundry.lock as a resume entry.

#### FR-34: Consistency pass

When a model change occurs mid-run, Foundry runs a final pass after all stories are implemented that reviews the entire codebase for style and convention consistency.

**Consequences (testable):**
- The consistency pass is a dedicated Claude Code session that receives the full codebase and instructions to normalize style, naming, and error handling.
- Changes are committed with a forge-log message: `foundry: consistency pass — model boundary at story <N>/<M>`.
- The consistency pass entry in foundry.lock records: original model, new model, boundary story, changes made.

### 4.13 Foundry Quick Mode

**Description:** A secondary entry point that skips the BMad planning pipeline. The LLM generates a one-page spec, the human reviews it, and implementation proceeds with the same incremental-commit and test-required guarantees as Pro mode. Realizes UJ-3.

**Functional Requirements:**

#### FR-35: Self-written spec

In Quick mode, the LLM generates a one-page spec before implementation: what it's building, what it's not building, and what it assumes.

**Consequences (testable):**
- The spec is opened in `$EDITOR` (or web UI if `--web`) as a checkpoint.
- The approved spec is written to foundry.lock and committed as the first forge-log entry.
- No BMad agents are invoked.

#### FR-36: Incremental commits

Quick mode produces logical, incremental commits rather than a single file dump.

**Consequences (testable):**
- Each logical unit (models, routes, tests, config) is committed separately.
- Every commit has a forge-log format message.
- Tests must pass before each commit (same as FR-18/FR-19).

## 5. Non-Goals (Explicit)

- **Foundry is not a deployment tool.** It produces a Dockerfile. Where the container runs is not Foundry's concern. No fly.toml, no render.yaml, no Kubernetes manifests.
- **Foundry is not a CD pipeline generator.** Foundry generates a minimal CI workflow (test runner only) to gate PR merges, but does not generate deployment pipelines, release workflows, or CD configuration.
- **Foundry is not fully autonomous.** Mandatory human checkpoints exist and cannot be disabled. `[NON-GOAL for MVP]` A headless/fully-autonomous mode may be explored in v2.
- **Foundry does not manage the deployed application.** No monitoring, no scaling, no log aggregation.
- **Foundry does not support multiple concurrent runs.** One run per repository at a time.
- **Foundry does not support team collaboration during a run.** One human operator per run.
- **Foundry does not reverse-engineer existing codebases** (`foundry reverse` is deferred to v2+).
- **Foundry does not backtrack through the planning pipeline** (DAG/rewind is deferred to v2+).

## 6. MVP Scope

### 6.1 In Scope

- `foundry pro` — full BMad planning pipeline with human checkpoints
- `foundry quick` — skip planning, spec-first implementation
- `foundry resume` — continue from last completed story
- `foundry status` — view run state
- `foundry cancel` — clean stop with resumable state
- foundry.lock provenance tracking (model pinning, artifact hashes, checkpoint records)
- Forge log (structured git commit messages)
- Three-category failure taxonomy with escalate-to-human
- Pre-flight analysis (contradiction detection, completeness check, traceability)
- GitHub repo creation after architecture approval
- Scaffold generation (README, LICENSE, .gitignore, CI workflow)
- Minimal GitHub Actions CI workflow (test runner only, no CD)
- Dockerfile generation as post-implementation step
- One PR per epic, auto-merge when CI workflow passes
- Resume from git state with model continuity check

### 6.2 Out of Scope for MVP

- **Checkpoint web UI** — `$EDITOR` is the v1 default. Web UI is a SHOULD target for v1.1. `[NOTE FOR PM: The web UI is the second most requested feature from brainstorming. Prioritize for fast follow.]`
- **foundry.yaml declarative process spec** — COULD. Useful for reproducible runs but not blocking.
- **Dependency verification step** — COULD. Validates that named dependencies exist on package registries.
- **Anti-rubber-stamp enforcement** — COULD. Minimum read times, acknowledgment flags in lock file.
- **--from-prd ingest** — COULD. Accept an external PRD as pipeline entry point.
- **`foundry reverse`** — WON'T. Reverse-engineer spec from existing codebase.
- **DAG pipeline with backtracking** — WON'T. `foundry rewind --to <phase>`.
- **Live project dashboard** — WON'T. Real-time browser-based run monitoring.
- **Template registry** — WON'T. Community-shared foundry.yaml templates.
- **Organization/team repo support** — deferred to v1.x.
- **CD pipeline generation** — deferred; Foundry generates CI (test runner) but not CD (deployment workflows).

## 7. Success Metrics

**Primary**

- **SM-1**: End-to-end completion rate — percentage of `foundry pro` runs that produce a working Dockerfile-ready repository without manual intervention outside of checkpoints. Target: >70% for well-scoped ideas. Validates FR-7 through FR-10, FR-16 through FR-20, FR-30.
- **SM-2**: Resume success rate — percentage of interrupted runs that resume and complete successfully. Target: >95%. Validates FR-32, FR-33, FR-34.

**Secondary**

- **SM-3**: Provenance completeness — percentage of Foundry actions that have a corresponding entry in both foundry.lock and the git log. Target: 100%. Validates FR-25 through FR-29.
- **SM-4**: Failure classification accuracy — percentage of errors correctly categorized (code vs. infra vs. environment) without human correction. Target: >90%. Validates FR-21, FR-22, FR-24.
- **SM-5**: Time from idea to Dockerfile — wall-clock time for a median-complexity project (10-15 stories). Target: under 60 minutes including human checkpoint time. Validates FR-1 through FR-10, FR-16 through FR-20, FR-30.

**Counter-metrics (do not optimize)**

- **SM-C1**: Checkpoint bypass rate — percentage of checkpoints where the human makes zero edits. If this exceeds 80%, the checkpoints may be too easy to rubber-stamp or the artifacts may be too good to need editing. Counterbalances SM-1, SM-5.
- **SM-C2**: Auto-retry success rate — if auto-retries succeed >95% of the time, the single-retry limit may be too conservative; if <30%, the LLM may not be capable of self-correction and the retry is wasted tokens. Counterbalances SM-1.

## 8. Cross-Cutting NFRs

### Performance

- **NFR-1**: Foundry CLI startup time must be under 2 seconds on a standard developer machine.
- **NFR-2**: foundry.lock writes must be atomic (write to temp file, rename) to prevent corruption on crash.
- **NFR-3**: Terminal streaming of BMad agent output must have no perceptible buffering delay (flush per line).

### Reliability

- **NFR-4**: Foundry must be crash-safe at every point in the pipeline. Any crash followed by `foundry resume` must not produce duplicate stories, lost stories, or corrupted state.
- **NFR-5**: foundry.lock must be valid TOML at all times, including during writes (atomic write via temp file).

### Security

- **NFR-6**: GitHub tokens must be read from environment variables or the system credential store, never from foundry.lock or committed files.
- **NFR-7**: foundry.lock must not contain LLM response content, API keys, or tokens — only hashes, metadata, and timestamps.

### Observability

- **NFR-8**: Every Foundry action must produce a forge-log commit message AND a foundry.lock entry. No silent state changes.
- **NFR-9**: `foundry status` must be accurate within one story of the actual run state (i.e., it reads committed state, not in-progress state).

## 9. Constraints and Guardrails

### Technical Constraints

- **Python 3.11+** as minimum runtime. `[ASSUMPTION: 3.11 chosen for tomllib in stdlib, avoiding a third-party TOML dependency for reading foundry.lock.]`
- **Typer** for CLI framework. `[ASSUMPTION: Typer over Click for auto-generated help, type hints, and less boilerplate. Both are viable.]`
- **Claude Code SDK** for all LLM agent interactions. No direct Anthropic API calls.
- **PyGitHub** for GitHub API operations.
- **aiosqlite** for internal run state management (in-progress state between atomic foundry.lock writes).
- **MIT License** for Foundry itself.

### Guardrails

- Foundry must never commit files that contain secrets (API keys, tokens, passwords). `[ASSUMPTION: Foundry runs a basic secret scan on staged files before commit, using pattern matching for common secret formats.]`
- Foundry must never force-push to any branch.
- Foundry must never delete a branch that was not created by the current run.
- Foundry must never modify files outside the repository directory.

## 10. Open Questions

1. **Multi-language support**: The architecture phase defines the tech stack, but does Foundry need to understand language-specific conventions (Python project structure vs. Node.js vs. Go) or is that entirely the Claude Code agent's responsibility?
2. **Cost estimation**: Should Foundry provide a token cost estimate before starting a Pro run, given that a full pipeline may consume significant API credits?
3. **Offline mode**: The brainstorm intent mentioned "works offline" as a benefit of local-first planning. Should Foundry support a fully offline planning phase (using a local model) with online-only implementation?
4. **foundry.lock merge conflicts**: When Foundry merges epic PRs to main, foundry.lock will have concurrent modifications. Strategy: always append, never edit in place? Or a more structured merge driver?

## 11. Assumptions Index

- **FR-5**: Cancel writes a "cancelled" entry to foundry.lock and leaves the repository resumable rather than cleaning up.
- **FR-14**: Repository is created under the authenticated user's personal account, not an organization.
- **FR-18**: Linting is not included in the green check for v1; the test runner is the sole arbiter.

- **FR-22**: Max infrastructure retries is 5 before escalating to the human.
- **FR-28**: Checkpoint duration is informational only in v1; anti-rubber-stamp enforcement is deferred.
- **FR-31**: Web UI is activated via `--web` flag, not the default.

- **§9**: Python 3.11+ chosen for tomllib in stdlib.
- **§9**: Typer chosen over Click.
- **§9**: Foundry runs a basic secret scan on staged files before commit.
