# Brainstorm Intent: Foundry CLI

## Intent

Foundry is a Python CLI that orchestrates BMad agents through a planning pipeline (brainstorm, PRD, architecture, epics, implementation), with provenance tracking (foundry.lock + git-as-forge-log), a three-category failure model, Dockerfile-only output, and human checkpoints designed to resist rubber-stamping.

## Core Identity

The crown jewel is provenance and traceability -- foundry.lock plus structured git history -- not BMad planning itself. BMad is the most powerful mode, not the only one. The core value proposition is structure (explicit contract), incrementality (navigable history), and verification (tests required). Foundry is an observable AI process: inspectable, replayable, auditable development.

## v1 Scope (MoSCoW)

- **MUST**: Foundry Pro (full BMad pipeline) + foundry.lock + git-as-forge-log + three-category failure taxonomy (code/infra/environment) + Dockerfile output
- **SHOULD**: Foundry Quick mode + checkpoint web UI + pre-flight analysis + escalate-to-human on failure
- **COULD**: foundry.yaml declarative process spec + dependency verification + anti-rubber-stamp design + --from-prd ingest
- **WON'T (v1)**: foundry reverse, DAG backtracking, live dashboard, template registry, teaching/interview modes

## Design Principles

1. **Model identity is a first-class build parameter.** Foundry never silently switches models mid-run. Model version is pinned in foundry.lock; switching requires explicit human override plus a mandatory consistency pass.
2. **External failure is the normal case, not the edge case.** Foundry talks to 4+ services and must treat partial outages as routine, not exceptional.
3. **The audit trail cuts both ways.** It proves diligence or proves negligence. Foundry must make rubber-stamping harder, not easier.
4. **Never hand an LLM a problem that isn't an LLM problem.** Infrastructure failures get pause-and-wait, not LLM retry. Environment failures get hard stops, not workarounds.
5. **Make human/AI responsibility boundaries explicit and honest.** Every checkpoint, every approval, every skip is recorded with attribution.

## Key Features

- **BMad planning pipeline**: Orchestrates brainstorm through implementation via BMad agents, producing 50+ tracked decisions that justify the lock file's existence.
- **foundry.lock**: Records exact prompt versions, model versions, agent versions, and response hashes per phase -- replayable, diffable, auditable.
- **Git-as-forge-log**: Every Foundry decision becomes a structured commit message; git log is the forge log; the repo tells the full story of how it was built.
- **Three-category failure taxonomy**: Code failures (LLM fixes), infra failures (pause and wait), environment failures (hard stop) -- prevents LLM corruption of working code during retry.
- **Dockerfile output**: Universal deployment interface via Dockerfile + health check + ports declaration; where it runs is not Foundry's problem.
- **Foundry Quick mode**: Skip planning, start from a self-written spec, enforce incremental commits and mandatory tests -- separates Foundry from raw LLM output even without BMad.
- **Checkpoint web UI**: Browser-based review and editing of planning documents with rich text, while terminal streams progress.
- **Pre-flight analysis**: Contradiction detection and traceability validation before architecture begins; catches internally inconsistent specs before they propagate.
- **Escalate-to-human**: Auto-retry once on failure, then pause with full context in checkpoint UI; human intervention is logged in foundry.lock.

## Non-obvious Connections

1. **Pre-flight analysis is the universal ingest layer.** The --from-prd feature and the contradiction detector are the same feature: every spec -- AI-generated or human-authored -- passes through pre-flight analysis before architecture begins.
2. **Failure taxonomy and anti-rubber-stamp share a principle.** Both exist to make human/AI responsibility boundaries explicit. The failure model prevents the AI from silently papering over infra problems; the anti-rubber-stamp design prevents the human from silently papering over review obligations.
3. **The real innovation is observable AI process.** Not code generation, not planning -- the through-line is that every decision, approval, failure, and recovery is inspectable, replayable, and auditable. Foundry makes AI development legible.
