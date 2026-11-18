"""Offline OPE-131 ISO-01 case planning and generated observation checks."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from itertools import permutations
from pathlib import Path
from typing import Literal

DeploymentMode = Literal["customer_controlled", "opentour_managed"]
ActorKind = Literal["human", "workload"]
FixtureStatus = Literal["PASS", "FAIL", "INCOMPLETE"]
Metric = bool | int
_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = _ROOT / "packages/contracts/fixtures/acceptance/ot-g2-c01.json"

_CELL_MODES: dict[str, DeploymentMode] = {
    "managed_a": "opentour_managed",
    "managed_b": "opentour_managed",
    "byoc_a": "customer_controlled",
    "byoc_b": "customer_controlled",
}
_SCOPE_FIELDS = ("workspace_id", "project_id", "environment_id")
_CROSS_SCOPE_FIELDS = ("organization_id", "cell_id", "resource_id")
_ACTOR_KINDS: tuple[ActorKind, ...] = ("human", "workload")
_ACTUAL_BINDING_FIELDS = (
    "cell_id",
    "account_id",
    "region",
    "installed_release_digest",
    "runtime_identity_ref",
    "authority_snapshot_ref",
    "approved_spend_ref",
)
_OPERATIONS = ("read", "list", "search", "write", "delete", "export", "subscription")
_CROSS_DENIAL = (
    ("foreign_bytes", 0),
    ("foreign_mutations", 0),
    ("useful_existence_details", 0),
)
_EXPECTED: dict[str, tuple[tuple[str, Metric], ...]] = {
    "cross_cell_every_pair": _CROSS_DENIAL,
    "same_org_wrong_scope": (("scope_bypass", False),),
    "own_scope_control": (("scope_authorized", True),),
}


class InvalidIsolationFixture(ValueError):
    """The generated case matrix is incomplete or contradictory."""


@dataclass(frozen=True)
class IsolationCase:
    case_id: str
    mode: DeploymentMode
    initiating_cell: str
    target_cell: str
    actor_kind: ActorKind
    stage: Literal["admission", "worker_handoff", "own_scope_control"]
    operation: str
    scope_fields: tuple[str, ...]
    expected: tuple[tuple[str, Metric], ...]


@dataclass(frozen=True)
class IsolationFixture:
    pairs: tuple[tuple[str, str], ...]
    intra_scopes: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if Counter(self.pairs) != Counter(permutations(_CELL_MODES, 2)):
            raise InvalidIsolationFixture(
                "Expected all twelve directed cross-cell pairs exactly once"
            )
        if Counter(self.intra_scopes) != Counter(
            (alias, field) for alias in _CELL_MODES for field in _SCOPE_FIELDS
        ):
            raise InvalidIsolationFixture("Expected all declared same-organization scope cases")

    def cases(self, mode: DeploymentMode) -> tuple[IsolationCase, ...]:
        """Expand generated placeholders. Route and principal bindings are external inputs."""
        if mode not in _CELL_MODES.values():
            raise InvalidIsolationFixture("Unrecognized deployment mode")
        cases: list[IsolationCase] = []
        for source, target in self.pairs:
            if _CELL_MODES[source] != mode:
                continue
            for actor in _ACTOR_KINDS:
                for operation in _OPERATIONS:
                    cases.append(
                        IsolationCase(
                            f"cross_cell_every_pair/{source}/{target}/{actor}/{operation}",
                            mode,
                            source,
                            target,
                            actor,
                            "admission",
                            operation,
                            _CROSS_SCOPE_FIELDS,
                            _CROSS_DENIAL,
                        )
                    )
            cases.append(
                IsolationCase(
                    f"cross_cell_every_pair/{source}/{target}/workload/worker_handoff",
                    mode,
                    source,
                    target,
                    "workload",
                    "worker_handoff",
                    "worker_fetch",
                    _CROSS_SCOPE_FIELDS,
                    _CROSS_DENIAL,
                )
            )
        for alias, field in self.intra_scopes:
            if _CELL_MODES[alias] != mode:
                continue
            for actor in _ACTOR_KINDS:
                cases.append(
                    IsolationCase(
                        f"same_org_wrong_scope/{alias}/{actor}/{field}",
                        mode,
                        alias,
                        alias,
                        actor,
                        "admission",
                        field,
                        (field,),
                        _EXPECTED["same_org_wrong_scope"],
                    )
                )
        for alias, cell_mode in _CELL_MODES.items():
            if cell_mode != mode:
                continue
            for actor in _ACTOR_KINDS:
                cases.append(
                    IsolationCase(
                        f"own_scope_control/{alias}/{actor}",
                        mode,
                        alias,
                        alias,
                        actor,
                        "own_scope_control",
                        "authorized_action",
                        (),
                        _EXPECTED["own_scope_control"],
                    )
                )
        return tuple(cases)


@dataclass(frozen=True)
class GeneratedObservation:
    case_id: str
    metrics: Mapping[str, object]


@dataclass(frozen=True)
class FixtureAssessment:
    fixture_status: FixtureStatus
    expected_count: int
    classified_count: int
    failed_cases: tuple[str, ...]
    incomplete_cases: tuple[str, ...]
    unexpected_cases: tuple[str, ...]
    product_acceptance: Literal["NOT_RUN"] = dataclass_field(default="NOT_RUN", init=False)


def _record(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise InvalidIsolationFixture("An isolation fixture record must be a JSON object")
    return value


def _records(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise InvalidIsolationFixture("An isolation fixture collection must be a JSON array")
    return tuple(_record(item) for item in value)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or not value.isascii():
        raise InvalidIsolationFixture("Expected a bounded generated identifier")
    if not all(char.isalnum() or char in "_-" for char in value):
        raise InvalidIsolationFixture("Unexpected generated identifier characters")
    return value


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    record = dict(pairs)
    if len(record) != len(pairs):
        raise InvalidIsolationFixture("Duplicate JSON field in isolation fixture")
    return record


def _validate_catalog(document: Mapping[str, object]) -> None:
    suites = _records(document.get("suites"))
    identifiers = [_identifier(suite.get("suite_id")) for suite in suites]
    if Counter(identifiers) != Counter(("ISO-01", "ISO-02", "ISO-03", "ISO-04")):
        raise InvalidIsolationFixture("Expected one declaration of each OPE-131 ISO suite")
    iso = next(suite for suite in suites if suite["suite_id"] == "ISO-01")
    rows = _records(iso.get("cases"))
    if Counter(_identifier(row.get("id")) for row in rows) != Counter(_EXPECTED.keys()):
        raise InvalidIsolationFixture("ISO-01 must include exact denial and positive controls")
    for row in rows:
        key = _identifier(row.get("id"))
        if not _matches_shape(row.get("expect"), _EXPECTED[key]):
            raise InvalidIsolationFixture(f"Incorrect oracle shape for {key}")
        if row["expect"] != dict(_EXPECTED[key]):
            raise InvalidIsolationFixture(f"Incorrect oracle values for {key}")
        if key == "cross_cell_every_pair" and (
            row.get("substituted_scope_fields") != list(_CROSS_SCOPE_FIELDS)
            or row.get("operations") != list(_OPERATIONS)
            or row.get("worker_handoff") != "separate_observation"
        ):
            raise InvalidIsolationFixture("Cross-cell admission and worker coverage are required")


def _valid_metric(value: object, expected: Metric) -> bool:
    if type(expected) is int:
        return type(value) is int and value >= 0
    return type(value) is bool


def _matches_shape(value: object, expected: tuple[tuple[str, Metric], ...]) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {key for key, _ in expected}
        and all(_valid_metric(value[key], metric) for key, metric in expected)
    )


def parse_fixture(raw: str) -> IsolationFixture:
    """Validate exact topology coverage before producing any generated case keys."""
    try:
        document = _record(json.loads(raw, object_pairs_hook=_unique_keys))
    except (json.JSONDecodeError, RecursionError):
        raise InvalidIsolationFixture("Malformed isolation fixture JSON") from None
    if (
        type(document.get("fixture_version")) is not int
        or document["fixture_version"] != 1
        or document.get("fixture_kind") != "synthetic"
        or document.get("gate_id") != "G2"
        or document.get("child_key") != "OT-G2-C01"
        or document.get("principal_kinds") != list(_ACTOR_KINDS)
        or document.get("actual_binding_requirements") != list(_ACTUAL_BINDING_FIELDS)
    ):
        raise InvalidIsolationFixture("Expected the OPE-131 synthetic fixture")
    _identifier(document.get("visible_resource_id"))
    cells = _records(document.get("cells"))
    aliases = [_identifier(cell.get("alias")) for cell in cells]
    if Counter(aliases) != Counter(_CELL_MODES.keys()):
        raise InvalidIsolationFixture("Expected exactly four named cells")
    for cell in cells:
        alias = _identifier(cell.get("alias"))
        if cell.get("deployment_mode") != _CELL_MODES[alias]:
            raise InvalidIsolationFixture("Each cell must retain its declared deployment mode")
    for field in ("organization_id", "sentinel", *_SCOPE_FIELDS):
        values = {_identifier(cell.get(field)) for cell in cells}
        if len(values) != len(_CELL_MODES):
            raise InvalidIsolationFixture(f"Each generated cell needs a distinct {field}")
    secondary: dict[tuple[str, str], str] = {}
    for field in _SCOPE_FIELDS:
        primary = {_identifier(cell.get(field)) for cell in cells}
        for cell in cells:
            alias = _identifier(cell.get("alias"))
            scope = _record(cell.get("secondary_scope"))
            if set(scope) != set(_SCOPE_FIELDS):
                raise InvalidIsolationFixture("Secondary scope must cover all dimensions")
            secondary[alias, field] = _identifier(scope[field])
        all_secondary = {secondary[alias, field] for alias in aliases}
        if primary & all_secondary or len(all_secondary) != len(cells):
            raise InvalidIsolationFixture("Secondary scope must use distinct generated identifiers")
    pairs = tuple(
        (_identifier(row.get("initiator_cell")), _identifier(row.get("target_cell")))
        for row in _records(document.get("directed_pairs"))
    )
    scopes = [
        (
            _identifier(row.get("cell_alias")),
            _identifier(row.get("scope_field")),
            _identifier(row.get("foreign_id")),
        )
        for row in _records(document.get("intra_organization_cases"))
    ]
    if Counter(scopes) != Counter(
        (alias, field, target) for (alias, field), target in secondary.items()
    ):
        raise InvalidIsolationFixture("Expected all declared same-organization scope cases")
    _validate_catalog(document)
    return IsolationFixture(pairs, tuple((alias, field) for alias, field, _ in scopes))


def load_fixture(path: Path = FIXTURE_PATH) -> IsolationFixture:
    return parse_fixture(path.read_text(encoding="utf-8"))


def assess_generated(
    fixture: IsolationFixture, mode: DeploymentMode, observations: Iterable[GeneratedObservation]
) -> FixtureAssessment:
    """Report synthetic conformance only. A complete local result leaves the product unrun."""
    expected = {case.case_id: case.expected for case in fixture.cases(mode)}
    seen: set[str] = set()
    failed: set[str] = set()
    incomplete: set[str] = set()
    unexpected: set[str] = set()
    for observed in observations:
        case_id = observed.case_id
        if case_id not in expected:
            unexpected.add(case_id)
            continue
        if case_id in seen:
            incomplete.add(case_id)
        seen.add(case_id)
        metrics = expected[case_id]
        actual = observed.metrics
        if not _matches_shape(actual, metrics):
            incomplete.add(case_id)
        if isinstance(actual, Mapping) and any(
            _valid_metric(value := actual.get(key), metric) and value != metric
            for key, metric in metrics
        ):
            failed.add(case_id)
    incomplete.update(expected.keys() - seen)
    status: FixtureStatus = "PASS"
    if failed:
        status = "FAIL"
    elif incomplete or unexpected:
        status = "INCOMPLETE"
    return FixtureAssessment(
        status,
        len(expected),
        len(expected) - len(incomplete),
        tuple(sorted(failed)),
        tuple(sorted(incomplete)),
        tuple(sorted(unexpected)),
    )


def matches_scoped_absence(
    status: int, body: Mapping[str, object], absent_status: int, absent: Mapping[str, object]
) -> bool:
    """Compare a scoped foreign-reference denial with its generated absent-resource control."""
    expected_fields = {
        "code",
        "message",
        "request_id",
        "retryable",
        "field_errors",
        "current_revision",
    }
    return (
        status == absent_status == 404
        and all(
            set(value) == expected_fields
            and value["code"] == "RESOURCE_NOT_FOUND"
            and type(value["message"]) is str
            and bool(value["message"])
            and type(value["request_id"]) is str
            and bool(value["request_id"])
            and value["retryable"] is False
            and value["field_errors"] == []
            and value["current_revision"] is None
            for value in (body, absent)
        )
        and body["message"] == absent["message"]
    )
