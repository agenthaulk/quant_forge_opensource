"""Unit tests for the QF_OS Extension Manifest v1 schema and registry scan.

Pins the D7/D7a declarative-registry contract of
quant_forge.extensions.manifest / quant_forge.extensions.registry:

- every validation issue code in the closed set has a minimal triggering
  payload, with dotted/indexed field locators;
- the unconditional no-exec rule: any truthy ``executable`` rejects the
  manifest and ``builtin`` grants no exemption of any kind;
- ``public_extension_view`` is a projection: unknown keys, ``builtin``, and
  tolerated ``executable: false`` flags are never echoed, optional fields
  absent from the manifest are omitted (FP-4);
- filesystem scan order, duplicate-id claims, unreadable manifests, and the
  fixed 10-row contribution-point catalog;
- the in-repo reference manifest validates as valid with zero issues.

Fixture strings use neutral MARKER_XYZ-style content only.
"""

from __future__ import annotations

import json
from pathlib import Path

from quant_forge.extensions.manifest import (
    ALL_CONTRIBUTION_POINTS,
    EXTENSION_KINDS,
    ManifestIssue,
    MVP_CONTRIBUTION_POINTS,
    RESERVED_CONTRIBUTION_POINTS,
    public_extension_view,
    validate_extension_manifest,
)
from quant_forge.extensions.registry import contribution_points_payload, scan_extensions


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _manifest(**overrides) -> dict:
    payload = {
        "id": "qf.marker.pack",
        "name": "Marker Pack",
        "version": "1.2.3",
        "kind": "docs-extension",
        "permissions": {"network_access": False, "secret_access": False},
    }
    payload.update(overrides)
    return payload


def _codes(payload: object) -> list[str]:
    return [issue.code for issue in validate_extension_manifest(payload)]


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------


def test_contribution_point_and_kind_catalogs_are_pinned() -> None:
    assert MVP_CONTRIBUTION_POINTS == (
        "data.snapshot_source",
        "data.canonical_mapping",
        "data.quality_rule",
        "agent.context_pack",
        "docs.pack",
    )
    assert RESERVED_CONTRIBUTION_POINTS == (
        "data.provider_adapter",
        "data.pit_resolver",
        "report.renderer",
        "agent.workflow",
        "lab.view",
    )
    assert ALL_CONTRIBUTION_POINTS == MVP_CONTRIBUTION_POINTS + RESERVED_CONTRIBUTION_POINTS
    assert EXTENSION_KINDS == ("data-extension", "docs-extension", "agent-extension", "mixed")


# ---------------------------------------------------------------------------
# validate_extension_manifest: one minimal payload per issue code
# ---------------------------------------------------------------------------


def test_valid_manifest_yields_zero_issues() -> None:
    assert validate_extension_manifest(_manifest()) == []
    assert validate_extension_manifest(
        _manifest(
            description="declarative marker pack",
            engine="qf-os-0.1",
            permissions={
                "network_access": False,
                "secret_access": False,
                "data_scopes": ["read_catalog"],
            },
            contributes=[{"id": "c_one", "point": "docs.pack", "config": {}, "docs": "marker"}],
        )
    ) == []


def test_manifest_not_object() -> None:
    for payload in ([], "text", 42, None):
        issues = validate_extension_manifest(payload)
        assert [issue.code for issue in issues] == ["manifest_not_object"]
        assert issues[0].field is None


def test_missing_and_invalid_id() -> None:
    payload = _manifest()
    del payload["id"]
    assert _codes(payload) == ["missing_id"]
    for bad_id in ("Upper.case", "1leading", "a..b", "a.", "-lead", "a b", "a" * 65, 42):
        issues = validate_extension_manifest(_manifest(id=bad_id))
        assert [issue.code for issue in issues] == ["invalid_id"], bad_id
        assert issues[0].field == "id"


def test_id_label_charset_edges_accepted() -> None:
    for good_id in ("a", "qf.reference.docs-pack", "a0.b-c_d9", "a" * 64):
        assert _codes(_manifest(id=good_id)) == [], good_id


def test_missing_name() -> None:
    payload = _manifest()
    del payload["name"]
    assert _codes(payload) == ["missing_name"]
    assert _codes(_manifest(name="")) == ["missing_name"]
    assert _codes(_manifest(name=42)) == ["missing_name"]


def test_missing_and_invalid_version() -> None:
    payload = _manifest()
    del payload["version"]
    assert _codes(payload) == ["missing_version"]
    for bad_version in ("1.0", "v1.0.0", "1.0.0.beta", "1.0.x", 100):
        assert _codes(_manifest(version=bad_version)) == ["invalid_version"], bad_version
    for good_version in ("0.1.0", "1.2.3-rc.1", "1.2.3+build.5"):
        assert _codes(_manifest(version=good_version)) == [], good_version


def test_invalid_kind_absent_or_outside_set() -> None:
    payload = _manifest()
    del payload["kind"]
    assert _codes(payload) == ["invalid_kind"]
    assert _codes(_manifest(kind="other-kind")) == ["invalid_kind"]


def test_contributions_invalid_shapes() -> None:
    assert _codes(_manifest(contributes="nope")) == ["contributions_invalid"]
    issues = validate_extension_manifest(_manifest(contributes=[42]))
    assert issues == [ManifestIssue("contributions_invalid", "contributes[0]")]
    issues = validate_extension_manifest(
        _manifest(contributes=[{"id": "c_one", "point": "docs.pack", "config": "nope"}])
    )
    assert issues == [ManifestIssue("contributions_invalid", "contributes[0].config")]
    assert _codes(_manifest(contributes=[])) == []


def test_invalid_and_duplicate_contribution_ids() -> None:
    issues = validate_extension_manifest(
        _manifest(contributes=[{"id": "BAD", "point": "docs.pack"}])
    )
    assert issues == [ManifestIssue("invalid_contribution_id", "contributes[0].id")]
    issues = validate_extension_manifest(
        _manifest(
            contributes=[
                {"id": "c_same", "point": "docs.pack"},
                {"id": "c_same", "point": "docs.pack"},
            ]
        )
    )
    assert issues == [ManifestIssue("duplicate_contribution_id", "contributes[1].id")]


def test_unknown_contribution_point() -> None:
    issues = validate_extension_manifest(
        _manifest(contributes=[{"id": "c_one", "point": "docs.unknown_point"}])
    )
    assert issues == [ManifestIssue("unknown_contribution_point", "contributes[0].point")]


def test_executable_contribution_rejected_unconditionally() -> None:
    issues = validate_extension_manifest(
        _manifest(contributes=[{"id": "c_one", "point": "docs.pack", "executable": True}])
    )
    assert issues == [
        ManifestIssue("executable_contribution_rejected", "contributes[0].executable")
    ]
    # D7 pin: builtin grants no exemption of any kind (manifest or entry).
    issues = validate_extension_manifest(
        _manifest(
            builtin=True,
            contributes=[
                {"id": "c_one", "point": "docs.pack"},
                {"id": "c_two", "point": "docs.pack", "executable": True, "builtin": True},
            ],
        )
    )
    assert issues == [
        ManifestIssue("executable_contribution_rejected", "contributes[1].executable")
    ]
    # Truthy non-boolean values reject too; executable: false is tolerated.
    assert _codes(
        _manifest(contributes=[{"id": "c_one", "point": "docs.pack", "executable": "yes"}])
    ) == ["executable_contribution_rejected"]
    assert _codes(
        _manifest(contributes=[{"id": "c_one", "point": "docs.pack", "executable": False}])
    ) == []


def test_permissions_missing() -> None:
    payload = _manifest()
    del payload["permissions"]
    assert validate_extension_manifest(payload) == [
        ManifestIssue("permissions_missing", "permissions")
    ]
    assert _codes(_manifest(permissions="all")) == ["permissions_missing"]


def test_network_and_secret_access_must_be_explicit_false_booleans() -> None:
    issues = validate_extension_manifest(
        _manifest(permissions={"network_access": True, "secret_access": False})
    )
    assert issues == [ManifestIssue("network_access_rejected", "permissions.network_access")]
    issues = validate_extension_manifest(
        _manifest(permissions={"network_access": False, "secret_access": True})
    )
    assert issues == [ManifestIssue("secret_access_rejected", "permissions.secret_access")]
    # Absent or non-boolean flags are rejected the same way (never defaulted).
    assert _codes(_manifest(permissions={"secret_access": False})) == ["network_access_rejected"]
    assert _codes(
        _manifest(permissions={"network_access": 0, "secret_access": False})
    ) == ["network_access_rejected"]


def test_data_scopes_invalid() -> None:
    assert _codes(
        _manifest(
            permissions={"network_access": False, "secret_access": False, "data_scopes": "read"}
        )
    ) == ["data_scopes_invalid"]
    assert _codes(
        _manifest(
            permissions={
                "network_access": False,
                "secret_access": False,
                "data_scopes": ["BAD SCOPE"],
            }
        )
    ) == ["data_scopes_invalid"]


def test_external_url_rejected_anywhere_in_manifest() -> None:
    issues = validate_extension_manifest(
        _manifest(description="see https://example.invalid/page")
    )
    assert issues == [ManifestIssue("external_url_rejected", "description")]
    issues = validate_extension_manifest(
        _manifest(contributes=[{"id": "c_one", "point": "docs.pack", "config": {"src": "http://example.invalid"}}])
    )
    assert issues == [ManifestIssue("external_url_rejected", "contributes[0].config.src")]


def test_external_url_rejected_case_insensitively() -> None:
    # URL schemes are case-insensitive: mixed- and upper-case spellings must
    # reject with the same issue code as lowercase ones.
    for spelling in (
        "see HTTPS://EXAMPLE.INVALID/page",
        "see HtTpS://example.invalid/page",
        "see HTTP://example.invalid",
    ):
        issues = validate_extension_manifest(_manifest(description=spelling))
        assert issues == [ManifestIssue("external_url_rejected", "description")], spelling
    # The scheme separator keeps the substring test from over-matching
    # scheme-free mentions of similar names.
    assert _codes(_manifest(description="uses the httpx client naming")) == []
    assert _codes(_manifest(description="shttp is mentioned without a scheme")) == []


def test_external_url_rejected_for_non_http_schemes() -> None:
    # The rejected-outright contract is scheme-general (D7 endpoints-via-env
    # only): ftp://, s3://, ws://, and any other scheme reject just like
    # http(s)://, so a contribution cannot smuggle a non-http endpoint in.
    issues = validate_extension_manifest(
        _manifest(
            contributes=[
                {"id": "c_one", "point": "docs.pack", "config": {"src": "ftp://host/x"}}
            ]
        )
    )
    assert issues == [ManifestIssue("external_url_rejected", "contributes[0].config.src")]
    for scheme_value in ("s3://bucket/x", "ws://host/socket", "gopher://host/x"):
        assert _codes(_manifest(description=f"see {scheme_value}")) == [
            "external_url_rejected"
        ], scheme_value
    # A bare package name with no scheme separator still does not over-match.
    assert _codes(_manifest(description="uses the httpx client naming")) == []


def test_redactable_value_rejected_for_paths_and_secret_pairs() -> None:
    home_path = str(Path.home() / "marker_cache")
    issues = validate_extension_manifest(_manifest(description=f"cache under {home_path}"))
    assert issues == [ManifestIssue("redactable_value_rejected", None)]
    assert _codes(_manifest(description="marker_token=MARKER_XYZ")) == [
        "redactable_value_rejected"
    ]


# ---------------------------------------------------------------------------
# public_extension_view projection
# ---------------------------------------------------------------------------


def test_public_view_projects_known_fields_and_never_echoes_unknown_keys() -> None:
    payload = _manifest(
        description="declarative marker pack",
        engine="qf-os-0.1",
        builtin=True,
        unknown_top_level="MARKER_XYZ",
        permissions={
            "network_access": False,
            "secret_access": False,
            "data_scopes": ["read_catalog"],
            "unknown_permission": True,
        },
        contributes=[
            {
                "id": "c_one",
                "point": "docs.pack",
                "config": {"docs_root": "docs"},
                "docs": "marker docs",
                "executable": False,
                "builtin": True,
                "unknown_entry_key": "MARKER_XYZ",
            },
            {"id": "c_two", "point": "agent.workflow"},
        ],
    )
    assert validate_extension_manifest(payload) == []

    view = public_extension_view(payload)

    assert set(view) == {
        "id",
        "name",
        "version",
        "kind",
        "description",
        "engine",
        "permissions",
        "contributions",
    }
    assert view["permissions"] == {
        "network_access": False,
        "secret_access": False,
        "data_scopes": ["read_catalog"],
    }
    assert view["contributions"][0] == {
        "id": "c_one",
        "point": "docs.pack",
        "reserved": False,
        "config": {"docs_root": "docs"},
        "docs": "marker docs",
    }
    # A reserved-point contribution is valid but inert.
    assert view["contributions"][1] == {"id": "c_two", "point": "agent.workflow", "reserved": True}
    serialized = json.dumps(view)
    assert "builtin" not in serialized
    assert "executable" not in serialized
    assert "unknown_top_level" not in serialized
    assert "unknown_permission" not in serialized
    assert "unknown_entry_key" not in serialized


def test_public_view_omits_absent_optional_fields() -> None:
    view = public_extension_view(_manifest())
    assert "description" not in view
    assert "engine" not in view
    assert "data_scopes" not in view["permissions"]
    assert view["contributions"] == []


# ---------------------------------------------------------------------------
# Registry scan
# ---------------------------------------------------------------------------


def _write_manifest(root: Path, directory: str, payload: object) -> None:
    target = root / directory
    target.mkdir(parents=True)
    (target / "extension.json").write_text(json.dumps(payload), encoding="utf-8")


def test_scan_orders_by_directory_name_and_skips_non_manifest_entries(tmp_path) -> None:
    root = tmp_path / "extensions"
    root.mkdir()
    _write_manifest(root, "b-second", _manifest(id="qf.marker.two"))
    _write_manifest(root, "a-first", _manifest(id="qf.marker.one"))
    (root / ".hidden-pack").mkdir()
    (root / ".hidden-pack" / "extension.json").write_text(
        json.dumps(_manifest(id="qf.marker.hidden")), encoding="utf-8"
    )
    (root / "no-manifest").mkdir()
    (root / "stray-file.json").write_text("{}", encoding="utf-8")

    rows = scan_extensions(root)

    assert [row["directory"] for row in rows] == ["a-first", "b-second"]
    assert all(row["status"] == "valid" for row in rows)


def test_scan_rejects_duplicate_extension_id_in_later_directory(tmp_path) -> None:
    root = tmp_path / "extensions"
    root.mkdir()
    _write_manifest(root, "a-first", _manifest(id="qf.marker.dup"))
    _write_manifest(root, "b-second", _manifest(id="qf.marker.dup"))

    rows = scan_extensions(root)

    assert rows[0]["directory"] == "a-first"
    assert rows[0]["status"] == "valid"
    assert rows[1]["directory"] == "b-second"
    assert rows[1]["status"] == "rejected"
    assert rows[1]["issues"] == [{"code": "duplicate_extension_id", "field": "id"}]
    assert set(rows[1]) == {"directory", "status", "issues"}


def test_scan_marks_unreadable_manifests(tmp_path) -> None:
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "invalid-json").mkdir()
    (root / "invalid-json" / "extension.json").write_text("{not json", encoding="utf-8")
    (root / "not-utf8").mkdir()
    (root / "not-utf8" / "extension.json").write_bytes(b"\xff\xfe{}")

    rows = scan_extensions(root)

    assert [row["directory"] for row in rows] == ["invalid-json", "not-utf8"]
    for row in rows:
        assert row["status"] == "rejected"
        assert row["issues"] == [{"code": "manifest_unreadable", "field": None}]
        assert set(row) == {"directory", "status", "issues"}


def test_scan_missing_root_returns_empty_list(tmp_path) -> None:
    assert scan_extensions(tmp_path / "does-not-exist") == []


def test_contribution_points_payload_is_the_pinned_catalog() -> None:
    payload = contribution_points_payload()
    assert [row["point"] for row in payload] == list(ALL_CONTRIBUTION_POINTS)
    assert len(payload) == 10
    by_point = {row["point"]: row for row in payload}
    for point in MVP_CONTRIBUTION_POINTS:
        assert by_point[point]["status"] == "supported"
    for point in RESERVED_CONTRIBUTION_POINTS:
        assert by_point[point]["status"] == "reserved"
    assert by_point["docs.pack"]["note"] == "declarative documentation set"
    assert by_point["agent.workflow"]["note"] == "reserved stub; commercial boundary per D6/D7a"
    assert by_point["data.provider_adapter"]["note"] == (
        "reserved; in-repo adapter implementation permitted per D7a, no dynamic loading"
    )


# ---------------------------------------------------------------------------
# In-repo reference manifest
# ---------------------------------------------------------------------------


def test_reference_manifest_in_repo_is_valid_with_zero_issues() -> None:
    manifest_path = _REPO_ROOT / "extensions" / "qf-reference-docs-pack" / "extension.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert validate_extension_manifest(payload) == []

    view = public_extension_view(payload)
    assert view["id"] == "qf.reference.docs-pack"
    assert view["version"] == "0.1.0"
    assert view["kind"] == "docs-extension"
    assert view["permissions"] == {
        "network_access": False,
        "secret_access": False,
        "data_scopes": [],
    }
    assert view["contributions"] == [
        {"id": "repo_docs", "point": "docs.pack", "reserved": False, "config": {"docs_root": "docs"}}
    ]
