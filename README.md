# foundry

Foundry takes an idea through a human-checkpointed pipeline and records every step as
tamper-evident provenance in two places at once: `foundry.lock` and git history.

> **Status:** Epics 1-2 (core infrastructure and the planning pipeline). `foundry pro` runs
> brainstorm -> PRD -> pre-flight -> architecture -> epics/stories, each phase a Claude Code
> subprocess followed by a human checkpoint. `foundry cancel` works; `quick`, `resume` and
> `status` are stubs. Repo creation and implementation arrive in Epic 3.

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
foundry pro "build a todo app" --review-epics   # also pause before each epic (used by Epic 3)
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
| `lock.py` | `foundry.lock`: append-only TOML, atomic temp-file + rename writes |
| `provenance.py` | `ProvenanceRecorder`: every action writes a lock section **and** a git commit |
| `state.py` | `.foundry/state.db`: aiosqlite WAL journal of in-flight actions (never committed) |
| `checkpoint.py` | `CheckpointHandler` protocol and the `$EDITOR` implementation |
| `cancel.py` | `.foundry/cancel` sentinel |
| `cli.py` | Typer entry point; thin dispatch only |

### foundry.lock

`[run]` is written once and never edited. Everything after it is appended as new, uniquely
named sections (`[artifacts.prd]`, `[checkpoints.prd]`, `[checkpoints.prd.revision-1]`,
`[run.test_runner]`, `[plan]`, `[cancelled]`, `[completed]`), so a run's
end is recorded by appending a section rather than by changing `[run].status`. A run is
"in progress" while `[run]` exists and neither `[cancelled]` nor `[completed]` does.

The lock stores hashes, never artifact content.

### Commits

Every recorded action is one commit, `foundry: <action> — <detail>`, containing only
`foundry.lock`.

## Tests

```bash
pytest
```
