"""Package-image evidence must identify the exact target, runtime and test boundary."""

import copy
import json

import pytest

from tools import container_smoke


@pytest.fixture
def manifest():
    return json.loads((container_smoke.ROOT / "tools/toolchain.json").read_text())


def result_for(target, manifest):
    kind, package, module = container_smoke.TARGETS[target]
    result = {"status": "PASS", "kind": "local_integration", "product_acceptance": "NOT_RUN"}
    if kind == "web":
        result.update(check="web-package-import", node=manifest["tools"]["node"]["version"])
    else:
        result["python"] = manifest["tools"]["python"]["version"]
        if kind == "learning":
            result.update(
                check="learning-container-package-import",
                role=package,
                gpu_qualification="NOT_RUN",
            )
        else:
            result.update(check="container-package-import", module=module)
    return result


@pytest.mark.parametrize("target", container_smoke.TARGETS)
def test_correct_package_result_is_accepted(target, manifest):
    container_smoke.validate_result(target, result_for(target, manifest), manifest)


@pytest.mark.parametrize("target", container_smoke.TARGETS)
def test_each_required_evidence_field_is_enforced(target, manifest):
    original = result_for(target, manifest)
    for key in original:
        result = copy.deepcopy(original)
        result[key] = "wrong"
        with pytest.raises(container_smoke.SmokeError):
            container_smoke.validate_result(target, result, manifest)


def test_container_result_cannot_claim_product_or_gpu_acceptance(manifest):
    for field in ("product_acceptance", "gpu_qualification"):
        result = result_for("trainer", manifest)
        result[field] = "PASS"
        with pytest.raises(container_smoke.SmokeError):
            container_smoke.validate_result("trainer", result, manifest)


@pytest.mark.parametrize("result", [[], None, "PASS", True])
def test_non_object_smoke_output_fails(result, manifest):
    with pytest.raises(container_smoke.SmokeError):
        container_smoke.validate_result("web", result, manifest)


def test_image_builds_use_exact_image_and_pnpm_digests(manifest):
    args = container_smoke.build_arguments("web", "linux/amd64", manifest)
    assert args[:4] == ["docker", "build", "--platform", "linux/amd64"]
    assert f"NODE_IMAGE={manifest['bootstrap_images']['node']}" in args
    assert f"PNPM_SHA256={manifest['tools']['pnpm']['artifacts']['Linux-x86_64']['sha256']}" in args
    manifest["bootstrap_images"]["node"] = "node:latest"
    with pytest.raises(ValueError, match="pinned"):
        container_smoke.build_arguments("web", "linux/amd64", manifest)
