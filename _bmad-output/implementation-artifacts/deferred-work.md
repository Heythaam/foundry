# Deferred Work

- source_spec: `_bmad-output/implementation-artifacts/spec-8-cli-ux.md`
  summary: PhaseSignal orchestrator hardcodes "completed" as outcome — meaningful outcome data requires Phase Protocol changes
  evidence: PhaseSignal.present accepts an outcome parameter but the orchestrator always passes the literal string "completed"; meaningful outcomes (e.g. "5 stories approved") cannot be produced without adding outcome-reporting to the Phase Protocol, which is outside this epic's scope
