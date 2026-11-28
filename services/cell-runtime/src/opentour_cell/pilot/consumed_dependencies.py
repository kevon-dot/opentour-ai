"""Customer-local consumed-dependency manifest, its seal and the training-parent manifest
(OPE-416, worker side).

``DependencyRecorder.record`` is called at the moment a worker artifact actually consumes
fields of one example from a committed as-of snapshot. It writes one immutable record, whether
or not any reader (OPE-544 reverse lookup, OPE-525 recompute, OPE-433 holds) is installed or
running. Nothing here imports those readers at module load: the projections at the bottom
import them lazily, only when called.

What a record holds, and where each value comes from:

* From the committed snapshot (worker-held, never from the caller): tenant, workflow,
  engagement and grant epochs, decision ID, revision and time, each consumed field's source,
  source revision and decision-time availability, verifier and the settings revision that
  admitted it, independent outcome time, partition, root family and split revision.
* From the consuming artifact, labelled ``caller_asserted``: its purpose, kind, reference and
  revision, the rule revision, and any dataset, model/comparator, frozen-result or proposal
  reference that already exists at consumption. These support no claim.

A path the snapshot does not list for that example is never guessed into a consumed edge: it is
recorded under ``unknown_dependencies`` and the example's root family is held at sealing. Later
artifacts attach as separate, versioned relationship files; the original record's bytes never
change, so a later completion cannot alter the inputs the original decision saw.

The seal covers ``dataset_preparation`` records only: an evaluation, rule or comparison record
made after sealing changes nothing in it, so a later record never wedges training parents.
A dataset record and the seal take the same lock, so neither can slip between the other's check
and write.

The recorder pins the settings store's identity on first use, like the producer, and refuses a
mismatch (``settings_store_mismatch``). Consumer-declared proposal references are labelled
``caller_asserted`` wherever they surface, and none is ever projected for OPE-544.

Evidence tier: ``digest_self_consistent_only``. Pins detect a one-file edit; they are not
keyed. Simulated and NOT proven: generated sentinel fixtures in a local directory only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot.candidate_snapshot import (
    DEVELOPMENT,
    DISPUTED,
    PROTECTED,
    VERIFIED,
    PairReader,
    SnapshotProducer,
    label_digest,
    prediction_digest,
)
from opentour_cell.pilot.learning_profile import (
    EVIDENCE_TIER,
    PilotSettings,
    ProfileError,
    SourcePair,
    format_time,
    locked,
    map_row,
    need,
    pin_identity,
    read_pinned,
    ref,
    write_pinned,
)
from opentour_cell.pilot.operating_point import digest

RECORD_SCHEMA = "opentour.pilot.consumed-dependency.v1"
RELATION_SCHEMA = "opentour.pilot.consumed-dependency-relation.v1"
SEAL_SCHEMA = "opentour.pilot.sealed-consumption-manifest.v1"
PARENTS_SCHEMA = "opentour.pilot.training-parent-manifest.v1"

PURPOSES = frozenset({"dataset_preparation", "evaluation", "simple_rule", "comparison"})
CONSUMER_KINDS = frozenset(
    {"dataset", "simple_rule", "local_adapter", "model", "comparator", "proposal"}
)
ARTIFACT_KINDS = frozenset({"dataset_ref", "model_ref", "frozen_result_ref", "proposal_ref"})
LATER_KINDS = frozenset({"outcome", "frozen_result", "proposal", "model", "comparator"})
LABEL_PATH = "outcome/label"
_PATH = re.compile(r"[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)*")


class DependencyRecorder:
    """Write-once consumption records, versioned later relationships and seals."""

    def __init__(
        self,
        directory: Path,
        producer: SnapshotProducer,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self.directory = Path(directory)
        self.producer = producer
        self.clock = clock

    def _base(self, tenant: str, workflow_ref: str) -> Path:
        return (
            self.directory
            / ref(tenant, "dependency_tenant")
            / ref(workflow_ref, "dependency_workflow")
        )

    def _folder(self, tenant: str, workflow_ref: str, kind: str) -> Path:
        return self._base(tenant, workflow_ref) / kind

    def store_identity(self, tenant: str, workflow_ref: str) -> str:
        """The producer's pinned settings store, pinned here too on this recorder's first use."""
        identity = self.producer.store_identity(tenant, workflow_ref)
        base = self._base(tenant, workflow_ref)
        first = not any((base / kind).is_dir() for kind in ("records", "seals", "parents"))
        pin_identity(base, identity, create=first)
        return identity

    # ------------------------------------------------------------------ write on consumption

    def record(
        self,
        tenant: str,
        workflow_ref: str,
        snapshot_digest: str,
        example_ref: str,
        *,
        purpose: str,
        consumer_kind: str,
        consumer_ref: str,
        consumer_revision: str,
        rule_revision: str,
        paths: Iterable[str],
        artifacts: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Persist what was actually consumed. Identical replay returns the same record."""
        need(type(purpose) is str and purpose in PURPOSES, "unknown_purpose")
        need(type(consumer_kind) is str and consumer_kind in CONSUMER_KINDS, "unknown_consumer")
        asserted_artifacts = dict(artifacts or {})
        need(set(asserted_artifacts) <= ARTIFACT_KINDS, "unknown_artifact_kind")
        for value in asserted_artifacts.values():
            ref(value, "unknown_artifact_kind")
        requested = list(paths)
        need(
            all(type(p) is str and _PATH.fullmatch(p) for p in requested)
            and len(set(requested)) == len(requested),
            "invalid_consumed_path",
        )
        snapshot = self.producer.snapshot(tenant, workflow_ref, snapshot_digest)
        need(
            self.store_identity(tenant, workflow_ref) == snapshot["settings_store"],
            "settings_store_mismatch",
        )
        example = next((e for e in snapshot["examples"] if e["example_ref"] == example_ref), None)
        need(example is not None, "example_unknown")
        assert example is not None
        # Disputed rows never enter a dataset, an evaluation or any other consumer.
        need(example["state"] != DISPUTED, "disputed_not_consumable")
        if purpose == "dataset_preparation":
            need(example["eligible"], "example_not_eligible")
        if example["partition"] == PROTECTED:
            need(purpose == "evaluation", "protected_evaluation_only")

        available = {i["path"]: i for i in example["inputs"]}
        correction = example["correction"]
        consumed, labels, unknown = [], [], []
        for path in sorted(requested):
            if path in available:
                item = available[path]
                consumed.append(
                    {
                        "source_ref": item["source_ref"],
                        "path": path,
                        "source_revision": item["source_revision"],
                        "available_at": item["available_at"],
                    }
                )
            elif path == LABEL_PATH and correction is not None and example["state"] == VERIFIED:
                labels.append(
                    {
                        "source_ref": correction["source_ref"],
                        "path": path,
                        "source_revision": correction["source_revision"],
                        "available_at": correction["outcome_time"],
                        "correction_version": correction["version"],
                    }
                )
            else:
                # Never guess an edge: an unlisted path is an unknown dependency.
                unknown.append(path)
        body: dict[str, Any] = {
            "schema": RECORD_SCHEMA,
            "tenant": snapshot["tenant"],
            "workflow_ref": snapshot["workflow_ref"],
            "engagement_epoch": snapshot["engagement_epoch"],
            "grant_epoch": snapshot["grant_epoch"],
            "snapshot_digest": snapshot_digest,
            "settings_store": snapshot["settings_store"],
            "settings_revision": snapshot["settings_revision"],
            "policy_revision": snapshot["policy_revision"],
            "split_revision": snapshot["split_revision"],
            "example_ref": example_ref,
            "origin": example["origin"],
            "case": example["case"],
            "decision": {
                "decision_id": example["decision_id"],
                "decision_revision": example["decision_revision"],
                "decision_time": example["decision_time"],
            },
            "consumed": consumed,
            "label_inputs": labels,
            "unknown_dependencies": unknown,
            "verifier": None
            if correction is None
            else {
                "actor_id": correction["verifier"],
                "settings_revision": correction["settings_revision"],
            },
            "outcome_time": None if correction is None else correction["outcome_time"],
            "state_at_consumption": example["state"],
            "partition": example["partition"],
            "root_family": example["group"],
            "caller_asserted": {
                "purpose": purpose,
                "consumer": {
                    "kind": consumer_kind,
                    "ref": ref(consumer_ref, "unknown_consumer"),
                    "revision": ref(consumer_revision, "unknown_consumer"),
                },
                "rule_revision": ref(rule_revision, "unknown_consumer"),
                "artifacts": dict(sorted(asserted_artifacts.items())),
            },
        }
        record_ref = "dep-" + digest(body)[:32]
        folder = self._folder(tenant, workflow_ref, "records")
        # One lock with ``seal``: the sealed check and the write are a single step.
        with locked(self._base(tenant, workflow_ref)):
            if (folder / f"{record_ref}.json").exists():
                existing = read_pinned(folder, record_ref, "dependency_tampered")
                need(existing["body"] == body, "dependency_conflict")
                return dict(existing)
            if purpose == "dataset_preparation":
                sealed = self._folder(tenant, workflow_ref, "seals")
                need(
                    not (sealed / f"{_seal_name(snapshot_digest)}.json").exists(),
                    "snapshot_sealed",
                )
            stored = {
                "record_ref": record_ref,
                "body": body,
                "recorded_at": format_time(self.clock().astimezone(UTC)),
            }
            write_pinned(folder, record_ref, stored, "dependency_conflict")
        return stored

    def records(
        self, tenant: str, workflow_ref: str, snapshot_digest: str | None = None
    ) -> list[dict[str, Any]]:
        folder = self._folder(tenant, workflow_ref, "records")
        found = []
        paths = sorted(folder.glob("dep-*.json")) if folder.is_dir() else []
        identity = self.store_identity(tenant, workflow_ref) if paths else None
        for path in paths:
            stored = read_pinned(folder, path.stem, "dependency_tampered")
            body = stored["body"]
            need(
                stored["record_ref"] == path.stem == "dep-" + digest(body)[:32]
                and body["tenant"] == tenant
                and body["workflow_ref"] == workflow_ref,
                "dependency_tampered",
            )
            need(body["settings_store"] == identity, "settings_store_mismatch")
            if snapshot_digest is None or body["snapshot_digest"] == snapshot_digest:
                found.append(stored)
        return found

    # ------------------------------------------------------------------ later relationships

    def attach_outcome(
        self, tenant: str, workflow_ref: str, record_ref: str, later_snapshot_digest: str
    ) -> dict[str, Any]:
        """Attach the independent outcome from a later committed snapshot as a new relation.

        The outcome is resolved from the later snapshot (worker-held), never supplied by the
        caller, and must postdate the decision. The original record is not modified.
        """
        record = self._record(tenant, workflow_ref, record_ref)["body"]
        later = self.producer.snapshot(tenant, workflow_ref, later_snapshot_digest)
        example = next(
            (e for e in later["examples"] if e["example_ref"] == record["example_ref"]), None
        )
        need(
            example is not None and example["state"] == VERIFIED and example["correction"],
            "outcome_not_verified",
        )
        assert example is not None
        outcome = example["correction"]
        need(outcome["outcome_time"] > record["decision"]["decision_time"], "outcome_not_later")
        return self._relate(
            tenant,
            workflow_ref,
            record_ref,
            "outcome",
            outcome["correction_ref"],
            outcome["outcome_time"],
            {"snapshot_digest": later_snapshot_digest},
        )

    def attach_artifact(
        self, tenant: str, workflow_ref: str, record_ref: str, kind: str, artifact_ref: str
    ) -> dict[str, Any]:
        """Attach a later artifact; its reference is caller-asserted and labelled so."""
        need(type(kind) is str and kind in LATER_KINDS - {"outcome"}, "unknown_relationship")
        record = self._record(tenant, workflow_ref, record_ref)["body"]
        now = format_time(self.clock().astimezone(UTC))
        need(now > record["decision"]["decision_time"], "relationship_not_later")
        return self._relate(
            tenant,
            workflow_ref,
            record_ref,
            kind,
            ref(artifact_ref, "unknown_relationship"),
            now,
            {"caller_asserted": True},
        )

    def _record(self, tenant: str, workflow_ref: str, record_ref: str) -> dict[str, Any]:
        for stored in self.records(tenant, workflow_ref):
            if stored["record_ref"] == record_ref:
                return stored
        raise ProfileError("dependency_unknown")

    def _relate(
        self,
        tenant: str,
        workflow_ref: str,
        record_ref: str,
        kind: str,
        target: str,
        available_at: str,
        basis: Mapping[str, Any],
    ) -> dict[str, Any]:
        existing = self.relations(tenant, workflow_ref, record_ref)
        for rel in existing:
            if rel["kind"] == kind and rel["ref"] == target:
                return rel
        relation = {
            "schema": RELATION_SCHEMA,
            "record_ref": record_ref,
            "kind": kind,
            "ref": target,
            "available_at": available_at,
            "relationship_version": len(existing) + 1,
            "basis": dict(basis),
        }
        write_pinned(
            self._folder(tenant, workflow_ref, "relations"),
            f"rel-{record_ref}-{relation['relationship_version']:04d}",
            relation,
            "relationship_conflict",
        )
        return relation

    def relations(self, tenant: str, workflow_ref: str, record_ref: str) -> list[dict[str, Any]]:
        folder = self._folder(tenant, workflow_ref, "relations")
        names = sorted(p.stem for p in folder.glob(f"rel-{record_ref}-*.json"))
        found = []
        for position, name in enumerate(names, start=1):
            need(name == f"rel-{record_ref}-{position:04d}", "relationship_tampered")
            relation = read_pinned(folder, name, "relationship_tampered")
            need(relation["relationship_version"] == position, "relationship_tampered")
            found.append(relation)
        return found

    def proposals(
        self, tenant: str, workflow_ref: str, snapshot_digest: str
    ) -> dict[str, set[str]]:
        """Outstanding proposal reference -> example references it actually consumed.

        The proposal references are consumer-declared (``caller_asserted``); the example
        references come from the worker's own records. ``current`` labels them on output.
        """
        out: dict[str, set[str]] = {}
        for stored in self.records(tenant, workflow_ref, snapshot_digest):
            proposal = stored["body"]["caller_asserted"]["artifacts"].get("proposal_ref")
            if proposal:
                out.setdefault(proposal, set()).add(stored["body"]["example_ref"])
        return out

    # ------------------------------------------------------------------ seal

    def seal(self, tenant: str, workflow_ref: str, snapshot_digest: str) -> dict[str, Any]:
        """Seal the redacted forward-consumption manifest of one committed snapshot.

        Redacted: references, paths, revisions and digests only, never a value. Deterministic:
        resealing after a restart yields the same bytes, so the same digest. Each eligible
        development example needs a recorded dataset-preparation consumption; a missing one, or
        an unknown dependency in one, holds that root family only. Only dataset-preparation
        records are sealed: a record for any other purpose, before or after sealing, does not
        change the seal. Protected roots expose aggregate status only.
        """
        with locked(self._base(tenant, workflow_ref)):
            return self._seal(tenant, workflow_ref, snapshot_digest)

    def _seal(self, tenant: str, workflow_ref: str, snapshot_digest: str) -> dict[str, Any]:
        snapshot = self.producer.snapshot(tenant, workflow_ref, snapshot_digest)
        records = [
            r["body"] | {"record_ref": r["record_ref"]}
            for r in self.records(tenant, workflow_ref, snapshot_digest)
            if r["body"]["caller_asserted"]["purpose"] == "dataset_preparation"
        ]
        eligible = [e for e in snapshot["examples"] if e["eligible"]]
        protected = [e for e in snapshot["examples"] if e["partition"] == PROTECTED]
        forward, holds, unknown = [], [], set()
        for example in eligible:
            mine = [r for r in records if r["example_ref"] == example["example_ref"]]
            prepared = mine
            entry_unknown = sorted({p for r in mine for p in r["unknown_dependencies"]})
            unknown.update(entry_unknown)
            reason = None
            if not prepared:
                reason = "consumption_record_missing"
            elif entry_unknown:
                reason = "unknown_dependency"
            if reason:
                holds.append(
                    {
                        "scope": "root_family",
                        "root_family": example["group"],
                        "example_ref": example["example_ref"],
                        "reason": reason,
                    }
                )
            forward.append(
                {
                    "example_ref": example["example_ref"],
                    "root_family": example["group"],
                    "records": [
                        {
                            "record_ref": r["record_ref"],
                            "purpose": r["caller_asserted"]["purpose"],
                            "consumer_kind": r["caller_asserted"]["consumer"]["kind"],
                            "paths": [c["path"] for c in r["consumed"]]
                            + [c["path"] for c in r["label_inputs"]],
                            # Each consumed path with the exact source revision it came from.
                            "fields": [
                                [f"{c['source_ref']}@{c['source_revision']}", c["path"]]
                                for c in r["consumed"] + r["label_inputs"]
                            ],
                        }
                        for r in sorted(mine, key=lambda r: r["record_ref"])
                    ],
                }
            )
        held_families = {h["root_family"] for h in holds}
        body = {
            "schema": SEAL_SCHEMA,
            "tenant": tenant,
            "workflow_ref": workflow_ref,
            "snapshot_digest": snapshot_digest,
            "settings_store": snapshot["settings_store"],
            "covers_purposes": ["dataset_preparation"],
            "settings_revision": snapshot["settings_revision"],
            "policy_revision": snapshot["policy_revision"],
            "benchmark": snapshot["benchmark"],
            "engagement_epoch": snapshot["engagement_epoch"],
            "grant_epoch": snapshot["grant_epoch"],
            "split_revision": snapshot["split_revision"],
            "partitions": {
                DEVELOPMENT: {
                    "digest": digest(sorted([e["example_ref"], e["group"]] for e in eligible)),
                    "root_families": len({e["group"] for e in eligible}),
                },
                PROTECTED: {
                    "digest": digest(sorted([e["example_ref"], e["group"]] for e in protected)),
                    "status": "sealed",
                    "examples": len(protected),
                    "groups": len({e["group"] for e in protected}),
                },
            },
            "forward": sorted(forward, key=lambda f: f["example_ref"]),
            "unknown_dependencies": sorted(unknown),
            "holds": sorted(holds, key=lambda h: (h["root_family"], h["example_ref"])),
            "held_root_families": sorted(held_families),
            "evidence_tier": EVIDENCE_TIER,
        }
        write_pinned(
            self._folder(tenant, workflow_ref, "seals"),
            _seal_name(snapshot_digest),
            body,
            "seal_conflict",
        )
        return {**body, "seal_digest": digest(body)}

    # ------------------------------------------------------------------ training parents

    def training_parents(
        self, tenant: str, workflow_ref: str, snapshot_digest: str, read_pair: PairReader
    ) -> dict[str, Any]:
        """Independently verified observed workplace corrections only, current at read time.

        Counts are published before any generation and come from these real parents alone, so
        unioning synthetic descendants (``union_synthetic``) cannot change them. A reopened or
        withdrawn correction is excluded from future training here even though the as-of
        snapshot still shows what the reviewer inspected, and so is an example whose current
        chosen correction differs from the as-of one (``label_superseded``). A retraction
        recorded in any manifest this worker stored for the snapshot sticks: a later read
        cannot re-admit the example.
        """
        identity = self.store_identity(tenant, workflow_ref)
        sealed = self.seal(tenant, workflow_ref, snapshot_digest)
        current = self.producer.current(
            tenant,
            workflow_ref,
            snapshot_digest,
            read_pair,
            self.proposals(tenant, workflow_ref, snapshot_digest),
        )
        need(current["state"] == "current", "current_eligibility_held")
        need(current["settings_store"] == identity, "settings_store_mismatch")
        snapshot = self.producer.snapshot(tenant, workflow_ref, snapshot_digest)
        by_ref = {e["example_ref"]: e for e in snapshot["examples"]}
        retracted: dict[str, Any] = {}
        for item in current["retracted"]:
            retracted.setdefault(item["example_ref"], item)
        for stored in self._stored_manifests(tenant, workflow_ref, snapshot_digest):
            for item in stored["retracted"]:
                retracted.setdefault(item["example_ref"], item)
        now = set(current["eligible"]) - set(retracted)
        proposals = self.proposals(tenant, workflow_ref, snapshot_digest)
        stale = sorted(p for p, used in proposals.items() if set(used) & set(retracted))
        held = set(sealed["held_root_families"])
        parents = []
        for entry in sealed["forward"]:
            example = by_ref[entry["example_ref"]]
            if entry["example_ref"] not in now or entry["root_family"] in held:
                continue
            parents.append(
                {
                    "example_ref": entry["example_ref"],
                    "root_family": entry["root_family"],
                    "origin": example["origin"],
                    "decision_revision": example["decision_revision"],
                    "correction_version": example["correction"]["version"],
                    "verifier": example["correction"]["verifier"],
                    "label_digest": example["correction"]["label_digest"],
                    "inputs_digest": example["inputs_digest"],
                    "paths": sorted({p for r in entry["records"] for p in r["paths"]}),
                }
            )
        body = {
            "schema": PARENTS_SCHEMA,
            "seal_digest": sealed["seal_digest"],
            "snapshot_digest": snapshot_digest,
            "settings_store": identity,
            "current_settings_revision": current["settings_revision"],
            "parents": parents,
            "counts": {
                "parents": len(parents),
                "independent_root_families": len({p["root_family"] for p in parents}),
                "verified_observed": current["counters"]["live"]["verified"],
            },
            "retracted": [retracted[r] for r in sorted(retracted)],
            "stale_proposals": [{"proposal_ref": p, "basis": "caller_asserted"} for p in stale],
            "holds": sealed["holds"],
            "evidence_tier": EVIDENCE_TIER,
        }
        manifest = {**body, "manifest_digest": digest(body)}
        # Kept write-once by digest, so ``union_synthetic`` can verify a manifest it is handed
        # against what this worker actually produced.
        write_pinned(
            self._folder(tenant, workflow_ref, "parents"),
            _parents_name(manifest["manifest_digest"]),
            manifest,
            "parents_manifest_conflict",
        )
        return manifest

    def _stored_manifests(
        self, tenant: str, workflow_ref: str, snapshot_digest: str
    ) -> list[dict[str, Any]]:
        """Every training-parent manifest this worker stored for one snapshot, re-verified."""
        folder = self._folder(tenant, workflow_ref, "parents")
        found = []
        for path in sorted(folder.glob("parents-*.json")) if folder.is_dir() else []:
            claimed = read_pinned(folder, path.stem, "parents_manifest_tampered")
            need(isinstance(claimed, dict), "parents_manifest_tampered")
            stored = self.parents_manifest(tenant, workflow_ref, claimed.get("manifest_digest"))
            need(path.stem == _parents_name(stored["manifest_digest"]), "parents_manifest_tampered")
            if stored["snapshot_digest"] == snapshot_digest:
                found.append(stored)
        return found

    def parents_manifest(self, tenant: str, workflow_ref: str, manifest_digest: str) -> Any:
        """A training-parent manifest this worker produced, re-verified from its own store."""
        self.store_identity(tenant, workflow_ref)
        folder = self._folder(tenant, workflow_ref, "parents")
        name = _parents_name(manifest_digest)
        need((folder / f"{name}.json").exists(), "parents_manifest_unknown")
        stored = read_pinned(folder, name, "parents_manifest_tampered")
        body = {k: v for k, v in stored.items() if k != "manifest_digest"}
        need(
            stored.get("manifest_digest") == manifest_digest == digest(body),
            "parents_manifest_tampered",
        )
        return stored


def _parents_name(manifest_digest: object) -> str:
    need(
        type(manifest_digest) is str and re.fullmatch(r"[0-9a-f]{64}", manifest_digest),
        "parents_manifest_unknown",
    )
    assert type(manifest_digest) is str
    return f"parents-{manifest_digest[:40]}"


def _seal_name(snapshot_digest: str) -> str:
    need(
        type(snapshot_digest) is str and re.fullmatch(r"[0-9a-f]{64}", snapshot_digest),
        "snapshot_unknown",
    )
    return f"seal-{snapshot_digest[:40]}"


class RootRetractor(Protocol):
    """The part of OPE-516's ``VariantLedger`` that invalidates a root's descendants."""

    def retract_root(self, root_ref: str, family_id: str, reason: str) -> tuple[str, ...]: ...


def propagate_retractions(
    ledger: RootRetractor, parents: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> dict[str, tuple[str, ...]]:
    """Retract every reopened or withdrawn parent in the descendant ledger.

    The root family comes from the committed snapshot, not from the caller. Returns the
    invalidated descendant IDs per retracted example.
    """
    families = {e["example_ref"]: e["group"] for e in snapshot["examples"]}
    out: dict[str, tuple[str, ...]] = {}
    for item in parents["retracted"]:
        family = families.get(item["example_ref"])
        need(type(family) is str, "retracted_root_unpartitioned")
        out[item["example_ref"]] = ledger.retract_root(
            item["example_ref"], str(family), str(item["reason"])
        )
    return out


def union_synthetic(
    recorder: DependencyRecorder,
    tenant: str,
    workflow_ref: str,
    parents: Mapping[str, Any],
    children: Iterable[Mapping[str, Any]],
    *,
    read_pair: PairReader,
) -> dict[str, Any]:
    """Union admitted synthetic descendants into a training set without touching live counts.

    The parents mapping is never trusted as handed in: its digest must match its body and the
    manifest this worker stored under that digest. Authenticity is not currency, so the
    current parents are then recomputed from the worker's own store (``training_parents``
    over the same snapshot, the pinned settings store and the worker's reader), and only a
    child whose root is a parent *now* is admitted. A child of a root that has since been
    reopened or withdrawn is rejected as ``child_of_retracted_root``; any other child without a
    current parent as ``child_without_live_parent``. Live counts come from the recomputed
    manifest.
    """
    claimed = parents.get("manifest_digest") if isinstance(parents, Mapping) else None
    stored = recorder.parents_manifest(tenant, workflow_ref, claimed)  # type: ignore[arg-type]
    need(dict(parents) == stored, "parents_manifest_tampered")
    current = recorder.training_parents(tenant, workflow_ref, stored["snapshot_digest"], read_pair)
    roots = {p["example_ref"] for p in current["parents"]}
    retracted = {r["example_ref"] for r in current["retracted"]} | (
        {p["example_ref"] for p in stored["parents"]} - roots
    )
    admitted, rejected = [], []
    for child in children:
        if not isinstance(child, Mapping):
            rejected.append({"child_ref": None, "reason": "malformed_child"})
            continue
        child_ref, root = child.get("child_ref"), child.get("root_ref")
        if type(child_ref) is not str or type(root) is not str:
            rejected.append({"child_ref": None, "reason": "malformed_child"})
        elif root in roots:
            admitted.append({"child_ref": child_ref, "root_ref": root, "origin": "synthetic"})
        elif root in retracted:
            rejected.append({"child_ref": child_ref, "reason": "child_of_retracted_root"})
        else:
            rejected.append({"child_ref": child_ref, "reason": "child_without_live_parent"})
    parents = current
    return {
        "requested_manifest": stored["manifest_digest"],
        "parent_manifest": parents["manifest_digest"],
        "live_counts": dict(parents["counts"]),
        "synthetic_children": len(admitted),
        "rows": len(parents["parents"]) + len(admitted),
        "admitted": admitted,
        "rejected": rejected,
    }


# --------------------------------------------------------------------------- projections
# Each projection imports its consumer lazily, so the producer runs with every consumer absent.


def lineage_refs(stored: Mapping[str, Any]) -> dict[str, Any]:
    """The worker-derived references an OPE-544 manifest of one record carries.

    Each is minted here from worker-held records only (the committed snapshot digest, the
    example and the record reference), never from anything a consumer declared:

    * ``result_ref``: the committed as-of snapshot the consumption read from;
    * ``candidate_ref``: the verified candidate (the example) inside that snapshot;
    * ``claim_refs``: one claim, "this record consumed exactly these fields".

    Gap: no worker-side producer of OPE-544 workflow claims or frozen results exists yet, so a
    claim made elsewhere (a Slack answer, a frozen result) cannot be projected until one does.
    """
    body = stored["body"]
    snapshot, example = body["snapshot_digest"], body["example_ref"]
    return {
        "result_ref": "asof-" + snapshot[:48],
        "candidate_ref": "cand-"
        + digest(["opentour.pilot.lineage-candidate.v1", snapshot, example])[:32],
        "claim_refs": [
            "claim-"
            + digest(
                [
                    "opentour.pilot.lineage-claim.v1",
                    body["tenant"],
                    body["workflow_ref"],
                    stored["record_ref"],
                ]
            )[:32]
        ],
    }


_LINEAGE_RULE_KINDS = frozenset({"simple_rule", "local_adapter"})


def to_lineage_manifest(
    recorder: DependencyRecorder,
    tenant: str,
    workflow_ref: str,
    record_ref: str,
    *,
    store: Any = None,
    **caller_refs: Any,
) -> dict[str, Any]:
    """Project one record onto OPE-544's ``consumed-field-manifest.v1`` reader shape.

    Only worker-derived references are projected (``lineage_refs``). OPE-544's manifest has no
    field that could mark a value caller-asserted, so any caller-supplied reference
    (``result_ref``, ``candidate_ref``, ``claim_refs``, ``proposal_refs``, ``rule_kind``, or
    anything else) is refused with ``lineage_ref_caller_asserted`` and nothing is written. For
    the same reason the record's caller-asserted artifacts and the caller-asserted later
    relations (``attach_artifact``) are not projected; only worker-resolved outcome relations
    are. The rule kind is the record's own consumer kind, which the consumer declared when the
    consumption happened. OPE-544's schema has no field to mark it caller-declared, so that is
    documented here and in the docs page: it names the kind of rule and never selects a claim.
    Unknown dependencies carry over, so the reader returns ``hold`` for them rather than a
    guessed edge. With ``store`` (an OPE-544 ``ManifestStore``) the manifest is also written.
    """
    need(not caller_refs, "lineage_ref_caller_asserted")
    from opentour_cell.pilot import consumption_lineage

    stored = recorder._record(tenant, workflow_ref, record_ref)
    body = stored["body"]
    need(body["partition"] in (DEVELOPMENT, PROTECTED), "lineage_partition_unavailable")
    kind = body["caller_asserted"]["consumer"]["kind"]
    need(kind in _LINEAGE_RULE_KINDS, "lineage_rule_kind_unavailable")
    relations = [
        r
        for r in recorder.relations(tenant, workflow_ref, record_ref)
        if r["kind"] == "outcome" and "caller_asserted" not in r["basis"]
    ]
    refs = lineage_refs(stored)
    correction = body["label_inputs"][0]["correction_version"] if body["label_inputs"] else 0
    manifest = {
        "schema": consumption_lineage.MANIFEST_SCHEMA,
        "manifest_ref": record_ref,
        "tenant": body["tenant"],
        "workflow_ref": body["workflow_ref"],
        "engagement_epoch": body["engagement_epoch"],
        "grant_epoch": body["grant_epoch"],
        "partition": body["partition"],
        "rule_kind": kind,
        "case_ref": body["case"],
        "decision_time": body["decision"]["decision_time"],
        "result_ref": refs["result_ref"],
        "candidate_ref": refs["candidate_ref"],
        "example_ref": body["example_ref"],
        "correction_version": correction,
        "policy_revision": body["policy_revision"],
        "claim_refs": refs["claim_refs"],
        "consumed": [
            {
                "source_ref": c["source_ref"],
                "path": c["path"],
                "source_revision": c["source_revision"],
                "available_at": c["available_at"],
                "span": None,
            }
            for c in body["consumed"]
        ],
        # An unlisted path has no known source, so OPE-544 sees the wildcard and holds.
        "unknown_dependencies": ["*"] if body["unknown_dependencies"] else [],
        "later_relationships": [
            {k: r[k] for k in ("kind", "ref", "available_at", "relationship_version")}
            for r in relations
        ],
    }
    validated = consumption_lineage.validate_manifest(manifest)
    if store is not None:
        store.put(validated)
    return validated


def to_recompute_families(sealed: Mapping[str, Any]) -> list[Any]:
    """Project a seal onto OPE-525 ``Family`` inputs: development root families only.

    A held root family is exported with ``lineage_complete=False`` so the planner holds it.
    """
    from opentour_cell.pilot.incremental_recompute import Family, FieldRef

    families: dict[str, set[tuple[str, str]]] = {}
    for entry in sealed["forward"]:
        refs = families.setdefault(entry["root_family"], set())
        for record in entry["records"]:
            if record["purpose"] != "dataset_preparation":
                continue
            refs.update((source, path) for source, path in record["fields"] if path != LABEL_PATH)
    held = set(sealed["held_root_families"])
    return [
        Family(
            family_id=family,
            tenant=sealed["tenant"],
            purpose="training",
            split=DEVELOPMENT,
            consumes=tuple(sorted(FieldRef(s, p) for s, p in refs)),
            lineage_complete=family not in held,
        )
        for family, refs in sorted(families.items())
    ]


def to_seed_roots(
    recorder: DependencyRecorder,
    tenant: str,
    workflow_ref: str,
    snapshot_digest: str,
    pair: SourcePair,
) -> tuple[list[Any], Any]:
    """Project the current training parents onto OPE-516 ``SeedRoot`` and ``SplitSeal`` inputs.

    Nothing that decides a target comes from the caller. The parents are recomputed from the
    worker store (``training_parents`` over ``pair``) and re-read through
    ``parents_manifest``; the settings revision is the one that manifest names in the pinned
    store; the HMAC key is the producer's own (``label_key``). Values are re-read from the
    source pair and must match the stored input and label digests, so a substituted target
    is refused (``seed_label_changed``). Every parent must be found; a missing one is refused.
    Vocabulary gap: OPE-516 names its seed population ``training``; this producer's
    populations are ``development`` and ``protected``. A training parent is a development
    example, so it maps to ``training``.
    """
    from opentour_cell.pilot.candidate_snapshot import example_ref as make_ref
    from opentour_cell.pilot.training_variants import SeedRoot, SplitSeal

    computed = recorder.training_parents(tenant, workflow_ref, snapshot_digest, lambda _s: pair)
    parents = recorder.parents_manifest(tenant, workflow_ref, computed["manifest_digest"])
    held = recorder.producer.settings.revision(
        tenant, workflow_ref, parents["current_settings_revision"]
    )
    need(held is not None, "settings_missing")
    assert held is not None
    settings: PilotSettings = held
    label_key = recorder.producer.label_key(tenant, workflow_ref)
    snapshot = recorder.producer.snapshot(tenant, workflow_ref, snapshot_digest)
    examples = {e["example_ref"]: e for e in snapshot["examples"]}
    revisions = {s["source_ref"]: s["source_revision"] for s in snapshot["sources"]}
    captures = pair.by_ref()
    wanted = {p["example_ref"]: p for p in parents["parents"]}
    roots = []
    for source in settings.sources:
        if source.role != "decision":
            continue
        for row in captures[source.source_ref].rows:
            mapped = map_row(row, settings.decision_mapping)
            if type(mapped["decision_id"]) is not str:
                continue
            ex = make_ref(
                settings.tenant, settings.workflow_ref, source.source_ref, mapped["decision_id"]
            )
            parent = wanted.get(ex)
            if parent is None or mapped["decision_revision"] != parent["decision_revision"]:
                continue
            fields = {
                name: row[column]
                for name, column in settings.feature_columns.items()
                if row.get(column) is not None
            }
            need(
                digest(["opentour.pilot.inputs.v1", ex, fields]) == parent["inputs_digest"],
                "seed_inputs_changed",
            )
            target = _target(settings, pair, mapped, parent)
            need(
                label_digest(label_key, settings.tenant, ex, target) == parent["label_digest"],
                "seed_label_changed",
            )
            # Case, prediction and source revision come from the worker's snapshot, never the
            # pair: the pair's values must equal what the snapshot recorded at capture.
            example = examples[ex]
            need(
                type(mapped["case"]) is str and mapped["case"] == example["case"],
                "seed_case_changed",
            )
            prediction = mapped["prediction"]
            need(
                type(prediction) is str
                and prediction_digest(label_key, settings.tenant, ex, prediction)
                == example["prediction_digest"],
                "seed_prediction_changed",
            )
            revision = revisions[source.source_ref]
            need(
                type(captures[source.source_ref].source_revision) is int
                and captures[source.source_ref].source_revision == revision,
                "seed_source_revision_changed",
            )
            roots.append(
                SeedRoot(
                    root_ref=ex,
                    case_ref=example["case"],
                    label_ref="lbl-" + parent["label_digest"][:32],
                    family_id=parent["root_family"],
                    split="training",
                    policy_revision=settings.policy_revision,
                    source_revision=str(revision),
                    fields=fields,
                    prediction=prediction,
                    target=target,
                    target_verified=True,
                    target_verifier_ref=parent["verifier"],
                    verifier_independent=True,
                )
            )
            wanted.pop(ex)
    need(not wanted, "seed_parent_missing")
    seal = SplitSeal.from_rows([(r.case_ref, r.family_id, "training") for r in roots])
    return sorted(roots, key=lambda r: r.root_ref), seal


def _target(
    settings: PilotSettings,
    pair: SourcePair,
    decision: Mapping[str, Any],
    parent: Mapping[str, Any],
) -> str:
    captures = pair.by_ref()
    for source in settings.sources:
        if source.role != "correction":
            continue
        for row in captures[source.source_ref].rows:
            mapped = map_row(row, settings.correction_mapping)
            if (
                mapped["decision_id"] == decision["decision_id"]
                and mapped["decision_revision"] == parent["decision_revision"]
                and mapped["correction_version"] == parent["correction_version"]
                and type(mapped["corrected_value"]) is str
            ):
                return str(mapped["corrected_value"])
    raise ProfileError("seed_label_changed")
