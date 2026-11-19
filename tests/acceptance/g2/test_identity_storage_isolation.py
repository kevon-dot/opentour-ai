"""Generated ISO-01 planning and conformance checks."""

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from itertools import permutations, product
from typing import Any

import pytest

from tools.acceptance.identity_storage_isolation import (
    FIXTURE_PATH,
    CaseObservationReceipt,
    CaseReceiptBinding,
    DeploymentMode,
    GeneratedObservation,
    InvalidIsolationFixture,
    IsolationFixture,
    assess_generated,
    assess_receipts,
    load_fixture,
    matches_scoped_absence,
    parse_fixture,
    parse_referenced_fixture,
)

_MODES: tuple[DeploymentMode, ...] = ("customer_controlled", "opentour_managed")
_INITIATORS = {
    "customer_controlled": {"byoc_a", "byoc_b"},
    "opentour_managed": {"managed_a", "managed_b"},
}
_CELLS = {"byoc_a", "byoc_b", "managed_a", "managed_b"}
_ACTORS = {"human", "workload"}
_OPERATIONS = {"read", "list", "search", "write", "delete", "export", "subscription"}
_SCOPES = {"workspace_id", "project_id", "environment_id"}
_ZERO_DISCLOSURE = {"foreign_bytes": 0, "foreign_mutations": 0, "useful_existence_details": 0}
_GENERATED_MODE: DeploymentMode = "customer_controlled"


@pytest.fixture(scope="module")
def topology() -> IsolationFixture:
    return load_fixture()


def _matching_observations(
    topology: IsolationFixture, mode: DeploymentMode
) -> list[GeneratedObservation]:
    return [
        GeneratedObservation(case.case_id, dict(case.expected)) for case in topology.cases(mode)
    ]


def test_topology_has_twelve_directed_pairs_and_both_initiating_modes(
    topology: IsolationFixture,
) -> None:
    assert Counter(topology.pairs) == Counter(permutations(_CELLS, 2))
    for mode in _MODES:
        initiators = _INITIATORS[mode]
        cross_cell = {
            (case.initiating_cell, case.target_cell)
            for case in topology.cases(mode)
            if case.initiating_cell != case.target_cell
        }
        assert len(cross_cell) == 6
        assert cross_cell == {pair for pair in topology.pairs if pair[0] in initiators}
        assert sum(target not in initiators for _, target in cross_cell) == 4


@pytest.mark.parametrize("mode", _MODES)
def test_each_directed_pair_has_all_actor_operations_and_separate_worker_row(
    topology: IsolationFixture, mode: DeploymentMode
) -> None:
    cases = topology.cases(mode)
    assert len({case.case_id for case in cases}) == len(cases) == 106
    assert {case.mode for case in cases} == {mode}

    pairs = {pair for pair in topology.pairs if pair[0] in _INITIATORS[mode]}
    admissions = [
        case
        for case in cases
        if case.stage == "admission" and case.initiating_cell != case.target_cell
    ]
    assert Counter(
        ((case.initiating_cell, case.target_cell), case.actor_kind, case.operation)
        for case in admissions
    ) == Counter(product(pairs, _ACTORS, _OPERATIONS))

    worker_rows = [case for case in cases if case.stage == "worker_handoff"]
    assert Counter(
        ((case.initiating_cell, case.target_cell), case.actor_kind, case.operation)
        for case in worker_rows
    ) == Counter(product(pairs, {"workload"}, {"worker_fetch"}))
    assert all(dict(case.expected) == _ZERO_DISCLOSURE for case in admissions + worker_rows)


@pytest.mark.parametrize("mode", _MODES)
def test_same_org_dimensions_and_positive_controls_have_separate_rows(
    topology: IsolationFixture, mode: DeploymentMode
) -> None:
    initiators = _INITIATORS[mode]
    within_cell = [
        case for case in topology.cases(mode) if case.initiating_cell == case.target_cell
    ]
    admissions = [case for case in within_cell if case.stage == "admission"]
    controls = [case for case in within_cell if case.stage == "own_scope_control"]
    assert len(admissions) + len(controls) == len(within_cell) == 16
    assert Counter(
        (case.initiating_cell, case.actor_kind, case.operation) for case in admissions
    ) == Counter(product(initiators, _ACTORS, _SCOPES))
    assert all(dict(case.expected) == {"scope_bypass": False} for case in admissions)
    assert Counter(
        (case.initiating_cell, case.actor_kind, case.operation) for case in controls
    ) == Counter(product(initiators, _ACTORS, {"authorized_action"}))
    assert all(dict(case.expected) == {"scope_authorized": True} for case in controls)


@pytest.mark.parametrize("mode", _MODES)
def test_complete_generated_conformance_keeps_product_not_run(
    topology: IsolationFixture, mode: DeploymentMode
) -> None:
    report = assess_generated(topology, mode, _matching_observations(topology, mode))

    assert report.fixture_status == "PASS"
    assert report.product_acceptance == "NOT_RUN"
    assert report.classified_count == report.expected_count == 106
    assert report.failed_cases == report.incomplete_cases == report.unexpected_cases == ()


def test_missing_worker_observation_keeps_conformance_incomplete(
    topology: IsolationFixture,
) -> None:
    worker_id = next(
        case.case_id for case in topology.cases(_GENERATED_MODE) if case.stage == "worker_handoff"
    )
    observations = [
        item
        for item in _matching_observations(topology, _GENERATED_MODE)
        if item.case_id != worker_id
    ]
    report = assess_generated(topology, _GENERATED_MODE, observations)

    assert report.fixture_status == "INCOMPLETE"
    assert report.product_acceptance == "NOT_RUN"
    assert report.incomplete_cases == (worker_id,)
    assert report.classified_count == report.expected_count - 1
    assert report.failed_cases == report.unexpected_cases == ()


def test_duplicate_observation_keeps_conformance_incomplete(topology: IsolationFixture) -> None:
    observations = _matching_observations(topology, _GENERATED_MODE)
    duplicate = observations[0]
    report = assess_generated(topology, _GENERATED_MODE, [*observations, duplicate])

    assert report.fixture_status == "INCOMPLETE"
    assert report.product_acceptance == "NOT_RUN"
    assert report.incomplete_cases == (duplicate.case_id,)
    assert report.classified_count == report.expected_count - 1
    assert report.failed_cases == report.unexpected_cases == ()


def test_wrong_mode_observation_cannot_complete_current_mode(topology: IsolationFixture) -> None:
    observations = _matching_observations(topology, _GENERATED_MODE)
    wrong_mode = _matching_observations(topology, "opentour_managed")[0]
    report = assess_generated(topology, _GENERATED_MODE, [*observations, wrong_mode])

    assert report.fixture_status == "INCOMPLETE"
    assert report.product_acceptance == "NOT_RUN"
    assert report.unexpected_cases == (wrong_mode.case_id,)
    assert report.classified_count == report.expected_count
    assert report.failed_cases == report.incomplete_cases == ()


@pytest.mark.parametrize(
    "metrics",
    [
        {},
        {**_ZERO_DISCLOSURE, "foreign_bytes": False},
        {**_ZERO_DISCLOSURE, "foreign_bytes": -1},
        {**_ZERO_DISCLOSURE, "extra_metric": 0},
    ],
    ids=("missing-field", "boolean-count", "negative-count", "extra-field"),
)
def test_malformed_observation_keeps_conformance_incomplete(
    topology: IsolationFixture, metrics: Mapping[str, object]
) -> None:
    first, *rest = _matching_observations(topology, _GENERATED_MODE)
    malformed = GeneratedObservation(first.case_id, metrics)
    report = assess_generated(topology, _GENERATED_MODE, [malformed, *rest])

    assert report.fixture_status == "INCOMPLETE"
    assert report.product_acceptance == "NOT_RUN"
    assert report.incomplete_cases == (first.case_id,)
    assert report.classified_count == report.expected_count - 1
    assert report.failed_cases == report.unexpected_cases == ()


@pytest.mark.parametrize("leak_first", [True, False], ids=("before-duplicate", "after-duplicate"))
def test_observed_leakage_stays_failed_with_duplicate_and_missing_cases(
    topology: IsolationFixture, leak_first: bool
) -> None:
    original, missing, *rest = _matching_observations(topology, _GENERATED_MODE)
    leakage = GeneratedObservation(original.case_id, {**original.metrics, "foreign_bytes": 1})
    repeated = [leakage, original] if leak_first else [original, leakage]
    report = assess_generated(topology, _GENERATED_MODE, [*repeated, *rest])

    assert report.fixture_status == "FAIL"
    assert report.product_acceptance == "NOT_RUN"
    assert report.failed_cases == (original.case_id,)
    assert set(report.incomplete_cases) == {original.case_id, missing.case_id}
    assert report.unexpected_cases == ()


@pytest.mark.parametrize(
    "metrics",
    [
        {"foreign_bytes": 1},
        {**_ZERO_DISCLOSURE, "foreign_bytes": 1, "extra_metric": 0},
        {**_ZERO_DISCLOSURE, "foreign_bytes": 1, "foreign_mutations": False},
    ],
    ids=("missing-siblings", "extra-sibling", "malformed-sibling"),
)
def test_observed_leakage_stays_failed_with_malformed_sibling_metrics(
    topology: IsolationFixture, metrics: Mapping[str, object]
) -> None:
    original, *rest = _matching_observations(topology, _GENERATED_MODE)
    report = assess_generated(
        topology, _GENERATED_MODE, [GeneratedObservation(original.case_id, metrics), *rest]
    )

    assert report.fixture_status == "FAIL"
    assert report.product_acceptance == "NOT_RUN"
    assert report.failed_cases == report.incomplete_cases == (original.case_id,)
    assert report.classified_count == report.expected_count - 1
    assert report.unexpected_cases == ()


def _absent_response(request_id: str) -> dict[str, object]:
    return {
        "code": "RESOURCE_NOT_FOUND",
        "message": "Resource not found.",
        "request_id": request_id,
        "retryable": False,
        "field_errors": [],
        "current_revision": None,
    }


def test_foreign_lookup_matches_safe_missing_resource_envelope() -> None:
    assert matches_scoped_absence(
        404, _absent_response("generated-foreign"), 404, _absent_response("generated-absent")
    )


@pytest.mark.parametrize(("foreign_status", "absent_status"), [(403, 404), (404, 403), (403, 403)])
def test_lookup_equivalence_requires_two_404_responses(
    foreign_status: int, absent_status: int
) -> None:
    assert not matches_scoped_absence(
        foreign_status,
        _absent_response("generated-foreign"),
        absent_status,
        _absent_response("generated-absent"),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"code": "RESOURCE_FORBIDDEN"},
        {"message": "Generated foreign scope exists."},
        {"request_id": ""},
        {"retryable": True},
        {"field_errors": [{"field": "generated-scope"}]},
        {"current_revision": "generated-revision"},
        {"scope": "generated-foreign"},
    ],
    ids=("code", "message", "request-id", "retryable", "field-errors", "revision", "extra-field"),
)
def test_unsafe_foreign_envelope_cannot_match_absence(changes: Mapping[str, object]) -> None:
    foreign = {**_absent_response("generated-foreign"), **changes}

    assert not matches_scoped_absence(404, foreign, 404, _absent_response("generated-absent"))


@pytest.mark.parametrize(
    "raw", ["[]", "{", '{"fixture_kind":"synthetic","fixture_kind":"synthetic"}']
)
def test_fixture_parser_rejects_bad_json_shape(raw: str) -> None:
    with pytest.raises(InvalidIsolationFixture):
        parse_fixture(raw)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda fixture: fixture.update(fixture_kind="provider_integration"),
        lambda fixture: fixture.update(fixture_version=True),
        lambda fixture: fixture["suites"].pop(),
        lambda fixture: fixture["directed_pairs"].pop(),
        lambda fixture: fixture["intra_organization_cases"].pop(),
    ],
    ids=("kind", "version", "missing-suite", "missing-pair", "missing-scope"),
)
def test_fixture_parser_rejects_invalid_or_incomplete_catalog(
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    mutate(fixture)

    with pytest.raises(InvalidIsolationFixture):
        parse_fixture(json.dumps(fixture))


def test_fixture_parser_requires_cross_cell_scope_metadata() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    iso_01 = next(suite for suite in fixture["suites"] if suite["suite_id"] == "ISO-01")
    cross_cell = next(row for row in iso_01["cases"] if row["id"] == "cross_cell_every_pair")
    del cross_cell["substituted_scope_fields"]

    with pytest.raises(InvalidIsolationFixture):
        parse_fixture(json.dumps(fixture))


@pytest.mark.parametrize(
    "mutation",
    ["duplicate", "self", "unknown"],
)
def test_fixture_parser_rejects_wrong_directed_pair_with_the_same_count(mutation: str) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    pairs = fixture["directed_pairs"]
    match mutation:
        case "duplicate":
            pairs[-1] = pairs[0]
        case "self":
            pairs[-1]["target_cell"] = pairs[-1]["initiator_cell"]
        case "unknown":
            pairs[-1]["target_cell"] = "generated-unknown-cell"
    assert len(pairs) == 12

    with pytest.raises(InvalidIsolationFixture, match="all twelve directed cross-cell pairs"):
        parse_fixture(json.dumps(fixture))


@pytest.mark.parametrize(
    "collection",
    ["cells", "directed_pairs", "intra_organization_cases"],
)
def test_fixture_parser_rejects_undeclared_topology_fields(collection: str) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    fixture[collection][0]["undeclared_binding"] = "generated-binding"

    with pytest.raises(InvalidIsolationFixture, match="Unexpected fields"):
        parse_fixture(json.dumps(fixture))


@pytest.mark.parametrize(
    "case_id", ["cross_cell_every_pair", "same_org_wrong_scope", "own_scope_control"]
)
@pytest.mark.parametrize("field", ["acceptance_ids", "evidence_files"])
def test_fixture_parser_rejects_missing_or_substituted_case_evidence(
    case_id: str, field: str
) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    iso = next(suite for suite in fixture["suites"] if suite["suite_id"] == "ISO-01")
    declaration = next(row for row in iso["cases"] if row["id"] == case_id)
    declaration[field].pop()
    with pytest.raises(InvalidIsolationFixture, match="Incorrect acceptance or evidence"):
        parse_fixture(json.dumps(fixture))

    declaration[field].append("generated-unapproved-association")
    with pytest.raises(InvalidIsolationFixture, match="Incorrect acceptance or evidence"):
        parse_fixture(json.dumps(fixture))


_CELL_IDS = {alias: f"test-cell-{alias}" for alias in sorted(_CELLS)}


def _artifact_ref(kind: str) -> dict[str, object]:
    return {
        "artifact_id": f"test-{kind}",
        "kind": kind,
        "revision": "test-revision",
        "sha256": "a" * 64,
        "manifest_id": "test-manifest",
        "environment_id": "test-environment",
        "classification": "control_metadata",
        "purpose_grant_id": "test-purpose",
    }


@pytest.fixture
def fixture_artifact_ref() -> dict[str, object]:
    return {
        **_artifact_ref("isolation-fixture"),
        "sha256": "e5ef5d089b2305c342be43bd759e84e03a3ba9ca14d0d67005e993fc9add2670",
    }


def test_referenced_fixture_parses_exact_reviewed_source(
    topology: IsolationFixture, fixture_artifact_ref: dict[str, object]
) -> None:
    assert parse_referenced_fixture(FIXTURE_PATH.read_bytes(), fixture_artifact_ref) == topology


def test_referenced_fixture_denies_byte_drift_before_json_parsing(
    fixture_artifact_ref: dict[str, object],
) -> None:
    raw = FIXTURE_PATH.read_bytes() + b" "
    assert parse_fixture(raw.decode()) == load_fixture()
    with pytest.raises(InvalidIsolationFixture, match="fixture artifact digest mismatch"):
        parse_referenced_fixture(raw, fixture_artifact_ref)


@pytest.mark.parametrize("bad_ref", [None, {}, {"sha256": "a" * 64}])
def test_referenced_fixture_requires_the_full_artifact_ref(bad_ref: object) -> None:
    with pytest.raises(InvalidIsolationFixture, match="fixture artifact reference"):
        parse_referenced_fixture(FIXTURE_PATH.read_bytes(), bad_ref)


def test_referenced_fixture_requires_canonical_digest(
    fixture_artifact_ref: dict[str, object],
) -> None:
    fixture_artifact_ref["sha256"] = str(fixture_artifact_ref["sha256"]) + "\n"
    with pytest.raises(InvalidIsolationFixture, match="fixture artifact reference"):
        parse_referenced_fixture(FIXTURE_PATH.read_bytes(), fixture_artifact_ref)


def test_referenced_fixture_refuses_matching_non_utf8_artifact(
    fixture_artifact_ref: dict[str, object],
) -> None:
    raw = b"\xff"
    fixture_artifact_ref["sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(InvalidIsolationFixture, match="fixture artifact encoding"):
        parse_referenced_fixture(raw, fixture_artifact_ref)


def _case_bindings(
    topology: IsolationFixture, mode: DeploymentMode = _GENERATED_MODE
) -> dict[str, CaseReceiptBinding]:
    return {
        case.case_id: CaseReceiptBinding(
            actor_ref=f"test-actor-{case.actor_kind}-{case.initiating_cell}",
            provider_contract_ref=f"test-contract-{mode}",
            input_digest=hashlib.sha256(f"{mode}:{case.case_id}".encode()).hexdigest(),
        )
        for case in topology.cases(mode)
    }


def _receipts(
    topology: IsolationFixture, mode: DeploymentMode = _GENERATED_MODE
) -> list[CaseObservationReceipt]:
    bindings = _case_bindings(topology, mode)
    return [
        {
            "case_id": case.case_id,
            "mode": mode,
            "initiating_cell_id": _CELL_IDS[case.initiating_cell],
            "target_cell_id": _CELL_IDS[case.target_cell],
            "actor_ref": bindings[case.case_id].actor_ref,
            "provider_contract_ref": bindings[case.case_id].provider_contract_ref,
            "input_digest": bindings[case.case_id].input_digest,
            "expected": dict(case.expected),
            "observed": dict(case.expected),
            "assertion_status": "PASS",
            "evidence_refs": [_artifact_ref("observation")],
            "started_at": "2026-11-18T00:00:00.000Z",
            "finished_at": "2026-11-18T00:00:01.000Z",
            "actual_usage_ref": _artifact_ref("usage"),
        }
        for case in topology.cases(mode)
    ]


@pytest.mark.parametrize("mode", _MODES)
def test_complete_receipts_bind_exact_cells_and_cases_and_never_claim_product_acceptance(
    topology: IsolationFixture, mode: DeploymentMode
) -> None:
    report = assess_receipts(
        topology,
        mode,
        _CELL_IDS,
        _receipts(topology, mode),
        case_bindings=_case_bindings(topology, mode),
    )

    assert report.receipt_status == "COMPLETE"
    assert report.expected_count == report.classified_count == 106
    assert not report.failed_cases and not report.incomplete_cases
    assert report.rejected_receipt_count == 0
    assert report.product_acceptance == "NOT_RUN"


def test_no_receipts_and_unbound_cells_are_not_run(topology: IsolationFixture) -> None:
    bindings = _case_bindings(topology)
    report = assess_receipts(topology, _GENERATED_MODE, _CELL_IDS, [], case_bindings=bindings)
    assert report.receipt_status == "NOT_RUN"
    assert report.expected_count == len(report.incomplete_cases) == 106
    assert report.classified_count == 0
    assert not report.missing_cell_bindings

    missing = {alias: actual for alias, actual in _CELL_IDS.items() if alias != "managed_a"}
    report = assess_receipts(
        topology, _GENERATED_MODE, missing, _unread_receipts(), case_bindings=bindings
    )
    assert report.receipt_status == "NOT_RUN"
    assert report.missing_cell_bindings == ("managed_a",)
    assert report.classified_count == 0


def _unread_receipts() -> Iterator[object]:
    yield pytest.fail("receipts were consumed without all approved bindings")


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        CaseReceiptBinding("", "test-contract", "a" * 64),
        CaseReceiptBinding("test-actor", "", "a" * 64),
        CaseReceiptBinding("test-actor", "test-contract", "A" * 64),
    ],
)
def test_missing_or_invalid_case_binding_refuses_intake(
    topology: IsolationFixture, invalid: CaseReceiptBinding | None
) -> None:
    bindings = _case_bindings(topology)
    case_id = next(iter(bindings))
    if invalid is None:
        del bindings[case_id]
    else:
        bindings[case_id] = invalid

    report = assess_receipts(
        topology, _GENERATED_MODE, _CELL_IDS, _unread_receipts(), case_bindings=bindings
    )

    assert report.receipt_status == report.product_acceptance == "NOT_RUN"
    assert report.missing_case_bindings == (case_id,)
    assert report.classified_count == 0


def test_unknown_case_binding_refuses_intake(topology: IsolationFixture) -> None:
    bindings = _case_bindings(topology)
    bindings["unapproved"] = next(iter(bindings.values()))
    with pytest.raises(InvalidIsolationFixture, match="Unexpected approved case binding ID"):
        assess_receipts(
            topology, _GENERATED_MODE, _CELL_IDS, _unread_receipts(), case_bindings=bindings
        )


@pytest.mark.parametrize("replacement", ["managed_a", _CELL_IDS["managed_b"]])
def test_fixture_alias_or_reused_cell_id_cannot_bind(
    topology: IsolationFixture, replacement: str
) -> None:
    report = assess_receipts(
        topology,
        _GENERATED_MODE,
        {**_CELL_IDS, "managed_a": replacement},
        _receipts(topology),
        case_bindings=_case_bindings(topology),
    )
    assert report.receipt_status == "NOT_RUN"
    assert "managed_a" in report.missing_cell_bindings
    assert report.classified_count == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "opentour_managed"),
        ("initiating_cell_id", "byoc_a"),
        ("target_cell_id", _CELL_IDS["byoc_a"]),
        ("target_cell_id", None),
        ("started_at", "invalid"),
        ("finished_at", "2026-11-17T23:59:59.000Z"),
        ("observed", None),
        ("assertion_status", "FAIL"),
        ("input_digest", "invalid"),
        ("evidence_refs", []),
        ("evidence_refs", [{"artifact_id": "test-only"}]),
        ("actual_usage_ref", None),
        ("unknown_property", "unapproved"),
    ],
)
def test_malformed_or_misattributed_receipt_is_incomplete(
    topology: IsolationFixture, field: str, value: object
) -> None:
    first, *other = _receipts(topology)
    report = assess_receipts(
        topology,
        _GENERATED_MODE,
        _CELL_IDS,
        [{**first, field: value}, *other],
        case_bindings=_case_bindings(topology),
    )

    assert report.receipt_status == "INCOMPLETE"
    assert report.classified_count == 105
    assert report.incomplete_cases == (first["case_id"],)
    assert report.rejected_receipt_count == 1
    assert report.product_acceptance == "NOT_RUN"


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("actor_ref", "test-actor-from-another-cell"),
        ("provider_contract_ref", "test-contract-other-revision"),
        ("input_digest", "1" * 64),
    ],
)
def test_wrong_case_binding_cannot_be_used_to_attribute_a_failure(
    topology: IsolationFixture, mode: DeploymentMode, field: str, replacement: str
) -> None:
    first, *rest = _receipts(topology, mode)
    mismatching = {
        **first,
        field: replacement,
        "observed": {**first["expected"], "foreign_bytes": 1},
        "assertion_status": "FAIL",
    }
    report = assess_receipts(
        topology,
        mode,
        _CELL_IDS,
        [mismatching, *rest],
        case_bindings=_case_bindings(topology, mode),
    )

    assert report.receipt_status == "INCOMPLETE"
    assert report.incomplete_cases == (first["case_id"],)
    assert report.rejected_receipt_count == 1
    assert not report.failed_cases


def test_duplicate_missing_and_unrecognized_cases_cannot_complete(
    topology: IsolationFixture,
) -> None:
    first, second, *rest = _receipts(topology)
    unknown = {**first, "case_id": "unrecognized-private-looking-value"}
    report = assess_receipts(
        topology,
        _GENERATED_MODE,
        _CELL_IDS,
        [first, first, unknown, *rest],
        case_bindings=_case_bindings(topology),
    )

    assert report.receipt_status == "INCOMPLETE"
    assert set(report.incomplete_cases) == {first["case_id"], second["case_id"]}
    assert report.classified_count == 104
    assert report.rejected_receipt_count == 1
    assert "unrecognized-private-looking-value" not in repr(report)


def test_oracle_preserves_scoped_failure_with_incomplete_metadata_and_missing_cases(
    topology: IsolationFixture,
) -> None:
    first = _receipts(topology)[0]
    forged = {**first["expected"], "foreign_bytes": 1}
    mismatching = {**first, "expected": forged, "observed": forged, "evidence_refs": []}
    report = assess_receipts(
        topology,
        _GENERATED_MODE,
        _CELL_IDS,
        [mismatching],
        case_bindings=_case_bindings(topology),
    )

    assert report.receipt_status == "FAIL"
    assert report.failed_cases == (first["case_id"],)
    assert len(report.incomplete_cases) == 106
    assert report.classified_count == 0
    assert report.product_acceptance == "NOT_RUN"


def test_unknown_cell_binding_alias_refuses_run(topology: IsolationFixture) -> None:
    with pytest.raises(InvalidIsolationFixture, match="Unexpected approved cell binding alias"):
        assess_receipts(
            topology,
            _GENERATED_MODE,
            {**_CELL_IDS, "unapproved": "test-other"},
            [],
            case_bindings=_case_bindings(topology),
        )


def test_ingestion_freezes_bindings_and_metrics_at_read(topology: IsolationFixture) -> None:
    cell_ids = dict(_CELL_IDS)
    bindings = _case_bindings(topology)
    first, *rest = _receipts(topology)
    metrics = first["observed"]
    assert metrics is not None

    def observations() -> Iterator[CaseObservationReceipt]:
        yield first
        cell_ids["byoc_a"] = "test-replacement"
        other_case = rest[0]["case_id"]
        bindings[other_case] = replace(bindings[other_case], input_digest="2" * 64)
        metrics["foreign_bytes"] = 1
        yield from rest

    report = assess_receipts(
        topology, _GENERATED_MODE, cell_ids, observations(), case_bindings=bindings
    )
    assert report.receipt_status == "COMPLETE"
    assert report.classified_count == 106
    assert not report.failed_cases


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("suffix", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("field", ["input_digest", "evidence_refs", "actual_usage_ref"])
def test_receipt_digests_reject_trailing_line_terminators(
    topology: IsolationFixture, mode: DeploymentMode, suffix: str, field: str
) -> None:
    first, *rest = _receipts(topology, mode)
    malformed = "a" * 64 + suffix
    if field == "input_digest":
        first["input_digest"] = malformed
    elif field == "evidence_refs":
        first["evidence_refs"][0]["sha256"] = malformed
    else:
        usage = first["actual_usage_ref"]
        assert usage is not None
        usage["sha256"] = malformed

    report = assess_receipts(
        topology, mode, _CELL_IDS, [first, *rest], case_bindings=_case_bindings(topology, mode)
    )
    assert report.receipt_status == "INCOMPLETE"
    assert report.classified_count == 105
    assert report.incomplete_cases == (first["case_id"],)
    assert report.rejected_receipt_count == 1
    assert report.product_acceptance == "NOT_RUN"
