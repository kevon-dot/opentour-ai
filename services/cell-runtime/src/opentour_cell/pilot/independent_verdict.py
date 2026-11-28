"""Verify a trained artifact's claim chain and independently re-score its verdict (OPE-528).

This is the one independent scorer (OPE-528, OPE-574 ruling A, OPE-573 rescope). It runs under an
evaluator identity that must differ from the worker that produced the claim. It replays each
frozen arm itself on the sealed decision-time inputs, keeps the per-case predictions and
attributed per-case verdict rows PRIVATELY in its own custody directory, and recomputes every
count from those predictions and the sealed answer key. Caller-asserted totals are never used as
expected values: they are only compared against the recomputation, and a mismatch is an
integrity failure.

The private per-case rows are this evaluator's own record. They share no row contract with the
OPE-573 worker journal, which keeps a separate, positional row of its own.

What it verifies, as three separately reported statements:
  1. ``job_produced_candidate``: the designated open-weight fit manifest is digest
     self-consistent, pinned to the exact base/revision/profile, records nonzero training steps
     and changed trainable parameters, and its artifact digest is the claimed candidate. This is
     ``self_consistent_manifest`` evidence only: the training statistics are the manifest's own
     unauthenticated statements (anyone can reseal a manifest), never independently observed;
  2. ``candidate_served_evaluation``: the replay's serving readback names that digest (no
     mutable alias), the pinned base and inference configuration, and the same fit manifest the
     evaluator was given; every comparator reads back its frozen revision. Fields the readback
     cannot observe (tokenizer bytes, process reload) are reported as unavailable, not assumed;
  3. ``paired_gain_cleared_gates``: on the same untouched population and fixed operating point,
     the candidate's paired improvement against each required comparator clears the registered
     practical threshold under family-level uncertainty, coverage, missing-output and
     critical-slice gates.

The operating point is supplied in its OPE-571 form, the frozen terms with every slice name
already replaced by its keyed digest plus the plain SHA-256 over them, and is checked by
``operating_point_binding`` alone. The evaluator never holds the slice key or plain slice names.

Comparator gaps never hide candidate errors: more comparator missing or malformed outputs than
the policy's ``max_missing`` hold the comparison as incompatible; the candidate's total wrong
automatic decisions may not exceed the comparator's; and cases the comparator deferred but the
candidate automated count against the candidate when wrong (in the family effects, the slice
veto and an error ceiling). Comparator gaps within ``max_missing`` stay outside the paired
counts and the family effects, so the policy bound is what limits them; any wrong automatic
decision the candidate made on those cases is still counted in its total wrong decisions.

One idempotent verdict exists per verdict key (the digest of the whole claim, including its
claimed counts, kind, job manifest, selection basis and producer, plus the full policy digest,
benchmark, answer key, inputs, audit receipt, rubric, scorer version and evaluator). A replay
returns the stored original after re-checking it against the current claim; a changed input is
a new key and a new verdict; a verdict past its ``not_after`` is archived and re-evaluated.
Verified non-wins (no material gain, regression, inconclusive) are first-class outcomes.

The fresh-cohort register is per custody directory: any new claim (whatever ``selection_basis``
the producer declares, since that basis is caller-asserted) is held when its cases or families
overlap any cohort a verdict in this directory scored. Another evaluator's custody directory
does not share it.

Evidence tier, stated precisely: every record is ``locally_keyed``. The evaluator commits its
private predictions with an HMAC key held in its own custody directory, but that directory sits on
a host the operator controls, so custody is NOT separated and the record is NOT proof to a third
party. The signing seam (``EvaluatorSigner``) hands the exact RFC 8785 transcript
``{"purpose": "opentour.pilot.independent-verdict.v1", "payload": <document>}`` to an evaluator
key and attaches an ES256 envelope in the ``opentour_journal`` format. No such key exists yet
(OPE-566 key ceremony), so production records carry no signature; nothing here fabricates one.
Raising the tier is the worker-side verifier's decision against the issuer registry
(``pilot/signed_records.py``, OPE-566), never this record's self-assertion.

Simulated: tests use generated sentinel populations and deterministic replay doubles. Not proven:
no SmolLM2 weights or numeric interpreter run here, so the open-weight adapter is exercised only
through injected entry points; no managed/custom provider route, no stochastic decoding, no Slack,
watch, cycle or release wiring, and no customer evidence. Passing tests prove software behavior
over generated data only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot import open_weight as ow
from opentour_cell.pilot.operating_point import digest

Json = dict[str, Any]
VERDICT = "opentour.pilot.independent-verdict.v1"
PURPOSE = "opentour.pilot.independent-verdict.v1"
RECEIPT = "opentour.benchmark-audit-receipt.v1"
FIT_MANIFEST = "opentour.open_weight_fit.v1"
EVIDENCE_TIER = "locally_keyed"
INTERVAL_METHOD = "bounded_independent_family_hoeffding_v1"
TARGET_METRIC = "wrong_automatic_decisions"
SCORER_ID = "opentour.pilot.independent-decision-scorer"
RUBRIC_REVISION = "exact-label-fixed-threshold.v1"
# Code version of this scorer implementation; bump on any change to scoring or aggregation.
SCORER_VERSION = "independent-decision-scorer.code.v2"
OUTCOMES = (
    "verified_gain",
    "no_material_gain",
    "regression",
    "inconclusive",
    "integrity_failure",
    "stale",
)
# Most severe first: the overall outcome is the first one any reason produces.
_PRECEDENCE = ("integrity_failure", "stale", "regression", "inconclusive", "no_material_gain")
CASE_VERDICTS = (
    "correct_automatic",
    "wrong_automatic",
    "deferred",
    "missing_output",
    "malformed_output",
)
_SHA = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:@/-]{0,127}")
_KINDS = ("open_weight_fit", "simple_repair")
# A tuned candidate must beat both the incumbent and the credible simple repair (OPE-528 AC).
_TUNED_KINDS = ("open_weight_fit",)
_TUNED_COMPARATORS = ("incumbent", "simple_repair")
_SELECTION = ("frozen_before_readout", "after_protected_readout")
_PAIR_FIELDS = ("comparable", "fixes", "new_errors", "unchanged_right", "unchanged_wrong")


class VerdictInputError(ValueError):
    """Structurally invalid or out-of-scope input. No verdict is produced or stored."""


class IndependenceError(VerdictInputError):
    """The evaluator is not separate from the producer of the claim."""


class SigningUnavailable(RuntimeError):
    """The ``opentour_journal`` transcript profile is not importable here (OPE-576 edge)."""


class SigningRefused(RuntimeError):
    """The signer returned something other than one canonical ES256 r||s signature."""


class CustodyError(RuntimeError):
    """The evaluator's private custody directory is unsafe, corrupt or tampered."""


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class DecisionCase:
    """One untouched decision-time input. It never carries the answer."""

    case_id: str
    family_id: str
    features: Mapping[str, str]
    slices: tuple[str, ...] = ()


class SealedPopulation:
    """The sealed benchmark population: decision-time inputs plus the private answer key.

    The answer key reaches only the scorer. ``repr`` and every public digest omit labels.
    """

    def __init__(
        self,
        cases: Sequence[DecisionCase],
        labels: Mapping[str, str],
        *,
        label_set: Sequence[str],
        benchmark_digest: str,
        evidence_origin: str = "generated",
    ) -> None:
        ids = [c.case_id for c in cases]
        if (
            not cases
            or len(set(ids)) != len(ids)
            or set(ids) != set(labels)
            or not all(isinstance(i, str) and _ID.fullmatch(i) for i in ids)
            or not all(isinstance(c.family_id, str) and _ID.fullmatch(c.family_id) for c in cases)
            or not label_set
            or not set(labels.values()) <= set(label_set)
            or not _is_sha(benchmark_digest)
            or evidence_origin not in ("generated", "observed")
        ):
            raise VerdictInputError("invalid_sealed_population")
        self.cases = tuple(sorted(cases, key=lambda c: c.case_id))
        self._labels = dict(labels)
        self.label_set = frozenset(label_set)
        self.benchmark_digest = benchmark_digest
        self.evidence_origin = evidence_origin

    def __repr__(self) -> str:
        return f"SealedPopulation(<{len(self.cases)} sealed cases>)"

    def truth(self, case_id: str) -> str:
        return self._labels[case_id]

    @property
    def membership_digest(self) -> str:
        return digest(["opentour.pilot.sealed-membership.v1", [c.case_id for c in self.cases]])

    @property
    def inputs_digest(self) -> str:
        return digest(
            [
                "opentour.pilot.decision-time-inputs.v1",
                [[c.case_id, c.family_id, dict(c.features), list(c.slices)] for c in self.cases],
            ]
        )

    @property
    def answer_key_digest(self) -> str:
        return digest(
            [
                "opentour.pilot.sealed-answer-key.v1",
                sorted(self._labels.items()),
                sorted(self.label_set),
            ]
        )


@dataclass(frozen=True)
class GatePolicy:
    """Preregistered decision rule, frozen before any outcome is opened."""

    policy_revision: str
    operating_point_digest: str
    # OPE-571 form: the frozen ``terms`` with every critical/material slice name already
    # replaced by its keyed digest. The evaluator never holds the slice key or plain names.
    operating_point_terms: Mapping[str, Any]
    minimum_improvement_bps: int
    alpha_bps: int
    coverage_floor_bps: int
    max_coverage_loss_bps: int
    max_missing: int
    min_families: int
    required_comparators: tuple[str, ...]
    optional_comparators: tuple[str, ...] = ()
    critical_slices: tuple[str, ...] = ()
    decoding: str = "deterministic"
    repeat_runs: int = 1
    retention_seconds: int = 30 * 86_400
    verdict_validity_seconds: int = 7 * 86_400

    def validate(self) -> None:
        ints = (
            self.minimum_improvement_bps,
            self.alpha_bps,
            self.coverage_floor_bps,
            self.max_coverage_loss_bps,
            self.max_missing,
            self.min_families,
            self.repeat_runs,
            self.retention_seconds,
            self.verdict_validity_seconds,
        )
        terms: tuple[object, ...] = (
            tuple(
                self.operating_point_terms.get(k, 0)
                for k in ("minimum_coverage_bps", "minimum_slice_cases", "minimum_verified_cases")
            )
            if isinstance(self.operating_point_terms, Mapping)
            else (None,)
        )
        names = (*self.required_comparators, *self.optional_comparators)
        if (
            not _ID.fullmatch(self.policy_revision)
            or not _is_sha(self.operating_point_digest)
            or not isinstance(self.operating_point_terms, Mapping)
            or not all(type(v) is int and v >= 0 for v in ints)
            or not 0 < self.alpha_bps < 10_000
            or self.min_families < 2
            or self.repeat_runs < 1
            or self.retention_seconds < 1
            or self.verdict_validity_seconds < 1
            or not all(type(v) is int and v >= 0 for v in terms)
            or not self.required_comparators
            or len(set(names)) != len(names)
            or "candidate" in names
            or not all(_ID.fullmatch(n) for n in (*names, *self.critical_slices))
        ):
            raise VerdictInputError("invalid_gate_policy")
        if self.decoding != "deterministic":
            raise VerdictInputError("stochastic_decoding_not_supported")

    def as_json(self) -> Json:
        return {
            "policy_revision": self.policy_revision,
            "operating_point_digest": self.operating_point_digest,
            "operating_point_terms": dict(self.operating_point_terms),
            "target_metric": TARGET_METRIC,
            "minimum_improvement_bps": self.minimum_improvement_bps,
            "interval_method": INTERVAL_METHOD,
            "alpha_bps": self.alpha_bps,
            "coverage_floor_bps": self.coverage_floor_bps,
            "max_coverage_loss_bps": self.max_coverage_loss_bps,
            "max_missing": self.max_missing,
            "min_families": self.min_families,
            "required_comparators": list(self.required_comparators),
            "optional_comparators": list(self.optional_comparators),
            "critical_slices": list(self.critical_slices),
            "decoding": self.decoding,
            "repeat_runs": self.repeat_runs,
            "retention_seconds": self.retention_seconds,
            "verdict_validity_seconds": self.verdict_validity_seconds,
        }

    def digest(self) -> str:
        """Binds every threshold, floor, alpha and minimum, not only the revision label."""
        return digest(self.as_json())

    def term(self, name: str) -> int:
        value = self.operating_point_terms.get(name, 0)
        return value if type(value) is int else 0


@dataclass(frozen=True)
class ClaimedResult:
    """What the worker asserts. Every number here is checked, never trusted."""

    tenant: str
    experiment_id: str
    producer_id: str
    result_revision: str
    candidate_kind: str
    candidate_digest: str
    comparator_digests: Mapping[str, str]
    benchmark_digest: str
    membership_digest: str
    protected_rows: int
    operating_point_digest: str
    policy_revision: str
    policy_digest: str
    claimed_paired: Mapping[str, Mapping[str, int]]
    job_manifest_digest: str | None = None
    # Caller-asserted: bound into the claim digest and recorded, but never trusted to waive the
    # fresh-cohort gate (``IndependentEvaluator._check_selection``).
    selection_basis: str = "frozen_before_readout"

    def validate(self) -> None:
        ids = (self.tenant, self.experiment_id, self.producer_id, self.policy_revision)
        shas = (
            self.result_revision,
            self.candidate_digest,
            self.benchmark_digest,
            self.membership_digest,
            self.operating_point_digest,
            self.policy_digest,
            *self.comparator_digests.values(),
        )
        if (
            not all(isinstance(v, str) and _ID.fullmatch(v) for v in ids)
            or not all(_is_sha(v) for v in shas)
            or not all(isinstance(k, str) and _ID.fullmatch(k) for k in self.comparator_digests)
            or type(self.protected_rows) is not int
            or self.protected_rows < 1
            or self.selection_basis not in _SELECTION
            or (self.job_manifest_digest is not None and not _is_sha(self.job_manifest_digest))
            or not all(
                isinstance(c, Mapping) and all(type(c.get(f)) is int for f in _PAIR_FIELDS)
                for c in self.claimed_paired.values()
            )
        ):
            raise VerdictInputError("invalid_claimed_result")
        if self.candidate_kind not in _KINDS:
            # Managed/custom provider verification is a separate route (not built here).
            raise VerdictInputError("unsupported_candidate_route")

    def digest(self) -> str:
        """Every claimed field, so a different claim can never replay another's verdict."""
        return digest(
            {
                "tenant": self.tenant,
                "experiment_id": self.experiment_id,
                "producer_id": self.producer_id,
                "result_revision": self.result_revision,
                "candidate_kind": self.candidate_kind,
                "candidate_digest": self.candidate_digest,
                "comparator_digests": dict(sorted(self.comparator_digests.items())),
                "benchmark_digest": self.benchmark_digest,
                "membership_digest": self.membership_digest,
                "protected_rows": self.protected_rows,
                "operating_point_digest": self.operating_point_digest,
                "policy_revision": self.policy_revision,
                "policy_digest": self.policy_digest,
                "claimed_paired": {
                    k: {f: v[f] for f in _PAIR_FIELDS}
                    for k, v in sorted(self.claimed_paired.items())
                },
                "job_manifest_digest": self.job_manifest_digest,
                "selection_basis": self.selection_basis,
            }
        )


class ArtifactReplay(Protocol):
    """One frozen arm, replayed by the evaluator itself. Receives decision-time inputs only."""

    def served_identity(self) -> Mapping[str, Any]: ...

    def predict(
        self, features: Sequence[Mapping[str, str]], *, expected_artifact: str
    ) -> Sequence[Mapping[str, Any] | None]: ...


@dataclass(frozen=True)
class EvaluatorSigner:
    """The evaluator-side signing seam (OPE-566 independent-verdict purpose).

    ``sign`` receives the exact RFC 8785 transcript bytes and must return one ES256 signature
    encoded ``base64url_unpadded_r_s_64``. It is held by the evaluator, never by the worker.
    """

    key_id: str
    key_registry_revision: str
    sign: Callable[[bytes], str]


@dataclass(frozen=True)
class IndependentVerdict:
    document: Json
    signature: Json | None
    replayed: bool

    @property
    def verdict_key(self) -> str:
        return str(self.document["verdict_key"])

    @property
    def outcome(self) -> str:
        return str(self.document["outcome"])


# --------------------------------------------------------------------------- scorer


class DecisionScorer:
    """Exact-label scorer at a fixed operating point. Holds no state and reads no totals."""

    scorer_id = SCORER_ID
    scorer_version = SCORER_VERSION
    rubric_revision = RUBRIC_REVISION

    def score_case(
        self, prediction: Mapping[str, Any] | None, truth: str, label_set: frozenset[str]
    ) -> str:
        if prediction is None:
            return "missing_output"
        if not isinstance(prediction, Mapping):
            return "malformed_output"
        status = prediction.get("status")
        if status == "review" and set(prediction) == {"status"}:
            return "deferred"
        label = prediction.get("label")
        if status != "automatic" or set(prediction) != {"status", "label"}:
            return "malformed_output"
        if not isinstance(label, str) or label not in label_set:
            return "malformed_output"
        return "correct_automatic" if label == truth else "wrong_automatic"


def _auto(label: str) -> Json:
    return {"status": "automatic", "label": label}


def _b4_gold() -> tuple[list[str], list[Json], list[Json]]:
    """B4: 200 cases; 40 deferred by both; 19 W->R, 5 W->W, 1 R->W, 135 R->R."""
    truth, real_only, augmented = [], [], []
    plan = [("WR", 19), ("WW", 5), ("RW", 1), ("RR", 135), ("DD", 40)]
    for kind, count in plan:
        for _ in range(count):
            truth.append("A")
            if kind == "DD":
                real_only.append({"status": "review"})
                augmented.append({"status": "review"})
                continue
            real_only.append(_auto("A" if kind[0] == "R" else "B"))
            augmented.append(_auto("A" if kind[1] == "R" else "B"))
    return truth, real_only, augmented


# Gold vectors and their hand-checked expected values. The scorer and the paired aggregator must
# reproduce every one before any verdict is computed; a wrong scorer fails here, not later.
QUALIFICATION_EXPECTED: Json = {
    "canary_matches": 4,
    "canary_permuted_matches": 2,
    "b4": {
        "comparable": 160,
        "fixes": 19,
        "new_errors": 1,
        "unchanged_right": 135,
        "unchanged_wrong": 5,
        "comparator_wrong": 24,
        "candidate_wrong": 6,
        "deferred_both": 40,
        "improvement": [18, 160],
        "candidate_coverage": [160, 200],
    },
}


def qualify_scorer(scorer: DecisionScorer) -> tuple[bool, Json]:
    """Run the fixture oracle. Returns ``(passed, observed)``; never raises on a wrong scorer."""
    labels = frozenset({"A", "B"})
    try:
        truth = ["A", "A", "B", "B"]
        permuted = ["A", "B", "A", "B"]
        frozen = [_auto(v) for v in ["A", "A", "B", "B"]]
        canary = sum(
            scorer.score_case(p, t, labels) == "correct_automatic"
            for p, t in zip(frozen, truth, strict=True)
        )
        canary_permuted = sum(
            scorer.score_case(p, t, labels) == "correct_automatic"
            for p, t in zip(frozen, permuted, strict=True)
        )
        b4_truth, comparator, candidate = _b4_gold()
        comp_rows = [
            scorer.score_case(p, t, labels) for p, t in zip(comparator, b4_truth, strict=True)
        ]
        cand_rows = [
            scorer.score_case(p, t, labels) for p, t in zip(candidate, b4_truth, strict=True)
        ]
        cand_auto = cand_rows.count("correct_automatic") + cand_rows.count("wrong_automatic")
        pair = _pair(comp_rows, cand_rows)
        observed: Json = {
            "canary_matches": canary,
            "canary_permuted_matches": canary_permuted,
            "b4": {
                **{k: pair[k] for k in _PAIR_FIELDS},
                "comparator_wrong": pair["comparator_wrong"],
                "candidate_wrong": pair["candidate_wrong"],
                "deferred_both": pair["excluded"].get("deferred_both", 0),
                "improvement": [
                    pair["comparator_wrong"] - pair["candidate_wrong"],
                    pair["comparable"],
                ],
                "candidate_coverage": [cand_auto, len(cand_rows)],
            },
        }
    except Exception:  # a broken scorer is a failed qualification, never a crash
        return False, {"error": "scorer_raised"}
    return observed == QUALIFICATION_EXPECTED, observed


# --------------------------------------------------------------------------- arithmetic


def _is_auto(v: str) -> bool:
    return v in ("correct_automatic", "wrong_automatic")


def _arm_counts(rows: Sequence[str]) -> Json:
    automatic = sum(_is_auto(v) for v in rows)
    return {
        "automatic": automatic,
        "wrong_automatic": rows.count("wrong_automatic"),
        "deferred": rows.count("deferred"),
        "missing": rows.count("missing_output"),
        "malformed": rows.count("malformed_output"),
        "coverage": [automatic, len(rows)],
    }


def _exclusion(comp: str, cand: str) -> str:
    for side, v in (("candidate", cand), ("comparator", comp)):
        if v == "missing_output":
            return "missing_" + side
        if v == "malformed_output":
            return "malformed_" + side
    if comp == "deferred" and cand == "deferred":
        return "deferred_both"
    return "deferred_by_candidate" if cand == "deferred" else "deferred_by_comparator"


def _pair(comp: Sequence[str], cand: Sequence[str]) -> Json:
    """Paired fixed-threshold transitions on cases both arms decided automatically.

    Cases outside that set are still counted: ``newly_automated`` (comparator deferred, candidate
    automated) with how many of those the candidate got wrong, and the comparator's own missing
    or malformed outputs, so no gate can be satisfied by hiding candidate errors there.
    """
    out: Json = {f: 0 for f in _PAIR_FIELDS}
    excluded: dict[str, int] = {}
    newly, newly_wrong, comp_gaps, gap_wrong = 0, 0, 0, 0
    for c, t in zip(comp, cand, strict=True):
        if c in ("missing_output", "malformed_output"):
            comp_gaps += 1
            gap_wrong += t == "wrong_automatic"
        if c == "deferred" and _is_auto(t):
            newly += 1
            newly_wrong += t == "wrong_automatic"
        if not (_is_auto(c) and _is_auto(t)):
            reason = _exclusion(c, t)
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        out["comparable"] += 1
        right_before, right_after = c == "correct_automatic", t == "correct_automatic"
        key = {
            (False, True): "fixes",
            (True, False): "new_errors",
            (True, True): "unchanged_right",
            (False, False): "unchanged_wrong",
        }[(right_before, right_after)]
        out[key] += 1
    out["comparator_wrong"] = out["unchanged_wrong"] + out["fixes"]
    out["candidate_wrong"] = out["unchanged_wrong"] + out["new_errors"]
    out["excluded"] = dict(sorted(excluded.items()))
    out["newly_automated"] = [newly_wrong, newly]
    out["comparator_gaps"] = comp_gaps
    out["comparator_gap_candidate_wrong"] = gap_wrong
    return out


def _decimal(value: Fraction, *, up: bool) -> str:
    scaled = value * 1_000_000
    n = math.ceil(scaled) if up else math.floor(scaled)
    sign = "-" if n < 0 else ""
    n = abs(n)
    return f"{sign}{n // 1_000_000}.{n % 1_000_000:06d}"


def _family_interval(effects: Sequence[Fraction], alpha_bps: int) -> Json:
    """Two-sided Hoeffding bound on the mean family effect, effects bounded in [-1, 1].

    radius = sqrt(2 ln(2 / alpha) / n). Bounds are rounded outward to 1e-6.
    """
    n = len(effects)
    if n < 2:
        return {"method": INTERVAL_METHOD, "status": "unavailable", "families": n}
    mean = sum(effects, Fraction(0)) / n
    radius = math.sqrt(2 * math.log(2 * 10_000 / alpha_bps) / n)
    radius_up = Fraction(math.nextafter(radius, math.inf))
    return {
        "method": INTERVAL_METHOD,
        "status": "available",
        "families": n,
        "alpha_bps": alpha_bps,
        "estimate": _decimal(mean, up=False),
        "lower": _decimal(mean - radius_up, up=False),
        "upper": _decimal(mean + radius_up, up=True),
    }


def _pp(value: Fraction) -> str:
    """Exact percentage points to four places (B4: 18/160 -> 11.2500)."""
    scaled = value * 100 * 10_000
    n = math.floor(scaled) if scaled >= 0 else math.ceil(scaled)
    sign = "-" if n < 0 else ""
    n = abs(n)
    return f"{sign}{n // 10_000}.{n % 10_000:04d}"


# --------------------------------------------------------------------------- evaluator


def _normal(prediction: object) -> Mapping[str, Any] | None:
    """Keep a replayed output only as plain JSON; anything else is recorded as malformed."""
    if prediction is None:
        return None
    if isinstance(prediction, Mapping):
        try:
            value: Json = json.loads(json.dumps(dict(prediction), allow_nan=False))
            return value
        except (TypeError, ValueError):
            pass
    return {"malformed_output_type": type(prediction).__name__}


def operating_point_binding(
    substituted_terms: Mapping[str, Any], operating_point_digest: str
) -> str | None:
    """Check an operating-point digest against its slice-substituted terms (OPE-571 form).

    REBASE POINT (OPE-571, branch claude/ope-571-slice-digests, unmerged): the digest is a plain
    SHA-256 over ``terms`` after every ``critical_slices``/``material_slices`` name has been
    replaced by its keyed digest, so it is checkable without the per-engagement slice key. The
    rebase target is ``comparison_packet.operating_point_digest`` (comparison_packet.py:411 on
    that branch); until it merges this uses ``operating_point.digest``, the canonicalization
    ``comparison_packet`` applies to ``terms`` on main. Returns ``None`` when bound, else the
    hold reason.

    The substitution check is FORMAT ONLY: a slice entry must be 64 lowercase hex. Without the
    slice key the evaluator cannot tell a keyed digest from a plain SHA-256 of a name (or a name
    that happens to be hex-shaped); keeping plain names out is the worker's OPE-571 obligation.
    """
    if not isinstance(substituted_terms, Mapping) or not _is_sha(operating_point_digest):
        return "operating_point_unverifiable"
    for key in ("critical_slices", "material_slices"):
        names = substituted_terms.get(key, [])
        if not isinstance(names, list) or not all(_is_sha(n) for n in names):
            return "operating_point_slice_names_not_substituted"
    try:
        computed = digest(dict(substituted_terms))
    except (TypeError, ValueError):
        return "operating_point_unverifiable"
    if computed != operating_point_digest:
        return "operating_point_digest_mismatch"
    return None


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA.fullmatch(value))


def fit_manifest_digest(value: object) -> str:
    """SHA-256 of the bytes ``open_weight`` seals a fit manifest with.

    Equivalent to ``open_weight``'s private sealing digest: sorted-key compact JSON, non-ASCII
    kept, NaN refused, one trailing newline. A test pins this to ``open_weight``'s own output, so
    a change there fails loudly instead of silently diverging.
    """
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256((encoded + "\n").encode()).hexdigest()


def signing_transcript(document: Json) -> bytes:
    """Exact signed bytes: RFC 8785 ``{"purpose", "payload"}`` from ``opentour_journal``.

    The primitives are imported, never reimplemented. ``opentour-cell`` does not yet declare the
    ``opentour-journal`` dependency edge (OPE-576); where it is not importable, signing is
    unavailable rather than approximated.
    """
    try:
        from opentour_journal.crypto import transcript
    except ImportError as exc:  # pragma: no cover - depends on the installed kit
        raise SigningUnavailable("opentour_journal_profile_unavailable") from exc
    return transcript(PURPOSE, document)


def _envelope(document: Json, signer: EvaluatorSigner) -> Json:
    try:
        from opentour_journal.crypto import raw_to_der
        from opentour_journal.models import JournalError
    except ImportError as exc:  # pragma: no cover - depends on the installed kit
        raise SigningUnavailable("opentour_journal_profile_unavailable") from exc
    if not (_ID.fullmatch(signer.key_id) and signer.key_registry_revision):
        raise SigningRefused("invalid_signer_identity")
    signature = signer.sign(signing_transcript(document))
    try:
        if not isinstance(signature, str):
            raise SigningRefused("signature_not_text")
        raw_to_der(signature)  # canonical base64url r||s with in-range scalars, or refuse
    except JournalError:
        raise SigningRefused("signature_not_es256_r_s_64") from None
    return {
        "profile_version": "1.0",
        "algorithm": "ES256",
        "encoding": "base64url_unpadded_r_s_64",
        "purpose": PURPOSE,
        "key_id": signer.key_id,
        "key_registry_revision": signer.key_registry_revision,
        "signature": signature,
    }


def _write_once(path: Path, data: bytes) -> bool:
    """Atomically create ``path``. Returns False if it already exists (never overwrites)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(tmp)


@dataclass
class _Reasons:
    items: list[tuple[str, str]] = field(default_factory=list)

    def add(self, outcome: str, reason: str) -> None:
        self.items.append((outcome, reason))

    def has(self, *outcomes: str) -> bool:
        return any(o in outcomes for o, _ in self.items)

    def outcome(self) -> str:
        for candidate in _PRECEDENCE:
            if self.has(candidate):
                return candidate
        return "verified_gain"

    def reasons(self) -> list[str]:
        return sorted({r for _, r in self.items})


class IndependentEvaluator:
    """Separate evaluator identity with its own private custody directory.

    The custody directory holds the evaluator's local commitment key, its private per-case
    predictions and attributed rows (with a retention limit), the issued verdicts and the cohort
    register. Nothing in it is readable through this API by any identity but the evaluator.
    """

    def __init__(
        self,
        *,
        evaluator_id: str,
        custody_dir: str | Path,
        worker_data_dirs: Sequence[str | Path],
        scorer: DecisionScorer | None = None,
        signer: EvaluatorSigner | None = None,
    ) -> None:
        if not isinstance(evaluator_id, str) or not _ID.fullmatch(evaluator_id):
            raise VerdictInputError("invalid_evaluator_identity")
        root = Path(custody_dir).resolve()
        if isinstance(worker_data_dirs, (str, Path)) or not worker_data_dirs:
            # The separation check is the only thing keeping custody off worker data; it is
            # mandatory, never skipped for an empty or missing list.
            raise CustodyError("worker_data_dirs_required")
        for other in worker_data_dirs:
            worker = Path(other).resolve()
            if root == worker or root.is_relative_to(worker) or worker.is_relative_to(root):
                raise CustodyError("evaluator_custody_overlaps_worker_data")
        self.evaluator_id = evaluator_id
        self.scorer = scorer or DecisionScorer()
        self.signer = signer
        self._root = root
        for sub in ("", "verdicts", "predictions", "cohorts", "expired"):
            (root / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
            mode = (root / sub).stat().st_mode
            if (root / sub).is_symlink() or mode & 0o077:
                raise CustodyError("evaluator_custody_not_private")
        key_path = root / "evaluator.key"
        if not key_path.exists():
            _write_once(key_path, secrets.token_bytes(32))
        if key_path.is_symlink() or key_path.stat().st_mode & 0o077:
            raise CustodyError("evaluator_custody_not_private")
        self._key = key_path.read_bytes()
        if len(self._key) != 32:
            raise CustodyError("evaluator_key_corrupt")

    # ---- custody

    def _commit(self, value: object) -> str:
        message = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def _load_verdict(self, key: str) -> IndependentVerdict | None:
        path = self._root / "verdicts" / (key + ".json")
        if not path.exists():
            return None
        try:
            stored = json.loads(path.read_bytes())
            document, signature = stored["document"], stored["signature"]
            body = {k: v for k, v in document.items() if k != "verdict_digest"}
            sealed = document["verdict_digest"] == digest(body)
            # The local seal covers the signature envelope too, so neither can be swapped alone.
            keyed = hmac.compare_digest(
                stored["local_seal"], self._commit({"document": document, "signature": signature})
            )
        except (OSError, ValueError, KeyError, TypeError):
            raise CustodyError("stored_verdict_corrupt") from None
        if not (sealed and keyed and document["verdict_key"] == key):
            raise CustodyError("stored_verdict_tampered")
        return IndependentVerdict(document, signature, replayed=True)

    def private_predictions(self, verdict_key: str, *, accessor_id: str) -> Json:
        """Return the private per-case record while it is within its retention limit.

        ``accessor_id`` is a caller-asserted label, not authentication: it stops an accidental
        read by the worker's code path. Real access control is the custody directory's
        file-system permissions and, later, separated custody (OPE-566).
        """
        if accessor_id != self.evaluator_id:
            raise CustodyError("private_predictions_access_denied")
        if not _is_sha(verdict_key):
            raise CustodyError("invalid_verdict_key")
        path = self._root / "predictions" / (verdict_key + ".json")
        try:
            stored: Json = json.loads(path.read_bytes())
        except FileNotFoundError:
            raise CustodyError("private_predictions_absent_or_purged") from None
        until = stored.get("retention_until")
        if type(until) is not int or until <= int(time.time()):
            raise CustodyError("private_predictions_expired")
        verdict = self._load_verdict(verdict_key)
        if verdict is None or verdict.document["predictions_commitment"] != self._commit(
            stored["record"]
        ):
            raise CustodyError("private_predictions_tampered")
        return stored

    def purge_expired(self) -> int:
        """Delete private predictions past their retention limit; verdicts keep the commitment."""
        removed = 0
        now = int(time.time())
        for path in sorted((self._root / "predictions").glob("*.json")):
            try:
                until = json.loads(path.read_bytes())["retention_until"]
            except (OSError, ValueError, KeyError):
                raise CustodyError("private_predictions_corrupt") from None
            if until <= now:
                path.unlink()
                removed += 1
        return removed

    # ---- evaluation

    def verdict_key(
        self,
        claim: ClaimedResult,
        population: SealedPopulation,
        policy: GatePolicy,
        audit_receipt: Mapping[str, Any],
    ) -> str:
        return digest(
            {
                "schema_version": VERDICT,
                "claim_digest": claim.digest(),
                "answer_key_digest": population.answer_key_digest,
                "inputs_digest": population.inputs_digest,
                "target_metric": TARGET_METRIC,
                "policy_digest": policy.digest(),
                "audit_receipt_digest": audit_receipt.get("receipt_digest"),
                "scorer_id": self.scorer.scorer_id,
                "scorer_version": self.scorer.scorer_version,
                "rubric_revision": self.scorer.rubric_revision,
                "evaluator_id": self.evaluator_id,
            }
        )

    def evaluate(
        self,
        *,
        claim: ClaimedResult,
        population: SealedPopulation,
        audit_receipt: Mapping[str, Any],
        policy: GatePolicy,
        replays: Mapping[str, ArtifactReplay],
        fit_manifest: Mapping[str, Any] | None = None,
    ) -> IndependentVerdict:
        claim.validate()
        policy.validate()
        if claim.candidate_kind in _TUNED_KINDS and not (
            set(_TUNED_COMPARATORS) <= set(policy.required_comparators)
            and set(_TUNED_COMPARATORS) <= set(claim.comparator_digests)
        ):
            # Separate comparisons against both, on the same untouched population; a claim or
            # policy that declares fewer is out of scope, not a weaker verdict.
            raise VerdictInputError("tuned_candidate_requires_incumbent_and_simple_repair")
        if claim.producer_id == self.evaluator_id:
            raise IndependenceError("evaluator_is_producer")
        if not isinstance(audit_receipt, Mapping):
            raise VerdictInputError("invalid_audit_receipt")
        key = self.verdict_key(claim, population, policy, audit_receipt)
        existing = self._load_verdict(key)
        if existing is not None:
            stored = existing.document
            if (
                stored.get("claim_digest") != claim.digest()
                or stored.get("policy_digest") != policy.digest()
                or stored.get("candidate_kind") != claim.candidate_kind
                or stored.get("producer_id") != claim.producer_id
            ):
                raise CustodyError("stored_verdict_claim_mismatch")
            if int(time.time()) <= stored["not_after"]:
                return existing
            self._archive(key, stored["issued_at"])

        reasons = _Reasons()
        qualified, qualification = qualify_scorer(self.scorer)
        if not qualified:
            reasons.add("integrity_failure", "scorer_failed_fixture_oracle")
        self._check_audit(audit_receipt, population, reasons)
        self._check_binding(claim, population, policy, reasons)
        job = self._job_statement(claim, fit_manifest)
        if job["status"] == "failed":
            reasons.add("integrity_failure", "job_" + job["reason"])
        served, identities = self._served_statement(claim, policy, replays, job)
        if served["status"] == "failed":
            reasons.add("integrity_failure", "served_" + served["reason"])

        comparisons: Json = {}
        record: Json = {"rows": [], "predictions": {}}
        gate: Json = {"status": "not_evaluated", "reason": "integrity_or_staleness_hold"}
        if not reasons.has("integrity_failure", "stale"):
            arms = self._replay(policy, population, replays, identities, reasons)
            if arms is not None:
                rows = {
                    name: [
                        self.scorer.score_case(p, population.truth(c.case_id), population.label_set)
                        for p, c in zip(preds, population.cases, strict=True)
                    ]
                    for name, preds in arms.items()
                }
                record = self._private_record(population, arms, rows)
                comparisons = self._compare_all(claim, policy, population, arms, rows, reasons)
                self._check_reproduction(claim, comparisons, reasons)
                self._check_selection(key, claim, population, reasons)
                gate = {
                    "status": reasons.outcome(),
                    "reason": ",".join(reasons.reasons()) or "all_registered_gates_cleared",
                }
        outcome = reasons.outcome()
        return self._issue(
            key,
            claim,
            population,
            audit_receipt,
            policy,
            outcome,
            reasons,
            qualification,
            {
                "job_produced_candidate": job,
                "candidate_served_evaluation": served,
                "paired_gain_cleared_gates": gate,
            },
            comparisons,
            record,
        )

    def _check_audit(
        self, receipt: Mapping[str, Any], population: SealedPopulation, reasons: _Reasons
    ) -> None:
        body = {k: v for k, v in receipt.items() if k not in ("receipt_digest", "receipt_id")}
        bindings = receipt.get("bindings")
        if (
            receipt.get("schema_version") != RECEIPT
            or receipt.get("receipt_digest") != digest(body)
            or receipt.get("receipt_id") != "audit-" + str(receipt.get("receipt_digest"))[:16]
            or not isinstance(bindings, Mapping)
            or not isinstance(bindings.get("frozen"), Mapping)
            or not isinstance(bindings.get("current"), Mapping)
        ):
            reasons.add("integrity_failure", "benchmark_audit_receipt_unverified")
            return
        if (
            receipt.get("superseded_by") is not None
            or receipt.get("verdict") == "stale"
            or bindings["frozen"] != bindings["current"]
        ):
            reasons.add("stale", "benchmark_audit_stale_or_superseded")
        elif receipt.get("verdict") != "evaluation_allowed":
            reasons.add("integrity_failure", "benchmark_audit_" + str(receipt.get("verdict")))
        current = bindings["current"]
        if current.get("answer_key") != population.answer_key_digest:
            reasons.add("stale", "answer_key_not_the_audited_key")
        if current.get("canonical_inputs") != population.inputs_digest:
            reasons.add("stale", "inputs_not_the_audited_inputs")

    def _check_binding(
        self,
        claim: ClaimedResult,
        population: SealedPopulation,
        policy: GatePolicy,
        reasons: _Reasons,
    ) -> None:
        if claim.benchmark_digest != population.benchmark_digest:
            reasons.add("stale", "benchmark_digest_mismatch")
        if claim.membership_digest != population.membership_digest or claim.protected_rows != len(
            population.cases
        ):
            reasons.add("integrity_failure", "population_or_denominator_mismatch")
        if (
            claim.operating_point_digest != policy.operating_point_digest
            or claim.policy_revision != policy.policy_revision
        ):
            reasons.add("integrity_failure", "operating_point_or_policy_mismatch")
        if claim.policy_digest != policy.digest():
            reasons.add("integrity_failure", "policy_digest_mismatch")
        unbound = operating_point_binding(
            policy.operating_point_terms, policy.operating_point_digest
        )
        if unbound:
            reasons.add("integrity_failure", unbound)
        elif set(policy.critical_slices) != set(
            policy.operating_point_terms.get("critical_slices", [])
        ):
            # The slice veto must gate exactly the slices the bound operating point registered.
            reasons.add("integrity_failure", "critical_slices_not_bound_to_operating_point")

    def _job_statement(self, claim: ClaimedResult, manifest: Mapping[str, Any] | None) -> Json:
        if claim.candidate_kind == "simple_repair":
            if manifest is not None or claim.job_manifest_digest is not None:
                return {"status": "failed", "reason": "fit_evidence_on_a_no_fit_repair"}
            return {"status": "not_applicable", "reason": "no_fit_claimed"}
        if manifest is None or claim.job_manifest_digest is None:
            return {"status": "failed", "reason": "job_binding_missing"}
        try:
            seal = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
            identity = manifest["identity"]
            training = manifest["training"]
            changed = training["changed_parameters"]
            steps = training["steps"]
            consistent = (
                manifest["schema"] == FIT_MANIFEST
                and manifest["manifest_sha256"] == fit_manifest_digest(seal)
                and manifest["artifact_sha256"] == fit_manifest_digest(identity)
            )
            pinned = (
                identity["profile"] == ow.PROFILE
                and identity["profile_sha256"] == ow.PROFILE_SHA256
                and identity["base_sha256"] == ow.BASE_SHA256
                and identity["revision"] == ow.REVISION
                and _is_sha(identity["adapter_sha256"])
            )
            trained = (
                type(steps) is int
                and steps > 0
                and isinstance(changed, list)
                and all(type(v) is int and v >= 0 for v in changed)
                and sum(changed) > 0
            )
        except (KeyError, TypeError, AttributeError):
            return {"status": "failed", "reason": "fit_manifest_malformed"}
        if not consistent:
            return {"status": "failed", "reason": "fit_manifest_not_self_consistent"}
        if not pinned:
            return {"status": "failed", "reason": "fit_not_on_pinned_base"}
        if not trained:
            return {"status": "failed", "reason": "no_training_steps_or_changed_parameters"}
        if manifest["manifest_sha256"] != claim.job_manifest_digest:
            return {"status": "failed", "reason": "designated_job_mismatch"}
        if manifest["artifact_sha256"] != claim.candidate_digest:
            return {"status": "failed", "reason": "job_artifact_is_not_the_candidate"}
        return {
            "status": "self_consistent_manifest",
            "reason": "fit_manifest_self_consistent_and_pinned",
            "artifact_digest": manifest["artifact_sha256"],
            "manifest_digest": manifest["manifest_sha256"],
            # The manifest's own statements; a resealed manifest can claim any value.
            "manifest_states_training_steps": steps,
            "manifest_states_changed_trainable_parameters": sum(changed),
            "evidence": "unauthenticated_manifest_self_consistency",
        }

    def _served_statement(
        self,
        claim: ClaimedResult,
        policy: GatePolicy,
        replays: Mapping[str, ArtifactReplay],
        job: Json,
    ) -> tuple[Json, dict[str, str]]:
        expected = {"candidate": claim.candidate_digest, **claim.comparator_digests}
        identities: dict[str, str] = {}
        drift: Json = {}
        for name in ("candidate", *policy.required_comparators, *policy.optional_comparators):
            if name not in replays:
                continue
            if name not in expected:
                return {"status": "failed", "reason": "arm_without_frozen_revision"}, identities
            try:
                served = dict(replays[name].served_identity())
            except Exception:
                return {"status": "failed", "reason": "serving_readback_unavailable"}, identities
            artifact = served.get("artifact_sha256")
            if not _is_sha(artifact):
                return {"status": "failed", "reason": "served_identity_mutable_alias"}, identities
            if artifact != expected[name]:
                return {"status": "failed", "reason": name + "_served_other_artifact"}, identities
            if name == "candidate" and claim.candidate_kind == "open_weight_fit":
                pins = [
                    served.get("base_sha256") == ow.BASE_SHA256,
                    served.get("inference_config_sha256") == ow.PROFILE_SHA256,
                ]
                # Observed only when the readback reports them; never assumed from constants.
                optional = {"tokenizer_sha256": ow.TOKENIZER_SHA256, "reload": "fresh_process"}
                for field_name, pinned in optional.items():
                    if field_name not in served:
                        drift[field_name] = "unavailable"
                    else:
                        pins.append(served[field_name] == pinned)
                        drift[field_name] = "checked"
                if not all(pins):
                    return {"status": "failed", "reason": "served_configuration_drift"}, identities
                if job.get("artifact_digest") not in (None, artifact):
                    return {"status": "failed", "reason": "served_is_not_job_artifact"}, identities
                bundle_manifest = served.get("manifest_sha256")
                if not _is_sha(bundle_manifest):
                    return {"status": "failed", "reason": "bundle_manifest_unavailable"}, identities
                if bundle_manifest != job.get("manifest_digest"):
                    return {"status": "failed", "reason": "bundle_manifest_mismatch"}, identities
                drift["bundle_manifest"] = "checked"
            identities[name] = str(artifact)
        if "candidate" not in identities:
            return {"status": "failed", "reason": "candidate_not_replayed"}, identities
        return {
            "status": "readback_consistent",
            "reason": "replay_readback_names_frozen_revisions",
            "served": dict(sorted(identities.items())),
            "drift_checks": dict(sorted(drift.items())),
        }, identities

    def _replay(
        self,
        policy: GatePolicy,
        population: SealedPopulation,
        replays: Mapping[str, ArtifactReplay],
        identities: Mapping[str, str],
        reasons: _Reasons,
    ) -> dict[str, list[Mapping[str, Any] | None]] | None:
        # Decision-time inputs only: the answer key never enters an inference payload.
        features = [dict(c.features) for c in population.cases]
        arms: dict[str, list[Mapping[str, Any] | None]] = {}
        for name, artifact in sorted(identities.items()):
            runs: list[list[Mapping[str, Any] | None]] = []
            for _ in range(policy.repeat_runs):
                try:
                    out = list(replays[name].predict(features, expected_artifact=artifact))
                except Exception:
                    reasons.add("integrity_failure", name + "_replay_failed")
                    return None
                if len(out) != len(features):
                    reasons.add("integrity_failure", name + "_replay_row_count_mismatch")
                    return None
                runs.append([_normal(p) for p in out])
            if any(r != runs[0] for r in runs[1:]):
                reasons.add("inconclusive", name + "_replay_drift_beyond_tolerance")
            arms[name] = runs[0]
        return arms

    def _private_record(
        self,
        population: SealedPopulation,
        arms: Mapping[str, list[Mapping[str, Any] | None]],
        rows: Mapping[str, list[str]],
    ) -> Json:
        ids = [c.case_id for c in population.cases]
        return {
            "rows": [
                {
                    "ref": case_id,
                    "arm": name,
                    "verdict": verdict,
                    "scorer_id": self.scorer.scorer_id,
                    "scorer_version": self.scorer.scorer_version,
                    "rubric_revision": self.scorer.rubric_revision,
                }
                for name in sorted(rows)
                for case_id, verdict in zip(ids, rows[name], strict=True)
            ],
            "predictions": {
                name: dict(zip(ids, preds, strict=True)) for name, preds in sorted(arms.items())
            },
        }

    def _compare_all(
        self,
        claim: ClaimedResult,
        policy: GatePolicy,
        population: SealedPopulation,
        arms: Mapping[str, list[Mapping[str, Any] | None]],
        rows: Mapping[str, list[str]],
        reasons: _Reasons,
    ) -> Json:
        out: Json = {}
        for name in policy.required_comparators:
            if name not in rows:
                reasons.add("inconclusive", "required_comparison_missing_" + name)
                out[name] = {"outcome": "unrun", "required": True}
                continue
            result = self._compare(claim, policy, population, name, arms, rows)
            out[name] = {**result, "required": True}
            if result["outcome"] != "verified_gain":
                reasons.add(result["outcome"], f"{name}:{result['reason']}")
        for name in policy.optional_comparators:
            if name not in rows:
                out[name] = {"outcome": "unrun", "required": False, "reason": "arm_not_run"}
                continue
            out[name] = {
                **self._compare(claim, policy, population, name, arms, rows),
                "required": False,
            }
        return out

    def _compare(
        self,
        claim: ClaimedResult,
        policy: GatePolicy,
        population: SealedPopulation,
        name: str,
        arms: Mapping[str, list[Mapping[str, Any] | None]],
        rows: Mapping[str, list[str]],
    ) -> Json:
        comp, cand = rows[name], rows["candidate"]
        pair = _pair(comp, cand)
        cand_arm, comp_arm = _arm_counts(cand), _arm_counts(comp)
        families: dict[str, list[int]] = {}
        slices: dict[str, Json] = {
            s: {"comparable": 0, "fixes": 0, "new_errors": 0} for s in policy.critical_slices
        }
        for case, c, t in zip(population.cases, comp, cand, strict=True):
            if _is_auto(c) and _is_auto(t):
                effect = (t == "correct_automatic") - (c == "correct_automatic")
            elif c == "deferred" and _is_auto(t):
                # Newly automated: no credit when right, a full error when wrong.
                effect = -1 if t == "wrong_automatic" else 0
            else:
                continue
            families.setdefault(case.family_id, []).append(effect)
            for s in case.slices:
                if s in slices:
                    slices[s]["comparable"] += 1
                    slices[s]["fixes"] += effect == 1
                    slices[s]["new_errors"] += effect == -1
        effects = [Fraction(sum(v), len(v)) for _, v in sorted(families.items())]
        interval = _family_interval(effects, policy.alpha_bps)
        improvement = Fraction(
            pair["comparator_wrong"] - pair["candidate_wrong"], pair["comparable"] or 1
        )
        threshold = Fraction(policy.minimum_improvement_bps, 10_000)
        min_slice = max(1, policy.term("minimum_slice_cases"))
        for v in slices.values():
            v["status"] = (
                "unsupported"
                if v["comparable"] < min_slice
                else "veto"
                if v["new_errors"] > v["fixes"]
                else "ok"
            )
        checks = {
            "candidate_missing": cand_arm["missing"] + cand_arm["malformed"],
            "comparator_missing": pair["comparator_gaps"],
            "comparator_gap_candidate_wrong": pair["comparator_gap_candidate_wrong"],
            "candidate_coverage": Fraction(*cand_arm["coverage"]),
            "comparator_coverage": Fraction(*comp_arm["coverage"]),
            "candidate_total_wrong": cand_arm["wrong_automatic"],
            "comparator_total_wrong": comp_arm["wrong_automatic"],
            "newly_automated": pair["newly_automated"],
            "shared_fixes": pair["fixes"],
            "shared_new_errors": pair["new_errors"],
            "supported_cases": sum(len(v) for v in families.values()),
        }
        outcome, reason = self._decide(
            claim, name, arms, interval, threshold, slices, checks, policy, len(families)
        )
        return {
            "comparator_digest": claim.comparator_digests[name],
            "candidate_arm": cand_arm,
            "comparator_arm": comp_arm,
            "paired": {k: pair[k] for k in (*_PAIR_FIELDS, "excluded")},
            "wrong_on_comparable": {
                "candidate": [pair["candidate_wrong"], pair["comparable"]],
                "comparator": [pair["comparator_wrong"], pair["comparable"]],
            },
            "total_wrong_automatic": {
                "candidate": cand_arm["wrong_automatic"],
                "comparator": comp_arm["wrong_automatic"],
            },
            "newly_automated": {
                "wrong": pair["newly_automated"][0],
                "total": pair["newly_automated"][1],
            },
            "comparator_gaps": pair["comparator_gaps"],
            "comparator_gap_candidate_wrong": pair["comparator_gap_candidate_wrong"],
            "improvement": {
                "fraction": [improvement.numerator, improvement.denominator]
                if pair["comparable"]
                else None,
                "counts": [pair["comparator_wrong"] - pair["candidate_wrong"], pair["comparable"]],
                "percentage_points": _pp(improvement) if pair["comparable"] else None,
                "threshold_bps": policy.minimum_improvement_bps,
            },
            "uncertainty": interval,
            "critical_slices": dict(sorted(slices.items())),
            "outcome": outcome,
            "reason": reason,
        }

    def _decide(
        self,
        claim: ClaimedResult,
        name: str,
        arms: Mapping[str, list[Mapping[str, Any] | None]],
        interval: Json,
        threshold: Fraction,
        slices: Mapping[str, Json],
        checks: Mapping[str, Any],
        policy: GatePolicy,
        family_count: int,
    ) -> tuple[str, str]:
        """Regression gates run first and are never skipped by an earlier hold.

        Order: identity/copy (exactly zero change) -> every regression gate (shared-case paired
        errors, newly automated errors over the ceiling, critical-slice veto, interval wholly
        below zero) -> holds (missing outputs, comparator gaps, coverage, no ceiling) -> the
        residual total-wrong check -> support -> the interval decision. Only after no regression
        gate fires may an excess of total wrong decisions that is entirely newly automated (and
        within the ceiling) map to ``coverage_expansion_within_ceiling``.
        """
        if claim.candidate_digest == claim.comparator_digests[name]:
            return "no_material_gain", "candidate_is_comparator"
        if arms["candidate"] == arms[name]:
            return "no_material_gain", "predictions_identical_to_comparator"
        newly_wrong, newly = checks["newly_automated"]
        ceiling = policy.operating_point_terms.get("error_ceiling_bps")
        available = interval["status"] == "available"
        # ---- regression gates
        if checks["shared_new_errors"] > checks["shared_fixes"]:
            return "regression", "shared_case_errors_increased"
        if newly and type(ceiling) is int and newly_wrong * 10_000 > ceiling * newly:
            return "regression", "newly_automated_errors_exceed_ceiling"
        if any(v["status"] == "veto" for v in slices.values()):
            return "regression", "critical_slice_veto"
        if available and Fraction(interval["upper"]) < 0:
            return "regression", "upper_bound_below_zero"
        # ---- holds: incompatible or unbounded comparisons
        if checks["candidate_missing"] > policy.max_missing:
            return "inconclusive", "missing_or_malformed_predictions"
        if checks["comparator_missing"] > policy.max_missing:
            # Dropping these would hide whatever the candidate did on them.
            return "inconclusive", "comparator_missing_or_malformed_outputs"
        cand_cov, comp_cov = checks["candidate_coverage"], checks["comparator_coverage"]
        floor = max(policy.coverage_floor_bps, policy.term("minimum_coverage_bps"))
        if cand_cov * 10_000 < floor or (
            (comp_cov - cand_cov) * 10_000 > policy.max_coverage_loss_bps
        ):
            return "inconclusive", "coverage_incompatible"
        if newly and type(ceiling) is not int:
            return "inconclusive", "newly_automated_without_error_ceiling"
        # ---- residual total-wrong check, safe only after the shared-case gate above
        excess = checks["candidate_total_wrong"] - checks["comparator_total_wrong"]
        if excess > 0:
            if excess > newly_wrong:
                # More extra errors than newly automated ones explain (e.g. candidate errors on
                # comparator-gap cases with nothing offsetting them).
                return "regression", "total_wrong_automatic_increased"
            if checks["comparator_gap_candidate_wrong"]:
                # The candidate erred on comparator-gap cases, and those errors are balanced
                # elsewhere (the candidate deferring comparator errors, or shared fixes); they
                # are not newly automated, so they are not labelled coverage expansion.
                return "inconclusive", "comparator_gap_errors_offset"
            # The excess is all newly automated, within the ceiling, with no shared-case,
            # slice or interval regression: more automation, not a gain. Never alerts.
            return "inconclusive", "coverage_expansion_within_ceiling"
        # ---- support and the interval decision
        if any(v["status"] == "unsupported" for v in slices.values()):
            return "inconclusive", "critical_slice_unsupported"
        if checks["supported_cases"] < policy.term("minimum_verified_cases"):
            return "inconclusive", "below_minimum_verified_cases"
        if not available or family_count < policy.min_families:
            return "inconclusive", "uncertainty_unavailable_or_insufficient_support"
        lower, upper = Fraction(interval["lower"]), Fraction(interval["upper"])
        if lower > threshold:
            return "verified_gain", "lower_bound_clears_threshold"
        if upper < threshold:
            return "no_material_gain", "upper_bound_below_threshold"
        return "inconclusive", "interval_spans_threshold"

    def _check_reproduction(
        self, claim: ClaimedResult, comparisons: Json, reasons: _Reasons
    ) -> None:
        for name, claimed in sorted(claim.claimed_paired.items()):
            ours = comparisons.get(name, {}).get("paired")
            if ours is None or any(claimed[f] != ours[f] for f in _PAIR_FIELDS):
                reasons.add("integrity_failure", "claimed_counts_not_reproduced_" + name)

    def _check_selection(
        self, key: str, claim: ClaimedResult, population: SealedPopulation, reasons: _Reasons
    ) -> None:
        """A new claim needs cases and families no scored verdict here has seen.

        ``selection_basis`` is the producer's own, caller-asserted statement, so it never waives
        this gate: whatever basis is declared, any claim other than the one under this verdict
        key is held when its cases or families overlap a cohort this custody already scored (a
        reselected candidate declared ``frozen_before_readout`` is held exactly like one declared
        ``after_protected_readout``). The one marker skipped is the claim's own: an expired
        verdict being re-evaluated must not count its own earlier scoring as a prior readout.
        """
        cases = {c.case_id for c in population.cases}
        families = {c.family_id for c in population.cases}
        for path in sorted((self._root / "cohorts").glob("*.json")):
            if path.name == key + ".json":
                continue
            try:
                seen = json.loads(path.read_bytes())
                overlap = cases & set(seen["cases"]) or families & set(seen["families"])
            except (OSError, ValueError, KeyError, TypeError):
                raise CustodyError("cohort_register_corrupt") from None
            if overlap:
                reasons.add("inconclusive", "fresh_confirmation_cohort_required")
                return

    def _mark_viewed(self, key: str, population: SealedPopulation) -> None:
        """Record exactly the cases and families a scored, issued verdict used."""
        marker = {
            "cases": [c.case_id for c in population.cases],
            "families": sorted({c.family_id for c in population.cases}),
        }
        _write_once(self._root / "cohorts" / (key + ".json"), json.dumps(marker).encode())

    def _archive(self, key: str, issued_at: int) -> None:
        """Move an expired verdict and its private record aside so a fresh one can be issued."""
        for sub in ("verdicts", "predictions"):
            source = self._root / sub / (key + ".json")
            if source.exists():
                os.replace(source, self._root / "expired" / f"{sub}-{key}-{issued_at}.json")

    def _issue(
        self,
        key: str,
        claim: ClaimedResult,
        population: SealedPopulation,
        receipt: Mapping[str, Any],
        policy: GatePolicy,
        outcome: str,
        reasons: _Reasons,
        qualification: Json,
        statements: Json,
        comparisons: Json,
        record: Json,
    ) -> IndependentVerdict:
        now = int(time.time())
        # A gain the worker never stated is not a reproduced claim: every required comparison
        # must carry claimed counts, and those counts must equal the recomputation.
        stated = all(n in claim.claimed_paired for n in policy.required_comparators)
        reproduced = outcome == "verified_gain" and stated
        alert = (
            reproduced
            and statements["candidate_served_evaluation"]["status"] == "readback_consistent"
        )
        primary = policy.required_comparators[0]
        body: Json = {
            "schema_version": VERDICT,
            "verdict_key": key,
            "tenant": claim.tenant,
            "experiment_id": claim.experiment_id,
            "producer_id": claim.producer_id,
            "verifier_id": self.evaluator_id,
            "claim_digest": claim.digest(),
            "policy_digest": policy.digest(),
            "selection_basis": claim.selection_basis,
            "selection_basis_source": "caller_asserted",
            "job_manifest_digest": claim.job_manifest_digest,
            "outcome": outcome,
            "reasons": reasons.reasons(),
            # Compatibility with the OPE-421 consumer: "reproduced" means the improvement claim
            # was independently reproduced (verified gain), nothing weaker.
            "verdict": "reproduced" if reproduced else "not_reproduced",
            "claim_counts_stated": stated,
            "statements": statements,
            "comparisons": comparisons,
            "candidate_kind": claim.candidate_kind,
            "candidate_digest": claim.candidate_digest,
            "comparator_digest": claim.comparator_digests.get(primary),
            "comparator_digests": dict(sorted(claim.comparator_digests.items())),
            "comparison_evidence_revision": claim.result_revision,
            "audit_receipt_digest": receipt.get("receipt_digest"),
            "benchmark_digest": claim.benchmark_digest,
            "answer_key_digest": population.answer_key_digest,
            "inputs_digest": population.inputs_digest,
            "operating_point_digest": claim.operating_point_digest,
            "target_metric": TARGET_METRIC,
            "policy": policy.as_json(),
            "denominator": {
                "protected_rows": len(population.cases),
                "membership_digest": population.membership_digest,
            },
            "scorer": {
                "scorer_id": self.scorer.scorer_id,
                "scorer_version": self.scorer.scorer_version,
                "rubric_revision": self.scorer.rubric_revision,
                "qualification": "passed"
                if "scorer_failed_fixture_oracle" not in reasons.reasons()
                else "failed",
                "qualification_digest": digest(qualification),
            },
            "predictions_commitment": self._commit(record),
            "predictions_custody": "evaluator_private_retention_limited",
            "effects": {
                "improvement_alert_eligible": alert,
                "release_request_eligible": False,
                "handoff_request_eligible": False,
            },
            "evidence": {
                "evidence_tier": EVIDENCE_TIER,
                "evidence_origin": population.evidence_origin,
                # Only the identity labels differ; who actually runs each side is asserted by
                # the caller and not established by this record.
                "producer_distinct_from_evaluator": "caller_asserted_identity_labels",
                "custody_separated": False,
                "issuer_authenticated": False,
                "claim_scope": "implementation_only",
                "accuracy_claim_eligible": False,
            },
            # Issuer time inside the signed payload (OPE-566 section 5): the evaluator's clock.
            "issued_at": now,
            "not_after": now + policy.verdict_validity_seconds,
        }
        document = {**body, "verdict_digest": digest(body)}
        signature = _envelope(document, self.signer) if self.signer is not None else None
        stored = {
            "document": document,
            "signature": signature,
            "local_seal": self._commit({"document": document, "signature": signature}),
        }
        path = self._root / "verdicts" / (key + ".json")
        private = {"retention_until": now + policy.retention_seconds, "record": record}
        _write_once(self._root / "predictions" / (key + ".json"), json.dumps(private).encode())
        if not _write_once(path, json.dumps(stored, sort_keys=True).encode()):
            original = self._load_verdict(key)  # lost a race: the first issued verdict stands
            assert original is not None
            return original
        if record["rows"]:
            self._mark_viewed(key, population)
        return IndependentVerdict(document, signature, replayed=False)


# --------------------------------------------------------------------------- open-weight adapter


class OpenWeightReplay:
    """``ArtifactReplay`` over the pinned SmolLM2 adapter (OPE-386/387 entry points).

    Identity comes from ``inspect_open_weight`` (manifest, pins and adapter bytes re-verified);
    inference is ``predict_open_weight_batch`` in a fresh process with the verified artifact as
    ``expected_artifact_sha256``, so a lineage mismatch is refused by ``open_weight`` itself.
    The readback reports only what the bundle states: it does not report tokenizer bytes or a
    reload, so the evaluator records those drift checks as unavailable.
    """

    def __init__(
        self,
        bundle: str | Path,
        *,
        model_dir: str | Path,
        grant: Mapping[str, Any],
        python: str | Path | None = None,
        inspect_fn: Callable[..., dict[str, Any]] = ow.inspect_open_weight,
        predict_fn: Callable[..., list[dict[str, str]]] = ow.predict_open_weight_batch,
    ) -> None:
        self._bundle = Path(bundle)
        self._model_dir = Path(model_dir)
        self._grant = grant
        self._python = python
        self._inspect = inspect_fn
        self._predict = predict_fn

    def served_identity(self) -> Mapping[str, Any]:
        manifest = self._inspect(self._bundle, grant=self._grant)
        identity = manifest["identity"]
        return {
            "artifact_sha256": manifest["artifact_sha256"],
            "manifest_sha256": manifest["manifest_sha256"],
            "base_sha256": identity["base_sha256"],
            "inference_config_sha256": identity["profile_sha256"],
        }

    def predict(
        self, features: Sequence[Mapping[str, str]], *, expected_artifact: str
    ) -> Sequence[Mapping[str, Any] | None]:
        out: list[Mapping[str, Any] | None] = []
        for start in range(0, len(features), 64):  # the profile's batch bound
            out.extend(
                self._predict(
                    self._bundle,
                    [dict(f) for f in features[start : start + 64]],
                    model_dir=self._model_dir,
                    grant=self._grant,
                    python=self._python,
                    expected_artifact_sha256=expected_artifact,
                )
            )
        return out
