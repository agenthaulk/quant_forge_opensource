"""Release safety scan for the public Quant Forge tree."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


PUBLIC_ROOTS = (
    Path("README.md"),
    Path("AGENTS.md"),
    Path("pyproject.toml"),
    Path(".env.example"),
    Path("configs"),
    Path("docs"),
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
        if value.startswith("127."):
            continue
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
