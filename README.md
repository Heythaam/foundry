# foundry

Foundry takes an idea through a human-checkpointed pipeline and records every step as
tamper-evident provenance in two places at once: `foundry.lock` and git history.

> **Status:** Epics 1-6 (core infrastructure, the planning pipeline, repo creation and the
> implementation loop, failure handling, resume and recovery, the Dockerfile). `foundry pro` plans
> (brainstorm -> PRD -> pre-flight -> architecture -> epics/stories, each with a human checkpoint),
> creates a GitHub repo, builds every story in its own Claude Code session, merges one PR per epic,
> then commits a Dockerfile to `main` and marks the run complete. `foundry cancel`, `resume` and
> `status` work; `quick` is a stub. Failed stories are retried once, flaky API calls are backed
> off, persistent failures are escalated to you in `$EDITOR`, and environment failures stop the
> run with what to fix.

## Install

Requires Python 3.11+ and git.

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; use `source .venv/bin/activate` elsewhere
pip install -e ".[dev]"
```

## Use

```bash
foundry --help
foundry pro "build a todo app"                  # run the planning pipeline in the current directory
foundry pro "build a todo app" --review-epics   # also pause before each epic starts
foundry pro "build a todo app" --private        # create the GitHub repo as private (default: public)
foundry cancel                                  # from another terminal: stop after the current phase
```

Each run is rooted in the current directory, which must not already contain a finished run's
`foundry.lock`.

Agents run as `claude -p` subprocesses (Claude Code must be on PATH and signed in). Each gets
its prompt in `.foundry/current-story.md`, is restricted to the `Skill` and `Read` tools, and
streams its output to your terminal as it is produced. The BMad skills it is told to use
(`bmad-brainstorming`, `bmad-prd`, `bmad-architecture`, `bmad-create-epics-and-stories`) must be
installed in your Claude Code.

### Approved artifacts on disk

Once you approve a checkpoint, the approved text is written to `.foundry/artifacts/`:
`brainstorm.md`, `prd.md`, `architecture.md` and `epics.md`. These are what scaffolding and
`foundry resume` read back; the lock keeps only their hashes. Each file holds the latest
approved version (a PRD reworked after pre-flight replaces the earlier one), and the folder is
git-ignored.

### Repository and implementation (Epic 3)

After the epics are approved Foundry leaves the local workspace (AD-9):

1. **Repo.** The name is derived from the PRD title (slugified) and you confirm or change it
   before anything is created. The repository is public unless you pass `--private`; the prompt
   says which. The initial commit, `foundry: scaffold`, holds `README.md`, an MIT `LICENSE`, a
   `.gitignore`, `.github/workflows/ci.yml`, `foundry.lock` and the approved planning documents
   under `docs/planning/`. `main` is pushed.
2. **Per epic.** A branch `epic-{N}-{slug}` is cut from `main` and pushed before any story starts.
   With `--review-epics` you review the epic first.
3. **Per story.** A fresh `claude -p` session gets `.foundry/current-story.md` (story text,
   acceptance criteria and the whole architecture inlined; deleted when the session ends), then
   the `[run.test_runner]` command runs from the repo root. On green the story is committed as
   `foundry: story {E}.{S} implemented — {title}` together with its
   `[implementation.epic-E.story-S]` lock entry (diff hash, tests passed, duration, retries).
   A story that changes nothing counts as a failure.
4. **Merge.** When an epic's stories are in, Foundry opens a PR to `main`, waits for CI and merges
   it when green, then records `foundry: epic {N} merged — PR #n` on `main` and pushes it. Red
   CI is escalated to you (below) and the PR stays open until it is green.

Set `GITHUB_TOKEN` (a token with `repo` and `workflow` scope) before running `foundry pro`; it is
checked first, before any planning work, and is the only place the token is read from. It is
never written to `foundry.lock` or `.git/config` and is scrubbed from error messages.
`GITHUB_API_URL` overrides the API endpoint (GitHub Enterprise). Implementation sessions get the
`Read`, `Write`, `Edit`, `MultiEdit`, `Glob`, `Grep`, `Bash` and `Skill` tools.

The CI workflow runs on pull requests to `main` and runs exactly one command, the one in
`[run.test_runner]`, after checking out and setting up the toolchain it detects. CI starts from a
clean checkout, so that command must install the project's dependencies too (for example
`pip install -e '.[dev]' && pytest -q`); the architecture prompt asks for this.

### Failure handling (Epic 4)

Every external call (Claude Code, GitHub, git, the test command, `$EDITOR`) fails as one of three
categories, and each is handled differently:

| Category | Examples | What Foundry does |
| --- | --- | --- |
| `code` | tests fail, the agent returns nothing | One automatic retry in a **new** session whose prompt is the original plus the failing test output. If it passes the story is committed as `foundry: story E.S auto-fixed — title`; if not, you are asked. |
| `infrastructure` | rate limits, 5xx, timeouts, network errors | Exponential backoff with jitter (about 2s, 4s, 8s ... up to 2 minutes), up to 5 retries (`FOUNDRY_MAX_RETRIES`). The error never reaches the LLM and nothing is written while waiting. Then you are asked. |
| `environment` | missing `GITHUB_TOKEN`, no `claude`, full disk, unknown errors | No retry. The run stops with the error and what to fix, and a `[halts.halt-N]` entry is saved so the run can be resumed once it is fixed. |

When you are asked, a file opens in `$EDITOR` with the story, the error, the failing test output
and the current diff. Write one word after `action:` at the bottom, save and close:

- `retry`: a brand-new session on the story as written in the file (edit the story first to
  change what it is asked to build);
- `continue`: you fixed the code yourself in the working tree: the tests run and, if green, it is
  committed as `foundry: story E.S — human intervention`;
- `skip`: the story is deferred, its changes are kept in `git stash`, and the epic carries on
  (the pull request lists what was deferred);
- `cancel`: the run ends as cancelled (an empty `action:` also cancels). The working tree is left
  as it is.

Red CI and exhausted retries on epic-level calls (push, open PR, merge) offer `retry` and
`cancel`; for CI, `retry` re-reads the PR (so push your fix first) and checks CI again.
Failures and your choices are recorded as `[failures.epic-E.story-S]` (category, retries, outcome,
a hash of the output) and `[interventions.epic-E.story-S]` (actions, whether you edited the story
or code); skipped stories get `[deferred.epic-E.story-S]`.

### Resume, status and model changes (Epic 5)

`foundry resume` continues an interrupted run (crash, `foundry cancel`, or an environment failure)
from the last completed story. It works from what is recorded, never from memory:

1. It reads `foundry.lock` and git, and prints the resume point (`story 1.2 (1 of 3 done)`) and
   the pinned model. Planning cannot be resumed: a run that stopped before the epics were approved
   has to be started again.
2. A lock entry that was written but whose commit was lost to a crash is committed, once, with
   the message the lost commit would have had. Missing planning documents in `.foundry/artifacts`
   are restored from `docs/planning/` in git, and `.foundry/state.db` is rebuilt from the lock.
3. A story that was in flight is settled by its working tree. Clean: it starts again. Changes that
   pass the tests: committed as that story (`recovered_on_resume = true`) and the loop moves on.
   Changes that fail: set aside in `git stash` and the story runs again. At worst one story is
   re-run, and no story is committed twice. A merged pull request, a branch or a repository that
   was created but never recorded are found again rather than created a second time.
4. It records `[resumes.resume-N]` and commits `foundry: resumed — <resume point>`.

`foundry status` prints the phase, the epic and story that are next, completed / total stories,
merged epics, the model, elapsed time and the time of the last recorded action. It reads only the
recorded state, so it is accurate to within the one story in flight and safe to run while another
terminal is working on the run.

If `ANTHROPIC_API_KEY` is set, resume asks the Models API whether the pinned model still exists.
If it does not, you pick a replacement from the list (or type an ID); it is recorded as
`[model_changes.change-N]` (`[run].model` is never edited) and the rest of the run uses it. The
change schedules a **consistency pass**: when the current epic has merged and before the next one
starts, one dedicated Claude Code session normalises naming, formatting and comment style (no
logic changes). It is committed on `main` as `foundry: consistency pass — model boundary at story
N/M` with a `[consistency.pass-N]` entry. If the tests fail afterwards, its changes are stashed and
the pass is recorded as reverted. Without an API key (or if the API cannot be reached) the check
is skipped with a notice and the run continues on the pinned model.

### Finalization: the Dockerfile (Epic 6)

When the last epic has merged, one more Claude Code session writes a production-ready `Dockerfile`
at the root of the repository, for the stack the architecture document names. Foundry checks the
result itself rather than taking the session's word for it:

- the **final stage** has a `FROM`, an `EXPOSE` with a port number and a `HEALTHCHECK ... CMD ...`
  (an `EXPOSE` or `HEALTHCHECK` in an earlier stage does not carry over, and `HEALTHCHECK NONE`
  does not count);
- it is **multi-stage exactly when the project has a build step** (a `package.json` with a `build`
  script, `Cargo.toml`, `go.mod`, a JVM or .NET project, `tsconfig.json`, a Makefile `build`
  target, or a build command named in the architecture), and a single stage otherwise;
- no platform-specific files appear (`docker-compose.yml`, `fly.toml`, `render.yaml`, `Procfile`,
  Kubernetes or Helm directories) and `foundry.lock` was not touched;
- the project's test command still passes (the session may have had to add a `/health` endpoint).

A Dockerfile that fails any of these goes back to the session with the list of problems, up to
three attempts in all. If it never passes, its files are put in `git stash`, the run stays in
progress, and `foundry resume` tries again. A Dockerfile that passes is committed to `main` as
`foundry: dockerfile generated — <base image>, <port>` together with its `[dockerfile]` entry
(base images, stages, port, health check, a hash of the file, attempts, the files committed) and
pushed. The run is then marked complete: `[completed]` is appended after `[dockerfile]` and is the
last section in the lock. Foundry does not run `docker build`; Docker is not required.

### Pre-flight

After the PRD, pre-flight reports contradictions, missing topics (authentication, error
handling, persistence, logging) and brainstorm themes no requirement covers. The last line of
the report is `Decision: revise-prd` or `Decision: proceed`. It defaults to `revise-prd` while
anything blocking is found; `revise-prd` reopens the PRD in your editor and re-runs the analysis.

### Architecture

The architecture must contain a `## Test Runner` section with one fenced command. It is
recorded in the lock as `[run.test_runner]`.

| Environment variable | Meaning |
| --- | --- |
| `GITHUB_TOKEN` | Required for `foundry pro`. The only source of GitHub credentials. |
| `GITHUB_API_URL` | Optional API base URL. Default `https://api.github.com`. |
| `ANTHROPIC_API_KEY` | Optional. Lets `foundry resume` check that the pinned model is still available. `ANTHROPIC_BASE_URL` overrides the API endpoint. |
| `FOUNDRY_MAX_RETRIES` | Optional. How many times an infrastructure failure is retried before you are asked. Default `5`; `0` turns backoff off. |
| `FOUNDRY_MODEL` | Pinned model ID recorded in `[run].model`. Default `claude-sonnet-4-5`. An ID whose last `-` segment has no digit (e.g. `claude-sonnet`) is rejected as an alias. |
| `EDITOR` | Command used for checkpoints. Falls back to `vi` (Unix) or `notepad` (Windows). It must block until the file is closed (`code --wait`, not `code`). |

## How it fits together

| Module | Role |
| --- | --- |
| `context.py` | `RunContext`, `Artifact` (auto SHA-256), `ArtifactType`, error taxonomy |
| `phases.py` | `Phase` protocol: `async def run(self, input, ctx) -> Artifact` |
| `orchestrator.py` | Sequences phases, runs checkpoints and pre-flight revisions, polls for cancel between phases |
| `agent.py` | `ClaudeCodeRunner`: runs a BMad agent as a streaming Claude Code subprocess |
| `planning.py` | Brainstorm, PRD, architecture and epics phases; test-runner extraction |
| `preflight.py` | Pre-flight analysis phase and report |
| `epics.py` | Parses epics/stories Markdown |
| `review.py` | `--review-epics` gate: `EpicReviewer.before_epic()` |
| `pipeline.py` | The ordered planning pipeline |
| `artifacts.py` | Atomic writes of approved artifacts to `.foundry/artifacts/<phase>.md` |
| `gitops.py` | `GitOps`: the only module that runs git or calls GitHub (AD-8). Domain methods: commit, branch, push, create repo/PR, poll CI, merge |
| `scaffold.py` | Repo name, README, LICENSE, `.gitignore` and CI workflow content (pure functions) |
| `repo.py` | `RepoStage`: confirm name, create repo, scaffold commit, push |
| `implementation.py` | `ImplementationStage`: epic branches, story sessions, test gate, commits, PRs, merge |
| `testgate.py` | Runs the recorded test command, captures its output |
| `progress.py` | Where a run stands, computed from the lock alone (shared by resume and status) |
| `resume.py` | `ResumeCoordinator`: validates, repairs crash leftovers, rebuilds the state buffer, checks the model, records `foundry: resumed` |
| `status.py` | `foundry status` output |
| `dockerfile.py` | `DockerfileStage`: build-step detection, the Dockerfile session, its checks, the commit |
| `models.py` | Model availability check (Models API) and the replacement-model prompt |
| `failures.py` | Failure policy: classifier, backoff, one-shot code retry, `$EDITOR` escalation, fix hints |
| `stages.py` | `Stage` protocol for the steps after planning |
| `lock.py` | `foundry.lock`: append-only TOML, atomic temp-file + rename writes |
| `provenance.py` | `ProvenanceRecorder`: every action writes a lock section **and** a git commit |
| `state.py` | `.foundry/state.db`: aiosqlite WAL journal of in-flight actions (never committed) |
| `checkpoint.py` | `CheckpointHandler` protocol and the `$EDITOR` implementation |
| `cancel.py` | `.foundry/cancel` sentinel |
| `cli.py` | Typer entry point; thin dispatch only |

### foundry.lock

`[run]` is written once and never edited. Everything after it is appended as new, uniquely
named sections (`[artifacts.prd]`, `[checkpoints.prd]`, `[checkpoints.prd.revision-1]`,
`[run.test_runner]`, `[plan]`, `[repo]`, `[scaffold]`, `[epics.epic-1]`, `[epics.epic-1.merged]`,
`[implementation.epic-1.story-1]`, `[failures.epic-1.story-1]`, `[interventions.epic-1.story-1]`,
`[deferred.epic-1.story-1]`, `[halts.halt-1]`, `[resumes.resume-1]`, `[model_changes.change-1]`,
`[consistency.pass-1]`, `[dockerfile]`, `[cancelled]`, `[cancelled.cancel-2]`, `[completed]`), so a run's
end is recorded by appending a section rather than by changing `[run].status`. A run is
"in progress" while `[run]` exists and neither `[cancelled]` nor `[completed]` does.

The lock stores hashes, never artifact content.

### Commits

Every recorded action is one commit, `foundry: <action> — <detail>`. Most contain only
`foundry.lock`; the scaffold commit also holds the scaffold files and a story commit holds the
story's changes.

## Tests

```bash
pytest
```
