import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit, urlunsplit

SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
GITHUB_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$")
INVALID_BRANCH_CHARS = set(" ~^:?*[\\")
ENVIRONMENT_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def validate_slug(value: str) -> str:
    if not SLUG_PATTERN.fullmatch(value):
        raise ValueError(
            "slug must contain 1-63 lowercase letters, digits or hyphens "
            "and cannot start or end with a hyphen"
        )
    return value


def normalize_github_url(value: str) -> str:
    parsed = urlsplit(value)
    path = parsed.path.removesuffix("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not GITHUB_PATH_PATTERN.fullmatch(path)
    ):
        raise ValueError("repo_url must be a public HTTPS GitHub repository URL")
    owner, repository = path.removeprefix("/").split("/", maxsplit=1)
    repository = repository.removesuffix(".git")
    if owner in {".", ".."} or repository in {"", ".", ".."}:
        raise ValueError("repo_url must include a GitHub owner and repository")
    return urlunsplit(("https", "github.com", path, "", ""))


def validate_branch(value: str | None) -> str | None:
    if value is None:
        return None
    if not value or len(value) > 255:
        raise ValueError("branch must contain 1-255 characters")
    if (
        value.startswith("-")
        or value.startswith("/")
        or value == "@"
        or value.endswith(("/", ".", ".lock"))
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(part.startswith(".") for part in value.split("/"))
        or any(char in INVALID_BRANCH_CHARS or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("branch is not a safe Git branch name")
    return value


def validate_dockerfile_path(value: str) -> str:
    if not value or len(value) > 500 or "\\" in value or "//" in value:
        raise ValueError("dockerfile_path must be a relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() == "."
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("dockerfile_path must stay inside the repository")
    normalized = path.as_posix()
    if normalized != value or value.endswith("/"):
        raise ValueError("dockerfile_path must be a normalized file path")
    return normalized


def validate_environment_key(value: str) -> str:
    if not ENVIRONMENT_KEY_PATTERN.fullmatch(value):
        raise ValueError(
            "environment variable key must start with a letter or underscore and contain "
            "only letters, digits or underscores"
        )
    return value


def validate_environment_value(value: str | None) -> str | None:
    if value is not None and (len(value.encode("utf-8")) > 65_536 or "\x00" in value):
        raise ValueError("environment variable value must be at most 65536 bytes without NUL")
    return value
