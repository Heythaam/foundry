from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from foundry.context import (
    Artifact,
    ArtifactType,
    CodeFailure,
    EnvFailure,
    RunContext,
)
from foundry.lock import LockFile
from foundry.orchestrator import Orchestrator
from foundry.phases import Phase, PhaseHooks
from foundry.pipeline import build_pipeline
from foundry.planning import BrainstormPhase, PrdPhase
from foundry.preflight import (
    DECISION_PROCEED,
    DECISION_REVISE,
    Contradiction,
    Findings,
    Omission,
    PreflightPhase,
    TraceabilityGap,
    find_omissions,
    parse_agent_findings,
    render_report,
    summarize_report,
)
from tests.helpers import FakeRunner, ScriptedCheckpoint

COMPLETE_PRD = (
    "# PRD\n\nFR-1: users authenticate with a password.\n"
    "FR-2: on failure the app retries, then reports the error.\n"
    "FR-3: todos are persisted in SQLite.\nNFR-1: events are logged to stderr.\n"
)
BARE_PRD = "# PRD\n\nFR-1: show a list.\nFR-2: add an item.\n"
BRAINSTORM = Artifact("# Brainstorm\n\nTheme: export to CSV\n", ArtifactType.BRAINSTORM)

CLEAN = json.dumps({"contradictions": [], "traceability_gaps": []})
CONFLICT = json.dumps(
    {
        "contradictions": [
            {"requirements": ["FR-1", "FR-4"], "description": "FR-1 is offline-only; FR-4 needs live sync"}
        ],
        "traceability_gaps": [{"theme": "CSV export", "description": "no requirement covers export"}],
    }
)


def lock_data(workspace: Path) -> dict:
    return tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))


def ctx_with_brainstorm(workspace: Path) -> RunContext:
    ctx = RunContext(idea="a todo app", workspace_path=workspace)
    ctx.upstream["brainstorm"] = BRAINSTORM
    return ctx


# ------------------------------------------------------------------ FR-12: omissions


class TestOmissions:
    def test_complete_prd_has_no_omissions(self) -> None:
        assert find_omissions(COMPLETE_PRD) == []

    def test_bare_prd_is_missing_everything(self) -> None:
        found = {o.key: o.critical for o in find_omissions(BARE_PRD)}
        assert found == {
            "auth": True,
            "error_handling": True,
            "persistence": True,
            "logging": False,
        }

    @pytest.mark.parametrize(
        ("mention", "key"),
        [
            ("Users must log in.", "auth"),
            ("Authorization is by role.", "auth"),
            ("There is NO AUTHENTICATION in v1.", "auth"),  # ruled out explicitly = addressed
            ("Invalid input shows an error message and the call is retried.", "error_handling"),
            ("Data is stored in a database.", "persistence"),
            ("State is saved to disk.", "persistence"),
            ("We log requests.", "logging"),
            ("Structured logging to stderr.", "logging"),
        ],
    )
    def test_a_mention_addresses_the_topic(self, mention: str, key: str) -> None:
        assert key not in {o.key for o in find_omissions(BARE_PRD + mention)}

    def test_only_the_missing_topics_are_reported(self) -> None:
        prd = BARE_PRD + "Users log in. Data is persisted."
        assert {o.key for o in find_omissions(prd)} == {"error_handling", "logging"}


# ------------------------------------------------------------- parsing agent output


class TestAgentFindings:
    def test_parses_a_fenced_json_block(self) -> None:
        text = f"Here you go:\n```json\n{CONFLICT}\n```\nHope that helps."
        contradictions, gaps = parse_agent_findings(text)
        assert contradictions == [
            Contradiction(("FR-1", "FR-4"), "FR-1 is offline-only; FR-4 needs live sync")
        ]
        assert gaps == [TraceabilityGap("CSV export", "no requirement covers export")]

    def test_parses_bare_json_surrounded_by_prose(self) -> None:
        contradictions, gaps = parse_agent_findings(f"Result: {CLEAN} done")
        assert contradictions == [] and gaps == []

    @pytest.mark.parametrize(
        "text",
        [
            "I found no problems.",
            "{not json}",
            json.dumps({"something": "else"}),
            json.dumps([1, 2]),
            json.dumps({"contradictions": "none", "traceability_gaps": []}),
            json.dumps({"contradictions": [{"description": ""}], "traceability_gaps": []}),
            json.dumps({"contradictions": [{"requirements": "FR-1", "description": "x"}], "traceability_gaps": []}),
            json.dumps({"contradictions": [], "traceability_gaps": [{"theme": "t"}]}),
        ],
    )
    def test_unusable_output_is_a_code_failure(self, text: str) -> None:
        with pytest.raises(CodeFailure):
            parse_agent_findings(text)


# --------------------------------------------------------------------- the report


def findings(contradictions: int = 0, critical_omissions: int = 0, gaps: int = 0) -> Findings:
    return Findings(
        [Contradiction(("FR-1",), f"c{n}") for n in range(contradictions)],
        [Omission("auth", "Authentication", True, "hint") for _ in range(critical_omissions)]
        + [Omission("logging", "Logging", False, "hint")],
        [TraceabilityGap(f"t{n}", "d") for n in range(gaps)],
    )


class TestReport:
    def prd(self) -> Artifact:
        return Artifact(COMPLETE_PRD, ArtifactType.PRD)

    def test_counts_round_trip(self) -> None:
        summary = summarize_report(render_report(findings(2, 1, 3), self.prd()))
        assert (summary.contradictions, summary.omissions, summary.gaps, summary.blocking) == (2, 2, 3, 3)

    def test_blocking_findings_default_to_revise(self) -> None:
        assert summarize_report(render_report(findings(contradictions=1), self.prd())).decision == DECISION_REVISE
        assert summarize_report(render_report(findings(critical_omissions=1), self.prd())).decision == DECISION_REVISE

    def test_advisory_findings_alone_default_to_proceed(self) -> None:
        report = render_report(findings(gaps=2), self.prd())  # gaps + advisory logging omission
        summary = summarize_report(report)
        assert summary.blocking == 0 and summary.decision == DECISION_PROCEED

    def test_the_humans_decision_overrides_the_default(self) -> None:
        report = render_report(findings(contradictions=1), self.prd()).replace(
            "Decision: revise-prd", "Decision: proceed"
        )
        assert summarize_report(report).decision == DECISION_PROCEED

    def test_decision_is_case_and_whitespace_tolerant_and_last_one_wins(self) -> None:
        report = render_report(findings(), self.prd()) + "\n  DECISION:   Revise-PRD  \n"
        assert summarize_report(report).decision == DECISION_REVISE

    def test_missing_decision_line_falls_back_to_findings(self) -> None:
        report = render_report(findings(contradictions=1), self.prd()).replace("Decision: revise-prd", "")
        assert summarize_report(report).decision == DECISION_REVISE
        clean = render_report(findings(), self.prd()).replace("Decision: proceed", "")
        assert summarize_report(clean).decision == DECISION_PROCEED

    def test_damaged_metadata_reads_as_zero_not_a_crash(self) -> None:
        report = render_report(findings(contradictions=2), self.prd())
        broken = report.replace('"contradictions": 2', '"contradictions": ')
        assert summarize_report(broken).contradictions == 0
        assert summarize_report("# whatever").decision == DECISION_PROCEED

    def test_report_names_each_finding(self) -> None:
        report = render_report(
            Findings(
                [Contradiction(("FR-1", "FR-4"), "offline vs live")],
                [Omission("auth", "Authentication / authorization", True, "Decide it.")],
                [TraceabilityGap("CSV export", "uncovered")],
            ),
            self.prd(),
        )
        assert "FR-1 vs FR-4" in report and "offline vs live" in report
        assert "Authentication / authorization" in report and "critical" in report
        assert "CSV export" in report


# --------------------------------------------------------------------- the phase


class TestPhase:
    def test_is_a_checkpointed_phase_with_hooks(self) -> None:
        phase = PreflightPhase(FakeRunner("x"))
        assert isinstance(phase, Phase) and isinstance(phase, PhaseHooks)
        assert phase.name == "preflight" and phase.checkpoint and phase.revise_target == "prd"

    async def test_agent_gets_the_prd_and_the_brainstorm_inlined(self, workspace: Path) -> None:
        runner = FakeRunner(CLEAN)
        await PreflightPhase(runner).run(Artifact(COMPLETE_PRD, ArtifactType.PRD), ctx_with_brainstorm(workspace))
        prompt = runner.prompts[0]
        assert COMPLETE_PRD.strip() in prompt and "export to CSV" in prompt
        assert "contradictions" in prompt and "traceability" in prompt.lower()

    async def test_report_combines_agent_findings_and_deterministic_omissions(
        self, workspace: Path
    ) -> None:
        out = await PreflightPhase(FakeRunner(CONFLICT)).run(
            Artifact(BARE_PRD, ArtifactType.PRD), ctx_with_brainstorm(workspace)
        )
        assert out.type is ArtifactType.PREFLIGHT
        assert "offline-only" in out.content  # FR-11 (agent)
        assert "Data persistence" in out.content  # FR-12 (keywords)
        assert "CSV export" in out.content  # FR-13 (agent)
        summary = summarize_report(out.content)
        assert (summary.contradictions, summary.omissions, summary.gaps) == (1, 4, 1)
        assert summary.decision == DECISION_REVISE

    async def test_clean_prd_proceeds_by_default(self, workspace: Path) -> None:
        out = await PreflightPhase(FakeRunner(CLEAN)).run(
            Artifact(COMPLETE_PRD, ArtifactType.PRD), ctx_with_brainstorm(workspace)
        )
        assert summarize_report(out.content).decision == DECISION_PROCEED

    async def test_requires_the_approved_brainstorm(self, workspace: Path) -> None:
        with pytest.raises(EnvFailure, match="brainstorm"):
            await PreflightPhase(FakeRunner(CLEAN)).run(
                Artifact(COMPLETE_PRD, ArtifactType.PRD), RunContext(idea="x", workspace_path=workspace)
            )

    async def test_unparseable_agent_output_is_a_code_failure(self, workspace: Path) -> None:
        with pytest.raises(CodeFailure):
            await PreflightPhase(FakeRunner("looks fine to me")).run(
                Artifact(COMPLETE_PRD, ArtifactType.PRD), ctx_with_brainstorm(workspace)
            )

    def test_hooks_read_the_approved_report(self) -> None:
        phase = PreflightPhase(FakeRunner("x"))
        prd = Artifact(COMPLETE_PRD, ArtifactType.PRD)
        report = Artifact(render_report(findings(contradictions=2), prd), ArtifactType.PREFLIGHT)
        assert phase.requests_revision(report) is True
        assert phase.checkpoint_fields(report) == {
            "contradiction_count": 2,
            "omission_count": 1,
            "traceability_gap_count": 0,
            "blocking_findings": 2,
            "resolution": "revise-prd",
            "proceeded_with_blocking_findings": False,
        }


# ------------------------------------------------------- in the pipeline (AD-10)


def pipeline(runner: FakeRunner, extra: list | None = None) -> list:
    return [BrainstormPhase(runner), PrdPhase(runner), PreflightPhase(runner), *(extra or [])]


class Marker:
    """Stands in for the architecture phase: records when it runs and what it was given."""

    name, checkpoint = "after", False

    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
        self.log.append(f"after:{input.type.value}")
        return input


class TestInPipeline:
    async def test_clean_run_checkpoints_the_report_and_records_the_results(
        self, workspace: Path
    ) -> None:
        runner = FakeRunner("# Brainstorm\n", COMPLETE_PRD, CLEAN)
        checkpoint = ScriptedCheckpoint()
        await Orchestrator(pipeline(runner), checkpoint=checkpoint).run(RunContext(idea="x"))

        assert checkpoint.phases == ["brainstorm", "prd", "preflight"]
        entry = lock_data(workspace)["checkpoints"]["preflight"]
        assert entry["contradiction_count"] == 0 and entry["omission_count"] == 0
        assert entry["resolution"] == "proceed" and entry["proceeded_with_blocking_findings"] is False
        assert "revision-1" not in lock_data(workspace)["checkpoints"]["prd"]

    async def test_contradictions_send_the_human_back_to_edit_the_prd_then_re_run(
        self, workspace: Path
    ) -> None:
        runner = FakeRunner("# Brainstorm\n", COMPLETE_PRD, CONFLICT, CLEAN)
        checkpoint = ScriptedCheckpoint({"prd.revision-1": lambda t: t.replace("offline", "online")})
        log: list[str] = []
        ctx = RunContext(idea="x")
        # edit the PRD itself on the revision pass
        checkpoint.edits["prd.revision-1"] = lambda t: t + "FR-4 resolved: sync is optional.\n"
        # the second report must say proceed; the first keeps its default (revise-prd)
        await Orchestrator(pipeline(runner, [Marker(log)]), checkpoint=checkpoint).run(ctx)

        assert checkpoint.phases == [
            "brainstorm", "prd", "preflight", "prd.revision-1", "preflight.revision-1",
        ]
        assert dict(checkpoint.presented)["prd.revision-1"] == COMPLETE_PRD  # the PRD reopened
        # pre-flight ran twice; the second time on the edited PRD
        assert len(runner.requests) == 4
        assert "FR-4 resolved: sync is optional." in runner.prompts[3]
        assert ctx.upstream["prd"].content.endswith("sync is optional.\n")

        data = lock_data(workspace)
        first, second = data["checkpoints"]["preflight"], data["checkpoints"]["preflight"]["revision-1"]
        assert first["contradiction_count"] == 1 and first["resolution"] == "revise-prd"
        assert second["contradiction_count"] == 0 and second["resolution"] == "proceed"
        assert data["checkpoints"]["prd"]["revision-1"]["approved_without_edits"] is False
        assert data["artifacts"]["prd"]["revision-1"]["artifact_hash"] == ctx.upstream["prd"].sha256_hash

    async def test_the_next_phase_does_not_run_until_findings_are_resolved(
        self, workspace: Path
    ) -> None:
        order: list[str] = []

        class Spy(FakeRunner):
            async def run(self, request, ctx):
                order.append(request.name)
                return await super().run(request, ctx)

        runner = Spy("# Brainstorm\n", COMPLETE_PRD, CONFLICT, CLEAN)
        log: list[str] = []

        class Tracking(Marker):
            async def run(self, input, ctx):
                order.append("after")
                return await super().run(input, ctx)

        await Orchestrator(pipeline(runner, [Tracking(log)]), checkpoint=ScriptedCheckpoint()).run(
            RunContext(idea="x")
        )
        assert order == ["brainstorm", "prd", "preflight", "preflight", "after"]
        assert log == ["after:preflight"]

    async def test_human_can_override_and_proceed_and_it_is_recorded(self, workspace: Path) -> None:
        runner = FakeRunner("# Brainstorm\n", BARE_PRD, CONFLICT)
        checkpoint = ScriptedCheckpoint(
            {"preflight": lambda t: t.replace("Decision: revise-prd", "Decision: proceed")}
        )
        await Orchestrator(pipeline(runner), checkpoint=checkpoint).run(RunContext(idea="x"))

        assert checkpoint.phases == ["brainstorm", "prd", "preflight"]  # no revision
        entry = lock_data(workspace)["checkpoints"]["preflight"]
        assert entry["resolution"] == "proceed"
        assert entry["proceeded_with_blocking_findings"] is True
        assert entry["blocking_findings"] == 4  # 1 contradiction + auth/errors/persistence

    async def test_a_run_that_never_converges_stops_with_a_valid_lock(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("foundry.orchestrator.MAX_REVISIONS", 2)
        runner = FakeRunner("# Brainstorm\n", COMPLETE_PRD, CONFLICT)  # always conflicting
        with pytest.raises(EnvFailure, match="after 2 revisions"):
            await Orchestrator(pipeline(runner), checkpoint=ScriptedCheckpoint()).run(
                RunContext(idea="x")
            )
        assert LockFile.for_workspace(workspace).is_in_progress()  # resumable, not corrupted
        assert "revision-2" in lock_data(workspace)["checkpoints"]["preflight"]

    async def test_nothing_is_recorded_for_an_unapproved_report(self, workspace: Path) -> None:
        class AbortAtPreflight(ScriptedCheckpoint):
            async def present(self, artifact, metadata=None):
                if (metadata or {}).get("phase") == "preflight":
                    raise EnvFailure("editor crashed")
                return await super().present(artifact, metadata)

        runner = FakeRunner("# Brainstorm\n", COMPLETE_PRD, CLEAN)
        with pytest.raises(EnvFailure):
            await Orchestrator(pipeline(runner), checkpoint=AbortAtPreflight()).run(RunContext(idea="x"))
        data = lock_data(workspace)
        assert "preflight" not in data["checkpoints"] and "preflight" not in data["artifacts"]

    async def test_a_failing_hook_is_classified(self, workspace: Path) -> None:
        class BadHooks(PreflightPhase):
            def checkpoint_fields(self, approved):
                raise ValueError("hook blew up")

        runner = FakeRunner("# Brainstorm\n", COMPLETE_PRD, CLEAN)
        phases = [BrainstormPhase(runner), PrdPhase(runner), BadHooks(runner)]
        with pytest.raises(EnvFailure, match="hook blew up"):
            await Orchestrator(phases, checkpoint=ScriptedCheckpoint()).run(RunContext(idea="x"))

    def test_preflight_sits_between_prd_and_the_end_of_the_current_pipeline(self) -> None:
        assert [p.name for p in build_pipeline(FakeRunner("x"))][:3] == ["brainstorm", "prd", "preflight"]
