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


def read_json(root: Path, name: str) -> tuple[bytes, dict[str, Any]]:
    file = local_file(root, name)
    if file.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Presentation metadata exceeds bound")
    raw = file.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Presentation metadata must be an object")
    return raw, value


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
        if not path.endswith(".json") or name not in NAMES:
            continue
        _, capture = read_json(root, path)
        captured_mode = capture.get("mode")
        if not isinstance(captured_mode, str):
            raise ValueError("Presentation mode must be explicit")
        key = (captured_mode, name)
        png = path.removesuffix(".json") + ".png"
        width = (
            int(name.removeprefix("gallery-"))
            if name.startswith("gallery-")
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
