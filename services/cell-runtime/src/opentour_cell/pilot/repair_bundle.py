"""Deliver and exercise one customer's signed-off offline repair on its approved local machine."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import io
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zipfile import ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.readout import evaluate_manifest, parse_manifest
from opentour_cell.pilot.training import LocalTrainingStore, registration_state

Json = dict[str, Any]
PACKAGE = "opentour.pilot.repair-handoff.v1"
GRANT = "opentour.pilot.handoff-grant.v1"
ENTITLEMENT = "opentour.pilot.handoff-entitlement.v1"
BENCHMARK = "opentour.pilot.handoff-benchmark.v1"
LEARNING = "opentour.pilot.handoff-learning.v1"
MAX_MEMBER = 16 * 1024 * 1024
MAX_ARCHIVE = 64 * 1024 * 1024
MANDATORY = {"evidence.json", "benchmark.json", "learning.json"}
BOUND = ("organization", "engagement", "audience", "policy_revision")
SHA = re.compile("[0-9a-f]{64}")
ID = re.compile("[a-zA-Z0-9][a-zA-Z0-9_.:@-]{0,127}")


class BundleError(ValueError):
    """Safe diagnostic, independent of customer content."""


def _require(test: Any, code: str = "invalid_handoff") -> None:
    if not test:
        raise BundleError(code)


def _object(value: Any) -> Json:
    _require(isinstance(value, dict))
    return dict(value)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash(value: Any) -> str:
    _require(isinstance(value, str) and SHA.fullmatch(value))
    return str(value)


def _id(value: Any) -> str:
    _require(isinstance(value, str) and ID.fullmatch(value))
    return str(value)


def _private_directory(path: Path) -> None:
    _require(path.is_absolute() and ".." not in path.parts, "private_absolute_path_required")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        _require(stat.S_ISDIR(current.lstat().st_mode), "unsafe_directory")
    attrs = path.lstat()
    _require(
        attrs.st_uid == os.getuid() and not attrs.st_mode & 0o077, "private_directory_required"
    )


def _read(path: Path, limit: int = MAX_MEMBER) -> bytes:
    _private_directory(path.parent)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        attrs = os.fstat(stream.fileno())
        _require(
            stat.S_ISREG(attrs.st_mode)
            and attrs.st_uid == os.getuid()
            and not attrs.st_mode & 0o077
            and attrs.st_nlink == 1,
            "private_regular_file_required",
        )
        _require(0 <= attrs.st_size <= limit, "handoff_file_too_large")
        value = stream.read(limit + 1)
    _require(len(value) <= limit, "handoff_file_too_large")
    return value


def _grant(path: Path, *, allow_withdrawn: bool = False, enroll: bool = False) -> Json:
    value = parse_manifest(_read(path))
    _require(
        value.get("schema_version") == GRANT
        and (
            value.get("state") == "active" or allow_withdrawn and value.get("state") == "withdrawn"
        ),
        "handoff_grant_inactive",
    )
    _require(
        type(value.get("expires_at")) is int
        and 0 < value["expires_at"] < 2**63
        and (allow_withdrawn or time.time() < value["expires_at"]),
        "handoff_grant_expired",
    )
    for key in (*BOUND, "actor", "dataset_id", "store_engagement"):
        _id(value.get(key))
    _hash(value.get("spec_sha256"))
    if "intake_revision" in value:
        _hash(value["intake_revision"])
    else:
        _hash(value.get("dataset_revision"))
    _require(value.get("role") in ("operator", "recipient"), "handoff_role_denied")
    if "recipient_custody" in value:
        _require(value["role"] == "recipient", "handoff_role_denied")
        _entitlement(value, allow_withdrawn=allow_withdrawn, enroll=enroll)
        return value
    store = Path(value["store"])
    _private_directory(store.parent)
    _require(
        store.name == "training.sqlite3" and stat.S_ISREG(store.lstat().st_mode),
        "enrolled_store_required",
    )
    if "intake_revision" in value:
        from opentour_cell.pilot.source_intake import inspect_intake

        state = inspect_intake(
            LocalTrainingStore(store, value["organization"]), value["intake_revision"]
        )
        _require(
            state["dataset_id"] == value["dataset_id"]
            and state["engagement"] == value["store_engagement"],
            "enrollment_unavailable",
        )
    else:
        _require(
            registration_state(
                store, value["organization"], value["dataset_id"], value["dataset_revision"]
            )
            == "registered",
            "enrollment_unavailable",
        )
    return value


@contextmanager
def _current(path: Path, initial: Json) -> Iterator[None]:
    current = _grant(path)
    _require(current == initial, "handoff_grant_changed")
    if "recipient_custody" in current:
        marker = _withdrawal_path(current)
        with _directory_lock(marker.parent):
            _entitlement(current)
            _require(parse_manifest(_read(path)) == current, "handoff_grant_changed")
            yield
        return
    domain = LocalTrainingStore(Path(current["store"]), current["organization"])
    with domain.engagement_guard(
        current["dataset_id"] if "intake_revision" not in current else None,
        current.get("dataset_revision") if "intake_revision" not in current else None,
        engagement_id=current["store_engagement"],
    ):
        if "intake_revision" in current:
            from opentour_cell.pilot.source_intake import _verified_marker

            _verified_marker(domain, current["intake_revision"])
        _require(
            parse_manifest(_read(path)) == current and time.time() < current["expires_at"],
            "handoff_grant_changed",
        )
        yield


@contextmanager
def _directory_lock(path: Path) -> Iterator[None]:
    _private_directory(path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
        os.fsync(fd)
    finally:
        os.close(fd)


def _install(path: Path, data: bytes) -> None:
    _private_directory(path.parent)
    if os.path.lexists(path):
        _require(_read(path, MAX_ARCHIVE) == data, "destination_already_contains_other_content")
        return
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(name, path)
        except FileExistsError:
            _require(_read(path, MAX_ARCHIVE) == data, "destination_already_contains_other_content")
    finally:
        os.unlink(name)


def _publish(path: Path, data: bytes, grant_path: Path, grant: Json) -> None:
    """Only the verified complete regular file appears at its immutable destination."""
    with _current(grant_path, grant):
        _install(path, data)


def _asset(spec: Json, name: str) -> Json:
    descriptor = _object(spec["assets"][name])
    mode = descriptor.get("mode")
    if mode == "sealed":
        _require(set(descriptor) == {"mode", "sha256", "cipher_sha256", "certificate_sha256"})
        _hash(descriptor["cipher_sha256"])
        _hash(descriptor["certificate_sha256"])
    else:
        _require(mode in ("local", "included") and set(descriptor) == {"mode", "sha256"})
    _hash(descriptor.get("sha256"))
    if name in MANDATORY:
        _require(mode in ("local", "sealed"), "sensitive_rows_require_pin_or_encryption")
    return descriptor


def _route(route: Json, name: str, spec: Json) -> set[str]:
    kind = route.get("kind")
    _require(set(route) == {"kind", "version", "policy_revision"})
    _require(route["policy_revision"] == spec["policy_revision"], "incompatible_fallback_policy")
    _hash(route["version"])
    if kind == "manual" and name == "fallback":
        _require(
            route["version"] == digest(["human_review", spec["policy_revision"]]),
            "manual_fallback_not_bound",
        )
        return set()
    if kind == "rule":
        return {name + ".rule.json"}
    if kind == "adapter":
        return {name + ".adapter.json", name + ".model.json"}
    raise BundleError("unsupported_repair_route")


def _spec(spec: Json, grant: Json) -> None:
    _require(
        set(spec) - {"release_required", "release"}
        == {
            "schema_version",
            *BOUND,
            "exporter",
            "support_owner",
            "source_revisions",
            "reference_provenance",
            "decision_time_assumptions",
            "limitations",
            "disposition",
            "evidence_revision",
            "intake_revision",
            "winner",
            "fallback",
            "assets",
        }
    )
    _require(
        spec["schema_version"] == PACKAGE and digest(spec) == grant["spec_sha256"],
        "handoff_revision_unapproved",
    )
    _require(all(spec[k] == grant[k] for k in BOUND), "handoff_scope_mismatch")
    _require(
        grant["actor"] == spec["exporter" if grant["role"] == "operator" else "audience"],
        "handoff_actor_denied",
    )
    for key in ("support_owner", "exporter"):
        _id(spec[key])
    _require(spec["exporter"] != spec["audience"], "independent_recipient_required")
    _hash(spec["evidence_revision"])
    _require(
        spec["intake_revision"] == grant.get("intake_revision", grant.get("dataset_revision")),
        "handoff_intake_mismatch",
    )
    for key in (
        "source_revisions",
        "reference_provenance",
        "decision_time_assumptions",
        "limitations",
    ):
        rows = spec[key]
        _require(
            isinstance(rows, list)
            and 0 < len(rows) <= 64
            and all(
                isinstance(v, str) and v.strip() and v.isprintable() and len(v) <= 512 for v in rows
            ),
            "handoff_provenance_required",
        )
    disposition = spec["disposition"]
    if "release_required" in spec or "release" in spec:
        binding = _object(spec.get("release"))
        _require(
            spec.get("release_required") is True
            and set(binding) == {"run_id", "run_revision", "artifact_sha"}
            and re.fullmatch(r"run-[0-9a-f]{32}", _id(binding["run_id"]))
            and _hash(binding["run_revision"])
            and _hash(binding["artifact_sha"])
            and binding["artifact_sha"] == _object(spec["winner"])["version"],
            "invalid_human_release_binding",
        )
    _require(disposition in ("rule", "model", "inconclusive", "stop"))
    assets = set(MANDATORY)
    if disposition in ("rule", "model"):
        winner = _object(spec["winner"])
        _require(winner.get("kind") == ("rule" if disposition == "rule" else "adapter"))
        assets |= _route(winner, "winner", spec)
        fallback = _object(spec["fallback"])
        assets |= _route(fallback, "fallback", spec)
        _require(fallback["version"] != winner["version"], "fallback_must_be_independent")
    else:
        _require(spec["winner"] is None and spec["fallback"] is None, "held_result_has_no_route")
    _require(set(_object(spec["assets"])) == assets, "handoff_asset_inventory_mismatch")
    for item in assets:
        _asset(spec, item)


def _member(name: str, desc: Json) -> str | None:
    if desc["mode"] == "local":
        return None
    return "assets/" + name + (".cms" if desc["mode"] == "sealed" else "")


def _zip(spec: Json, members: dict[str, bytes]) -> bytes:
    blob = io.BytesIO()
    with ZipFile(blob, "w", compression=ZIP_STORED) as archive:
        for name, body in sorted(
            {"manifest.json": (canonical(spec) + "\n").encode(), **members}.items()
        ):
            info = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            archive.writestr(info, body)
    _require(blob.tell() <= MAX_ARCHIVE, "handoff_file_too_large")
    return blob.getvalue()


def _unpack(
    blob: bytes, grant: Json, approve: Callable[[Json], None] | None = None
) -> tuple[Json, dict[str, bytes]]:
    _require(len(blob) <= MAX_ARCHIVE)
    with ZipFile(io.BytesIO(blob)) as archive:
        items = archive.infolist()
        names = archive.namelist()
        _require(
            1 <= len(names) <= 16 and len(set(names)) == len(names) and "manifest.json" in names,
            "unsafe_archive_members",
        )
        _require(
            all(
                x.compress_type == ZIP_STORED
                and x.flag_bits & 1 == 0
                and 0 <= x.file_size <= MAX_MEMBER
                for x in items
            ),
            "unsafe_archive_members",
        )
        spec = parse_manifest(archive.read("manifest.json"))
        if approve is None:
            _spec(spec, grant)
        else:
            approve(spec)
        members = {}
        for asset, desc in spec["assets"].items():
            name = _member(asset, desc)
            if name is not None:
                _require(name in names, "handoff_asset_missing")
                data = archive.read(name)
                _require(
                    _sha(data) == desc.get("cipher_sha256", desc["sha256"]),
                    "handoff_asset_digest_mismatch",
                )
                members[name] = data
        _require(set(names) == {"manifest.json", *members}, "unsafe_archive_members")
        _require(blob == _zip(spec, members), "noncanonical_or_hidden_archive_content")
        return spec, members


def _openssl() -> str:
    binary = shutil.which("openssl", path=os.defpath)
    _require(binary is not None, "local_openssl_required")
    return str(binary)


def _crypto(arguments: Sequence[str], data: bytes, reason: str) -> bytes:
    result = subprocess.run(
        [_openssl(), *arguments],
        input=data,
        capture_output=True,
        timeout=30,
        env={"PATH": os.defpath},
        check=False,
    )
    _require(result.returncode == 0, reason)
    return result.stdout


def _withdrawal_path(grant: Json) -> Path:
    custody = _object(grant.get("recipient_custody"))
    directory = Path(custody["ledger"])
    _private_directory(directory)
    scope = digest([grant[k] for k in ("organization", "engagement", "audience")])
    return directory / (scope + ".withdrawn")


def _entitlement(grant: Json, *, allow_withdrawn: bool = False, enroll: bool = False) -> Json:
    custody = _object(grant["recipient_custody"])
    public_key = _read(Path(custody["owner_public_key"]))
    _require(_sha(public_key) == _hash(custody["owner_key_sha256"]), "owner_trust_anchor_mismatch")
    packet = parse_manifest(_read(Path(custody["entitlement"])))
    claim = _object(packet.get("claim"))
    signature = base64.b64decode(packet["signature"], validate=True)
    with tempfile.TemporaryDirectory() as temp:
        key, sig = Path(temp) / "public", Path(temp) / "signature"
        _write_private(key, public_key)
        _write_private(sig, signature)
        _crypto(
            ["dgst", "-sha256", "-verify", str(key), "-signature", str(sig)],
            _bytes(claim),
            "invalid_owner_entitlement",
        )
    _require(
        claim.get("schema_version") == ENTITLEMENT
        and all(
            claim.get(k) == grant.get(k)
            for k in (*BOUND, "dataset_id", "store_engagement", "intake_revision", "spec_sha256")
        )
        and grant["actor"] == claim["audience"]
        and grant["expires_at"] <= claim.get("expires_at", 0)
        and (allow_withdrawn or time.time() < claim["expires_at"]),
        "recipient_entitlement_mismatch",
    )
    _hash(claim.get("package_sha256"))
    _require(
        allow_withdrawn or not os.path.lexists(_withdrawal_path(grant)),
        "recipient_custody_withdrawn",
    )
    if not enroll:
        pointer = _withdrawal_path(grant).with_suffix(".current")
        _require(
            os.path.lexists(pointer)
            and _read(pointer) == _bytes({"spec_sha256": grant["spec_sha256"]}),
            "recipient_version_not_current",
        )
    return claim


def _enroll_recipient(grant_path: Path) -> Json:
    grant = _grant(grant_path, enroll=True)
    _require(
        grant["role"] == "recipient" and "recipient_custody" in grant, "recipient_custody_required"
    )
    pointer = _withdrawal_path(grant).with_suffix(".current")
    with _directory_lock(pointer.parent):
        claim = _entitlement(grant, enroll=True)
        _require(parse_manifest(_read(grant_path)) == grant, "handoff_grant_changed")
        previous = (
            parse_manifest(_read(pointer))["spec_sha256"] if os.path.lexists(pointer) else None
        )
        revision = grant["spec_sha256"]
        if previous != revision:
            _require(claim["supersedes"] == previous, "recipient_version_not_current")
            with tempfile.TemporaryDirectory(dir=pointer.parent) as private:
                staged = Path(private) / "revision"
                _write_private(staged, _bytes({"spec_sha256": revision}))
                os.replace(staged, pointer)
    return {"state": "recipient_enrolled", "spec_sha256": revision}


def _withdraw_recipient(grant_path: Path) -> Json:
    grant = _grant(grant_path, allow_withdrawn=True)
    _require(
        grant["role"] == "recipient" and "recipient_custody" in grant, "recipient_custody_required"
    )
    marker = _withdrawal_path(grant)
    with _directory_lock(marker.parent):
        _require(parse_manifest(_read(grant_path)) == grant, "handoff_grant_changed")
        _install(marker, _bytes({k: grant[k] for k in ("organization", "engagement", "audience")}))
    return {"state": "recipient_custody_withdrawn"}


def _decrypt(payload: bytes, desc: Json, grant: Json) -> bytes:
    cert = Path(grant.get("recipient_certificate", ""))
    key = Path(grant.get("recipient_key", ""))
    cert_bytes = _read(cert)
    _require(_sha(cert_bytes) == desc["certificate_sha256"], "recipient_certificate_mismatch")
    # Preserve no plaintext temporary files. The certificate and key are owner-selected, never
    # derived from archive members or a network location. Refuse interactive key prompts.
    key_bytes = _read(key)
    with tempfile.TemporaryDirectory() as private:
        certificate, private_key = Path(private) / "cert", Path(private) / "key"
        _write_private(certificate, cert_bytes)
        _write_private(private_key, key_bytes)
        checked = subprocess.run(
            [
                _openssl(),
                "cms",
                "-decrypt",
                "-binary",
                "-inform",
                "DER",
                "-recip",
                str(certificate),
                "-inkey",
                str(private_key),
                "-passin",
                "pass:",
            ],
            input=payload,
            capture_output=True,
            timeout=30,
            env={"PATH": os.defpath},
            check=False,
        )
    _require(
        checked.returncode == 0 and _sha(checked.stdout) == desc["sha256"],
        "recipient_cannot_open_approved_asset",
    )
    return checked.stdout


def _write_private(path: Path, value: bytes) -> None:
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as stream:
        stream.write(value)


def _value(name: str, spec: Json, members: dict[str, bytes], grant: Json) -> bytes:
    desc = _asset(spec, name)
    if desc["mode"] == "local":
        asset_path = _object(grant.get("local_assets", {})).get(desc["sha256"])
        if not isinstance(asset_path, str):
            raise BundleError("private_asset_retrieval_required")
        body = _read(Path(asset_path))
    else:
        body = members[str(_member(name, desc))]
        if desc["mode"] == "sealed":
            body = _decrypt(body, desc, grant)
    _require(_sha(body) == desc["sha256"], "handoff_asset_digest_mismatch")
    return body


def _evidence(spec: Json, members: dict[str, bytes], grant: Json) -> Json:
    obj = parse_manifest(_value("evidence.json", spec, members, grant))
    _require(
        obj.get("schema_version") == "opentour.pilot.protected-comparison.v1"
        and obj.get("evidence_revision") == spec["evidence_revision"]
        and digest({k: v for k, v in obj.items() if k != "evidence_revision"})
        == obj["evidence_revision"],
        "frozen_decision_mismatch",
    )
    _require(
        all(obj.get(k) == spec[k] for k in ("organization", "engagement", "audience")),
        "frozen_decision_scope_mismatch",
    )
    _require(
        obj.get("intake_revision") == spec["intake_revision"], "frozen_decision_scope_mismatch"
    )
    decision = _object(obj["decision"])
    state = spec["disposition"]
    if state in ("model", "rule"):
        arm = "model" if state == "model" else "simple"
        _require(
            decision.get("disposition") == "recommend"
            and decision.get("winner") == arm
            and obj["arms"][arm]["revision"] == spec["winner"]["version"],
            "winner_not_in_frozen_decision",
        )
        if spec["fallback"]["kind"] != "manual":
            _require(
                spec["fallback"]["version"] == obj["arms"]["incumbent"]["revision"],
                "unverified_incumbent_fallback",
            )
    elif state == "inconclusive":
        _require(
            decision.get("disposition") in ("hold", "no_change"), "inconclusive_evidence_mismatch"
        )
    # A business owner may stop even after the technical evidence recommends a repair.
    return obj


def _learning(spec: Json, members: dict[str, bytes], grant: Json) -> tuple[bytes, int]:
    body = _value("learning.json", spec, members, grant)
    obj = parse_manifest(body)
    _require(
        obj.get("schema_version") == LEARNING
        and obj.get("organization") == spec["organization"]
        and obj.get("engagement") == spec["engagement"]
        and isinstance(obj.get("rows"), list)
        and isinstance(obj.get("exclusions"), list)
        and obj.get("purpose") == "permitted_learning",
        "learning_scope_mismatch",
    )
    if obj["rows"] and "recipient_custody" in grant:
        claim = _entitlement(grant)
        _require(
            time.time() < claim.get("learning_expires_at", 0), "source_learning_permission_required"
        )
    elif obj["rows"] and "intake_revision" in grant:
        from opentour_cell.pilot.source_intake import inspect_intake

        stored = LocalTrainingStore(Path(grant["store"]), grant["organization"])
        _require(
            inspect_intake(stored, grant["intake_revision"])["training_allowed"],
            "source_learning_permission_required",
        )
    return body, len(obj["rows"])


def _predictions(
    route_name: str,
    spec: Json,
    members: dict[str, bytes],
    grant: Json,
    features: list[Json],
    incumbents: Sequence[Json | None],
) -> list[Json]:
    route = spec[route_name]
    _require(isinstance(route, dict), "handoff_has_no_promoted_route")
    _require(route["policy_revision"] == grant["policy_revision"], "incompatible_fallback_policy")
    if route["kind"] == "manual":
        return [{"status": "review"} for _ in features]
    if route["kind"] == "rule":
        from opentour_cell.pilot.simple_repair import predict_rule, validate_rule

        rule = parse_manifest(_value(route_name + ".rule.json", spec, members, grant))
        validate_rule(rule)
        _require(rule.get("revision") == route["version"], "wrong_rule_version")
        return [
            _object(predict_rule(rule, point, old))
            for point, old in zip(features, incumbents, strict=True)
        ]
    from opentour_cell.pilot.open_weight import inspect_open_weight, predict_open_weight_batch

    runtime = _object(grant.get("model_runtime"))
    _require(
        isinstance(runtime.get("model_dir"), str) and isinstance(runtime.get("grant"), dict),
        "approved_local_model_required",
    )

    def check_authority() -> None:
        if "recipient_custody" in grant:
            claim = _entitlement(grant)
            _require(
                claim.get("model_version") == route["version"]
                and claim.get("model_grant_sha256") == digest(runtime["grant"]),
                "staged_model_authority_changed",
            )
        else:
            authority_bundle = Path(runtime.get("authority_bundle", ""))
            _private_directory(authority_bundle)
            live = inspect_open_weight(authority_bundle, grant=runtime["grant"])
            _require(live["artifact_sha256"] == route["version"], "staged_model_authority_changed")

    check_authority()
    manifest = _value(route_name + ".model.json", spec, members, grant)
    _require(
        parse_manifest(manifest).get("artifact_sha256") == route["version"], "wrong_model_version"
    )
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp)
        _write_private(
            path / "adapter.json", _value(route_name + ".adapter.json", spec, members, grant)
        )
        _write_private(path / "manifest.json", manifest)
        answers = []
        projected = [
            {key: row[key] for key in runtime["grant"]["features"] if key in row}
            for row in features
        ]
        for offset in range(0, len(features), 64):
            check_authority()
            answers.extend(
                predict_open_weight_batch(
                    path,
                    projected[offset : offset + 64],
                    model_dir=Path(runtime["model_dir"]),
                    grant=runtime["grant"],
                    python=runtime.get("python"),
                )
            )
        check_authority()
        _require(len(answers) == len(features), "unverified_model_inference")
        return [_decision(d) for d in answers]


def _decision(value: Any) -> Json:
    v = _object(value)
    if v.get("status") == "automatic":
        _require(
            set(v) == {"status", "label"} and isinstance(v["label"], str) and v["label"],
            "invalid_benchmark_decision",
        )
    else:
        _require(
            v.get("status") in ("missing", "review") and set(v) == {"status"},
            "invalid_benchmark_decision",
        )
    return v


def _replay(spec: Json, members: dict[str, bytes], grant: Json) -> Json:
    evidence = _evidence(spec, members, grant)
    data = parse_manifest(_value("benchmark.json", spec, members, grant))
    _require(
        data.get("schema_version") == BENCHMARK
        and data.get("organization") == spec["organization"]
        and data.get("engagement") == spec["engagement"]
        and data.get("evidence_revision") == spec["evidence_revision"]
        and isinstance(data.get("cases"), list),
        "unverified_benchmark",
    )
    arms = _object(evidence["arms"])
    paired: dict[str, list[Json]] = {k: [] for k in arms}
    unscored = not arms and not data["cases"] and spec["disposition"] in ("stop", "inconclusive")
    _require(unscored or set(arms) in ({"incumbent", "simple"}, {"incumbent", "simple", "model"}))
    winner = spec["winner"]
    if winner is not None:
        _require(bool(data["cases"]), "nonempty_benchmark_required")
    features, incumbent_outputs = [], []
    for row in data["cases"]:
        observed = _object(row["decisions"])
        _require(set(observed) == set(arms), "benchmark_arms_mismatch")
        current = _decision(observed["incumbent"])
        features.append(_object(row["features"]))
        incumbent_outputs.append(current)
        for name, decision in observed.items():
            paired[name].append(
                {
                    "case_id": row["case_id"],
                    "reference": row["reference"],
                    "current": current,
                    "repair": _decision(decision),
                }
            )
    if winner:
        nominated = "model" if spec["disposition"] == "model" else "simple"
        selected = _predictions("winner", spec, members, grant, features, incumbent_outputs)
        _require(
            selected == [r["decisions"][nominated] for r in data["cases"]],
            "winning_artifact_did_not_replay",
        )
        fallback = _predictions("fallback", spec, members, grant, features, incumbent_outputs)
        _require(
            fallback == [_decision(r["fallback"]) for r in data["cases"]], "fallback_did_not_replay"
        )
    reports = {}
    for arm, cases in paired.items():
        reports[arm] = evaluate_manifest(
            {
                "schema_version": "opentour.pilot.case-manifest.v1",
                "error_ceiling_bps": data["error_ceiling_bps"],
                "cases": cases,
            }
        )
        snapshot = _object(arms[arm])
        _require(
            digest({c["case_id"]: c["repair"] for c in cases}) == snapshot.get("prediction_digest"),
            "benchmark_predictions_disagree_with_frozen_arm",
        )
        frozen_metrics = _object(snapshot.get("metrics"))
        _require(
            all(frozen_metrics.get(k) == v for k, v in reports[arm]["repair"].items()),
            "benchmark_metrics_disagree_with_frozen_arm",
        )
    return {
        "state": "replayed",
        "cases": len(data["cases"]),
        "evidence_revision": spec["evidence_revision"],
        "arm_readout_digests": {k: digest(v) for k, v in reports.items()},
        "fallback_rehearsed": winner is not None,
        "disposition": spec["disposition"],
    }


def _load(path: Path, grant: Json) -> tuple[Json, dict[str, bytes], bytes]:
    raw = _read(path, MAX_ARCHIVE)
    if "recipient_custody" in grant:
        _require(
            _sha(raw) == _entitlement(grant)["package_sha256"], "package_not_in_owner_entitlement"
        )
    spec, members = _unpack(raw, grant)
    _evidence(spec, members, grant)
    return spec, members, raw


def _release(spec: Json, grant: Json) -> None:
    if spec.get("release_required"):
        from opentour_cell.pilot.release_gate import authorize_release

        binding = spec["release"]
        authorize_release(
            Path(grant["store"]).parent,
            grant["organization"],
            binding["run_id"],
            binding["run_revision"],
            binding["artifact_sha"],
        )


def _export(spec_path: Path, source: Path, grant: Json) -> tuple[Json, bytes]:
    spec = parse_manifest(_read(spec_path))
    _spec(spec, grant)
    _release(spec, grant)
    _private_directory(source)
    members = {}
    for name, desc in spec["assets"].items():
        key = _member(name, desc)
        if key is not None:
            content = _read(source / (name + (".cms" if desc["mode"] == "sealed" else "")))
            _require(_sha(content) == desc.get("cipher_sha256", desc["sha256"]), "source_changed")
            members[key] = content
    blob = _zip(spec, members)
    # A sender need not hold a recipient's decryption key. Anchored asset hashes are checked
    # here, while the recipient independently decrypts and replays the package after receipt.
    if spec["assets"]["evidence.json"]["mode"] != "sealed":
        _evidence(spec, members, grant)
    return spec, blob


def _sealed_packet(
    spec_path: Path, source: Path, grant_path: Path, grant: Json
) -> tuple[Json, bytes]:
    spec, original = _export(spec_path, source, grant)
    _, members = _unpack(original, grant)
    names = grant.get("seal_assets")
    _require(
        isinstance(names, list)
        and 0 < len(names) <= len(MANDATORY)
        and len(names) == len(set(names))
        and set(names) <= MANDATORY,
        "explicit_seal_assets_required",
    )
    assert isinstance(names, list)
    certificate = _read(Path(grant.get("recipient_certificate", "")))
    cert_hash = _hash(grant.get("recipient_certificate_sha256"))
    _require(_sha(certificate) == cert_hash, "recipient_certificate_mismatch")
    for name in names:
        _require(spec["assets"][name]["mode"] == "local", "local_asset_expected")
    if "learning.json" in names:
        _learning(spec, members, grant)
    # A learned rule can encode customer vocabulary. Seal executable artifacts together with
    # the approved data so the encrypted transfer never reveals a rule mapping or adapter.
    names = sorted(set(names) | {n for n, d in spec["assets"].items() if d["mode"] == "included"})

    def approved(candidate: Json) -> None:
        restored = dict(candidate, assets=dict(candidate["assets"]))
        for asset in names:
            desc = _asset(candidate, asset)
            _require(
                desc["mode"] == "sealed" and desc["certificate_sha256"] == cert_hash,
                "seal_cache_conflict",
            )
            restored["assets"][asset] = {
                "mode": spec["assets"][asset]["mode"],
                "sha256": desc["sha256"],
            }
        _require(restored == spec, "seal_cache_conflict")
        _spec(candidate, dict(grant, spec_sha256=digest(candidate)))

    cache = source / ".handoff-envelopes"
    with _directory_lock(source):
        cache.mkdir(mode=0o700, exist_ok=True)
    _private_directory(cache)
    filename = digest([grant["spec_sha256"], cert_hash, sorted(names)]) + ".zip"
    with _directory_lock(cache):
        target = cache / filename
        if os.path.lexists(target):
            data = _read(target, MAX_ARCHIVE)
            transformed, _ = _unpack(data, grant, approved)
            return transformed, data
        transformed = dict(spec, assets=dict(spec["assets"]))
        with tempfile.TemporaryDirectory() as temp:
            key = Path(temp) / "recipient-cert"
            _write_private(key, certificate)
            for name in names:
                content = _value(name, spec, members, grant)
                original_member = _member(name, spec["assets"][name])
                if original_member is not None:
                    del members[original_member]
                cipher = _crypto(
                    ["cms", "-encrypt", "-binary", "-aes-256-gcm", "-outform", "DER", str(key)],
                    content,
                    "recipient_encryption_failed",
                )
                _require(len(cipher) <= MAX_MEMBER, "handoff_file_too_large")
                desc = {
                    "mode": "sealed",
                    "sha256": _sha(content),
                    "cipher_sha256": _sha(cipher),
                    "certificate_sha256": cert_hash,
                }
                transformed["assets"][name] = desc
                members[str(_member(name, desc))] = cipher
        approved(transformed)
        data = _zip(transformed, members)
        _publish(target, data, grant_path, grant)
        return transformed, data


def _issue(args: argparse.Namespace, grant: Json) -> Json:
    from opentour_cell.pilot.source_intake import inspect_intake

    _require("intake_revision" in grant, "current_source_intake_required")
    authority = _object(grant.get("portable"))
    _require(authority.get("evidence_access") is True, "portable_recipient_authorization_required")
    original, reference = _export(args.spec, args.source, grant)
    _, source_members = _unpack(reference, grant)
    raw = _read(args.archive, MAX_ARCHIVE)
    if raw == reference:
        spec = original
        _require(
            not any(d["mode"] == "included" for d in spec["assets"].values())
            or authority.get("clear_repair_transfer") is True,
            "encrypted_repair_transfer_required",
        )
    else:
        spec, reference = _sealed_packet(args.spec, args.source, args.grant, grant)
        _require(raw == reference, "package_not_from_approved_export")
    _, count = _learning(original, source_members, grant)
    source = inspect_intake(
        LocalTrainingStore(Path(grant["store"]), grant["organization"]), grant["intake_revision"]
    )
    cutoff = authority.get("expires_at")
    _require(
        type(cutoff) is int
        and time.time() < cutoff <= min(grant["expires_at"], source["expires_at"]),
        "bounded_recipient_cutoff_required",
    )
    _require(
        not count or authority.get("learning_access") is True,
        "portable_learning_authorization_required",
    )
    claim = {
        "schema_version": ENTITLEMENT,
        **{k: grant[k] for k in (*BOUND, "dataset_id", "store_engagement", "intake_revision")},
        "spec_sha256": digest(spec),
        "package_sha256": _sha(raw),
        "expires_at": cutoff,
        "learning_expires_at": min(cutoff, source["training_expires_at"]) if count else 0,
        "model_version": None,
        "model_grant_sha256": None,
        "supersedes": _hash(authority["supersedes"]) if "supersedes" in authority else None,
    }
    if spec["disposition"] == "model":
        from opentour_cell.pilot.open_weight import inspect_open_weight

        runtime = _object(grant["model_runtime"])
        native = _object(runtime["grant"])
        _require(
            all(native.get(k) == spec[k] for k in ("organization", "engagement")),
            "model_scope_mismatch",
        )
        path = Path(runtime["authority_bundle"])
        _private_directory(path)
        identity = inspect_open_weight(path, grant=native)
        _require(
            identity["artifact_sha256"] == spec["winner"]["version"]
            and cutoff <= native["expires_at"],
            "staged_model_authority_changed",
        )
        claim.update(model_version=identity["artifact_sha256"], model_grant_sha256=digest(native))
    public = _read(Path(authority["owner_public_key"]))
    _require(_sha(public) == _hash(authority["owner_key_sha256"]), "owner_trust_anchor_mismatch")
    with tempfile.TemporaryDirectory() as temporary:
        private_key, public_key, sig = [Path(temporary) / n for n in ("private", "public", "sig")]
        _write_private(private_key, _read(Path(authority["owner_signing_key"])))
        _write_private(public_key, public)
        if os.path.lexists(args.output):
            packet = parse_manifest(_read(args.output))
            _require(packet.get("claim") == claim, "entitlement_destination_conflict")
            signature = base64.b64decode(packet["signature"], validate=True)
        else:
            signature = _crypto(
                ["dgst", "-sha256", "-sign", str(private_key), "-passin", "pass:"],
                _bytes(claim),
                "owner_entitlement_signing_failed",
            )
        _write_private(sig, signature)
        _crypto(
            ["dgst", "-sha256", "-verify", str(public_key), "-signature", str(sig)],
            _bytes(claim),
            "invalid_owner_entitlement",
        )
    _release(spec, grant)
    _publish(
        args.output,
        _bytes({"claim": claim, "signature": base64.b64encode(signature).decode("ascii")}),
        args.grant,
        grant,
    )
    return {
        "state": "owner_issued",
        "spec_sha256": digest(spec),
        "package_sha256": _sha(raw),
        "expires_at": cutoff,
        "owner_key_sha256": _sha(public),
    }


def _bytes(value: Json) -> bytes:
    return (canonical(value) + "\n").encode()


def prepare_bundle_spec(
    directory: Path,
    *,
    manifest: Json,
    intake: Json,
    plan: Json,
    comparison: Json,
    rule: Json,
    model: Path | None = None,
) -> Json:
    """Stage an actual completed run locally. The custodian issues a separate delivery grant."""
    _private_directory(directory)
    authority = _object(manifest.get("handoff"))
    organization = _id(plan["organization"])
    engagement = _id(plan["engagement"])
    audience = _id(plan["audience"])
    intake_rev = _hash(intake["intake_revision"])
    _require(
        plan["intake_revision"]
        == manifest["intake_revision"]
        == comparison["intake_revision"]
        == intake_rev
        and plan["revision"] == comparison["plan_revision"]
        and plan["organization"] == intake["organization"]
        and plan["engagement"] == intake["engagement"]
        and all(comparison.get(k) == plan[k] for k in ("organization", "engagement", "audience"))
        and comparison["evidence_revision"]
        == digest({k: v for k, v in comparison.items() if k != "evidence_revision"}),
        "inconsistent_run_inputs",
    )
    policy = _id(authority["policy_revision"])
    decision = _object(comparison["decision"])
    selected = decision.get("winner")
    if authority.get("disposition") == "stop":
        state = "stop"
    elif decision.get("disposition") == "recommend" and selected in ("simple", "model"):
        state = "rule" if selected == "simple" else "model"
    else:
        state = "inconclusive"
    staged = {"evidence.json": _bytes(comparison)}
    bound = {"organization": organization, "engagement": engagement}
    winner, fallback = None, None
    if state in ("rule", "model"):
        chosen = _hash(comparison["arms"][selected]["revision"])
        winner = {
            "kind": "rule" if state == "rule" else "adapter",
            "version": chosen,
            "policy_revision": policy,
        }
        if state == "rule":
            from opentour_cell.pilot.simple_repair import validate_rule

            _require(validate_rule(rule)["revision"] == chosen, "wrong_rule_version")
            staged["winner.rule.json"] = _bytes(rule)
        else:
            _require(model is not None, "real_model_artifacts_required")
            assert model is not None
            staged["winner.adapter.json"] = _read(model / "adapter.json")
            staged["winner.model.json"] = _read(model / "manifest.json")
            _require(
                parse_manifest(staged["winner.model.json"]).get("artifact_sha256") == chosen,
                "wrong_model_version",
            )
        authorized = authority["fallback"]
        if authorized == "manual_review":
            fallback = {
                "kind": "manual",
                "version": digest(["human_review", policy]),
                "policy_revision": policy,
            }
        else:
            from opentour_cell.pilot.simple_repair import validate_rule

            prior = _object(authorized)
            _require(
                prior.get("kind") == "rule" and prior.get("policy_revision") == policy,
                "incompatible_fallback_policy",
            )
            prior_rule = validate_rule(prior["rule"])
            _require(
                prior_rule["revision"] == comparison["arms"]["incumbent"]["revision"],
                "unverified_incumbent_fallback",
            )
            fallback = {
                "kind": "rule",
                "version": prior_rule["revision"],
                "policy_revision": policy,
            }
            staged["fallback.rule.json"] = _bytes(prior_rule)
    protected = {row["case_id"]: row for row in intake["protected"]}
    rows = []
    for pair in comparison["paired_cases"]:
        case = protected.get(pair["case_id"])
        if case is None:
            raise BundleError("case_evidence_mismatch")
        _require(
            case["reference"] == pair["reference"] and case["incumbent"] == pair["incumbent"],
            "case_evidence_mismatch",
        )
        views = {arm: pair[arm] for arm in comparison["arms"]}
        point = {
            "case_id": pair["case_id"],
            "features": case["features"],
            "reference": pair["reference"],
            "decisions": views,
        }
        if fallback:
            point["fallback"] = (
                {"status": "review"} if fallback["kind"] == "manual" else pair["incumbent"]
            )
        rows.append(point)
    unscored = not rows and not comparison["arms"] and state in ("inconclusive", "stop")
    _require(
        unscored
        or (len(rows) == len(protected) and {r["case_id"] for r in rows} == set(protected)),
        "incomplete_benchmark",
    )
    staged["benchmark.json"] = _bytes(
        {
            "schema_version": BENCHMARK,
            **bound,
            "evidence_revision": comparison["evidence_revision"],
            "error_ceiling_bps": plan["terms"]["error_ceiling_bps"],
            "cases": rows,
        }
    )
    learn = []
    if authority.get("allow_learning_rows") is True:
        features = authority.get("learning_features")
        _require(
            isinstance(features, list)
            and features
            and len(features) == len(set(features))
            and set(features) <= set(intake["allowed_features"]),
            "learning_field_scope_required",
        )
        assert isinstance(features, list)
        _require(
            intake.get("training_allowed") is True and intake["training_expires_at"] > time.time(),
            "source_learning_permission_required",
        )
        permitted = set(intake["training_case_ids"])
        for case in intake["development"]:
            if case["reference"] is not None and case["case_id"] in permitted:
                learn.append(
                    {
                        "case_id": case["case_id"],
                        "features": {
                            k: case["features"][k] for k in features if k in case["features"]
                        },
                        "reference": case["reference"],
                    }
                )
    staged["learning.json"] = _bytes(
        {
            "schema_version": LEARNING,
            **bound,
            "purpose": "permitted_learning",
            "rows": learn,
            "exclusions": ["protected holdout", "outside explicitly granted learning fields"]
            + ([] if learn else ["no eligible or permitted learning rows"]),
        }
    )
    assets = {
        name: {"sha256": _sha(body), "mode": "local" if name in MANDATORY else "included"}
        for name, body in staged.items()
    }
    spec = {
        "schema_version": PACKAGE,
        **bound,
        "audience": audience,
        "policy_revision": policy,
        "exporter": _id(authority["exporter"]),
        "support_owner": _id(authority["support_owner"]),
        "evidence_revision": comparison["evidence_revision"],
        "intake_revision": intake_rev,
        "decision_time_assumptions": authority["decision_time_assumptions"],
        "limitations": authority["limitations"],
        "winner": winner,
        "fallback": fallback,
        "source_revisions": [intake_rev, plan["revision"]],
        "reference_provenance": [
            f"{role}/{_id(value['source_id'])}/{_hash(value['sha256'])}"
            for role, value in sorted(intake["sources"].items())
        ],
        "disposition": state,
        "assets": assets,
    }
    if "release_review" in manifest and winner is not None:
        spec.update(
            release_required=True,
            release={
                "run_id": directory.name,
                "run_revision": digest(comparison),
                "artifact_sha": winner["version"],
            },
        )
    ref = digest(spec)
    # Validate the structure before any file is published.
    _spec(
        spec,
        {
            **bound,
            "audience": audience,
            "role": "operator",
            "actor": spec["exporter"],
            "intake_revision": intake_rev,
            "policy_revision": policy,
            "spec_sha256": ref,
        },
    )
    staged["spec.json"] = _bytes(spec)
    output = directory / "handoff"
    with tempfile.TemporaryDirectory(dir=directory, prefix=".handoff-") as temporary:
        stage = Path(temporary) / "content"
        stage.mkdir(mode=0o700)
        for name, data in staged.items():
            _write_private(stage / name, data)
        handle = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if os.path.lexists(output):
                _private_directory(output)
                _require(
                    {p.name for p in output.iterdir()} - {".handoff-envelopes"} == set(staged)
                    and all(_read(output / n) == b for n, b in staged.items()),
                    "handoff_stage_conflict",
                )
            else:
                os.rename(stage, output)
                os.fsync(handle)
        finally:
            os.close(handle)
    return {
        **bound,
        "audience": audience,
        "policy_revision": policy,
        "dataset_id": intake["dataset_id"],
        "intake_revision": intake_rev,
        "store_engagement": engagement,
        "source_directory": str(output),
        "spec_path": str(output / "spec.json"),
        "spec_sha256": ref,
        "evidence_revision": spec["evidence_revision"],
        "disposition": state,
        "winner_version": winner["version"] if winner else None,
        "fallback_version": fallback["version"] if fallback else None,
        "local_assets": {
            d["sha256"]: str(output / n) for n, d in assets.items() if d["mode"] == "local"
        },
    }


def run(args: argparse.Namespace) -> Json:
    if args.command == "enroll-recipient":
        return _enroll_recipient(args.grant)
    if args.command == "withdraw-recipient":
        return _withdraw_recipient(args.grant)
    grant = _grant(args.grant)
    with _current(args.grant, grant):
        _require(
            grant["role"]
            == (
                "operator"
                if args.command in ("export", "seal", "issue-entitlement")
                else "recipient"
            ),
            "handoff_role_denied",
        )
    if args.command == "issue-entitlement":
        return _issue(args, grant)
    if args.command in ("export", "seal"):
        spec, raw = (
            _sealed_packet(args.spec, args.source, args.grant, grant)
            if args.command == "seal"
            else _export(args.spec, args.source, grant)
        )
        _release(spec, grant)
        _publish(args.output, raw, args.grant, grant)
        return {
            "state": "sealed" if args.command == "seal" else "exported",
            "sha256": _sha(raw),
            "spec_sha256": digest(spec),
            "evidence_revision": spec["evidence_revision"],
        }
    spec, members, raw = _load(args.archive, grant)
    if args.command in ("verify", "import", "replay"):
        result = _replay(spec, members, grant)
        _, count = _learning(spec, members, grant)
        result.update(learning_rows=count, package_sha256=_sha(raw))
        if args.command == "import":
            _publish(args.output, raw, args.grant, grant)
            result["state"] = "imported"
        elif args.command == "verify":
            result["state"] = "verified"
    elif args.command == "witness":
        result = _replay(spec, members, grant)
        _, count = _learning(spec, members, grant)
        fact = dict(
            result,
            actor=grant["actor"],
            learning_rows=count,
            package_sha256=_sha(raw),
            attestation=_id(args.attestation),
            kind="explicit_recipient_repeat",
        )
        if os.path.lexists(args.output):
            previous = parse_manifest(_read(args.output))
            _require(
                isinstance(previous.get("witnessed_utc"), str)
                and {k: v for k, v in previous.items() if k != "witnessed_utc"} == fact,
                "witness_receipt_conflict",
            )
            result = previous
        else:
            result = dict(fact, witnessed_utc=datetime.now(UTC).isoformat())
            _publish(args.output, (canonical(result) + "\n").encode(), args.grant, grant)
    elif args.command == "learning":
        body, count = _learning(spec, members, grant)
        _require(grant.get("materialize_learning") is True, "learning_materialization_denied")
        _publish(args.output, body, args.grant, grant)
        result = {"state": "materialized_in_private_custody", "learning_rows": count}
    else:
        _require(spec["winner"] is not None, "handoff_has_no_promoted_route")
        route = "fallback"
        if args.command == "predict":
            routes = {spec[n]["version"]: n for n in ("winner", "fallback")}
            _require(args.version in routes, "version_not_approved")
            route = routes[args.version]
        request = parse_manifest(_read(args.input))
        _require(
            set(request) <= {"features", "incumbent"} and "features" in request,
            "decision_time_features_only",
        )
        incumbent = _decision(request["incumbent"]) if "incumbent" in request else None
        decision = _predictions(
            route, spec, members, grant, [_object(request["features"])], [incumbent]
        )[0]
        result = {
            "state": "offline_prediction",
            "version": spec[route]["version"],
            "decision": decision,
            "policy_revision": spec["policy_revision"],
        }
    with _current(args.grant, grant):
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grant", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "export",
        "seal",
        "issue-entitlement",
        "withdraw-recipient",
        "enroll-recipient",
        "verify",
        "import",
        "replay",
        "predict",
        "fallback",
        "learning",
        "witness",
    ):
        sub = commands.add_parser(name)
        if name in ("export", "seal", "issue-entitlement"):
            sub.add_argument("--spec", type=Path, required=True)
            sub.add_argument("--source", type=Path, required=True)
        if name not in ("export", "seal", "enroll-recipient", "withdraw-recipient"):
            sub.add_argument("--archive", type=Path, required=True)
        if name in ("export", "seal", "issue-entitlement", "import", "learning", "witness"):
            sub.add_argument("--output", type=Path, required=True)
        if name in ("predict", "fallback"):
            sub.add_argument("--input", type=Path, required=True)
        if name == "witness":
            sub.add_argument("--attestation", required=True)
        if name == "predict":
            sub.add_argument("--version", required=True)
    try:
        print(canonical(run(parser.parse_args())))
    except BundleError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        ImportError,
        RuntimeError,
        EOFError,
        sqlite3.Error,
        BadZipFile,
        subprocess.SubprocessError,
    ):
        print("private_handoff_unavailable", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
