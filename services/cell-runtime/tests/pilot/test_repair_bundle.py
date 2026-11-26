"""Owner-local, process-level repair delivery and independent recipient replay on generated data."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from zipfile import ZIP_STORED, ZipFile

import pytest
from opentour_cell.pilot import repair_bundle as bundle
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.readout import INPUT_VERSION, evaluate_manifest
from opentour_cell.pilot.simple_repair import declared_rule, predict_rule
from test_source_intake import Sample

Json = dict[str, Any]


def write(path: Path, value: Json | bytes) -> None:
    path.write_bytes(value if isinstance(value, bytes) else (canonical(value) + "\n").encode())
    path.chmod(0o600)


def read(path: Path) -> Json:
    return json.loads(path.read_bytes())


def cli(grant: Path, *args: object, success: bool = True) -> Json:
    proc = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "opentour_cell.pilot.repair_bundle",
            "--grant",
            str(grant),
            *map(str, args),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if success:
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)
    assert proc.returncode == 2 and not proc.stdout, proc.stdout
    return {"reason": proc.stderr.strip()}


class OwnerFixture:
    def __init__(self, path: Path, *, disposition: str = "rule", learning: bool = True) -> None:
        self.sample = sample = Sample(path)
        if learning:
            sample.g["training_attestation"] = "att-2222222222222222"
            sample.g["training_expires_at"] = int(time.time()) + 1200
            sample.save()
        self.intake = frozen = sample.freeze(sample.confirmation())
        self.rule = declared_rule(
            feature="description",
            allowed_features=["description"],
            mapping={"vehicle advance": "loan"},
            fallback="incumbent",
        )
        self.plan = {
            "organization": sample.store.organization,
            "engagement": sample.store.engagement_id,
            "audience": "recipient-a",
            "intake_revision": frozen["intake_revision"],
            "terms": {"error_ceiling_bps": 1000},
        }
        self.plan["revision"] = digest(self.plan)
        self.manifest = {
            "intake_revision": frozen["intake_revision"],
            "handoff": {
                "policy_revision": "chart-1",
                "exporter": "operator-a",
                "support_owner": "support-a",
                "limitations": ["Generated local examples"],
                "decision_time_assumptions": ["Chart effective at posting"],
                "fallback": "manual_review",
                "allow_learning_rows": learning,
                "learning_features": ["description"],
            },
        }
        expected = [
            {
                "case_id": r["case_id"],
                "reference": r["reference"],
                "incumbent": r["incumbent"],
                "simple": predict_rule(self.rule, r["features"], r["incumbent"]),
            }
            for r in frozen["protected"]
        ]

        def arm(name: str, version: str) -> Json:
            cases = [
                {
                    "case_id": r["case_id"],
                    "reference": r["reference"],
                    "current": r["incumbent"],
                    "repair": r[name],
                }
                for r in expected
            ]
            report = evaluate_manifest(
                {"schema_version": INPUT_VERSION, "error_ceiling_bps": 1000, "cases": cases}
            )
            return {
                "revision": version,
                "metrics": report["repair"],
                "prediction_digest": digest({r["case_id"]: r[name] for r in expected}),
            }

        self.comparison = {
            "schema_version": "opentour.pilot.protected-comparison.v1",
            "organization": self.plan["organization"],
            "engagement": self.plan["engagement"],
            "audience": self.plan["audience"],
            "intake_revision": frozen["intake_revision"],
            "plan_revision": self.plan["revision"],
            "paired_cases": expected,
            "arms": {
                "incumbent": arm("incumbent", digest("incumbent")),
                "simple": arm("simple", self.rule["revision"]),
            },
            "decision": {
                "disposition": "hold"
                if disposition in ("inconclusive", "unscored")
                else "recommend",
                "winner": None if disposition in ("inconclusive", "unscored") else "simple",
            },
        }
        if disposition == "stop":
            self.manifest["handoff"]["disposition"] = "stop"
        if disposition == "unscored":
            self.comparison.update(arms={}, paired_cases=[])
        self.comparison["evidence_revision"] = digest(self.comparison)
        self.stage = bundle.prepare_bundle_spec(
            path,
            manifest=self.manifest,
            intake=frozen,
            plan=self.plan,
            comparison=self.comparison,
            rule=self.rule,
        )
        self.folder = Path(self.stage["source_directory"])
        self.spec = self.folder / "spec.json"
        common = {
            "schema_version": bundle.GRANT,
            "state": "active",
            "expires_at": int(time.time()) + 3600,
            "store": str(sample.store.path),
            **{
                k: self.stage[k]
                for k in (
                    *bundle.BOUND,
                    "intake_revision",
                    "dataset_id",
                    "store_engagement",
                    "spec_sha256",
                    "local_assets",
                )
            },
        }
        self.operator = path / "operator-grant.json"
        self.recipient = path / "recipient-grant.json"
        self.archive = path / "delivery.zip"
        write(self.operator, dict(common, role="operator", actor="operator-a"))
        write(
            self.recipient,
            dict(common, role="recipient", actor="recipient-a", materialize_learning=True),
        )

    def export(self, **kwargs: Any) -> Json:
        return cli(
            self.operator,
            "export",
            "--spec",
            self.spec,
            "--source",
            self.folder,
            "--output",
            self.archive,
            **kwargs,
        )


def test_recipient_import_replay_infer_fallback_and_witness_in_fresh_processes(
    tmp_path: Path,
) -> None:
    fx = OwnerFixture(tmp_path)
    assert fx.export() == fx.export()
    raw = fx.archive.read_bytes()
    with ZipFile(io.BytesIO(raw)) as artifact:
        assert set(artifact.namelist()) == {"manifest.json", "assets/winner.rule.json"}
        assert b"invoice-4" not in raw
    imported = tmp_path / "recipient-copy.zip"
    with ThreadPoolExecutor(max_workers=3) as pool:
        replies = list(
            pool.map(
                lambda _: cli(
                    fx.recipient, "import", "--archive", fx.archive, "--output", imported
                ),
                range(3),
            )
        )
    assert all(r["state"] == "imported" for r in replies) and imported.read_bytes() == raw
    result = cli(fx.recipient, "verify", "--archive", imported)
    assert result["cases"] == 3 and result["learning_rows"] == 3 and result["fallback_rehearsed"]
    assert (
        result["arm_readout_digests"]
        == cli(fx.recipient, "replay", "--archive", imported)["arm_readout_digests"]
    )
    fresh = tmp_path / "fresh.json"
    write(fresh, {"features": {"description": "vehicle advance"}})
    winning = cli(
        fx.recipient,
        "predict",
        "--archive",
        imported,
        "--version",
        fx.stage["winner_version"],
        "--input",
        fresh,
    )
    assert winning["decision"] == {"status": "automatic", "label": "loan"}
    fallback = cli(fx.recipient, "fallback", "--archive", imported, "--input", fresh)
    assert (
        fallback["decision"] == {"status": "review"}
        and fallback["version"] == fx.stage["fallback_version"]
    )
    witness = tmp_path / "witness.json"
    first = cli(
        fx.recipient,
        "witness",
        "--archive",
        imported,
        "--output",
        witness,
        "--attestation",
        "att-private-witness",
    )
    assert first == cli(
        fx.recipient,
        "witness",
        "--archive",
        imported,
        "--output",
        witness,
        "--attestation",
        "att-private-witness",
    )
    assert (
        first["actor"] == "recipient-a"
        and first["witnessed_utc"]
        and first["evidence_revision"] == fx.comparison["evidence_revision"]
    )
    rows = tmp_path / "private-learning.json"
    cli(fx.recipient, "learning", "--archive", imported, "--output", rows)
    assert len(read(rows)["rows"]) == 3 and not any(
        r["case_id"] == "invoice-4" for r in read(rows)["rows"]
    )


@pytest.mark.parametrize("disposition", ["inconclusive", "stop"])
def test_diagnosis_and_customer_stop_have_no_serving_route(
    tmp_path: Path, disposition: str
) -> None:
    fx = OwnerFixture(tmp_path, disposition=disposition, learning=False)
    fx.export()
    verified = cli(fx.recipient, "verify", "--archive", fx.archive)
    assert (
        verified["disposition"] == disposition
        and not verified["fallback_rehearsed"]
        and verified["learning_rows"] == 0
    )
    case = tmp_path / "fresh.json"
    write(case, {"features": {}})
    cli(fx.recipient, "fallback", "--archive", fx.archive, "--input", case, success=False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "withdrawn"),
        ("expires_at", 1),
        ("spec_sha256", "f" * 64),
        ("actor", "other-user"),
        ("organization", "other-org"),
        ("policy_revision", "chart-2"),
    ],
)
def test_inactive_superseded_foreign_wrong_recipient_and_current_policy(
    tmp_path: Path, field: str, value: Any
) -> None:
    fx = OwnerFixture(tmp_path)
    fx.export()
    write(fx.recipient, read(fx.recipient) | {field: value})
    cli(fx.recipient, "verify", "--archive", fx.archive, success=False)


def test_roles_source_withdrawal_and_unsafe_paths_fail_closed(tmp_path: Path) -> None:
    fx = OwnerFixture(tmp_path)
    fx.export()
    cli(fx.operator, "verify", "--archive", fx.archive, success=False)
    cli(
        fx.recipient,
        "export",
        "--spec",
        fx.spec,
        "--source",
        fx.folder,
        "--output",
        tmp_path / "unsafe.zip",
        success=False,
    )
    link = tmp_path / "alias.zip"
    link.symlink_to(fx.archive)
    cli(fx.recipient, "verify", "--archive", link, success=False)
    fx.sample.store.withdraw()
    cli(fx.recipient, "replay", "--archive", fx.archive, success=False)
    assert not (tmp_path / "unsafe.zip").exists()


def test_archive_checksums_inventory_and_interrupted_output(tmp_path: Path) -> None:
    fx = OwnerFixture(tmp_path)
    fx.export()
    origin = fx.archive.read_bytes()
    hidden = tmp_path / "extra-bytes.zip"
    write(hidden, origin + b"unapproved-trailing-data")
    assert (
        cli(fx.recipient, "verify", "--archive", hidden, success=False)["reason"]
        == "noncanonical_or_hidden_archive_content"
    )
    blocked = tmp_path / "blocked"
    write(blocked, b"existing file")
    cli(fx.recipient, "import", "--archive", fx.archive, "--output", blocked, success=False)
    assert blocked.read_bytes() == b"existing file"
    for variant in ("tampered", "missing", "path"):
        trial = tmp_path / (variant + ".zip")
        with ZipFile(io.BytesIO(origin)) as old, ZipFile(trial, "w", compression=ZIP_STORED) as new:
            for name in old.namelist():
                if variant == "missing" and name.startswith("assets"):
                    continue
                data = old.read(name)
                if variant == "tampered" and name.startswith("assets"):
                    data += b" "
                new.writestr(name, data)
            if variant == "path":
                new.writestr("../unexpected", b"bad")
        trial.chmod(0o600)
        cli(fx.recipient, "verify", "--archive", trial, success=False)
    assert not (tmp_path / "unexpected").exists()
    write(fx.folder / "benchmark.json", {"cases": []})
    assert fx.archive.read_bytes() == origin
    cli(fx.recipient, "replay", "--archive", fx.archive, success=False)


def test_failed_stage_and_unapproved_reversion_never_override_prior(tmp_path: Path) -> None:
    fx = OwnerFixture(tmp_path)
    first = fx.spec.read_bytes()
    # A previous policy did not authorize the requested fallback.
    fx.manifest["handoff"]["fallback"] = {
        "kind": "rule",
        "policy_revision": "expired-policy",
        "rule": fx.rule,
    }
    with pytest.raises(bundle.BundleError, match="incompatible_fallback_policy"):
        bundle.prepare_bundle_spec(
            tmp_path,
            manifest=fx.manifest,
            intake=fx.intake,
            plan=fx.plan,
            comparison=fx.comparison,
            rule=fx.rule,
        )
    assert fx.spec.read_bytes() == first


def test_missing_real_model_cannot_be_labeled_as_qualified(tmp_path: Path) -> None:
    fx = OwnerFixture(tmp_path)
    fx.comparison["decision"] = {"disposition": "recommend", "winner": "model"}
    fx.comparison["arms"]["model"] = {"revision": "1" * 64}
    fx.comparison["evidence_revision"] = digest(
        {k: v for k, v in fx.comparison.items() if k != "evidence_revision"}
    )
    with pytest.raises(bundle.BundleError, match="real_model_artifacts_required"):
        bundle.prepare_bundle_spec(
            tmp_path,
            manifest=fx.manifest,
            intake=fx.intake,
            plan=fx.plan,
            comparison=fx.comparison,
            rule=fx.rule,
        )


def test_unscored_development_hold_preserves_no_promoted_artifact(tmp_path: Path) -> None:
    fx = OwnerFixture(tmp_path, disposition="unscored", learning=False)
    fx.export()
    result = cli(fx.recipient, "verify", "--archive", fx.archive)
    assert result["cases"] == 0 and result["arm_readout_digests"] == {}
    assert result["disposition"] == "inconclusive" and not result["fallback_rehearsed"]


def keys(owner: Path, recipient: Path) -> tuple[Path, Path, Path]:
    def generate(*args: object) -> None:
        subprocess.run(["openssl", *map(str, args)], capture_output=True, check=True)

    signing, public = owner / "issuer.pem", owner / "issuer.public.pem"
    certificate, decryption = recipient / "recipient.crt", recipient / "recipient.key"
    generate("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", signing)
    generate("pkey", "-in", signing, "-pubout", "-out", public)
    generate(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        1,
        "-subj",
        "/CN=generated-recipient",
        "-keyout",
        decryption,
        "-out",
        certificate,
    )
    for path in [signing, public, certificate, decryption]:
        path.chmod(0o600)
    return signing, public, certificate


def transfer(
    owner: Path, recipient: Path, stage: Json, operator: Path, actor: str
) -> tuple[Path, Path, Json]:
    signing, public, cert = keys(owner, recipient)
    custody = recipient / "custody"
    custody.mkdir(mode=0o700)
    certpin = hashlib.sha256(cert.read_bytes()).hexdigest()
    pubpin = hashlib.sha256(public.read_bytes()).hexdigest()
    cutoff = int(time.time()) + 180
    source_cert = owner / "registered-recipient.pem"
    write(source_cert, cert.read_bytes())
    operator_grant = read(operator) | {
        "seal_assets": sorted(bundle.MANDATORY),
        "recipient_certificate": str(source_cert),
        "recipient_certificate_sha256": certpin,
        "portable": {
            "evidence_access": True,
            "learning_access": True,
            "owner_public_key": str(public),
            "owner_key_sha256": pubpin,
            "owner_signing_key": str(signing),
            "expires_at": cutoff,
        },
    }
    write(operator, operator_grant)
    args = ["--spec", stage["spec_path"], "--source", stage["source_directory"]]
    archive, ticket = owner / "sealed.zip", owner / "entitlement.json"
    sent = cli(operator, "seal", *args, "--output", archive)
    assert sent == cli(operator, "seal", *args, "--output", archive)
    issued = cli(operator, "issue-entitlement", *args, "--archive", archive, "--output", ticket)
    assert issued == cli(
        operator, "issue-entitlement", *args, "--archive", archive, "--output", ticket
    )
    remote_archive = recipient / "offered.zip"
    remote_ticket, remote_key = custody / "entitlement.json", custody / "approved-signer.pem"
    for source, target in [
        (archive, remote_archive),
        (ticket, remote_ticket),
        (public, remote_key),
    ]:
        write(target, source.read_bytes())
    grant = {
        "schema_version": bundle.GRANT,
        **{
            k: stage[k]
            for k in (*bundle.BOUND, "dataset_id", "store_engagement", "intake_revision")
        },
        "actor": actor,
        "role": "recipient",
        "state": "active",
        "expires_at": cutoff,
        "spec_sha256": issued["spec_sha256"],
        "recipient_certificate": str(cert),
        "recipient_key": str(recipient / "recipient.key"),
        "recipient_custody": {
            "entitlement": str(remote_ticket),
            "owner_public_key": str(remote_key),
            "owner_key_sha256": pubpin,
            "ledger": str(custody),
        },
        "materialize_learning": True,
    }
    remote = recipient / "recipient.json"
    write(remote, grant)
    assert hashlib.sha256(remote_archive.read_bytes()).hexdigest() == issued["package_sha256"]
    assert cli(remote, "enroll-recipient")["spec_sha256"] == issued["spec_sha256"]
    return remote, remote_archive, grant


def test_sealed_recipient_replay_in_disjoint_custody_and_withdrawal(tmp_path: Path) -> None:
    owner, receiver = tmp_path / "sender", tmp_path / "receiver"
    receiver.mkdir(mode=0o700)
    fx = OwnerFixture(owner)
    grant_path, offered, grant = transfer(owner, receiver, fx.stage, fx.operator, "recipient-a")
    with ZipFile(offered) as capture:
        assert all(capture.read("assets/" + name + ".cms")[:1] == b"0" for name in bundle.MANDATORY)
        assert (
            b"invoice-4" not in offered.read_bytes() and b"adjudicator" not in offered.read_bytes()
        )
    owner.rename(tmp_path / "producer-disconnected")
    installed = receiver / "installed.zip"
    confirmed = cli(grant_path, "import", "--archive", offered, "--output", installed)
    assert (
        confirmed["state"] == "imported"
        and confirmed["cases"] == 3
        and confirmed["learning_rows"] == 3
    )
    assert cli(grant_path, "replay", "--archive", installed)["fallback_rehearsed"]
    decision = receiver / "decision.json"
    write(decision, {"features": {"description": "vehicle advance"}})
    assert cli(grant_path, "fallback", "--archive", installed, "--input", decision)["decision"] == {
        "status": "review"
    }
    entitlement = Path(grant["recipient_custody"]["entitlement"])
    original_ticket = read(entitlement)
    write(
        entitlement,
        original_ticket | {"claim": original_ticket["claim"] | {"expires_at": 4102444800}},
    )
    assert (
        cli(grant_path, "verify", "--archive", installed, success=False)["reason"]
        == "invalid_owner_entitlement"
    )
    write(entitlement, original_ticket)
    write(grant_path, grant | {"recipient_key": str(receiver / "recipient.crt")})
    assert (
        cli(grant_path, "verify", "--archive", installed, success=False)["reason"]
        == "recipient_cannot_open_approved_asset"
    )
    write(grant_path, grant | {"materialize_learning": False})
    cli(
        grant_path,
        "learning",
        "--archive",
        installed,
        "--output",
        receiver / "unauthorized.json",
        success=False,
    )
    assert not (receiver / "unauthorized.json").exists()
    write(grant_path, grant)
    assert cli(grant_path, "withdraw-recipient") == cli(grant_path, "withdraw-recipient")
    assert (
        cli(
            grant_path,
            "predict",
            "--archive",
            installed,
            "--version",
            fx.stage["winner_version"],
            "--input",
            decision,
            success=False,
        )["reason"]
        == "recipient_custody_withdrawn"
    )
    assert (
        cli(grant_path, "fallback", "--archive", offered, "--input", decision, success=False)[
            "reason"
        ]
        == "recipient_custody_withdrawn"
    )


def test_export_requires_live_training_rights_and_registered_recipient_cert(tmp_path: Path) -> None:
    fx = OwnerFixture(tmp_path)
    grant = read(fx.operator) | {
        "seal_assets": ["learning.json"],
        "recipient_certificate": str(fx.spec),
        "recipient_certificate_sha256": "f" * 64,
    }
    write(fx.operator, grant)
    reply = cli(
        fx.operator,
        "seal",
        "--spec",
        fx.spec,
        "--source",
        fx.folder,
        "--output",
        fx.archive,
        success=False,
    )
    assert reply["reason"] == "recipient_certificate_mismatch" and not fx.archive.exists()
    grant["recipient_certificate_sha256"] = hashlib.sha256(fx.spec.read_bytes()).hexdigest()
    write(fx.operator, grant)
    source = read(fx.sample.grant)
    del source["training_attestation"]
    del source["training_expires_at"]
    write(fx.sample.grant, source)
    cli(
        fx.operator,
        "seal",
        "--spec",
        fx.spec,
        "--source",
        fx.folder,
        "--output",
        fx.archive,
        success=False,
    )
    assert not fx.archive.exists()


@pytest.mark.local_integration
def qualify_actual_pretrained_adapter_disconnected_recipient_qualified_replay(
    tmp_path: Path,
) -> None:
    base = os.environ["OPENTOUR_QUALIFICATION_BASE"]
    from opentour_cell.pilot import repair_workflow as workflow

    owner, receiver = tmp_path / "owner", tmp_path / "recipient"
    receiver.mkdir(mode=0o700)
    example = Path(__file__).resolve().parents[2] / "examples" / "prepare-repair-example.py"
    subprocess.run(
        [sys.executable, str(example), str(owner), "--route", "model"],
        check=True,
        capture_output=True,
    )
    shutil.copytree(base, owner / "model-base")
    manifest = read(owner / "managed-plan.json")
    org, run_id = manifest["organization"], "run-" + "4" * 32
    workflow.grant_model(
        owner,
        org,
        manifest,
        trainer="generated-trainer",
        payer="generated-local",
        rights="att-" + "4" * 16,
        destination="generated-recipient",
        features=["description"],
        labels=["refund", "sale"],
        max_seconds=120,
        expires_at=int(time.time()) + 600,
    )
    result = workflow.execute_managed(owner, org, manifest, run_id)
    stage = workflow.prepare_handoff(owner, org, manifest, run_id)
    assert stage["disposition"] == "model", result
    model = owner / "managed-outputs" / run_id / "model"
    native = read(next(owner.glob("model-grant-*.json")))
    runtime = {
        "authority_bundle": str(model),
        "model_dir": str(owner / "model-base"),
        "grant": native,
        "python": os.environ["OPENTOUR_TRAINING_PYTHON"],
    }
    main = {
        k: stage[k]
        for k in (
            *bundle.BOUND,
            "dataset_id",
            "store_engagement",
            "intake_revision",
            "spec_sha256",
            "local_assets",
        )
    }
    operator = owner / "operator.json"
    write(
        operator,
        {
            **main,
            "schema_version": bundle.GRANT,
            "role": "operator",
            "actor": manifest["handoff"]["exporter"],
            "state": "active",
            "expires_at": int(time.time()) + 600,
            "store": str(owner / "training.sqlite3"),
            "model_runtime": runtime,
        },
    )
    remote, archive, grant = transfer(owner, receiver, stage, operator, manifest["audience"])
    shutil.copytree(base, receiver / "model-base")
    write(
        remote,
        grant
        | {
            "model_runtime": {
                "model_dir": str(receiver / "model-base"),
                "grant": native,
                "python": runtime["python"],
            }
        },
    )
    owner.rename(tmp_path / "producer-disconnected")
    installed = receiver / "installed.zip"
    independent = cli(remote, "import", "--archive", archive, "--output", installed)
    assert (
        independent["cases"] == 4
        and independent["disposition"] == "model"
        and independent["fallback_rehearsed"]
    )
    assert (
        cli(remote, "replay", "--archive", installed)["arm_readout_digests"]
        == independent["arm_readout_digests"]
    )
    query = receiver / "fresh.json"
    write(query, {"features": {"description": "Please return this order and refund my card."}})
    predicted = cli(
        remote,
        "predict",
        "--archive",
        installed,
        "--version",
        stage["winner_version"],
        "--input",
        query,
    )
    assert predicted["decision"]["status"] == "automatic"
    assert cli(remote, "fallback", "--archive", installed, "--input", query)["decision"] == {
        "status": "review"
    }
    cli(remote, "withdraw-recipient")
    cli(
        remote,
        "predict",
        "--archive",
        installed,
        "--version",
        stage["winner_version"],
        "--input",
        query,
        success=False,
    )


def test_offline_current_pointer_requires_signed_exact_predecessor(tmp_path: Path) -> None:
    owner, receiver = tmp_path / "producer", tmp_path / "consumer"
    receiver.mkdir(mode=0o700)
    fx = OwnerFixture(owner)
    prior, original_zip, prior_grant = transfer(
        owner, receiver, fx.stage, fx.operator, "recipient-a"
    )
    approved = read(fx.operator)
    approved["seal_assets"] = ["benchmark.json", "evidence.json"]
    approved["portable"]["supersedes"] = prior_grant["spec_sha256"]
    write(fx.operator, approved)
    args = ["--spec", fx.spec, "--source", fx.folder]
    offered, entitlement = owner / "revised.zip", owner / "revised.entitlement.json"
    seal = cli(fx.operator, "seal", *args, "--output", offered)
    cli(fx.operator, "issue-entitlement", *args, "--archive", offered, "--output", entitlement)
    assert seal["spec_sha256"] != prior_grant["spec_sha256"]
    candidate = receiver / "second.json"
    offer = receiver / "second.zip"
    signed = receiver / "second-entitlement.json"
    local = receiver / "permitted-learning.json"
    for src, dst in [(offered, offer), (entitlement, signed), (fx.folder / "learning.json", local)]:
        write(dst, src.read_bytes())
    grant = prior_grant | {
        "spec_sha256": seal["spec_sha256"],
        "local_assets": {hashlib.sha256(local.read_bytes()).hexdigest(): str(local)},
        "recipient_custody": prior_grant["recipient_custody"] | {"entitlement": str(signed)},
    }
    write(candidate, grant)
    assert (
        cli(candidate, "verify", "--archive", offer, success=False)["reason"]
        == "recipient_version_not_current"
    )
    cli(candidate, "enroll-recipient")
    assert cli(candidate, "verify", "--archive", offer)["cases"] == 3
    assert (
        cli(prior, "enroll-recipient", success=False)["reason"] == "recipient_version_not_current"
    )
    assert (
        cli(prior, "verify", "--archive", original_zip, success=False)["reason"]
        == "recipient_version_not_current"
    )


def test_privately_staged_candidate_cannot_export_without_exact_human_release(
    tmp_path: Path,
) -> None:
    from opentour_cell.pilot import approval_inbox, repair_workflow, source_intake
    from test_approval_inbox import callback, opt_in, stage
    from test_reviewer_workflow import buttons, screen

    h, manifest, run, receipt, audit = stage(tmp_path)
    directory = h.path.parent
    work = directory / "managed-outputs" / run
    intake = source_intake.load_intake(h.store, directory / manifest["intake_file"])
    prepared = bundle.prepare_bundle_spec(
        work,
        manifest=manifest,
        intake=intake,
        plan=read(work / "plan.json"),
        comparison=read(work / "comparison.json"),
        rule=read(work / "rule.json"),
    )
    data = read(Path(prepared["spec_path"]))
    assert data["release"] == {
        "artifact_sha": audit["candidate_digest"],
        "run_revision": audit["run_revision"],
        "run_id": run,
    }
    grant = directory / "export-grant.json"
    write(
        grant,
        {
            **{
                k: prepared[k]
                for k in (
                    *bundle.BOUND,
                    "dataset_id",
                    "store_engagement",
                    "intake_revision",
                    "spec_sha256",
                    "local_assets",
                )
            },
            "role": "operator",
            "actor": manifest["handoff"]["exporter"],
            "schema_version": bundle.GRANT,
            "expires_at": int(time.time()) + 180,
            "state": "active",
            "store": str(h.store.path),
        },
    )
    destination = directory / "authorized.zip"
    cmd = [
        "export",
        "--spec",
        prepared["spec_path"],
        "--source",
        prepared["source_directory"],
        "--output",
        destination,
    ]
    cli(grant, *cmd, success=False)
    assert not destination.exists()
    opt_in(h)
    assert approval_inbox.flush_approvals(h.app) == 1
    h.send("interactive", callback(h, receipt))
    view = list(h.slack.screens)[-1]
    selected = buttons(screen(h, view))["ot_release_approve"]
    h.action(view, "ot_release_approve", selected)
    assert (
        repair_workflow.prepare_handoff(directory, h.store.organization, manifest, run)[
            "spec_sha256"
        ]
        == prepared["spec_sha256"]
    )
    assert cli(grant, *cmd)["state"] == "exported"
    (work / "release-decision.json").unlink()
    new_destination = directory / "no-longer-authorized.zip"
    cli(grant, *cmd[:-1], new_destination, success=False)
    assert not new_destination.exists()
