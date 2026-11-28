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
)
from opentour_cell.pilot.learning_profile import (
    EVIDENCE_TIER,
    PilotSettings,
    ProfileError,
    SourcePair,
    format_time,
    map_row,
    need,
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

    def _folder(self, tenant: str, workflow_ref: str, kind: str) -> Path:
        return (
            self.directory
            / ref(tenant, "dependency_tenant")
            / ref(workflow_ref, "dependency_workflow")
            / kind
        )

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
        example = next((e for e in snapshot["examples"] if e["example_ref"] == example_ref), None)
        need(example is not None, "example_unknown")
        assert example is not None
        # Disputed rows never enter a dataset, an evaluation or any other consumer.
        need(example["state"] != DISPUTED, "disputed_not_consumable")
        if purpose == "dataset_preparation":
            need(example["eligible"], "example_not_eligible")
            need(
                not (
                    self._folder(tenant, workflow_ref, "seals") / f"{_seal_name(snapshot_digest)}"
                    ".json"
                ).exists(),
                "snapshot_sealed",
            )
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
        if (folder / f"{record_ref}.json").exists():
            existing = read_pinned(folder, record_ref, "dependency_tampered")
            need(existing["body"] == body, "dependency_conflict")
            return dict(existing)
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
        for path in sorted(folder.glob("dep-*.json")) if folder.is_dir() else []:
            stored = read_pinned(folder, path.stem, "dependency_tampered")
            body = stored["body"]
            need(
                stored["record_ref"] == path.stem == "dep-" + digest(body)[:32]
                and body["tenant"] == tenant
                and body["workflow_ref"] == workflow_ref,
                "dependency_tampered",
            )
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
        """Outstanding proposal reference -> example references it actually consumed."""
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
        an unknown dependency, holds that root family only. Protected roots expose aggregate
        status only.
        """
        snapshot = self.producer.snapshot(tenant, workflow_ref, snapshot_digest)
        records = [
            r["body"] | {"record_ref": r["record_ref"]}
            for r in self.records(tenant, workflow_ref, snapshot_digest)
        ]
        eligible = [e for e in snapshot["examples"] if e["eligible"]]
        protected = [e for e in snapshot["examples"] if e["partition"] == PROTECTED]
        forward, holds, unknown = [], [], set()
        for example in eligible:
            mine = [r for r in records if r["example_ref"] == example["example_ref"]]
            prepared = [r for r in mine if r["caller_asserted"]["purpose"] == "dataset_preparation"]
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
        snapshot still shows what the reviewer inspected.
        """
        sealed = self.seal(tenant, workflow_ref, snapshot_digest)
        current = self.producer.current(
            tenant,
            workflow_ref,
            snapshot_digest,
            read_pair,
            self.proposals(tenant, workflow_ref, snapshot_digest),
        )
        need(current["state"] == "current", "current_eligibility_held")
        snapshot = self.producer.snapshot(tenant, workflow_ref, snapshot_digest)
        by_ref = {e["example_ref"]: e for e in snapshot["examples"]}
        now = set(current["eligible"])
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
            "current_settings_revision": current["settings_revision"],
            "parents": parents,
            "counts": {
                "parents": len(parents),
                "independent_root_families": len({p["root_family"] for p in parents}),
                "verified_observed": current["counters"]["live"]["verified"],
            },
            "retracted": current["retracted"],
            "stale_proposals": current["stale_proposals"],
            "holds": sealed["holds"],
            "evidence_tier": EVIDENCE_TIER,
        }
        return {**body, "manifest_digest": digest(body)}


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
    parents: Mapping[str, Any], children: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Union admitted synthetic descendants into a training set without touching live counts.

    A child must name a current real parent; any other child is rejected with a reason.
    """
    roots = {p["example_ref"] for p in parents["parents"]}
    admitted, rejected = [], []
    for child in children:
        child_ref, root = child.get("child_ref"), child.get("root_ref")
        if type(child_ref) is not str or type(root) is not str:
            rejected.append({"child_ref": None, "reason": "malformed_child"})
        elif root not in roots:
            rejected.append({"child_ref": child_ref, "reason": "child_without_live_parent"})
        else:
            admitted.append({"child_ref": child_ref, "root_ref": root, "origin": "synthetic"})
    return {
        "parent_manifest": parents["manifest_digest"],
        "live_counts": dict(parents["counts"]),
        "synthetic_children": len(admitted),
        "rows": len(parents["parents"]) + len(admitted),
        "admitted": admitted,
        "rejected": rejected,
    }


# --------------------------------------------------------------------------- projections
# Each projection imports its consumer lazily, so the producer runs with every consumer absent.


def to_lineage_manifest(
    recorder: DependencyRecorder,
    tenant: str,
    workflow_ref: str,
    record_ref: str,
    *,
    rule_kind: str,
    result_ref: str,
    candidate_ref: str,
    claim_refs: list[str],
) -> dict[str, Any]:
    """Project one record onto OPE-544's ``consumed-field-manifest.v1`` reader shape.

    OPE-544 needs result, candidate and claim references that this writer does not produce;
    they are taken here as caller-asserted inputs. Unknown dependencies carry over, so the
    reader returns ``hold`` for them rather than a guessed edge.
    """
    from opentour_cell.pilot import consumption_lineage

    stored = recorder._record(tenant, workflow_ref, record_ref)
    body = stored["body"]
    need(body["partition"] in (DEVELOPMENT, PROTECTED), "lineage_partition_unavailable")
    relations = recorder.relations(tenant, workflow_ref, record_ref)
    correction = body["label_inputs"][0]["correction_version"] if body["label_inputs"] else 0
    manifest = {
        "schema": consumption_lineage.MANIFEST_SCHEMA,
        "manifest_ref": record_ref,
        "tenant": body["tenant"],
        "workflow_ref": body["workflow_ref"],
        "engagement_epoch": body["engagement_epoch"],
        "grant_epoch": body["grant_epoch"],
        "partition": body["partition"],
        "rule_kind": rule_kind,
        "case_ref": body["case"],
        "decision_time": body["decision"]["decision_time"],
        "result_ref": result_ref,
        "candidate_ref": candidate_ref,
        "example_ref": body["example_ref"],
        "correction_version": correction,
        "policy_revision": body["policy_revision"],
        "claim_refs": claim_refs,
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
    return consumption_lineage.validate_manifest(manifest)


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
    parents: Mapping[str, Any],
    sealed: Mapping[str, Any],
    settings: PilotSettings,
    pair: SourcePair,
) -> tuple[list[Any], Any]:
    """Project training parents onto OPE-516 ``SeedRoot`` and ``SplitSeal`` inputs.

    Values are re-read from the worker's source pair and must match the sealed label and
    input digests, so a caller cannot substitute a target. Vocabulary gap: OPE-516 names its
    seed population ``training``; this producer's populations are ``development`` and
    ``protected``. A training parent is a development example, so it maps to ``training``.
    """
    from opentour_cell.pilot.candidate_snapshot import example_ref as make_ref
    from opentour_cell.pilot.training_variants import SeedRoot, SplitSeal

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
                label_digest(settings.tenant, ex, target) == parent["label_digest"],
                "seed_label_changed",
            )
            roots.append(
                SeedRoot(
                    root_ref=ex,
                    case_ref=mapped["case"],
                    label_ref="lbl-" + parent["label_digest"][:32],
                    family_id=parent["root_family"],
                    split="training",
                    policy_revision=settings.policy_revision,
                    source_revision=str(captures[source.source_ref].source_revision),
                    fields=fields,
                    prediction=mapped["prediction"],
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
