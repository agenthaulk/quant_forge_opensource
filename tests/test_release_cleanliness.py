from __future__ import annotations

from pathlib import Path
import subprocess

from scripts.release_safety_scan import _contains_env_secret_assignment, _contains_non_loopback_ip


PUBLIC_ROOTS = [
    Path("README.md"),
    Path("AGENTS.md"),
    Path("LICENSE"),
    Path("LICENSE-APACHE-2.0"),
    Path("CLA.md"),
    Path("CONTRIBUTING.md"),
    Path("pyproject.toml"),
    Path(".env.example"),
    Path(".github"),
    Path("configs"),
    Path("docs"),
    Path("scripts"),
    Path("src/quant_forge"),
]


def iter_public_files() -> list[Path]:
    git_files = _git_public_files()
    if git_files is not None:
        return git_files

    files: list[Path] = []
    for root in PUBLIC_ROOTS:
        if root.is_file():
            if not _is_gitless_local_file(root):
                files.append(root)
        elif root.exists():
            files.extend(
                path
                for path in root.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".pyo"}
                and not _is_gitless_local_file(path)
            )
    return files


def _git_public_files() -> list[Path] | None:
    try:
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
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return [
        Path(line)
        for line in result.stdout.splitlines()
        if line
        and Path(line).is_file()
        and "__pycache__" not in Path(line).parts
        and Path(line).suffix not in {".pyc", ".pyo"}
    ]


def _is_gitless_local_file(path: Path) -> bool:
    if not path.parts:
        return False
    if path.parts[0] == "configs":
        return path.name.endswith((".local.yaml", ".local.env", ".secrets.env")) or path.name.startswith("local")
    return path.name in {".env"} or path.name.startswith(".env.")


def test_public_files_do_not_expose_local_paths_or_secret_markers() -> None:
    forbidden = [
        "/" + "Users/",
        "/" + "Volumes/",
        ".env" + ".local",
        "api" + "_key:",
        "secret" + ":",
        "token" + ":",
    ]
    offenders: list[str] = []
    for path in iter_public_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in forbidden:
            if marker in text:
                offenders.append(f"{path}:{marker}")
    assert offenders == []


def test_public_source_has_no_internal_platform_imports() -> None:
    forbidden = [
        "qf_" + "studio",
        "post" + "gres",
        "private" + "_provider",
        "quant_forge" + "_data_platform",
        "integrations" + ".",
    ]
    offenders: list[str] = []
    for path in Path("src/quant_forge").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in forbidden:
            if marker in text:
                offenders.append(f"{path}:{marker}")
    assert offenders == []


def test_no_large_public_files() -> None:
    offenders = [str(path) for path in iter_public_files() if path.stat().st_size > 500_000]
    assert offenders == []


def test_license_files_define_delayed_open_source_path() -> None:
    license_text = Path("LICENSE").read_text(encoding="utf-8")
    apache_text = Path("LICENSE-APACHE-2.0").read_text(encoding="utf-8")
    assert "SPDX-License-Identifier: BUSL-1.1" in license_text
    assert "Change Date: 2027-12-31" in license_text
    assert "Change License: Apache License, Version 2.0" in license_text
    assert "Apache License" in apache_text


def test_release_scan_allows_public_env_placeholders() -> None:
    text = """
DEEPSEEK_API_KEY=
DEEPSEEK_API_KEY="<your-deepseek-api-key>"
QF_TEST_API_KEY="test-value"
"""
    assert not _contains_env_secret_assignment(text)


def test_release_scan_flags_real_env_secret_values() -> None:
    fake_value = "abc1234567890" + "secret"
    assert _contains_env_secret_assignment(f"DEEPSEEK_API_KEY={fake_value}\n")
    assert _contains_env_secret_assignment(f'deepseek_api_key="{fake_value}" # local only\n')


def test_release_scan_allows_docker_bind_address_but_flags_private_hosts() -> None:
    assert not _contains_non_loopback_ip("bind host 0.0.0.0 and publish to 127.0.0.1")
    private_host = ".".join(("192", "168", "1", "10"))
    assert _contains_non_loopback_ip(f"private host {private_host}")
