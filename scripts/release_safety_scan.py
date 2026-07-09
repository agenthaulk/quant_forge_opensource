"""Release safety scan for the public Quant Forge tree."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


PUBLIC_ROOTS = (
    Path("README.md"),
    Path("AGENTS.md"),
    Path("CLAUDE.md"),
    Path("LICENSE"),
    Path("LICENSE-APACHE-2.0"),
    Path("CLA.md"),
    Path("CONTRIBUTING.md"),
    Path("pyproject.toml"),
    Path("constraints.txt"),
    Path("Dockerfile"),
    Path(".env.example"),
    Path(".github"),
    Path("configs"),
    Path("docs"),
    Path("extensions"),
    Path("scripts"),
    Path("src"),
    Path("tests"),
)

SECRET_PATTERNS = {
    "private_key": re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    "aws_access_key": re.compile("A" + r"KIA[0-9A-Z]{16}"),
    "google_api_key": re.compile("AI" + r"za[0-9A-Za-z\-_]{20,}"),
    "openai_like_key": re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    "bearer_token": re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
    "x_api_key_header": re.compile(r"x-api-key\s*:\s*\S+", re.IGNORECASE),
    "password_assignment": re.compile(r"\b(passwd|password|client_secret)\s*[:=]\s*\S+", re.IGNORECASE),
}

ENV_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?m)^[^\S\n]*(?:export[^\S\n]+)?"
    r"(?P<name>[A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|SECRET|PASSWORD))"
    r"[^\S\n]*=[^\S\n]*(?P<value>[^\n#]*)",
    re.IGNORECASE,
)

PLACEHOLDER_SECRET_VALUES = {
    "changeme",
    "dummy",
    "example",
    "placeholder",
    "test",
    "test-value",
    "your-api-key",
}

FORBIDDEN_TEXT_MARKERS = (
    "/" + "Users/",
    "/" + "Volumes/",
    "/" + "private/",
    ".env" + ".local",
    "api" + "_key:",
    "secret" + ":",
    "token" + ":",
)

MAX_PUBLIC_FILE_BYTES = 500_000


def main() -> int:
    offenders: list[str] = []
    files = public_files()
    for path in files:
        if path.stat().st_size > MAX_PUBLIC_FILE_BYTES:
            offenders.append(f"{path}: file exceeds {MAX_PUBLIC_FILE_BYTES} bytes")
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in FORBIDDEN_TEXT_MARKERS:
            if marker in text:
                offenders.append(f"{path}: contains forbidden marker {marker!r}")
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                offenders.append(f"{path}: matches secret pattern {name}")
        if _contains_env_secret_assignment(text):
            offenders.append(f"{path}: matches secret pattern env_secret_assignment")
        if _contains_non_loopback_ip(text):
            offenders.append(f"{path}: contains non-loopback IPv4 address")

    if offenders:
        print("Release safety scan failed:", file=sys.stderr)
        for item in offenders:
            print(f"- {item}", file=sys.stderr)
        return 1
    print(f"Release safety scan passed for {len(files)} public files.")
    return 0


def public_files() -> list[Path]:
    git_files = _git_public_files()
    if git_files is not None:
        return git_files
    files: list[Path] = []
    for root in PUBLIC_ROOTS:
        if root.is_file():
            files.append(root)
        elif root.exists():
            files.extend(
                path
                for path in root.rglob("*")
                if path.is_file()
                and ".git" not in path.parts
                and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".pyo"}
            )
    return sorted(set(files))


def _git_public_files() -> list[Path] | None:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *(str(root) for root in PUBLIC_ROOTS),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    files: list[Path] = []
    for line in result.stdout.splitlines():
        path = Path(line)
        if (
            line
            and path.is_file()
            and ".git" not in path.parts
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        ):
            files.append(path)
    return sorted(set(files))


def _contains_non_loopback_ip(text: str) -> bool:
    ip_pattern = re.compile(r"\b((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b")
    for match in ip_pattern.finditer(text):
        value = match.group(0)
        if value == "0.0.0.0" or value.startswith("127."):
            continue
        return True
    return False


def _contains_env_secret_assignment(text: str) -> bool:
    for match in ENV_SECRET_ASSIGNMENT_PATTERN.finditer(text):
        value = _strip_matching_quotes(match.group("value").strip())
        if not value or _is_placeholder_secret_value(value):
            continue
        if any(marker in value for marker in ("$(", "`")):
            continue
        if value.startswith(("sk-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_")):
            return True
        # Flag any surviving non-placeholder value that looks like a single secret
        # token (no whitespace, token-shaped, length >= 16), regardless of char
        # class. Dropping the earlier digit requirement catches purely alphabetic
        # keys such as an alpha-only DEEPSEEK_API_KEY; the whitespace-free token
        # regex plus the placeholder allowlist keep prose values from tripping.
        if re.fullmatch(r"[A-Za-z0-9._\-/+=]{16,}", value):
            return True
    return False


def _strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _is_placeholder_secret_value(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in PLACEHOLDER_SECRET_VALUES:
        return True
    if normalized.startswith("<") and normalized.endswith(">"):
        return True
    return _looks_like_hyphenated_word_placeholder(normalized)


def _looks_like_hyphenated_word_placeholder(normalized: str) -> bool:
    """Treat multi-word ``-``/``_``-delimited fixtures as placeholders.

    A real leaked key is a single dense token (e.g. ``abcdefghijklmnopqrstuvwx``
    or ``abc1234567890secret``). A documentation/test fixture such as
    ``local-fixture-secret-value`` splits into several short lowercase
    dictionary-like words. This keeps such prose fixtures from tripping the
    length-only secret heuristic without weakening detection of dense tokens.
    """

    segments = re.split(r"[-_]", normalized)
    if len(segments) < 3:
        return False
    return all(bool(seg) and seg.isalpha() for seg in segments)


if __name__ == "__main__":
    raise SystemExit(main())
