---
stepsCompleted: [step-01-validate-prerequisites, step-02-design-epics, step-03-create-stories]
inputDocuments:
  - docs/prds/prd-foundry-2026-10-28/prd.md
  - docs/architecture/architecture-foundry-2026-10-28/ARCHITECTURE-SPINE.md
---

# Foundry CLI - Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for Foundry CLI, decomposing the requirements from the PRD and Architecture into implementable stories.

## Requirements Inventory

### Functional Requirements

FR-1: User can run `foundry pro "<idea>"` to start a full planning-pipeline run with mandatory human checkpoints
FR-2: User can run `foundry quick "<idea>"` to start a Quick mode run that skips planning
FR-3: User can run `foundry resume` to continue a previously interrupted run
FR-4: User can run `foundry status` to view current run state
FR-5: User can run `foundry cancel` to cleanly stop an in-progress run
FR-6: User can pass `--review-epics` to enable human checkpoints before each epic
FR-7: Foundry invokes BMad brainstorm agent, streams output, opens checkpoint
FR-8: Foundry invokes BMad PRD agent with brainstorm output, opens checkpoint
FR-9: Foundry invokes BMad architecture agent with PRD, opens checkpoint, extracts test runner
FR-10: Foundry invokes BMad epics/stories agent with architecture output
FR-11: Foundry analyzes PRD for contradictions before architecture phase
FR-12: Foundry checks PRD for common omissions (auth, error handling, persistence)
FR-13: Foundry verifies brainstorm-to-PRD traceability
FR-14: Foundry derives repo name, prompts user, creates GitHub repo
FR-15: Foundry commits scaffold: README, LICENSE, .gitignore, ci.yml, foundry.lock
FR-16: Foundry creates feature branch per epic
FR-17: Foundry passes story text + codebase to new Claude Code session
FR-18: Foundry runs test runner command from foundry.lock after each story
FR-19: Foundry commits story changes with structured forge-log message on green
FR-20: Foundry opens PR per epic, merges when CI passes
FR-21: Code failure: auto-retry once with test output fed back to new session
FR-22: Infrastructure failure: exponential backoff, never passed to LLM
FR-23: Persistent failure: escalate to human with full context in $EDITOR
FR-24: Environment failure: stop immediately with clear error message
FR-25: foundry.lock is TOML with sections per phase, atomic writes
FR-26: foundry.lock records exact model ID, checks on resume
FR-27: Every artifact is SHA-256 hashed and recorded in foundry.lock
FR-28: Every checkpoint records editor open/close timestamps
FR-29: All commits follow `foundry: <action> — <detail>` format
FR-30: Foundry generates Dockerfile with health check and port declaration
FR-31: Web UI checkpoint via local HTTP server (SHOULD scope — deferred)
FR-32: `foundry resume` determines last completed story from lock + git
FR-33: On resume, check model availability and prompt if unavailable
FR-34: Consistency pass normalizes style when model changes mid-run
FR-35: Quick mode: LLM generates one-page spec as first checkpoint
FR-36: Quick mode: incremental logical commits with tests required
FR-37: Foundry generates minimal .github/workflows/ci.yml in scaffold

### NonFunctional Requirements

NFR-1: CLI startup under 2 seconds
NFR-2: foundry.lock writes are atomic (temp + rename)
NFR-3: Terminal streaming with no perceptible buffering delay
NFR-4: Crash-safe at every pipeline point; resume produces no duplicates
NFR-5: foundry.lock is valid TOML at all times
NFR-6: GitHub tokens from env vars or credential store, never committed
NFR-7: foundry.lock contains no LLM response content, keys, or tokens
NFR-8: Every action produces both a forge-log commit AND a lock entry
NFR-9: `foundry status` accurate within one story of actual state

### Additional Requirements

- AD-1: Pipes-and-filters Phase protocol for all pipeline phases
- AD-2: ProvenanceRecorder dual-sink pattern (lock + git) for all mutations
- AD-3: FailureClassifier wraps all external calls, classifies before bubbling
- AD-4: CheckpointHandler protocol with swappable $EDITOR and web UI implementations
- AD-5: Append-only foundry.lock structure (never edit, only add sections)
- AD-6: aiosqlite ephemeral WAL with crash recovery procedure
- AD-7: Claude Code prompt file at .foundry/current-story.md, session-per-story isolation
- AD-8: GitOps adapter as sole owner of all git/GitHub mutations
- AD-9: Planning runs in local temp workspace; repo creation is one-time transition
- AD-10: Pre-flight analysis is a pipeline phase, not a side-check

### UX Design Requirements

N/A — CLI tool with $EDITOR-based checkpoints. Web UI is SHOULD scope, deferred.

### FR Coverage Map

| FR | Epic | Description |
|---|---|---|
| FR-1 | 1 | Pro mode CLI entry point |
| FR-2 | 7 | Quick mode CLI entry point |
| FR-3 | 5 | Resume CLI entry point |
| FR-4 | 5 | Status CLI entry point |
| FR-5 | 1 | Cancel CLI entry point |
| FR-6 | 2 | --review-epics flag |
| FR-7 | 2 | Brainstorm phase |
| FR-8 | 2 | PRD phase |
| FR-9 | 2 | Architecture phase |
| FR-10 | 2 | Epics/stories phase |
| FR-11 | 2 | Contradiction detection |
| FR-12 | 2 | Completeness check |
| FR-13 | 2 | Traceability validation |
| FR-14 | 3 | Repository creation |
| FR-15 | 3 | Scaffold commit |
| FR-16 | 3 | Feature branch management |
| FR-17 | 3 | Story implementation |
| FR-18 | 3 | Test execution |
| FR-19 | 3 | Story commit |
| FR-20 | 3 | PR and merge |
| FR-21 | 4 | Code failure handling |
| FR-22 | 4 | Infra failure handling |
| FR-23 | 4 | Escalate-to-human |
| FR-24 | 4 | Environment failure handling |
| FR-25 | 1 | Lock file structure |
| FR-26 | 1 | Model version pinning |
| FR-27 | 1 | Artifact hashing |
| FR-28 | 1 | Checkpoint timestamps |
| FR-29 | 1 | Commit message format |
| FR-30 | 6 | Dockerfile generation |
| FR-31 | — | Web UI (SHOULD, deferred) |
| FR-32 | 5 | Resume from git state |
| FR-33 | 5 | Model continuity check |
| FR-34 | 5 | Consistency pass |
| FR-35 | 7 | Self-written spec |
| FR-36 | 7 | Incremental commits |
| FR-37 | 3 | CI workflow generation |

## Epic List

### Epic 1: Core Pipeline Infrastructure
The developer can run `foundry pro "idea"` and have the orchestrator sequence through phases with checkpoint pauses and provenance recording.
**FRs covered:** FR-1, FR-5, FR-25, FR-26, FR-27, FR-28, FR-29

---

## Epic 1 Stories

### Story 1.1: CLI Entry Point and Phase Protocol

As a developer,
I want to run `foundry pro "my idea"` and see the orchestrator initialize and sequence through placeholder phases,
So that the pipeline skeleton exists for real phases to plug into.

**Acceptance Criteria:**

**Given** Python 3.11+ is installed and foundry is on PATH
**When** I run `foundry pro "build a todo app"`
**Then** the CLI parses the idea string, creates a `RunContext` with a unique run ID, and invokes phases in sequence
**And** a `Phase` protocol is defined with `async def run(self, input: Artifact, ctx: RunContext) -> Artifact`
**And** at least one stub phase executes and returns an artifact
**And** CLI startup completes in under 2 seconds (NFR-1)
**And** `foundry --help` shows `pro`, `quick`, `resume`, `status`, and `cancel` subcommands (only `pro` functional)

### Story 1.2: foundry.lock Initialization and Atomic Writes

As a developer,
I want the lock file created at run start with the run metadata section,
So that every run is recorded and crash-safe from the first moment.

**Acceptance Criteria:**

**Given** a `foundry pro` run has started with a RunContext
**When** the orchestrator initializes
**Then** a `foundry.lock` file is created at the workspace root with a `[run]` section containing `idea_hash` (SHA-256 of the idea string), `started` (ISO 8601 UTC), and `model` (exact model ID per FR-26)
**And** the lock file is valid TOML at all times (NFR-5)
**And** writes use atomic temp-file + rename (NFR-2)
**And** the `[run]` section is never modified after creation (AD-5 append-only)

### Story 1.3: Artifact Hashing and Provenance Recording

As a developer,
I want every phase artifact SHA-256 hashed and recorded in both foundry.lock and a git commit,
So that I have tamper-evident provenance of every decision.

**Acceptance Criteria:**

**Given** a phase completes and produces an artifact
**When** the ProvenanceRecorder processes the artifact
**Then** a SHA-256 hash prefixed with `sha256:` is computed over the artifact content (FR-27)
**And** a new section is appended to `foundry.lock` with the artifact hash and timestamp
**And** a git commit is created with message format `foundry: <action> — <detail>` (FR-29)
**And** both the lock entry and commit are created for every action (NFR-8 dual-sink)
**And** the lock file remains valid TOML after the append

### Story 1.4: Checkpoint Handler with $EDITOR

As a developer,
I want the pipeline to pause at checkpoints, open my $EDITOR with the artifact, and wait for my review,
So that I maintain human-in-the-loop control over every planning decision.

**Acceptance Criteria:**

**Given** a phase completes and the orchestrator reaches a checkpoint
**When** the CheckpointHandler is invoked
**Then** the artifact is written to a temp file and opened in `$EDITOR` (falling back to `vi` on Unix, `notepad` on Windows)
**And** the handler blocks until the editor process exits
**And** the (possibly edited) artifact is read back and passed to the next phase
**And** `foundry.lock` records `editor_open` and `editor_close` timestamps in the checkpoint section (FR-28)
**And** the CheckpointHandler is a protocol with swappable implementations (AD-4)

### Story 1.5: Cancel Command

As a developer,
I want to run `foundry cancel` to cleanly stop an in-progress run,
So that I can abort without leaving corrupted state.

**Acceptance Criteria:**

**Given** a `foundry pro` run is in progress
**When** I run `foundry cancel` in another terminal
**Then** the running orchestrator receives a stop signal and exits after completing the current atomic operation
**And** a `foundry: cancelled` commit is created (FR-29)
**And** a cancellation entry is appended to `foundry.lock`
**And** the lock file and git history are consistent (no partial writes)

### Story 1.6: Model Version Pinning

As a developer,
I want the exact Claude model ID recorded in foundry.lock at run start,
So that resume can detect model changes and the full run is reproducible.

**Acceptance Criteria:**

**Given** a `foundry pro` run starts
**When** the orchestrator queries the Claude API for the resolved model ID
**Then** the exact model identifier (e.g., `claude-sonnet-5-5-20261001`) is written to `[run].model` in foundry.lock (FR-26)
**And** the model ID is a concrete version, not an alias like `claude-sonnet-5-5`
**And** this value is available to the resume flow for comparison (consumed by Epic 5)

---

### Epic 2: Planning Pipeline
The developer can brainstorm, generate a PRD, run pre-flight analysis, design architecture, and generate epics/stories — each with human review.
**FRs covered:** FR-6, FR-7, FR-8, FR-9, FR-10, FR-11, FR-12, FR-13

---

## Epic 2 Stories

### Story 2.1: Brainstorm Phase

As a developer,
I want Foundry to invoke the BMad brainstorm agent with my idea and stream output to my terminal,
So that I get a creative exploration of my idea before committing to a PRD.

**Acceptance Criteria:**

**Given** the orchestrator has initialized a run with an idea string
**When** the brainstorm phase executes
**Then** the BMad brainstorm agent is invoked with the idea as input
**And** agent output streams to the terminal with no perceptible buffering delay (NFR-3)
**And** when the agent completes, the brainstorm artifact is passed to the checkpoint handler
**And** after checkpoint approval, provenance is recorded (lock + commit)
**And** the phase runs in a local workspace, no GitHub repo exists yet (AD-9)

### Story 2.2: PRD Phase

As a developer,
I want Foundry to generate a PRD from the approved brainstorm and let me review it,
So that my project has a rigorous requirements document before architecture begins.

**Acceptance Criteria:**

**Given** the brainstorm phase completed and the artifact was approved at checkpoint
**When** the PRD phase executes
**Then** the BMad PRD agent is invoked with the brainstorm artifact as input (FR-8)
**And** agent output streams to the terminal
**And** the PRD artifact is presented at a checkpoint for human review
**And** the user can edit the PRD in their editor before approving
**And** provenance is recorded after approval

### Story 2.3: Pre-flight Analysis Phase

As a developer,
I want Foundry to analyze the approved PRD for contradictions, omissions, and traceability gaps,
So that architectural work builds on a sound foundation.

**Acceptance Criteria:**

**Given** the PRD phase completed and the artifact was approved
**When** the pre-flight phase executes (AD-10: this is a pipeline phase, not a side-check)
**Then** the PRD is analyzed for internal contradictions between requirements (FR-11)
**And** the PRD is checked for common omissions: auth, error handling, persistence, logging (FR-12)
**And** brainstorm-to-PRD traceability is verified — flagging brainstorm themes not reflected in any FR (FR-13)
**And** findings are presented at a checkpoint with a summary report
**And** if contradictions or critical omissions are found, the user can go back to edit the PRD
**And** provenance records the pre-flight results including contradiction count and resolutions

### Story 2.4: Architecture Phase

As a developer,
I want Foundry to generate an architecture document from the PRD and extract the test runner command,
So that the implementation loop has both structural guidance and a verification gate.

**Acceptance Criteria:**

**Given** the pre-flight phase completed with no unresolved critical issues
**When** the architecture phase executes
**Then** the BMad architecture agent is invoked with the PRD artifact (FR-9)
**And** the architecture artifact is presented at a checkpoint for human review
**And** the test runner command is extracted from the architecture and stored in `foundry.lock` as `[run].test_runner` (FR-9)
**And** provenance is recorded after approval

### Story 2.5: Epics and Stories Phase

As a developer,
I want Foundry to generate epics and stories from the architecture document,
So that the implementation loop has a concrete work plan to execute.

**Acceptance Criteria:**

**Given** the architecture phase completed and was approved
**When** the epics/stories phase executes
**Then** the BMad epics/stories agent is invoked with the architecture artifact (FR-10)
**And** the generated epics and stories are presented at a checkpoint
**And** the user can review and edit epic/story definitions before approving
**And** provenance is recorded after approval
**And** the planning pipeline is complete — the next phase is repo creation (Epic 3)

### Story 2.6: Review-Epics Flag

As a developer,
I want to pass `--review-epics` so I get a checkpoint before each epic starts implementation,
So that I can adjust priorities between epics during a long run.

**Acceptance Criteria:**

**Given** the user started with `foundry pro "idea" --review-epics` (FR-6)
**When** the implementation loop (Epic 3) is about to begin a new epic
**Then** a checkpoint is presented showing the epic's stories and current project state
**And** the user can reorder, skip, or edit stories before the epic proceeds
**And** if `--review-epics` was not passed, epic transitions happen without pausing

---

### Epic 3: Repository Creation and Implementation Loop
The developer's planning artifacts are committed to a new GitHub repo, and each story is implemented via Claude Code with tests required before commit.
**FRs covered:** FR-14, FR-15, FR-16, FR-17, FR-18, FR-19, FR-20, FR-37

---

## Epic 3 Stories

### Story 3.1: Repository Creation and Scaffold

As a developer,
I want Foundry to derive a repo name from my project, confirm it with me, and create a GitHub repo with a complete scaffold,
So that implementation has a proper home with CI from the start.

**Acceptance Criteria:**

**Given** the planning pipeline is complete with all artifacts approved
**When** the repo creation phase executes
**Then** a repo name is derived from the project name and presented for confirmation (FR-14)
**And** a GitHub repository is created via the GitHub API using credentials from env vars or credential store (NFR-6)
**And** an initial commit includes: README.md, LICENSE, .gitignore, `.github/workflows/ci.yml`, and `foundry.lock` (FR-15)
**And** the CI workflow runs only the test command from `foundry.lock` on PRs to main (FR-37)
**And** all git/GitHub operations go through the GitOps adapter (AD-8)
**And** a `foundry: scaffold` commit is created

### Story 3.2: Feature Branch per Epic

As a developer,
I want Foundry to create a feature branch for each epic before implementing its stories,
So that each epic's work is isolated and reviewable as a unit.

**Acceptance Criteria:**

**Given** the repository is scaffolded and the implementation loop begins
**When** an epic is about to start
**Then** a feature branch is created from `main` named `epic-{N}-{slug}` (FR-16)
**And** the branch is pushed to the remote
**And** all story commits for this epic land on this branch
**And** the GitOps adapter owns all branch operations (AD-8)

### Story 3.3: Story Implementation via Claude Code

As a developer,
I want each story implemented by a fresh Claude Code session that receives the story text and full codebase context,
So that each story gets focused, isolated implementation.

**Acceptance Criteria:**

**Given** a feature branch exists for the current epic
**When** a story is ready for implementation
**Then** a prompt file is written to `.foundry/current-story.md` containing the story text, acceptance criteria, and architecture document inlined (AD-7)
**And** a new Claude Code subprocess is invoked with `cwd` set to the repo root (FR-17)
**And** each story gets its own subprocess — no session reuse (AD-7)
**And** the subprocess output streams to the terminal

### Story 3.4: Test Execution Gate

As a developer,
I want Foundry to run the test command after each story implementation,
So that no story is committed unless its tests pass.

**Acceptance Criteria:**

**Given** a Claude Code session has completed a story implementation
**When** the test gate runs
**Then** the test runner command from `foundry.lock` is executed (FR-18)
**And** if tests pass (exit code 0), the story proceeds to commit
**And** if tests fail, the story is handed to the failure handler (Epic 4)
**And** test output is captured for potential retry context

### Story 3.5: Story Commit with Forge Log

As a developer,
I want passing story changes committed with a structured forge-log message,
So that every implementation step is traceable in git history.

**Acceptance Criteria:**

**Given** a story's tests have passed
**When** the commit step executes
**Then** all changes are committed with message `foundry: story {E}.{S} implemented — {story title}` (FR-19, FR-29)
**And** the corresponding `[implementation.epic-{E}.story-{S}]` section is appended to `foundry.lock` with `artifact_hash`, `tests_passed` count, `duration_seconds`, and `retries` count
**And** both lock entry and commit are created (NFR-8)

### Story 3.6: PR Creation and Merge

As a developer,
I want Foundry to open a PR for each completed epic and merge it when CI passes,
So that approved work flows to main automatically.

**Acceptance Criteria:**

**Given** all stories in an epic are committed on the feature branch
**When** the epic completes
**Then** a pull request is opened from the feature branch to `main` (FR-20)
**And** Foundry waits for CI checks to pass on the PR
**And** when CI is green, the PR is merged automatically (FR-20)
**And** if CI fails, the failure is escalated to the human (Epic 4 handles this)
**And** a `foundry: epic {N} merged` commit message appears after merge

---

### Epic 4: Failure Handling and Human Escalation
When implementation fails, the developer experiences intelligent error handling with auto-retry, backoff, escalation, and clean halts per failure category.
**FRs covered:** FR-21, FR-22, FR-23, FR-24

---

## Epic 4 Stories

### Story 4.1: Failure Classifier

As a developer,
I want every external call wrapped by a classifier that categorizes failures before they propagate,
So that the right recovery strategy is applied automatically.

**Acceptance Criteria:**

**Given** any external call (Claude API, GitHub API, subprocess) fails
**When** the FailureClassifier intercepts the error
**Then** it is classified as one of: `code` (test failures, LLM output issues), `infrastructure` (API rate limits, network errors, timeouts), or `environment` (missing credentials, disk full, invalid config)
**And** the classification is attached to the error before it reaches the orchestrator (AD-3)
**And** unclassifiable errors default to `environment` (safest: hard stop)

### Story 4.2: Code Failure — Auto-Retry

As a developer,
I want code failures auto-retried once with test output fed to a fresh Claude Code session,
So that transient LLM mistakes get a second chance without my intervention.

**Acceptance Criteria:**

**Given** a story implementation fails with classification `code` (tests fail)
**When** the retry handler activates
**Then** a new Claude Code session is spawned with the original story prompt plus the failing test output (FR-21)
**And** the retry is limited to exactly one attempt — no infinite loops
**And** if the retry passes, the story is committed with `foundry: story {E}.{S} auto-fixed` message
**And** `foundry.lock` records the failure in `[failures.epic-{E}.story-{S}]` with `retry_outcome = "passed"`
**And** if the retry also fails, the failure escalates to human (Story 4.4)

### Story 4.3: Infrastructure Failure — Exponential Backoff

As a developer,
I want infrastructure failures retried with exponential backoff,
So that transient API issues resolve without human intervention.

**Acceptance Criteria:**

**Given** an external call fails with classification `infrastructure`
**When** the backoff handler activates
**Then** retries use exponential backoff with jitter (FR-22)
**And** the error is never passed to the LLM — only the orchestrator handles it
**And** after a configurable max retries (default 5), the failure escalates to human
**And** the orchestrator state remains consistent during the wait (no partial writes)

### Story 4.4: Human Escalation

As a developer,
I want persistent failures escalated to me with full context in my $EDITOR,
So that I can make an informed decision about how to proceed.

**Acceptance Criteria:**

**Given** a code failure persists after auto-retry, or infrastructure retries are exhausted
**When** escalation triggers
**Then** a context file is opened in `$EDITOR` containing: the story text, the error output, the failing test output, and the current codebase diff (FR-23)
**And** the user can choose to: edit the story and retry, edit the code and continue, skip the story (defer), or cancel the run
**And** the chosen action is recorded in `foundry.lock` under `[interventions.epic-{E}.story-{S}]`
**And** a `foundry: story {E}.{S} — human intervention` commit is created if code was edited

### Story 4.5: Environment Failure — Hard Stop

As a developer,
I want environment failures to stop the run immediately with a clear error message,
So that I don't waste time on unrecoverable problems.

**Acceptance Criteria:**

**Given** a failure is classified as `environment` (missing credentials, disk full, invalid config)
**When** the environment handler activates
**Then** the run stops immediately with a clear, actionable error message (FR-24)
**And** no retry is attempted
**And** the current state is saved to `foundry.lock` so `foundry resume` can pick up after the issue is fixed
**And** the error message includes what to fix (e.g., "Set GITHUB_TOKEN environment variable")

---

### Epic 5: Resume and Recovery
The developer can crash mid-run, reboot, run `foundry resume`, and pick up exactly where they left off.
**FRs covered:** FR-3, FR-4, FR-32, FR-33, FR-34

---

## Epic 5 Stories

### Story 5.1: Resume — Determine Last Completed Story

As a developer,
I want `foundry resume` to figure out exactly where my run stopped by inspecting the lock file and git history,
So that I pick up without duplicating or skipping work.

**Acceptance Criteria:**

**Given** a previous run was interrupted (crash, cancel, or environment failure)
**When** I run `foundry resume` (FR-3)
**Then** the lock file and git log are inspected to determine the last completed story (FR-32)
**And** crash recovery checks git status: dirty+green → commit and advance; dirty+red → discard and re-run; clean → re-run next story (AD-6)
**And** at worst, one story is re-run (never zero-loss, never two-loss)
**And** the aiosqlite WAL is rebuilt from `foundry.lock` + git state if missing (AD-6)
**And** a `foundry: resumed` commit is created
**And** no duplicate commits are produced (NFR-4)

### Story 5.2: Status Command

As a developer,
I want to run `foundry status` to see the current state of my run,
So that I know what's been done and what's next without reading raw files.

**Acceptance Criteria:**

**Given** a run exists (in-progress or interrupted)
**When** I run `foundry status` (FR-4)
**Then** a summary is displayed: current phase/epic/story, completed stories count, total stories, last action timestamp
**And** the status is accurate within one story of actual state (NFR-9)
**And** if no run exists, a clear message is shown
**And** the command works whether or not the run is currently active

### Story 5.3: Model Continuity Check on Resume

As a developer,
I want `foundry resume` to check if the original model is still available and prompt me if it's not,
So that I'm aware of potential style drift before continuing.

**Acceptance Criteria:**

**Given** a run is being resumed
**When** the orchestrator checks the model ID from `foundry.lock` against the API (FR-33)
**Then** if the model is available, the run continues silently
**And** if the model is unavailable, the user is prompted with the available alternatives and asked to choose
**And** the new model ID is recorded in `foundry.lock` as a new section (append-only, doesn't overwrite `[run].model`)
**And** the model change triggers a consistency pass flag for after the current epic completes

### Story 5.4: Consistency Pass on Model Change

As a developer,
I want a style normalization pass when the model changes mid-run,
So that the final codebase doesn't have jarring style shifts between sections.

**Acceptance Criteria:**

**Given** a model change was recorded during resume (Story 5.3 set the flag)
**When** the current epic completes and before the next epic begins
**Then** a dedicated Claude Code session runs a consistency pass over the codebase (FR-34)
**And** the pass focuses on naming conventions, formatting, and comment style — not logic changes
**And** changes are committed with `foundry: consistency pass` message
**And** the pass is skipped if no model change occurred

---

### Epic 6: Dockerfile and Finalization
After all epics are merged, the developer gets a production-ready Dockerfile committed to main.
**FRs covered:** FR-30

---

## Epic 6 Stories

### Story 6.1: Dockerfile Generation

As a developer,
I want Foundry to generate a production-ready Dockerfile after all epics are merged,
So that my project is containerized and ready for any deployment platform.

**Acceptance Criteria:**

**Given** all epic PRs have been merged to main
**When** the finalization phase executes
**Then** a Dockerfile is generated that builds and runs the project (FR-30)
**And** the Dockerfile includes a `HEALTHCHECK` instruction
**And** the Dockerfile includes an `EXPOSE` declaration for the application port
**And** the Dockerfile uses a multi-stage build if the project has a build step
**And** the Dockerfile is committed with message `foundry: dockerfile generated`
**And** provenance is recorded in `foundry.lock`
**And** the run is marked complete in `foundry.lock`

---

### Epic 7: Quick Mode
The developer can run `foundry quick "idea"` for spec-first implementation without the full planning pipeline.
**FRs covered:** FR-2, FR-35, FR-36

---

## Epic 7 Stories

### Story 7.1: Quick Mode Entry Point and Spec Generation

As a developer,
I want to run `foundry quick "my idea"` and get a one-page spec generated as my first checkpoint,
So that even a fast run has structure before code.

**Acceptance Criteria:**

**Given** Python 3.11+ is installed and foundry is on PATH
**When** I run `foundry quick "build a todo app"` (FR-2)
**Then** the CLI creates a RunContext in quick mode (no BMad planning phases)
**And** a Claude Code session generates a one-page spec covering: feature list, data model sketch, and test strategy (FR-35)
**And** the spec is presented at a checkpoint for human review
**And** the user can edit the spec before approving
**And** after approval, a GitHub repo is created and scaffolded (reuses Epic 3's GitOps adapter)
**And** `foundry.lock` records the run as `mode = "quick"`

### Story 7.2: Quick Mode Implementation with Incremental Commits

As a developer,
I want quick mode to implement the spec with incremental logical commits and tests required,
So that the output is structured and verified, not a single blob of code.

**Acceptance Criteria:**

**Given** a quick mode spec has been approved and the repo scaffolded
**When** the implementation phase executes
**Then** a Claude Code session implements the spec in logical increments (FR-36)
**And** each logical increment is committed separately with a descriptive message
**And** tests are required before each commit — no commit lands without green tests (FR-36)
**And** commits follow the `foundry: story Q.{N} implemented` format for traceability
**And** on completion, a Dockerfile is generated (reuses Epic 6's finalization)
**And** provenance is recorded for each increment
