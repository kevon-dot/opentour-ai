"""Owner-local, optional supervised fine-tuning on one immutable pretrained checkpoint."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from pathlib import Path
from typing import Any

from opentour_cell.pilot.operating_point import digest as evidence_digest
from opentour_cell.pilot.simple_repair import RepairError, membership, validate_features

PROFILE = "smollm2_135m_lora_v1"
PROFILE_SHA256 = "4bf04f573c4d762f5c4288f56aca2e680ab00f9c57db183ef58dfbbbddb7804c"
BASE_SHA256 = "5af571cbf074e6d21a03528d2330792e532ca608f24ac70a143f6b369968ab8c"
REVISION = "12fd25f77366fa6b3b4b768ec3050bf629380bac"
GRANTS = (
    "model_authorized",
    "compute_authorized",
    "training_data_authorized",
    "model_use_authorized",
    "destination_authorized",
)


class OpenWeightError(ValueError):
    """An actionable hold on this optional profile."""


def _bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        + "\n"
    ).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(value: Any) -> str:
    return _sha(_bytes(value))


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def _text(value: object, limit: int = 96) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and value.strip() == value
        and value.isprintable()
        and "<|" not in value
    )


def _names(value: Any, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, list)
        and minimum <= len(value) <= maximum
        and all(_text(item) for item in value)
        and value == sorted(set(value))
    )


def _grant(grant: Mapping[str, Any], *, allow_expired: bool = False) -> str:
    if not isinstance(grant, Mapping) or grant.get("profile") != PROFILE:
        raise OpenWeightError("unsupported_open_weight_profile")
    if any(grant.get(key) is not True for key in GRANTS):
        raise OpenWeightError("model_compute_data_and_destination_approval_required")
    if any(
        not _text(grant.get(key), 256)
        for key in ("organization", "engagement", "trainer", "payer", "destination")
    ):
        raise OpenWeightError("named_scope_trainer_payer_and_destination_required")
    if not _text(grant.get("rights_attestation"), 256):
        raise OpenWeightError("model_and_data_rights_attestation_required")
    expiry = grant.get("expires_at")
    if type(expiry) is not int or (not allow_expired and expiry <= time.time()):
        raise OpenWeightError("current_open_weight_permission_required")
    if not _is_digest(grant.get("residual_revision")):
        raise OpenWeightError("approved_residual_revision_required")
    if not _names(grant.get("features"), 1, 8) or not _names(grant.get("labels"), 2, 8):
        raise OpenWeightError("unsupported_feature_or_label_schema")
    try:
        validate_features(grant["features"][0], grant["features"])
    except RepairError as exc:
        raise OpenWeightError("decision_time_feature_required") from exc
    seconds, cap, rate = (
        grant.get(key) for key in ("max_seconds", "max_cost_cents", "hourly_cost_cents")
    )
    if (
        type(seconds) is not int
        or not 1 <= seconds <= 300
        or type(cap) is not int
        or cap < 0
        or type(rate) is not int
        or rate != 0
    ):
        raise OpenWeightError("bounded_zero_cost_local_compute_required")
    return _digest(dict(grant))


def _feature_row(value: Any, names: Sequence[str]) -> bool:
    return (
        isinstance(value, dict)
        and value.keys() == set(names)
        and all(_text(field, 1024) for field in value.values())
        and sum(len(field) for field in value.values()) <= 2048
    )


def partition_fingerprints(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """Seal identifiers, related-case families and decision-time content without labels."""
    keys = set()
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or not _text(row.get("case_id"), 256)
            or not _text(row.get("family_id"), 256)
        ):
            raise OpenWeightError("case_and_family_required")
        fields = row.get("features")
        if (
            not isinstance(fields, dict)
            or not fields
            or not all(_text(k) and _text(v, 1024) for k, v in fields.items())
        ):
            raise OpenWeightError("invalid_partition_features")
        keys.update(
            {
                _digest(["case", row["case_id"]]),
                _digest(["family", row["family_id"]]),
                _digest(["features", fields]),
            }
        )
    return keys


def _resources() -> tuple[Path, Path]:
    local = Path(__file__).resolve().parent
    worker_name = "open_weight_worker.py"
    profile_name = PROFILE + ".json"
    candidates = [(local / worker_name, local / profile_name)]
    candidates.extend(
        (
            parent / "training/src/opentour_training/training" / worker_name,
            parent / "training/profiles" / profile_name,
        )
        for parent in local.parents
    )
    for worker, profile in candidates:
        if worker.is_file() and profile.is_file() and _sha(profile.read_bytes()) == PROFILE_SHA256:
            return worker, profile
    raise OpenWeightError("pinned_open_weight_worker_and_profile_required")


def _python(path: str | Path | None) -> str:
    if path is not None:
        return os.fspath(path)
    if os.environ.get("OPENTOUR_TRAINING_PYTHON"):
        return os.environ["OPENTOUR_TRAINING_PYTHON"]
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "training/.venv/bin/python"
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _marker(bundle: Path) -> Path:
    return bundle.with_name("." + bundle.name + ".withdrawn")


def _check_current(bundle: Path, check_permission: Callable[[], None] | None = None) -> None:
    if _marker(bundle).exists() or (bundle / "WITHDRAWN").exists():
        raise OpenWeightError("open_weight_authority_withdrawn")
    if check_permission is not None:
        try:
            check_permission()
        except OSError as exc:
            raise OpenWeightError("source_permission_check_unavailable") from exc


@contextmanager
def _locked(bundle: Path) -> Iterator[None]:
    import fcntl

    bundle.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = bundle.with_name("." + bundle.name + ".lock")
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _immutable_write(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        os.chmod(path, 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _run(
    bundle: Path,
    model_dir: str | Path,
    grant: Mapping[str, Any],
    python: str | Path | None,
    request: dict[str, Any],
    *,
    check_permission: Callable[[], None] | None = None,
    dispatch: Callable[[], AbstractContextManager[None]] = nullcontext,
) -> dict[str, Any]:
    _check_current(bundle, check_permission)
    worker, profile = _resources()
    directory = Path(model_dir).resolve()
    for name in json.loads(profile.read_bytes())["files"]:
        source = directory / name
        if source.is_symlink() or not source.is_file():
            raise OpenWeightError("staged_model_file_required")
    args = [_python(python), "-I", "-B", str(worker), str(profile)]
    request.update(
        model_dir=str(directory),
        max_seconds=grant["max_seconds"],
        labels=grant["labels"],
    )
    env = {
        "LANG": "C.UTF-8",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "RAYON_NUM_THREADS": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    deadline = time.monotonic() + grant["max_seconds"]
    try:
        with ExitStack() as process:
            with dispatch():
                _check_current(bundle, check_permission)
                proc = process.enter_context(
                    subprocess.Popen(
                        args,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        env=env,
                    )
                )
            pending: bytes | None = _bytes(request)
            try:
                while True:
                    try:
                        # communicate may still send buffered input after a timeout.
                        with dispatch():
                            _check_current(bundle, check_permission)
                            if time.time() >= grant["expires_at"]:
                                raise OpenWeightError("current_open_weight_permission_required")
                            left = deadline - time.monotonic()
                            if left <= 0:
                                raise OpenWeightError("open_weight_deadline_exceeded")
                            answer, _ = proc.communicate(pending, timeout=min(left, 0.2))
                    except subprocess.TimeoutExpired:
                        pending = None
                        continue
                    break
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
    except OSError as exc:
        raise OpenWeightError("pinned_numeric_interpreter_unavailable") from exc
    _check_current(bundle, check_permission)
    _grant(grant)
    try:
        result: dict[str, Any] = json.loads(answer)
    except (ValueError, UnicodeError) as exc:
        raise OpenWeightError("worker_interrupted_or_resource_limit") from exc
    if proc.returncode or "error" in result:
        raise OpenWeightError(result.get("error", "worker_interrupted_or_resource_limit"))
    return result


def _load(bundle: Path, grant_hash: str) -> tuple[dict[str, Any], dict[str, Any]]:
    _check_current(bundle)
    try:
        m, a = bundle / "manifest.json", bundle / "adapter.json"
        if bundle.is_symlink() or any(p.is_symlink() or p.stat().st_size > 131072 for p in (m, a)):
            raise OpenWeightError("invalid_open_weight_bundle")
        manifest: dict[str, Any] = json.loads(m.read_bytes())
        encoded = a.read_bytes()
        adapter: dict[str, Any] = json.loads(encoded)
        seal = dict(manifest)
        seal.pop("manifest_sha256")
        core = manifest["identity"]
        worker, _ = _resources()
        if (
            manifest["schema"] != "opentour.open_weight_fit.v1"
            or manifest["manifest_sha256"] != _digest(seal)
            or core["profile"] != PROFILE
            or core["profile_sha256"] != PROFILE_SHA256
            or core["source_sha256"] != _sha(worker.read_bytes())
            or core["base_sha256"] != BASE_SHA256
            or core["revision"] != REVISION
            or core["adapter_sha256"] != _sha(encoded)
            or manifest["artifact_sha256"] != _digest(core)
        ):
            raise OpenWeightError("open_weight_artifact_mismatch")
        if core["grant_sha256"] != grant_hash:
            raise OpenWeightError("open_weight_grant_mismatch")
        if len(base64.b64decode(adapter["a"], validate=True)) == 0:
            raise OpenWeightError("invalid_open_weight_bundle")
    except OpenWeightError:
        raise
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise OpenWeightError("invalid_or_incomplete_open_weight_bundle") from exc
    return manifest, adapter


def inspect_open_weight(bundle: str | Path, *, grant: Mapping[str, Any]) -> dict[str, Any]:
    """Check exact model identity, granted scope and saved adapter before allocating compute."""
    manifest, _ = _load(Path(bundle), _grant(grant))
    return manifest


def fit_open_weight(
    bundle: str | Path,
    training_rows: Sequence[Mapping[str, Any]],
    *,
    model_dir: str | Path,
    grant: Mapping[str, Any],
    residual: Mapping[str, Any],
    protected_fingerprints: Iterable[str],
    python: str | Path | None = None,
    check_permission: Callable[[], None] | None = None,
    dispatch: Callable[[], AbstractContextManager[None]] = nullcontext,
) -> dict[str, Any]:
    """Fit on the authorized training partition. Protected outcomes never enter the subprocess."""
    grant_hash = _grant(grant)
    _check_current(Path(bundle), check_permission)
    if (
        not isinstance(residual, Mapping)
        or residual.get("route") != "model"
        or any(residual.get(key) is not True for key in ("model_authorized", "compute_authorized"))
        or residual.get("revision") != grant["residual_revision"]
        or residual.get("residual_revision") != grant["residual_revision"]
        or type(residual.get("residual_count")) is not int
        or residual["residual_count"] < 1
    ):
        raise OpenWeightError("approved_residual_comparator_branch_required")
    if (
        residual.get("schema_version") != "opentour.pilot.development-decision.v1"
        or residual.get("fit_calls") != 0
        or evidence_digest(
            {k: v for k, v in residual.items() if k not in {"revision", "residual_revision"}}
        )
        != residual["revision"]
    ):
        raise OpenWeightError("unverified_development_decision")
    if not 2 <= len(training_rows) <= 32:
        raise OpenWeightError("training_rows_outside_profile")
    fields = {"case_id", "family_id", "features", "label"}
    if any(
        not isinstance(row, Mapping)
        or row.keys() != fields
        or not _feature_row(row["features"], grant["features"])
        or row["label"] not in grant["labels"]
        for row in training_rows
    ):
        raise OpenWeightError("invalid_training_rows_or_labels")
    protected = set(protected_fingerprints)
    if not protected or not all(_is_digest(item) for item in protected):
        raise OpenWeightError("sealed_protected_partition_required")
    if partition_fingerprints(training_rows) & protected:
        raise OpenWeightError("training_protected_partition_overlap")
    groups = [{"case_id": row["case_id"], "group_id": row["family_id"]} for row in training_rows]
    if not set(membership(groups, "case_id")) <= set(
        residual.get("development_case_keys", [])
    ) or not set(membership(groups, "group_id")) <= set(residual.get("development_group_keys", [])):
        raise OpenWeightError("training_cases_outside_approved_development")
    if len({row["case_id"] for row in training_rows}) != len(training_rows) or {
        row["label"] for row in training_rows
    } != set(grant["labels"]):
        raise OpenWeightError("unique_training_cases_and_all_labels_required")
    rows = sorted(training_rows, key=lambda row: row["case_id"])
    request_sha = _digest([rows, sorted(protected), grant_hash, residual["revision"]])
    path = Path(bundle)
    with _locked(path):
        _check_current(path, check_permission)
        if path.exists():
            recorded, _ = _load(path, grant_hash)
            if recorded["identity"]["request_sha256"] != request_sha:
                raise OpenWeightError("open_weight_fit_conflict")
            _check_current(path, check_permission)
            return recorded
        authority = path.with_name("." + path.name + ".authority")
        if authority.exists():
            if authority.read_bytes() != _bytes({"grant_sha256": grant_hash}):
                raise OpenWeightError("open_weight_grant_mismatch")
        else:
            _immutable_write(authority, _bytes({"grant_sha256": grant_hash}))
        result = _run(
            path,
            model_dir,
            grant,
            python,
            {
                "operation": "fit",
                "examples": [row["features"] for row in rows],
                "targets": [grant["labels"].index(row["label"]) for row in rows],
            },
            check_permission=check_permission,
            dispatch=dispatch,
        )
        encoded = _bytes(result.pop("adapter"))
        result.pop("pid")
        identity = {
            "profile": PROFILE,
            "profile_sha256": PROFILE_SHA256,
            "base_sha256": BASE_SHA256,
            "revision": REVISION,
            "source_sha256": _sha(_resources()[0].read_bytes()),
            "adapter_sha256": _sha(encoded),
            "request_sha256": request_sha,
            "training_sha256": _digest(rows),
            "protected_partition_sha256": _digest(sorted(protected)),
            "residual_revision": residual["revision"],
            "grant_sha256": grant_hash,
        }
        manifest = {
            "schema": "opentour.open_weight_fit.v1",
            "identity": identity,
            "artifact_sha256": _digest(identity),
            "training_rows": len(rows),
            "training": result,
            "incremental_compute_cost_cents": 0,
        }
        manifest["manifest_sha256"] = _digest(manifest)
        stage = Path(tempfile.mkdtemp(prefix="." + path.name + ".pending-", dir=path.parent))
        try:
            _immutable_write(stage / "adapter.json", encoded)
            _immutable_write(stage / "manifest.json", _bytes(manifest))
            with dispatch():
                _check_current(path, check_permission)
                stage.rename(path)
                try:
                    _check_current(path, check_permission)
                except BaseException:
                    # The engagement cannot withdraw during this guarded publication.
                    # Restore staging if another permission check fails.
                    path.rename(stage)
                    raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return manifest


def predict_open_weight_batch(
    bundle: str | Path,
    features: Sequence[dict[str, str]],
    *,
    model_dir: str | Path,
    grant: Mapping[str, Any],
    python: str | Path | None = None,
    check_permission: Callable[[], None] | None = None,
    dispatch: Callable[[], AbstractContextManager[None]] = nullcontext,
) -> list[dict[str, str]]:
    """Fresh-process inference from the saved projection and exact owner-staged base."""
    path = Path(bundle)
    _check_current(path, check_permission)
    _, adapter = _load(path, _grant(grant))
    if len(features) > 64:
        raise OpenWeightError("inference_batch_outside_profile")
    valid = [i for i, value in enumerate(features) if _feature_row(value, grant["features"])]
    decisions = [{"status": "review"} for _ in features]
    if valid:
        result = _run(
            path,
            model_dir,
            grant,
            python,
            {
                "operation": "predict",
                "examples": [features[i] for i in valid],
                "adapter": adapter,
            },
            check_permission=check_permission,
            dispatch=dispatch,
        )
        for i, value in zip(valid, result["decisions"], strict=True):
            decisions[i] = value
    _check_current(path, check_permission)
    return decisions


def predict_open_weight(
    bundle: str | Path,
    features: dict[str, str],
    *,
    model_dir: str | Path,
    grant: Mapping[str, Any],
    python: str | Path | None = None,
    check_permission: Callable[[], None] | None = None,
) -> dict[str, str]:
    return predict_open_weight_batch(
        bundle,
        [features],
        model_dir=model_dir,
        grant=grant,
        python=python,
        check_permission=check_permission,
    )[0]


def withdraw_open_weight(bundle: str | Path, *, grant: Mapping[str, Any]) -> None:
    """Persist a withdrawal before waiting for any in-flight worker to stop."""
    grant_hash = _grant(grant, allow_expired=True)
    path = Path(bundle)
    marker = _marker(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    authority = path.with_name("." + path.name + ".authority")
    if authority.exists() and authority.read_bytes() != _bytes({"grant_sha256": grant_hash}):
        raise OpenWeightError("open_weight_grant_mismatch")
    if marker.exists():
        if marker.read_bytes() != _bytes({"grant_sha256": grant_hash}):
            raise OpenWeightError("open_weight_grant_mismatch")
        return
    if path.exists():
        _load(path, grant_hash)
    try:
        _immutable_write(marker, _bytes({"grant_sha256": grant_hash}))
    except FileExistsError:
        if marker.read_bytes() != _bytes({"grant_sha256": grant_hash}):
            raise OpenWeightError("open_weight_grant_mismatch") from None
    with _locked(path):
        if path.exists() and not (path / "WITHDRAWN").exists():
            _immutable_write(path / "WITHDRAWN", _bytes({"grant_sha256": grant_hash}))
