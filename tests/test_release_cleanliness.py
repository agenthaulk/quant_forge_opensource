from __future__ import annotations

from pathlib import Path
import subprocess


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
            files.append(root)
        elif root.exists():
            files.extend(
                path
                for path in root.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
            )
    return files


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
    return [
        Path(line)
        for line in result.stdout.splitlines()
        if line
        and Path(line).is_file()
        and "__pycache__" not in Path(line).parts
        and Path(line).suffix not in {".pyc", ".pyo"}
    ]


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
