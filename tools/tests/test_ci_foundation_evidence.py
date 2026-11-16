"""Controlled evidence mutations test disclosure/provenance, not rendered product behavior."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from tools.ci.browser_evidence import DEFAULT_BROWSER_TARGET, retain_browser_evidence
from tools.ci.foundation_evidence import FOUNDATION, MODES, NAMES, PROFILE, SPEC, TARGET, VIEWPORTS
from tools.ci.run import browser_selection, run_browser
from tools.public_content_check import digest
from tools.test_runner import CommandError


def fixture(root: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
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
    outputs = []
    nonce = "a" * 32
    prefix = "evidence/OT-P23-C12/runs/" + nonce + "/"
    for mode in MODES:
        for name in NAMES:
            image = b"generated-png-fixture-" + (mode + name).encode()
            baseline = f"testing/baselines/darwin-{mode.replace('_', '-')}/{name}.png"
            sha = write(FOUNDATION + baseline, image)
            images[baseline] = sha
            policy["reviewed_binary_sha256"][FOUNDATION + baseline] = sha
            png_path = prefix + f"artifacts/{mode}/{name}.png"
            write(png_path, image)
            width = (
                int(name.split("-")[1])
                if name.startswith("gallery-")
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
    write(".github/public-content-policy.json", encode(policy))
    tests = [
        {"id": "generated-" + mode, "file": TARGET, "status": "passed", "mode": mode}
        for mode in MODES
    ]
    report: dict[str, Any] = {
        "binding_nonce": nonce,
        "status": "PASS",
        "collected": 2,
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
        "presentation_sources": [
            {"path": TARGET, "sha256": target_sha},
            {"path": review_path, "sha256": review_sha},
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
    assert result["manifest"] == value and result["verified_output_files"] == 32
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
