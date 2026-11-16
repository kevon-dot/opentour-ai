"""Controlled evidence mutations test disclosure/provenance, not rendered product behavior."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from tools.ci.browser_evidence import DEFAULT_BROWSER_TARGET, retain_browser_evidence
from tools.ci.foundation_evidence import (
    FOUNDATION,
    MODES,
    NAMES,
    OVERLAY_NAMES,
    OVERLAY_NATIVE,
    OVERLAY_SPEC,
    PROFILE,
    SPEC,
    TARGET,
    VIEWPORTS,
)
from tools.ci.run import browser_selection, run_browser
from tools.public_content_check import digest
from tools.test_runner import CommandError


def fixture(root: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    source_root = Path(__file__).resolve().parents[2]

    def write(name: str, content: bytes) -> str:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return digest(content)

    def encode(value: Any) -> bytes:
        return json.dumps(value, sort_keys=True).encode()

    policy = json.loads(
        (Path(__file__).resolve().parents[2] / ".github/public-content-policy.json").read_text()
    )
    policy["reviewed_binary_sha256"] = {}
    source_fonts = []
    fonts = []
    for family in ("Geist", "GeistMono"):
        path = f"apps/web/styles/fonts/{family}.woff2"
        sha = write(path, b"generated-font-container-" + family.encode())
        source_fonts.append({"family": family, "sha256": sha})
        fonts.append({"family": family, "path": path, "sha256": sha})
        policy["reviewed_binary_sha256"][path] = sha
    font_sha = write("apps/web/styles/fonts/source.json", encode({"fonts": source_fonts}))
    design = {"fixture": "generated-design", "focus_profile": PROFILE}
    token_sha = write("apps/web/styles/product-tokens.json", encode({"design": design}))
    target_sha = write(TARGET, b"// Generated presentation specification")
    images = {}
    overlay_images = {}
    outputs = []
    nonce = "a" * 32
    prefix = "evidence/OT-P23-C12/runs/" + nonce + "/"
    for mode in MODES:
        for name in NAMES + OVERLAY_NAMES:
            baseline = f"testing/baselines/darwin-{mode.replace('_', '-')}/{name}.png"
            image = (
                (source_root / FOUNDATION / baseline).read_bytes()
                if name in OVERLAY_NAMES
                else b"generated-png-fixture-" + (mode + name).encode()
            )
            sha = write(FOUNDATION + baseline, image)
            (overlay_images if name in OVERLAY_NAMES else images)[baseline] = sha
            policy["reviewed_binary_sha256"][FOUNDATION + baseline] = sha
            png_path = prefix + f"artifacts/{mode}/{name}.png"
            write(png_path, image)
            width = (
                int(name.rsplit("-", 1)[1])
                if name.startswith(("gallery-", "overlay-"))
                else (768 if name == "focus-forced-colors" else 1440)
            )
            capture = {
                "name": name,
                "mode": mode,
                "raw_png_sha256": sha,
                "viewport": next(v for v in VIEWPORTS if v["width"] == width),
                "threshold": 0,
                "max_diff_pixels": 0,
                "masks": [],
            }
            path = png_path.removesuffix(".png") + ".json"
            capture_sha = write(path, encode(capture))
            outputs.extend(
                [{"path": png_path, "sha256": sha}, {"path": path, "sha256": capture_sha}]
            )
    review = {
        "issue_key": "OPE-253",
        "profile_id": PROFILE,
        "browser_baseline_status": "APPROVED",
        "max_diff_pixels": 0,
        "threshold": 0,
        "masks": [],
        "images": images,
    }
    review_path = FOUNDATION + "testing/baseline-review.json"
    review_sha = write(review_path, encode(review))
    overlay_path = FOUNDATION + "testing/overlay-baseline-review.json"
    overlay_raw = (source_root / overlay_path).read_bytes()
    assert json.loads(overlay_raw)["images"] == overlay_images
    overlay_sha = write(overlay_path, overlay_raw)
    write(".github/public-content-policy.json", encode(policy))
    current = json.loads(
        (Path(__file__).resolve().parents[2] / "docs/specification/manifest.json").read_text()
    )
    selected = [
        row
        for row in current["test_bindings"]
        if row["issue_key"] in {"OPE-253", "OPE-300"} and row["test_id"].startswith(TARGET + "::")
    ]
    write(
        "docs/specification/manifest.json",
        encode(
            {
                "source_projection": {
                    "issues": [
                        row
                        for row in current["source_projection"]["issues"]
                        if row["issue_key"] == "OPE-300"
                    ]
                },
                "test_bindings": selected,
            }
        ),
    )
    tests = [
        {"id": row["test_id"], "file": TARGET, "status": "passed", "mode": row["mode"]}
        for row in selected
    ]
    report: dict[str, Any] = {
        "binding_nonce": nonce,
        "status": "PASS",
        "collected": len(tests),
        "tests": tests,
    }
    value: dict[str, Any] = {
        "schema_version": "opentour.browser-artifacts.v1",
        "issue_key": "OPE-253",
        "kind": "simulated",
        "product_acceptance": "NOT_RUN",
        "git_worktree_dirty": False,
        "commit_sha": "b" * 40,
        "report": report,
        "target": TARGET,
        "test_source_sha256": target_sha,
        "baseline_review_request": None,
        "outputs": outputs,
        "fixture_id": "presentation-foundation-v1",
        "seed": 253,
        "spec_sha256": SPEC,
        "viewport_sizes": VIEWPORTS,
        "source_refs": ["OPE-253", PROFILE],
        "platform": "darwin",
        "fonts": fonts,
        "font_manifest_sha256": font_sha,
        "token_manifest_sha256": token_sha,
        "design": design,
        "baseline_manifest_sha256": review_sha,
        "component_specifications": [
            {
                "issue_key": "OPE-300",
                "logical_key": "OT-P23-C12-OVERLAY",
                "spec_sha256": OVERLAY_SPEC,
                "native_specification": OVERLAY_NATIVE,
                "test_ids": sorted(
                    row["test_id"] for row in selected if row["issue_key"] == "OPE-300"
                ),
                "baseline_manifest_sha256": overlay_sha,
            }
        ],
        "presentation_sources": [
            {"path": TARGET, "sha256": target_sha},
            {"path": review_path, "sha256": review_sha},
            {"path": overlay_path, "sha256": overlay_sha},
        ],
    }
    manifest_path = root / (prefix + "evidence.json")
    manifest_path.write_bytes(encode(value))
    transport = copy.deepcopy(report)
    for test in transport["tests"]:
        test["id"] = "sha256:" + digest(test["id"].encode())
    return (
        manifest_path,
        value,
        {"commit_sha": "b" * 40, "steps": [{"runner": "playwright", "tests": transport}]},
    )


def test_exact_foundation_inventory_is_retained_without_raw_upload(tmp_path: Path) -> None:
    path, value, command = fixture(tmp_path)
    result = retain_browser_evidence(tmp_path, command, TARGET)
    assert result["manifest_sha256"] == digest(path.read_bytes())
    assert result["manifest"] == value and result["verified_output_files"] == 64
    assert result["raw_artifacts_uploaded"] is False


@pytest.mark.parametrize(
    "field,changed",
    [
        ("issue_key", "OPE-127"),
        ("fixture_id", "simulated-browser-harness"),
        ("seed", 127),
        ("spec_sha256", "c" * 64),
        ("source_refs", ["OPE-127"]),
        ("font_manifest_sha256", "c" * 64),
        ("token_manifest_sha256", "c" * 64),
        ("baseline_manifest_sha256", "c" * 64),
        ("presentation_sources", []),
        ("fonts", []),
        ("design", {}),
        ("viewport_sizes", []),
        ("font_sha256", "c" * 64),
        ("platform", "unknown"),
        ("product_acceptance", "PASS"),
        ("git_worktree_dirty", True),
        ("baseline_review_request", {"reason": "authoring"}),
    ],
)
def test_cross_profile_or_unbound_metadata_is_not_publishable(
    tmp_path: Path, field: str, changed: Any
) -> None:
    path, value, command = fixture(tmp_path)
    value[field] = changed
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        retain_browser_evidence(tmp_path, command, TARGET)


@pytest.mark.parametrize(
    "change",
    [
        "font",
        "token",
        "baseline",
        "missing-source",
        "unlisted-source",
        "source-link",
        "font-link",
        "unapproved-font",
    ],
)
def test_changed_runtime_material_cannot_reuse_old_evidence(tmp_path: Path, change: str) -> None:
    _, _, command = fixture(tmp_path)
    if change in {"font", "font-link"}:
        path = tmp_path / "apps/web/styles/fonts/Geist.woff2"
    elif change == "token":
        path = tmp_path / "apps/web/styles/product-tokens.json"
    elif change == "baseline":
        path = next((tmp_path / FOUNDATION / "testing/baselines").rglob("*.png"))
    elif change in {"missing-source", "source-link"}:
        path = tmp_path / TARGET
    elif change == "unlisted-source":
        path = tmp_path / FOUNDATION / "unexpected.ts"
    else:
        path = tmp_path / ".github/public-content-policy.json"
        policy = json.loads(path.read_text())
        del policy["reviewed_binary_sha256"]["apps/web/styles/fonts/Geist.woff2"]
        path.write_text(json.dumps(policy))
        with pytest.raises(ValueError):
            retain_browser_evidence(tmp_path, command, TARGET)
        return
    if change.endswith("link"):
        other = tmp_path / "generated-backup"
        other.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(other)
    elif change == "missing-source":
        path.unlink()
    else:
        path.write_bytes(b"generated-changed-content")
    with pytest.raises(ValueError):
        retain_browser_evidence(tmp_path, command, TARGET)


@pytest.mark.parametrize(
    "change",
    ["missing", "duplicate", "tolerance", "viewport", "wrong-mode", "digest", "different-baseline"],
)
def test_passing_result_requires_complete_exact_visual_bindings(
    tmp_path: Path, change: str
) -> None:
    path, value, command = fixture(tmp_path)
    output = next(item for item in value["outputs"] if item["path"].endswith("gallery-320.json"))
    capture_path = tmp_path / output["path"]
    capture = json.loads(capture_path.read_text())
    if change == "missing":
        value["outputs"] = [item for item in value["outputs"] if item != output]
    elif change == "duplicate":
        value["outputs"].append(dict(output))
    elif change == "different-baseline":
        png = output["path"].removesuffix(".json") + ".png"
        (tmp_path / png).write_bytes(b"generated-changed-png")
        sha = digest((tmp_path / png).read_bytes())
        capture["raw_png_sha256"] = sha
        next(item for item in value["outputs"] if item["path"] == png)["sha256"] = sha
    else:
        field, altered = {
            "tolerance": ("max_diff_pixels", 14),
            "viewport": ("viewport", {"width": 320, "height": 1100}),
            "wrong-mode": ("mode", "foreign"),
            "digest": ("raw_png_sha256", "c" * 64),
        }[change]
        capture[field] = altered
    if change not in {"missing", "duplicate"}:
        capture_path.write_text(json.dumps(capture))
        output["sha256"] = digest(capture_path.read_bytes())
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        retain_browser_evidence(tmp_path, command, TARGET)


def test_bound_failure_retains_partial_output_but_is_never_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, value, command = fixture(tmp_path)
    value["report"]["status"] = "FAIL"
    command["steps"][0]["tests"]["status"] = "FAIL"
    value["outputs"] = value["outputs"][:2]
    path.write_text(json.dumps(value))
    from tools.ci import run as ci

    monkeypatch.setattr(
        ci, "run_one", lambda *a, **kw: {"status": "FAIL", "command_evidence": command}
    )
    result = run_browser(tmp_path, {}, TARGET)
    assert result["status"] == "FAIL" and result["browser_artifacts"]["verified_output_files"] == 2


def test_selection_routes_every_present_known_suite_and_refuses_links(tmp_path: Path) -> None:
    assert browser_selection(tmp_path) == [DEFAULT_BROWSER_TARGET]
    target = tmp_path / TARGET
    target.parent.mkdir(parents=True)
    target.write_text("// generated")
    assert browser_selection(tmp_path) == [DEFAULT_BROWSER_TARGET, TARGET]
    target.unlink()
    target.symlink_to(tmp_path / "missing")
    with pytest.raises(CommandError):
        browser_selection(tmp_path)


def test_foreign_browser_profile_is_rejected_before_execution(tmp_path: Path) -> None:
    with pytest.raises(CommandError):
        run_browser(tmp_path, {}, "apps/web/unowned.spec.ts")
    with pytest.raises(ValueError):
        retain_browser_evidence(tmp_path, {}, "apps/web/unowned.spec.ts")


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "extra-component",
        "foreign-field",
        "parent-identity",
        "stale-spec",
        "stale-native",
        "duplicate-id",
        "missing-id",
        "parent-id",
        "baseline-digest",
    ],
)
def test_overlay_child_attribution_is_closed_and_separate(tmp_path: Path, change: str) -> None:
    path, value, command = fixture(tmp_path)
    component = value["component_specifications"][0]
    if change == "missing":
        del value["component_specifications"]
    elif change == "extra-component":
        value["component_specifications"].append(copy.deepcopy(component))
    elif change == "foreign-field":
        component["unreviewed"] = True
    elif change == "parent-identity":
        component["issue_key"] = "OPE-253"
    elif change == "stale-spec":
        component["spec_sha256"] = SPEC
    elif change == "stale-native":
        component["native_specification"] = {
            **OVERLAY_NATIVE,
            "issue_updated_at": "2026-01-01T00:00:00Z",
        }
    elif change == "duplicate-id":
        component["test_ids"].append(component["test_ids"][0])
    elif change == "missing-id":
        component["test_ids"].pop()
    elif change == "parent-id":
        component["test_ids"][0] = next(
            row["id"] for row in value["report"]["tests"] if row["id"] not in component["test_ids"]
        )
    else:
        component["baseline_manifest_sha256"] = "c" * 64
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        retain_browser_evidence(tmp_path, command, TARGET)


@pytest.mark.parametrize(
    "change",
    [
        "swapped-owner",
        "missing-binding",
        "duplicate-binding",
        "wrong-mode",
        "stale-capture",
        "stale-publication",
        "stale-revision",
    ],
)
def test_overlay_requires_reviewed_native_capture_and_case_owners(
    tmp_path: Path, change: str
) -> None:
    _, _, command = fixture(tmp_path)
    manifest_path = tmp_path / "docs/specification/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    row = next(row for row in manifest["test_bindings"] if row["issue_key"] == "OPE-300")
    if change == "swapped-owner":
        row["issue_key"] = "OPE-253"
    elif change == "missing-binding":
        manifest["test_bindings"].remove(row)
    elif change == "duplicate-binding":
        manifest["test_bindings"].append(dict(row))
    elif change == "wrong-mode":
        row["mode"] = (
            "opentour_managed" if row["mode"] == "customer_controlled" else "customer_controlled"
        )
    else:
        key = {
            "stale-capture": "technical_capture_sha256",
            "stale-publication": "publication_spec_sha256",
            "stale-revision": "updated_at",
        }[change]
        manifest["source_projection"]["issues"][0][key] = "changed"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        retain_browser_evidence(tmp_path, command, TARGET)


@pytest.mark.parametrize(
    "change",
    [
        "approval",
        "design",
        "extra-image",
        "tolerance",
        "boolean-zero",
        "missing-png",
        "different-png",
    ],
)
def test_overlay_reference_cannot_be_silently_reapproved(tmp_path: Path, change: str) -> None:
    path, value, command = fixture(tmp_path)
    review_path = tmp_path / FOUNDATION / "testing/overlay-baseline-review.json"
    review = json.loads(review_path.read_text())
    if change == "approval":
        review["independent_browser_baseline_review"]["review_sha256"] = "c" * 64
    elif change == "design":
        review["design_image_sha256"] = "c" * 64
    elif change == "extra-image":
        review["images"]["unreviewed.png"] = "c" * 64
    elif change == "tolerance":
        review["max_diff_pixels"] = 1
    elif change == "boolean-zero":
        review["max_diff_pixels"] = False
    else:
        png = tmp_path / FOUNDATION / next(iter(review["images"]))
        if change == "missing-png":
            png.unlink()
        else:
            png.write_bytes(b"generated-changed-overlay")
    review_path.write_text(json.dumps(review))
    sha = digest(review_path.read_bytes())
    value["component_specifications"][0]["baseline_manifest_sha256"] = sha
    next(
        item
        for item in value["presentation_sources"]
        if item["path"].endswith("/overlay-baseline-review.json")
    )["sha256"] = sha
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        retain_browser_evidence(tmp_path, command, TARGET)


def test_passing_overlay_run_cannot_omit_one_new_comparison(tmp_path: Path) -> None:
    path, value, command = fixture(tmp_path)
    value["outputs"] = [
        item
        for item in value["outputs"]
        if not item["path"].endswith("customer_controlled/overlay-dialog-320.json")
    ]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="every reviewed screenshot"):
        retain_browser_evidence(tmp_path, command, TARGET)


def test_coordinated_overlay_replacement_cannot_reuse_an_old_approval(tmp_path: Path) -> None:
    path, value, command = fixture(tmp_path)
    review_relative = FOUNDATION + "testing/overlay-baseline-review.json"
    review_path = tmp_path / review_relative
    review = json.loads(review_path.read_bytes())
    baseline = "testing/baselines/darwin-customer-controlled/overlay-dialog-320.png"
    altered = b"generated-coordinated-replacement"
    altered_sha = digest(altered)
    (tmp_path / FOUNDATION / baseline).write_bytes(altered)
    review["images"][baseline] = altered_sha
    # Keep every old independent approval reference while changing all self-reported hashes.
    review_path.write_text(json.dumps(review))
    review_sha = digest(review_path.read_bytes())
    policy_path = tmp_path / ".github/public-content-policy.json"
    policy = json.loads(policy_path.read_text())
    policy["reviewed_binary_sha256"][FOUNDATION + baseline] = altered_sha
    policy_path.write_text(json.dumps(policy))
    value["component_specifications"][0]["baseline_manifest_sha256"] = review_sha
    next(row for row in value["presentation_sources"] if row["path"] == review_relative)[
        "sha256"
    ] = review_sha
    capture_row = next(
        row
        for row in value["outputs"]
        if row["path"].endswith("customer_controlled/overlay-dialog-320.json")
    )
    capture_path = tmp_path / capture_row["path"]
    capture = json.loads(capture_path.read_text())
    capture["raw_png_sha256"] = altered_sha
    capture_path.write_text(json.dumps(capture))
    capture_row["sha256"] = digest(capture_path.read_bytes())
    png = capture_row["path"].removesuffix(".json") + ".png"
    (tmp_path / png).write_bytes(altered)
    next(row for row in value["outputs"] if row["path"] == png)["sha256"] = altered_sha
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="exact independently reviewed bytes"):
        retain_browser_evidence(tmp_path, command, TARGET)
