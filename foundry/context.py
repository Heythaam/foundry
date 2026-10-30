"""Core data types shared by every layer: RunContext, Artifact, and the error taxonomy."""

from __future__ import annotations

import errno
import hashlib
import os
import socket
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "claude-sonnet-4-5"
MODEL_ENV_VAR = "FOUNDRY_MODEL"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(moment: datetime) -> str:
    """ISO 8601 UTC with a Z suffix, e.g. 2026-10-28T14:30:00Z."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_of(content: str | bytes) -> str:
    """`sha256:<hex>` digest, the only hash format used anywhere in Foundry."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


# --------------------------------------------------------------------------- errors


class FailureCategory(str, Enum):
    CODE = "code"
    INFRASTRUCTURE = "infrastructure"
    ENVIRONMENT = "environment"


class FoundryError(Exception):
    """Base for every error that is allowed to leave a component boundary."""

    category: FailureCategory = FailureCategory.ENVIRONMENT

    def __init__(
        self, message: str, context: dict[str, Any] | None = None, *, fix: str | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.context = context or {}
        self.fix = fix  # what the human should do about it, when we know
        self.source: str | None = None  # which external call failed, once classified
        self.phase: str | None = None  # which phase raised the error, when known
        self.story: str | None = None  # which story raised the error, when known


def format_failure_line(err: FoundryError) -> str:
    """Return a single formatted failure line: `[<phase> / <story>] failed: <reason>`.

    Both fields are always shown; `-` is used as a placeholder when phase or story is None.
    When `fix` is set, the hint appears on the next line. The `[...] failed: ...` part
    never contains an embedded newline so the caller can safely write it as the final stderr line.
    """
    phase = err.phase or "-"
    story = err.story or "-"
    line = f"[{phase} / {story}] failed: {err.message.splitlines()[0]}"
    if err.fix:
        line = f"{line}\nTo fix: {err.fix}"
    return line


class CodeFailure(FoundryError):
    category = FailureCategory.CODE


class InfraFailure(FoundryError):
    category = FailureCategory.INFRASTRUCTURE


class EnvFailure(FoundryError):
    category = FailureCategory.ENVIRONMENT


_DISK_FULL_ERRNOS = {errno.ENOSPC, errno.EDQUOT}
_PERMISSION_ERRNOS = {errno.EACCES, errno.EPERM, errno.EROFS}


def classify_exception(exc: BaseException, source: str | None = None) -> FoundryError:
    """Turn any exception raised by an external call into a categorised FoundryError (AD-3).

    The category is attached to the error itself, and `source` names the call that failed.
      * infrastructure: timeouts, connection and DNS failures, a subprocess that timed out;
      * environment: a full disk, a permission problem, and anything not recognised, because
        a hard stop is the safest default.
    Code failures are never inferred from a bare exception: they are raised as CodeFailure by
    the component that can tell a wrong answer from a broken environment.
    """
    if isinstance(exc, FoundryError):
        error = exc
    elif isinstance(
        exc, (TimeoutError, ConnectionError, socket.gaierror, subprocess.TimeoutExpired)
    ):
        error = InfraFailure(f"{type(exc).__name__}: {exc}")
        error.__cause__ = exc
    elif isinstance(exc, OSError) and exc.errno in _DISK_FULL_ERRNOS:
        error = EnvFailure(
            f"the disk is full ({exc})", fix="Free up disk space, then resume the run."
        )
        error.__cause__ = exc
    elif isinstance(exc, OSError) and exc.errno in _PERMISSION_ERRNOS:
        target = f" {exc.filename!r}" if exc.filename else ""
        error = EnvFailure(
            f"permission denied{target}: {exc.strerror or exc}",
            fix="Check that the current user can write to the project directory.",
        )
        error.__cause__ = exc
    else:
        error = EnvFailure(f"{type(exc).__name__}: {exc}")
        error.__cause__ = exc
    if source and error.source is None:
        error.source = source
    return error


class ModelAliasError(EnvFailure):
    """FOUNDRY_MODEL names an alias rather than a pinned model version."""


def check_model_alias(model_id: str) -> None:
    """Refuse model IDs whose last segment carries no digit (e.g. `claude-sonnet`).

    A run must record the exact model it used (FR-26); an alias would silently drift.
    """
    last_segment = model_id.rsplit("-", 1)[-1]
    if not any(char.isdigit() for char in last_segment):
        raise ModelAliasError(
            f"Warning: model {model_id!r} has no version suffix, so it may resolve to a "
            f"different model later. Set {MODEL_ENV_VAR} to a pinned model ID whose last "
            f"segment contains a version or date (default: {DEFAULT_MODEL}).",
            {"model_id": model_id},
        )


# ------------------------------------------------------------------------ artifacts


class ArtifactType(str, Enum):
    IDEA = "idea"
    BRAINSTORM = "brainstorm"
    PRD = "prd"
    PREFLIGHT = "preflight"
    ARCHITECTURE = "architecture"
    EPICS = "epics"
    SPEC = "spec"
    EPIC_REVIEW = "epic_review"
    ESCALATION = "escalation"
    STUB = "stub"


@dataclass(frozen=True)
class Artifact:
    """What flows between phases. Immutable: an edit produces a new Artifact."""

    content: str
    type: ArtifactType
    sha256_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sha256_hash", sha256_of(self.content))


# ---------------------------------------------------------------------- run context


def default_model_id() -> str:
    return os.environ.get(MODEL_ENV_VAR) or DEFAULT_MODEL


@dataclass
class RunContext:
    idea: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    workspace_path: Path = field(default_factory=Path.cwd)
    model_id: str = field(default_factory=default_model_id)
    start_time: datetime = field(default_factory=utc_now)
    review_epics: bool = False  # --review-epics: checkpoint before each epic (FR-6)
    mode: str = "pro"  # "pro" (full planning pipeline) or "quick" (spec first, FR-2)
    yes: bool = False  # --yes: skip all interactive gates; still prints readback and signals
    # Approved artifact per phase name, filled by the orchestrator only. A phase's `input` is
    # just the previous phase's output; this is how it reads an earlier one (e.g. the PRD).
    upstream: dict[str, Artifact] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.workspace_path = Path(self.workspace_path)

    @property
    def idea_hash(self) -> str:
        return sha256_of(self.idea)
