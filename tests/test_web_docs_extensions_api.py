"""Characterization tests for the CP6-4 Docs + Extensions endpoints.

Covers ``GET /api/docs``, ``GET /api/docs/{relpath}``, and
``GET /api/extensions`` over the live HTTP adapter, with
``web_server.DOCS_ROOT`` / ``web_server.EXTENSIONS_ROOT`` monkeypatched to
tmp trees (the call-time facade-lookup seam):

- docs index: section grouping and pinned casefold ordering, ATX-title
  extraction with filename-stem fallback, dotfile/dot-dir and symlink-escape
  exclusion, available:false degradation;
- docs document: the full containment matrix (traversal, single- and
  double-encoded probes, backslash, null byte, non-.md shapes, dot segments,
  directories, symlink escapes, unreadable files) all map to 404;
- redact-before-render ordering: absolute paths in a doc source reach the
  ``html`` field as escaped ``&lt;redacted-path&gt;`` text, never as raw
  ``<redacted-path>`` markup, and no tmp path ever appears in a payload;
- extensions: the in-repo reference manifest validates, the rejection matrix
  pins one issue code per safety rule (including the D7 no-builtin-exemption
  pin), rejected rows never echo manifest content, the 10-row point catalog
  and scan ordering are pinned;
- token gate: all three endpoints 401 without the control token when gated;
- routing dispatches all three builders late-bound through the server module
  namespace, and the server facade re-exports the CP6-4 names.

Fixture strings use neutral MARKER_XYZ-style content only.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.server import create_local_web_server
from quant_forge.config import QuantForgeConfig, WebSettings
from quant_forge.data.local import create_demo_workspace


JSON_CONTENT_TYPE = "application/json; charset=utf-8"

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def web_config(tmp_path):
    create_demo_workspace(tmp_path / "demo")
    return QuantForgeConfig().resolve(tmp_path / "demo")


@pytest.fixture()
def web_app(web_config):
    server = create_local_web_server(host="127.0.0.1", port=0, config=web_config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


@pytest.fixture()
def docs_root(monkeypatch, tmp_path):
    root = tmp_path / "docs_tree"
    root.mkdir()
    monkeypatch.setattr(web_server, "DOCS_ROOT", root)
    return root


@pytest.fixture()
def extensions_root(monkeypatch, tmp_path):
    root = tmp_path / "extensions_tree"
    root.mkdir()
    monkeypatch.setattr(web_server, "EXTENSIONS_ROOT", root)
    return root


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, str, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def _valid_manifest(extension_id: str = "qf.marker.pack", **overrides) -> dict:
    payload = {
        "id": extension_id,
        "name": "Marker Pack",
        "version": "0.1.0",
        "kind": "docs-extension",
        "permissions": {"network_access": False, "secret_access": False},
    }
    payload.update(overrides)
    return payload


def _write_manifest(root: Path, directory: str, payload: object) -> None:
    target = root / directory
    target.mkdir(parents=True)
    (target / "extension.json").write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# GET /api/docs
# ---------------------------------------------------------------------------


def test_docs_list_orders_sections_and_docs_with_titles(docs_root, web_app) -> None:
    (docs_root / "UPPER.md").write_text("# Upper Title\n\nbody\n", encoding="utf-8")
    (docs_root / "lower.md").write_text("no heading here\n", encoding="utf-8")
    sub = docs_root / "sub"
    sub.mkdir()
    (sub / "inner.md").write_text("## Inner Heading ##\n", encoding="utf-8")

    status, content_type, body = _get(f"{web_app}/api/docs")

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert set(payload) == {"available", "count", "sections"}
    assert payload["available"] is True
    assert payload["count"] == 3
    # Sections: literal "root" first, then remaining labels casefold-ascending.
    assert [section["section"] for section in payload["sections"]] == ["root", "sub"]
    root_docs = payload["sections"][0]["docs"]
    # Within a section: relpath casefold-ascending ("lower" < "upper").
    assert [doc["relpath"] for doc in root_docs] == ["lower.md", "UPPER.md"]
    by_relpath = {doc["relpath"]: doc for doc in root_docs}
    assert by_relpath["UPPER.md"] == {
        "relpath": "UPPER.md",
        "section": "root",
        "title": "Upper Title",
    }
    # No ATX heading: the title falls back to the filename stem.
    assert by_relpath["lower.md"]["title"] == "lower"
    assert payload["sections"][1]["docs"] == [
        {"relpath": "sub/inner.md", "section": "sub", "title": "Inner Heading"}
    ]


def test_docs_list_excludes_dotfiles_dotdirs_and_symlink_escapes(
    docs_root, web_app, tmp_path
) -> None:
    (docs_root / "visible.md").write_text("# Visible\n", encoding="utf-8")
    (docs_root / ".hidden.md").write_text("# Hidden\n", encoding="utf-8")
    dot_dir = docs_root / ".internal"
    dot_dir.mkdir()
    (dot_dir / "inside.md").write_text("# Inside\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (docs_root / "escape.md").symlink_to(outside)

    status, _, body = _get(f"{web_app}/api/docs")

    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["count"] == 1
    assert [doc["relpath"] for section in payload["sections"] for doc in section["docs"]] == [
        "visible.md"
    ]


def test_docs_list_empty_root_is_available_with_zero_docs(docs_root, web_app) -> None:
    status, _, body = _get(f"{web_app}/api/docs")
    assert status == 200
    assert json.loads(body.decode("utf-8")) == {"available": True, "count": 0, "sections": []}


def test_docs_list_missing_root_degrades_to_unavailable(monkeypatch, web_app, tmp_path) -> None:
    monkeypatch.setattr(web_server, "DOCS_ROOT", tmp_path / "missing_docs")
    status, _, body = _get(f"{web_app}/api/docs")
    assert status == 200
    assert json.loads(body.decode("utf-8")) == {"available": False, "count": 0, "sections": []}


# ---------------------------------------------------------------------------
# GET /api/docs/{relpath}
# ---------------------------------------------------------------------------


def test_docs_document_returns_rendered_payload(docs_root, web_app, tmp_path) -> None:
    guide = docs_root / "guide"
    guide.mkdir()
    (guide / "intro.md").write_text(
        "# Intro Title\n\nSome **bold** text and <b>raw html</b>.\n", encoding="utf-8"
    )

    status, content_type, body = _get(f"{web_app}/api/docs/guide/intro.md")

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    text = body.decode("utf-8")
    assert str(tmp_path) not in text
    payload = json.loads(text)
    assert set(payload) == {"relpath", "section", "title", "html"}
    assert payload["relpath"] == "guide/intro.md"
    assert payload["section"] == "guide"
    assert payload["title"] == "Intro Title"
    assert "<h1>Intro Title</h1>" in payload["html"]
    assert "<strong>bold</strong>" in payload["html"]
    assert "&lt;b&gt;raw html&lt;/b&gt;" in payload["html"]


def test_docs_document_root_level_section_is_root(docs_root, web_app) -> None:
    (docs_root / "top.md").write_text("body only\n", encoding="utf-8")
    status, _, body = _get(f"{web_app}/api/docs/top.md")
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["section"] == "root"
    # Stem fallback applies to the document payload too.
    assert payload["title"] == "top"


def test_docs_document_containment_matrix_returns_404(docs_root, web_app) -> None:
    (docs_root / "real.md").write_text("# Real\n", encoding="utf-8")
    (docs_root / "dir.md").mkdir()  # a directory with a .md-shaped name
    (docs_root / "UP.MD").write_text("# Upper Suffix\n", encoding="utf-8")
    (docs_root / "plain").write_text("extensionless\n", encoding="utf-8")
    (docs_root / "trick.md.txt").write_text("wrong suffix\n", encoding="utf-8")
    (docs_root / ".hidden.md").write_text("# Hidden\n", encoding="utf-8")

    probes = (
        "/api/docs/../AGENTS.md",  # literal traversal
        "/api/docs/..%2FAGENTS.md",  # single-encoded traversal, decoded once
        "/api/docs/%2e%2e/AGENTS.md",  # encoded dot segments
        "/api/docs/%252e%252e%252fAGENTS.md",  # double-encoded stays literal
        "/api/docs//etc/hosts.md",  # leading slash
        "/api/docs/..%5CAGENTS.md",  # backslash probe
        "/api/docs/real%00.md",  # null byte
        "/api/docs/",  # empty rest
        "/api/docs/dir.md",  # directory, not a file
        "/api/docs/UP.MD",  # uppercase suffix: case-sensitive contract
        "/api/docs/plain",  # extensionless
        "/api/docs/trick.md.txt",  # .md.txt
        "/api/docs/.hidden.md",  # dot segment file
        "/api/docs/missing.md",  # unknown file
    )
    for probe in probes:
        status, content_type, body = _get(f"{web_app}{probe}")
        assert status == 404, probe
        assert content_type == JSON_CONTENT_TYPE, probe
        assert "unknown doc" in json.loads(body.decode("utf-8"))["error"], probe


def test_docs_document_names_outside_charset_404_even_when_file_exists(
    docs_root, web_app
) -> None:
    """Doc-name rule (single server-side definition): a name outside the
    conservative segment charset is unknown to the detail endpoint even when
    a matching file exists under the docs root."""

    guide = docs_root / "guide"
    guide.mkdir()
    for name in ("file name.md", "api_token=XYZ.md", "AT&T.md"):
        (guide / name).write_text("# Marker\n", encoding="utf-8")

    probes = (
        "/api/docs/guide/file%20name.md",  # space, %20-encoded request
        "/api/docs/guide/api_token=XYZ.md",  # '=' outside the charset
        "/api/docs/guide/AT&T.md",  # '&' outside the charset, literal
        "/api/docs/guide/AT%26T.md",  # '&' outside the charset, encoded
    )
    for probe in probes:
        status, content_type, body = _get(f"{web_app}{probe}")
        assert status == 404, probe
        assert content_type == JSON_CONTENT_TYPE, probe
        assert "unknown doc" in json.loads(body.decode("utf-8"))["error"], probe


def test_docs_list_skips_names_outside_charset(docs_root, web_app) -> None:
    """List and detail share one relpath rule: files the detail endpoint
    would 404 never appear in the index, and their name-derived text (which
    could look like a KEY=value pair) never reaches a payload."""

    guide = docs_root / "guide"
    guide.mkdir()
    (guide / "kept.md").write_text("# Kept\n", encoding="utf-8")
    for name in ("file name.md", "api_token=XYZ.md", "AT&T.md"):
        (guide / name).write_text("# Marker\n", encoding="utf-8")

    status, _, body = _get(f"{web_app}/api/docs")

    assert status == 200
    text = body.decode("utf-8")
    payload = json.loads(text)
    assert payload["count"] == 1
    assert [doc["relpath"] for section in payload["sections"] for doc in section["docs"]] == [
        "guide/kept.md"
    ]
    assert "api_token" not in text
    assert "file name" not in text
    assert "AT&T" not in text


def test_docs_document_symlink_escape_and_unreadable_file_return_404(
    docs_root, web_app, tmp_path
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (docs_root / "escape.md").symlink_to(outside)

    status, _, body = _get(f"{web_app}/api/docs/escape.md")
    assert status == 404
    assert "unknown doc" in json.loads(body.decode("utf-8"))["error"]

    locked = docs_root / "locked.md"
    locked.write_text("# Locked\n", encoding="utf-8")
    locked.chmod(0)
    try:
        status, _, body = _get(f"{web_app}/api/docs/locked.md")
    finally:
        locked.chmod(0o644)
    assert status == 404
    assert "unknown doc" in json.loads(body.decode("utf-8"))["error"]


def test_docs_document_redacts_source_before_rendering(docs_root, web_app, tmp_path) -> None:
    """Redact-before-render ordering: the html field carries only the escaped
    redaction token, never raw <redacted-path> markup or the original path."""

    absolute_path = str(tmp_path / "marker_secret_dir" / "notes")
    (docs_root / "leaky.md").write_text(
        f"# Leaky\n\npath {absolute_path} end\n", encoding="utf-8"
    )

    status, _, body = _get(f"{web_app}/api/docs/leaky.md")

    assert status == 200
    text = body.decode("utf-8")
    assert "marker_secret_dir" not in text
    assert str(tmp_path) not in text
    payload = json.loads(text)
    assert "&lt;redacted-path&gt;" in payload["html"]
    assert "<redacted-path>" not in payload["html"]


# ---------------------------------------------------------------------------
# GET /api/extensions
# ---------------------------------------------------------------------------


def test_extensions_reference_manifest_in_repo_root_is_valid(monkeypatch, web_app) -> None:
    monkeypatch.setattr(web_server, "EXTENSIONS_ROOT", _REPO_ROOT / "extensions")

    status, content_type, body = _get(f"{web_app}/api/extensions")

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload["available"] is True
    by_directory = {row["directory"]: row for row in payload["extensions"]}
    row = by_directory["qf-reference-docs-pack"]
    assert row["status"] == "valid"
    assert row["issues"] == []
    assert row["id"] == "qf.reference.docs-pack"
    assert row["name"] == "Reference Docs Pack"
    assert row["version"] == "0.1.0"
    assert row["kind"] == "docs-extension"
    assert row["permissions"] == {
        "network_access": False,
        "secret_access": False,
        "data_scopes": [],
    }
    assert row["contributions"] == [
        {"id": "repo_docs", "point": "docs.pack", "reserved": False, "config": {"docs_root": "docs"}}
    ]


def test_extensions_points_catalog_is_static_and_pinned(extensions_root, web_app) -> None:
    status, _, body = _get(f"{web_app}/api/extensions")

    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert set(payload) == {
        "available",
        "points",
        "extensions",
        "count",
        "valid_count",
        "rejected_count",
    }
    assert payload["available"] is True
    assert payload["extensions"] == []
    assert payload["count"] == payload["valid_count"] == payload["rejected_count"] == 0
    assert [(row["point"], row["status"]) for row in payload["points"]] == [
        ("data.snapshot_source", "supported"),
        ("data.canonical_mapping", "supported"),
        ("data.quality_rule", "supported"),
        ("agent.context_pack", "supported"),
        ("docs.pack", "supported"),
        ("data.provider_adapter", "reserved"),
        ("data.pit_resolver", "reserved"),
        ("report.renderer", "reserved"),
        ("agent.workflow", "reserved"),
        ("lab.view", "reserved"),
    ]
    for row in payload["points"]:
        assert set(row) == {"point", "status", "note"}
        assert row["note"]


def test_extensions_rejection_matrix_pins_issue_codes(extensions_root, web_app) -> None:
    cases: dict[str, tuple[object, str]] = {
        "case-exec": (
            _valid_manifest(
                "qf.marker.exec",
                contributes=[{"id": "c_one", "point": "docs.pack", "executable": True}],
            ),
            "executable_contribution_rejected",
        ),
        # D7 pin: builtin grants no exemption from the no-exec rule.
        "case-exec-builtin": (
            _valid_manifest(
                "qf.marker.builtin",
                builtin=True,
                contributes=[
                    {"id": "c_one", "point": "docs.pack", "executable": True, "builtin": True}
                ],
            ),
            "executable_contribution_rejected",
        ),
        "case-unknown-point": (
            _valid_manifest(
                "qf.marker.point",
                contributes=[{"id": "c_one", "point": "docs.unknown_point"}],
            ),
            "unknown_contribution_point",
        ),
        "case-bad-semver": (_valid_manifest("qf.marker.semver", version="1.0"), "invalid_version"),
        "case-no-permissions": (
            {
                key: value
                for key, value in _valid_manifest("qf.marker.noperm").items()
                if key != "permissions"
            },
            "permissions_missing",
        ),
        "case-network-true": (
            _valid_manifest(
                "qf.marker.network",
                permissions={"network_access": True, "secret_access": False},
            ),
            "network_access_rejected",
        ),
        "case-secret-true": (
            _valid_manifest(
                "qf.marker.secret",
                permissions={"network_access": False, "secret_access": True},
            ),
            "secret_access_rejected",
        ),
        "case-url-value": (
            _valid_manifest("qf.marker.url", description="see https://example.invalid/page"),
            "external_url_rejected",
        ),
        "case-abs-path": (
            _valid_manifest(
                "qf.marker.path",
                description=f"cache under {Path.home() / 'marker_cache'}",
            ),
            "redactable_value_rejected",
        ),
        "case-secret-pair": (
            _valid_manifest("qf.marker.pair", description="marker_token=MARKER_XYZ"),
            "redactable_value_rejected",
        ),
    }
    for directory, (manifest, _) in cases.items():
        _write_manifest(extensions_root, directory, manifest)
    (extensions_root / "case-invalid-json").mkdir()
    (extensions_root / "case-invalid-json" / "extension.json").write_text(
        "{not json", encoding="utf-8"
    )
    _write_manifest(extensions_root, "case-top-array", [])

    status, _, body = _get(f"{web_app}/api/extensions")

    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    rows = payload["extensions"]
    # Scan order pinned: directory name ascending.
    assert [row["directory"] for row in rows] == sorted(row["directory"] for row in rows)
    by_directory = {row["directory"]: row for row in rows}
    for directory, (_, expected_code) in cases.items():
        row = by_directory[directory]
        assert row["status"] == "rejected", directory
        assert row["issues"][0]["code"] == expected_code, directory
        # Manifest content that failed safety validation is never echoed.
        assert set(row) == {"directory", "status", "issues"}, directory
    assert by_directory["case-invalid-json"]["issues"] == [
        {"code": "manifest_unreadable", "field": None}
    ]
    assert by_directory["case-top-array"]["issues"] == [
        {"code": "manifest_not_object", "field": None}
    ]
    assert payload["count"] == len(rows) == 12
    assert payload["valid_count"] == 0
    assert payload["rejected_count"] == 12
    # No rejected manifest content leaks anywhere in the response body.
    text = body.decode("utf-8")
    assert "Marker Pack" not in text
    assert "marker_cache" not in text
    assert "MARKER_XYZ" not in text


def test_extensions_duplicate_id_rejects_second_directory_in_scan_order(
    extensions_root, web_app
) -> None:
    _write_manifest(extensions_root, "a-first", _valid_manifest("qf.marker.dup"))
    _write_manifest(extensions_root, "b-second", _valid_manifest("qf.marker.dup"))

    status, _, body = _get(f"{web_app}/api/extensions")

    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    by_directory = {row["directory"]: row for row in payload["extensions"]}
    assert by_directory["a-first"]["status"] == "valid"
    assert by_directory["b-second"]["status"] == "rejected"
    assert by_directory["b-second"]["issues"] == [
        {"code": "duplicate_extension_id", "field": "id"}
    ]
    assert payload["count"] == 2
    assert payload["valid_count"] == 1
    assert payload["rejected_count"] == 1


def test_extensions_reserved_point_contribution_is_valid_but_inert(
    extensions_root, web_app
) -> None:
    _write_manifest(
        extensions_root,
        "reserved-pack",
        _valid_manifest(
            "qf.marker.reserved",
            contributes=[{"id": "c_adapter", "point": "data.provider_adapter"}],
        ),
    )

    status, _, body = _get(f"{web_app}/api/extensions")

    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    row = payload["extensions"][0]
    assert row["status"] == "valid"
    assert row["issues"] == []
    assert row["contributions"] == [
        {"id": "c_adapter", "point": "data.provider_adapter", "reserved": True}
    ]
    assert payload["valid_count"] == 1


def test_extensions_missing_root_degrades_to_unavailable(monkeypatch, web_app, tmp_path) -> None:
    monkeypatch.setattr(web_server, "EXTENSIONS_ROOT", tmp_path / "missing_extensions")

    status, _, body = _get(f"{web_app}/api/extensions")

    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["available"] is False
    assert payload["extensions"] == []
    assert payload["count"] == payload["valid_count"] == payload["rejected_count"] == 0
    # The static point catalog is always present.
    assert len(payload["points"]) == 10


# ---------------------------------------------------------------------------
# Token gate + routing seams + facade surface
# ---------------------------------------------------------------------------


def test_new_endpoints_require_control_token_when_gated(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QF_TEST_WEB_TOKEN", "token-for-tests")
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        web=WebSettings(allow_docker_bind=True, control_token_env="QF_TEST_WEB_TOKEN")
    ).resolve(tmp_path / "demo")
    docs_root = tmp_path / "docs_tree"
    docs_root.mkdir()
    (docs_root / "real.md").write_text("# Real\n", encoding="utf-8")
    extensions_root = tmp_path / "extensions_tree"
    extensions_root.mkdir()
    monkeypatch.setattr(web_server, "DOCS_ROOT", docs_root)
    monkeypatch.setattr(web_server, "EXTENSIONS_ROOT", extensions_root)
    server = create_local_web_server(host="0.0.0.0", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    endpoints = ("/api/docs", "/api/docs/real.md", "/api/extensions")

    try:
        for endpoint in endpoints:
            status, content_type, body = _get(f"{base_url}{endpoint}")
            assert status == 401, endpoint
            assert content_type == JSON_CONTENT_TYPE
            assert json.loads(body.decode("utf-8")) == {"error": "unauthorized"}

            status, _, _ = _get(
                f"{base_url}{endpoint}",
                headers={"Authorization": "Bearer token-for-tests"},
            )
            assert status == 200, endpoint
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_routing_dispatches_builders_via_server_namespace(monkeypatch, web_app) -> None:
    seen: dict[str, object] = {}

    def fake_docs_list(config):
        seen["docs_list"] = True
        return {"echo": "docs-list"}

    def fake_docs_document(config, relpath):
        seen["docs_document"] = relpath
        return {"echo": "docs-document"}

    def fake_extensions(config):
        seen["extensions"] = True
        return {"echo": "extensions"}

    monkeypatch.setattr(web_server, "_docs_list_payload", fake_docs_list)
    monkeypatch.setattr(web_server, "_docs_document_payload", fake_docs_document)
    monkeypatch.setattr(web_server, "_extensions_payload", fake_extensions)

    status, _, body = _get(f"{web_app}/api/docs")
    assert status == 200
    assert json.loads(body.decode("utf-8")) == {"echo": "docs-list"}

    # Per-segment encoding: an encoded slash decodes once into a separator.
    status, _, body = _get(f"{web_app}/api/docs/guide%2Fintro.md")
    assert status == 200
    assert json.loads(body.decode("utf-8")) == {"echo": "docs-document"}
    assert seen["docs_document"] == "guide/intro.md"

    status, _, body = _get(f"{web_app}/api/extensions")
    assert status == 200
    assert json.loads(body.decode("utf-8")) == {"echo": "extensions"}
    assert seen.keys() == {"docs_list", "docs_document", "extensions"}


def test_server_facade_re_exports_cp64_names() -> None:
    for name in (
        "DOCS_ROOT",
        "EXTENSIONS_ROOT",
        "_docs_document_payload",
        "_docs_list_payload",
        "_docs_relpath_from_path",
        "_extensions_payload",
        "extract_markdown_title",
        "render_markdown_html",
    ):
        assert hasattr(web_server, name), f"missing facade re-export: {name}"
