"""Actual numerical software checks over generated/reference inputs, never model proof."""

from __future__ import annotations

import base64
import copy
import importlib.metadata
import inspect
import json
import math
import subprocess
import sys
from decimal import Context, Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest
from numeric_oracle import (
    binomial_probability,
    cdf,
    family_lower,
    lower,
    multinomial_probability,
    upper,
)
from opentour_training.evaluation.inference_plan import (
    ContractValidator,
    InvalidKernelInput,
    NumericalComputationError,
    UnsupportedNumericProfile,
    build_contract_validator,
    validate_inference_plan,
)
from opentour_training.evaluation.statistics_kernel import (
    binomial_lower_bound,
    binomial_upper_bound,
    compute_interval,
    independent_family_interval,
    paired_binary_interval,
)
from scipy import special  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/precision-numeric-fixtures.json").read_bytes()
)


@pytest.fixture(scope="module")
def contracts() -> ContractValidator:
    return build_contract_validator(
        (
            ROOT / "packages/contracts/learning/benchmarks/statistical-design.schema.json"
        ).read_bytes(),
        (ROOT / "packages/contracts/common/common-contract.schema.json").read_bytes(),
    )


def plan_data(*, n: int, family: bool = False) -> dict[str, Any]:
    ref = {
        "artifact_id": "generated",
        "kind": "synthetic_fixture",
        "revision": "v1",
        "sha256": "a" * 64,
        "manifest_id": "generated",
        "environment_id": "generated",
        "classification": "control_metadata",
        "purpose_grant_id": "generated",
    }
    return {
        "independent_unit": "family" if family else "case",
        "cluster_method_ref": ref if family else None,
        "paired_effect_method": (
            "bounded_independent_family_hoeffding_v1"
            if family
            else "paired_binary_exact_marginals_v1"
        ),
        "confidence_level": 0.95,
        "effect_interval_sides": 2,
        "retention_interval_sides": 1,
        "critical_bound_method": "binomial_clopper_pearson_one_sided_v1",
        "multiplicity_policy_ref": ref,
        "stopping_mode": "fixed_final",
        "look_plan": [
            {"look_id": "final", "cumulative_independent_units": n, "claim_alpha": {"claim": 0.05}}
        ],
        "repetitions": 3 if family else 1,
        "nuisance_assumptions_ref": ref,
    }


def denoms(n: int) -> dict[str, int]:
    return {
        "attempted": n,
        "paired_gradeable": n,
        "unknown": 0,
        "excluded": 0,
        "runtime_failed": 0,
        "independent_units": n,
    }


def test_paired_canonical_fixture() -> None:
    result = paired_binary_interval(40, 10, 130, 20, 0.05, 2)
    expected = FIXTURES["paired_exact_marginal_200"]
    assert result.value == 0.15
    assert result.lower == pytest.approx(expected["lower"], abs=1e-11)
    assert result.upper == pytest.approx(expected["upper"], abs=1e-11)
    assert result.undefined_reason is None
    assert result.lower != pytest.approx(0.0838966006, abs=1e-6)


def test_retention_canonical_fixture() -> None:
    result = paired_binary_interval(10, 12, 355, 23, 0.05, 1)
    assert result.value == -0.005
    assert result.lower == pytest.approx(
        FIXTURES["retention_exact_marginal_400"]["lower"], abs=1e-11
    )
    assert result.lower is not None and result.lower < -0.01
    assert result.upper is None


@pytest.mark.parametrize(
    "b,c,correct,wrong", [(0, 0, 20, 0), (10, 10, 0, 0), (20, 0, 0, 0), (0, 20, 0, 0), (2, 3, 4, 5)]
)
@pytest.mark.parametrize("sides", [1, 2])
def test_paired_endpoints_and_symmetry(
    b: int, c: int, correct: int, wrong: int, sides: Any
) -> None:
    result = paired_binary_interval(b, c, correct, wrong, 0.05, sides)
    opposite = paired_binary_interval(c, b, correct, wrong, 0.05, 2)
    assert result.lower is not None and -1 <= result.lower <= 1
    if sides == 2:
        assert opposite.lower is not None
        assert result.upper == pytest.approx(-opposite.lower, abs=1e-13)
    else:
        assert result.upper is None


def test_paired_one_sided_tail_allocation() -> None:
    result = paired_binary_interval(40, 10, 130, 20, 0.05, 1)
    expected = lower(40, 200, Decimal(".025")) - upper(10, 200, Decimal(".025"))
    assert result.lower is not None and result.upper is None
    lower_float = Decimal.from_float(result.lower)
    assert 0 <= expected - lower_float < Decimal("1e-11")


@pytest.mark.parametrize(
    "k,n,key",
    [(0, 240, "critical_zero_240"), (0, 300, "critical_zero_300"), (1, 300, "critical_one_300")],
)
def test_critical_canonical_fixtures(k: int, n: int, key: str) -> None:
    assert binomial_upper_bound(k, n, 0.05) == pytest.approx(FIXTURES[key], abs=1e-11)


def test_high_alpha_one_sided_bounds() -> None:
    lo, hi = binomial_lower_bound(1, 2, 0.8), binomial_upper_bound(1, 2, 0.8)
    assert lo == pytest.approx(1 - math.sqrt(0.2), abs=1e-14)
    assert hi == pytest.approx(math.sqrt(0.2), abs=1e-14)
    assert lo is not None and hi is not None and lo > hi  # Separate bounds, not a joint CI.


@pytest.mark.parametrize("n", [1, 2, 20, 1_000_000])
def test_binomial_endpoints_and_monotonicity(n: int) -> None:
    assert binomial_lower_bound(0, n, 0.05) == 0
    assert binomial_upper_bound(n, n, 0.05) == 1
    a = binomial_upper_bound(0, n, 0.05)
    b = binomial_upper_bound(1, n, 0.05)
    assert a is not None and b is not None and a <= b
    c = binomial_upper_bound(0, n, 0.1)
    assert c is not None and c <= a


@pytest.mark.parametrize("alpha", [1e-12, 1 - 1e-12])
@pytest.mark.parametrize("k", [0, 1, 500_000, 999_999, 1_000_000])
def test_alpha_and_maximum_count_profile_boundary(alpha: float, k: int) -> None:
    assert binomial_lower_bound(k, 1_000_000, alpha) is not None
    assert binomial_upper_bound(k, 1_000_000, alpha) is not None


@pytest.mark.parametrize("alpha", [math.nextafter(1e-12, 0), math.nextafter(1 - 1e-12, 1)])
def test_positive_alpha_outside_profile(alpha: float) -> None:
    with pytest.raises(UnsupportedNumericProfile):
        binomial_upper_bound(0, 10, alpha)


@pytest.mark.parametrize(
    "k,n,alpha",
    [
        (-1, 10, 0.05),
        (11, 10, 0.05),
        (True, 10, 0.05),
        (0, 10.0, 0.05),
        (0, 10, 0),
        (0, 10, 1),
        (0, 10, math.nan),
        (0, 10, math.inf),
        (0, 10, True),
    ],
)
def test_invalid_binomial_input(k: Any, n: Any, alpha: Any) -> None:
    with pytest.raises(InvalidKernelInput):
        binomial_upper_bound(k, n, alpha)


def test_zero_exposure_is_unknown_and_unrelated_rows_not_added() -> None:
    assert binomial_upper_bound(0, 0, 0.05) is None
    assert binomial_lower_bound(0, 0, 0.05) is None
    before = binomial_upper_bound(0, 240, 0.05)
    unrelated_rows = denoms(10_000)
    unrelated_rows["runtime_failed"] = 50
    assert binomial_upper_bound(0, 240, 0.05) == before
    with pytest.raises(UnsupportedNumericProfile):
        binomial_upper_bound(0, 1_000_001, 0.05)


@pytest.mark.parametrize("n", [200, 1000])
def test_family_canonical_fixtures(n: int) -> None:
    result = independent_family_interval([(str(i), 0.15) for i in range(n)], 0.05, 1)
    assert result.value == pytest.approx(0.15)
    assert result.lower == pytest.approx(
        FIXTURES[f"family_hoeffding_{n}"]["one_sided95_lower"], abs=1e-11
    )
    assert result.upper is None


@pytest.mark.parametrize("sides", [1, 2])
def test_family_decimal_formula_oracle(sides: Any) -> None:
    result = independent_family_interval([(str(i), 0.15) for i in range(200)], 0.05, sides)
    expected = family_lower(Decimal(".15"), 200, Decimal(".05"), sides)
    assert result.lower is not None
    assert Decimal.from_float(result.lower) <= expected
    assert abs(Decimal.from_float(result.lower) - expected) < Decimal("1e-13")
    if sides == 2:
        assert result.upper is not None
        expected_upper = -family_lower(Decimal("-.15"), 200, Decimal(".05"), sides)
        assert 0 <= Decimal.from_float(result.upper) - expected_upper < Decimal("1e-13")


@pytest.mark.parametrize(
    "effect,sides,check_lower", [(0.4723807270774388, 1, True), (-0.9572307620809911, 2, False)]
)
def test_family_operand_rounding_preserves_direction(
    effect: float, sides: Any, check_lower: bool
) -> None:
    result = independent_family_interval([("one", effect), ("two", effect)], 0.8, sides)
    exact = Decimal.from_float(effect)
    expected = (
        family_lower(exact, 2, Decimal(".8"), sides)
        if check_lower
        else -family_lower(-exact, 2, Decimal(".8"), sides)
    )
    bound = result.lower if check_lower else result.upper
    assert bound is not None
    error = (
        expected - Decimal.from_float(bound)
        if check_lower
        else Decimal.from_float(bound) - expected
    )
    assert 0 <= error < Decimal("1e-14")


def test_equal_family_weight() -> None:
    # Each supplied value already summarizes its own family; there are no raw-row weights.
    result = independent_family_interval([("small", -1), ("large", 1)], 0.05, 1)
    assert result.value == 0
    assert result.lower == -1


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        [("same", 0.1), ("same", 0.2)],
        [("", 0.1)],
        [("x" * 257, 0.1)],
        [("x", math.nan)],
        [("x", math.inf)],
        [("x", 1.001)],
        [("x", True)],
        [("x", 0.1, 2)],
        [(0.1, 0.2)],
    ],
)
def test_invalid_family_aggregates(data: Any) -> None:
    with pytest.raises(InvalidKernelInput):
        independent_family_interval(data, 0.05, 1)


def test_family_size_profile_boundary() -> None:
    data = [(str(i), 0.0) for i in range(1_000_000)]
    result = independent_family_interval(data, 0.05, 1)
    assert result.value == 0
    data.append(("overflow", 0.0))
    with pytest.raises(UnsupportedNumericProfile):
        independent_family_interval(data, 0.05, 1)


@pytest.mark.parametrize("value", [math.nan, math.inf, -1, 2, 0])
def test_backend_failure_is_numeric_failure(monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    monkeypatch.setattr(special, "betaincinv", lambda *_: value)
    with pytest.raises(NumericalComputationError):
        binomial_lower_bound(4, 20, 0.05)


def test_backend_exception_is_private(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "generated-private-marker"

    def fail(*_: Any) -> float:
        raise RuntimeError(marker)

    monkeypatch.setattr(special, "betainccinv", fail)
    with pytest.raises(NumericalComputationError) as observed:
        binomial_upper_bound(4, 20, 0.05)
    assert marker not in str(observed.value)
    assert marker not in caplog.text
    assert observed.value.__suppress_context__


def test_internal_tail_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    original = special.betaincinv
    tails: list[float] = []

    def observe(a: float, b: float, tail: float) -> Any:
        tails.append(tail)
        return original(a, b, tail)

    monkeypatch.setattr(special, "betaincinv", observe)
    result = paired_binary_interval(40, 10, 130, 20, 1e-12, 2)
    assert result.lower is not None
    assert tails and set(tails) == {2.5e-13}


@pytest.mark.parametrize(
    "n,k,alpha",
    [(2, 1, ".8"), (20, 4, ".05"), (200, 40, ".0125"), (400, 12, ".025"), (50, 1, "1e-12")],
)
def test_independent_decimal_oracle(n: int, k: int, alpha: str) -> None:
    lo = binomial_lower_bound(k, n, float(alpha))
    hi = binomial_upper_bound(k, n, float(alpha))
    assert lo is not None and hi is not None
    expected_lo, expected_hi = lower(k, n, Decimal(alpha)), upper(k, n, Decimal(alpha))
    assert abs(Decimal.from_float(lo) - expected_lo) < Decimal("1e-11")
    assert abs(Decimal.from_float(hi) - expected_hi) < Decimal("1e-11")
    assert Decimal.from_float(lo) <= expected_lo
    assert Decimal.from_float(hi) >= expected_hi


@pytest.mark.parametrize("n,k,alpha", [(10, 3, ".4"), (40, 20, "1e-12"), (32, 31, ".999999999999")])
def test_binomial_tail_residuals(n: int, k: int, alpha: str) -> None:
    lo = binomial_lower_bound(k, n, float(alpha))
    hi = binomial_upper_bound(k, n, float(alpha))
    assert lo is not None and hi is not None
    with localcontext(Context(prec=80)):
        target = Decimal(alpha)
        for probability in (
            cdf(n - k, n, 1 - Decimal.from_float(lo)),
            cdf(k, n, Decimal.from_float(hi)),
        ):
            assert 0 <= target - probability < min(target, 1 - target) * Decimal("1e-7")


@pytest.mark.parametrize("alpha", ["1e-12", ".05", ".999999999999"])
def test_large_n_analytic_oracle(alpha: str) -> None:
    n = 1_000_000
    with localcontext() as ctx:
        ctx.prec = 80
        expected_lo = (Decimal(alpha).ln() / n).exp()
        expected_hi = 1 - expected_lo
    lo, hi = binomial_lower_bound(n, n, float(alpha)), binomial_upper_bound(0, n, float(alpha))
    assert lo is not None and hi is not None
    assert lo < 1 and hi > 0
    assert Decimal.from_float(lo) <= expected_lo
    assert Decimal.from_float(hi) >= expected_hi


@pytest.mark.parametrize("n", [2, 19, 1_000_000])
@pytest.mark.parametrize("alpha", ["1e-12", ".999999999999"])
def test_extreme_interior_tail_has_conservative_representable_root(n: int, alpha: str) -> None:
    with localcontext(Context(prec=80)):
        expected_hi = ((1 - Decimal(alpha)).ln() / n).exp()
        expected_lo = 1 - expected_hi
    lo = binomial_lower_bound(1, n, float(alpha))
    hi = binomial_upper_bound(n - 1, n, float(alpha))
    assert lo is not None and hi is not None
    assert 0 <= expected_lo - Decimal.from_float(lo) < Decimal("1e-13")
    assert 0 <= Decimal.from_float(hi) - expected_hi < Decimal("1e-13")


@pytest.mark.parametrize("n", [2, 5, 12])
@pytest.mark.parametrize("p", ["0", ".01", ".1", ".5", ".9", ".99", "1"])
def test_exact_binomial_coverage_grid(n: int, p: str) -> None:
    truth, alpha = Decimal(p), Decimal(".05")
    covered = Decimal(0)
    for k in range(n + 1):
        hi = binomial_upper_bound(k, n, float(alpha))
        assert hi is not None
        if Decimal.from_float(hi) >= truth:
            covered += binomial_probability(k, n, truth)
    assert covered >= 1 - alpha


@pytest.mark.parametrize("n", [2, 3, 5])
@pytest.mark.parametrize(
    "probabilities", [("0", "0", ".5", ".5"), (".1", ".05", ".4", ".45"), (".5", ".4", ".1", "0")]
)
def test_exact_paired_coverage_grid(n: int, probabilities: tuple[str, ...]) -> None:
    probs = tuple(Decimal(p) for p in probabilities)
    truth, covered = probs[0] - probs[1], Decimal(0)
    for b in range(n + 1):
        for c in range(n - b + 1):
            for correct in range(n - b - c + 1):
                wrong = n - b - c - correct
                result = paired_binary_interval(b, c, correct, wrong, 0.05, 2)
                assert result.lower is not None and result.upper is not None
                if Decimal.from_float(result.lower) <= truth <= Decimal.from_float(result.upper):
                    covered += multinomial_probability((b, c, correct, wrong), probs)
    assert covered >= Decimal(".95")


def test_dispatcher_replay_and_no_mutation(contracts: ContractValidator) -> None:
    raw = plan_data(n=200)
    before = copy.deepcopy(raw)
    parsed = validate_inference_plan(raw, contracts)
    cells = {"b": 40, "c": 10, "both_correct": 130, "both_wrong": 20}
    first = compute_interval(
        parsed, denoms(200), "final", "claim", metric_role="effect", pair_cells=cells
    )
    second = compute_interval(
        parsed, denoms(200), "final", "claim", metric_role="effect", pair_cells=cells
    )
    assert first == second
    assert raw == before
    assert cells == {"b": 40, "c": 10, "both_correct": 130, "both_wrong": 20}


def test_family_dispatch_and_empty(contracts: ContractValidator) -> None:
    parsed = validate_inference_plan(plan_data(n=200, family=True), contracts)
    data = tuple((str(i), 0.15) for i in range(200))
    result = compute_interval(
        parsed, denoms(200), "final", "claim", metric_role="retention", family_effects=data
    )
    assert result.upper is None
    empty = compute_interval(
        parsed, denoms(0), "final", "claim", metric_role="effect", family_effects=[]
    )
    assert empty.undefined_reason == "no_gradeable_units"
    with pytest.raises(InvalidKernelInput):
        compute_interval(
            parsed,
            denoms(200),
            "final",
            "claim",
            metric_role="effect",
            pair_cells={},
            family_effects=data,
        )


@pytest.mark.parametrize("fault", ["missing", "malformed", "incomplete", "extra", "wrong-count"])
def test_family_dispatch_requires_one_effect_per_independent_unit(
    contracts: ContractValidator, fault: str
) -> None:
    parsed = validate_inference_plan(plan_data(n=3, family=True), contracts)
    data: Any = [("one", 0.2), ("two", 0.1)]
    den = denoms(20)
    den["independent_units"] = 2
    if fault == "missing":
        data = None
    elif fault == "malformed":
        data = [("one", 0.2, 5), ("two", 0.1)]
    elif fault == "extra":
        data += [("three", 0.15), ("four", 0.15)]
        den["independent_units"] = 4
    elif fault == "wrong-count":
        den["independent_units"] = 3
    if fault == "incomplete":
        result = compute_interval(
            parsed, den, "final", "claim", metric_role="effect", family_effects=data
        )
        assert result.undefined_reason == "incomplete_planned_look"
    else:
        with pytest.raises(InvalidKernelInput):
            compute_interval(
                parsed, den, "final", "claim", metric_role="effect", family_effects=data
            )


def test_retention_role_uses_exact_selected_claim(contracts: ContractValidator) -> None:
    raw = plan_data(n=400)
    raw["look_plan"][0]["claim_alpha"] = {"effect-named-claim": 0.025, "other-claim": 0.02}
    parsed = validate_inference_plan(raw, contracts)
    cells = {"b": 10, "c": 12, "both_correct": 355, "both_wrong": 23}
    actual = compute_interval(
        parsed,
        denoms(400),
        "final",
        "effect-named-claim",
        metric_role="retention",
        pair_cells=cells,
    )
    assert actual == paired_binary_interval(10, 12, 355, 23, 0.025, 1)
    assert actual.upper is None


def test_critical_helper_argument_boundary() -> None:
    assert set(inspect.signature(binomial_upper_bound).parameters) == {"k", "n", "alpha"}
    with pytest.raises(TypeError):
        binomial_upper_bound(0, 10, 0.05, metric_role="effect")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        binomial_lower_bound(0, 10)  # type: ignore[call-arg]


def test_installed_cpu_package(tmp_path: Path) -> None:
    assert importlib.metadata.version("opentour-training") == "0.0.0"
    for name, version in [("scipy", "1.18.1"), ("numpy", "2.5.5"), ("jsonschema", "4.26.0")]:
        assert importlib.metadata.version(name) == version
    payload = {
        "schemas": [
            base64.b64encode((ROOT / f"packages/contracts/{path}").read_bytes()).decode("ascii")
            for path in (
                "learning/benchmarks/statistical-design.schema.json",
                "common/common-contract.schema.json",
            )
        ],
        "checkout": str(ROOT / "training/src"),
        "plan": plan_data(n=200),
        "denominators": denoms(200),
        "cells": {"b": 40, "c": 10, "both_correct": 130, "both_wrong": 20},
    }
    script = """
import base64
import json
import os
import sys
import sysconfig
from pathlib import Path

value = json.load(sys.stdin)
# Initialize the interpreter's own platform metadata before guarding package imports.
sysconfig.get_config_vars()
allowed = tuple(Path(p).resolve() for p in (sys.prefix, sys.base_prefix, value["checkout"]))
write_flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_RDWR | os.O_APPEND

def require_pure_execution(event, args):
    if event.startswith("socket.") or event in ("subprocess.Popen", "os.mkdir", "os.system"):
        raise AssertionError("Unexpected external effect")
    if event == "open":
        path, mode, flags = args
        if flags & write_flags:
            raise AssertionError("Unexpected file write")
        if isinstance(path, (str, bytes)):
            accessed = Path(os.fsdecode(path)).resolve()
            if not any(accessed.is_relative_to(root) for root in allowed):
                raise AssertionError("Unexpected file read")

sys.addaudithook(require_pure_execution)
from opentour_training.evaluation.inference_plan import (
    build_contract_validator,
    validate_inference_plan,
)
from opentour_training.evaluation.statistics_kernel import binomial_upper_bound, compute_interval

schemas = [base64.b64decode(item, validate=True) for item in value["schemas"]]
validator = build_contract_validator(*schemas)
plan = validate_inference_plan(value["plan"], validator)
first = compute_interval(plan, value["denominators"], "final", "claim",
                         metric_role="effect", pair_cells=value["cells"])
second = compute_interval(plan, value["denominators"], "final", "claim",
                          metric_role="effect", pair_cells=value["cells"])
assert first == second and first.lower is not None
assert 0 < binomial_upper_bound(0, 300, .05) < .01
forbidden = {"boto3", "botocore", "workos", "torch", "psycopg", "opentour_control"}
assert not forbidden.intersection(sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-IB", "-c", script],
        input=json.dumps(payload).encode(),
        cwd=tmp_path,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stdout == completed.stderr == b""
