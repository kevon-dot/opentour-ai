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
     For a managed provider or customer trainer candidate (``managed_fit``,
     ``custom_trainer_fit``) statement 1 is ``provider_reported`` / ``trainer_reported``: the job
     record (``ProviderJobRecord``) is bound by digest to the claim, reports terminal success,
     and names the frozen parent and an immutable candidate id; weights are recorded as
     ``provider_attested_opaque`` and no tensor-level evidence is claimed. Statement 2 needs a
     readback whose immutable served model id (and parent and artifact digest, where reported)
     match; a mutable alias, a missing job binding, a parent mismatch or a readback naming
     another revision is an integrity failure and no inference runs. Real fine-tuning APIs do
     not sign, so nothing on this route is called verified;
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

Correction and revocation: ``revoke_verdict`` appends one sealed, write-once revocation per
verdict key. From then on ``read_verdict``, ``evaluate`` and the consumer check ``authorize``
report the key as ``stale`` (state ``revoked``) and never as its original outcome, for the live
verdict, archived generations and any re-issue alike. When the consumer says a watch already
fired (``watch_fired=True``), exactly one correction-notice record exists for the key, across
retries, restarts and concurrent callers. ``revoke_scope`` revokes by scope instead: every
verdict, issued or future, scored by one scorer version or bound to one answer key or audit
receipt, so a scorer defect is not revoked one key at a time. It refuses a value this custody
never scored (``revocation_scope_unknown``) instead of reporting a revocation that matches
nothing.

Semantic re-keying (re-review C1, C2, C4). The producer supplies the population and the audit
receipt, and the OPE-526 receipt is self-sealed by a plain digest. So a producer can re-mint an
equivalent receipt under a new digest (C1: escapes an audit-receipt revocation) or rename every
case id (C2: changes the answer-key digest). C2 is closed while reference fields are declared
honestly: an answer-key revocation also matches the key's id-free content digest. C4,
re-minting combined with the A2 id renaming that once emptied the cohort content digest, is
closed for the cohort gate, again while reference fields are declared honestly. A dishonest
declaration is held or refused, never trusted: an undeclared field carrying the case's own id
holds as ``undeclared_reference_field`` before any inference (D2, E1); a declared name that
differs from its feature key by case or a look-alike letter is refused (D3, D3b); a declared
reference must carry its case's own id (G7, G8: ``reference_field_carries_no_id``) and, once
those ids are removed, be identical across all cases, so a reference can carry at most a
constant decoration and no per-case content (J1c, J1d: ``reference_field_carries_content``);
and a population whose id-free content collapses is refused (``decision_content_collapsed``,
exact rule in ``_check_content_not_collapsed``). A producer can still edit or split the
decision text itself; that is new content (D5 below), not content hidden in a reference. Case
and family ids must be random or opaque (for example a UUID or a hash), not purely numeric and
at least ``MIN_ID_LENGTH`` characters (``case_id_format_unverifiable``; only length and the
not-numeric rule are enforced), so these own-id checks do not fire on ordinary text.
Populations that cannot be compared (``undeclared_reference_field``,
``decision_content_unverifiable``) are decided when they are built and held before any arm is
replayed. One ``served_identity`` readback per arm still runs before the hold (the identity
statement is computed first): one provider call, but no inference.
Relabelling the same partition (swapping label names consistently) escapes the answer-key
content match; the fresh-cohort gate is the backstop there. Residual, NOT closed: A1 (a new
field with a fresh per-case value), D5 (any edit to the decision text is new content, so a
fresh cohort) and C1 (nothing here can tell a re-minted receipt from a new one). Ids are
producer-chosen, up to 128 characters, and can carry content the register never sees. Only an
authenticated OPE-526 audit issuer closes these.

Known limits of revocation, kept on purpose and pinned by tests. The revocation lives in the
same custody directory as the verdict, so whoever can delete files there can also delete a
revocation, and deleting it restores the original verdict's authority; that is the same
``locally_keyed`` limit as every other record here, and only separated custody (OPE-566) closes
it. ``issued_by`` and ``evaluator_id`` are caller-chosen labels, unauthenticated: nothing here
proves who revoked or who evaluated. ``authorize`` is the check that sees a revocation; a
consumer that checks only a document's plain digest (``comparison_packet``) does not, so a
consumer must call ``authorize`` before acting.

The fresh-cohort register is per custody directory: any new claim (whatever ``selection_basis``
the producer declares, since that basis is caller-asserted) is held when its cases or families
overlap any cohort a verdict in this directory scored. Another evaluator's custody directory
does not share it.

Evidence tier, stated precisely: an exported verdict document is ``digest_self_consistent_only``.
Its ``verdict_digest`` is a plain SHA-256 that anyone can recompute after rewriting the document,
so a consumer that checks only that digest (as ``comparison_packet._verdict_ok`` does) accepts a
forgery. The keyed seal stays in the evaluator's store: only a verdict the evaluator re-reads from
its own custody directory, after checking its HMAC seal, is ``locally_keyed``
(``IndependentVerdict.custody_tier``). That key is held on a host the operator controls, so
custody is NOT separated and even a ``locally_keyed`` record is NOT proof to a third party. The
audit receipt it binds is self-sealed by a plain digest too, and is recorded as such. The signing
seam (``EvaluatorSigner``) hands the exact RFC 8785 transcript
``{"purpose": "opentour.pilot.independent-verdict.v1", "payload": <document>}`` to an evaluator
key and attaches an ES256 envelope in the ``opentour_journal`` format. No such key exists yet
(OPE-566 key ceremony), so production records carry no signature; nothing here fabricates one.
Raising the tier is the worker-side verifier's decision against the issuer registry
(``pilot/signed_records.py``, OPE-566), never this record's self-assertion.

Simulated: tests use generated sentinel populations and deterministic replay doubles. Not proven:
no SmolLM2 weights or numeric interpreter run here, so the open-weight adapter is exercised only
through injected entry points; the managed/custom provider route is exercised only through
generated job records and provider doubles, with no real provider account, job or serving
endpoint; no stochastic decoding, no Slack, watch, cycle or release wiring, and no customer
evidence. Passing tests prove software behavior over generated data only.
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
import unicodedata
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
# The exported document's tier: its plain verdict_digest can be recomputed by anyone.
EVIDENCE_TIER = "digest_self_consistent_only"
# Only a verdict re-read from the evaluator's own store, with its HMAC seal checked.
STORE_TIER = "locally_keyed"
RECEIPT_TIER = "self_sealed_digest_only"
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
# Managed provider / customer trainer candidates: candidate kind -> route of the job record.
_PROVIDER_ROUTES = {"managed_fit": "managed_provider", "custom_trainer_fit": "custom_trainer"}
# The terminal success state each route's job record must report.
_JOB_DONE = {"managed_provider": "succeeded", "custom_trainer": "completed"}
# Who makes the job statement. Never "verified": real fine-tuning APIs do not sign results.
_JOB_REPORTER = {"managed_provider": "provider_reported", "custom_trainer": "trainer_reported"}
# Identifier tails that name a moving pointer rather than one revision (format-only defence).
_ALIASES = frozenset(
    {"latest", "current", "default", "stable", "prod", "production", "live", "main", "head"}
)
# A tuned candidate must beat both the incumbent and the credible simple repair (OPE-528 AC).
_TUNED_KINDS = ("open_weight_fit", *_PROVIDER_ROUTES)
_TUNED_COMPARATORS = ("incumbent", "simple_repair")
_SELECTION = ("frozen_before_readout", "after_protected_readout")
_PAIR_FIELDS = ("comparable", "fixes", "new_errors", "unchanged_right", "unchanged_wrong")
REVOCATION = "opentour.pilot.independent-verdict-revocation.v1"
SCOPE_REVOCATION = "opentour.pilot.independent-verdict-scope-revocation.v1"
# What a scope revocation can name, and where each value sits in a verdict document.
REVOCATION_SCOPES = (
    "scorer_version",
    "answer_key_digest",
    "audit_receipt_digest",
    "answer_key_content_digest",
)
# Scopes a revoker can compute without a verdict here, so they may be revoked pre-emptively.
_PREEMPTIVE_SCOPES = ("answer_key_content_digest",)
CORRECTION_NOTICE = "opentour.pilot.independent-verdict-correction-notice.v1"
HELD_VIEW = "opentour.pilot.independent-verdict-held-view.v1"
# Closed vocabulary: a revocation never carries free text.
REVOCATION_REASONS = (
    "benchmark_audit_superseded",
    "answer_key_defect",
    "scorer_defect",
    "artifact_identity_defect",
    "evidence_withdrawn",
    "claim_corrected",
)
_EFFECT_FLAGS = {
    "improvement_alert": "improvement_alert_eligible",
    "release_request": "release_request_eligible",
    "handoff_request": "handoff_request_eligible",
}
_NO_EFFECTS = {flag: False for flag in _EFFECT_FLAGS.values()}


@dataclass(frozen=True)
class PolicyFloor:
    """A registered minimum verdict policy (master PRD owner decision D11).

    A verdict's preregistered policy is the producer's own choice; this floor is the owner's.
    A verdict may drive an effect only when its bound policy is at least this strict.
    """

    floor_id: str
    max_alpha_bps: int
    min_improvement_bps: int
    min_families: int
    min_coverage_floor_bps: int
    max_missing: int

    def shortfalls(self, policy: Mapping[str, Any]) -> list[str]:
        def value(name: str) -> int | None:
            v = policy.get(name) if isinstance(policy, Mapping) else None
            return v if type(v) is int else None

        checks: tuple[tuple[str, Callable[[int], bool]], ...] = (
            ("alpha_bps", lambda v: v <= self.max_alpha_bps),
            ("minimum_improvement_bps", lambda v: v >= self.min_improvement_bps),
            ("min_families", lambda v: v >= self.min_families),
            ("coverage_floor_bps", lambda v: v >= self.min_coverage_floor_bps),
            ("max_missing", lambda v: v <= self.max_missing),
        )
        out = []
        for name, ok in checks:
            v = value(name)
            if v is None or not ok(v):
                out.append(name)
        return out


# D11 SEAM. The owner has not decided a minimum verdict policy (alpha, minimum improvement,
# minimum families, coverage, the comparator-gap allowance and whether a tie at the ceiling
# counts), so NO floor is registered and every effect a verified_gain could drive holds with
# ``policy_floor_unset``. As with the OPE-526 audit/verdict issuer hold in comparison_packet,
# there is no re-enable path short of a reviewed code change here: registering a floor means
# replacing this ``None`` with the owner's ``PolicyFloor`` (and adding any D11 terms it lacks).
# It is never a caller argument, a policy field or an environment setting.
REGISTERED_POLICY_FLOOR: PolicyFloor | None = None


def policy_floor_status(policy: Mapping[str, Any]) -> str:
    """``unset`` (no D11 floor registered), ``satisfied`` or ``below_registered_floor``."""
    floor = REGISTERED_POLICY_FLOOR
    if floor is None:
        return "unset"
    return "below_registered_floor" if floor.shortfalls(policy) else "satisfied"


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
        reference_fields: Sequence[str] = (),
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
            or isinstance(reference_fields, str)
            # Shape of each declared name is checked by ``_check_schema``.
            or not all(isinstance(f, str) for f in reference_fields)
            or len(set(reference_fields)) != len(reference_fields)
            or not all(isinstance(c.features, Mapping) for c in cases)
        ):
            raise VerdictInputError("invalid_sealed_population")
        _check_id_format(cases)
        _check_schema(cases, reference_fields)
        _check_references_carry_ids(cases, reference_fields)
        _check_references_carry_no_content(cases, reference_fields)
        _check_content_not_collapsed(cases, reference_fields)
        self.cases = tuple(sorted(cases, key=lambda c: c.case_id))
        self._labels = dict(labels)
        self.label_set = frozenset(label_set)
        self.benchmark_digest = benchmark_digest
        self.evidence_origin = evidence_origin
        # The benchmark schema's declared reference/id fields, by exact field name. They are left
        # out of the id-free decision content; nothing is ever excluded by what a value contains.
        self.reference_fields = tuple(sorted(reference_fields))
        # Computed once, before any inference. ``evaluate`` holds such a population without
        # replaying any arm: some undeclared value carries its case's own id
        # (``undeclared_reference_field``), or some case has no id-free content left to compare
        # (``decision_content_unverifiable``).
        self.undeclared_reference = any(carries_own_id(c, self.reference_fields) for c in cases)
        self.content_unverifiable = any(
            decision_content_digest(c, self.reference_fields) is None for c in cases
        )

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
                list(self.reference_fields),
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

    @property
    def answer_key_content_digest(self) -> str | None:
        """The answer key without ids: each case's decision content paired with its label.

        Renaming every case id changes ``answer_key_digest`` but not this. ``None`` when any case
        has no id-free content, since such a key cannot be recognised without its ids.
        """
        pairs = []
        for c in self.cases:
            content = decision_content_digest(c, self.reference_fields)
            if content is None:
                return None
            pairs.append([content, self._labels[c.case_id]])
        return digest(["opentour.pilot.answer-key-content.v1", sorted(pairs)])


# Feature keys and declared reference names: ASCII identifiers only. Hyphenated ("case-ref") and
# dotted ("case.ref") keys are refused too; a schema using them must be mapped first.
_ASCII_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Case and family ids: random or opaque (for example a UUID or a hash), not purely numeric, at
# least this many characters. Only the length and the not-numeric parts are enforced; whether an
# id is really opaque is not (no dictionary is consulted).
MIN_ID_LENGTH = 8


def _fold(text: str) -> str:
    """NFKC then casefold: the form two names or texts are compared in."""
    return unicodedata.normalize("NFKC", text).casefold()


def _value_text(value: object) -> str:
    """A feature value as comparable text: strings as is, anything else as canonical JSON."""
    return _fold(value if isinstance(value, str) else json.dumps(value, sort_keys=True))


def _contains_own_id(case: DecisionCase, value: object) -> bool:
    text = _value_text(value)
    return _fold(case.case_id) in text or _fold(case.family_id) in text


def _id_free_remainder(case: DecisionCase, value: object) -> str:
    """The folded value with each occurrence of the case's own ids replaced by a typed token.

    One pass, independent of order: every match span of either id is found in the folded value,
    overlapping matches included (a self-overlapping id such as ``aaaaaaaa`` in ``aaaaaaaaa``
    is one span). Spans that truly overlap are merged; spans that merely touch are not. Each
    resulting span is replaced by a placeholder, ``\\x00c`` when it covers only the case id,
    ``\\x00f`` only the family id and ``\\x00m`` when merged from both, rather than deleted, so
    how many times the ids occur and in what order stays visible (T4, T5): a varying count or
    sequence changes the remainder. NUL characters already in the value are doubled first, so
    no value can forge a placeholder. The result never depends on removal order, id lengths or
    set iteration order (PYTHONHASHSEED). Non-string values are folded from their canonical
    JSON, so ids nested in lists or mappings are masked too.

    Cost: because the token records which ids a span covers, a population in which the family
    id sits inside SOME case ids (``\\x00m``) but not others (``\\x00c``) is refused, even when
    each reference is honest. Use one id scheme for the whole population. Not visible: the
    number of overlapping repeats of one self-overlapping id (``aaaaaaaaa`` and ``aaaaaaaaaa``
    both mask to one span).
    """
    text = _value_text(value)
    spans: list[tuple[int, int, str]] = []
    for own, kind in sorted({(_fold(case.case_id), "c"), (_fold(case.family_id), "f")}):
        start = text.find(own)
        while start != -1:
            spans.append((start, start + len(own), kind))
            start = text.find(own, start + 1)
    merged: list[list[Any]] = []
    for begin, end, kind in sorted(spans):
        if merged and begin < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2].add(kind)
        else:
            merged.append([begin, end, {kind}])
    kept, cursor = [], 0
    for begin, end, kinds in merged:
        kept.append(text[cursor:begin].replace("\x00", "\x00\x00"))
        kept.append("\x00" + (kinds.pop() if len(kinds) == 1 else "m"))
        cursor = end
    kept.append(text[cursor:].replace("\x00", "\x00\x00"))
    return "".join(kept)


def _check_id_format(cases: Sequence[DecisionCase]) -> None:
    """Refuse ids too short or plain to tell from content (``case_id_format_unverifiable``).

    The documented id format: random or opaque, for example a UUID or a hash; not purely
    numeric; at least ``MIN_ID_LENGTH`` characters. Only the length and the not-numeric parts
    are enforced. Short or numeric ids ("1", "200", "en", "ref") occur inside honest decision
    text, so the own-id checks could neither trust nor hold them fairly; they are refused.

    Consequences of ids that are NOT opaque, which pass this check:
      * dictionary-word ids (a family called ``shipping`` or ``customer``) appear in honest
        text, and every such case holds the cohort as ``undeclared_reference_field``;
      * a benchmark whose decision text cites its own ticket id (``TCK-000042``) cannot be
        scored today: undeclared, the text carries its own id and holds; declared a reference,
        its id-free remainder is not constant and is refused
        (``reference_field_carries_content``), and if it were constant no decision content would
        remain (``decision_content_unverifiable``).
    Use random ids that never appear in the decision content.
    """
    for c in cases:
        for i in (c.case_id, c.family_id):
            if len(i) < MIN_ID_LENGTH or i.isdigit():
                raise VerdictInputError("case_id_format_unverifiable")


def _check_references_carry_ids(
    cases: Sequence[DecisionCase], reference_fields: Sequence[str]
) -> None:
    """Refuse a declared reference whose value does not contain its case's own id (G7, G8).

    Declaring a field a reference removes it from the content, so declaring real content (a
    decision text, a locale) as a reference would collapse the cohort's content. A declared
    reference must therefore carry its case's own case id or family id, in every case
    (``reference_field_carries_no_id``). ``_check_references_carry_no_content`` then bounds
    what else it may carry.
    """
    for c in cases:
        for name in reference_fields:
            if not _contains_own_id(c, c.features[name]):
                raise VerdictInputError("reference_field_carries_no_id")


def _check_references_carry_no_content(
    cases: Sequence[DecisionCase], reference_fields: Sequence[str]
) -> None:
    """Refuse a declared reference that also carries varying content (J1c, J1d).

    Carrying the own id is not enough: ``<case id> + first 10 chars of the text`` carries the id
    and smuggles content out of the comparison. So, for each declared reference, the value with
    every occurrence of its case's own ids replaced by a typed placeholder
    (``_id_free_remainder``) must be identical across all cases: only a constant decoration such
    as ``ref:`` or ``.json`` may surround the ids. Otherwise ``reference_field_carries_content``.

    What this enforces, precisely: across all cases, a declared reference has the same
    decoration and the same sequence of own-id occurrences (which id, how many times, in which
    order, and whether case and family ids overlap). Its only per-case variation is the
    identity of the ids themselves, and ids are producer-chosen (see D5). It does not stop a
    producer from editing or splitting the decision text itself (moving a constant prefix out of
    the text, or splitting one value into two undeclared fields): that is new content, residual
    D5.
    """
    for name in reference_fields:
        remainders = {_id_free_remainder(c, c.features[name]) for c in cases}
        if len(remainders) > 1:
            raise VerdictInputError("reference_field_carries_content")


def _check_content_not_collapsed(
    cases: Sequence[DecisionCase], reference_fields: Sequence[str]
) -> None:
    """Refuse a population whose id-free content collapses (``decision_content_collapsed``).

    Exactly what is refused: among the cases that have id-free content (``n`` of them, with
    ``d`` distinct digests), the population is refused only when ``n > 1`` and either every case
    shares one digest (``d == 1``) or fewer than half are distinct (``2 * d < n``). So exactly
    50% distinct is accepted, 49.5% is refused, and one duplicate pair among 200 cases is
    accepted. A mostly templated honest benchmark (60% or more identical inputs) has no route
    today: it is refused, not compared. Where that threshold should sit is an owner decision.
    """
    digests = [decision_content_digest(c, reference_fields) for c in cases]
    present = [d for d in digests if d is not None]
    distinct = len(set(present))
    if len(present) > 1 and (distinct == 1 or distinct * 2 < len(present)):
        raise VerdictInputError("decision_content_collapsed")


def _check_schema(cases: Sequence[DecisionCase], reference_fields: Sequence[str]) -> None:
    """Refuse a feature schema whose reference declarations cannot be trusted by name (D3, D3b).

    Every feature key and declared reference name must be an ASCII identifier (no look-alike
    letters: ``feature_key_unverifiable``); every declared name must be, exactly, a feature key of
    every case (``reference_field_undeclared_in_schema``); and no two names may collide after
    NFKC and casefold, such as ``case_ref`` beside ``Case_Ref`` (``feature_key_collision``).
    Anything else is refused, never silently matched or dropped.
    """
    keys = {k for c in cases for k in c.features}
    names = keys | set(reference_fields)
    if not all(isinstance(k, str) and _ASCII_KEY.fullmatch(k) for k in names):
        raise VerdictInputError("feature_key_unverifiable")
    for name in reference_fields:
        if any(name not in c.features for c in cases):
            raise VerdictInputError("reference_field_undeclared_in_schema")
    folded: dict[str, str] = {}
    for name in sorted(names):
        if folded.setdefault(_fold(name), name) != name:
            raise VerdictInputError("feature_key_collision")


def carries_own_id(case: DecisionCase, reference_fields: Sequence[str]) -> bool:
    """True when an undeclared feature value contains this case's own case or family id.

    Compared by exact substring on NFKC-casefolded text; a non-string value is scanned as its
    canonical JSON, so an id nested in a list or mapping is found too. Such a value is an
    undeclared reference (D2, E1). It is never dropped from the content, which would reopen A2:
    the population is held instead (``undeclared_reference_field``), before any inference. With
    the documented id format this rarely fires on honest text; a text that cites its own id
    should be declared a reference (see ``_check_references_carry_ids``).
    """
    return any(
        _contains_own_id(case, value)
        for name, value in case.features.items()
        if name not in reference_fields
    )


def decision_content_digest(case: DecisionCase, reference_fields: Sequence[str] = ()) -> str | None:
    """The id-free content of one decision-time input, for the fresh-cohort register.

    Left out: the case and family ids, the benchmark schema's declared reference fields (by exact
    field name, ``SealedPopulation.reference_fields``), every feature name (so renaming,
    re-casing or re-spacing a name changes nothing) and feature order. String values lose only
    insignificant surrounding whitespace; their internal content, case included, is kept as is.
    Returns ``None`` when no content value remains: such a case is ``decision_content_unverifiable``
    and can never count as fresh.

    Probed bypasses, now closed:
      renaming case/family ids or feature names, or reordering features;
      A2 renaming each id to a substring of its own decision text, which defeated the earlier
         rule of dropping any value containing the id. Nothing is excluded by what a value
         contains, only by declared field name;
      A3 adding surrounding whitespace to a value;
      D2/E1 leaving an id-carrying field undeclared: the case is held as
         ``undeclared_reference_field`` (``carries_own_id``), its value never dropped;
      D3/D3b declaring a reference name that differs from the key by case or by a look-alike
         letter: refused when the population is built (``_check_schema``).
    These hold only while reference fields are declared honestly enough to pass those checks.
    Residual, NOT closed here, because the producer controls the population:
      A1 adding a field with a fresh per-case value that is not an id (such as a hash of it);
      D5 any edit to the decision text itself, which is new content and so a fresh cohort.
    Ids are producer-chosen, up to 128 characters, and can carry content the register never
    sees (ids and declared references are masked out of the content). Only an authenticated
    OPE-526 audit receipt binding the benchmark content can close these; here the receipt is
    self-sealed (see also C1 in the module docstring).
    """
    excluded = set(reference_fields)
    kept = [
        v.strip() if isinstance(v, str) else v
        for k, v in case.features.items()
        if k not in excluded
    ]
    values = sorted(json.dumps(v, sort_keys=True, ensure_ascii=False) for v in kept if v != "")
    if not values:  # nothing but declared references or blank strings: no content to compare
        return None
    return digest(["opentour.pilot.decision-content.v1", values])


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
        if self.candidate_kind not in (*_KINDS, *_PROVIDER_ROUTES):
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
class ProviderJobRecord:
    """A managed provider's or customer trainer's own statement about one fine-tuning job.

    Built by the caller from the connector's records (``managed_connector.ProviderJob`` plus the
    provider's reported base model, or ``custom_trainer_connector.TrainerResult``). Nothing here
    is authenticated: real fine-tuning APIs do not sign their job objects, and a trainer's shared
    secret HMAC is checked by its connector, not by this evaluator. ``parent_revision`` must be
    the parent the provider or trainer itself reports, not one copied from the request.
    ``artifact_digest`` is the trainer's reported digest when it gives one; a managed provider's
    weights are opaque and it gives none.
    """

    route: str
    job_id: str
    client_token: str
    intent_sha256: str
    state: str
    parent_revision: str
    candidate_id: str
    artifact_digest: str | None = None
    reporter_version: str | None = None

    def well_formed(self) -> bool:
        ids = (self.job_id, self.client_token, self.parent_revision, self.candidate_id)
        return (
            self.route in _JOB_DONE
            and all(isinstance(v, str) and _ID.fullmatch(v) for v in ids)
            and _is_sha(self.intent_sha256)
            and isinstance(self.state, str)
            and (self.artifact_digest is None or _is_sha(self.artifact_digest))
            and (
                self.reporter_version is None
                or (
                    isinstance(self.reporter_version, str)
                    and _ID.fullmatch(self.reporter_version) is not None
                )
            )
        )

    def as_json(self) -> Json:
        return {
            "route": self.route,
            "job_id": self.job_id,
            "client_token": self.client_token,
            "intent_sha256": self.intent_sha256,
            "state": self.state,
            "parent_revision": self.parent_revision,
            "candidate_id": self.candidate_id,
            "artifact_digest": self.artifact_digest,
            "reporter_version": self.reporter_version,
        }

    def digest(self) -> str:
        """The job binding a claim names in ``job_manifest_digest``."""
        return digest(["opentour.pilot.provider-job-record.v1", self.as_json()])


@dataclass(frozen=True)
class ProviderEvidence:
    """A caller-supplied expectation for a provider route, plus the job record it got.

    Every field here is supplied by the caller, so a match proves agreement with what the caller
    supplied, nothing stronger; labels say ``matches_supplied_expectation`` /
    ``checked_against_supplied_expectation``, never "verified" or "evaluator-held".
    ``parent_model_id`` and ``candidate_id`` are meant to come from the frozen experiment, not
    from the provider; the job record and the serving readback must both agree with them.

    ``expected_intent_sha256`` and ``expected_client_token`` are the supplied fit intent digest
    and client token, when the caller has them. Nothing supplies them today, so they default to
    ``None``: the job record's intent and token are then recorded as
    ``unchecked_no_expectation_supplied``, never as bound.
    """

    job: ProviderJobRecord | None
    parent_model_id: str
    candidate_id: str
    expected_intent_sha256: str | None = None
    expected_client_token: str | None = None


def provider_candidate_digest(route: str, parent_model_id: str, candidate_id: str) -> str:
    """The claim's ``candidate_digest`` for a provider route: route, parent and immutable id."""
    return digest(["opentour.pilot.provider-candidate.v1", route, parent_model_id, candidate_id])


def _mutable_alias(identifier: object) -> bool:
    """FORMAT ONLY: an id that is, or ends in, a moving-pointer name. Not proof of immutability."""
    if not isinstance(identifier, str):
        return True
    lowered = identifier.lower()
    return lowered in _ALIASES or re.split(r"[:@/]", lowered)[-1] in _ALIASES


@dataclass(frozen=True)
class IndependentVerdict:
    document: Json
    signature: Json | None
    replayed: bool
    # ``locally_keyed`` only when the evaluator re-read this verdict from its own store and
    # checked the HMAC seal; otherwise the document's own digest tier. Never in the document.
    custody_tier: str = EVIDENCE_TIER

    @property
    def verdict_key(self) -> str:
        return str(self.document["verdict_key"])

    @property
    def outcome(self) -> str:
        return str(self.document["outcome"])


@dataclass(frozen=True)
class CorrectionNotice:
    """The single correction notice for a revoked verdict whose watch had already fired."""

    document: Json
    replayed: bool


@dataclass(frozen=True)
class Revocation:
    """An append-only, sealed revocation of one verdict key, and its notice if one is owed."""

    record: Json
    notice: CorrectionNotice | None
    replayed: bool


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
        for sub in (
            "",
            "verdicts",
            "predictions",
            "cohorts",
            "expired",
            "revocations",
            "revoked_scopes",
            "notices",
        ):
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
        return self._load_stored(path, key)

    def _load_stored(self, path: Path, key: str) -> IndependentVerdict:
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
        return IndependentVerdict(document, signature, replayed=True, custody_tier=STORE_TIER)

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

    # ---- correction and revocation

    def _generations(self, key: str) -> list[Json]:
        """Every sealed verdict issued under ``key``: the live one and any archived ones."""
        documents = []
        live = self._load_verdict(key)
        if live is not None:
            documents.append(live.document)
        for path in sorted((self._root / "expired").glob(f"verdicts-{key}-*.json")):
            documents.append(self._load_stored(path, key).document)
        return documents

    def _load_revocation(self, key: str) -> Json | None:
        path = self._root / "revocations" / (key + ".json")
        if not path.exists():
            return None
        try:
            stored = json.loads(path.read_bytes())
            record = stored["record"]
            body = {k: v for k, v in record.items() if k != "revocation_digest"}
            sealed = record["revocation_digest"] == digest(body)
            keyed = hmac.compare_digest(stored["local_seal"], self._commit({"record": record}))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise CustodyError("stored_revocation_corrupt") from None
        if not (
            sealed
            and keyed
            and record.get("schema_version") == REVOCATION
            and record.get("verdict_key") == key
        ):
            raise CustodyError("stored_revocation_tampered")
        return dict(record)

    def _load_notice(self, key: str, applicable: list[Json]) -> Json | None:
        path = self._root / "notices" / (key + ".json")
        if not path.exists():
            return None
        try:
            stored = json.loads(path.read_bytes())
            notice = stored["notice"]
            body = {k: v for k, v in notice.items() if k != "notice_digest"}
            sealed = notice["notice_digest"] == digest(body)
            keyed = hmac.compare_digest(stored["local_seal"], self._commit({"notice": notice}))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise CustodyError("stored_correction_notice_corrupt") from None
        if not (
            sealed
            and keyed
            and notice.get("schema_version") == CORRECTION_NOTICE
            and notice.get("verdict_key") == key
            # Bound to a revocation that applies to this key (a later one never unbinds it).
            and notice.get("revocation_digest") in {r["revocation_digest"] for r in applicable}
        ):
            raise CustodyError("stored_correction_notice_tampered")
        return dict(notice)

    def revoke_verdict(
        self, key: str, *, reason: str, issued_by: str, watch_fired: bool = False
    ) -> Revocation:
        """Revoke every verdict issued under ``key``. Append-only, sealed and idempotent.

        The first revocation of a key is final: a repeat (a retry, a restart, a second caller)
        returns that original record unchanged, whatever reason it names. A revocation is scoped
        to the key, not to one issued document, so an expired verdict, its archived generations
        and any later re-issue under the same key all read as revoked; ``evaluate`` never
        re-scores a revoked key. A corrected claim is a different claim and so a different key.

        ``issued_by`` is a caller-asserted identity label, not authentication (no evaluator key
        exists yet, OPE-566); a revocation only removes authority, so it fails closed.
        ``watch_fired`` is the consumer's statement that a watch already fired on this verdict;
        when true, exactly one correction notice exists for the key afterwards (see
        ``correction_notice``).
        """
        if not _is_sha(key):
            raise VerdictInputError("invalid_verdict_key")
        if reason not in REVOCATION_REASONS:
            raise VerdictInputError("invalid_revocation_reason")
        if not isinstance(issued_by, str) or not _ID.fullmatch(issued_by):
            raise VerdictInputError("invalid_revocation_issuer")
        if type(watch_fired) is not bool:
            raise VerdictInputError("invalid_watch_fired_flag")
        record = self._load_revocation(key)
        replayed = record is not None
        if record is None:
            generations = self._generations(key)
            if not generations:
                # Nothing issued here under this key: there is no authority to revoke, and a
                # record bound to no verdict would be unverifiable. Nothing is stored.
                raise VerdictInputError("revocation_target_unknown")
            body: Json = {
                "schema_version": REVOCATION,
                "verdict_key": key,
                "revoked_verdict_digests": sorted({d["verdict_digest"] for d in generations}),
                "reason": reason,
                "issued_by": issued_by,
                "issued_by_authenticated": False,
                "evaluator_id": self.evaluator_id,
                "revoked_at": int(time.time()),
                "effect": "invalidates_outstanding_authority",
                "evidence_tier": EVIDENCE_TIER,
            }
            sealed = {**body, "revocation_digest": digest(body)}
            stored = {"record": sealed, "local_seal": self._commit({"record": sealed})}
            path = self._root / "revocations" / (key + ".json")
            replayed = not _write_once(path, json.dumps(stored, sort_keys=True).encode())
            record = self._load_revocation(key)  # a lost race returns the first record
            assert record is not None
        notice = self.correction_notice(key, watch_fired=watch_fired)
        return Revocation(record, notice, replayed)

    @staticmethod
    def _scope_file(scope: str, value: str) -> str:
        return digest([SCOPE_REVOCATION, scope, value]) + ".json"

    def _scope_records(self) -> list[Json]:
        """Every sealed scope revocation in this custody, each checked like a key revocation."""
        records = []
        for path in sorted((self._root / "revoked_scopes").glob("*.json")):
            try:
                stored = json.loads(path.read_bytes())
                record = stored["record"]
                body = {k: v for k, v in record.items() if k != "revocation_digest"}
                sealed = record["revocation_digest"] == digest(body)
                keyed = hmac.compare_digest(stored["local_seal"], self._commit({"record": record}))
                named = path.name == self._scope_file(record["scope"], record["scope_value"])
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                raise CustodyError("stored_scope_revocation_corrupt") from None
            if not (
                sealed
                and keyed
                and named
                and record.get("schema_version") == SCOPE_REVOCATION
                and record.get("scope") in REVOCATION_SCOPES
            ):
                raise CustodyError("stored_scope_revocation_tampered")
            records.append(dict(record))
        return records

    @staticmethod
    def _scope_values(document: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "scorer_version": document["scorer"]["scorer_version"],
            "answer_key_digest": document["answer_key_digest"],
            "audit_receipt_digest": document["audit_receipt_digest"],
            "answer_key_content_digest": document.get("answer_key_content_digest"),
        }

    @staticmethod
    def _scope_matches(record: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
        if values.get(record["scope"]) == record["scope_value"]:
            return True
        # An answer-key revocation also follows the key's id-free content (re-review C2), so
        # renaming every case id does not escape it.
        content = values.get("answer_key_content_digest")
        return (
            record["scope"] == "answer_key_digest"
            and content is not None
            and content in record.get("answer_key_content_digests", [])
        )

    def _scope_hits(self, values: Mapping[str, Any]) -> list[Json]:
        """Scope revocations matching any of these verdict values, in a fixed order."""
        hits = [r for r in self._scope_records() if self._scope_matches(r, values)]
        return sorted(hits, key=lambda r: str(r["revocation_digest"]))

    def _all_documents(self) -> list[Json]:
        """Every sealed verdict this custody issued: live and archived generations."""
        documents = []
        for path in sorted((self._root / "verdicts").glob("*.json")):
            documents.append(self._load_stored(path, path.stem).document)
        for path in sorted((self._root / "expired").glob("verdicts-*.json")):
            documents.append(self._load_stored(path, path.name[len("verdicts-") :][:64]).document)
        return documents

    def _governing(self, key: str) -> tuple[Json | None, list[Json]]:
        """The key revocation (if any) and every scope revocation matching a verdict of ``key``."""
        hits: dict[str, Json] = {}
        for document in self._generations(key):
            for record in self._scope_hits(self._scope_values(document)):
                hits[record["revocation_digest"]] = record
        return self._load_revocation(key), sorted(
            hits.values(), key=lambda r: str(r["revocation_digest"])
        )

    def revoke_scope(self, scope: str, value: str, *, reason: str, issued_by: str) -> Revocation:
        """Revoke every verdict, issued or future, whose ``scope`` equals ``value``.

        ``scope`` is one of ``REVOCATION_SCOPES``: a scorer version (a scorer defect invalidates
        every verdict it scored, not one key), an answer key digest, an audit receipt digest, or
        an answer key's id-free content digest (``SealedPopulation.answer_key_content_digest``).
        For the first three, the value must be one this custody has actually scored a verdict
        with; an unknown value would match nothing, so it is refused with
        ``revocation_scope_unknown`` rather than reported as a success. Trade-off: those three
        can no longer be revoked pre-emptively, before any verdict carries the value. The
        content digest is the exception: a revoker can compute it from a defective key itself,
        so it may be revoked before any verdict here uses that key (``matched_verdicts`` may be
        0). The record states how many verdicts it matched when written. It is sealed,
        write-once and idempotent like a key revocation, and it also refuses any later verdict
        with the same value. From then on ``authorize`` refuses a matching verdict with
        ``verdict_revoked_by_scope``, ``read_verdict`` reads it as revoked and ``evaluate``
        never scores a matching claim. ``issued_by`` is caller-asserted.

        An answer-key revocation also records the id-free content digests of the keys it hit
        (``answer_key_content_digests``) and matches on them, so renaming every case id does not
        escape it (re-review C2), while reference fields are declared honestly. Known limits:
        relabelling the same partition (swapping label names consistently) changes the content
        digest and escapes this match; the fresh-cohort gate is the backstop there. An
        audit-receipt revocation matches the receipt digest only (C1): the OPE-526 receipt is
        self-sealed, so a re-minted equivalent receipt escapes it. Only an authenticated audit
        issuer closes C1.
        """
        if scope not in REVOCATION_SCOPES:
            raise VerdictInputError("invalid_revocation_scope")
        well_formed = (
            isinstance(value, str) and _ID.fullmatch(value) is not None
            if scope == "scorer_version"
            else _is_sha(value)
        )
        if not well_formed:
            raise VerdictInputError("invalid_revocation_scope_value")
        if reason not in REVOCATION_REASONS:
            raise VerdictInputError("invalid_revocation_reason")
        if not isinstance(issued_by, str) or not _ID.fullmatch(issued_by):
            raise VerdictInputError("invalid_revocation_issuer")
        path = self._root / "revoked_scopes" / self._scope_file(scope, value)
        replayed = path.exists()
        if not replayed:
            matched = [d for d in self._all_documents() if self._scope_values(d)[scope] == value]
            if not matched and scope not in _PREEMPTIVE_SCOPES:
                raise VerdictInputError("revocation_scope_unknown")
            body: Json = {
                "schema_version": SCOPE_REVOCATION,
                "scope": scope,
                "scope_value": value,
                "matched_verdicts": len({d["verdict_key"] for d in matched}),
                "answer_key_content_digests": sorted(
                    {
                        d["answer_key_content_digest"]
                        for d in matched
                        if d.get("answer_key_content_digest") is not None
                    }
                )
                if scope == "answer_key_digest"
                else [],
                "reason": reason,
                "issued_by": issued_by,
                "issued_by_authenticated": False,
                "evaluator_id": self.evaluator_id,
                "revoked_at": int(time.time()),
                "effect": "invalidates_outstanding_authority",
                "evidence_tier": EVIDENCE_TIER,
            }
            sealed = {**body, "revocation_digest": digest(body)}
            stored = {"record": sealed, "local_seal": self._commit({"record": sealed})}
            replayed = not _write_once(path, json.dumps(stored, sort_keys=True).encode())
        records = [
            r for r in self._scope_records() if (r["scope"], r["scope_value"]) == (scope, value)
        ]
        assert len(records) == 1  # a lost race returns the first record
        return Revocation(records[0], None, replayed)

    def correction_notice(self, key: str, *, watch_fired: bool) -> CorrectionNotice | None:
        """The one correction notice for a revoked verdict whose watch already fired.

        Exactly one notice record ever exists per revoked key: it is created write-once under a
        deterministic id, so a retry, a restart or a concurrent caller gets the same record back
        (``replayed``). With ``watch_fired`` false and no notice yet, nothing is created and
        ``None`` is returned: no watch fired, so no correction is owed. Delivering the notice
        exactly once (Slack, OPE-524) is the consumer's job, keyed by ``notice_id``.
        """
        if not _is_sha(key):
            raise VerdictInputError("invalid_verdict_key")
        if type(watch_fired) is not bool:
            raise VerdictInputError("invalid_watch_fired_flag")
        key_record, scope_records = self._governing(key)
        applicable = ([key_record] if key_record is not None else []) + scope_records
        if not applicable:
            raise VerdictInputError("verdict_not_revoked")
        existing = self._load_notice(key, applicable)
        if existing is not None:
            return CorrectionNotice(existing, replayed=True)
        if not watch_fired:
            return None
        # One notice per verdict key, whichever revocation (key or scope) reached it first.
        revocation = applicable[0]
        body: Json = {
            "schema_version": CORRECTION_NOTICE,
            "notice_id": "correction-" + digest([CORRECTION_NOTICE, key])[:32],
            "verdict_key": key,
            "revocation_digest": revocation["revocation_digest"],
            "revoked_scope": (
                {"scope": revocation["scope"], "value": revocation["scope_value"]}
                if revocation["schema_version"] == SCOPE_REVOCATION
                else None
            ),
            "revoked_verdict_digests": sorted(
                {d["verdict_digest"] for d in self._generations(key)}
            ),
            "reason": revocation["reason"],
            "evaluator_id": self.evaluator_id,
            "created_at": int(time.time()),
        }
        sealed = {**body, "notice_digest": digest(body)}
        stored = {"notice": sealed, "local_seal": self._commit({"notice": sealed})}
        path = self._root / "notices" / (key + ".json")
        created = _write_once(path, json.dumps(stored, sort_keys=True).encode())
        notice = self._load_notice(key, applicable)
        assert notice is not None
        return CorrectionNotice(notice, replayed=not created)

    def _held_view(self, key: str, state: str, reasons: list[str], extra: Json) -> Json:
        """A verdict that must not be acted on. It never carries the original outcome."""
        return {
            "schema_version": HELD_VIEW,
            "verdict_key": key,
            "outcome": "stale",
            "state": state,
            "verdict": "not_reproduced",
            "reasons": reasons,
            "effects": dict(_NO_EFFECTS),
            "verifier_id": self.evaluator_id,
            **extra,
        }

    def _revoked_view(self, revocation: Json) -> Json:
        return self._held_view(
            revocation["verdict_key"],
            "revoked",
            ["revocation_reason_" + revocation["reason"], "verdict_revoked"],
            {
                "revocation_digest": revocation["revocation_digest"],
                "revoked_verdict_digests": revocation["revoked_verdict_digests"],
            },
        )

    def _scope_revoked_view(self, key: str, revocation: Json) -> Json:
        return self._held_view(
            key,
            "revoked",
            [
                "revocation_reason_" + revocation["reason"],
                "verdict_revoked_by_scope_" + revocation["scope"],
            ],
            {
                "revocation_digest": revocation["revocation_digest"],
                "revoked_scope": {"scope": revocation["scope"], "value": revocation["scope_value"]},
            },
        )

    def read_verdict(self, key: str) -> Json:
        """What a consumer reads for ``key``: the issued document only while it is current.

        A revoked key reads as ``stale`` with state ``revoked``; an expired one as ``stale`` with
        state ``expired``. Neither view carries the original outcome or any eligible effect, and
        neither has the verdict schema, so the OPE-421 packet boundary refuses it.
        """
        if not _is_sha(key):
            raise VerdictInputError("invalid_verdict_key")
        revocation = self._load_revocation(key)
        if revocation is not None:
            return self._revoked_view(revocation)
        live = self._load_verdict(key)
        if live is None:
            raise VerdictInputError("verdict_unknown")
        scoped = self._scope_hits(self._scope_values(live.document))
        if scoped:
            return self._scope_revoked_view(key, scoped[0])
        if int(time.time()) > live.document["not_after"]:
            return self._held_view(
                key,
                "expired",
                ["verdict_expired"],
                {"verdict_digest": live.document["verdict_digest"]},
            )
        return live.document

    def authorize(self, document: Mapping[str, Any], *, effect: str) -> Json:
        """The consumer check: may ``effect`` act on this verdict document now?

        Every consumer (eligibility, watch, report, release gate) calls this with the document it
        holds before acting. It refuses a revoked key, an expired verdict, a document that is not
        the one stored under its key (including an older archived generation), and an effect the
        verdict did not make eligible. Any effect on a ``verified_gain`` also refuses with
        ``policy_floor_unset`` while no D11 floor is registered (``REGISTERED_POLICY_FLOOR``),
        and with ``policy_below_registered_floor`` when the verdict's policy is laxer than it.
        There is deliberately no argument for a policy or a floor here. Custody tampering raises
        ``CustodyError``.
        """
        if effect not in _EFFECT_FLAGS:
            raise VerdictInputError("unknown_consumer_effect")

        def refuse(reason: str, state: str) -> Json:
            return {"authorized": False, "effect": effect, "reason": reason, "state": state}

        if (
            not isinstance(document, Mapping)
            or document.get("schema_version") != VERDICT
            or not _is_sha(document.get("verdict_key"))
        ):
            return refuse("verdict_unverified", "unverified")
        key = str(document["verdict_key"])
        if self._load_revocation(key) is not None:
            return refuse("verdict_revoked", "revoked")
        stored = self._load_verdict(key)
        if stored is None:
            return refuse("verdict_unknown", "unknown")
        if self._scope_hits(self._scope_values(stored.document)):
            return refuse("verdict_revoked_by_scope", "revoked")
        if stored.document != dict(document):
            return refuse("verdict_document_mismatch", "unverified")
        if int(time.time()) > stored.document["not_after"]:
            return refuse("verdict_expired", "expired")
        held_doc = stored.document
        if held_doc["outcome"] == "verified_gain":
            # D11: no consumer acts on a gain until an owner-registered floor exists and the
            # verdict's bound policy meets it. Read from this module, never from the caller.
            floor = policy_floor_status(held_doc["policy"])
            if floor != "satisfied":
                return refuse(
                    "policy_floor_unset" if floor == "unset" else "policy_below_registered_floor",
                    stored.outcome,
                )
        if effect == "improvement_alert":
            eligible = held_doc.get("alert_gates", {}).get("cleared")
        else:
            eligible = held_doc["effects"].get(_EFFECT_FLAGS[effect])
        if eligible is not True:
            return refuse("effect_not_eligible", stored.outcome)
        return {
            "authorized": True,
            "effect": effect,
            "reason": "verdict_current_and_eligible",
            "state": stored.outcome,
        }

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
        provider_evidence: ProviderEvidence | None = None,
    ) -> IndependentVerdict:
        """Verify and score one claim. A provider route takes ``provider_evidence``.

        Everything after the job and serving statements (replay, scoring, every gate, issuing)
        is the same code for every route.
        """
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
        revocation = self._load_revocation(key)
        if revocation is not None:
            # A revoked key is final: never re-scored, never re-issued, never its original state.
            return IndependentVerdict(self._revoked_view(revocation), None, replayed=True)
        scoped = self._scope_hits(
            {
                "scorer_version": self.scorer.scorer_version,
                "answer_key_digest": population.answer_key_digest,
                "answer_key_content_digest": population.answer_key_content_digest,
                "audit_receipt_digest": audit_receipt.get("receipt_digest"),
            }
        )
        if scoped:
            # A revoked scorer version, answer key or audit receipt never scores another claim.
            view = self._scope_revoked_view(key, scoped[0])
            return IndependentVerdict(view, None, replayed=True)
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
        job = self._job_statement(claim, fit_manifest, provider_evidence)
        if job["status"] == "failed":
            reasons.add("integrity_failure", "job_" + job["reason"])
        served, identities = self._served_statement(claim, policy, replays, job, provider_evidence)
        if served["status"] == "failed":
            reasons.add("integrity_failure", "served_" + served["reason"])

        comparisons: Json = {}
        record: Json = {"rows": [], "predictions": {}}
        gate: Json = {"status": "not_evaluated", "reason": "integrity_or_staleness_hold"}
        # Populations that cannot be compared were decided when they were built; they hold here,
        # before any arm is replayed (only the served-identity readbacks above have run).
        population_holds = [
            reason
            for reason, flag in (
                # An undeclared field carries a case's own id (D2, E1).
                ("undeclared_reference_field", population.undeclared_reference),
                # Some case has no id-free content to compare (J1a).
                ("decision_content_unverifiable", population.content_unverifiable),
            )
            if flag
        ]
        for reason in population_holds:
            reasons.add("inconclusive", reason)
        if population_holds:
            gate = {"status": "not_evaluated", "reason": ",".join(population_holds)}
        elif not reasons.has("integrity_failure", "stale"):
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
                if reasons.outcome() == "verified_gain":
                    # A gain the worker never stated is not a reproduced claim, so it is never
                    # emitted as verified_gain beside verdict "not_reproduced".
                    for name in policy.required_comparators:
                        if name not in claim.claimed_paired:
                            reasons.add("inconclusive", "claimed_counts_not_stated_" + name)
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

    def _job_statement(
        self,
        claim: ClaimedResult,
        manifest: Mapping[str, Any] | None,
        provider: ProviderEvidence | None = None,
    ) -> Json:
        route = _PROVIDER_ROUTES.get(claim.candidate_kind)
        if route is not None:
            if manifest is not None:
                return {"status": "failed", "reason": "fit_manifest_on_a_provider_route"}
            return self._provider_job_statement(claim, route, provider)
        if provider is not None:
            return {"status": "failed", "reason": "provider_evidence_on_a_non_provider_route"}
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

    def _provider_job_statement(
        self, claim: ClaimedResult, route: str, provider: ProviderEvidence | None
    ) -> Json:
        """Bind a provider or trainer job record to the frozen parent and immutable candidate.

        The status is ``provider_reported`` or ``trainer_reported``, never "verified": the record
        is the provider's own unauthenticated statement. Weights are opaque and recorded as
        provider-attested; no tensor-level evidence is claimed or invented.
        """

        def failed(reason: str) -> Json:
            return {"status": "failed", "reason": reason}

        if provider is None or provider.job is None or claim.job_manifest_digest is None:
            return failed("job_binding_missing")
        job = provider.job
        if not job.well_formed():
            return failed("provider_job_record_malformed")
        if (
            not all(
                isinstance(v, str) and _ID.fullmatch(v)
                for v in (provider.parent_model_id, provider.candidate_id)
            )
            or (
                provider.expected_intent_sha256 is not None
                and not _is_sha(provider.expected_intent_sha256)
            )
            or (
                provider.expected_client_token is not None
                and not (
                    isinstance(provider.expected_client_token, str)
                    and _ID.fullmatch(provider.expected_client_token)
                )
            )
        ):
            return failed("provider_expectation_malformed")
        if job.route != route:
            return failed("job_route_mismatch")
        if route == "custom_trainer" and job.artifact_digest is None:
            # A trainer reports its artifact digest; without it nothing names the revision.
            return failed("trainer_artifact_digest_missing")
        if job.state != _JOB_DONE[route]:
            return failed("job_not_completed")
        if job.digest() != claim.job_manifest_digest:
            return failed("designated_job_mismatch")
        held = (provider.expected_intent_sha256, provider.expected_client_token)
        if held[0] is not None and job.intent_sha256 != held[0]:
            return failed("job_intent_mismatch")
        if held[1] is not None and job.client_token != held[1]:
            return failed("job_client_token_mismatch")
        intent_binding = (
            "checked_against_supplied_expectation"
            if held[0] is not None and held[1] is not None
            else "partially_checked_against_supplied_expectation"
            if held != (None, None)
            else "unchecked_no_expectation_supplied"
        )
        if _mutable_alias(provider.parent_model_id) or _mutable_alias(job.parent_revision):
            return failed("parent_mutable_alias")
        if job.parent_revision != provider.parent_model_id:
            return failed("parent_mismatch")
        if _mutable_alias(provider.candidate_id) or _mutable_alias(job.candidate_id):
            return failed("candidate_id_mutable_alias")
        if job.candidate_id != provider.candidate_id:
            return failed("job_candidate_is_not_the_frozen_candidate")
        candidate = provider_candidate_digest(route, provider.parent_model_id, job.candidate_id)
        if candidate != claim.candidate_digest:
            return failed("job_artifact_is_not_the_candidate")
        return {
            "status": _JOB_REPORTER[route],
            "reason": "job_record_names_supplied_parent_and_candidate",
            "route": route,
            "job_id": job.job_id,
            "job_record_digest": job.digest(),
            "parent_revision": job.parent_revision,
            "candidate_id": job.candidate_id,
            "candidate_digest": candidate,
            "artifact_digest_reported": job.artifact_digest,
            # intent_sha256 and client_token are the record's own; bound only if checked here.
            "job_intent_binding": intent_binding,
            "weights": "provider_attested_opaque",
            "tensor_evidence": "not_available",
            "evidence": "unauthenticated_provider_statement",
        }

    def _provider_readback(
        self,
        claim: ClaimedResult,
        served: Mapping[str, Any],
        provider: ProviderEvidence | None,
        job: Json,
        drift: Json,
    ) -> str | None:
        """Check a provider serving readback names the evaluated revision. ``None`` if bound."""
        route = _PROVIDER_ROUTES[claim.candidate_kind]
        if provider is None:
            return "provider_expectation_missing"
        if served.get("route") != route:
            return "route_mismatch"
        served_id = served.get("served_model_id")
        kind = served.get("identity_kind")
        if not (isinstance(served_id, str) and _ID.fullmatch(served_id)):
            return "identity_unverifiable"
        if kind == "mutable_alias" or _mutable_alias(served_id):
            return "identity_mutable_alias"
        if kind != "immutable_model_id":
            return "identity_unverifiable"
        if served_id != provider.candidate_id:
            return "candidate_served_other_revision"
        if (
            provider_candidate_digest(route, provider.parent_model_id, served_id)
            != claim.candidate_digest
        ):
            return "candidate_served_other_artifact"
        if "parent_revision" in served:
            if served["parent_revision"] != provider.parent_model_id:
                return "parent_mismatch"
            # Both sides are statements: the provider's readback and the caller's expectation.
            drift["parent_revision"] = "matches_supplied_expectation"
        else:
            drift["parent_revision"] = "unavailable"
        reported = job.get("artifact_digest_reported")
        if route == "custom_trainer":
            # Fail closed: a trainer readback must name the artifact digest the job reported.
            if reported is None or "artifact_digest" not in served:
                return "trainer_artifact_digest_unavailable"
            if served["artifact_digest"] != reported:
                return "candidate_served_other_revision"
            drift["artifact_digest"] = "matches_job_record"
        elif "artifact_digest" in served and reported is not None:
            if served["artifact_digest"] != reported:
                return "candidate_served_other_revision"
            drift["artifact_digest"] = "matches_job_record"
        else:
            drift["artifact_digest"] = "unavailable"  # managed weights are opaque
        drift["served_model_id"] = "matches_supplied_expectation"
        # The provider's own claim that the id is immutable: required, never verified here,
        # and never a reason to raise any tier.
        drift["identity_kind"] = "provider_self_claim"
        return None

    def _served_statement(
        self,
        claim: ClaimedResult,
        policy: GatePolicy,
        replays: Mapping[str, ArtifactReplay],
        job: Json,
        provider: ProviderEvidence | None = None,
    ) -> tuple[Json, dict[str, str]]:
        expected = {"candidate": claim.candidate_digest, **claim.comparator_digests}
        identities: dict[str, str] = {}
        drift: Json = {}
        provider_route = claim.candidate_kind in _PROVIDER_ROUTES
        for name in ("candidate", *policy.required_comparators, *policy.optional_comparators):
            if name not in replays:
                continue
            if name not in expected:
                return {"status": "failed", "reason": "arm_without_frozen_revision"}, identities
            try:
                served = dict(replays[name].served_identity())
            except Exception:
                return {"status": "failed", "reason": "serving_readback_unavailable"}, identities
            if name == "candidate" and provider_route:
                # Opaque weights: the readback names the served model, never a tensor digest.
                refusal = self._provider_readback(claim, served, provider, job, drift)
                if refusal is not None:
                    return {"status": "failed", "reason": refusal}, identities
                identities[name] = claim.candidate_digest
                continue
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
        evidence = {"serving_evidence": "provider_reported_readback"} if provider_route else {}
        return {
            "status": "readback_consistent",
            "reason": "replay_readback_names_frozen_revisions",
            **evidence,
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
            s: {"comparable": 0, "fixes": 0, "new_errors": 0, "uncovered": 0}
            for s in policy.critical_slices
        }
        gaps = ("missing_output", "malformed_output")
        for case, c, t in zip(population.cases, comp, cand, strict=True):
            if _is_auto(c) and _is_auto(t):
                effect = (t == "correct_automatic") - (c == "correct_automatic")
            elif c == "deferred" and _is_auto(t):
                # Newly automated: no credit when right, a full error when wrong.
                effect = -1 if t == "wrong_automatic" else 0
            else:
                if c in gaps or t in gaps:
                    # A comparator gap or a candidate missing/malformed output still counts
                    # against the slice: it is an uncovered slice case, and a wrong automatic
                    # candidate decision on it is a slice error the veto must see.
                    for s in case.slices:
                        if s in slices:
                            slices[s]["uncovered"] += 1
                            slices[s]["new_errors"] += t == "wrong_automatic"
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
        # The slice's comparable share must meet the same coverage floor the comparison does
        # (no slice-specific share term is registered yet).
        floor = max(policy.coverage_floor_bps, policy.term("minimum_coverage_bps"))
        for v in slices.values():
            # The veto is checked first: slice errors regress even where support is thin.
            v["status"] = (
                "veto"
                if v["new_errors"] > v["fixes"]
                else "unsupported"
                if v["comparable"] < min_slice
                else "insufficient_comparable"
                if v["comparable"] * 10_000 < floor * (v["comparable"] + v["uncovered"])
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
        below zero) -> holds (missing outputs, comparator gaps, coverage, critical-slice
        comparable share, no ceiling) -> the
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
        if any(v["status"] == "insufficient_comparable" for v in slices.values()):
            return "inconclusive", "critical_slice_insufficient_comparable"
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

        Case and family ids are the producer's own names, so overlap is also checked on content:
        each case's ``decision_content_digest`` (its decision-time feature values, with feature
        names, ordering, declared reference fields and surrounding whitespace left out) is
        registered, and any overlap with a scored cohort holds as
        ``decision_content_seen_in_scored_cohort``. A population with any case that has no
        content left to compare holds as ``decision_content_unverifiable``: an empty digest can
        never count as fresh. See ``decision_content_digest`` for the bypasses this closes and
        the evasion class it cannot.

        This fails closed on purpose and has a cost: ANY second claim on an already scored
        cohort is held, including legitimately preregistered siblings (for example a
        ``simple_repair`` claim and an ``open_weight_fit`` claim frozen together on the same
        population in one custody directory). The second one scored is held as inconclusive. No
        registry of jointly preregistered claims exists to tell a sibling from a reselection,
        so siblings need separate cohorts or separate custody until one does.
        """
        cases = {c.case_id for c in population.cases}
        families = {c.family_id for c in population.cases}
        digests = [
            decision_content_digest(c, population.reference_fields) for c in population.cases
        ]
        # An unverifiable (empty-content) case never reaches here: it is held before replay.
        contents = {d for d in digests if d is not None}
        id_overlap = content_overlap = False
        for path in sorted((self._root / "cohorts").glob("*.json")):
            if path.name == key + ".json":
                continue
            try:
                seen = json.loads(path.read_bytes())
                id_overlap = id_overlap or bool(
                    cases & set(seen["cases"]) or families & set(seen["families"])
                )
                content_overlap = content_overlap or bool(contents & set(seen["contents"]))
            except (OSError, ValueError, KeyError, TypeError):
                raise CustodyError("cohort_register_corrupt") from None
        if id_overlap:
            reasons.add("inconclusive", "fresh_confirmation_cohort_required")
        if content_overlap:
            # Renamed ids, keys or ordering over decision content already scored here.
            reasons.add("inconclusive", "decision_content_seen_in_scored_cohort")

    def _mark_viewed(self, key: str, population: SealedPopulation) -> None:
        """Record the cases, families and id-free decision content a scored verdict used."""
        marker = {
            "cases": [c.case_id for c in population.cases],
            "families": sorted({c.family_id for c in population.cases}),
            "contents": sorted(
                {
                    d
                    for d in (
                        decision_content_digest(c, population.reference_fields)
                        for c in population.cases
                    )
                    if d is not None
                }
            ),
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
        floor = policy_floor_status(policy.as_json())
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
            # The same key without ids, so an answer-key revocation survives renamed ids.
            "answer_key_content_digest": population.answer_key_content_digest,
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
                # Held until a D11 policy floor is registered and met (``policy_floor_status``).
                "improvement_alert_eligible": alert and floor == "satisfied",
                "release_request_eligible": False,
                "handoff_request_eligible": False,
            },
            # What the verdict's own gates decided, kept apart from the owner's floor so a
            # later registered floor can be applied by ``authorize`` without re-scoring.
            "alert_gates": {
                "cleared": alert,
                "policy_floor": floor,
                "policy_floor_hold": None if floor == "satisfied" else "policy_floor_" + floor,
            },
            "evidence": {
                # This exported document: a plain digest anyone can recompute. The keyed seal
                # stays in the evaluator's store (``IndependentVerdict.custody_tier``).
                "evidence_tier": EVIDENCE_TIER,
                "store_seal": "evaluator_hmac_held_in_custody_not_exported",
                "audit_receipt_tier": RECEIPT_TIER,
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


# --------------------------------------------------------------------------- provider adapter


class ProviderReplay:
    """``ArtifactReplay`` over a provider- or trainer-served candidate, via injected callables.

    ``readback_fn`` returns the provider's serving readback (``route``, ``served_model_id``,
    ``identity_kind`` and, where the provider reports them, ``parent_revision`` and
    ``artifact_digest``). ``predict_fn(candidate_id, inputs)`` returns the outputs plus the model
    id the provider says served that batch; a batch served by any other id is refused, so a
    provider-side alias swap mid-replay is an integrity failure rather than a scored arm. It
    never imports or drives the managed/custom connectors; the caller wires them in.
    """

    def __init__(
        self,
        *,
        route: str,
        parent_model_id: str,
        candidate_id: str,
        readback_fn: Callable[[], Mapping[str, Any]],
        predict_fn: Callable[
            [str, list[dict[str, str]]], tuple[Sequence[Mapping[str, Any] | None], str]
        ],
        batch_size: int = 64,
    ) -> None:
        if route not in _JOB_DONE or type(batch_size) is not int or batch_size < 1:
            raise VerdictInputError("invalid_provider_replay")
        self._route = route
        self._parent = parent_model_id
        self._candidate = candidate_id
        self._readback = readback_fn
        self._predict = predict_fn
        self._batch = batch_size

    def served_identity(self) -> Mapping[str, Any]:
        return dict(self._readback())

    def predict(
        self, features: Sequence[Mapping[str, str]], *, expected_artifact: str
    ) -> Sequence[Mapping[str, Any] | None]:
        if expected_artifact != provider_candidate_digest(
            self._route, self._parent, self._candidate
        ):
            raise ValueError("provider_candidate_lineage_mismatch")
        out: list[Mapping[str, Any] | None] = []
        for start in range(0, len(features), self._batch):
            batch = [dict(f) for f in features[start : start + self._batch]]
            outputs, served_by = self._predict(self._candidate, batch)
            if served_by != self._candidate:
                raise ValueError("provider_served_other_revision")
            out.extend(outputs)
        return out


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
