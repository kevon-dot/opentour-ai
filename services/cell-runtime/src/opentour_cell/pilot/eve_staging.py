"""Decide whether an EVE result may be offered for Slack review (OPE-563, OPE-573).

This is the one staging gate. Offering a result for review needs three checks, and no single
module can make all three: the replay worker (``eve_replay``) may import neither the aggregator
nor the evaluator, so it has no staging decision of its own. ``stage_decision`` here requires
all three and reports every reason found, not only the first:

1. Worker rebuild. The result is rebuilt from every arm the journal holds for the manifest
   (re-running the scorer self-check and the attribution checks), and the supplied result must
   equal that rebuild exactly. ``recomputed_digest`` is the digest the caller expects, for example
   the one a reviewer was shown; it is compared with the derived digest and never stands in for
   the derivation. The self-check and the improvement claim are read from the rebuild.
2. Aggregator (``eve_aggregate.reconcile``). Every field of the result is compared with totals
   summed from the journal rows by separately written code, and with the aggregator's own
   literals. This catches an aggregation bug the rebuild shares with ``build_result``.
3. Independent verdict (the ``eve_evaluator`` document). The separate evaluator replayed every
   arm itself, checked each of its predictions against the worker's keyed commitment, and its own
   verdicts agree case by case. The document must name exactly this manifest and this result,
   be ``reproduced`` with no reason, be fresh, carry a qualified scorer whose identity differs
   from the worker's, claim nothing beyond ``generated`` / ``implementation_only``, and recount
   every arm to the totals the aggregator sums from the journal.

A result is stageable only when all three pass and the rebuilt claim is ``observed_net_fixes``.
Being stageable means it may be offered for Slack review; it is never a release or fit
authorization, and the result's own authorization flags stay False.

Evidence, stated precisely. The verdict document reaching this gate is
``digest_self_consistent_only``: without the evaluator's key (OPE-566 signing does not exist yet)
this gate cannot tell the evaluator's document from a consistent forgery written by someone who
can write files on this host. What the document check adds is that a forgery must also agree
with the journal, the aggregator and the rebuild. Everything here sits at ``locally_keyed`` at
best and is not proof to a third party. The expectations below are this module's own literals,
never read back from the modules it checks.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from opentour_cell.pilot import eve_aggregate as ag
from opentour_cell.pilot.eve_replay import (
    AnswerKey,
    ArmRecord,
    ReplayJournal,
    ReplayManifest,
    Scorer,
    build_result,
    scorer_attribution,
)
from opentour_cell.pilot.eve_task import EveRefusal, canonical, digest

EXPECTED_RESULT_SCHEMA = "opentour.pilot.eve-protected-replay.v1"
EXPECTED_RESULT_TIER = "generated"
EXPECTED_CLAIM_SCOPE = "implementation_only"
EXPECTED_VERDICT_SCHEMA = "opentour.pilot.eve-independent-verdict.v1"
_VERDICT_OUTCOMES = frozenset({"reproduced", "held", "integrity_failure"})


@dataclass(frozen=True)
class StageDecision:
    """Whether a result may be offered for Slack review, and every reason it may not.

    ``aggregate_mismatches`` names the result fields the aggregator found inconsistent.
    """

    stageable: bool
    reasons: tuple[str, ...]
    aggregate_mismatches: tuple[str, ...] = ()


def _same(left: Any, right: Any) -> bool:
    try:
        return canonical(left) == canonical(right)
    except (TypeError, ValueError):
        return False


def _rebuild(
    manifest: ReplayManifest, journal: ReplayJournal, scorer: Scorer, key: AnswerKey
) -> dict[str, Any]:
    """Rebuild the result from every arm the journal holds for ``manifest``, one per arm id."""
    arms: dict[str, ArmRecord] = {}
    for record in journal.arms_for(manifest.digest):
        if record.spec.arm_id in arms:
            raise EveRefusal("arm_ambiguous")
        arms[record.spec.arm_id] = record
    if not arms:
        raise EveRefusal("arm_not_journaled")
    return build_result(manifest, arms, scorer=scorer, journal=journal, key=key)


def _worker_reasons(
    result: Mapping[str, Any],
    recomputed_digest: str,
    manifest: ReplayManifest,
    journal: ReplayJournal,
    scorer: Scorer,
    key: AnswerKey,
) -> tuple[list[str], dict[str, Any] | None]:
    """Check 1: the supplied result equals the worker-side rebuild from the journal."""
    try:
        rebuilt = _rebuild(manifest, journal, scorer, key)
    except EveRefusal as refusal:
        return [f"rebuild_refused_{refusal}"], None
    reasons: list[str] = []
    try:
        scorer_attribution(scorer)
    except EveRefusal:
        reasons.append("staging_scorer_unattributed")
    body, derived = rebuilt["body"], rebuilt["result_digest"]
    try:
        supplied_body, supplied_digest = result["body"], result["result_digest"]
        if not (body["scorer"]["selfcheck_passed"] and supplied_body["scorer"]["selfcheck_passed"]):
            reasons.append("scorer_selfcheck_failed")
        if (
            supplied_digest != derived
            or recomputed_digest != derived
            or digest(supplied_body) != derived
        ):
            reasons.append("recompute_mismatch")
        if (
            supplied_body["schema_version"] != EXPECTED_RESULT_SCHEMA
            or supplied_body["evidence_tier"] != EXPECTED_RESULT_TIER
            or supplied_body["claim_scope"] != EXPECTED_CLAIM_SCOPE
        ):
            reasons.append("result_not_generated_implementation_only")
        if (
            supplied_body["fit_authorized"] is not False
            or supplied_body["release_authorized"] is not False
        ):
            reasons.append("authorization_flag_set")
    except (KeyError, TypeError):
        return ["result_malformed"], rebuilt
    if body["improvement_claim"] != "observed_net_fixes":
        reasons.append(f"improvement_{body['improvement_claim']}")
    return reasons, rebuilt


def _aggregate_reasons(
    result: Mapping[str, Any], manifest: ReplayManifest, journal: ReplayJournal
) -> tuple[list[str], tuple[str, ...], dict[str, dict[str, Any]] | None]:
    """Check 2: the aggregator's field-by-field reconcile, and its per-arm totals by spec."""
    try:
        reconciliation = ag.reconcile(result, journal_path=journal.path, manifest=manifest)
        journaled = ag.read_arms(journal.path, manifest)
    except EveRefusal as refusal:
        return [f"aggregate_refused_{refusal}"], (), None
    totals = {spec: ag.arm_totals(manifest, arm) for spec, arm in journaled.items()}
    if reconciliation.consistent:
        return [], (), totals
    return ["aggregate_inconsistent"], reconciliation.mismatches, totals


def _verdict_reasons(
    verdict: Mapping[str, Any] | None,
    *,
    manifest: ReplayManifest,
    rebuilt: Mapping[str, Any] | None,
    journal_totals: Mapping[str, Mapping[str, Any]] | None,
    now: int,
) -> list[str]:
    """Check 3: the independent evaluator reproduced exactly this claim."""
    if verdict is None:
        return ["independent_verdict_missing"]
    if rebuilt is None or journal_totals is None:
        return ["independent_verdict_unbindable"]
    reasons: list[str] = []
    try:
        document = dict(verdict)
        unsealed = {k: v for k, v in document.items() if k != "verdict_digest"}
        if document["verdict_digest"] != digest(unsealed):
            reasons.append("independent_verdict_inconsistent")
        if document["schema_version"] != EXPECTED_VERDICT_SCHEMA:
            reasons.append("independent_verdict_unrecognized")
        if (
            document["manifest_digest"] != manifest.digest
            or document["result_digest"] != rebuilt["result_digest"]
        ):
            reasons.append("independent_verdict_for_another_claim")
        outcome = document["outcome"]
        if outcome not in _VERDICT_OUTCOMES:
            reasons.append("independent_verdict_unrecognized")
        elif outcome != "reproduced":
            reasons.append(f"independent_verdict_{outcome}")
        elif document["reasons"] != []:
            reasons.append("independent_verdict_inconsistent_outcome")
        scorer = document["scorer"]
        worker = {
            "scorer_id": manifest.scorer_id,
            "version": manifest.scorer_version,
            "rubric_revision": manifest.rubric_revision,
        }
        if scorer["qualified"] is not True:
            reasons.append("independent_scorer_unqualified")
        if (
            manifest.scorer_id in (document["evaluator_id"], scorer["scorer_id"])
            or scorer["version"] == manifest.scorer_version
            or scorer["rubric_revision"] != manifest.rubric_revision
            or not _same(document["worker_scorer"], worker)
        ):
            reasons.append("independent_verdict_not_independent")
        if (
            document["evidence_tier"] != EXPECTED_RESULT_TIER
            or document["claim_scope"] != EXPECTED_CLAIM_SCOPE
            or document["custody_separated"] is not False
            or document["verdicts_rechecked"] is not True
        ):
            reasons.append("independent_verdict_overstated")
        issued, not_after = document["issued_at"], document["not_after"]
        if type(issued) is not int or type(not_after) is not int or not issued <= now < not_after:
            reasons.append("independent_verdict_stale")
        body = rebuilt["body"]
        arms = document["arms"]
        eligible = len(manifest.eligible_case_ids)
        if set(arms) != set(body["arms"]):
            reasons.append("independent_verdict_incomplete")
        for name, arm in sorted(arms.items()):
            if (
                arm["replayed"] is not True
                or arm["outputs_committed"] != eligible
                or arm["outputs_not_committed"] != 0
                or arm["verdict_disagreements"] != 0
            ):
                reasons.append("independent_verdict_incomplete")
            spec = arm["spec_digest"]
            want = journal_totals.get(spec) if isinstance(spec, str) else None
            if (
                want is None
                or name not in body["arms"]
                or body["arms"][name]["spec_digest"] != spec
                or not _same(arm["totals"], want)
            ):
                reasons.append("independent_recount_mismatch")
        if not _same(document["paired_comparison"], body["paired_comparison"]) or not _same(
            document["improvement_claim"], body["improvement_claim"]
        ):
            reasons.append("independent_recount_mismatch")
    except (KeyError, TypeError, AttributeError, ValueError):
        return ["independent_verdict_malformed"]
    return sorted(set(reasons))


def stage_decision(
    result: Mapping[str, Any],
    *,
    recomputed_digest: str,
    manifest: ReplayManifest,
    journal: ReplayJournal,
    scorer: Scorer,
    key: AnswerKey,
    verdict: Mapping[str, Any] | None,
    clock: Callable[[], float] = time.time,
) -> StageDecision:
    """Whether ``result`` may be offered for Slack review. Never a release authorization.

    It requires the worker rebuild, the aggregator's ``reconcile`` and the independent verdict
    (see the module notes). A missing verdict is a reason, never a pass.
    """
    worker, rebuilt = _worker_reasons(result, recomputed_digest, manifest, journal, scorer, key)
    aggregate, mismatches, totals = _aggregate_reasons(result, manifest, journal)
    independent = _verdict_reasons(
        verdict, manifest=manifest, rebuilt=rebuilt, journal_totals=totals, now=int(clock())
    )
    reasons = tuple(dict.fromkeys([*worker, *aggregate, *independent]))
    return StageDecision(not reasons, reasons, mismatches)
