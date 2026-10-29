"""GitOps: the sole owner of every git command and GitHub API call (AD-8).

Nothing else in Foundry runs `git`, imports PyGithub or reads GITHUB_TOKEN. Other components
hand GitOps content and domain requests ("commit these paths", "open a PR") and get back
plain data. The token is read once at construction (NFR-6), is never stored in `.git/config`
(pushes authenticate through git's environment-supplied config, not argv or the remote URL)
and is scrubbed from every error message.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from foundry.context import (
    EnvFailure,
    InfraFailure,
    classify_exception,
    sha256_of,
)

TOKEN_ENV_VAR = "GITHUB_TOKEN"
API_URL_ENV_VAR = "GITHUB_API_URL"  # same name GitHub Actions / Enterprise use
DEFAULT_API_URL = "https://api.github.com"
MAIN_BRANCH = "main"
REMOTE = "origin"


class GitError(EnvFailure):
    """A git invocation failed."""


class GitHubError(EnvFailure):
    """A GitHub API call failed in a way retrying will not fix."""


class RepoExists(GitHubError):
    """The repository name is already taken for this account."""


def commit_message(action: str, detail: str) -> str:
    """`foundry: <action> — <detail>` (FR-29)."""
    return f"foundry: {action} — {detail}"


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class RepoInfo:
    name: str
    full_name: str
    clone_url: str
    html_url: str
    default_branch: str
    private: bool


@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    url: str
    head_branch: str
    head_sha: str


@dataclass(frozen=True)
class CIResult:
    passed: bool
    checks: tuple[str, ...] = ()  # every check seen, "name: conclusion"
    failures: tuple[str, ...] = ()  # "name: conclusion (url)" for each red check


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    sha: str


# ---------------------------------------------------------------------------- git


# Wording differs between git builds and libcurl versions; all of these are the network's fault.
_GIT_NETWORK_ERROR = re.compile(
    r"Could not resolve host|Failed to connect to|Could not connect to server|"
    r"Couldn't connect to server|Connection (?:timed out|reset|refused)|Operation timed out|"
    r"SSL_ERROR_SYSCALL|RPC failed|early EOF|the remote end hung up unexpectedly|"
    r"The requested URL returned error: (?:5\d\d|429)|unable to access .*(?:50\d|timed out)",
    re.IGNORECASE,
)


async def _run_git_raw(
    workspace: Path, *args: str, env: Mapping[str, str] | None = None, secret: str | None = None
) -> bytes:
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=workspace,
            env=full_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except FileNotFoundError as exc:
        raise GitError("git was not found on PATH; install git to use Foundry") from exc
    except OSError as exc:
        raise classify_exception(exc) from exc

    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        infra = _GIT_NETWORK_ERROR.search(detail)  # before scrubbing: the secret may be anything
        if secret:
            detail = detail.replace(secret, "***")
        error_cls = InfraFailure if infra else GitError
        raise error_cls(
            f"git {args[0]} failed (exit {proc.returncode}): {detail}",
            {"args": list(args), "returncode": proc.returncode},
        )
    return stdout


async def run_git(workspace: Path, *args: str) -> str:
    """Run git in `workspace`, returning stdout. Never raises anything but FoundryError."""
    return (await _run_git_raw(workspace, *args)).decode("utf-8", errors="replace")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8"))  # bytes: no newline translation
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    except OSError as exc:
        raise EnvFailure(f"cannot write {path}: {exc}") from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ------------------------------------------------------------------------- GitOps


@dataclass
class GitOps:
    workspace: Path
    token: str | None = field(default=None, repr=False)
    api_url: str = DEFAULT_API_URL
    repo_full_name: str | None = None
    # CI polling knobs: tests shrink them; Epic 4's backoff will own anything fancier.
    ci_poll_interval: float = 10.0
    ci_timeout: float = 1800.0
    ci_no_checks_grace: float = 180.0
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, repr=False)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace)

    @classmethod
    def from_env(cls, workspace: Path, *, require_token: bool = False) -> GitOps:
        token = os.environ.get(TOKEN_ENV_VAR) or None
        if require_token and token is None:
            raise missing_token_error()
        return cls(
            workspace,
            token=token,
            api_url=os.environ.get(API_URL_ENV_VAR) or DEFAULT_API_URL,
        )

    # --------------------------------------------------------------- local git

    async def ensure_repo(self) -> None:
        if (self.workspace / ".git").exists():
            return
        try:
            await run_git(self.workspace, "rev-parse", "--git-dir")
        except GitError:
            await run_git(self.workspace, "init", "--quiet", "-b", MAIN_BRANCH)

    async def commit(self, message: str, paths: Sequence[str] | None = None) -> None:
        """Commit `paths` only, or (paths=None) everything in the working tree."""
        await self.ensure_repo()
        if paths is None:
            await run_git(self.workspace, "add", "-A")
            await run_git(self.workspace, "commit", "--quiet", "-m", message)
            return
        await run_git(self.workspace, "add", "--", *paths)
        # The pathspec keeps unrelated staged work out of this commit.
        await run_git(self.workspace, "commit", "--quiet", "-m", message, "--", *paths)

    async def stage_all(self) -> None:
        await self.ensure_repo()
        await run_git(self.workspace, "add", "-A")

    async def staged_diff_hash(self) -> str | None:
        """SHA-256 of the staged diff, or None when nothing is staged."""
        diff = await _run_git_raw(self.workspace, "diff", "--cached", "--binary")
        return sha256_of(diff) if diff else None

    async def discard_staged(self) -> None:
        await run_git(self.workspace, "reset", "--quiet")

    async def working_diff(self) -> str:
        """Every change in the working tree since the last commit, new files included."""
        await self.stage_all()
        return await run_git(self.workspace, "diff", "--cached", "--no-color")

    async def stash(self, message: str) -> None:
        """Set the working-tree changes (untracked files too) aside without losing them."""
        await run_git(self.workspace, "stash", "push", "--quiet", "--include-untracked", "-m", message)

    async def is_clean(self) -> bool:
        await self.ensure_repo()
        return not (await run_git(self.workspace, "status", "--porcelain")).strip()

    async def dirty_files(self) -> list[str]:
        out = await run_git(self.workspace, "status", "--porcelain")
        return [line[3:] for line in out.splitlines() if line.strip()]

    async def current_branch(self) -> str:
        return (await run_git(self.workspace, "rev-parse", "--abbrev-ref", "HEAD")).strip()

    async def head_sha(self) -> str:
        return (await run_git(self.workspace, "rev-parse", "HEAD")).strip()

    async def has_remote(self, name: str = REMOTE) -> bool:
        return name in (await run_git(self.workspace, "remote")).split()

    async def user_name(self) -> str | None:
        try:
            name = (await run_git(self.workspace, "config", "--get", "user.name")).strip()
        except GitError:
            return None
        return name or None

    def write_files(self, files: Mapping[str, str]) -> list[str]:
        """Write scaffold content into the working tree; returns the repo-relative paths."""
        root = self.workspace.resolve()
        written: list[str] = []
        for relative, content in files.items():
            target = (root / relative).resolve()
            if root != target and root not in target.parents:
                raise EnvFailure(f"refusing to write outside the repository: {relative}")
            _atomic_write(target, content)
            written.append(Path(relative).as_posix())
        return written

    async def prepare_for_scaffold(self) -> None:
        """The workspace must not already be wired to a remote; its branch becomes `main`."""
        await self.ensure_repo()
        if await self.has_remote():
            raise EnvFailure(
                f"this directory already has a git remote ({REMOTE}); Foundry creates a new "
                "repository and needs a directory that is not attached to one."
            )
        if await self.current_branch() != MAIN_BRANCH:
            await run_git(self.workspace, "branch", "-M", MAIN_BRANCH)

    async def create_branch(self, name: str, base: str = MAIN_BRANCH) -> None:
        await run_git(self.workspace, "checkout", "--quiet", "-b", name, base)

    async def checkout(self, branch: str) -> None:
        await run_git(self.workspace, "checkout", "--quiet", branch)

    async def sync_branch(self, branch: str) -> None:
        """Check out `branch` and fast-forward it to its remote state."""
        await self.checkout(branch)
        await self._remote_git("pull", "--quiet", "--ff-only", REMOTE, branch)

    # ------------------------------------------------------------------ remote

    async def add_remote(self, url: str) -> None:
        await run_git(self.workspace, "remote", "add", REMOTE, url)

    async def push(self, branch: str, *, set_upstream: bool = True) -> None:
        args = ["push", "--quiet"]
        if set_upstream:
            args.append("--set-upstream")
        await self._remote_git(*args, REMOTE, branch)

    async def _remote_git(self, *args: str) -> None:
        token = self._require_token()
        credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env = {  # in the environment, not argv or .git/config
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
        }
        try:
            await _run_git_raw(self.workspace, *args, env=env, secret=token)
        except EnvFailure as exc:
            exc.message = exc.message.replace(credentials, "***")
            raise

    # ------------------------------------------------------------------ GitHub

    def _require_token(self) -> str:
        if not self.token:
            raise missing_token_error()
        return self.token

    def _client(self) -> Any:
        from github import Auth, Github

        # retry=None: backoff on infrastructure failures is Foundry's job (FR-22), not PyGithub's.
        return Github(
            auth=Auth.Token(self._require_token()),
            base_url=self.api_url.rstrip("/"),
            retry=None,
            timeout=30,
        )

    async def _github(self, call: Callable[[Any], Any], what: str) -> Any:
        """Run a blocking PyGithub call off the event loop; raise only classified errors."""
        try:
            return await asyncio.to_thread(lambda: call(self._client()))
        except Exception as exc:
            raise classify_github_error(exc, what, self.token) from exc

    async def create_repo(
        self, name: str, description: str = "", *, private: bool = False
    ) -> RepoInfo:
        def call(gh: Any) -> RepoInfo:
            repo = gh.get_user().create_repo(
                name, description=description[:350], private=private, auto_init=False
            )
            return RepoInfo(
                name=repo.name,
                full_name=repo.full_name,
                clone_url=repo.clone_url,
                html_url=repo.html_url,
                default_branch=repo.default_branch or MAIN_BRANCH,
                private=bool(repo.private),
            )

        info: RepoInfo = await self._github(call, f"create repository {name!r}")
        self.repo_full_name = info.full_name
        return info

    async def create_pr(
        self, head: str, base: str, title: str, body: str = ""
    ) -> PullRequestInfo:
        full_name = self._require_repo()

        def call(gh: Any) -> PullRequestInfo:
            repo = gh.get_repo(full_name)
            # A retry after a timeout must not open a second PR if the first one got through.
            for existing in repo.get_pulls(state="open", base=base):
                if existing.head.ref == head:
                    return PullRequestInfo(existing.number, existing.html_url, head, existing.head.sha)
            pr = repo.create_pull(title=title, body=body, head=head, base=base)
            return PullRequestInfo(pr.number, pr.html_url, head, pr.head.sha)

        return await self._github(call, f"open a pull request from {head!r}")

    async def pull_request(self, number: int) -> PullRequestInfo:
        """The PR as it is now: its head moves when someone pushes to the branch."""
        full_name = self._require_repo()

        def call(gh: Any) -> PullRequestInfo:
            pr = gh.get_repo(full_name).get_pull(number)
            return PullRequestInfo(pr.number, pr.html_url, pr.head.ref, pr.head.sha)

        return await self._github(call, f"read pull request #{number}")

    async def poll_ci(self, sha: str) -> CIResult:
        """Wait for every check on `sha` to finish; red if any failed, InfraFailure on timeout."""
        started = self.clock()
        while True:
            checks = await self._ci_checks(sha)
            if checks and all(conclusion is not None for _, conclusion, _, _ in checks):
                return _summarise_checks(checks)
            elapsed = self.clock() - started
            if not checks and elapsed >= self.ci_no_checks_grace:
                raise EnvFailure(
                    f"no CI checks appeared for {sha[:7]} after {self.ci_no_checks_grace:.0f}s; "
                    "check that .github/workflows/ci.yml is on main and Actions is enabled."
                )
            if elapsed >= self.ci_timeout:
                raise InfraFailure(f"CI did not finish within {self.ci_timeout:.0f}s for {sha[:7]}")
            await self.sleep(self.ci_poll_interval)

    async def _ci_checks(self, sha: str) -> list[tuple[str, str | None, str, str]]:
        """(name, conclusion-or-None-if-running, url, source) for every check and status."""
        full_name = self._require_repo()

        def call(gh: Any) -> list[tuple[str, str | None, str, str]]:
            commit = gh.get_repo(full_name).get_commit(sha)
            found: list[tuple[str, str | None, str, str]] = []
            for run in commit.get_check_runs():
                done = run.status == "completed"
                found.append((run.name, (run.conclusion or "failure") if done else None,
                              run.html_url or "", "check"))
            combined = commit.get_combined_status()
            for status in combined.statuses:
                state = status.state
                found.append((status.context, None if state == "pending" else state,
                              status.target_url or "", "status"))
            return found

        return await self._github(call, f"read CI checks for {sha[:7]}")

    async def merge_pr(self, number: int, title: str) -> MergeResult:
        full_name = self._require_repo()

        def call(gh: Any) -> MergeResult:
            status = gh.get_repo(full_name).get_pull(number).merge(
                commit_title=title, merge_method="merge"
            )
            return MergeResult(bool(status.merged), status.sha or "")

        result: MergeResult = await self._github(call, f"merge pull request #{number}")
        if not result.merged:
            raise GitHubError(f"GitHub did not merge pull request #{number}")
        return result

    def _require_repo(self) -> str:
        if not self.repo_full_name:
            raise EnvFailure("no GitHub repository is attached to this run yet")
        return self.repo_full_name


def _summarise_checks(checks: list[tuple[str, str | None, str, str]]) -> CIResult:
    ok = {"success", "neutral", "skipped"}
    failures = tuple(
        f"{name}: {conclusion}" + (f" ({url})" if url else "")
        for name, conclusion, url, _ in checks
        if conclusion not in ok
    )
    return CIResult(
        passed=not failures,
        checks=tuple(f"{name}: {conclusion}" for name, conclusion, _, _ in checks),
        failures=failures,
    )


def missing_token_error() -> EnvFailure:
    return EnvFailure(
        f"{TOKEN_ENV_VAR} is not set. Foundry creates and updates a GitHub repository, and reads "
        f"the token only from the {TOKEN_ENV_VAR} environment variable (a token with `repo` and "
        "`workflow` scope)."
    )


def classify_github_error(exc: BaseException, what: str, token: str | None = None) -> Exception:
    """Map PyGithub / requests failures onto the failure taxonomy (AD-3); scrub the token."""
    from foundry.context import FoundryError

    if isinstance(exc, FoundryError):
        return exc

    def scrub(text: str) -> str:
        return text.replace(token, "***") if token else text

    try:
        import requests
        from github import GithubException, RateLimitExceededException
    except ImportError:  # pragma: no cover - PyGithub is a hard dependency
        return classify_exception(exc)

    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return InfraFailure(scrub(f"could not reach GitHub to {what}: {exc}"))
    if isinstance(exc, GithubException):
        status = exc.status
        data = exc.data if isinstance(exc.data, dict) else {}
        detail = scrub(str(data.get("message") or exc.data or "no detail"))
        errors = scrub(str(data.get("errors") or ""))
        if isinstance(exc, RateLimitExceededException) or (
            status in (403, 429) and "rate limit" in detail.lower()
        ):
            return InfraFailure(f"GitHub rate limit hit while trying to {what}: {detail}")
        if status == 429 or status >= 500:
            return InfraFailure(f"GitHub failed (HTTP {status}) while trying to {what}: {detail}")
        if status == 401:
            return GitHubError(
                f"GitHub rejected {TOKEN_ENV_VAR} while trying to {what}: {detail}. "
                "Check that the token is valid and has not expired."
            )
        if status == 403:
            return GitHubError(
                f"GitHub refused to {what}: {detail}. The token needs `repo` and `workflow` scope."
            )
        if status == 404:
            return GitHubError(
                f"GitHub could not find the resource needed to {what}: {detail}. "
                "A missing repository also shows up as 404 when the token cannot see it."
            )
        if status == 422 and "already exists" in f"{detail} {errors}".lower():
            return RepoExists(f"cannot {what}: the name is already taken ({detail})")
        return GitHubError(f"GitHub rejected the request to {what} (HTTP {status}): {detail} {errors}".strip())
    return classify_exception(exc)
