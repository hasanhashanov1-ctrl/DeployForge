import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloneResult:
    commit_sha: str
    log: str


class GitSource(Protocol):
    def clone(self, repo_url: str, branch: str | None, destination: Path) -> CloneResult: ...


class SubprocessGitSource:
    def __init__(self, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds

    def clone(self, repo_url: str, branch: str | None, destination: Path) -> CloneResult:
        command = ["git", "clone", "--depth", "1"]
        if branch:
            command.extend(["--branch", branch, "--single-branch"])
        command.extend(["--", repo_url, str(destination)])
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise SourceError(f"Git clone timed out after {self.timeout_seconds} seconds") from exc
        log = completed.stdout + completed.stderr
        if completed.returncode != 0:
            raise SourceError(log.strip() or "Git clone failed")
        commit = subprocess.run(
            ["git", "-C", str(destination), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
        if commit.returncode != 0:
            raise SourceError(
                (commit.stdout + commit.stderr).strip() or "Cannot resolve commit SHA"
            )
        commit_sha = commit.stdout.strip().lower()
        if len(commit_sha) != 40 or any(char not in "0123456789abcdef" for char in commit_sha):
            raise SourceError("Git returned an invalid commit SHA")
        return CloneResult(commit_sha=commit_sha, log=log)
