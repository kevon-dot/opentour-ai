"""Bind presentation evidence to its own reviewed fonts, source and exact baselines."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.ci.browser_evidence import local_file
from tools.public_content_check import MAX_FILE_BYTES, digest, load_policy

FOUNDATION = "apps/web/components/foundation/tokens/"
TARGET = FOUNDATION + "visual.spec.ts"
PROFILE = "c4865bd5-0fce-4468-b6ea-05003ebfa5a2"
SPEC = "968ff7c59d40afa568e5dfd0ea2488a8906531b38bb3daa00b66c58d8a7f8264"
VIEWPORTS = [
    {"width": 320, "height": 800},
    {"width": 768, "height": 1024},
    {"width": 1280, "height": 900},
    {"width": 1440, "height": 1024},
]
NAMES = (
    "gallery-320",
    "gallery-768",
    "gallery-1280",
    "gallery-1440",
    "focus-primary",
    "focus-secondary",
    "focus-quiet",
    "focus-forced-colors",
)
MODES = ("customer_controlled", "opentour_managed")
OVERLAY_NAMES = tuple(
    f"overlay-{kind}-{width}" for kind in ("dialog", "drawer") for width in (320, 768, 1280, 1440)
)
OVERLAY_SPEC = "429990397f6a755551664d50ad0c80116abf65be8ac8e64e35002cbc18923fdf"
OVERLAY_DESIGN = "a048ee48055c5a3de9089331e41a9e91aa0d64106dde8f8e065a3bd2e093b79d"
OVERLAY_NATIVE = {
    "digest_recipe": (
        "SHA256 UTF-8 JSON sorted keys, compact separators, ASCII escaping: "
        "{issue_description, implementation_profile:{id,content}}; exact native strings"
    ),
    "issue_description_sha256": "8ff8e1c004bc6f6e8fa33f00181337ea82bb6d58b537280b996bd40312a36142",
    "issue_updated_at": "2026-11-16T04:28:36.979Z",
    "implementation_profile_id": PROFILE,
    "implementation_profile_sha256": (
        "323f5919a4e396a4d844cf19f30c5fba2a88e8d9cffe3584405b1d86e2ffac9b"
    ),
    "implementation_profile_updated_at": "2026-11-16T04:28:36.062Z",
}
OVERLAY_APPROVAL = {
    "reviewer": "root-independent-Codex",
    "source_commit": "7c68462c06101d727c2a1f88d3b37f545b16f169",
    "review_sha256": "0370c59eb923acfdfeba0e875fef3f7e7c98a1d1ed2708347798ee8c5f864ad0",
    "baseline_authoring_nonce": "3d93df6163434bbc9b87841a4f27cfcf",
}
CASE_GROUPS = {
    "OPE-253": (36, "146c046a366f26c6ad053dec8959798f8dc26aad1867f59c983df82ba359f0dd"),
    "OPE-300": (84, "fb36fd09a37bcde0fc8461cf476d06579701152c87890bb029f40856e2700580"),
}


def read_json(root: Path, name: str) -> tuple[bytes, dict[str, Any]]:
    file = local_file(root, name)
    if file.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Presentation metadata exceeds bound")
    raw = file.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Presentation metadata must be an object")
    return raw, value


def verify_overlay_components(
    root: Path, value: dict[str, Any], policy: dict[str, str]
) -> dict[tuple[str, str], str]:
    """Keep child provenance separate from the historical parent gallery profile."""
    _, manifest = read_json(root, "docs/specification/manifest.json")
    children = [
        row
        for row in manifest.get("source_projection", {}).get("issues", [])
        if row.get("issue_key") == "OPE-300"
    ]
    if len(children) != 1 or any(
        children[0].get(key) != expected
        for key, expected in {
            "logical_key": "OT-P23-C12-OVERLAY",
            "native_uuid": "b215de00-fc9e-497b-915f-3dc835614bfa",
            "technical_capture_sha256": OVERLAY_SPEC,
            "publication_spec_sha256": OVERLAY_NATIVE["issue_description_sha256"],
            "updated_at": OVERLAY_NATIVE["issue_updated_at"],
        }.items()
    ):
        raise ValueError("Overlay native source capture mismatch")
    groups = {}
    for owner, (count, expected_digest) in CASE_GROUPS.items():
        bindings = [
            row
            for row in manifest.get("test_bindings", [])
            if row.get("issue_key") == owner
            and isinstance(row.get("test_id"), str)
            and row["test_id"].startswith(TARGET + "::")
        ]
        ids = sorted(row["test_id"] for row in bindings)
        if (
            len(ids) != count
            or len(set(ids)) != count
            or digest(json.dumps(ids, separators=(",", ":")).encode()) != expected_digest
        ):
            raise ValueError("Overlay or historical parent case inventory changed")
        for row in bindings:
            if (
                row.get("kind") != "simulated"
                or row.get("mode") not in MODES
                or row.get("fixture_id") != "bootstrap-command-contract"
                or row.get("fixture_version") != "1"
                or not row["test_id"].startswith(f"{TARGET}::{row['mode']}::")
            ):
                raise ValueError("Overlay case provenance mismatch")
        groups[owner] = ids
    all_ids = set(groups["OPE-253"] + groups["OPE-300"])
    observed = value["report"]["tests"]
    observed_ids = [row["id"] for row in observed]
    if (
        len(all_ids) != 120
        or len(set(observed_ids)) != len(observed_ids)
        or not set(observed_ids) <= all_ids
        or (value["report"]["status"] == "PASS" and set(observed_ids) != all_ids)
    ):
        raise ValueError("Overlay observed inventory does not match separate owners")
    for row in observed:
        if (
            row.get("file") != TARGET
            or row.get("mode") not in MODES
            or not row["id"].startswith(f"{TARGET}::{row['mode']}::")
        ):
            raise ValueError("Overlay observed case source mismatch")
    review_raw, review = read_json(root, FOUNDATION + "testing/overlay-baseline-review.json")
    expected_metadata = {
        "schema_version": "opentour.overlay-baseline-review.v1",
        "issue_key": "OPE-300",
        "logical_key": "OT-P23-C12-OVERLAY",
        "spec_sha256": OVERLAY_SPEC,
        "design_image_sha256": OVERLAY_DESIGN,
        "browser_baseline_status": "APPROVED",
        "max_diff_pixels": 0,
        "threshold": 0,
        "masks": [],
        "independent_browser_baseline_review": OVERLAY_APPROVAL,
    }
    if (
        set(review) != set(expected_metadata) | {"images"}
        or any(review.get(key) != expected for key, expected in expected_metadata.items())
        or type(review["max_diff_pixels"]) is not int
        or type(review["threshold"]) not in (int, float)
    ):
        raise ValueError("Overlay independent baseline approval mismatch")
    expected_component = {
        "issue_key": "OPE-300",
        "logical_key": "OT-P23-C12-OVERLAY",
        "spec_sha256": OVERLAY_SPEC,
        "native_specification": OVERLAY_NATIVE,
        "test_ids": groups["OPE-300"],
        "baseline_manifest_sha256": digest(review_raw),
    }
    if value.get("component_specifications") != [expected_component]:
        raise ValueError("Overlay component attribution mismatch")
    expected_baselines = {}
    image_paths = set()
    for mode in MODES:
        for name in OVERLAY_NAMES:
            relative = f"testing/baselines/{value['platform']}-{mode.replace('_', '-')}/{name}.png"
            image_paths.add(relative)
            actual = digest(local_file(root, FOUNDATION + relative).read_bytes())
            if (
                review.get("images", {}).get(relative) != actual
                or policy.get(FOUNDATION + relative) != actual
            ):
                raise ValueError("Overlay baseline hash mismatch")
            expected_baselines[(mode, name)] = actual
    if set(review.get("images", {})) != image_paths:
        raise ValueError("Overlay baseline inventory mismatch")
    return expected_baselines


def verify_foundation_evidence(root: Path, value: dict[str, Any]) -> None:
    if (
        value.get("fixture_id") != "presentation-foundation-v1"
        or value.get("seed") != 253
        or value.get("spec_sha256") != SPEC
        or value.get("viewport_sizes") != VIEWPORTS
        or value.get("source_refs") != ["OPE-253", PROFILE]
        or "font_sha256" in value
        or value.get("platform") not in {"darwin", "linux"}
    ):
        raise ValueError("Presentation profile identity mismatch")
    policy = load_policy(root)["reviewed_binary_sha256"]
    fonts_raw, font_source = read_json(root, "apps/web/styles/fonts/source.json")
    expected_fonts = []
    for family in ("Geist", "GeistMono"):
        path = f"apps/web/styles/fonts/{family}.woff2"
        actual = digest(local_file(root, path).read_bytes())
        if (
            policy.get(path) != actual
            or sum(
                item.get("family") == family and item.get("sha256") == actual
                for item in font_source.get("fonts", [])
                if isinstance(item, dict)
            )
            != 1
        ):
            raise ValueError("Presentation font provenance mismatch")
        expected_fonts.append({"family": family, "path": path, "sha256": actual})
    if value.get("fonts") != expected_fonts or value.get("font_manifest_sha256") != digest(
        fonts_raw
    ):
        raise ValueError("Presentation font manifest mismatch")
    tokens_raw, tokens = read_json(root, "apps/web/styles/product-tokens.json")
    if value.get("token_manifest_sha256") != digest(tokens_raw) or value.get(
        "design"
    ) != tokens.get("design"):
        raise ValueError("Presentation token manifest mismatch")
    review_raw, review = read_json(root, FOUNDATION + "testing/baseline-review.json")
    if (
        value.get("baseline_manifest_sha256") != digest(review_raw)
        or review.get("profile_id") != PROFILE
        or review.get("issue_key") != "OPE-253"
        or review.get("browser_baseline_status") != "APPROVED"
        or review.get("max_diff_pixels") != 0
        or review.get("threshold") != 0
        or review.get("masks") != []
    ):
        raise ValueError("Presentation baseline approval mismatch")
    expected_baselines = {}
    for mode in MODES:
        for name in NAMES:
            baseline_path = (
                f"testing/baselines/{value['platform']}-{mode.replace('_', '-')}/{name}.png"
            )
            actual = digest(local_file(root, FOUNDATION + baseline_path).read_bytes())
            if (
                review.get("images", {}).get(baseline_path) != actual
                or policy.get(FOUNDATION + baseline_path) != actual
            ):
                raise ValueError("Presentation baseline hash mismatch")
            expected_baselines[(mode, name)] = actual
    expected_baselines.update(verify_overlay_components(root, value, policy))
    declared_sources = value.get("presentation_sources")
    if not isinstance(declared_sources, list):
        raise ValueError("Missing presentation source inventory")
    expected_sources = {}
    for file in (root / FOUNDATION).rglob("*"):
        if file.is_symlink():
            raise ValueError("Presentation source link refused")
        if file.is_file() and "/testing/baselines/" not in str(file):
            relative = str(file.relative_to(root))
            expected_sources[relative] = digest(local_file(root, relative).read_bytes())
    actual_sources = {}
    for item in declared_sources:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or item["path"] in actual_sources
        ):
            raise ValueError("Invalid presentation source inventory")
        actual_sources[item["path"]] = item["sha256"]
    if actual_sources != expected_sources:
        raise ValueError("Presentation source inventory changed")
    outputs = {item["path"]: item["sha256"] for item in value["outputs"]}
    comparisons: set[tuple[str, str]] = set()
    for path in outputs:
        name = Path(path).stem
        if not path.endswith(".json") or name not in NAMES + OVERLAY_NAMES:
            continue
        _, capture = read_json(root, path)
        captured_mode = capture.get("mode")
        if not isinstance(captured_mode, str):
            raise ValueError("Presentation mode must be explicit")
        key = (captured_mode, name)
        png = path.removesuffix(".json") + ".png"
        width = (
            int(name.rsplit("-", 1)[1])
            if name.startswith(("gallery-", "overlay-"))
            else (768 if name == "focus-forced-colors" else 1440)
        )
        viewport = next(item for item in VIEWPORTS if item["width"] == width)
        if (
            set(capture)
            != {
                "name",
                "mode",
                "raw_png_sha256",
                "viewport",
                "threshold",
                "max_diff_pixels",
                "masks",
            }
            or key not in expected_baselines
            or key in comparisons
            or capture.get("name") != name
            or capture.get("viewport") != viewport
            or capture.get("threshold") != 0
            or capture.get("max_diff_pixels") != 0
            or capture.get("masks") != []
            or png not in outputs
            or capture.get("raw_png_sha256") != outputs[png]
        ):
            raise ValueError("Presentation screenshot binding mismatch")
        if value["report"]["status"] == "PASS" and outputs[png] != expected_baselines[key]:
            raise ValueError("Passing presentation screenshot differs from approved baseline")
        comparisons.add(key)
    if value["report"]["status"] == "PASS" and comparisons != set(expected_baselines):
        raise ValueError("Passing presentation report lacks every reviewed screenshot")
