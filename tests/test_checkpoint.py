from __future__ import annotations

import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from foundry.checkpoint import (
    CheckpointHandler,
    CheckpointResult,
    EditorCheckpoint,
    resolve_editor_argv,
)
from foundry.context import (
    Artifact,
    ArtifactType,
    EnvFailure,
    FailureCategory,
    RunContext,
    utc_now,
)
from foundry.orchestrator import Orchestrator
from foundry.provenance import ProvenanceRecorder

MakeEditor = Callable[[str], list[str]]

APPEND_EDIT = "path.write_bytes(path.read_bytes() + b'edit\\n')"


def sections(workspace: Path) -> dict:
    return tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))


@pytest.fixture
def prd() -> Artifact:
    return Artifact("# PRD\n\nThe product.\n", ArtifactType.PRD)


class TestProtocol:
    def test_editor_checkpoint_is_a_checkpoint_handler(self) -> None:
        assert isinstance(EditorCheckpoint(), CheckpointHandler)

    async def test_any_class_with_present_is_swappable(self) -> None:
        class AutoApprove:
            async def present(
                self, artifact: Artifact, metadata: Mapping[str, Any] | None = None
            ) -> CheckpointResult:
                now = utc_now()
                return CheckpointResult(artifact, artifact.sha256_hash, now, now)

        handler = AutoApprove()
        assert isinstance(handler, CheckpointHandler)
        result = await handler.present(Artifact("x", ArtifactType.PRD))
        assert result.approved_without_edits


class TestEditorResolution:
    def test_uses_editor_env_with_arguments(self) -> None:
        assert resolve_editor_argv({"EDITOR": "code --wait"}, "linux") == ["code", "--wait"]

    def test_falls_back_to_vi_on_unix(self) -> None:
        assert resolve_editor_argv({}, "linux") == ["vi"]
        assert resolve_editor_argv({"EDITOR": "  "}, "darwin") == ["vi"]

    def test_falls_back_to_notepad_on_windows(self) -> None:
        assert resolve_editor_argv({}, "win32") == ["notepad"]

    def test_windows_paths_with_spaces_and_backslashes_survive(self) -> None:
        argv = resolve_editor_argv({"EDITOR": r'"C:\Program Files\Editor\ed.exe" --wait'}, "win32")
        assert argv == [r"C:\Program Files\Editor\ed.exe", "--wait"]

    def test_reads_the_real_environment_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EDITOR", "nano")
        assert resolve_editor_argv() == ["nano"]


class TestEditorCheckpoint:
    async def test_editor_receives_the_artifact_content_in_a_temp_file(
        self, make_editor: MakeEditor, tmp_path: Path, prd: Artifact
    ) -> None:
        seen = tmp_path / "seen.txt"
        editor = make_editor(f"pathlib.Path({str(seen)!r}).write_bytes(path.read_bytes())")
        await EditorCheckpoint(editor).present(prd, {"phase": "prd"})
        assert seen.read_bytes() == prd.content.encode()

    async def test_temp_file_is_removed_afterwards(
        self, make_editor: MakeEditor, tmp_path: Path, prd: Artifact
    ) -> None:
        where = tmp_path / "where.txt"
        editor = make_editor(f"pathlib.Path({str(where)!r}).write_text(str(path))")
        await EditorCheckpoint(editor).present(prd, {"phase": "prd"})
        used = Path(where.read_text())
        assert used.name == "prd.md"
        assert not used.exists()

    async def test_unedited_artifact_is_detected_and_returned_as_is(
        self, make_editor: MakeEditor, prd: Artifact
    ) -> None:
        result = await EditorCheckpoint(make_editor("pass")).present(prd, {"phase": "prd"})
        assert result.approved_without_edits is True
        assert result.artifact is prd
        assert result.artifact_hash == result.presented_hash == prd.sha256_hash

    async def test_edited_artifact_is_read_back_and_rehashed(
        self, make_editor: MakeEditor, prd: Artifact
    ) -> None:
        editor = make_editor("path.write_bytes(path.read_bytes() + b'## Added by human\\n')")
        result = await EditorCheckpoint(editor).present(prd, {"phase": "prd"})
        assert result.approved_without_edits is False
        assert result.artifact.content == prd.content + "## Added by human\n"
        assert result.artifact.type is ArtifactType.PRD
        assert result.artifact_hash != prd.sha256_hash
        assert result.presented_hash == prd.sha256_hash

    async def test_content_without_trailing_newline_is_not_flagged_as_edited(
        self, make_editor: MakeEditor
    ) -> None:
        bare = Artifact("no trailing newline", ArtifactType.SPEC)
        result = await EditorCheckpoint(make_editor("pass")).present(bare, {"phase": "spec"})
        assert result.approved_without_edits is True

    async def test_blocks_until_the_editor_exits(self, make_editor: MakeEditor, prd: Artifact) -> None:
        editor = make_editor("time.sleep(0.5)\npath.write_bytes(path.read_bytes() + b'late edit\\n')")
        result = await EditorCheckpoint(editor).present(prd, {"phase": "prd"})
        assert "late edit" in result.artifact.content
        assert (result.editor_close - result.editor_open).total_seconds() >= 0.5

    async def test_records_open_and_close_timestamps(self, make_editor: MakeEditor, prd: Artifact) -> None:
        before = utc_now()
        result = await EditorCheckpoint(make_editor("pass")).present(prd, {"phase": "prd"})
        after = utc_now()
        assert before <= result.editor_open <= result.editor_close <= after
        assert result.editor_open.tzinfo is not None

    async def test_unicode_survives_the_round_trip(self, make_editor: MakeEditor) -> None:
        art = Artifact("café — ☕ 日本語\n", ArtifactType.PRD)
        result = await EditorCheckpoint(make_editor("pass")).present(art, {"phase": "prd"})
        assert result.approved_without_edits

    async def test_missing_editor_is_an_environment_failure(self, prd: Artifact) -> None:
        with pytest.raises(EnvFailure) as info:
            await EditorCheckpoint(["definitely-not-an-editor-xyz"]).present(prd, {"phase": "prd"})
        assert info.value.category is FailureCategory.ENVIRONMENT
        assert "EDITOR" in info.value.message

    async def test_nonzero_editor_exit_is_not_an_approval(
        self, make_editor: MakeEditor, prd: Artifact
    ) -> None:
        with pytest.raises(EnvFailure, match="exited with status 3"):
            await EditorCheckpoint(make_editor("sys.exit(3)")).present(prd, {"phase": "prd"})

    async def test_editor_that_saves_invalid_utf8_is_an_environment_failure(
        self, make_editor: MakeEditor, prd: Artifact
    ) -> None:
        editor = make_editor("path.write_bytes(b'\\xff\\xfe\\xfa')")
        with pytest.raises(EnvFailure, match="read the artifact back"):
            await EditorCheckpoint(editor).present(prd, {"phase": "prd"})

    async def test_uses_editor_env_when_no_explicit_editor(
        self, make_editor: MakeEditor, prd: Artifact, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv = make_editor("path.write_bytes(path.read_bytes() + b'via env\\n')")
        if sys.platform == "win32":
            monkeypatch.setenv("EDITOR", subprocess.list2cmdline(argv))
        else:
            import shlex

            monkeypatch.setenv("EDITOR", shlex.join(argv))
        result = await EditorCheckpoint().present(prd, {"phase": "prd"})
        assert "via env" in result.artifact.content


class TestLockRecording:
    async def test_unedited_approval_is_logged_in_the_lock(
        self, workspace: Path, make_editor: MakeEditor, prd: Artifact
    ) -> None:
        recorder = ProvenanceRecorder(workspace)
        await recorder.record_run_start(RunContext(idea="x", workspace_path=workspace))
        result = await EditorCheckpoint(make_editor("pass")).present(prd, {"phase": "prd"})
        await recorder.record_checkpoint("prd", result)

        entry = sections(workspace)["checkpoints"]["prd"]
        assert entry["approved_without_edits"] is True
        assert entry["artifact_hash"] == prd.sha256_hash
        assert entry["editor_open"].endswith("Z") and entry["editor_close"].endswith("Z")
        assert "approved_without_edits = true" in (workspace / "foundry.lock").read_text(encoding="utf-8")

    async def test_edited_approval_is_logged_with_both_hashes(
        self, workspace: Path, make_editor: MakeEditor, prd: Artifact
    ) -> None:
        recorder = ProvenanceRecorder(workspace)
        await recorder.record_run_start(RunContext(idea="x", workspace_path=workspace))
        result = await EditorCheckpoint(make_editor(APPEND_EDIT)).present(prd, {"phase": "prd"})
        await recorder.record_checkpoint("prd", result)

        entry = sections(workspace)["checkpoints"]["prd"]
        assert entry["approved_without_edits"] is False
        assert entry["presented_hash"] == prd.sha256_hash
        assert entry["artifact_hash"] == result.artifact.sha256_hash != prd.sha256_hash

    async def test_checkpoint_commit_follows_the_message_format(
        self, workspace: Path, make_editor: MakeEditor, prd: Artifact
    ) -> None:
        recorder = ProvenanceRecorder(workspace)
        await recorder.record_run_start(RunContext(idea="x", workspace_path=workspace))
        result = await EditorCheckpoint(make_editor("pass")).present(prd, {"phase": "prd"})
        await recorder.record_checkpoint("prd", result)
        subject = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        assert subject == "foundry: checkpoint approved — prd (no edits)"


class TestOrchestratorIntegration:
    async def test_edited_artifact_is_what_the_next_phase_receives(
        self, workspace: Path, make_editor: MakeEditor
    ) -> None:
        received: list[str] = []

        class Draft:
            name, checkpoint = "draft", True

            async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
                return Artifact("draft v1\n", ArtifactType.PRD)

        class Next:
            name, checkpoint = "next", False

            async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
                received.append(input.content)
                return input

        editor = make_editor("path.write_bytes(b'human rewrite\\n')")
        await Orchestrator([Draft(), Next()], checkpoint=EditorCheckpoint(editor)).run(
            RunContext(idea="x")
        )

        assert received == ["human rewrite\n"]
        data = sections(workspace)
        assert data["checkpoints"]["draft"]["approved_without_edits"] is False
        assert "next" not in data["checkpoints"]

    async def test_phases_without_checkpoint_never_open_the_editor(self, workspace: Path) -> None:
        class Boom:
            async def present(self, artifact: Artifact, metadata: Any = None) -> CheckpointResult:
                raise AssertionError("editor must not open")

        class Quiet:
            name, checkpoint = "quiet", False

            async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
                return input

        await Orchestrator([Quiet()], checkpoint=Boom()).run(RunContext(idea="x"))

    async def test_handler_failures_are_classified(self, workspace: Path) -> None:
        class Broken:
            async def present(self, artifact: Artifact, metadata: Any = None) -> CheckpointResult:
                raise RuntimeError("ui crashed")

        class Gate:
            name, checkpoint = "gate", True

            async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
                return input

        with pytest.raises(EnvFailure, match="ui crashed"):
            await Orchestrator([Gate()], checkpoint=Broken()).run(RunContext(idea="x"))
