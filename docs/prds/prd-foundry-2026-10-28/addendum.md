# PRD Addendum: Foundry CLI

Technical depth, rejected alternatives, and options-considered that inform architecture but don't belong in the PRD's requirements narrative.

## foundry.lock Format (Sketch)

```toml
[run]
idea_hash = "sha256:abc123..."
started = 2026-10-28T14:30:00Z
model = "claude-sonnet-5-5"
agent_versions.brainstorm = "bmad-brainstorm@6.13.0"
agent_versions.prd = "bmad-prd@6.13.0"
agent_versions.architecture = "bmad-architecture@6.13.0"
agent_versions.epics = "bmad-epics@6.13.0"
test_runner = "pytest"

[checkpoints.brainstorm]
timestamp = 2026-10-28T14:32:00Z
artifact_hash = "sha256:def456..."
editor_open = 2026-10-28T14:32:05Z
editor_close = 2026-10-28T14:35:22Z
duration_seconds = 197

[checkpoints.prd]
# ...same structure

[checkpoints.preflight]
contradictions_found = 2
resolutions = [
  { ids = ["FR-4", "FR-11"], resolution = "FR-4 wins, FR-11 removed", timestamp = "..." },
]

[implementation.epic-1.story-1]
artifact_hash = "sha256:..."
tests_passed = 12
duration_seconds = 45
retries = 0

[failures.epic-1.story-3]
category = "code"
original_output_hash = "sha256:..."
retry_artifact_hash = "sha256:..."
retry_outcome = "passed"

[interventions.epic-2.story-1]
action = "story_edited"
reason = "LLM misunderstood caching requirement"
timestamp = "..."
```

## Forge Log Commit Message Grammar

```
foundry: <action> — <detail>

Actions:
  scaffold        Initial repo setup (README, LICENSE, .gitignore, ci.yml, foundry.lock)
  <phase> approved  Planning checkpoint (brainstorm, prd, architecture, epics)
  preflight        Pre-flight analysis results
  story <E>.<S> implemented  Story completed with passing tests
  story <E>.<S> auto-fixed   Auto-retry succeeded
  story <E>.<S> deferred     Human chose to skip
  story <E>.<S> — human intervention  Human edited code/story
  dockerfile generated  Post-impl Dockerfile
  consistency pass  Model-boundary style normalization
  resumed          Run continued after interruption
  cancelled        Clean stop
```

## Rejected Alternatives

### Separate forge.log file vs. git-as-forge-log
Considered maintaining a separate `forge.log` file. Rejected because:
- It can drift out of sync with actual git state
- It duplicates information already in commit messages
- It requires its own resume/recovery logic
- Git log is already a time-ordered, immutable, tamper-evident log

### Deployment target opinions (Fly.io, Render, etc.)
Original brainstorm included generating fly.toml and render.yaml. Rejected because:
- Deployment topology varies most between teams/projects
- Dockerfile is the universal interface any platform can consume
- Removes an entire category of config Foundry can get wrong
- Eliminates the "which cloud provider?" checkpoint

### aiosqlite as primary state store vs. foundry.lock + git
Considered using aiosqlite as the source of truth. Kept as internal-only (in-progress state buffer between atomic lock file writes). Rejected as primary because:
- The lock file is committed and travels with the repo
- Git state is already distributed and resumable
- A database file in the repo would need .gitignore handling

### Typer vs. Click
Chose Typer for: auto-generated help, type-hint-driven argument parsing, less boilerplate. Click remains viable; the abstraction layer is thin enough to swap if needed.

## CI Bootstrap (Resolved)

Foundry generates a minimal `.github/workflows/ci.yml` as part of the scaffold step (FR-37). The workflow runs only the test command from foundry.lock on PRs to main. No CD, no deployment steps. This resolves the bootstrap problem: PR auto-merge (FR-20) has a real gate to pass.
