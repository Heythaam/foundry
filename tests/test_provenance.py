from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from foundry.context import (
    Artifact,
    ArtifactType,
    EnvFailure,
    FailureCategory,
    FoundryError,
    RunContext,
)
from foundry.provenance import GitError, ProvenanceRecorder, commit_message, run_git
from foundry.state import FAILED, PENDING, StateBuffer, state_db_path

COMMIT_FORMAT = re.compile(r"^foundry: .+ — .+$")


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout


def subjects(workspace: Path) -> list[str]:
    return git(workspace, "log", "--format=%s").splitlines()


def lock_sections(workspace: Path) -> dict:
    return tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))


@pytest.fixture
def ctx(workspace: Path) -> RunContext:
    return RunContext(idea="build a todo app", workspace_path=workspace)


@pytest.fixture
def recorder(workspace: Path) -> ProvenanceRecorder:
    return ProvenanceRecorder(workspace)


class TestCommitMessage:
    def test_format(self) -> None:
        assert commit_message("scaffold", "initial files") == "foundry: scaffold — initial files"

    async def test_real_commits_use_the_format_with_the_em_dash(
        self, recorder: ProvenanceRecorder, ctx: RunContext, workspace: Path
    ) -> None:
        await recorder.record_run_start(ctx)
        await recorder.record_artifact("prd", Artifact("# PRD", ArtifactType.PRD))
        for subject in subjects(workspace):
            assert COMMIT_FORMAT.match(subject), subject


class TestArtifactRecording:
    async def test_hash_is_sha256_prefixed_and_matches_artifact(
        self, recorder: ProvenanceRecorder, ctx: RunContext, workspace: Path
    ) -> None:
        await recorder.record_run_start(ctx)
        artifact = Artifact("# Brainstorm\n\nideas", ArtifactType.BRAINSTORM)
        await recorder.record_artifact("brainstorm", artifact)

        section = lock_sections(workspace)["artifacts"]["brainstorm"]
        assert section["artifact_hash"] == artifact.sha256_hash
        assert section["artifact_hash"].startswith("sha256:")
        assert len(section["artifact_hash"]) == len("sha256:") + 64
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", section["recorded_at"])
        assert section["type"] == "brainstorm"

    async def test_lock_never_contains_artifact_content(
        self, recorder: ProvenanceRecorder, ctx: RunContext, workspace: Path
    ) -> None:
        await recorder.record_run_start(ctx)
        await recorder.record_artifact("prd", Artifact("SECRET-LLM-RESPONSE-TEXT", ArtifactType.PRD))
        assert "SECRET-LLM-RESPONSE-TEXT" not in (workspace / "foundry.lock").read_text(encoding="utf-8")

    async def test_lock_stays_valid_toml_and_append_only(
        self, recorder: ProvenanceRecorder, ctx: RunContext, workspace: Path
    ) -> None:
        await recorder.record_run_start(ctx)
        before = (workspace / "foundry.lock").read_bytes()
        await recorder.record_artifact("prd", Artifact("# PRD", ArtifactType.PRD))
        after = (workspace / "foundry.lock").read_bytes()
        assert after.startswith(before)
        assert lock_sections(workspace)["run"]["run_id"] == ctx.run_id


class TestDualSink:
    async def test_every_action_creates_exactly_one_lock_section_and_one_commit(
        self, recorder: ProvenanceRecorder, ctx: RunContext, workspace: Path
    ) -> None:
        await recorder.record_run_start(ctx)
        assert len(subjects(workspace)) == 1

        for n, phase in enumerate(["brainstorm", "prd", "architecture"], start=2):
            await recorder.record_artifact(phase, Artifact(phase, ArtifactType.STUB))
            assert len(subjects(workspace)) == n
            assert phase in lock_sections(workspace)["artifacts"]

    async def test_committed_lock_equals_working_lock(
        self, recorder: ProvenanceRecorder, ctx: RunContext, workspace: Path
    ) -> None:
        await recorder.record_run_start(ctx)
        await recorder.record_artifact("prd", Artifact("# PRD", ArtifactType.PRD))
        assert git(workspace, "show", "HEAD:foundry.lock").encode("utf-8") == (
            workspace / "foundry.lock"
        ).read_bytes().replace(b"\r\n", b"\n")
        assert git(workspace, "status", "--porcelain", "--", "foundry.lock") == ""

    async def test_commit_only_touches_the_lock_even_with_other_staged_work(
        self, recorder: ProvenanceRecorder, ctx: RunContext, workspace: Path
    ) -> None:
        await recorder.record_run_start(ctx)
        (workspace / "user_work.txt").write_text("not foundry's", encoding="utf-8")
        git(workspace, "add", "user_work.txt")
        await recorder.record_artifact("prd", Artifact("# PRD", ArtifactType.PRD))
        assert git(workspace, "show", "--name-only", "--format=", "HEAD").split() == ["foundry.lock"]
        assert "user_work.txt" in git(workspace, "diff", "--cached", "--name-only")

    async def test_creates_a_repo_when_the_workspace_is_not_one(
        self, recorder: ProvenanceRecorder, ctx: RunContext, workspace: Path
    ) -> None:
        assert not (workspace / ".git").exists()
        await recorder.record_run_start(ctx)
        assert (workspace / ".git").exists()

    async def test_commit_failure_fails_the_action_and_leaves_a_visible_trace(
        self,
        recorder: ProvenanceRecorder,
        ctx: RunContext,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await recorder.record_run_start(ctx)

        async def broken_git(workspace: Path, *args: str) -> str:
            if args[0] == "commit":
                raise GitError("commit refused by hook")
            return await run_git(workspace, *args)

        monkeypatch.setattr("foundry.provenance.run_git", broken_git)
        with pytest.raises(GitError, match="commit refused"):
            await recorder.record_artifact("prd", Artifact("# PRD", ArtifactType.PRD))

        # the lock is valid, the commit is missing, and the state buffer says so
        assert "prd" in lock_sections(workspace)["artifacts"]
        assert len(subjects(workspace)) == 1
        async with StateBuffer(state_db_path(workspace)) as buffer:
            pending = await buffer.pending()
        assert [(op.section, op.status) for op in pending] == [("artifacts.prd", FAILED)]

    async def test_lock_failure_means_no_commit(
        self, recorder: ProvenanceRecorder, ctx: RunContext, workspace: Path
    ) -> None:
        await recorder.record_run_start(ctx)
        artifact = Artifact("# PRD", ArtifactType.PRD)
        await recorder.record_artifact("prd", artifact)
        with pytest.raises(FoundryError, match="already exists"):
            await recorder.record_artifact("prd", artifact)  # section names are never reused
        assert len(subjects(workspace)) == 2

    async def test_missing_git_is_an_environment_failure(
        self, ctx: RunContext, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(workspace))
        with pytest.raises(EnvFailure) as info:
            await ProvenanceRecorder(workspace).record_run_start(ctx)
        assert info.value.category is FailureCategory.ENVIRONMENT
        assert "git" in info.value.message


class TestStateBuffer:
    async def test_uses_wal_journal_mode(self, workspace: Path) -> None:
        async with StateBuffer(state_db_path(workspace)) as buffer:
            assert (await buffer.journal_mode()).lower() == "wal"

    async def test_completed_ops_are_not_pending(
        self, recorder: ProvenanceRecorder, ctx: RunContext, workspace: Path
    ) -> None:
        await recorder.record_run_start(ctx)
        async with StateBuffer(state_db_path(workspace)) as buffer:
            assert await buffer.pending() == []

    async def test_begun_but_unfinished_op_is_reported_as_pending(self, workspace: Path) -> None:
        async with StateBuffer(state_db_path(workspace)) as buffer:
            await buffer.begin("artifact recorded", "prd", "artifacts.prd")
        async with StateBuffer(state_db_path(workspace)) as buffer:  # survives reopen, like a crash
            pending = await buffer.pending()
        assert [(op.section, op.status) for op in pending] == [("artifacts.prd", PENDING)]

    async def test_state_db_lives_under_dot_foundry_and_is_not_committed(
        self, recorder: ProvenanceRecorder, ctx: RunContext, workspace: Path
    ) -> None:
        await recorder.record_run_start(ctx)
        assert state_db_path(workspace) == workspace / ".foundry" / "state.db"
        assert state_db_path(workspace).exists()
        assert ".foundry" not in git(workspace, "ls-files")
