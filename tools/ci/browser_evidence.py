"""Retain only a verified browser artifact inventory; raw traces stay unexported."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from tools.public_content_check import MAX_FILE_BYTES, digest, inspect_bytes, load_policy

PRESENTATION_TARGETS = {
    "apps/web/components/foundation/forms/derived-value.visual.spec.ts": "OPE-320",
    "apps/web/components/foundation/data-display/relationship-context.visual.spec.ts": "OPE-319",
    "apps/web/components/foundation/tokens/selection-controls.visual.spec.ts": "OPE-318",
    "apps/web/components/foundation/forms/single-choice-select.visual.spec.ts": "OPE-317",
    "apps/web/components/foundation/forms/text-field.visual.spec.ts": "OPE-316",
    "apps/web/components/foundation/tokens/skeleton.visual.spec.ts": "OPE-310",
    "apps/web/components/foundation/tokens/icon-button.visual.spec.ts": "OPE-311",
    "apps/web/components/foundation/tokens/status-icon.visual.spec.ts": "OPE-312",
    "apps/web/components/foundation/tokens/tooltip.visual.spec.ts": "OPE-313",
    "apps/web/components/foundation/forms/difference-review.visual.spec.ts": "OPE-314",
    "apps/web/features/operations/cancellation-impact.visual.spec.ts": "OPE-315",
    "apps/web/features/workspace/customer-shell.visual.spec.ts": "OPE-307",
    "apps/web/components/foundation/data-display/chart-evidence.visual.spec.ts": "OPE-303",
    "apps/web/components/foundation/data-display/table-populations.visual.spec.ts": "OPE-305",
    "apps/web/components/foundation/data-display/metric-semantics.visual.spec.ts": "OPE-304",
    "apps/web/components/foundation/async-state/status-copy.visual.spec.ts": "OPE-306",
    "apps/web/components/foundation/async-state/operation-state.visual.spec.ts": "OPE-308",
}
BROWSER_PROFILES = {
    "apps/web/tests/browser-harness.browser.spec.ts": ("OPE-127", "OT-BOOT-C06"),
    "apps/web/components/foundation/tokens/visual.spec.ts": ("OPE-253", "OT-P23-C12"),
    **{target: (issue, issue) for target, issue in PRESENTATION_TARGETS.items()},
}
DEFAULT_BROWSER_TARGET = "apps/web/tests/browser-harness.browser.spec.ts"


def local_file(root: Path, relative: str) -> Path:
    candidate = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Unsafe browser evidence path")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("Browser evidence escapes repository")
    current = candidate
    while current != root:
        if current.is_symlink():
            raise ValueError("Browser evidence link refused")
        current = current.parent
    if not candidate.is_file():
        raise ValueError("Browser evidence is missing")
    return candidate


def retain_browser_evidence(
    root: Path, command: dict[str, Any], target: str = DEFAULT_BROWSER_TARGET
) -> dict[str, Any]:
    if target not in BROWSER_PROFILES:
        raise ValueError("Unknown browser evidence profile")
    issue, evidence_root = BROWSER_PROFILES[target]
    steps = command.get("steps", [])
    if len(steps) != 1 or steps[0].get("runner") != "playwright":
        raise ValueError("Unexpected browser command inventory")
    report = steps[0]["tests"]
    nonce = report.get("binding_nonce")
    if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{32}", nonce):
        raise ValueError("Browser report nonce is invalid")
    prefix = "evidence/" + evidence_root + "/runs/" + nonce + "/"
    path = local_file(root, prefix + "evidence.json")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Browser manifest exceeds bound")
    raw = path.read_bytes()
    value = json.loads(raw)
    normalized_report = copy.deepcopy(value.get("report"))
    if not isinstance(normalized_report, dict) or not isinstance(
        normalized_report.get("tests"), list
    ):
        raise ValueError("Browser manifest has no actual test inventory")
    for test in normalized_report["tests"]:
        if not isinstance(test, dict) or not isinstance(test.get("id"), str):
            raise ValueError("Browser test identity is invalid")
        test["id"] = "sha256:" + digest(test["id"].encode())
    if (
        value.get("schema_version") != "opentour.browser-artifacts.v1"
        or value.get("issue_key") != issue
        or value.get("kind") != "simulated"
        or value.get("product_acceptance") != "NOT_RUN"
        or value.get("git_worktree_dirty") is not False
        or value.get("commit_sha") != command.get("commit_sha")
        or normalized_report != report
        or value.get("target") != target
        or value.get("baseline_review_request") is not None
    ):
        raise ValueError("Browser manifest source/report binding failed")
    source = local_file(root, value["target"])
    if value.get("test_source_sha256") != digest(source.read_bytes()):
        raise ValueError("Browser test source changed")
    outputs = value.get("outputs")
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= 1000:
        raise ValueError("Browser output inventory is missing or unbounded")
    seen: set[str] = set()
    total = 0
    for output in outputs:
        if not isinstance(output, dict) or set(output) != {"path", "sha256"}:
            raise ValueError("Invalid browser artifact record")
        name = output["path"]
        if (
            not isinstance(name, str)
            or not (name.startswith(prefix + "artifacts/") or name == prefix + "runner.log")
            or name in seen
        ):
            raise ValueError("Browser artifact scope mismatch")
        seen.add(name)
        file = local_file(root, name)
        size = file.stat().st_size
        total += size
        if size > 33_554_432 or total > 134_217_728:
            raise ValueError("Browser artifact inventory exceeds verification bound")
        actual = hashlib.sha256()
        with file.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65_536), b""):
                actual.update(chunk)
        if actual.hexdigest() != output["sha256"]:
            raise ValueError("Browser artifact digest mismatch")
    if inspect_bytes("browser-manifest.json", raw, load_policy(root), binary_content=True):
        raise ValueError("Browser manifest disclosure refused")
    if target in PRESENTATION_TARGETS:
        verify_scoped_presentation(root, value)
    if issue == "OPE-253":
        from tools.ci.foundation_evidence import verify_foundation_evidence

        verify_foundation_evidence(root, value)
    return {
        "manifest_sha256": digest(raw),
        "manifest": value,
        "verified_output_files": len(outputs),
        "verified_output_bytes": total,
        "raw_artifacts_uploaded": False,
    }


def verify_scoped_presentation(root: Path, value: dict[str, Any]) -> None:
    report = value["report"]
    if (
        value.get("fixture_id") != "scoped-presentation-v1"
        or value.get("font_sha256")
        != digest(local_file(root, "apps/web/styles/fonts/Geist.woff2").read_bytes())
        or any(case["mode"] is not None for case in report["tests"])
    ):
        raise ValueError("Scoped presentation fixture identity mismatch")
    if report["status"] != "PASS":
        return  # Preserve the verified failure inventory, including incomplete captures.
    images = {
        record["path"]: record["sha256"]
        for record in value["outputs"]
        if record["path"].endswith(".png") and "/attachments/" not in record["path"]
    }
    widths = (320, 768, 1280, 1440)
    if value["issue_key"] == "OPE-304":
        expected = {
            (width, scale, name)
            for width in widths
            for scale in (100, 200)
            for name in ("metrics", "source")
        }
        cases = [
            re.search(
                r"at-(320|768|1280|1440)px-and-(100|200)-text-presentation/(metrics|source)\.png$",
                name,
            )
            for name in images
        ]
        if (
            len(cases) != len(expected)
            or {(int(case[1]), int(case[2]), case[3]) for case in cases if case} != expected
        ):
            raise ValueError("Metric presentation capture inventory mismatch")
    elif value["issue_key"] == "OPE-310":
        expected_skeletons = {
            f"skeleton-{mode}-{variant}-{width}-{scale}-{state}.png"
            for mode in ("customer-controlled", "opentour-managed")
            for variant in ("rows", "card")
            for width in widths
            for scale in (100, 200)
            for state in ("loading", "ready", "specimen")
        } | {
            f"skeleton-{mode}-{variant}-{treatment}.png"
            for mode in ("customer-controlled", "opentour-managed")
            for variant in ("rows", "card")
            for treatment in ("reduced-motion", "forced-colors")
        }
        if (
            len(images) != len(expected_skeletons)
            or {Path(name).name for name in images} != expected_skeletons
        ):
            raise ValueError("Skeleton presentation capture inventory mismatch")
    elif value["issue_key"] == "OPE-311":
        expected_icons = {
            f"icon-button-{width}-{scale}-{state}.png"
            for width in widths
            for scale in (100, 200)
            for state in ("default", "hover", "focus", "disabled", "loading")
        }
        actual_icons = {Path(path).name for path in images}
        if len(images) != len(expected_icons) or actual_icons != expected_icons:
            raise ValueError("Icon button capture inventory mismatch")
    elif value["issue_key"] == "OPE-312":
        modes = ("customer-controlled", "opentour-managed")
        expected_icons = (
            {
                f"status-icons-{mode}-{width}-{scale}.png"
                for mode in modes
                for width in widths
                for scale in (100, 200)
            }
            | {
                f"status-icon-native-{mode}-{tone}.png"
                for mode in modes
                for tone in ("success", "hold", "failed", "unknown", "info")
            }
            | {f"status-icon-composition-{mode}.png" for mode in modes}
            | {
                f"status-icons-{mode}-{treatment}.png"
                for mode in modes
                for treatment in ("forced-colors", "reduced-motion")
            }
        )
        if (
            len(images) != len(expected_icons)
            or {Path(name).name for name in images} != expected_icons
        ):
            raise ValueError("Status-icon presentation capture inventory mismatch")
    elif value["issue_key"] == "OPE-313":
        expected_tooltips = {
            f"tooltip-{kind}-{width}-{scale}.png"
            for kind in ("evidence", "help", "long")
            for width in widths for scale in (100, 200)
        } | {"tooltip-forced-colors.png"}
        if (
            len(images) != len(expected_tooltips)
            or {Path(name).name for name in images} != expected_tooltips
        ):
            raise ValueError("Tooltip presentation capture inventory mismatch")
    elif value["issue_key"] == "OPE-314":
        expected_cards = {
            f"difference-{width}-{scale}-{case}.png"
            for width in widths
            for scale in (100, 200)
            for case in ("reference", "raw")
        }
        actual_cards = {Path(path).name for path in images}
        if len(images) != len(expected_cards) or actual_cards != expected_cards:
            raise ValueError("Difference review capture inventory mismatch")
    elif value["issue_key"] == "OPE-315":
        expected_impact = {
            f"impact-{width}-{scale}.png" for width in widths for scale in (100, 200)
        }
        actual_impact = {Path(name).name for name in images}
        if len(images) != len(expected_impact) or actual_impact != expected_impact:
            raise ValueError("Cancellation impact capture inventory mismatch")
    elif value["issue_key"] == "OPE-316":
        expected_fields = {
            f"field-{width}-{scale}-{state}.png"
            for width in widths
            for scale in (100, 200)
            for state in ("default", "focus", "error", "disabled", "input")
        }
        actual_fields = {Path(name).name for name in images}
        if len(images) != len(expected_fields) or actual_fields != expected_fields:
            raise ValueError("Text field capture inventory mismatch")
    elif value["issue_key"] == "OPE-317":
        if len(images) != len(SINGLE_CHOICE_CAPTURES) or {
            Path(name).name for name in images
        } != SINGLE_CHOICE_CAPTURES:
            raise ValueError("Single-choice select capture inventory mismatch")
    elif value["issue_key"] == "OPE-318":
        desktop_states = (
            *(f"checkbox-{state}" for state in ("unchecked", "checked", "disabled", "error")),
            *(f"switch-{state}" for state in ("off", "on", "disabled", "pending")),
            *(
                f"context-{name}"
                for name in ("terms-loading", "terms-error", "automation", "hover")
            ),
        )
        matrix_states = {
            (1280, 100): desktop_states,
            (320, 200): ("checkbox-family", "switch-family", "contexts"),
        }
        behavior_states = (
            "checkbox-interaction",
            "switch-interaction",
            "controlled-rerender",
            "disabled-error",
            "pending",
            "checkbox-focus",
            "checkbox-hover",
            "switch-focus",
            "switch-hover",
            "context-hover-checkbox",
            "context-hover-switch",
            "forced-colors",
            "forced-colors-focus",
            "reduced-motion",
        )
        expected_controls = {
            f"selection-controls-{mode}-{width}-{scale}-{state}.png"
            for mode in ("customer-controlled", "opentour-managed")
            for width, scale, states in (
                *(
                    (width, scale, ("gallery", *matrix_states.get((width, scale), ())))
                    for width in widths
                    for scale in (100, 200)
                ),
                (1280, 100, behavior_states),
            )
            for state in states
        }
        if (
            len(images) != len(expected_controls)
            or {Path(name).name for name in images} != expected_controls
        ):
            raise ValueError("Selection control presentation capture inventory mismatch")
    elif value["issue_key"] == "OPE-319":
        relationship_states = ("populated", "selected", "empty", "redacted")
        expected_relationships = (
            {
                f"relationship-{state}-{width}-{scale}.png"
                for state in relationship_states
                for width in widths
                for scale in (100, 200)
            }
            | {"relationship-selected-cleared.png"}
            | {
                f"relationship-long-{width}-{scale}.png"
                for width in (320, 1440)
                for scale in (100, 200)
            }
            | {
                f"relationship-forced-colors-{state}-{width}.png"
                for state in relationship_states
                for width in (320, 1280)
            }
        )
        if (
            len(images) != len(expected_relationships)
            or {Path(name).name for name in images} != expected_relationships
        ):
            raise ValueError("Relationship context capture inventory mismatch")
    elif value["issue_key"] == "OPE-320":
        modes = ("customer-controlled", "opentour-managed")
        derived_expected = {
            f"derived-value-{mode}-{width}-{scale}.png"
            for mode in modes for width in widths for scale in (100, 200)
        } | {
            f"derived-value-{mode}-{state}.png"
            for mode in modes
            for state in (
                "native", "keyboard-copied", "clipboard-failed", "race-current",
                "forced-colors", "reduced-motion",
            )
        }
        if len(images) != len(derived_expected) or {
            Path(name).name for name in images
        } != derived_expected:
            raise ValueError("Derived-value capture inventory mismatch")
    elif value["issue_key"] == "OPE-303":
        expected_charts = {
            f"charts-{width}-{scale}.png" for width in widths for scale in (100, 200)
        }
        if (
            len(images) != len(expected_charts)
            or {Path(name).name for name in images} != expected_charts
        ):
            raise ValueError("Chart presentation capture inventory mismatch")
    elif value["issue_key"] == "OPE-305":
        states = {"populated", "partial", "empty", "filtered-to-nothing", "navigation", "query"}
        expected_tables = {
            (width, scale, state) for width in widths for scale in (100, 200) for state in states
        }
        captures = [
            re.search(
                r"(320|768|1280|1440)px-and-(100|200)-text-presentation/([a-z-]+)\.png$", name
            )
            for name in images
        ]
        actual_tables = {(int(case[1]), int(case[2]), case[3]) for case in captures if case}
        if len(images) != len(expected_tables) or actual_tables != expected_tables:
            raise ValueError("Table presentation capture inventory mismatch")
    elif value["issue_key"] == "OPE-307":
        expected_shells = {
            f"{identity}-{width}-{scale}x-{state}.png"
            for identity in (0, 1)
            for width in widths
            for scale in (1, 2)
            for state in (("closed", "open") if width < 1024 else ("closed",))
        }
        if (
            len(images) != len(expected_shells)
            or {Path(name).name for name in images} != expected_shells
        ):
            raise ValueError("Shell presentation capture inventory mismatch")
    elif value["issue_key"] == "OPE-308":
        expected_names = {
            f"{state}-{width}-{scale}x.png"
            for width in widths
            for scale in (1, 2)
            for state in (
                "held",
                "authorized",
                "staging",
                "accepted",
                "verifying",
                "active",
                "unknown",
            )
        }
        if (
            len(images) != len(expected_names)
            or {Path(name).name for name in images} != expected_names
        ):
            raise ValueError("Operation presentation capture inventory mismatch")
    else:
        platform = value.get("platform")
        if platform not in {"darwin", "linux"}:
            raise ValueError("Unknown status-copy baseline platform")
        expected_baselines = {
            f"status-{width}-{scale}.png" for width in widths for scale in (100, 200)
        }
        approved = load_policy(root)["reviewed_binary_sha256"]
        if (
            len(images) != len(expected_baselines)
            or {Path(name).name for name in images} != expected_baselines
        ):
            raise ValueError("Status-copy presentation capture inventory mismatch")
        for name, actual in images.items():
            baseline = (
                f"apps/web/tests/fixtures/presentation/baselines/{platform}/{Path(name).name}"
            )
            if actual != approved.get(baseline) or actual != digest(
                local_file(root, baseline).read_bytes()
            ):
                raise ValueError("Status-copy capture differs from its reviewed platform baseline")
