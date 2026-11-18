"""Pure validation of the admitted inference contract; no authority or I/O."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from typing import Any, Literal, cast

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

INFERENCE_SHA256 = "76fa494afea46f7bb9f505838d1269320b4aacaaa010b5545731e223affd2cda"
COMMON_SHA256 = "2d78b380cf3c924683d11549a1c455e2287ac5d1b30eb4007b24eb2aa48775fe"
INFERENCE_URI = "urn:opentour:contracts:learning:statistical-inference:v1"
COMMON_URI = "urn:opentour:common-contract:1.0"
MAX_JSON_INTEGER = 9_007_199_254_740_991
MAX_UNITS = 1_000_000
MAX_LOOKS = 64
MAX_CLAIMS_PER_LOOK = 128
MAX_CLAIM_CELLS = 8192
MIN_ALPHA = Fraction(1, 10**12)
MAX_ALPHA = 1 - MIN_ALPHA
MetricRole = Literal["effect", "retention"]
Method = Literal["paired_binary_exact_marginals_v1", "bounded_independent_family_hoeffding_v1"]
_SEAL = object()


class InvalidKernelInput(ValueError):
    """Malformed or semantically contradictory input; never include input text."""

    def __init__(self) -> None:
        super().__init__("Invalid numerical input")


class UnsupportedNumericProfile(ValueError):
    """An input or request exceeds the explicitly qualified implementation profile."""

    def __init__(self) -> None:
        super().__init__("Unsupported numerical profile")


class NumericalComputationError(ArithmeticError):
    """A numerical operation failed; this is not missing empirical evidence."""

    def __init__(self) -> None:
        super().__init__("Numerical computation unavailable")


def strict_count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_JSON_INTEGER:
        raise InvalidKernelInput()
    return value


def strict_number(value: object) -> float:
    if type(value) not in (int, float):
        raise InvalidKernelInput()
    if type(value) is int and abs(value) > MAX_JSON_INTEGER:
        raise InvalidKernelInput()
    result = float(cast(float | int, value))
    if not math.isfinite(result):
        raise InvalidKernelInput()
    return result


def strict_id(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 256:
        raise InvalidKernelInput()
    return value


def numeric_alpha(value: object) -> Fraction:
    result = strict_number(value)
    if not 0 < result < 1:
        raise InvalidKernelInput()
    exact = Fraction(str(result))
    if not MIN_ALPHA <= exact <= MAX_ALPHA:
        raise UnsupportedNumericProfile()
    return exact


def _json_copy(value: object, depth: int = 0) -> Any:
    # The admitted closed contract has shallow structure. This is not a new schema.
    if depth > 32:
        raise InvalidKernelInput()
    if value is None or type(value) in (bool, str):
        return value
    if type(value) in (int, float):
        strict_number(value)
        return value
    if type(value) is list:
        return [_json_copy(item, depth + 1) for item in cast(list[object], value)]
    if type(value) is dict:
        source = cast(dict[object, object], value)
        if any(type(key) is not str for key in source):
            raise InvalidKernelInput()
        return {key: _json_copy(item, depth + 1) for key, item in source.items()}
    raise InvalidKernelInput()


def _deny_retrieval(uri: str) -> Resource[Any]:
    raise NoSuchResource(uri)


def _format_checker() -> Any:
    checker = FormatChecker()

    @checker.checks("date-time", raises=(ValueError, TypeError))  # type: ignore[untyped-decorator]
    def timestamp(value: Any) -> bool:
        if not isinstance(value, str):
            return True  # The canonical type keyword handles this case.
        if (
            re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z",
                value,
            )
            is None
        ):
            return False
        datetime.fromisoformat(value)
        return True

    return checker


@dataclass(frozen=True)
class ContractValidator:
    _validator: Any = field(repr=False)
    _seal: object = field(repr=False)

    def validate_gate_policy(self, value: object) -> dict[str, Any]:
        """Return an independent copy validated by the registered GatePolicy definition."""
        if self._seal is not _SEAL:
            raise InvalidKernelInput()
        policy = _json_copy(value)
        validator = self._validator.evolve(schema={"$ref": INFERENCE_URI + "#/$defs/GatePolicy"})
        if not isinstance(policy, dict) or not validator.is_valid(policy):
            raise InvalidKernelInput()
        return policy


def build_contract_validator(
    inference_schema_bytes: bytes, common_schema_bytes: bytes
) -> ContractValidator:
    """Only these two exact admitted byte strings can construct the validator."""
    pairs = (
        (inference_schema_bytes, INFERENCE_SHA256, INFERENCE_URI),
        (common_schema_bytes, COMMON_SHA256, COMMON_URI),
    )
    documents: list[dict[str, Any]] = []
    for data, expected_hash, expected_uri in pairs:
        if type(data) is not bytes or hashlib.sha256(data).hexdigest() != expected_hash:
            raise InvalidKernelInput()
        try:
            document = json.loads(data)
            if document.get("$id") != expected_uri:
                raise InvalidKernelInput()
            Draft202012Validator.check_schema(document)
        except Exception:
            raise InvalidKernelInput() from None
        documents.append(document)
    try:
        registry: Registry[Any] = Registry(retrieve=_deny_retrieval)  # type: ignore[call-arg]
        for document in documents:
            registry = registry.with_resource(document["$id"], Resource.from_contents(document))
        validator = Draft202012Validator(
            documents[0], registry=registry, format_checker=_format_checker()
        )
    except Exception:
        raise InvalidKernelInput() from None
    return ContractValidator(validator, _SEAL)


@dataclass(frozen=True)
class AnalysisLook:
    look_id: str
    cumulative_independent_units: int
    claim_alpha: tuple[tuple[str, Fraction], ...]


@dataclass(frozen=True, repr=False)
class InferencePlan:
    """Immutable internal representation of existing native fields, not a wire type."""

    independent_unit: Literal["case", "family"]
    cluster_method_ref: bytes | None
    paired_effect_method: Method
    confidence_level: Fraction
    effect_interval_sides: Literal[1, 2]
    retention_interval_sides: Literal[1, 2]
    critical_bound_method: str
    multiplicity_policy_ref: bytes
    stopping_mode: Literal["fixed_final", "bonferroni_finite_looks"]
    look_plan: tuple[AnalysisLook, ...]
    repetitions: int
    nuisance_assumptions_ref: bytes
    _seal: object = field(repr=False)
    _source: bytes = field(repr=False)
    _contracts: ContractValidator = field(repr=False)


def _reference(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def validate_inference_plan(plan: object, contract_validator: ContractValidator) -> InferencePlan:
    if type(contract_validator) is not ContractValidator or contract_validator._seal is not _SEAL:
        raise InvalidKernelInput()
    try:
        value = _json_copy(plan)
        if not contract_validator._validator.is_valid(value):
            raise InvalidKernelInput()
    except InvalidKernelInput:
        raise
    except Exception:
        raise InvalidKernelInput() from None
    repetitions = strict_count(value["repetitions"])
    if repetitions == 0:
        raise InvalidKernelInput()
    for key in ("effect_interval_sides", "retention_interval_sides"):
        if type(value[key]) is not int or value[key] not in (1, 2):
            raise InvalidKernelInput()
    looks: list[AnalysisLook] = []
    seen: set[str] = set()
    previous = 0
    allocated = Fraction(0)
    for item in value["look_plan"]:
        identifier = strict_id(item["look_id"])
        count = strict_count(item["cumulative_independent_units"])
        if identifier in seen or count <= previous:
            raise InvalidKernelInput()
        seen.add(identifier)
        previous = count
        claims = []
        for gate, alpha in item["claim_alpha"].items():
            strict_id(gate)
            number = strict_number(alpha)
            if not 0 < number < 1:
                raise InvalidKernelInput()
            exact = Fraction(str(alpha))
            allocated += exact
            claims.append((gate, exact))
        looks.append(AnalysisLook(identifier, count, tuple(sorted(claims))))
    confidence = Fraction(str(value["confidence_level"]))
    if allocated > 1 - confidence:
        raise InvalidKernelInput()
    return InferencePlan(
        independent_unit=value["independent_unit"],
        cluster_method_ref=(
            None if value["cluster_method_ref"] is None else _reference(value["cluster_method_ref"])
        ),
        paired_effect_method=value["paired_effect_method"],
        confidence_level=confidence,
        effect_interval_sides=value["effect_interval_sides"],
        retention_interval_sides=value["retention_interval_sides"],
        critical_bound_method=value["critical_bound_method"],
        multiplicity_policy_ref=_reference(value["multiplicity_policy_ref"]),
        stopping_mode=value["stopping_mode"],
        look_plan=tuple(looks),
        repetitions=repetitions,
        nuisance_assumptions_ref=_reference(value["nuisance_assumptions_ref"]),
        _seal=_SEAL,
        _source=_reference(value),
        _contracts=contract_validator,
    )


@dataclass(frozen=True)
class SelectedComputation:
    method: Method
    metric_role: MetricRole
    sides: Literal[1, 2]
    alpha: Fraction
    independent_units: int


def _select_claim(
    plan: InferencePlan,
    selected_look_id: str,
    selected_gate_id: str,
    *,
    metric_role: MetricRole,
) -> SelectedComputation:
    if type(plan) is not InferencePlan or plan._seal is not _SEAL:
        raise InvalidKernelInput()
    try:
        if plan != validate_inference_plan(json.loads(plan._source), plan._contracts):
            raise InvalidKernelInput()
    except Exception:
        raise InvalidKernelInput() from None
    if type(metric_role) is not str or metric_role not in ("effect", "retention"):
        raise InvalidKernelInput()
    strict_id(selected_look_id)
    strict_id(selected_gate_id)
    look = next((item for item in plan.look_plan if item.look_id == selected_look_id), None)
    if look is None:
        raise InvalidKernelInput()
    exact_alpha = dict(look.claim_alpha).get(selected_gate_id)
    if exact_alpha is None:
        raise InvalidKernelInput()
    return SelectedComputation(
        plan.paired_effect_method,
        metric_role,
        plan.effect_interval_sides if metric_role == "effect" else plan.retention_interval_sides,
        exact_alpha,
        look.cumulative_independent_units,
    )


def _check_profile(plan: InferencePlan, selected: SelectedComputation) -> None:
    if (
        len(plan.look_plan) > MAX_LOOKS
        or any(len(item.claim_alpha) > MAX_CLAIMS_PER_LOOK for item in plan.look_plan)
        or sum(len(item.claim_alpha) for item in plan.look_plan) > MAX_CLAIM_CELLS
        or selected.independent_units > MAX_UNITS
        or not MIN_ALPHA <= selected.alpha <= MAX_ALPHA
    ):
        raise UnsupportedNumericProfile()


def qualify_numeric_profile(
    plan: InferencePlan,
    selected_look_id: str,
    selected_gate_id: str,
    *,
    metric_role: MetricRole,
) -> SelectedComputation:
    """Return the frozen claim and side policy after static and resource checks."""
    selected = _select_claim(plan, selected_look_id, selected_gate_id, metric_role=metric_role)
    _check_profile(plan, selected)
    return selected


DENOMINATOR_FIELDS = frozenset(
    {"attempted", "paired_gradeable", "unknown", "excluded", "runtime_failed", "independent_units"}
)
PAIR_FIELDS = ("b", "c", "both_correct", "both_wrong")


def validate_denominators(denominators: object, pair_cells: object = None) -> dict[str, int]:
    if type(denominators) is not dict or set(denominators) != DENOMINATOR_FIELDS:
        raise InvalidKernelInput()
    values = {key: strict_count(value) for key, value in denominators.items()}
    if (
        values["attempted"] != values["paired_gradeable"] + values["unknown"] + values["excluded"]
        or values["runtime_failed"] > values["attempted"]
    ):
        raise InvalidKernelInput()
    if pair_cells is not None:
        cells = validate_pair_cells(pair_cells)
        if sum(cells) != values["paired_gradeable"]:
            raise InvalidKernelInput()
    return values


def validate_pair_cells(pair_cells: object) -> tuple[int, int, int, int]:
    if type(pair_cells) is not dict or set(pair_cells) != set(PAIR_FIELDS):
        raise InvalidKernelInput()
    cells = tuple(strict_count(pair_cells[key]) for key in PAIR_FIELDS)
    if sum(cells) > MAX_JSON_INTEGER:
        raise InvalidKernelInput()
    return cast(tuple[int, int, int, int], cells)
