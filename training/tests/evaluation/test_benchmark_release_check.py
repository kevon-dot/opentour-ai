"""OPE-526 release consumer: the worker refuses a receipt that is not allowed, stale or superseded.

Fixture kind: generated. The check opens the worker store itself and takes no ledger, event list,
"current" bindings, verdict or boolean from the caller. Receipts and the event log are unsigned
(OPE-566/OPE-582), so even a receipt that passes every check is held and release is never
permitted in this revision.

The ``test_m1_*`` cases re-create the review repros against b3f51b0, where the caller handed the
check an in-memory ledger and the "current" bindings: a ledger replayed from before a
supersession, a ledger with the supersession event dropped, a ledger with the status flipped, and
a post-freeze relabel with the frozen bindings echoed as current. Each returned only the issuer
hold there. Here each is a store-level attack and each is refused.
"""

from __future__ import annotations

import inspect
import os
import shutil
import sqlite3
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest
from audit_fixtures import (
    ANSWER_KEY,
    BINDING_MATERIALS,
    CANONICAL,
    CONSUMED,
    SCORING,
    SOURCES,
    baseline_records,
    commit,
    inputs_for,
)
from ope526_run_inventory import POPULATIONS
from opentour_training.evaluation.benchmark_audit import audit as audit_module
from opentour_training.evaluation.benchmark_audit import worker_store
from opentour_training.evaluation.benchmark_audit.audit import audit
from opentour_training.evaluation.benchmark_audit.ledger import AuditLedger, LedgerError
from opentour_training.evaluation.benchmark_audit.model import (
    BindingDigests,
    FamilyRecord,
    SamplingDesign,
    Verdict,
    canonical_digest,
)
from opentour_training.evaluation.benchmark_audit.release_check import (
    HOLD_CODES,
    REFUSAL_CODES,
    ReleaseCheck,
    _superseded,
    check_receipt_for_release,
    receipt_body_digest,
)
from opentour_training.evaluation.benchmark_audit.worker_store import (
    BASIS_ASSERTED,
    HEAD_FILE,
    LEDGER_FILE,
    MATERIAL_BINDINGS,
    WorkerAuditStore,
)

V = Verdict
BENCH = "generated-benchmark-1"
UNSIGNED = ("audit_ledger_unsigned", "audit_receipt_issuer_unauthenticated")
GENERATED_HOLDS = (*UNSIGNED, "generated_fixture_evidence_only")


def new_store(
    tmp_path: Path,
    records: tuple[FamilyRecord, ...] | None = None,
    *,
    name: str = "store",
    evidence_kind: Any = "generated_sentinel",
    **kw: Any,
) -> WorkerAuditStore:
    store = WorkerAuditStore.create(tmp_path / name)
    kw.setdefault("materials", BINDING_MATERIALS)
    store.set_benchmark(
        "benchmark:1", BENCH, records if records is not None else baseline_records(),
        evidence_kind=evidence_kind, **kw,
    )  # fmt: skip
    return store


def issued(tmp_path: Path, receipt: dict[str, Any], **kw: Any) -> WorkerAuditStore:
    store = new_store(tmp_path, **kw)
    store.issue(f"issue:{receipt['receipt_id']}", receipt, benchmark_id=BENCH)
    return store


def check(receipt: dict[str, Any], store: WorkerAuditStore) -> ReleaseCheck:
    return check_receipt_for_release(receipt["receipt_id"], store_directory=store.directory)


def reseal(receipt: dict[str, Any]) -> dict[str, Any]:
    """Recompute a self-consistent digest, as a forger without a key could."""
    body = {k: v for k, v in receipt.items() if k not in {"receipt_digest", "receipt_id"}}
    digest = receipt_body_digest(body)
    return {**body, "receipt_digest": digest, "receipt_id": f"audit-{digest[:16]}"}


def raw(store: WorkerAuditStore) -> sqlite3.Connection:
    """Direct write access to the store file, as an attacker with file access has."""
    db = sqlite3.connect(store.directory / LEDGER_FILE, isolation_level=None)
    for name in ("events_append_only_update", "events_append_only_delete"):
        db.execute(f"DROP TRIGGER IF EXISTS {name}")
    return db


def allowed() -> dict[str, Any]:
    receipt = audit(inputs_for(baseline_records()))
    assert receipt["verdict"] == V.EVALUATION_ALLOWED.value
    return receipt


def superseded_pair(tmp_path: Path) -> tuple[WorkerAuditStore, dict[str, Any], dict[str, Any]]:
    old = allowed()
    store = issued(tmp_path, old)
    fresh = tuple(replace(r, fresh_confirmation=True) for r in baseline_records())
    successor = audit(inputs_for(fresh, successor_of=old["receipt_id"]))
    assert successor["verdict"] == V.EVALUATION_ALLOWED.value
    return store, old, successor


# ---- M1 repros (each returned only the issuer hold at b3f51b0) ----


def test_m1_ledger_replayed_from_before_the_supersession_is_refused(tmp_path: Path) -> None:
    store, old, successor = superseded_pair(tmp_path)
    before = tmp_path / "before-supersede"
    shutil.copytree(store.directory, before)
    store.supersede("supersede:1", old["receipt_id"], successor, benchmark_id=BENCH)
    assert check(old, store).refusals == ("audit_receipt_superseded",)
    # Swap the earlier copy in under the configured path. The live store is moved aside, not
    # deleted, so the copy cannot reuse its inodes (see the inode-reuse residual in
    # worker_store's docstring).
    os.rename(store.directory, tmp_path / "moved-aside")
    shutil.copytree(before, store.directory)
    assert check(old, store).refusals == ("audit_store_identity_mismatch",)
    # The earlier copy, opened where it is, is a different store from the one that issued.
    restored = check_receipt_for_release(old["receipt_id"], store_directory=before)
    assert restored.refusals == ("audit_store_identity_mismatch",)


def test_m1_in_place_rollback_of_the_log_is_refused_by_the_stored_head(tmp_path: Path) -> None:
    store, old, successor = superseded_pair(tmp_path)
    snapshot = (store.directory / LEDGER_FILE).read_bytes()
    store.supersede("supersede:1", old["receipt_id"], successor, benchmark_id=BENCH)
    inode = os.stat(store.directory / LEDGER_FILE).st_ino
    with open(store.directory / LEDGER_FILE, "r+b") as handle:  # same inode, older content
        handle.write(snapshot)
        handle.truncate()
    assert os.stat(store.directory / LEDGER_FILE).st_ino == inode
    assert check(old, store).refusals == ("audit_ledger_head_mismatch",)


def test_m1_dropped_supersession_event_is_refused(tmp_path: Path) -> None:
    store, old, successor = superseded_pair(tmp_path)
    store.supersede("supersede:1", old["receipt_id"], successor, benchmark_id=BENCH)
    db = raw(store)
    db.execute("DELETE FROM events WHERE seq = (SELECT MAX(seq) FROM events)")
    db.close()
    assert check(old, store).refusals == ("audit_ledger_head_mismatch",)
    db = raw(store)
    db.execute("DELETE FROM events WHERE seq = 2")  # a gap in the middle of the chain
    db.close()
    assert check(old, store).refusals == ("audit_ledger_chain_broken",)


def test_m1_dropped_event_with_a_renumbered_chain_is_refused(tmp_path: Path) -> None:
    store, old, successor = superseded_pair(tmp_path)
    store.supersede("supersede:1", old["receipt_id"], successor, benchmark_id=BENCH)
    db = raw(store)
    db.execute("DELETE FROM events WHERE seq = 3")  # successor issuance
    db.execute("UPDATE events SET seq = 3 WHERE seq = 4")
    db.close()
    assert check(old, store).refusals == ("audit_ledger_chain_broken",)


def test_m1_flipped_status_is_refused(tmp_path: Path) -> None:
    store, old, successor = superseded_pair(tmp_path)
    store.supersede("supersede:1", old["receipt_id"], successor, benchmark_id=BENCH)
    db = raw(store)
    text = db.execute("SELECT event FROM events WHERE seq = 4").fetchone()[0]
    assert '"kind":"superseded"' in text
    flipped = text.replace(old["receipt_id"], "audit-0000000000000000")
    db.execute("UPDATE events SET event = ? WHERE seq = 4", (flipped,))
    db.close()
    assert check(old, store).refusals == ("audit_ledger_chain_broken",)


def test_m1_relabel_after_freeze_is_stale_whatever_the_caller_believes(tmp_path: Path) -> None:
    receipt = allowed()
    store = issued(tmp_path, receipt)
    relabeled = tuple(
        replace(r, final_label_digest=commit(r.family_id, "generated-changed-action"))
        if r.family_id == "generated-r03"
        else r
        for r in baseline_records()
    )
    store.set_benchmark(
        "benchmark:2", BENCH, relabeled, evidence_kind="generated_sentinel",
        materials=BINDING_MATERIALS,
    )  # fmt: skip
    result = check(receipt, store)
    assert result.refusals == ("audit_receipt_stale",)
    # There is no parameter through which a caller could echo the frozen bindings as current.
    with pytest.raises(TypeError):
        check_receipt_for_release(  # type: ignore[call-arg]
            receipt["receipt_id"],
            store_directory=store.directory,
            current=BindingDigests(**receipt["bindings"]["frozen"]),
        )


def test_m1_caller_cannot_hand_the_check_a_ledger_or_bindings() -> None:
    params = inspect.signature(check_receipt_for_release).parameters
    assert list(params) == ["receipt_id", "store_directory"]
    receipt = allowed()
    ledger = AuditLedger()
    ledger.issue("issue:1", receipt)
    with pytest.raises(TypeError):
        check_receipt_for_release(receipt["receipt_id"], ledger=ledger)  # type: ignore[call-arg]


# ---- Ordinary decisions ----


def test_allowed_current_receipt_passes_every_worker_check_but_is_held_unsigned(
    tmp_path: Path,
) -> None:
    receipt = allowed()
    result = check(receipt, issued(tmp_path, receipt))
    assert result.refusals == ()
    assert result.outcome == "held"
    assert result.holds == GENERATED_HOLDS
    assert result.release_permitted is False
    assert result.verified_receipt_digest == receipt["receipt_digest"]
    as_json = result.as_json()
    assert as_json["issuer_authenticated"] is False and as_json["ledger_signed"] is False
    assert as_json["integrity"] == "digest_self_consistent_only"
    basis = as_json["decision_basis"]
    assert basis["bindings_recomputed_by_worker"] == sorted(f.name for f in fields(BindingDigests))
    assert basis["bindings_caller_asserted"] == [] and basis["bindings_absent"] == []
    assert basis["event_log"] == "sha256_hash_chain_to_worker_stored_head_unsigned"


def test_fixture_materials_recompute_the_fixture_binding_digests() -> None:
    expected = {
        "canonical_inputs": CANONICAL, "source_revisions": SOURCES,
        "scoring_policy": SCORING, "answer_key": ANSWER_KEY,
    }  # fmt: skip
    assert {k: canonical_digest(v) for k, v in BINDING_MATERIALS.items()} == expected


VERIFIER_EDITS: dict[str, Any] = {
    "fresh_confirmation": lambda r: replace(r, fresh_confirmation=True),
    "adjudicator_id": lambda r: replace(r, adjudicator_id="generated-adjudicator-c"),
    "unresolved": lambda r: replace(r, unresolved=True),
    "final_label_digest": lambda r: replace(r, final_label_digest=commit(r.family_id, "other")),
}


@pytest.mark.parametrize("edit", sorted(VERIFIER_EDITS))
def test_worker_recompute_is_the_audit_binding_function_not_a_copy(
    tmp_path: Path, edit: str
) -> None:
    """The store calls audit's own digest functions, so it agrees whatever fields they bind."""
    assert worker_store.label_manifest_digest is audit_module.label_manifest_digest
    assert worker_store.split_manifest_digest is audit_module.split_manifest_digest
    edited = tuple(
        VERIFIER_EDITS[edit](r) if r.split == "protected" else r for r in baseline_records()
    )
    store = new_store(tmp_path, edited)
    worker, _ = store.state().benchmarks[BENCH].bindings()
    expected = audit_module.binding_digests(
        edited, canonical_inputs=CANONICAL, source_revisions=SOURCES,
        scoring_policy=SCORING, answer_key=ANSWER_KEY,
    )  # fmt: skip
    assert worker == {f.name: getattr(expected, f.name) for f in fields(BindingDigests)}


@pytest.mark.parametrize("field", MATERIAL_BINDINGS)
def test_a_caller_asserted_binding_is_labelled_and_never_counts_as_recomputed(
    tmp_path: Path, field: str
) -> None:
    receipt = allowed()
    materials = {k: v for k, v in BINDING_MATERIALS.items() if k != field}
    asserted = {field: canonical_digest(BINDING_MATERIALS[field])}
    store = issued(tmp_path, receipt, materials=materials, asserted_digests=asserted)
    result = check(receipt, store)
    assert result.refusals == ()
    assert "audit_binding_caller_asserted" in result.holds
    as_json = result.as_json()
    assert as_json["binding_basis"][field] == BASIS_ASSERTED
    assert as_json["decision_basis"]["bindings_caller_asserted"] == [field]
    assert field not in as_json["decision_basis"]["bindings_recomputed_by_worker"]


@pytest.mark.parametrize("field", MATERIAL_BINDINGS)
def test_a_binding_the_worker_holds_nothing_for_is_refused(tmp_path: Path, field: str) -> None:
    receipt = allowed()
    materials = {k: v for k, v in BINDING_MATERIALS.items() if k != field}
    result = check(receipt, issued(tmp_path, receipt, materials=materials))
    assert result.refusals == ("audit_binding_unavailable_to_worker",)
    assert result.as_json()["decision_basis"]["bindings_absent"] == [field]


@pytest.mark.parametrize("field", MATERIAL_BINDINGS)
def test_changed_material_makes_the_receipt_stale(tmp_path: Path, field: str) -> None:
    receipt = allowed()
    materials = {**BINDING_MATERIALS, field: "generated-moved-material"}
    result = check(receipt, issued(tmp_path, receipt, materials=materials))
    assert result.refusals == ("audit_receipt_stale",)


def test_changed_split_in_worker_records_makes_the_receipt_stale(tmp_path: Path) -> None:
    receipt = allowed()
    moved = tuple(
        replace(r, split="development") if r.family_id == "generated-r01" else r
        for r in baseline_records()
    )
    result = check(receipt, issued(tmp_path, receipt, records=moved))
    assert result.refusals == ("audit_receipt_stale",)


NOT_ALLOWED = {
    "claim_limited": (
        lambda: audit(
            inputs_for(
                tuple(
                    r
                    for r in baseline_records()
                    if r.split == "development" or r.outcome_kind == "correction"
                ),
                prereg_overrides={"sampling_design": SamplingDesign.CORRECTION_ONLY},
            )
        ),
        "audit_claim_limited_to_named_population",
        lambda: tuple(
            r
            for r in baseline_records()
            if r.split == "development" or r.outcome_kind == "correction"
        ),
    ),
    "starter_only": (
        lambda: audit(
            inputs_for((), prereg_overrides={"sampling_design": SamplingDesign.POLICY_ONLY})
        ),
        "audit_verdict_starter_only",
        lambda: (),
    ),
    "insufficient": (
        lambda: audit(inputs_for(baseline_records(), preprocessing=None)),
        "audit_verdict_insufficient_evidence",
        baseline_records,
    ),
    "invalid": (
        lambda: audit(
            inputs_for(baseline_records(), consumed_features=(*CONSUMED, "refund_settled_at"))
        ),
        "audit_verdict_invalid",
        baseline_records,
    ),
}


@pytest.mark.parametrize("name", sorted(NOT_ALLOWED))
def test_every_verdict_other_than_evaluation_allowed_is_refused(tmp_path: Path, name: str) -> None:
    build, code, records = NOT_ALLOWED[name]
    receipt = build()
    assert receipt["verdict"] != V.EVALUATION_ALLOWED.value
    result = check(receipt, issued(tmp_path, receipt, records=records()))
    assert result.refusals == (code,)
    assert result.outcome == "refused" and result.release_permitted is False
    assert result.holds == ()


def test_post_freeze_relabel_stale_receipt_is_refused(tmp_path: Path) -> None:
    frozen = baseline_records()
    relabeled = tuple(
        replace(r, final_label_digest=commit(r.family_id, "generated-changed-action"))
        if r.family_id == "generated-r03"
        else r
        for r in frozen
    )
    receipt = audit(inputs_for(relabeled, frozen_records=frozen))
    assert receipt["verdict"] == V.STALE.value
    result = check(receipt, issued(tmp_path, receipt, records=relabeled))
    assert result.refusals == ("audit_receipt_stale",)


def test_receipt_superseded_in_the_worker_store_is_refused_and_successor_is_checked(
    tmp_path: Path,
) -> None:
    store, old, successor = superseded_pair(tmp_path)
    # The successor was audited over freshly confirmed records, so the worker's benchmark state
    # moves to those records first; its bindings are then recomputed from them. (Once the label
    # manifest also binds verifier evidence such as fresh_confirmation, PR #219, the old
    # receipt is stale against these records as well as superseded.)
    fresh = tuple(replace(r, fresh_confirmation=True) for r in baseline_records())
    store.set_benchmark(
        "benchmark:fresh", BENCH, fresh, evidence_kind="generated_sentinel",
        materials=BINDING_MATERIALS,
    )  # fmt: skip
    store.supersede("supersede:1", old["receipt_id"], successor, benchmark_id=BENCH)
    stale_old = check(old, store)
    assert "audit_receipt_superseded" in stale_old.refusals
    assert set(stale_old.refusals) <= {"audit_receipt_superseded", "audit_receipt_stale"}
    assert store.state().ledger.promotion_allowed(old["receipt_id"]) is False
    new = check(successor, store)
    assert new.refusals == () and new.release_permitted is False
    assert new.holds == GENERATED_HOLDS


def test_status_other_than_current_alone_is_superseded() -> None:
    """R7: the status test stands on its own, without a successor link or a named successor."""
    receipt = allowed()
    ledger = AuditLedger()
    ledger.issue("issue:1", receipt)
    rid = receipt["receipt_id"]
    assert _superseded(ledger, rid, receipt) is False
    ledger.status[rid] = "archived_superseded"
    assert rid not in ledger.successor and receipt["superseded_by"] is None
    assert _superseded(ledger, rid, receipt) is True
    del ledger.status[rid]
    assert _superseded(ledger, rid, receipt) is True


def test_successor_link_alone_is_superseded() -> None:
    receipt = allowed()
    ledger = AuditLedger()
    ledger.issue("issue:1", receipt)
    ledger.successor[receipt["receipt_id"]] = "audit-generated-next"
    assert ledger.status[receipt["receipt_id"]] == "current"
    assert _superseded(ledger, receipt["receipt_id"], receipt) is True


def test_receipt_that_names_its_own_successor_is_refused(tmp_path: Path) -> None:
    receipt = audit(inputs_for(baseline_records(), superseded_by="generated-successor"))
    result = check(receipt, issued(tmp_path, receipt))
    assert "audit_receipt_superseded" in result.refusals
    assert "audit_receipt_stale" in result.refusals


def test_receipt_unknown_to_the_worker_store_is_refused(tmp_path: Path) -> None:
    receipt = allowed()
    result = check(receipt, new_store(tmp_path))
    assert result.refusals == ("audit_receipt_unknown_to_worker",)
    assert result.verified_receipt_digest is None


def test_missing_or_foreign_store_directory_is_refused(tmp_path: Path) -> None:
    receipt = allowed()
    missing = check_receipt_for_release(receipt["receipt_id"], store_directory=tmp_path / "none")
    assert missing.refusals == ("audit_store_unavailable",)
    assert missing.as_json()["decision_basis"]["receipt_and_status"] == (
        "worker_store_not_verified_nothing_read"
    )
    store = issued(tmp_path, receipt)
    link = tmp_path / "link"
    link.symlink_to(store.directory)
    linked = check_receipt_for_release(receipt["receipt_id"], store_directory=link)
    assert linked.refusals == ("audit_store_unavailable",)


def test_replaced_head_file_is_refused(tmp_path: Path) -> None:
    receipt = allowed()
    store = issued(tmp_path, receipt)
    (store.directory / HEAD_FILE).write_text("{}\n")
    assert check(receipt, store).refusals == ("audit_ledger_head_mismatch",)


def test_append_refuses_to_extend_a_log_that_no_longer_matches_its_head(tmp_path: Path) -> None:
    receipt = allowed()
    store = issued(tmp_path, receipt)
    db = raw(store)
    db.execute("DELETE FROM events WHERE seq = (SELECT MAX(seq) FROM events)")
    db.close()
    with pytest.raises(ValueError, match="audit_ledger_head_mismatch"):
        store.log_disclosure("disclosure:1", receipt["receipt_id"], ["generated-r01"])


def test_log_triggers_refuse_update_and_delete(tmp_path: Path) -> None:
    store = issued(tmp_path, allowed())
    db = sqlite3.connect(store.directory / LEDGER_FILE, isolation_level=None)
    for statement in ("DELETE FROM events", "UPDATE events SET event = '{}'", "DELETE FROM meta"):
        with pytest.raises(sqlite3.DatabaseError, match="append_only"):
            db.execute(statement)
    db.close()


def test_store_refuses_to_issue_a_receipt_that_is_not_self_consistent(tmp_path: Path) -> None:
    receipt = audit(inputs_for(baseline_records(), unlogged_disclosures=1))
    store = new_store(tmp_path)
    for edit in ({"verdict": V.EVALUATION_ALLOWED.value}, {"reasons": []}):
        with pytest.raises(LedgerError):
            store.issue("issue:x", {**receipt, **edit}, benchmark_id=BENCH)
    with pytest.raises(LedgerError):
        store.issue("issue:y", receipt, benchmark_id="generated-unknown-benchmark")


def test_edited_receipt_written_past_the_store_api_fails_integrity(tmp_path: Path) -> None:
    receipt = audit(inputs_for(baseline_records(), unlogged_disclosures=1))
    store = new_store(tmp_path)
    edited = {**receipt, "verdict": V.EVALUATION_ALLOWED.value, "reasons": []}
    # A writer that skips issue()'s own check still cannot get the edit past the release check.
    store._append([{"key": "issue:1", "kind": "issued", "receipt": edited, "benchmark_id": BENCH}])
    result = check(receipt, store)
    assert result.refusals == ("audit_receipt_integrity_failed",)
    assert result.release_permitted is False


def test_issuance_sealed_in_another_store_is_refused(tmp_path: Path) -> None:
    """An event grafted from another store, with the chain and head rebuilt, keeps its seal."""
    receipt = allowed()
    other = issued(tmp_path, receipt, name="other")
    grafted = next(e for e in other.events() if e["kind"] == "issued")
    store = new_store(tmp_path)
    state = store.state()
    seq = state.seq + 1
    link = worker_store._link(state.head, seq, grafted)
    db = raw(store)
    db.execute(
        "INSERT INTO events (seq, event, prev, head) VALUES (?, ?, ?, ?)",
        (seq, worker_store._canonical_text(grafted), state.head, link),
    )
    db.close()
    worker_store._write_head(store.directory, state.store_id, seq, link)
    assert grafted["store_id"] == other.store_id != store.store_id
    assert check(receipt, store).refusals == ("audit_store_identity_mismatch",)
    assert check(receipt, other).refusals == ()


def test_a_whole_log_transplanted_into_another_store_is_refused(tmp_path: Path) -> None:
    """The chain starts from a genesis bound to the store id, so a log cannot be moved."""
    source = new_store(tmp_path, name="source")
    rows = (
        sqlite3.connect(source.directory / LEDGER_FILE)
        .execute("SELECT seq, event, prev, head FROM events ORDER BY seq")
        .fetchall()
    )
    target = WorkerAuditStore.create(tmp_path / "target")
    db = sqlite3.connect(target.directory / LEDGER_FILE, isolation_level=None)
    db.executemany("INSERT INTO events (seq, event, prev, head) VALUES (?, ?, ?, ?)", rows)
    db.close()
    last_seq, _, _, last_head = rows[-1]
    target_id = worker_store.store_identity(target.directory)
    worker_store._write_head(target.directory, target_id, last_seq, last_head)
    assert target_id != source.store_id
    with pytest.raises(worker_store.StoreError, match="audit_ledger_chain_broken"):
        target.state()


def test_hardlinked_store_file_in_another_directory_is_refused(tmp_path: Path) -> None:
    receipt = allowed()
    store = issued(tmp_path, receipt)
    other = tmp_path / "hardlinked"
    other.mkdir()
    os.link(store.directory / LEDGER_FILE, other / LEDGER_FILE)
    shutil.copy2(store.directory / HEAD_FILE, other / HEAD_FILE)
    result = check_receipt_for_release(receipt["receipt_id"], store_directory=other)
    assert result.refusals == ("audit_store_identity_mismatch",)


def test_forged_meta_id_is_refused(tmp_path: Path) -> None:
    receipt = allowed()
    victim = issued(tmp_path, receipt, name="victim")
    forged = issued(tmp_path, receipt, name="forged")
    db = sqlite3.connect(forged.directory / LEDGER_FILE, isolation_level=None)
    db.execute("DROP TRIGGER meta_fixed_update")
    db.execute("UPDATE meta SET store_id = ?", (victim.store_id,))
    db.close()
    assert check(receipt, forged).refusals == ("audit_store_identity_mismatch",)


def test_residual_a_fresh_attacker_store_passes_with_only_the_holds(tmp_path: Path) -> None:
    """Documented residual: the store vouches for itself and the caller picks the directory.

    A receipt superseded in the worker's store is issued into a fresh store built with the
    public API. Nothing is refused; only the unsigned and generated-evidence holds remain.
    Closing this needs an expected store id from worker configuration, a signed head
    (OPE-566/OPE-582), an external monotonic anchor and the issuing store id in a signed
    receipt. This test pins the gap so that closing it is a deliberate, visible change.
    """
    store, old, successor = superseded_pair(tmp_path)
    store.supersede("supersede:1", old["receipt_id"], successor, benchmark_id=BENCH)
    assert check(old, store).refusals == ("audit_receipt_superseded",)
    attacker = issued(tmp_path, old, name="attacker")
    result = check(old, attacker)
    assert result.refusals == ()
    assert result.holds == GENERATED_HOLDS
    assert result.release_permitted is False


def test_residual_an_attacker_store_claiming_observed_leaves_exactly_the_two_holds(
    tmp_path: Path,
) -> None:
    """The benchmark record's evidence kind is written by whoever writes the store."""
    spec, build = next(p for p in POPULATIONS if p[0].name == "real_sample_standin")
    inputs = build()
    receipt = audit(inputs)
    assert receipt["verdict"] == V.EVALUATION_ALLOWED.value
    assert receipt["evidence_origin"] == "observed"
    attacker = issued(tmp_path, receipt, records=inputs.records, evidence_kind="observed")
    result = check(receipt, attacker)
    assert result.refusals == ()
    assert result.holds == UNSIGNED
    assert result.release_permitted is False
    assert spec.expected_verdict == V.EVALUATION_ALLOWED


def test_resealed_forgery_is_still_held_because_the_issuer_is_unauthenticated(
    tmp_path: Path,
) -> None:
    invalid = audit(inputs_for(baseline_records(), unlogged_disclosures=1))
    forged = reseal({**invalid, "verdict": V.EVALUATION_ALLOWED.value, "reasons": []})
    result = check(forged, issued(tmp_path, forged))
    assert result.refusals == ()
    assert "audit_receipt_issuer_unauthenticated" in result.holds
    assert result.release_permitted is False


def test_unrecognized_verdict_with_consistent_digest_is_refused(tmp_path: Path) -> None:
    forged = reseal({**allowed(), "verdict": "accepted"})
    assert check(forged, issued(tmp_path, forged)).refusals == ("audit_verdict_unrecognized",)


def test_missing_bindings_are_stale(tmp_path: Path) -> None:
    forged = reseal({k: v for k, v in allowed().items() if k != "bindings"})
    assert check(forged, issued(tmp_path, forged)).refusals == ("audit_receipt_stale",)


def test_observed_origin_over_a_generated_benchmark_is_flagged_and_held(tmp_path: Path) -> None:
    forged = reseal({**allowed(), "evidence_origin": "observed"})
    result = check(forged, issued(tmp_path, forged))
    assert result.holds == (
        *UNSIGNED,
        "evidence_origin_contradicts_worker_record",
        "generated_fixture_evidence_only",
    )
    assert result.release_permitted is False


def test_observed_origin_over_an_observed_benchmark_is_still_held_unsigned(
    tmp_path: Path,
) -> None:
    forged = reseal({**allowed(), "evidence_origin": "observed"})
    result = check(forged, issued(tmp_path, forged, evidence_kind="observed"))
    assert result.holds == UNSIGNED
    assert result.release_permitted is False
    # A generated receipt over an observed benchmark record is still generated evidence.
    receipt = allowed()
    plain = check(receipt, issued(tmp_path, receipt, name="s2", evidence_kind="observed"))
    assert plain.holds == GENERATED_HOLDS


def test_promotion_helper_is_not_a_release_check(tmp_path: Path) -> None:
    build, _, records = NOT_ALLOWED["claim_limited"]
    receipt = build()
    store = issued(tmp_path, receipt, records=records())
    assert store.state().ledger.promotion_allowed(receipt["receipt_id"]) is True
    assert check(receipt, store).refusals == ("audit_claim_limited_to_named_population",)


def test_a_later_store_append_cannot_change_a_returned_decision(tmp_path: Path) -> None:
    store, old, successor = superseded_pair(tmp_path)
    result = check(old, store)
    before = result.as_json()
    store.supersede("supersede:1", old["receipt_id"], successor, benchmark_id=BENCH)
    assert result.as_json() == before
    assert check(old, store).refusals == ("audit_receipt_superseded",)


def test_retried_event_keys_are_no_ops_and_replay_is_exact(tmp_path: Path) -> None:
    receipt = allowed()
    store = issued(tmp_path, receipt)
    first = store.state()
    assert store.issue(f"issue:{receipt['receipt_id']}", receipt, benchmark_id=BENCH) is False
    again = store.state()
    assert (again.seq, again.head) == (first.seq, first.head)
    assert again.ledger.state_digest() == first.ledger.state_digest()


def test_malformed_benchmark_state_is_refused(tmp_path: Path) -> None:
    store = WorkerAuditStore.create(tmp_path / "store")
    bad: list[dict[str, Any]] = [
        {"materials": {"unknown_binding": "x"}},
        {"materials": BINDING_MATERIALS, "asserted_digests": {"answer_key": ANSWER_KEY}},
        {"evidence_kind": "customer"},
    ]
    for kw in bad:
        kw.setdefault("evidence_kind", "generated_sentinel")
        with pytest.raises(LedgerError):
            store.set_benchmark("benchmark:1", BENCH, baseline_records(), **kw)
    assert store.state().seq == 0


def test_outcome_codes_are_closed_vocabularies() -> None:
    assert ReleaseCheck("r", ("audit_verdict_invalid",), (), None).outcome == "refused"
    assert set(REFUSAL_CODES).isdisjoint(HOLD_CODES)
    for _, code, _ in NOT_ALLOWED.values():
        assert code in REFUSAL_CODES
