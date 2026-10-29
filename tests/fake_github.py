"""A local stand-in for the GitHub REST API, backed by real bare git repositories.

PyGithub talks to it over real HTTP (`GITHUB_API_URL`), and `clone_url` is a local bare repo,
so pushes, branches and merges in the tests exercise real git. Only the routes Foundry uses
exist. `requests` records (method, path, authorization) for every call.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

OWNER = "octocat"
TOKEN = "ghp_test_token_0123456789"

CheckSpec = dict[str, Any]


def passing(name: str = "test") -> CheckSpec:
    return {"name": name, "status": "completed", "conclusion": "success"}


def failing(name: str = "test", conclusion: str = "failure") -> CheckSpec:
    return {"name": name, "status": "completed", "conclusion": conclusion}


def running(name: str = "test") -> CheckSpec:
    return {"name": name, "status": "in_progress", "conclusion": None}


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout.strip()


class FakeGitHub:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repos: dict[str, dict[str, Any]] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, str]] = []
        self.fail_next: list[tuple[str, int, dict[str, Any]]] = []  # (route substring, status, body)
        # What CI reports per poll (one poll = one check-runs read + one status read);
        # the last entry repeats. `ci_calls` counts completed polls.
        self.ci: list[list[CheckSpec]] = [[passing()]]
        self.ci_calls = 0
        self.merge_refused: str | None = None
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # ----------------------------------------------------------------- lifecycle

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> FakeGitHub:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # ------------------------------------------------------------------- helpers

    def bare(self, name: str) -> Path:
        return self.repos[name]["bare"]

    def remote_branches(self, name: str) -> list[str]:
        out = _git(self.bare(name), "for-each-ref", "--format=%(refname:short)", "refs/heads")
        return out.splitlines()

    def remote_files(self, name: str, ref: str = "main") -> list[str]:
        return _git(self.bare(name), "ls-tree", "-r", "--name-only", ref).splitlines()

    def remote_show(self, name: str, ref: str, path: str) -> str:
        return _git(self.bare(name), "show", f"{ref}:{path}")

    def remote_log(self, name: str, ref: str = "main", fmt: str = "%s") -> list[str]:
        return _git(self.bare(name), "log", f"--format={fmt}", ref).splitlines()

    def fail(self, route_contains: str, status: int, message: str = "boom", **extra: Any) -> None:
        """Make the next request whose path contains `route_contains` fail with `status`."""
        self.fail_next.append((route_contains, status, {"message": message, **extra}))

    # -------------------------------------------------------------------- routes

    def _repo_json(self, name: str) -> dict[str, Any]:
        record = self.repos[name]
        full = f"{OWNER}/{name}"
        return {
            "id": 1,
            "name": name,
            "full_name": full,
            "private": record["private"],
            "html_url": f"https://github.com/{full}",
            "clone_url": record["bare"].as_posix(),
            "url": f"{self.url}/repos/{full}",
            "default_branch": "main",
        }

    def _pull_json(self, number: int) -> dict[str, Any]:
        pull = self.pulls[number]
        full = f"{OWNER}/{pull['repo']}"
        return {
            "number": number,
            "state": "closed" if pull["merged"] else "open",
            "merged": pull["merged"],
            "title": pull["title"],
            "html_url": f"https://github.com/{full}/pull/{number}",
            "url": f"{self.url}/repos/{full}/pulls/{number}",
            "head": {"ref": pull["head"], "sha": pull["sha"]},
            "base": {"ref": pull["base"]},
        }

    def _merge(self, number: int, title: str) -> tuple[int, dict[str, Any]]:
        pull = self.pulls[number]
        if self.merge_refused:
            return 405, {"message": self.merge_refused}
        bare = self.bare(pull["repo"])
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "w"
            _git(Path(tmp), "clone", "--quiet", bare.as_posix(), "w")
            _git(work, "checkout", "--quiet", pull["base"])
            _git(work, "merge", "--no-ff", "--quiet", "-m", title, f"origin/{pull['head']}")
            sha = _git(work, "rev-parse", "HEAD")
            _git(work, "push", "--quiet", "origin", pull["base"])
        pull["merged"] = True
        return 200, {"merged": True, "sha": sha, "message": "Pull Request successfully merged"}

    def _route(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        for index, (needle, status, payload) in enumerate(self.fail_next):
            if needle in path:
                del self.fail_next[index]
                return status, payload

        if method == "POST" and path == "/user/repos":
            name = body["name"]
            if name in self.repos:
                return 422, {
                    "message": "Repository creation failed.",
                    "errors": [{"field": "name", "message": "name already exists on this account"}],
                }
            bare = self.root / "remotes" / f"{name}.git"
            bare.parent.mkdir(parents=True, exist_ok=True)
            _git(self.root, "init", "--bare", "--quiet", "-b", "main", bare.as_posix())
            self.repos[name] = {"bare": bare, "private": bool(body.get("private"))}
            return 201, self._repo_json(name)

        match = re.fullmatch(rf"/repos/{OWNER}/([^/]+)(/.*)?", path)
        if not match or match.group(1) not in self.repos:
            return 404, {"message": "Not Found"}
        repo, rest = match.group(1), match.group(2) or ""
        bare = self.bare(repo)

        if method == "GET" and rest == "":
            return 200, self._repo_json(repo)
        if method == "GET" and rest == "/pulls":  # open PRs (the query string is ignored)
            return 200, [
                self._pull_json(n)
                for n, pull in self.pulls.items()
                if pull["repo"] == repo and not pull["merged"]
            ]
        if method == "POST" and rest == "/pulls":
            try:
                sha = _git(bare, "rev-parse", f"refs/heads/{body['head']}")
            except RuntimeError:
                return 422, {"message": "Validation Failed",
                             "errors": [{"field": "head", "code": "invalid"}]}
            number = len(self.pulls) + 1
            self.pulls[number] = {"repo": repo, "head": body["head"], "base": body["base"],
                                  "title": body["title"], "sha": sha, "merged": False,
                                  "body": body.get("body") or ""}
            return 201, self._pull_json(number)
        pull = re.fullmatch(r"/pulls/(\d+)(/merge)?", rest)
        if pull and int(pull.group(1)) in self.pulls:
            number = int(pull.group(1))
            if method == "GET" and not pull.group(2):
                return 200, self._pull_json(number)
            if method == "PUT" and pull.group(2):
                return self._merge(number, body.get("commit_title") or f"Merge #{number}")
        commit = re.fullmatch(r"/commits/([0-9a-f]{7,40})(/check-runs|/status)?", rest)
        if method == "GET" and commit:
            sha, tail = commit.group(1), commit.group(2)
            full = f"{self.url}/repos/{OWNER}/{repo}/commits/{sha}"
            if tail is None:
                return 200, {"sha": sha, "url": full, "commit": {"message": "x"}}
            if tail == "/check-runs":
                checks = self._current_ci()
                runs = [{"id": i, "html_url": f"https://ci.example/{i}", **spec}
                        for i, spec in enumerate(checks, 1)]
                return 200, {"total_count": len(runs), "check_runs": runs}
            # Foundry reads check runs, then statuses, once per poll: statuses end a "tick".
            self._tick_ci()
            return 200, {"state": "success", "sha": sha, "total_count": 0, "statuses": []}
        return 404, {"message": "Not Found"}

    def _current_ci(self) -> list[CheckSpec]:
        with self._lock:
            return self.ci[min(self.ci_calls, len(self.ci) - 1)]

    def _tick_ci(self) -> None:
        with self._lock:
            self.ci_calls += 1

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep test output quiet
                pass

            def _serve(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else {}
                path = self.path.split("?", 1)[0]
                outer.requests.append((method, path, self.headers.get("Authorization", "")))
                try:
                    status, payload = outer._route(method, path, body)
                except Exception as exc:  # surface fake bugs as 500s rather than hangs
                    status, payload = 500, {"message": f"fake github error: {exc}"}
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._serve("GET")

            def do_POST(self) -> None:
                self._serve("POST")

            def do_PUT(self) -> None:
                self._serve("PUT")

        return Handler

