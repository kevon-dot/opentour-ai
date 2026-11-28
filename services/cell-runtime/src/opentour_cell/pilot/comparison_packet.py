"""Stage one verified comparison packet and exactly one worker completion event (OPE-421).

Worker/backend side only. The hosted completion Zap and the version-bound Slack message are
owned by Codex (OPE-550 and the Slack approval listener); this module only produces the
least-disclosure packet and the single outbox event those consumers read.

What this module does:
  * assembles a packet from a frozen protected comparison (``comparison_plan`` RESULT) only when
    that comparison, its benchmark audit receipt (OPE-526 shape) and its independent verdict
    (OPE-528 interface) agree on exact digests, denominator and expiry;
  * refuses, with no packet, no event and no authority, anything missing, unverified, stale,
    superseded, scope-limited, expired, re-denominated or on a branch this ticket does not own;
  * commits the packet and its one outbox event in a single transaction, and delivers that event
    at-least-once to a sink that deduplicates on (resource, event id, event revision);
  * fences prior packets on a changed benchmark and reconciles earlier disclosures when a
    receipt is superseded.

What is simulated: every input is a generated sentinel. The audit receipt follows the OPE-526
receipt shape (which is not yet on ``main``); the independent verdict shape here is a PROPOSAL
pending OPE-528. The audit receipt is checked for digest self-consistency only: neither
document carries a signature, so authenticity of the issuer is NOT proven. The sink is a test
double.

What is NOT proven: no provider, Zapier, Slack or customer behavior; no installed-provider or
customer evidence; no OPE-526 receipt issued by a landed audit; no OPE-528 verdict from an
independent verifier. Passing tests prove software behavior over generated data only. A packet
is never a release decision, grants no release authority and triggers no delivery.

Exactly-one claim, stated precisely: the packet and its single outbox row commit in one
transaction (zero or one of each, never two). Delivery is AT-LEAST-ONCE under the stable key
(resource, event id, event revision). The exactly-one EFFECT therefore depends on the consumer
deduplicating on that key; here that is proven only against a test double, never against the
Codex-side consumer. This module does not and cannot claim exactly-once delivery.

Authenticity: digests alone cannot authenticate an auditor or independent verifier.
Improvement staging holds until this boundary can verify both issuers. An explicit fixture
option exercises the staging mechanics with generated evidence only.

DELIBERATE DECISION, NOT AN UNFINISHED FEATURE. No signing or key-custody contract exists in
this project, so no auditor or independent-verifier issuer can be verified here, and
``issuer_authenticated`` is hard-coded ``False``. Live (non-fixture) measured-improvement
staging therefore always holds with ``audit_and_verdict_issuers_unverified``. There is no
re-enable path short of a code change, and that is intended. It stands until a signing contract
lands. Lifting it requires, together: (1) a landed contract defining issuer keys, custody and
rotation for the OPE-526 audit receipt and the OPE-528 verdict; (2) signature verification of
both documents against pinned issuer keys inside this module, replacing the hard-coded
``False``; (3) tests that a forged, unsigned, wrong-key or replayed document is refused; and
(4) independent review of that change. Do not "fix" the hold by loosening the gate, widening
``allow_generated_fixture`` or trusting a self-asserted ``evidence_origin``. Hold and no-change
packets are not gated by this and are delivered.

Fixture flag: ``allow_generated_fixture`` exercises staging mechanics with generated evidence
only. It is honored only while ``PYTEST_CURRENT_TEST`` is set (pytest sets it). Otherwise
``PacketOutbox`` refuses to construct and ``assemble_packet`` refuses, with the closed code
``generated_fixture_not_permitted_in_live_context``, so a misconfiguration is loud. This is a
guard against accidentally enabling generated fixtures in a live process, nothing more. The
variable is caller-controlled, so it authenticates nothing and is not a security control. The
real barrier to live improvement staging is the issuer hold above: an authenticatable OPE-526
audit plus an OPE-528 independent verdict, neither of which exists yet.

Free text: a sealed comparison proves only that its bytes are self-consistent, not that its
strings are safe. Every string that this module copies from a comparison into a packet (the
decision reason, fallback and disposition, the winner, the metric, each arm's hold reason, arm
and pair names, comparison verdicts, slice statuses, economics basis, uncertainty method) is
checked against a closed vocabulary derived from what ``comparison_plan`` can emit, on every
path including holds. A value outside its vocabulary refuses the comparison with the fixed code
``comparison_unverified``, which never echoes the offending value.

Critical-slice names (OPE-571, decision D2): slice names are customer-chosen and can carry
customer or personal information, so the packet never carries them. Each name is published as
a KEYED digest, HMAC-SHA-256 under a per-engagement key that stays on the customer worker
(the keyed-commitment pattern decided on OPE-574/OPE-563). A plain SHA-256 is not used because
short names are guessable. Names resolve only in the customer's private view
(``private_slice_view``), which needs the same key and is never stored or sent. No key, no
packet: a missing or malformed engagement key refuses with ``slice_digest_key_unavailable`` and
never falls back to plain hashing; a keyring that raises refuses with
``slice_digest_key_lookup_failed``. Comparison refusals always precede the key lookup. The digest
key is derived from the supplied key, the purpose label, the tenant and the engagement, so even a
shared master key gives unlinkable digests across engagements. Until a signing and key-custody
contract lands (OPE-566) the key is engagement-local and caller-injected (``SliceKeyView``),
and the packet states that the digest is not issuer-authenticated. Because the key never leaves
the worker, only the key holder can re-derive the slice digests; a consumer without it can check
self-consistency and pointers.

The operating-point digest follows the same rule. It is a plain canonical SHA-256 over the
terms with every critical- and material-slice name first replaced by its keyed digest
(``slice_substituted_terms``), so a guessed name cannot be confirmed against it, while a
verifier (including OPE-528's evaluator) can still check the binding from the substituted terms
without the key. Raw terms are never hashed anywhere in this module. A verdict must declare
``operating_point_digest_form: "slice_substituted.v1"``: a verdict without that field refuses
with ``operating_point_digest_predates_slice_digests`` WITHOUT any digest comparison, and a
declared form with any other digest (including one computed over raw terms) refuses with the
same ``verdict_binding_mismatch`` as a random wrong digest, so no reason code distinguishes a
correct name guess from a wrong one. The rule applies on the hold path as well as the
improvement path, because a hold publishes the verdict's digest too.

Threat model for the caller-injected engagement key. Until OPE-566 defines key custody, the
key is caller-supplied authority, so this module makes the narrow claim below and no more.

* Only someone holding the key can confirm a guessed slice name, by keyed-hashing the guess.
  A consumer without the key cannot. Whoever controls ``SliceKeyView`` holds the key, so that
  caller can confirm guesses, and can choose a key that is weak or known to others, which lets
  those others confirm guesses too. Nothing here detects a known or reused key: ``_key_ok``
  checks only that it is exactly ``bytes``, 32 long and not one repeated byte value; there is
  no entropy estimation. Key strength is ``SliceKeyView``'s responsibility under OPE-566: keys
  come from a CSPRNG or a hardware token and are never derived from public inputs. Name
  privacy is therefore exactly as strong as the caller's key handling.
* Key control does not let anyone forge a comparison, audit receipt or verdict. Assembly still
  requires the comparison, the receipt and the independent verdict to agree on exact digests,
  denominator and expiry. A different key changes the substituted operating-point digest, so a
  verdict bound under one key does not verify under another, and is refused rather than
  re-bound.
* Key control does not grant release authority. A packet is never a release decision.
* A key controller can always deny service, by supplying no key, a malformed key or a raising
  keyring. Every such case refuses closed.
* The digests are not issuer-authenticated. They are at most ``locally_keyed``, and the packet
  says so.

Incumbent revision (F6). ``freeze_plan`` accepts any planner identifier as the incumbent
revision, so it is caller-chosen free text and is published only as a keyed digest under the
same purpose- and engagement-scoped key, domain-separated from slice names. A keyed digest was
chosen over a digest bound to the run because no keyless verifier needs it: the verdict binds
the raw comparator revision against the comparison, not against the packet, and a run-bound
plain digest would be guessable from the published run reference.

The keyed digest protects only ``arms.incumbent.revision``. It does NOT make the incumbent
unlinkable. On an improvement packet the remedy binding (``remedy_binding_mismatch``) requires
``binding.fallback_digest`` to equal the incumbent revision, because the key-less approval and
rollback consumers need that content digest. So an improvement packet publishes the incumbent
revision as-is: two engagements with the same incumbent publish the same value, which links
them, and anyone can confirm a guessed revision by equality. That is kept by decision, not
fixed. The packet states it per packet: ``critical_slice_names.incumbent_revision
.linkable_across_engagements_via_fallback_digest`` is ``True`` whenever the fallback digest
equals the incumbent revision, which is always so on an improvement packet. A hold does not
bind the two, but a worker may still supply equal values, and then the flag is ``True`` there
too. Identifier-form revisions cannot reach an improvement packet, because the fallback digest
must be 64-hex.

``previous_deployed_version`` is a worker-resolved context identifier and is published verbatim.
It must not carry the incumbent revision: a context whose ``previous_deployed_version`` equals
the raw incumbent revision (identifier or 64-hex form, both exact ``str``) is refused with
``previous_deployed_version_equals_incumbent_revision`` before any key lookup. Only exact
equality is detected. A value merely derived from the incumbent (a prefix, a suffix, another
case) is not, so the worker remains responsible for supplying a distinct deployment identifier.

Published digests that still cover raw names (recorded, not fixed here). ``comparison
.evidence_revision`` and ``plan_revision`` are plain SHA-256 over the whole comparison and
plan, and both contain the raw slice names in ``terms`` and in each arm's ``critical_slices``.
They are not practically guessable, because they also cover values the packet never
publishes (timestamps, case ids, references, the intake revision). But a verdict binds
``comparison_evidence_revision``, so an evaluator checking that binding currently needs the
raw comparison. The follow-up, under OPE-528, is to bind a slice-substituted evidence revision
instead.

A differential test pins what this module itself derives. It builds the same packet under two
keys and two sets of slice names (or two incumbent revisions) and requires that no published
leaf is equal under both keys yet different between the names, except ``comparison
.evidence_revision`` and its ``protected_comparison`` evidence pointer (F4, above) and, for the
incumbent on an improvement packet, ``binding.fallback_digest`` (by contract, above). It varies
the comparison this module receives and holds ``plan_revision`` fixed, because this module copies
that digest without being able to recompute it. A real re-plan with other names would change
``plan_revision`` too, so the F4 limit above applies to it unchanged.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot.comparison_plan import RESULT
from opentour_cell.pilot.operating_point import canonical, digest

Json = dict[str, Any]
PACKET = "opentour.pilot.comparison-packet.v1"
EVENT = "opentour.pilot.comparison-completed-event.v1"
VERDICT = "opentour.pilot.independent-verdict.v1"
RECEIPT = "opentour.benchmark-audit-receipt.v1"
SLICE_DIGEST = "opentour.pilot.critical-slice-digest.v1"
_SLICE_TERMS = ("critical_slices", "material_slices")
# The only operating-point digest form a verdict may bind. A verdict that does not declare it
# predates OPE-571 and is refused by the field's absence alone, never by comparing a digest.
OPERATING_POINT_DIGEST_FORM = "slice_substituted.v1"
_LEGACY_POINT = "operating_point_digest_predates_slice_digests"
EVENT_REVISION = 1
_FIXTURE_REFUSAL = "generated_fixture_not_permitted_in_live_context"
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:@/-]{0,127}")
_SHA = re.compile(r"[0-9a-f]{64}")
_REPRODUCED = "reproduced"
_ALLOWED = "evaluation_allowed"
_ORIGINS = ("generated", "observed")
_VERDICTS = (
    "evaluation_allowed",
    "starter_only",
    "claim_limited_to_named_population",
    "insufficient_evidence",
    "invalid",
    "stale",
)
# Closed vocabularies for strings copied from a comparison into a packet. Sources, per value:
#   * hold reasons: ``comparison_plan._checks`` return values.
#   * observed comparison: ``readout.evaluate_manifest`` (``comparison = ...`` assignments).
#   * decision reasons: ``comparison_plan._pick`` (``outcome(...)`` literals, the literals
#     appended to ``reasons``, every arm hold reason, and every observed comparison other than
#     ``improved``), plus the development-hold reasons from
#     ``simple_repair.assess_development`` carried by ``comparison_plan.development_hold``.
#   * caps: ``pilot_metrics.economics``; basis: ``pilot_metrics.economics`` and ``_unavailable``.
#   * operating-point and slice statuses: ``operating_point._arm`` and ``comparison_plan``.
#   * exclusion keys: ``source_intake`` (``_reason``, ``_partition`` and its caller).
# Tuples, not sets: membership must never raise on an unhashable value.
_HOLD_REASONS = (
    None,
    "insufficient_protected_support",
    "incomplete_protected_evidence",
    "operating_point_missed_or_undetermined",
    "critical_slice_failed_or_unverified",
    "cost_limit_failed_or_unverified",
    "latency_limit_failed_or_unverified",
)
_OBSERVED = (
    "improved",
    "regressed",
    "equivalent",
    "insufficient_evidence",
    "different_automatic_cases",
)
_DECISION_REASONS = (
    "protected_improvement_at_agreed_limits",
    "adequate_simple_repair_at_lower_observed_cost",
    "no_confirmed_improvement",
    "equivalent_to_incumbent",
    "quality_cost_tradeoff_requires_owner_choice",
    *(r for r in _HOLD_REASONS if r),
    *(r for r in _OBSERVED if r not in ("improved", "equivalent")),
    # Development-stage hold reasons carried by ``development_hold``.
    "insufficient_independent_development_evidence",
    "recurring_residual_requires_model_and_compute_rights",
    "simple_repair_unproven_and_residual_support_insufficient",
)
_FALLBACKS = ("incumbent",)
_METRICS = (None, "verified_wrong_automatic")  # the planner default; a custom metric is refused
_DISPOSITIONS = ("recommend", "hold", "no_change")
_WINNERS = (None, "incumbent", "simple", "model")
_ARM_NAMES = ("incumbent", "simple", "model")
_PAIR_NAMES = ("incumbent_vs_simple", "incumbent_vs_model", "simple_vs_model")
_SLICE_STATUSES = ("met", "missed", "undetermined")
_COMBINED = ("met", "missed", "undetermined")
_CAPS = ("met", "missed", "unavailable")
_EXCLUSION_KEYS = (
    "changed_policy_or_schema",
    "fields_outside_grant",
    "future_decision_features",
    "missing_outcome",
    "contradictory_outcomes",
    "ambiguous_outcomes",
    "reused_outcome",
    "unapproved_adjudicator",
    "outcome_unavailable",
    "facts_unavailable_at_decision",
    "inconsistent_outcome_time",
    "verification_after_cutoff",
    "ambiguous_trace",
    "group_crosses_split",
    "development_label_after_split",
)
# Critical-slice names are customer-chosen, so no closed vocabulary exists. This is a
# WELL-FORMEDNESS check only, NOT a disclosure control: it is exactly the identifier shape the
# planner enforces (``simple_repair.identifier``, used by ``comparison_plan._terms``), so every
# planner-legal name passes. The disclosure control is ``slice_digest`` (OPE-571): the packet
# carries only keyed digests of these names. A test pins this pattern to the planner's own.
_SLICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
# Rendered decimal measurements (``pilot_metrics.rendered``): six fixed places, or absent.
_DECIMAL = re.compile(r"[0-9]{1,40}\.[0-9]{6}")
# The planner's own identifier shape (``simple_repair.identifier``). ``freeze_plan`` requires only
# this of ``incumbent_revision``, so the incumbent revision cannot be required to be a digest. The
# arm field carries only its keyed digest (F6), but an improvement packet also publishes the
# revision as-is in ``binding.fallback_digest`` (see the module docstring), and
# ``previous_deployed_version`` may not equal it.
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_ECON_BASES = ("unavailable", "observed", "modeled", "generated")
_UNCERTAINTY = ("wilson_95_independent_verified_cases", "unavailable")
_PAIRED_FIELDS = (
    "comparable",
    "excluded",
    "fixes",
    "regressions",
    "ties_correct",
    "ties_incorrect",
)
_ARM_FIELDS = ("automatic", "incorrect_automatic", "review", "missing")
_ECON_FIELDS = (
    "basis",
    "cost_per_1000",
    "review_minutes_per_1000",
    "mean_latency_ms",
    "total_latency_ms_per_1000",
    "cost_cap",
    "latency_cap",
)


def _fixture_context() -> bool:
    """True while ``PYTEST_CURRENT_TEST`` is set. An accident guard, not authentication: the
    variable is caller-controlled. The repository has no other live/fixture switch."""
    return "PYTEST_CURRENT_TEST" in os.environ


class PacketStoreError(RuntimeError):
    """Safe diagnostic for a corrupt or unusable local outbox."""


@dataclass(frozen=True)
class PacketContext:
    """Bindings the worker resolves from its current connection and mandate, never the caller."""

    tenant: str
    engagement: str
    recipient: str
    mandate_revision: str
    run_ref: str
    proposal_version: str
    benchmark_digest: str
    remedy_digest: str | None
    fallback_digest: str
    expires_at: int
    previous_deployed_version: str | None
    worker_id: str

    def valid(self) -> bool:
        ids = (
            self.tenant,
            self.engagement,
            self.recipient,
            self.mandate_revision,
            self.run_ref,
            self.proposal_version,
            self.worker_id,
        )
        digests = (self.benchmark_digest, self.fallback_digest)
        return (
            all(isinstance(v, str) and _ID.fullmatch(v) for v in ids)
            and all(isinstance(v, str) and _SHA.fullmatch(v) for v in digests)
            and (self.remedy_digest is None or bool(_SHA.fullmatch(self.remedy_digest)))
            and isinstance(self.expires_at, int)
            and not isinstance(self.expires_at, bool)
            and (
                self.previous_deployed_version is None
                or (
                    type(self.previous_deployed_version) is str
                    and bool(_ID.fullmatch(self.previous_deployed_version))
                )
            )
        )

    def as_json(self) -> Json:
        return {
            "tenant": self.tenant,
            "engagement": self.engagement,
            "recipient": self.recipient,
            "mandate_revision": self.mandate_revision,
            "run_ref": self.run_ref,
            "proposal_version": self.proposal_version,
            "benchmark_digest": self.benchmark_digest,
            "remedy_digest": self.remedy_digest,
            "fallback_digest": self.fallback_digest,
            "expires_at": self.expires_at,
            "previous_deployed_version": self.previous_deployed_version,
        }


class AuthorityView(Protocol):
    """Injected boundary: the worker's current mandate. ``None`` means unavailable."""

    def current(self, run_ref: str) -> PacketContext | None: ...


class SliceKeyView(Protocol):
    """Injected boundary: the engagement-local slice-digest key on the customer worker.

    Returns the 32-byte key for ``(tenant, engagement)``, or ``None`` when it is unavailable.
    The key never enters a packet, event, refusal or the outbox. Its custody and rotation are
    owned by OPE-566; until then it is engagement-local and not issuer-authenticated. Key
    STRENGTH is this boundary's responsibility: keys come from a CSPRNG or a hardware token and
    are never derived from public inputs. This module only refuses obviously unusable keys.
    """

    def slice_key(self, tenant: str, engagement: str) -> bytes | None: ...


class EventSink(Protocol):
    """Injected boundary: the consumer. It must deduplicate on the event's idempotency key."""

    def deliver(self, event: Json) -> None: ...


@dataclass(frozen=True)
class StageOutcome:
    status: str  # staged | already_staged | refused
    reason: str | None = None
    packet: Json | None = None
    event_id: str | None = None
    scoped_hold: Json | None = None


def _refuse(reason: str) -> StageOutcome:
    # A failed, missing or inconclusive receipt is displayed as a scoped hold; it acquires no
    # release authority and creates neither a packet nor an event.
    return StageOutcome(
        "refused",
        reason,
        scoped_hold={
            "scope": "measured_improvement_proposal",
            "state": "hold",
            "reason": reason,
            "release_authority": False,
        },
    )


def _sealed(value: Mapping[str, Any], field: str) -> bool:
    return value.get(field) == digest({k: v for k, v in value.items() if k != field})


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _digest_ok(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA.fullmatch(value))


def _decimal_ok(value: Any) -> bool:
    return value is None or (isinstance(value, str) and bool(_DECIMAL.fullmatch(value)))


def _bound_ok(value: Any) -> bool:
    return value is None or (
        isinstance(value, float) and not isinstance(value, bool) and 0.0 <= value <= 1.0
    )


def _fraction_ok(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _count(value.get("numerator"))
        and _count(value.get("denominator"))
    )


def _key_ok(key: Any) -> bool:
    """Exactly ``bytes`` (no subclass), 32 long, and not a single repeated byte value."""
    return type(key) is bytes and len(key) == 32 and len(set(key)) > 1


def _scoped_key(key: bytes, tenant: str, engagement: str) -> bytes:
    """Derive the slice-digest key for one purpose, tenant and engagement.

    Same two-step shape as the repository's other keyed seals (``starter_reconnect``): derive a
    scoped key from the supplied key, then MAC the value. Binding tenant and engagement into the
    derivation keeps digests unlinkable across engagements even if one master key is shared.
    """
    label = canonical([SLICE_DIGEST, tenant, engagement]).encode()
    return hmac.digest(key, label, "sha256")


def _mac(name: str, scoped: bytes) -> str:
    return hmac.digest(scoped, name.encode(), "sha256").hex()


def _substitute(terms: Json, scoped: bytes) -> Json:
    return {k: [_mac(n, scoped) for n in v] if k in _SLICE_TERMS else v for k, v in terms.items()}


def slice_digest(name: str, key: bytes, *, tenant: str, engagement: str) -> str:
    """Keyed digest of one slice name: HMAC-SHA-256 under the tenant/engagement-scoped key.

    Never a plain hash.
    """
    return _mac(name, _scoped_key(key, tenant, engagement))


def slice_substituted_terms(terms: Json, key: bytes, *, tenant: str, engagement: str) -> Json:
    """``terms`` with every critical- and material-slice name replaced by its keyed digest.

    Order and every other term are kept, so the canonical form differs only in the names.
    """
    return _substitute(terms, _scoped_key(key, tenant, engagement))


def operating_point_digest(substituted_terms: Json) -> str:
    """Plain canonical SHA-256 over SLICE-SUBSTITUTED terms (OPE-571 follow-up).

    Deliberately unkeyed: a verifier, including the OPE-528 evaluator, checks the binding from
    the substituted terms alone, without the engagement key. The names inside it are already
    keyed digests, so a guessed name cannot be confirmed against it. Never pass raw terms.
    """
    return digest(substituted_terms)


def _terms_ok(terms: Any) -> bool:
    """Terms are digested, not copied, but each slice name must be a planner-shaped string."""
    return isinstance(terms, dict) and all(
        isinstance(terms.get(k, []), list)
        and all(isinstance(n, str) and bool(_SLICE_NAME.fullmatch(n)) for n in terms.get(k, []))
        for k in _SLICE_TERMS
    )


def incumbent_revision_digest(revision: str, key: bytes, *, tenant: str, engagement: str) -> str:
    """Keyed digest of the incumbent arm's revision (F6), under the same scoped slice key."""
    return _revision_mac(revision, _scoped_key(key, tenant, engagement))


def _revision_mac(revision: str, scoped: bytes) -> str:
    # Domain-separated from slice names: a slice name is a bare identifier and never begins
    # with "[", so the two can never share an input or a digest.
    return _mac(canonical(["incumbent_revision", revision]), scoped)


def _comparison_scope(comparison: Json) -> tuple[str, str]:
    return comparison["organization"], comparison["engagement"]


def _point_binding(verdict: Json, comparison: Json, slice_key: bytes) -> str | None:
    """The verdict's operating-point binding, on the hold and the improvement path alike.

    Key-free and oracle-free: the legacy case is decided by the form field's ABSENCE, with no
    digest comparison, and every wrong digest (a raw-terms digest, correct names or not, or a
    random one) gets the same ``verdict_binding_mismatch``. Raw terms are never hashed.
    """
    if "operating_point_digest_form" not in verdict:
        return _LEGACY_POINT
    point = operating_point_digest(_substitute(comparison.get("terms", {}), slice_key))
    if (
        verdict["operating_point_digest_form"] != OPERATING_POINT_DIGEST_FORM
        or verdict.get("operating_point_digest") != point
    ):
        return "verdict_binding_mismatch"
    return None


def _arm_vocab_ok(name: str, arm: Any) -> bool:
    """Closed-vocabulary and type checks for everything ``_arm_aggregate`` copies."""
    if not isinstance(arm, dict) or not isinstance(arm.get("metrics"), dict):
        return False
    metrics = arm["metrics"]
    slices = arm.get("critical_slices")
    economics = metrics.get("economics")
    uncertainty = metrics.get("uncertainty")
    operating = metrics.get("operating_point")
    revision = arm.get("revision")
    revision_ok = (
        # Exact ``str``: the revision is compared with ``previous_deployed_version`` (LOW-2).
        type(revision) is str and bool(_IDENTIFIER.fullmatch(revision))
        if name == "incumbent"
        else _digest_ok(revision)
    )
    return (
        arm.get("hold_reason") in _HOLD_REASONS
        and revision_ok
        and _digest_ok(arm.get("prediction_digest"))
        and all(_count(metrics.get(k)) for k in _ARM_FIELDS)
        and _fraction_ok(metrics.get("coverage"))
        and _fraction_ok(metrics.get("observed_error_rate"))
        and isinstance(operating, dict)
        and operating.get("combined") in _COMBINED
        and isinstance(slices, dict)
        and all(
            isinstance(key, str)
            and bool(_SLICE_NAME.fullmatch(key))
            and isinstance(v, dict)
            and v.get("status") in _SLICE_STATUSES
            for key, v in slices.items()
        )
        and isinstance(economics, dict)
        and economics.get("basis") in _ECON_BASES
        and economics.get("cost_cap") in _CAPS
        and economics.get("latency_cap") in _CAPS
        and all(_decimal_ok(economics.get(k)) for k in _ECON_FIELDS[1:5])
        and isinstance(uncertainty, dict)
        and uncertainty.get("method") in _UNCERTAINTY
        and _bound_ok(uncertainty.get("lower"))
        and _bound_ok(uncertainty.get("upper"))
        and (uncertainty.get("small_sample") is None or type(uncertainty["small_sample"]) is bool)
    )


def _exclusions_ok(exclusions: Any) -> bool:
    """Keys are intake exclusion reasons, values non-negative counts; anything else refuses."""
    return isinstance(exclusions, dict) and all(
        key in _EXCLUSION_KEYS and _count(value) and value >= 0 for key, value in exclusions.items()
    )


def _comparison_vocab_ok(comparison: Json) -> bool:
    """Free text cannot reach a packet or a refusal: every copied string is from a closed set.

    Runs on every path (hold, no-change and improvement) because a hold packet is delivered
    without any receipt or verdict.
    """
    decision = comparison["decision"]
    arms, pairs = comparison["arms"], comparison["comparisons"]
    return (
        decision.get("reason") in _DECISION_REASONS
        and decision.get("fallback") in _FALLBACKS
        and decision.get("disposition") in _DISPOSITIONS
        and decision.get("winner") in _WINNERS
        and comparison.get("metric") in _METRICS
        and all(name in _ARM_NAMES for name in arms)
        and all(_arm_vocab_ok(name, arm) for name, arm in arms.items())
        and ("exclusions" not in comparison or _exclusions_ok(comparison["exclusions"]))
        and _count(comparison.get("protected_rows_evaluated"))
        and all(name in _PAIR_NAMES for name in pairs)
        and all(
            isinstance(pair, dict)
            and pair.get("observed_comparison") in _OBSERVED
            and isinstance(pair.get("paired"), dict)
            and pair["paired"].keys() == set(_PAIRED_FIELDS)
            and all(_count(v) for v in pair["paired"].values())
            for pair in pairs.values()
        )
        and _digest_ok(comparison.get("plan_revision"))
        and _digest_ok(comparison.get("protected_membership_digest"))
    )


def _comparison_ok(comparison: Any) -> bool:
    return (
        isinstance(comparison, dict)
        and comparison.get("schema_version") == RESULT
        and _sealed(comparison, "evidence_revision")
        and isinstance(comparison.get("protected_rows_evaluated"), int)
        and isinstance(comparison.get("decision"), dict)
        and isinstance(comparison.get("arms"), dict)
        and isinstance(comparison.get("comparisons"), dict)
        and _comparison_vocab_ok(comparison)
        and _terms_ok(comparison.get("terms", {}))
        and isinstance(comparison.get("engagement"), str)
        and bool(_IDENTIFIER.fullmatch(comparison["engagement"]))
    )


def _receipt_ok(receipt: Any) -> bool:
    if not isinstance(receipt, dict) or receipt.get("schema_version") != RECEIPT:
        return False
    body = {k: v for k, v in receipt.items() if k not in ("receipt_digest", "receipt_id")}
    bindings = receipt.get("bindings")
    return (
        isinstance(receipt.get("receipt_digest"), str)
        and receipt["receipt_digest"] == digest(body)
        and receipt.get("receipt_id") == "audit-" + receipt["receipt_digest"][:16]
        and receipt.get("verdict") in _VERDICTS
        and receipt.get("evidence_origin", "generated") in _ORIGINS
        and isinstance(bindings, dict)
        and isinstance(bindings.get("frozen"), dict)
        and isinstance(bindings.get("current"), dict)
    )


def _verdict_ok(verdict: Any) -> bool:
    return (
        isinstance(verdict, dict)
        and verdict.get("schema_version") == VERDICT
        and _sealed(verdict, "verdict_digest")
        and isinstance(verdict.get("denominator"), dict)
        # OPE-528 emits content digests here. A free-text revision (such as an identifier-form
        # incumbent) inside a verdict whose digest the packet publishes could be confirmed by
        # guessing, so only 64-hex values are accepted (F6).
        and _digest_ok(verdict.get("candidate_digest"))
        and _digest_ok(verdict.get("comparator_digest"))
    )


def _denominator_ok(comparison: Json) -> bool:
    total = comparison["protected_rows_evaluated"]
    for arm in comparison["arms"].values():
        if arm["metrics"]["coverage"]["denominator"] != total:
            return False
        if arm["metrics"]["observed_error_rate"]["denominator"] > total:
            return False
    return all(
        pair["paired"]["comparable"] + pair["paired"]["excluded"] <= total
        for pair in comparison["comparisons"].values()
    )


def _arm_aggregate(name: str, arm: Json, slice_key: bytes) -> Json:
    metrics = arm["metrics"]
    economics = metrics["economics"]
    return {
        # The incumbent revision is caller-chosen free text (any planner identifier), so it is
        # published only as a keyed digest. The other arms' revisions are content digests.
        "revision": _revision_mac(arm["revision"], slice_key)
        if name == "incumbent"
        else arm["revision"],
        "prediction_digest": arm["prediction_digest"],
        "counts": {k: metrics[k] for k in _ARM_FIELDS},
        "coverage": [metrics["coverage"]["numerator"], metrics["coverage"]["denominator"]],
        "wrong_automatic": [
            metrics["observed_error_rate"]["numerator"],
            metrics["observed_error_rate"]["denominator"],
        ],
        "uncertainty": {
            k: metrics["uncertainty"].get(k) for k in ("method", "lower", "upper", "small_sample")
        },
        "operating_point": metrics["operating_point"]["combined"],
        "critical_slices": dict(
            sorted((_mac(k, slice_key), v["status"]) for k, v in arm["critical_slices"].items())
        ),
        "hold_reason": arm["hold_reason"],
        "economics": {k: economics[k] for k in _ECON_FIELDS},
    }


def _missing_units(arms: Json) -> list[Json]:
    found = []
    for name, arm in sorted(arms.items()):
        economics = arm["economics"]
        if economics["basis"] == "unavailable":
            found.append({"arm": name, "unit": "review_time_runtime_and_spend"})
    return found


def _assemble(
    comparison: Json,
    receipt: Json | None,
    verdict: Json | None,
    ctx: PacketContext,
    *,
    slice_key: bytes,
    allow_generated_fixture: bool,
) -> tuple[Json | None, str | None]:
    """Pure and deterministic: identical inputs reproduce the identical packet digest.

    ``slice_key`` here (and in the helpers it calls) is the already-scoped key from
    ``_scoped_key``, never the supplied engagement key.
    """
    decision = comparison["decision"]
    winner = decision.get("winner")
    improvement = decision.get("disposition") == "recommend"
    if improvement and winner != "simple":
        return None, "model_winner_branch_not_in_this_packet"
    if improvement:
        failure = _improvement_checks(comparison, receipt, verdict, ctx, winner, slice_key)
        if failure:
            return None, failure
        assert receipt is not None
        if not (allow_generated_fixture and receipt.get("evidence_origin") == "generated"):
            return None, "audit_and_verdict_issuers_unverified"
    else:
        # A hold needs neither document, but whatever is supplied is copied into the packet
        # (ids, digests, verdict), so it is validated here exactly as on the improvement path.
        if receipt is not None and not _receipt_ok(receipt):
            return None, "audit_receipt_unverified"
        if verdict is not None and not _verdict_ok(verdict):
            return None, "independent_verdict_unverified"
        # A hold publishes the verdict's digest, so the verdict must bind the substituted
        # operating point exactly as on the improvement path (F5).
        point_failure = _point_binding(verdict, comparison, slice_key) if verdict else None
        if point_failure:
            return None, point_failure
    arms = {
        name: _arm_aggregate(name, arm, slice_key)
        for name, arm in sorted(comparison["arms"].items())
    }
    pairs = {
        name: {**pair["paired"], "observed_comparison": pair["observed_comparison"]}
        for name, pair in sorted(comparison["comparisons"].items())
    }
    origin = receipt["evidence_origin"] if receipt and "evidence_origin" in receipt else None
    packet: Json = {
        "schema_version": PACKET,
        "kind": "measured_improvement" if improvement else "hold",
        "binding": ctx.as_json(),
        "comparison": {
            "evidence_revision": comparison["evidence_revision"],
            "plan_revision": comparison["plan_revision"],
            "protected_membership_digest": comparison["protected_membership_digest"],
            "operating_point_digest": operating_point_digest(
                _substitute(comparison.get("terms", {}), slice_key)
            ),
            "metric": comparison.get("metric"),
        },
        "denominator": {
            "protected_rows": comparison["protected_rows_evaluated"],
            "excluded_counts": dict(sorted(comparison.get("exclusions", {}).items())),
        },
        "arms": arms,
        "critical_slice_names": {
            "form": "keyed_digest",
            "algorithm": "hmac_sha256",
            "key_scope": "engagement",
            "keyed_engagement": ctx.engagement,
            "key_custody": "customer_worker_local",
            "names_disclosed": False,
            # F6: the incumbent arm's revision is published under the same keyed digest, but
            # that protects the arm field only. When the fallback digest equals the revision
            # (always on an improvement packet, by the remedy binding) the revision is
            # published as-is in ``binding.fallback_digest`` and links engagements.
            "incumbent_revision": {
                "arm_revision": "keyed_digest",
                "linkable_across_engagements_via_fallback_digest": _fallback_is_incumbent(
                    comparison, ctx
                ),
            },
            # Engagement-local key, no custody or signing contract yet (OPE-566 unlanded).
            "issuer_authenticated": False,
        },
        "paired": pairs,
        "decision": {
            "disposition": decision["disposition"],
            "winner": winner,
            "reason": decision["reason"],
            "fallback": decision["fallback"],
        },
        "immature_outcome_data": bool(sum(comparison.get("exclusions", {}).values())),
        "missing_comparable_units": _missing_units(arms),
        "audit": {
            "receipt_id": receipt.get("receipt_id") if receipt else None,
            "receipt_digest": receipt.get("receipt_digest") if receipt else None,
            "verdict": receipt.get("verdict") if receipt else None,
            "independent_verdict_digest": verdict.get("verdict_digest") if verdict else None,
            # No signature exists on either document yet (OPE-526/OPE-528 unlanded).
            "issuer_authenticated": False,
            "integrity": "digest_self_consistent_only",
        },
        "claim": {
            "evidence_tier": "generated" if origin == "generated" else "none",
            "scope": "implementation_only",
            "provider_or_customer_evidence": False,
            "accuracy_claim_eligible": False,
        },
        "evidence_pointers": [
            {"kind": "protected_comparison", "digest": comparison["evidence_revision"]},
            {"kind": "benchmark_audit_receipt", "digest": _pointer(receipt, "receipt_digest")},
            {"kind": "independent_verdict", "digest": _pointer(verdict, "verdict_digest")},
            {"kind": "paired_case_set", "digest": digest(comparison.get("paired_cases", []))},
        ],
        "authority": {"release": False, "delivery": False},
        "next_action": "separate_human_decision_required"
        if improvement
        else "no_release_authority_held",
    }
    return {**packet, "packet_digest": digest(packet)}, None


def _fallback_is_incumbent(comparison: Json, ctx: PacketContext) -> bool:
    incumbent = comparison["arms"].get("incumbent")
    return incumbent is not None and ctx.fallback_digest == incumbent["revision"]


def _pointer(document: Json | None, field: str) -> str | None:
    return document.get(field) if document else None


def _improvement_checks(
    comparison: Json,
    receipt: Json | None,
    verdict: Json | None,
    ctx: PacketContext,
    winner: str,
    slice_key: bytes,
) -> str | None:
    if receipt is None:
        return "audit_receipt_missing"
    if not _receipt_ok(receipt):
        return "audit_receipt_unverified"
    if receipt.get("superseded_by") is not None:
        return "audit_receipt_superseded"
    bindings = receipt["bindings"]
    if receipt["verdict"] == "stale" or bindings["frozen"] != bindings["current"]:
        return "audit_receipt_stale"
    if receipt["verdict"] == "claim_limited_to_named_population":
        return "audit_claim_limited_to_named_population"
    if receipt["verdict"] != _ALLOWED:
        return "audit_verdict_" + re.sub(r"\W", "_", receipt["verdict"])
    if verdict is None:
        return "independent_verdict_missing"
    if not _verdict_ok(verdict):
        return "independent_verdict_unverified"
    if verdict.get("verdict") != _REPRODUCED:
        return "independent_verdict_not_reproduced"
    if (
        verdict.get("verifier_id") in (None, ctx.worker_id)
        or verdict.get("producer_id") != ctx.worker_id
    ):
        return "independent_verdict_not_independent"
    arms = comparison["arms"]
    if not _denominator_ok(comparison) or verdict["denominator"] != {
        "protected_rows": comparison["protected_rows_evaluated"],
        "membership_digest": comparison["protected_membership_digest"],
    }:
        return "denominator_changed"
    pair = comparison["comparisons"].get("incumbent_vs_" + winner)
    coverage = arms[winner]["metrics"]["coverage"]
    floor = comparison.get("terms", {}).get("minimum_coverage_bps", 0)
    if (
        arms[winner]["hold_reason"]
        or not isinstance(pair, dict)
        or pair.get("observed_comparison") != "improved"
        or coverage["numerator"] * 10_000 < floor * coverage["denominator"]
    ):
        return "decision_inconsistent_with_evidence"
    point_failure = _point_binding(verdict, comparison, slice_key)
    if point_failure:
        return point_failure
    point = operating_point_digest(_substitute(comparison.get("terms", {}), slice_key))
    bound = {
        "audit_receipt_digest": receipt["receipt_digest"],
        "comparison_evidence_revision": comparison["evidence_revision"],
        "candidate_digest": arms[winner]["revision"],
        "comparator_digest": arms["incumbent"]["revision"],
        "operating_point_digest": point,
        "benchmark_digest": ctx.benchmark_digest,
    }
    if any(verdict.get(k) != v for k, v in bound.items()):
        return "verdict_binding_mismatch"
    if (
        ctx.remedy_digest != arms[winner]["revision"]
        or ctx.fallback_digest != (arms["incumbent"]["revision"])
    ):
        return "remedy_binding_mismatch"
    return None


def assemble_packet(
    comparison: Any,
    receipt: Json | None,
    verdict: Json | None,
    ctx: PacketContext,
    *,
    slice_key: bytes | None,
    allow_generated_fixture: bool = False,
) -> tuple[Json | None, str | None]:
    """Return ``(packet, None)`` or ``(None, reason)``. Never raises on a governance failure.

    ``slice_key`` is the engagement key for ``comparison["engagement"]``. Without a valid one
    there is no packet: slice names are never published plainly or under a plain hash.
    """
    failure = _preflight(comparison, ctx, allow_generated_fixture)
    if failure:
        return None, failure
    if not _key_ok(slice_key):
        return None, "slice_digest_key_unavailable"
    assert slice_key is not None
    return _assemble(
        comparison,
        receipt,
        verdict,
        ctx,
        slice_key=_scoped_key(slice_key, ctx.tenant, ctx.engagement),
        allow_generated_fixture=allow_generated_fixture,
    )


def _preflight(comparison: Any, ctx: PacketContext, allow_generated_fixture: bool) -> str | None:
    """Refusals that never need the slice key. They always precede any key lookup."""
    if allow_generated_fixture and not _fixture_context():
        return _FIXTURE_REFUSAL
    if not ctx.valid():
        return "invalid_packet_context"
    if not _comparison_ok(comparison):
        return "comparison_unverified"
    if comparison.get("organization") != ctx.tenant:
        return "comparison_scope_mismatch"
    if comparison.get("engagement") != ctx.engagement:
        # The engagement is resolved by the worker, never chosen by the comparison's author.
        return "comparison_engagement_mismatch"
    incumbent = comparison["arms"].get("incumbent")
    # Both values are exact ``str`` here (``PacketContext.valid`` and ``_arm_vocab_ok``), so
    # this is plain string equality. The published context must not carry the raw revision.
    if (
        ctx.previous_deployed_version is not None
        and incumbent is not None
        and ctx.previous_deployed_version == incumbent["revision"]
    ):
        return "previous_deployed_version_equals_incumbent_revision"
    return None


def verify_packet(
    packet: Json,
    comparison: Json,
    receipt: Json | None,
    verdict: Json | None,
    ctx: PacketContext,
    *,
    slice_key: bytes | None,
    allow_generated_fixture: bool = False,
) -> bool:
    """Replay: re-derive the packet from its sources and compare digests.

    Re-deriving the slice digests needs the engagement key, so only its holder (the customer
    worker) can run a full replay. A consumer without the key checks ``packet_digest`` and the
    evidence pointers only.
    """
    rebuilt, reason = assemble_packet(
        comparison,
        receipt,
        verdict,
        ctx,
        slice_key=slice_key,
        allow_generated_fixture=allow_generated_fixture,
    )
    return reason is None and rebuilt == packet and _sealed(packet, "packet_digest")


def private_slice_view(packet: Any, comparison: Any, slice_key: bytes | None) -> Json | None:
    """The customer's private view: each arm's slice statuses under their readable names.

    Resolved locally from the customer's own comparison and engagement key. The result is for
    the customer's display only: it is never stored in the outbox, put in an event or sent.
    Returns ``None`` unless every published digest resolves to one of the comparison's names.
    """
    if not (_key_ok(slice_key) and _comparison_ok(comparison)) or not isinstance(packet, dict):
        return None
    assert slice_key is not None
    arms = packet.get("arms")
    if not _sealed(packet, "packet_digest") or not isinstance(arms, dict):
        return None
    scoped = _scoped_key(slice_key, *_comparison_scope(comparison))
    names = {
        _mac(name, scoped): name
        for arm in comparison["arms"].values()
        for name in arm["critical_slices"]
    }
    view: Json = {}
    for arm_name, arm in sorted(arms.items()):
        slices = arm.get("critical_slices") if isinstance(arm, dict) else None
        if not isinstance(slices, dict) or not all(d in names for d in slices):
            return None
        view[arm_name] = {names[d]: status for d, status in sorted(slices.items())}
    return view


def event_for(packet: Json) -> Json:
    ctx = packet["binding"]
    improvement = packet["kind"] == "measured_improvement"
    return {
        "schema_version": EVENT,
        "resource": "comparison/" + ctx["run_ref"],
        "event_id": "evt-" + packet["packet_digest"][:32],
        "event_revision": EVENT_REVISION,
        "tenant": ctx["tenant"],
        "recipient": ctx["recipient"],
        "run_ref": ctx["run_ref"],
        "proposal_version": ctx["proposal_version"],
        "expires_at": ctx["expires_at"],
        "disposition": packet["decision"]["disposition"],
        "staged_for_human_decision": improvement,
        "packet_ref": {
            "packet_id": "pkt-" + packet["packet_digest"][:32],
            "packet_digest": packet["packet_digest"],
        },
        "authority": {"release": False, "delivery": False},
    }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS packets(
 packet_id TEXT PRIMARY KEY, run_ref TEXT NOT NULL, proposal_version TEXT NOT NULL,
 benchmark_digest TEXT NOT NULL, receipt_id TEXT, packet TEXT NOT NULL,
 state TEXT NOT NULL, state_reason TEXT, UNIQUE(run_ref, proposal_version));
CREATE TABLE IF NOT EXISTS outbox(
 resource TEXT NOT NULL, event_id TEXT NOT NULL, event_revision INTEGER NOT NULL,
 packet_id TEXT NOT NULL REFERENCES packets(packet_id), event TEXT NOT NULL,
 state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, receipt_ref TEXT,
 PRIMARY KEY(resource, event_id, event_revision));
CREATE TABLE IF NOT EXISTS superseded_receipts(receipt_id TEXT PRIMARY KEY, successor TEXT);
CREATE TABLE IF NOT EXISTS reconciliations(
 packet_id TEXT PRIMARY KEY REFERENCES packets(packet_id), receipt_id TEXT NOT NULL,
 event_delivered INTEGER NOT NULL, state TEXT NOT NULL);
"""
_TABLES = ("packets", "outbox", "superseded_receipts", "reconciliations")


class PacketOutbox:
    """File-backed transactional store. Reopening the file is the restart path."""

    def __init__(
        self,
        path: Path | str,
        *,
        slice_keys: SliceKeyView | None = None,
        allow_generated_fixture: bool = False,
    ):
        if allow_generated_fixture and not _fixture_context():
            raise PacketStoreError(_FIXTURE_REFUSAL)
        self.path = Path(path)
        # Engagement-local slice-digest keys on this worker. ``None``: every stage refuses with
        # ``slice_digest_key_unavailable``; there is no plain-hash fallback.
        self.slice_keys = slice_keys
        self.allow_generated_fixture = allow_generated_fixture
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, isolation_level=None, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    def snapshot(self) -> Json:
        """Every durable row, for proving that a repeat, late or duplicate call changed nothing."""
        with self._db() as db:
            return {
                t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
                for t in _TABLES
            }

    def _fence(self, db: sqlite3.Connection, packet_ids: list[str], reason: str) -> None:
        for pid in packet_ids:
            db.execute(
                "UPDATE packets SET state='superseded', state_reason=? WHERE packet_id=?",
                (reason, pid),
            )
            db.execute(
                "UPDATE outbox SET state='fenced' WHERE packet_id=? AND state IN "
                "('pending','sending')",
                (pid,),
            )

    def stage(
        self,
        comparison: Any,
        receipt: Json | None,
        verdict: Json | None,
        *,
        authority: AuthorityView,
        run_ref: str,
        now: int,
        fault: Callable[[str], None] | None = None,
    ) -> StageOutcome:
        ctx = authority.current(run_ref) if _ID.fullmatch(run_ref or "") else None
        if ctx is None or ctx.run_ref != run_ref:
            return _refuse("authority_unavailable")
        failure = _preflight(comparison, ctx, self.allow_generated_fixture)
        if failure:
            return _refuse(failure)  # before any key lookup
        if self.slice_keys is None:
            return _refuse("slice_digest_key_unavailable")
        try:
            key = self.slice_keys.slice_key(ctx.tenant, ctx.engagement)
        except Exception:
            # An unknown keyring result is never success and never raises out of staging.
            return _refuse("slice_digest_key_lookup_failed")
        packet, reason = assemble_packet(
            comparison,
            receipt,
            verdict,
            ctx,
            slice_key=key,
            allow_generated_fixture=self.allow_generated_fixture,
        )
        if packet is None:
            return _refuse(reason or "comparison_unverified")
        if ctx.expires_at <= now:
            return _refuse("packet_expired")
        event = event_for(packet)
        pid = event["packet_ref"]["packet_id"]
        with self._db() as db:
            if (
                receipt
                and db.execute(
                    "SELECT 1 FROM superseded_receipts WHERE receipt_id=?",
                    (receipt.get("receipt_id"),),
                ).fetchone()
            ):
                return _refuse("audit_receipt_superseded")
            prior = db.execute(
                "SELECT packet_id, packet, state FROM packets WHERE run_ref=? AND "
                "proposal_version=?",
                (run_ref, ctx.proposal_version),
            ).fetchone()
            if prior is not None:
                if prior["packet_id"] == pid and prior["state"] == "staged":
                    return StageOutcome("already_staged", None, packet, event["event_id"])
                return _refuse("restage_conflict")
            older = db.execute(
                "SELECT packet_id FROM packets WHERE run_ref=? AND state='staged' AND "
                "benchmark_digest<>?",
                (run_ref, ctx.benchmark_digest),
            ).fetchall()
            self._fence(db, [r[0] for r in older], "benchmark_changed")
            db.execute(
                "INSERT INTO packets VALUES(?,?,?,?,?,?,'staged',NULL)",
                (
                    pid,
                    run_ref,
                    ctx.proposal_version,
                    ctx.benchmark_digest,
                    receipt.get("receipt_id") if receipt else None,
                    canonical(packet),
                ),
            )
            if fault:
                fault("before_outbox")
            db.execute(
                "INSERT INTO outbox VALUES(?,?,?,?,?,'pending',0,NULL)",
                (
                    event["resource"],
                    event["event_id"],
                    event["event_revision"],
                    pid,
                    canonical(event),
                ),
            )
            if fault:
                fault("before_commit")
        return StageOutcome("staged", None, packet, event["event_id"])

    def packet_status(self, packet_id: str) -> str:
        """What an approval consumer reads: only ``current`` may be acted on."""
        with self._db() as db:
            row = db.execute("SELECT state FROM packets WHERE packet_id=?", (packet_id,)).fetchone()
        if row is None:
            return "unknown"
        return "current" if row[0] == "staged" else str(row[0])

    def supersede_benchmark(self, run_ref: str, benchmark_digest: str) -> list[str]:
        """A changed benchmark supersedes every packet bound to a different one."""
        with self._db() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT packet_id FROM packets WHERE run_ref=? AND state='staged' AND "
                    "benchmark_digest<>?",
                    (run_ref, benchmark_digest),
                )
            ]
            self._fence(db, ids, "benchmark_changed")
        return ids

    def record_receipt_supersession(
        self, receipt_id: str, successor: str | None = None
    ) -> list[str]:
        """Block new packets on this receipt and reconcile every earlier disclosure once."""
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO superseded_receipts VALUES(?,?)", (receipt_id, successor)
            )
            rows = db.execute(
                "SELECT packet_id FROM packets WHERE receipt_id=?", (receipt_id,)
            ).fetchall()
            for (pid,) in rows:
                # 'sending' is an uncertain delivery: it may have disclosed, so treat it as such.
                delivered = db.execute(
                    "SELECT 1 FROM outbox WHERE packet_id=? AND state IN ('delivered','sending')",
                    (pid,),
                ).fetchone()
                db.execute(
                    "INSERT OR IGNORE INTO reconciliations VALUES(?,?,?,'open')",
                    (pid, receipt_id, int(delivered is not None)),
                )
            self._fence(db, [r[0] for r in rows], "audit_receipt_superseded")
        return [r[0] for r in rows]

    def reconciliations(self) -> list[Json]:
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM reconciliations ORDER BY 1")]

    def dispatch(
        self,
        sink: EventSink,
        authority: AuthorityView,
        *,
        now: int,
        fault: Callable[[str], None] | None = None,
    ) -> list[tuple[str, str]]:
        """Deliver each live outbox row with its stable key. An unknown result stays pending."""
        with self._db() as db:
            todo = db.execute(
                "SELECT o.resource, o.event_id, o.event_revision, o.event, o.packet_id, p.packet "
                "FROM outbox o JOIN packets p USING(packet_id) "
                "WHERE o.state IN ('pending','sending') ORDER BY o.resource, o.event_id"
            ).fetchall()
        results = []
        for row in todo:
            key = (row["resource"], row["event_id"], row["event_revision"])
            results.append(
                (row["event_id"], self._dispatch_one(row, key, sink, authority, now, fault))
            )
        return results

    def _dispatch_one(
        self,
        row: sqlite3.Row,
        key: tuple[str, str, int],
        sink: EventSink,
        authority: AuthorityView,
        now: int,
        fault: Callable[[str], None] | None,
    ) -> str:
        packet = json.loads(row["packet"])
        if packet["kind"] == "measured_improvement" and not (
            self.allow_generated_fixture and packet["claim"]["evidence_tier"] == "generated"
        ):
            with self._db() as db:
                self._fence(db, [row["packet_id"]], "audit_and_verdict_issuers_unverified")
            return "fenced"
        bound = packet["binding"]
        current = authority.current(bound["run_ref"])
        if current is None:
            return "authority_unavailable"
        if bound["expires_at"] <= now or current.as_json() != bound:
            reason = "packet_expired" if bound["expires_at"] <= now else "binding_changed"
            with self._db() as db:
                self._fence(db, [row["packet_id"]], reason)
            return "fenced"
        with self._db() as db:
            live = db.execute(
                "UPDATE outbox SET state='sending', attempts=attempts+1 WHERE resource=? AND "
                "event_id=? AND event_revision=? AND state IN ('pending','sending')",
                key,
            ).rowcount
        if not live:
            return "skipped"
        try:
            sink.deliver(json.loads(row["event"]))
            if fault:
                fault("after_deliver")
        except Exception:
            return "uncertain"  # never inferred as success; the next dispatch retries the key
        with self._db() as db:
            db.execute(
                "UPDATE outbox SET state='delivered' WHERE resource=? AND event_id=? AND "
                "event_revision=? AND state='sending'",
                key,
            )
        return "delivered"

    def acknowledge(
        self, resource: str, event_id: str, event_revision: int, receipt_ref: str
    ) -> str:
        """Consumer callback. Late, duplicate, fenced or unknown callbacks change nothing."""
        with self._db() as db:
            row = db.execute(
                "SELECT state FROM outbox WHERE resource=? AND event_id=? AND event_revision=?",
                (resource, event_id, event_revision),
            ).fetchone()
            if row is None:
                return "unknown_ignored"
            if row[0] == "delivered":
                return "duplicate_ignored"
            if row[0] == "fenced":
                return "fenced_ignored"
            if row[0] != "sending":
                return "not_dispatched_ignored"
            if not isinstance(receipt_ref, str) or not _ID.fullmatch(receipt_ref):
                return "invalid_receipt_ignored"
            db.execute(
                "UPDATE outbox SET state='delivered', receipt_ref=? WHERE resource=? AND "
                "event_id=? AND event_revision=?",
                (receipt_ref, resource, event_id, event_revision),
            )
        return "acknowledged"
