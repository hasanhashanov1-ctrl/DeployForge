import pytest

from app.services.log_buffer import LogBuffer, bounded_text
from app.validation import (
    normalize_github_url,
    validate_branch,
    validate_dockerfile_path,
    validate_slug,
)


@pytest.mark.parametrize("slug", ["a", "my-api", "api2", "a" * 63])
def test_valid_slug(slug: str) -> None:
    assert validate_slug(slug) == slug


@pytest.mark.parametrize("slug", ["", "My-App", "-api", "api-", "a_b", "a" * 64])
def test_invalid_slug(slug: str) -> None:
    with pytest.raises(ValueError):
        validate_slug(slug)


def test_github_url_is_normalized() -> None:
    assert (
        normalize_github_url("https://github.com/example/project.git/")
        == "https://github.com/example/project.git"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/example/project",
        "https://gitlab.com/example/project",
        "https://user@github.com/example/project",
        "https://github.com/example/project?token=secret",
        "https://github.com/example",
    ],
)
def test_unsafe_repo_url_is_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_github_url(url)


@pytest.mark.parametrize("branch", ["main", "feature/safe-name", "release-1.2"])
def test_valid_branch(branch: str) -> None:
    assert validate_branch(branch) == branch


@pytest.mark.parametrize(
    "branch", ["-main", "bad..name", "bad name", "main.lock", "a@{b", "@", ".hidden"]
)
def test_invalid_branch(branch: str) -> None:
    with pytest.raises(ValueError):
        validate_branch(branch)


@pytest.mark.parametrize("path", ["Dockerfile", "docker/api.Dockerfile"])
def test_valid_dockerfile_path(path: str) -> None:
    assert validate_dockerfile_path(path) == path


@pytest.mark.parametrize(
    "path", ["../Dockerfile", "/Dockerfile", "a/../Dockerfile", "a\\Dockerfile"]
)
def test_unsafe_dockerfile_path(path: str) -> None:
    with pytest.raises(ValueError):
        validate_dockerfile_path(path)


def test_log_buffer_stops_at_utf8_byte_limit() -> None:
    buffer = LogBuffer(80)
    buffer.append("я" * 100)
    assert buffer.truncated is True
    assert len(buffer.value.encode()) <= 80
    assert "truncated" in buffer.value


def test_bounded_runtime_log_keeps_tail() -> None:
    value, truncated = bounded_text("old\n" * 100 + "last line", 100)
    assert truncated is True
    assert value.endswith("last line")
    assert len(value.encode()) <= 100
