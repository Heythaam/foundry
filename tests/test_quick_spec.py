"""Story 7.1: `foundry quick` writes a one-page spec, you review it, then the repo is created."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from foundry.context import Artifact, ArtifactType, CodeFailure, RunContext, sha256_of
from foundry.lock import LockFile
from foundry.orchestrator import RunStatus
from foundry.planning import extract_test_runner
from foundry.quick import (
    SPEC_TEST_SECTION,
    SpecPhase,
    build_quick_pipeline,
    build_spec_prompt,
    check_spec,
)
from tests.fake_github import FakeGitHub
from tests.helpers import FakeRunner, ScriptedCheckpoint
from tests.planned import QuickRunner, count_command, make_spec, quick_run

REPO = "link-shortener"
COMMAND = "pytest -q"


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


def lock(workspace: Path) -> dict:
    return tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))


# ------------------------------------------------------------------------ the spec itself


class TestTheSpecPrompt:
    def test_it_asks_for_the_sections_the_story_names(self) -> None:
        prompt = build_spec_prompt("a link shortener")
        for expected in ("## Feature list", "## Data model", f"## {SPEC_TEST_SECTION}",
                         "## Not building", "## Assumptions", "ONE page", "fenced code block"):
            assert expected in prompt, expected
        assert "a link shortener" in prompt

    def test_it_invokes_no_bmad_skill(self) -> None:
        prompt = build_spec_prompt("a link shortener")
        assert "skill" not in prompt.lower() and "bmad" not in prompt.lower()

    def test_it_forbids_writing_files(self) -> None:
        assert "Do not write or modify files" in build_spec_prompt("x")


class TestCheckingASpec:
    def test_a_good_spec_passes(self) -> None:
        check_spec(make_spec(COMMAND))

    @pytest.mark.parametrize("heading", ["Feature list", "Data model", "Test strategy"])
    def test_each_required_section_is_required(self, heading: str) -> None:
        spec = make_spec(COMMAND).replace(f"## {heading}", "## Something else")
        with pytest.raises(CodeFailure, match=f"'## {heading}'"):
            check_spec(spec)

    def test_an_empty_section_is_missing(self) -> None:
        spec = make_spec(COMMAND).replace("- Link(code, url, created_at)\n", "")
        with pytest.raises(CodeFailure, match="Data model"):
            check_spec(spec)

    def test_all_missing_sections_are_named_at_once(self) -> None:
        with pytest.raises(CodeFailure) as failed:
            check_spec("# Title\n\nJust prose.\n")
        for heading in ("Feature list", "Data model", "Test strategy"):
            assert heading in failed.value.message

    def test_a_spec_with_no_test_command_is_refused_with_a_fix(self) -> None:
        spec = make_spec(COMMAND).replace(f"```bash\n{COMMAND}\n```\n", "Tests are good.\n")
        with pytest.raises(CodeFailure, match="no command"):
            check_spec(spec)

    def test_two_commands_are_refused(self) -> None:
        spec = make_spec("pip install -e .\npytest -q")
        with pytest.raises(CodeFailure, match="exactly one command"):
            check_spec(spec)

    def test_headings_match_whatever_the_case(self) -> None:
        spec = make_spec(COMMAND).replace("## Test strategy", "## Test Strategy")
        check_spec(spec)

    def test_windows_line_endings_are_fine(self) -> None:
        check_spec(make_spec(COMMAND).replace("\n", "\r\n"))

    def test_the_command_is_read_from_the_test_strategy_section(self) -> None:
        assert extract_test_runner(make_spec("npm test"), SPEC_TEST_SECTION, "spec") == "npm test"

    def test_the_architecture_default_is_unchanged(self) -> None:
        text = "# A\n\n## Test Runner\n\n```bash\npytest -q\n```\n"
        assert extract_test_runner(text) == "pytest -q"
        with pytest.raises(CodeFailure, match="The architecture has no '## Test Runner'"):
            extract_test_runner("# A\n")


class TestTheSpecPhase:
    async def test_the_session_output_becomes_the_spec_artifact(self, workspace: Path) -> None:
        runner = FakeRunner(make_spec(COMMAND))
        phase = SpecPhase(runner)
        ctx = RunContext(idea="a link shortener", workspace_path=workspace, mode="quick")
        artifact = await phase.run(Artifact("a link shortener", ArtifactType.IDEA), ctx)

        assert artifact.type.value == "spec" and artifact.content.startswith("# Link Shortener")
        assert runner.requests[0].name == "spec"
        assert "a link shortener" in runner.prompts[0]
        assert phase.checkpoint is True

    async def test_an_empty_answer_fails(self, workspace: Path) -> None:
        phase = SpecPhase(FakeRunner("   "))
        with pytest.raises(CodeFailure, match="empty document"):
            await phase.run(
                Artifact("x", ArtifactType.IDEA),
                RunContext(idea="x", workspace_path=workspace, mode="quick"),
            )

    async def test_an_incomplete_spec_fails_before_you_are_asked_to_review_it(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        checkpoint = ScriptedCheckpoint()
        runner = QuickRunner(spec="# Link Shortener\n\nIt shortens links.\n")
        with pytest.raises(CodeFailure, match="Feature list"):
            await quick_run(workspace, tmp_path, runner, checkpoint=checkpoint)
        assert checkpoint.presented == []  # your time was not spent on it
        assert "spec" not in lock(workspace).get("artifacts", {})
        assert REPO not in github.repos

    def test_the_pipeline_is_the_spec_phase_alone(self) -> None:
        pipeline = build_quick_pipeline(FakeRunner("x"))
        assert [phase.name for phase in pipeline] == ["spec"]  # no brainstorm, PRD, architecture, epics

    def test_the_default_session_has_no_skill_or_write_tools(self) -> None:
        phase = build_quick_pipeline()[0]
        assert phase.runner.allowed_tools == ["Read"]


# ---------------------------------------------------------------------- the checkpoint


class TestTheCheckpoint:
    async def test_the_spec_is_presented_for_review_before_anything_else_happens(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        seen: dict[str, object] = {}

        class Observing(ScriptedCheckpoint):
            async def present(self, artifact, metadata=None):
                seen["repo_exists"] = REPO in github.repos
                seen["lock_sections"] = list(lock(workspace))
                return await super().present(artifact, metadata)

        checkpoint = Observing()
        runner = QuickRunner()
        await quick_run(workspace, tmp_path, runner, checkpoint=checkpoint, through="repo")

        assert checkpoint.phases == ["spec"]  # one checkpoint, and it is the spec
        assert checkpoint.presented[0][1].startswith("# Link Shortener")
        assert seen["repo_exists"] is False  # nothing touches GitHub before you approve
        assert seen["lock_sections"] == ["run"]  # and nothing but the run start is recorded
        assert runner.sessions == ["spec"]  # one session: no BMad planning agents

    async def test_you_can_edit_the_spec_and_the_edit_is_what_is_built_from(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        edited = {}

        def edit(text: str) -> str:
            edited["text"] = text.replace("Python 3.12, standard library only", "Python 3.12 and Flask")
            return edited["text"]

        checkpoint = ScriptedCheckpoint({"spec": edit})
        await quick_run(workspace, tmp_path, QuickRunner(), checkpoint=checkpoint, through="repo")

        stored = (workspace / ".foundry" / "artifacts" / "spec.md").read_text(encoding="utf-8")
        assert stored == edited["text"] and "Flask" in stored
        data = lock(workspace)
        assert data["artifacts"]["spec"]["artifact_hash"] == sha256_of(edited["text"])
        assert data["checkpoints"]["spec"]["approved_without_edits"] is False
        assert data["checkpoints"]["spec"]["presented_hash"] != data["checkpoints"]["spec"]["artifact_hash"]
        assert "Flask" in github.remote_show(REPO, "main", "docs/planning/spec.md")

    async def test_approving_it_as_it_is_is_recorded_as_no_edits(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await quick_run(workspace, tmp_path, QuickRunner(), through="repo")
        assert lock(workspace)["checkpoints"]["spec"]["approved_without_edits"] is True

    async def test_a_changed_test_command_is_the_one_that_gets_recorded(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        checkpoint = ScriptedCheckpoint({"spec": lambda text: text.replace(COMMAND, "make check")})
        runner = QuickRunner(spec=make_spec(COMMAND))
        await quick_run(workspace, tmp_path, runner, checkpoint=checkpoint, through="repo")

        runner_entry = lock(workspace)["run"]["test_runner"]
        assert runner_entry["command"] == "make check"
        assert LockFile.for_workspace(workspace).test_runner() == "make check"

    async def test_deleting_the_test_command_records_nothing_and_stops(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        strip = lambda text: text.replace(f"```bash\n{COMMAND}\n```\n", "")  # noqa: E731
        checkpoint = ScriptedCheckpoint({"spec": strip})
        with pytest.raises(CodeFailure, match="no command"):
            await quick_run(workspace, tmp_path, QuickRunner(spec=make_spec(COMMAND)), checkpoint=checkpoint)
        data = lock(workspace)
        assert "spec" not in data.get("artifacts", {}) and "test_runner" not in data["run"]
        assert REPO not in github.repos

    async def test_the_approved_spec_is_recorded_by_hash_not_by_content(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await quick_run(workspace, tmp_path, QuickRunner(), through="repo")
        assert "Shorten a URL" not in (workspace / "foundry.lock").read_text(encoding="utf-8")  # NFR-7
        assert "spec" in lock(workspace)["artifacts"]
        subjects = git(workspace, "log", "--format=%s").splitlines()
        assert any(s.startswith("foundry: checkpoint approved — spec") for s in subjects)
        assert any(s.startswith("foundry: artifact recorded — spec") for s in subjects)


# ---------------------------------------------------------------------------- the lock


class TestTheMode:
    async def test_the_lock_records_a_quick_run(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await quick_run(workspace, tmp_path, QuickRunner(), through="repo")
        run = lock(workspace)["run"]
        assert run["mode"] == "quick" and run["idea"] == "a link shortener"
        assert run["status"] == "in_progress"

    def test_a_run_context_is_pro_unless_it_says_otherwise(self) -> None:
        assert RunContext(idea="x").mode == "pro"
        assert RunContext(idea="x", mode="quick").mode == "quick"

    async def test_mode_is_the_first_thing_in_run_so_it_is_never_edited_later(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await quick_run(workspace, tmp_path, QuickRunner(), through="repo")
        assert list(lock(workspace))[0] == "run"
        assert "mode" in lock(workspace)["run"]

    async def test_the_planning_pipeline_artifacts_are_never_created(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await quick_run(workspace, tmp_path, QuickRunner(), through="repo")
        artifacts = lock(workspace)["artifacts"]
        assert list(artifacts) == ["spec"]
        assert "plan" not in lock(workspace)
        assert sorted(p.name for p in (workspace / ".foundry" / "artifacts").iterdir()) == ["spec.md"]


# ---------------------------------------------------------------- the repository


class TestTheRepository:
    async def test_after_approval_the_repo_is_created_and_scaffolded(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        _, result = await quick_run(workspace, tmp_path, QuickRunner(), through="repo")

        assert result.status is RunStatus.COMPLETED
        files = github.remote_files(REPO, "main")
        for expected in ("README.md", "LICENSE", ".gitignore", ".github/workflows/ci.yml",
                         "docs/planning/spec.md", "foundry.lock"):
            assert expected in files, expected
        data = lock(workspace)
        assert data["repo"]["full_name"].endswith(f"/{REPO}")
        assert "scaffold" in data and data["scaffold"]["branch"] == "main"

    async def test_the_scaffold_holds_the_spec_and_only_the_spec(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await quick_run(workspace, tmp_path, QuickRunner(), through="repo")
        planning = [f for f in github.remote_files(REPO, "main") if f.startswith("docs/planning/")]
        assert planning == ["docs/planning/spec.md"]
        spec = (workspace / ".foundry" / "artifacts" / "spec.md").read_text(encoding="utf-8")
        assert github.remote_show(REPO, "main", "docs/planning/spec.md").strip() == spec.strip()

    async def test_the_repo_is_named_after_the_specs_title(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = QuickRunner(spec=make_spec(count_command(tmp_path), title="Pocket Ledger"))
        await quick_run(workspace, tmp_path, runner, through="repo")
        assert lock(workspace)["repo"]["name"] == "pocket-ledger"

    async def test_a_title_that_says_spec_does_not_leak_into_the_name(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = QuickRunner(spec=make_spec(count_command(tmp_path), title="Pocket Ledger: One-Page Spec"))
        await quick_run(workspace, tmp_path, runner, through="repo")
        assert lock(workspace)["repo"]["name"] == "pocket-ledger"

    async def test_ci_runs_the_test_command_from_the_spec(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = QuickRunner(spec=make_spec("npm ci && npm test"))
        await quick_run(workspace, tmp_path, runner, through="repo")
        ci = github.remote_show(REPO, "main", ".github/workflows/ci.yml")
        assert "npm ci && npm test" in ci and "setup-node" in ci  # the language came from the command
        assert "## Tests" in github.remote_show(REPO, "main", "README.md")

    async def test_the_history_is_the_spec_then_the_repo_then_one_scaffold_commit(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await quick_run(workspace, tmp_path, QuickRunner(), through="repo")
        log = git(workspace, "log", "--reverse", "--format=%s").splitlines()
        assert [s.split(" — ")[0] for s in log] == [
            "foundry: run started",
            "foundry: checkpoint approved",
            "foundry: artifact recorded",
            "foundry: derived section recorded",
            "foundry: repo created",
            "foundry: scaffold",
            "foundry: run completed",  # `through="repo"` ends the run here
        ]
        assert git(workspace, "status", "--porcelain") == ""
        assert "foundry: scaffold — README, LICENSE, .gitignore, ci.yml, foundry.lock seeded" in github.remote_log(REPO, "main")

    async def test_a_pro_run_is_unaffected(self) -> None:
        from foundry.scaffold import PLANNING_PHASES, QUICK_PHASES, scaffold_files

        pro = scaffold_files(
            name="X", idea="x", test_command="pytest -q", license_holder="me", year=2026,
            documents={"prd": "# P", "architecture": "# A", "epics": "# E", "brainstorm": "# B"},
        )
        assert sorted(f for f in pro if f.startswith("docs/planning/")) == [
            f"docs/planning/{p}.md" for p in sorted(PLANNING_PHASES)
        ]
        assert QUICK_PHASES == ("spec",)
