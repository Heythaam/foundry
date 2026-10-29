from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

import pytest

from foundry.context import EnvFailure, FailureCategory, RunContext, sha256_of
from foundry.lock import (
    LOCK_FILENAME,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    LockError,
    LockFile,
)


@pytest.fixture
def lock(tmp_path: Path) -> LockFile:
    return LockFile.for_workspace(tmp_path)


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    return RunContext(idea="build a todo app", workspace_path=tmp_path, model_id="claude-sonnet-4-5")


def parsed(lock: LockFile) -> dict:
    return tomllib.loads(lock.path.read_text(encoding="utf-8"))


class TestInitRun:
    def test_creates_lock_at_workspace_root(self, lock: LockFile, ctx: RunContext) -> None:
        lock.init_run(ctx)
        assert lock.path == ctx.workspace_path / LOCK_FILENAME
        assert lock.path.is_file()

    def test_run_section_has_required_fields(self, lock: LockFile, ctx: RunContext) -> None:
        lock.init_run(ctx)
        run = parsed(lock)["run"]
        assert run["run_id"] == ctx.run_id
        assert run["idea"] == "build a todo app"
        assert run["idea_hash"] == sha256_of("build a todo app")
        assert run["model"] == "claude-sonnet-4-5"
        assert run["status"] == "in_progress"
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", run["started_at"])

    def test_refuses_to_reinitialise_an_existing_run(self, lock: LockFile, ctx: RunContext) -> None:
        lock.init_run(ctx)
        before = lock.path.read_bytes()
        with pytest.raises(LockError, match="already"):
            lock.init_run(RunContext(idea="other", workspace_path=ctx.workspace_path))
        assert lock.path.read_bytes() == before

    @pytest.mark.parametrize(
        "idea",
        ['say "hi"', "multi\nline\nidea", "[run]\nstatus = 'done'", "tabs\tand\\backslashes", "café — ☕ 日本語"],
    )
    def test_awkward_ideas_round_trip_as_valid_toml(self, lock: LockFile, tmp_path: Path, idea: str) -> None:
        lock.init_run(RunContext(idea=idea, workspace_path=tmp_path))
        data = parsed(lock)
        assert data["run"]["idea"] == idea
        assert set(data) == {"run"}  # the idea could not inject a section


class TestAppendOnly:
    def test_append_preserves_every_existing_byte(self, lock: LockFile, ctx: RunContext) -> None:
        lock.init_run(ctx)
        after_init = lock.path.read_bytes()
        lock.append_section("checkpoints.brainstorm", {"artifact_hash": "sha256:abc"})
        after_append = lock.path.read_bytes()
        assert after_append.startswith(after_init)
        assert len(after_append) > len(after_init)

    def test_run_section_is_unchanged_after_later_appends(self, lock: LockFile, ctx: RunContext) -> None:
        lock.init_run(ctx)
        run_before = parsed(lock)["run"]
        lock.append_section("artifacts.stub", {"hash": "sha256:1"})
        lock.append_section("cancelled", {"at": "2026-10-28T00:00:00Z"})
        assert parsed(lock)["run"] == run_before
        assert parsed(lock)["run"]["status"] == "in_progress"

    def test_dotted_sections_nest(self, lock: LockFile, ctx: RunContext) -> None:
        lock.init_run(ctx)
        lock.append_section("implementation.epic-1.story-2", {"tests_passed": 4})
        lock.append_section("implementation.epic-1.story-3", {"tests_passed": 7})
        data = parsed(lock)
        assert data["implementation"]["epic-1"]["story-2"] == {"tests_passed": 4}
        assert data["implementation"]["epic-1"]["story-3"] == {"tests_passed": 7}

    def test_section_names_are_never_reused(self, lock: LockFile, ctx: RunContext) -> None:
        lock.init_run(ctx)
        lock.append_section("checkpoints.prd", {"artifact_hash": "sha256:1"})
        before = lock.path.read_bytes()
        with pytest.raises(LockError, match="already exists"):
            lock.append_section("checkpoints.prd", {"artifact_hash": "sha256:2"})
        assert lock.path.read_bytes() == before

    def test_valid_toml_after_every_append(self, lock: LockFile, ctx: RunContext) -> None:
        lock.init_run(ctx)
        for n in range(25):
            lock.append_section(f"artifacts.phase-{n}", {"hash": sha256_of(str(n)), "n": n})
            parsed(lock)  # raises if invalid
        assert len(parsed(lock)["artifacts"]) == 25

    def test_unserialisable_value_is_a_lock_error_and_leaves_file_alone(
        self, lock: LockFile, ctx: RunContext
    ) -> None:
        lock.init_run(ctx)
        before = lock.path.read_bytes()
        with pytest.raises(LockError):
            lock.append_section("bad", {"value": object()})
        assert lock.path.read_bytes() == before


class TestAtomicWrites:
    def test_writes_via_temp_file_in_same_directory_then_rename(
        self, lock: LockFile, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy(src: str, dst: str) -> None:
            calls.append((str(src), str(dst)))
            real_replace(src, dst)

        monkeypatch.setattr("foundry.lock.os.replace", spy)
        lock.init_run(ctx)
        assert len(calls) == 1
        src, dst = map(Path, calls[0])
        assert dst == lock.path
        assert src != dst and src.parent == dst.parent
        assert not src.exists()

    def test_failed_rename_leaves_original_intact_and_no_temp_files(
        self, lock: LockFile, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock.init_run(ctx)
        before = lock.path.read_bytes()

        def boom(src: str, dst: str) -> None:
            raise PermissionError("locked by another process")

        monkeypatch.setattr("foundry.lock.os.replace", boom)
        with pytest.raises(LockError):
            lock.append_section("artifacts.x", {"hash": "sha256:1"})
        assert lock.path.read_bytes() == before
        assert sorted(p.name for p in lock.path.parent.iterdir()) == [LOCK_FILENAME]
        parsed(lock)

    def test_crash_before_rename_never_exposes_a_partial_file(
        self, lock: LockFile, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock.init_run(ctx)
        before = lock.path.read_bytes()
        monkeypatch.setattr("foundry.lock.os.fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
        with pytest.raises(LockError):
            lock.append_section("artifacts.x", {"hash": "sha256:1"})
        assert lock.path.read_bytes() == before

    def test_write_errors_are_classified_as_environment_failures(
        self, lock: LockFile, ctx: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("foundry.lock.tempfile.mkstemp", lambda **kw: (_ for _ in ()).throw(OSError("full")))
        with pytest.raises(EnvFailure) as info:
            lock.init_run(ctx)
        assert info.value.category is FailureCategory.ENVIRONMENT
        assert not lock.exists()

    def test_corrupt_existing_lock_is_refused_not_overwritten(self, lock: LockFile) -> None:
        lock.path.write_text("this is [not toml", encoding="utf-8")
        with pytest.raises(LockError, match="not valid TOML"):
            lock.append_section("artifacts.x", {"hash": "sha256:1"})
        assert lock.path.read_text(encoding="utf-8") == "this is [not toml"


class TestStatus:
    def test_no_lock_means_no_run(self, lock: LockFile) -> None:
        assert lock.status() is None
        assert not lock.is_in_progress()

    def test_fresh_run_is_in_progress(self, lock: LockFile, ctx: RunContext) -> None:
        lock.init_run(ctx)
        assert lock.status() == STATUS_IN_PROGRESS
        assert lock.is_in_progress()

    def test_terminal_sections_end_the_run_without_editing_run(self, lock: LockFile, ctx: RunContext) -> None:
        lock.init_run(ctx)
        lock.append_section("cancelled", {"at": "2026-10-28T00:00:00Z"})
        assert lock.status() == STATUS_CANCELLED and not lock.is_in_progress()

        other = LockFile(lock.path.parent / "other.lock")
        other.init_run(ctx)
        other.append_section("completed", {"at": "2026-10-28T00:00:00Z"})
        assert other.status() == STATUS_COMPLETED and not other.is_in_progress()

    def test_unreadable_lock_raises_instead_of_guessing(self, lock: LockFile) -> None:
        lock.path.write_text("[run\n", encoding="utf-8")
        with pytest.raises(LockError):
            lock.status()
