# foundry

Foundry takes an idea through a human-checkpointed pipeline and records every step as
tamper-evident provenance in two places at once: `foundry.lock` and git history.

> **Status:** Epics 1-3 (core infrastructure, the planning pipeline, repo creation and the
> implementation loop). `foundry pro` plans (brainstorm -> PRD -> pre-flight -> architecture ->
> epics/stories, each with a human checkpoint), creates a GitHub repo, then builds every story in
> its own Claude Code session and merges one PR per epic. `foundry cancel` works; `quick`,
> `resume` and `status` are stubs, and a failed story or red CI currently stops the run
> (retry and escalation arrive in Epic 4).

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
   CI stops the run and leaves the PR open.

Set `GITHUB_TOKEN` (a token with `repo` and `workflow` scope) before running `foundry pro`; it is
checked first, before any planning work, and is the only place the token is read from. It is
never written to `foundry.lock` or `.git/config` and is scrubbed from error messages.
`GITHUB_API_URL` overrides the API endpoint (GitHub Enterprise). Implementation sessions get the
`Read`, `Write`, `Edit`, `MultiEdit`, `Glob`, `Grep`, `Bash` and `Skill` tools.

The CI workflow runs on pull requests to `main` and runs exactly one command, the one in
`[run.test_runner]`, after checking out and setting up the toolchain it detects. CI starts from a
clean checkout, so that command must install the project's dependencies too (for example
`pip install -e '.[dev]' && pytest -q`); the architecture prompt asks for this.

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
`[implementation.epic-1.story-1]`, `[cancelled]`, `[completed]`), so a run's
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
