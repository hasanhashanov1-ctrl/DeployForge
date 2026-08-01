import subprocess
from pathlib import Path

import pytest

from app.services.git_source import SourceError, SubprocessGitSource


def test_clone_uses_argument_lists_and_safe_separator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, stdout="a" * 40 + "\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="cloned\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = SubprocessGitSource(30).clone(
        "https://github.com/example/project", "main", tmp_path / "repo"
    )
    assert result.commit_sha == "a" * 40
    assert calls[0][:6] == ["git", "clone", "--depth", "1", "--branch", "main"]
    assert "--" in calls[0]
    assert calls[0][-2:] == ["https://github.com/example/project", str(tmp_path / "repo")]


def test_clone_timeout_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def timeout(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise subprocess.TimeoutExpired(["git", "clone"], 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(SourceError, match="timed out"):
        SubprocessGitSource(30).clone("https://github.com/example/project", None, tmp_path / "repo")
