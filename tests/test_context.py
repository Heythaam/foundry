from __future__ import annotations

import hashlib
import uuid
from dataclasses import FrozenInstanceError
from datetime import timedelta
from pathlib import Path

import pytest

from foundry.context import (
    DEFAULT_MODEL,
    Artifact,
    ArtifactType,
    CodeFailure,
    EnvFailure,
    FailureCategory,
    FoundryError,
    InfraFailure,
    ModelAliasError,
    RunContext,
    check_model_alias,
    classify_exception,
    iso_utc,
    sha256_of,
    utc_now,
)
from foundry.phases import Phase, StubPhase


class TestArtifact:
    def test_hash_is_computed_on_init_with_sha256_prefix(self) -> None:
        artifact = Artifact(content="hello", type=ArtifactType.PRD)
        assert artifact.sha256_hash == "sha256:" + hashlib.sha256(b"hello").hexdigest()

    def test_hash_covers_content_exactly(self) -> None:
        a = Artifact("hello", ArtifactType.PRD)
        b = Artifact("hello ", ArtifactType.PRD)
        assert a.sha256_hash != b.sha256_hash
        assert a.sha256_hash == Artifact("hello", ArtifactType.BRAINSTORM).sha256_hash

    def test_unicode_content_hashes_as_utf8(self) -> None:
        artifact = Artifact("café — ☕", ArtifactType.SPEC)
        assert artifact.sha256_hash == sha256_of("café — ☕".encode("utf-8"))

    def test_hash_cannot_be_passed_in_or_reassigned(self) -> None:
        with pytest.raises(TypeError):
            Artifact("x", ArtifactType.PRD, sha256_hash="sha256:forged")  # type: ignore[call-arg]
        artifact = Artifact("x", ArtifactType.PRD)
        with pytest.raises(FrozenInstanceError):
            artifact.content = "y"  # type: ignore[misc]


class TestRunContext:
    def test_run_id_is_a_uuid4_and_unique(self) -> None:
        first, second = RunContext(idea="a"), RunContext(idea="a")
        assert uuid.UUID(first.run_id).version == 4
        assert first.run_id != second.run_id

    def test_workspace_is_a_path_and_defaults_to_cwd(self, workspace: Path) -> None:
        assert RunContext(idea="a").workspace_path == Path.cwd()
        assert RunContext(idea="a", workspace_path="somewhere").workspace_path == Path("somewhere")  # type: ignore[arg-type]

    def test_model_defaults_when_env_unset(self) -> None:
        assert RunContext(idea="a").model_id == DEFAULT_MODEL == "claude-sonnet-4-5"

    def test_model_comes_from_foundry_model_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FOUNDRY_MODEL", "claude-opus-4-1-20250805")
        assert RunContext(idea="a").model_id == "claude-opus-4-1-20250805"

    def test_empty_model_env_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FOUNDRY_MODEL", "")
        assert RunContext(idea="a").model_id == DEFAULT_MODEL

    def test_start_time_is_timezone_aware_utc_and_recent(self) -> None:
        ctx = RunContext(idea="a")
        assert ctx.start_time.utcoffset() == timedelta(0)
        assert abs(utc_now() - ctx.start_time) < timedelta(seconds=5)

    def test_idea_hash(self) -> None:
        assert RunContext(idea="build a todo app").idea_hash == sha256_of("build a todo app")

    def test_iso_utc_format(self) -> None:
        assert iso_utc(utc_now()).endswith("Z")
        assert len(iso_utc(utc_now())) == len("2026-10-28T14:30:00Z")


class TestErrorTaxonomy:
    def test_categories(self) -> None:
        assert CodeFailure("x").category is FailureCategory.CODE
        assert InfraFailure("x").category is FailureCategory.INFRASTRUCTURE
        assert EnvFailure("x").category is FailureCategory.ENVIRONMENT

    def test_foundry_errors_pass_through_classification(self) -> None:
        err = InfraFailure("rate limited", {"status": 429})
        assert classify_exception(err) is err
        assert err.context == {"status": 429}

    @pytest.mark.parametrize("exc", [TimeoutError("t"), ConnectionRefusedError("c")])
    def test_timeouts_and_connection_errors_are_infrastructure(self, exc: Exception) -> None:
        assert classify_exception(exc).category is FailureCategory.INFRASTRUCTURE

    @pytest.mark.parametrize("exc", [OSError("disk full"), ValueError("weird"), KeyError("k")])
    def test_everything_else_defaults_to_environment(self, exc: Exception) -> None:
        classified = classify_exception(exc)
        assert isinstance(classified, FoundryError)
        assert classified.category is FailureCategory.ENVIRONMENT
        assert classified.__cause__ is exc


class TestModelAliasGuard:
    @pytest.mark.parametrize(
        "model_id",
        [
            "claude-sonnet-4-5",
            "claude-sonnet-4-5-20250929",
            "claude-opus-4-1-20250805",
            "claude-sonnet-5-5-20261001",
            "gpt4",
        ],
    )
    def test_versioned_ids_pass(self, model_id: str) -> None:
        check_model_alias(model_id)

    @pytest.mark.parametrize(
        "model_id", ["claude-sonnet", "claude-sonnet-latest", "sonnet", "claude-opus-4-latest", ""]
    )
    def test_aliases_are_rejected_as_environment_failures(self, model_id: str) -> None:
        with pytest.raises(ModelAliasError) as info:
            check_model_alias(model_id)
        assert info.value.category is FailureCategory.ENVIRONMENT
        assert "version suffix" in info.value.message
        assert "FOUNDRY_MODEL" in info.value.message

    def test_the_default_model_is_itself_pinned(self) -> None:
        check_model_alias(DEFAULT_MODEL)


class TestPhaseProtocol:
    def test_stub_phase_satisfies_protocol(self) -> None:
        assert isinstance(StubPhase(), Phase)

    async def test_run_takes_artifact_and_context_and_returns_artifact(self) -> None:
        ctx = RunContext(idea="build a todo app")
        source = Artifact("build a todo app", ArtifactType.IDEA)
        out = await StubPhase().run(source, ctx)
        assert isinstance(out, Artifact)
        assert "build a todo app" in out.content
        assert source.sha256_hash in out.content
