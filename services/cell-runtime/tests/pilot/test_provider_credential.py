"""OPE-578 provider credential references, resolver, rotation, revocation and mandate binding.

Fixture kind for every test: generated. Every secret is a sentinel generated at test time and
every value-like string is assembled at runtime; no real key, provider or account is used. The
standing mandate is a real ``LearningMandates`` ledger in a temporary directory. Because its
schema forces ``max_cost_cents == 0``, paid-dispatch paths use ``ProviderCapTestSeam`` from the
tests-only ``provider_credential_seams`` module, registered into the source registry that is
empty in the source tree. These tests prove local software behavior only (EvidenceTier.FIXTURE).

Item 1  least-privilege reference record and value refusal ........ C1-C8
Item 2  value only at the locator; resolver; leak scan ............ R1-R7, L1-L3
Item 3  rotation drill with fresh-subprocess refusal of the old .... T1-T4
Item 4  durable append-only revocation; withdrawal; outcomes ...... V1-V7
Item 5  verified mandate owner, payer and cap before dispatch ..... D1-D9
Store   pinned mandate store identity (copy, recreate, replace) ... P1-P4
Scope   source registry, clock, evidence tier and the document .... S1-S3, E1
"""

from __future__ import annotations

import ast
import base64
import gc
import hashlib
import inspect
import json
import logging
import os
import pickle
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import provider_credential_seams as seams
import pytest
from opentour_cell.pilot import learning_mandate as lm
from opentour_cell.pilot import open_weight
from opentour_cell.pilot import provider_credential as pc
from opentour_cell.pilot.connector_ext import EvidenceTier
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.training import LocalTrainingStore

seams.register()


@pytest.fixture(autouse=True)
def _restore_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc, "_wall_clock", pc._wall_clock)


SRC = Path(pc.__file__).resolve().parents[2]
TEST_FILE = __file__
CHILD_PATH = os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH", "")]))
DOC = SRC.parent / "docs" / "pilot-provider-credential.md"
NOW = int(time.time())
ORG = "example-org"
OWNER, WORKER, PAYER = "owner-0001", "worker-0001", "payer-0001"
PROVIDER, ACCOUNT = "sentinel-provider", "customer-account-0001"
MANDATE = "standing-0001"


class Clock:
    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)


def sentinel() -> str:
    """A fresh generated secret. It never exists outside this test process and its files."""
    return "otsentinel" + secrets.token_hex(24)


# ----------------------------------------------------------------- generated mandate ledger
@dataclass
class MandateEnv:
    directory: Path
    engagement: str
    mandates: lm.LearningMandates
    runs: int = 0

    def run(self) -> str:
        self.runs += 1
        idx = self.runs
        source = {
            "source_revision": digest(["source", idx]),
            "trace_sha256": digest(["traces", idx]),
            "correction_sha256": digest(["outcomes", idx]),
            "workflow": "support",
            "connector": "approved-mcp",
            "trace_source": "tickets",
            "correction_source": "outcomes",
            "field": "route",
            "fields": ["question"],
            "rows": 10,
        }
        run_id = str(self.mandates.authorize_source(MANDATE, source, actor=WORKER)["run_id"])
        self.mandates.confirm_class(
            run_id,
            actor=WORKER,
            discovery_revision=digest(["discovery", run_id]),
            candidate={
                "from_label": "billing",
                "to_label": "technical",
                "class_id": "class-" + digest(["billing", "technical"])[:24],
                "count": 4,
                "groups": 3,
            },
        )
        return run_id

    def reservation(self, seconds: int = 10) -> dict[str, Any]:
        run_id = self.run()
        return {
            "run_id": run_id,
            "plan_revision": digest(["plan", run_id]),
            "intake_revision": digest(["intake", run_id]),
            "residual_revision": digest(["residual", run_id]),
            "max_seconds": seconds,
        }

    def args(self, directory: Path | None = None) -> list[str]:
        return [
            "--mandates",
            str(directory or self.directory),
            "--organization",
            ORG,
            "--engagement",
            self.engagement,
        ]


def mandate_spec(**changes: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "mandate_id": MANDATE,
        "owner_id": OWNER,
        "delegate": WORKER,
        "mode": "autonomous",
        "workflow": "support",
        "connectors": ["approved-mcp"],
        "trace_source": "tickets",
        "correction_source": "outcomes",
        "field": "route",
        "fields": ["question"],
        "labels": ["billing", "technical"],
        "profile": open_weight.PROFILE,
        "uses": ["inspect", "evaluate", "train"],
        "permitted_error_pairs": [["billing", "technical"]],
        "agreement_revision": digest("operating-limits"),
        "trainer": WORKER,
        "payer": PAYER,
        "reviewer": "reviewer-0001",
        "audience": "private-review-channel",
        "destination": "customer-local",
        "expires_at": NOW + 3_600,
        "max_runs": 12,
        "max_fits": 3,
        "max_total_seconds": 60,
        "max_seconds_per_fit": 20,
        "max_rows_per_run": 40,
        "max_total_rows": 240,
        "max_cost_cents": 0,
    }
    return spec | changes


@pytest.fixture
def env(tmp_path_factory: pytest.TempPathFactory) -> MandateEnv:
    directory = tmp_path_factory.mktemp("mandates")
    directory.chmod(0o700)
    store = LocalTrainingStore(directory / "training.sqlite3", ORG)
    mandates = lm.LearningMandates(directory, ORG, store.engagement_id, owner=OWNER)
    mandates.enroll(mandate_spec(), actor=OWNER)
    return MandateEnv(directory, store.engagement_id, mandates)


def seam(env: MandateEnv, cap: int = 5_000, consumed: int = 1_000, **kw: Any) -> Any:
    return seams.ProviderCapTestSeam(
        env.mandates,
        cap_minor_units=cap,
        provider=PROVIDER,
        account_id=ACCOUNT,
        consumed_minor_units=consumed,
        **kw,
    )


# ----------------------------------------------------------------- credential fixtures
@pytest.fixture
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("store")
    directory.chmod(0o700)
    return directory


@pytest.fixture
def ledger_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("ledger") / "state"


def put_secret(store: Path, name: str, value: str, mode: int = 0o600) -> Path:
    path = store / name
    path.write_text(value + "\n")
    path.chmod(mode)
    return path


def reference(locator: Path | str, **changes: Any) -> pc.CredentialReference:
    fields: dict[str, Any] = {
        "reference_id": "pcref-0001",
        "provider": PROVIDER,
        "account_id": ACCOUNT,
        "owner_ref": OWNER,
        "payer_ref": PAYER,
        "scope": frozenset({pc.Scope.FINE_TUNE, pc.Scope.MODEL_READ}),
        "locator_kind": pc.LocatorKind.FILE,
        "locator": str(locator),
        "budget_id": "budget-0001",
        "issued_at": NOW - 60,
        "not_after": NOW + 86_400,
    }
    return pc.CredentialReference(**(fields | changes))


def request(env: MandateEnv, job: str = "job-0001", **changes: Any) -> pc.DispatchRequest:
    fields: dict[str, Any] = {
        "job_ref": job,
        "reference_id": "pcref-0001",
        "mandate_id": MANDATE,
        "owner_id": OWNER,
        "payer": PAYER,
        "currency": "usd",
        "max_minor_units": 2_000,
    }
    fields |= changes
    if "reservation" not in fields:
        fields["reservation"] = env.reservation()
    return pc.DispatchRequest(**fields)


def ledger_at(
    directory: Path, clock: Clock | None = None, env: MandateEnv | None = None
) -> pc.CredentialLedger:
    if clock is not None:
        seams.use_clock(clock)
    ledger = pc.CredentialLedger(directory)
    if env is not None:
        ledger.bind_mandate_store(env.mandates)
    return ledger


def active_ledger(
    ledger_dir: Path,
    store: Path,
    value: str,
    clock: Clock | None = None,
    env: MandateEnv | None = None,
) -> pc.CredentialLedger:
    ledger = ledger_at(ledger_dir, clock, env)
    ledger.issue(reference(put_secret(store, "acct-0001.secret", value)))
    ledger.activate("pcref-0001")
    return ledger


def authorized(
    ledger_dir: Path, store: Path, env: MandateEnv, value: str
) -> tuple[pc.CredentialLedger, Any, pc.DispatchAuthorization, pc.CredentialResolver]:
    ledger = active_ledger(ledger_dir, store, value, env=env)
    source = seam(env, cap=50_000)
    auth = ledger.authorize_dispatch(request(env), mandates=source, actor=WORKER)
    resolver = pc.CredentialResolver(ledger, expected_uid=os.getuid(), mandates=source)
    return ledger, source, auth, resolver


def lines(ledger: pc.CredentialLedger) -> list[bytes]:
    return ledger.path.read_bytes().splitlines() if ledger.path.exists() else []


def code(excinfo: pytest.ExceptionInfo[pc.CredentialError]) -> str:
    return excinfo.value.code


def refusal(action: Callable[..., object], *args: Any, **kwargs: Any) -> str:
    with pytest.raises(pc.CredentialError) as excinfo:
        action(*args, **kwargs)
    return code(excinfo)


def forge(ledger: pc.CredentialLedger, kind: str, body: dict[str, Any]) -> None:
    """Append a correctly chained, canonical event that the module itself would not write."""
    existing = lines(ledger)
    prev = hashlib.sha256(existing[-1]).hexdigest() if existing else "0" * 64
    event = {
        "v": pc.VERSION,
        "seq": len(existing) + 1,
        "prev": prev,
        "kind": kind,
        "at": NOW,
        "body": body,
    }
    with ledger.path.open("ab") as stream:
        stream.write(pc._canonical(event) + b"\n")


# ----------------------------------------------------------------- runtime-built value shapes
def _prefixed() -> list[str]:
    body = secrets.token_hex(4)
    return [
        "sk" + "-" + body,
        "sk" + "_" + body,
        "ghp" + "_" + body,
        "xoxb" + "-" + body,
        "hf" + "_" + body,
        "glpat" + "-" + body,
        "AKIA" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ234567") for _ in range(16)),
        "AIza" + "x" * 20,
        "eyJ" + "h" * 12,
        "acct-" + "bearer" + ":" + body,
        "-----" + "BEGIN" + " key",
        "sk" + "-" + secrets.token_hex(24),
        "sk" + "-" + secrets.token_hex(30),
    ]


def _high_entropy() -> list[str]:
    return [
        secrets.token_urlsafe(32)[:32].replace("-", "a").replace("_", "b"),
        # Fragmented tokens whose pieces are each short: only the whole-value screen sees them.
        "-".join(["Xk9", "Qm2", "Lp7", "Rt4", "Wz8", "Hy3"]),
        "-".join(["qwzx", "plmk", "jnhb", "gvfc", "dxsr", "teyu", "oiaq"]),
        secrets.token_hex(20),
        secrets.token_hex(32),  # a bare 64-hex digest is never accepted from a caller
        "fit-" + secrets.token_hex(20),
        base64.b64encode(secrets.token_bytes(30)).decode(),
        "acct-" + secrets.token_hex(12),
        "q" * 4 + "".join(secrets.choice("bcdfghjklmnpqrstvwxz") for _ in range(20)),
        "acct-" + "k3" * 5,  # a 10-character letter and digit run with low entropy
        "acct-" + "Ab" * 5,  # a 10-character mixed-case run with low entropy
        str(uuid.uuid4()),  # a UUID is allowed only as a locator segment
        "00000000-0000-4000-8000-000000000001",  # low entropy: only the UUID rule sees it
    ]


ID_FIELDS = ("reference_id", "provider", "account_id", "owner_ref", "payer_ref", "budget_id")


# ================================================================= item 1: reference record
def test_c1_valid_reference_round_trips_without_any_value(store: Path) -> None:
    ref = reference(store / "acct-0001.secret")
    mapping = ref.to_mapping()
    assert set(mapping) == pc.REFERENCE_KEYS
    assert mapping["scope"] == ["fine_tune", "model_read"]
    assert pc.CredentialReference.from_mapping(mapping) == ref
    assert reference(store / "x.secret", budget_id=None).budget_id is None
    model_read_only = reference(store / "x.secret", scope=frozenset({pc.Scope.MODEL_READ}))
    assert model_read_only.scope == frozenset({pc.Scope.MODEL_READ})
    keyed = reference("customer-store/fine-tuning/acct-0001", locator_kind=pc.LocatorKind.STORE_KEY)
    assert keyed.locator_kind is pc.LocatorKind.STORE_KEY


@pytest.mark.parametrize("scope", [[], ["fine_tune", "admin"], ["inference"], ["files_write"]])
def test_c2_scope_outside_fine_tune_and_model_read_is_refused(
    store: Path, scope: list[str]
) -> None:
    mapping = reference(store / "x.secret").to_mapping() | {"scope": scope}
    assert refusal(lambda: pc.CredentialReference.from_mapping(mapping)) == (
        "scope_outside_least_privilege"
    )
    assert refusal(lambda: reference(store / "x.secret", scope=frozenset())) == (
        "scope_outside_least_privilege"
    )
    duplicate = reference(store / "x.secret").to_mapping() | {"scope": ["fine_tune"] * 2}
    assert refusal(lambda: pc.CredentialReference.from_mapping(duplicate)) == (
        "scope_outside_least_privilege"
    )


@pytest.mark.parametrize("field", ID_FIELDS)
def test_c3_known_key_prefixes_are_refused_in_every_field(store: Path, field: str) -> None:
    for value in _prefixed():
        assert refusal(reference, store / "x.secret", **{field: value}) == (
            "suspected_credential_prefix"
        ), field


@pytest.mark.parametrize("field", ID_FIELDS)
def test_c4_high_entropy_values_are_refused_in_every_field(store: Path, field: str) -> None:
    for value in _high_entropy():
        assert refusal(reference, store / "x.secret", **{field: value}) == (
            "suspected_credential_entropy"
        ), field


@pytest.mark.parametrize("field", ID_FIELDS)
def test_c5_values_over_the_length_bound_are_refused(store: Path, field: str) -> None:
    assert refusal(lambda: reference(store / "x.secret", **{field: "ab-" * 22})) == (
        "reference_field_too_long"
    )
    assert reference(store / "x.secret", **{field: "ab-" * 21 + "a"})  # 64 is the bound


def test_c6_locator_and_unknown_fields_cannot_carry_a_value(store: Path) -> None:
    for value in _prefixed():
        locator = str(store / value.lower().replace(" ", "-"))
        assert refusal(reference, locator) == "suspected_credential_prefix"
    for value in [v for v in _high_entropy() if not pc._UUID.search(v)]:
        assert refusal(reference, str(store) + "/" + value) == ("suspected_credential_entropy")
    assert refusal(lambda: reference("/" + "/".join(["segment"] * 26))) == (
        "reference_field_too_long"
    )
    for bad in ("relative/path.secret", "/etc/../x.secret", "/onlyone", str(store) + "/UPPER"):
        with pytest.raises(pc.CredentialError):
            reference(bad)
    mapping = reference(store / "x.secret").to_mapping()
    for extra in ("value", "api_key", "secret", "token"):
        extended = mapping | {extra: "generated-placeholder"}
        assert refusal(pc.CredentialReference.from_mapping, extended) == (
            "unknown_or_missing_reference_field"
        )
    missing = {k: v for k, v in mapping.items() if k != "budget_id"}
    assert refusal(lambda: pc.CredentialReference.from_mapping(missing)) == (
        "unknown_or_missing_reference_field"
    )
    with pytest.raises(pc.CredentialError):
        reference(store / "x.secret", issued_at=NOW, not_after=NOW)
    benign = ["customer-account-0001", "asia-pacific-0001", "fine-tuning-budget-2026-q4"]
    assert all(pc.suspected_value(v) is None for v in benign)


def test_c7_uuid_locator_segments_are_allowed_without_weakening_key_screens(store: Path) -> None:
    for _ in range(200):
        name = str(uuid.uuid4())
        assert reference(f"{store}/{name}.secret").locator.endswith(name + ".secret")
        assert reference(f"{store}/{name}/acct-0001.secret")
    name = str(uuid.uuid4())
    assert refusal(lambda: reference(f"{store}/sk-{name}")) == "suspected_credential_prefix"
    assert refusal(lambda: reference(f"{store}/{name}{secrets.token_hex(8)}")) == (
        "suspected_credential_entropy"
    )
    assert refusal(lambda: reference(store / "x.secret", account_id=name)) == (
        "suspected_credential_entropy"
    )


def test_c8_ledger_written_by_anyone_else_cannot_smuggle_a_value(
    ledger_dir: Path, store: Path
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel())
    first = json.loads(lines(ledger)[0])
    first["body"]["reference"]["account_id"] = secrets.token_hex(20)
    ledger.path.write_bytes(pc._canonical(first) + b"\n")
    assert refusal(lambda: ledger.status("pcref-0001")) == "credential_ledger_corrupt"


# ================================================================= item 2: resolver
def test_r1_resolver_reads_the_value_only_at_use_for_an_authorization(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    value = sentinel()
    ledger, _, auth, resolver = authorized(ledger_dir, store, env, value)
    assert value.encode() not in ledger.path.read_bytes()
    secret = resolver.resolve(auth.authorization_id, job_ref="job-0001")
    assert secret.reveal() == value
    assert value not in repr(secret) and value not in str(secret) and value not in f"{secret}"
    with pytest.raises(TypeError) as excinfo:
        pickle.dumps(secret)
    assert value not in str(excinfo.value)
    with pytest.raises(TypeError):
        json.dumps(secret)
    with pytest.raises(AttributeError):
        secret.__dict__  # noqa: B018 - slots only, nothing else to leak
    replacement = sentinel()
    put_secret(store, "acct-0001.secret", replacement)
    assert resolver.resolve(auth.authorization_id, job_ref="job-0001").reveal() == replacement


def test_r2_resolution_requires_a_journaled_unfenced_authorization(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    ledger, source, auth, resolver = authorized(ledger_dir, store, env, sentinel())
    other = "a" * 64
    assert refusal(lambda: resolver.resolve(other, job_ref="job-0001")) == (
        "dispatch_authorization_required"
    )
    assert refusal(lambda: resolver.resolve(auth.authorization_id, job_ref="job-0002")) == (
        "dispatch_authorization_required"
    )
    assert refusal(lambda: resolver.resolve("pcref-0001", job_ref="job-0001")) == (
        "dispatch_authorization_required"
    )
    # A second, settled job cannot resolve.
    second = ledger.authorize_dispatch(request(env, "job-0002"), mandates=source, actor=WORKER)
    ledger.record_outcome(
        "job-0002", pc.JobOutcome.COMPLETED, pc.ObservedBy.PROVIDER_REPORTED, "ev-0001"
    )
    assert refusal(lambda: resolver.resolve(second.authorization_id, job_ref="job-0002")) == (
        "dispatch_settled"
    )
    # A changed or withdrawn mandate stops resolution even for a journaled authorization.
    source.receipt_overrides["mandate_revision"] = "c" * 64
    assert refusal(lambda: resolver.resolve(auth.authorization_id, job_ref="job-0001")) == (
        "mandate_changed"
    )
    source.receipt_overrides.clear()
    assert resolver.resolve(auth.authorization_id, job_ref="job-0001")
    env.mandates.withdraw(MANDATE, actor=OWNER)
    assert refusal(lambda: resolver.resolve(auth.authorization_id, job_ref="job-0001")) == (
        "mandate_not_active"
    )
    ledger.revoke(["pcref-0001"], pc.RevocationCause.WITHDRAWAL)
    assert refusal(lambda: resolver.resolve(auth.authorization_id, job_ref="job-0001")) == (
        "dispatch_fenced"
    )


def _unsafe_cases(store: Path, value: str) -> dict[str, tuple[Callable[[], None], str]]:
    path = store / "acct-0001.secret"

    def mode(m: int) -> Callable[[], None]:
        return lambda: path.chmod(m)

    def final_link() -> None:
        real = put_secret(store, "real.secret", value)
        path.unlink()
        path.symlink_to(real)

    def parent_link() -> None:
        real = store.parent / (store.name + "-real")
        real.mkdir(mode=0o700)
        put_secret(real, "acct-0001.secret", value)
        for child in store.iterdir():
            child.unlink()
        store.rmdir()
        store.symlink_to(real)

    def hard_link() -> None:
        os.link(path, store / "second.secret")

    def directory() -> None:
        path.unlink()
        path.mkdir(mode=0o700)

    def fifo() -> None:
        path.unlink()
        os.mkfifo(path, 0o600)

    def empty() -> None:
        path.write_text("")

    def oversize() -> None:
        path.write_text("x" * (pc.MAX_SECRET_BYTES + 1))

    def spaced() -> None:
        path.write_text(value + " trailing words\n")

    def missing() -> None:
        path.unlink()

    def parent_writable() -> None:
        store.chmod(0o770)

    return {
        "mode-0640": (mode(0o640), "secret_mode_not_0600"),
        "mode-0400": (mode(0o400), "secret_mode_not_0600"),
        "mode-0666": (mode(0o666), "secret_mode_not_0600"),
        "final-link": (final_link, "secret_link_refused"),
        "parent-link": (parent_link, "secret_link_refused"),
        "hard-link": (hard_link, "secret_link_refused"),
        "directory": (directory, "secret_not_regular_file"),
        "fifo": (fifo, "secret_not_regular_file"),
        "empty": (empty, "secret_empty"),
        "oversize": (oversize, "secret_too_large"),
        "whitespace": (spaced, "secret_format_invalid"),
        "missing": (missing, "secret_unavailable"),
        "parent-writable": (parent_writable, "secret_parent_unsafe"),
    }


CASES = [
    "mode-0640",
    "mode-0400",
    "mode-0666",
    "final-link",
    "parent-link",
    "hard-link",
    "directory",
    "fifo",
    "empty",
    "oversize",
    "whitespace",
    "missing",
    "parent-writable",
]


@pytest.mark.parametrize("case", CASES)
def test_r3_unsafe_secret_files_are_refused(
    ledger_dir: Path, store: Path, env: MandateEnv, case: str
) -> None:
    value = sentinel()
    _, _, auth, resolver = authorized(ledger_dir, store, env, value)
    mutate, expected = _unsafe_cases(store, value)[case]
    mutate()
    with pytest.raises(pc.CredentialError) as excinfo:
        resolver.resolve(auth.authorization_id, job_ref="job-0001")
    assert code(excinfo) == expected
    assert value not in repr(excinfo.value) and value not in str(excinfo.value)


def test_r4_owner_other_than_the_dispatch_identity_is_refused(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    ledger, source, auth, _ = authorized(ledger_dir, store, env, sentinel())
    other = pc.CredentialResolver(ledger, expected_uid=os.getuid() + 1, mandates=source)
    # Non-root: the test-owned parent is not owned by root or the expected uid, so the parent
    # check refuses first. Root: a root-owned parent is permitted and the file owner is checked.
    expected = "secret_owner_mismatch" if os.getuid() == 0 else "secret_parent_unsafe"
    assert refusal(lambda: other.resolve(auth.authorization_id, job_ref="job-0001")) == expected
    if os.getuid() == 0:
        os.chown(store / "acct-0001.secret", 65534, 65534)
        root = pc.CredentialResolver(ledger, expected_uid=0, mandates=source)
        assert refusal(lambda: root.resolve(auth.authorization_id, job_ref="job-0001")) == (
            "secret_owner_mismatch"
        )


def test_r5_only_an_active_reference_inside_its_window_resolves(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    clock = Clock()
    ledger = ledger_at(ledger_dir, clock, env)
    source = seam(env)
    resolver = pc.CredentialResolver(ledger, expected_uid=os.getuid(), mandates=source)
    ledger.issue(reference(put_secret(store, "acct-0001.secret", sentinel())))
    assert refusal(
        lambda: ledger.authorize_dispatch(request(env), mandates=source, actor=WORKER)
    ) == ("reference_not_active")
    ledger.activate("pcref-0001")
    auth = ledger.authorize_dispatch(request(env), mandates=source, actor=WORKER)
    assert resolver.resolve(auth.authorization_id, job_ref="job-0001")
    clock.now = NOW + 86_400
    assert refusal(lambda: resolver.resolve(auth.authorization_id, job_ref="job-0001")) == (
        "reference_outside_window"
    )
    assert refusal(lambda: resolver.resolve(auth.authorization_id, job_ref=sentinel())) == (
        "suspected_credential_entropy"
    )


def test_r6_store_key_locators_need_a_secret_store_resolver(
    ledger_dir: Path, env: MandateEnv
) -> None:
    ledger = ledger_at(ledger_dir, env=env)
    ledger.issue(
        reference("customer-store/fine-tuning/acct-0001", locator_kind=pc.LocatorKind.STORE_KEY)
    )
    ledger.activate("pcref-0001")
    source = seam(env)
    auth = ledger.authorize_dispatch(request(env), mandates=source, actor=WORKER)
    resolver = pc.CredentialResolver(ledger, expected_uid=os.getuid(), mandates=source)
    assert refusal(lambda: resolver.resolve(auth.authorization_id, job_ref="job-0001")) == (
        "locator_kind_unsupported"
    )


def test_r7_ledger_storage_is_private_and_fails_closed(
    ledger_dir: Path, store: Path, env: MandateEnv, tmp_path: Path
) -> None:
    ledger, _, auth, resolver = authorized(ledger_dir, store, env, sentinel())
    assert (ledger_dir.stat().st_mode & 0o777, ledger.path.stat().st_mode & 0o777) == (
        0o700,
        0o600,
    )
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    open_dir.chmod(0o755)
    assert refusal(lambda: pc.CredentialLedger(open_dir)) == "ledger_directory_unsafe"
    ledger.path.chmod(0o644)
    assert refusal(lambda: resolver.resolve(auth.authorization_id, job_ref="job-0001")) == (
        "credential_ledger_unsafe"
    )


# ================================================================= leak scan (item 2)
class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def captured() -> Iterator[_Capture]:
    handler = _Capture()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def _b64_cores(data: bytes) -> list[str]:
    """The base64 characters that depend only on ``data``, at each of the three alignments."""
    cores = []
    for offset in range(3):
        encoded = base64.b64encode(b"\0" * offset + data).decode()
        start = -(-8 * offset // 6)
        end = (8 * (offset + len(data)) - 6) // 6 + 1
        core = encoded[start:end]
        cores += [core, core.replace("+", "-").replace("/", "_")]
    return cores


def leak_forms(needle: str) -> tuple[list[str], list[str], list[str]]:
    """(case-insensitive forms, case-sensitive forms, 12-character windows) of ``needle``."""
    raw = needle.encode()
    folded = [
        needle.lower(),
        needle[::-1].lower(),
        raw.hex(),
        "".join(f"%{b:02x}" for b in raw),
        "".join(f"\\u{b:04x}" for b in raw),
        json.dumps(needle)[1:-1].lower(),
    ]
    windows = sorted({needle[i : i + 12].lower() for i in range(len(needle) - 11)})
    return folded, _b64_cores(raw), windows


def _exception_texts(exc: BaseException, *, frames: bool = True) -> Iterator[str]:
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        yield str(item)
        yield repr(item)
        yield repr(item.args)
        yield repr(getattr(item, "__dict__", {}))
        if isinstance(item, UnicodeError):
            yield repr(getattr(item, "object", b""))
        tb = item.__traceback__ if frames else None
        while tb is not None:
            # Every frame below this test's own frames: module code and generated __init__.
            frame = tb.tb_frame
            if frame.f_code.co_filename != TEST_FILE:
                yield repr(frame.f_locals)
            tb = tb.tb_next
        pending.extend([item.__cause__, item.__context__])


def scan_for_leaks(
    needles: list[str],
    records: list[logging.LogRecord],
    exceptions: list[BaseException],
    objects: list[object],
    roots: list[Path],
    streams: list[str],
    input_refusals: tuple[BaseException, ...] = (),
) -> list[str]:
    """Every channel a value could escape through, except the caller-held ``SecretValue``.

    Each needle is searched as itself, upper/lower case, reversed, hex, URL-encoded, JSON
    escaped and base64 (standard and URL-safe) at all three alignments; log messages and
    arguments are also searched for every 12-character window of it. ``input_refusals`` are
    refusals of a value the caller itself passed in as a field: their message, args, repr,
    attributes and chained exceptions are scanned, but not their traceback frames, which
    necessarily hold the caller's own refused argument.
    """
    formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
    texts: list[str] = list(streams)
    log_texts: list[str] = []
    for record in records:
        parts = [record.getMessage(), repr(record.args), str(record.msg), formatter.format(record)]
        if isinstance(record.args, tuple):
            parts += [str(arg) for arg in record.args]
        texts += parts
        log_texts += parts
        if record.exc_info and record.exc_info[1] is not None:
            texts.extend(_exception_texts(record.exc_info[1]))
    for exc in exceptions:
        texts.extend(_exception_texts(exc))
    for exc in input_refusals:
        texts.extend(_exception_texts(exc, frames=False))
    for obj in objects:
        texts.append(repr(obj))
        texts.append(repr(getattr(obj, "__dict__", {})))
    texts += [
        path.read_bytes().decode("latin-1")
        for root in roots
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]
    # repr() doubles backslashes; also search the texts with that escaping undone.
    texts += [text.replace("\\\\", "\\") for text in texts if "\\\\" in text]
    lowered = [text.lower() for text in texts]
    log_lowered = [text.lower() for text in log_texts]
    found = []
    for needle in needles:
        folded, exact, windows = leak_forms(needle)
        for form in folded:
            if any(form in text for text in lowered):
                found.append("folded:" + str(folded.index(form)))
        if any(form in text for form in exact for text in texts):
            found.append("base64")
        if any(window in text for window in windows for text in log_lowered):
            found.append("window")
    return found


def _probe(
    ledger_dir: Path, env: MandateEnv, authorization: str, job: str, mandates: Path | None = None
) -> tuple[int, dict[str, Any], str]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.provider_credential",
            "probe",
            "--ledger",
            str(ledger_dir),
            "--authorization",
            authorization,
            "--job",
            job,
            "--expected-uid",
            str(os.getuid()),
            *env.args(mandates),
        ],
        env=dict(os.environ, PYTHONPATH=CHILD_PATH),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return completed.returncode, json.loads(completed.stdout), completed.stdout + completed.stderr


def test_l1_no_value_reaches_logs_exceptions_reprs_files_or_the_journal(
    ledger_dir: Path,
    store: Path,
    env: MandateEnv,
    tmp_path: Path,
    captured: _Capture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first, second, bad = sentinel(), sentinel(), sentinel()
    ledger = ledger_at(ledger_dir, env=env)  # real time, as the fresh probe processes use
    source = seam(env, cap=50_000)
    resolver = pc.CredentialResolver(ledger, expected_uid=os.getuid(), mandates=source)
    old = reference(put_secret(store, "acct-0001.secret", first))
    ledger.issue(old)
    rotation0 = ledger.activate("pcref-0001")
    exceptions: list[BaseException] = []
    refused_inputs: list[BaseException] = []
    objects: list[object] = [ledger, resolver, old, rotation0, source]

    def expect_refusal(action: Callable[[], object], *, input_refusal: bool = False) -> None:
        try:
            action()
        except pc.CredentialError as exc:
            assert exc.args == (exc.code,)
            (refused_inputs if input_refusal else exceptions).append(exc)
        else:  # pragma: no cover - a refusal is required
            raise AssertionError("refusal expected")

    auth1 = ledger.authorize_dispatch(request(env, "job-0001"), mandates=source, actor=WORKER)
    secret = resolver.resolve(auth1.authorization_id, job_ref="job-0001")
    assert secret.reveal() == first
    objects += [secret, auth1]
    new = reference(
        put_secret(store, "acct-0001-rotation-0002.secret", second), reference_id="pcref-0002"
    )
    rotation = ledger.rotate(new)
    objects += [new, rotation]
    auth2 = ledger.authorize_dispatch(
        request(env, "job-0002", reference_id="pcref-0002"), mandates=source, actor=WORKER
    )
    assert resolver.resolve(auth2.authorization_id, job_ref="job-0002").reveal() == second
    resolved_probe = _probe(ledger_dir, env, auth2.authorization_id, "job-0002")
    retired_probe = _probe(ledger_dir, env, auth1.authorization_id, "job-0001")
    expect_refusal(lambda: resolver.resolve(auth1.authorization_id, job_ref="job-0001"))
    # Refusals that happen after the value was read must not carry it.
    put_secret(store, "acct-0001-rotation-0002.secret", second + " " + bad)
    expect_refusal(lambda: resolver.resolve(auth2.authorization_id, job_ref="job-0002"))
    put_secret(store, "acct-0001-rotation-0002.secret", second)
    (store / "acct-0001-rotation-0002.secret").chmod(0o644)
    expect_refusal(lambda: resolver.resolve(auth2.authorization_id, job_ref="job-0002"))
    (store / "acct-0001-rotation-0002.secret").chmod(0o600)
    expect_refusal(lambda: reference(store / "x.secret", account_id=first), input_refusal=True)
    expect_refusal(lambda: reference(store / "x.secret", owner_ref=second), input_refusal=True)
    expect_refusal(
        lambda: resolver.resolve(auth2.authorization_id, job_ref=first), input_refusal=True
    )
    expect_refusal(
        lambda: ledger.authorize_dispatch(
            request(env, bad, reservation={}), mandates=source, actor=WORKER
        ),
        input_refusal=True,
    )
    revocation = ledger.withdraw(PROVIDER, ACCOUNT)
    objects += [revocation, ledger.job_status("job-0002")]
    expect_refusal(lambda: resolver.resolve(auth2.authorization_id, job_ref="job-0002"))
    status = ledger.record_outcome(
        "job-0002", pc.JobOutcome.UNKNOWN, pc.ObservedBy.NOT_OBSERVABLE, "evidence-0001"
    )
    objects.append(status)
    fenced_probe = _probe(ledger_dir, env, auth2.authorization_id, "job-0002")
    echo_probe = _probe(ledger_dir, env, auth2.authorization_id, first)
    out = capsys.readouterr()
    gc.collect()
    probes = (resolved_probe, retired_probe, fenced_probe, echo_probe)
    leaks = scan_for_leaks(
        [first, second, bad],
        captured.records,
        exceptions,
        objects,
        [ledger_dir, env.directory, tmp_path],
        [out.out, out.err, *(p[2] for p in probes)],
        tuple(refused_inputs),
    )
    assert leaks == []
    assert [p[1]["result"] for p in probes] == [
        "resolved_locally",
        "reference_retired",
        "dispatch_fenced",
        "suspected_credential_entropy",
    ]
    # The scan is live: it saw module log records, refusals and the journal.
    assert any(r.name == pc.__name__ for r in captured.records)
    assert len(exceptions) >= 4 and len(refused_inputs) == 4 and len(lines(ledger)) >= 6


def test_l2_the_leak_scanner_detects_every_channel_and_transform(
    tmp_path: Path, captured: _Capture
) -> None:
    needle = sentinel()
    raw = needle.encode()
    transforms = {
        "literal": needle,
        "upper": needle.upper(),
        "reversed": needle[::-1],
        "hex": raw.hex(),
        "hex-upper": raw.hex().upper(),
        "url-encoded": "".join(f"%{b:02X}" for b in raw),
        "json-escaped": json.dumps(needle, ensure_ascii=True).replace(
            needle, "".join(f"\\u{b:04x}" for b in raw)
        ),
        "base64-0": base64.b64encode(raw).decode(),
        "base64-1": base64.b64encode(b"x" + raw).decode(),
        "base64-2": base64.b64encode(b"xy" + raw).decode(),
        "base64-url": base64.urlsafe_b64encode(b"\xff" + raw).decode(),
    }
    for name, form in transforms.items():
        for channel in ("log", "exception", "object", "file", "stream"):
            captured.records.clear()
            records, exceptions, objects, roots, streams = [], [], [], [], []
            if channel == "log":
                logging.getLogger(pc.__name__).info("leaked %s", "pre" + form + "post")
                records = list(captured.records)
            elif channel == "exception":
                try:
                    raise RuntimeError("wrapped") from ValueError(form)
                except RuntimeError as exc:
                    exceptions = [exc]
            elif channel == "object":

                class Holder:
                    def __init__(self, kept: str = form) -> None:
                        self.kept = kept

                objects = [Holder()]
            elif channel == "file":
                (tmp_path / name).mkdir(exist_ok=True)
                (tmp_path / name / "journal.jsonl").write_text(json.dumps({"v": form}))
                roots = [tmp_path / name]
            else:
                streams = ["stdout " + form]
            assert scan_for_leaks([needle], records, exceptions, objects, roots, streams), (
                name,
                channel,
            )
            for path in roots:
                for child in path.iterdir():
                    child.unlink()
    # A 12-character fragment in a log argument is caught; the same in a non-log text is not
    # searched by window, so the window control is specific to logs.
    captured.records.clear()
    logging.getLogger(pc.__name__).info("part %s", needle[17:29])
    assert scan_for_leaks([needle], list(captured.records), [], [], [], []) == ["window"]
    assert scan_for_leaks([needle], [], [], [], [], [needle[17:29]]) == []
    assert scan_for_leaks([needle], [], [], [], [], ["unrelated text"]) == []


def test_l3_probe_never_echoes_its_inputs(ledger_dir: Path, store: Path, env: MandateEnv) -> None:
    ledger, _, auth, _ = authorized(ledger_dir, store, env, sentinel())
    del ledger
    needle = sentinel()
    status, body, output = _probe(ledger_dir, env, auth.authorization_id, "job-0001")
    assert (status, set(body)) == (0, {"result", "provider_side_refusal"})
    assert auth.authorization_id not in output and "job-0001" not in output
    for authorization, job in ((needle, "job-0001"), (auth.authorization_id, needle)):
        status, body, output = _probe(ledger_dir, env, authorization, job)
        assert status == 2 and set(body) == {"result", "provider_side_refusal"}
        assert needle not in output and str(ledger_dir) not in output


# ================================================================= item 3: rotation drill
def test_t1_rotation_drill_old_reference_refused_from_a_fresh_process(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    old_value, new_value = sentinel(), sentinel()
    ledger = ledger_at(ledger_dir, env=env)
    source = seam(env, cap=50_000)
    ledger.issue(reference(put_secret(store, "acct-0001.secret", old_value)))
    ledger.activate("pcref-0001")
    old = ledger.authorize_dispatch(request(env, "job-0001"), mandates=source, actor=WORKER)
    assert _probe(ledger_dir, env, old.authorization_id, "job-0001")[:2] == (
        0,
        {"result": "resolved_locally", "provider_side_refusal": "not_attempted_out_of_scope"},
    )
    receipt = ledger.rotate(
        reference(
            put_secret(store, "acct-0001-rotation-0002.secret", new_value),
            reference_id="pcref-0002",
        )
    )
    assert receipt == pc.RotationReceipt(
        "pcref-0001", "pcref-0002", "previous_reference_retired", "not_attempted_out_of_scope"
    )
    # The old value is still at its locator with safe permissions and its authorization is
    # still journaled and unfenced: the refusal is the ledger's, not an accident of file state.
    assert (store / "acct-0001.secret").read_text().strip() == old_value
    assert ledger.job_status("job-0001").fenced is False
    status, body, output = _probe(ledger_dir, env, old.authorization_id, "job-0001")
    assert (status, body["result"]) == (2, "reference_retired")
    assert body["provider_side_refusal"] == "not_attempted_out_of_scope"
    assert old_value not in output and new_value not in output
    new = ledger.authorize_dispatch(
        request(env, "job-0002", reference_id="pcref-0002"), mandates=source, actor=WORKER
    )
    status, body, output = _probe(ledger_dir, env, new.authorization_id, "job-0002")
    assert (status, body["result"]) == (0, "resolved_locally")
    assert old_value not in output and new_value not in output
    old_ref_dispatch = request(env, "job-0003", reference_id="pcref-0001")
    assert refusal(
        lambda: ledger.authorize_dispatch(old_ref_dispatch, mandates=source, actor=WORKER)
    ) == ("reference_not_active")


def test_t2_old_locator_cannot_be_reissued_or_reactivated(ledger_dir: Path, store: Path) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel())
    old_locator = ledger.reference("pcref-0001").locator
    ledger.rotate(
        reference(put_secret(store, "rotation-0002.secret", sentinel()), reference_id="pcref-0002")
    )
    before = lines(ledger)
    attempts: list[tuple[Callable[[], object], str]] = [
        (
            lambda: ledger.issue(reference(old_locator, reference_id="pcref-0003")),
            "secret_locator_reused",
        ),
        (
            lambda: ledger.rotate(reference(old_locator, reference_id="pcref-0003")),
            "secret_locator_reused",
        ),
        (lambda: ledger.activate("pcref-0001"), "reference_not_activatable"),
        (
            lambda: ledger.issue(reference(store / "fresh.secret", reference_id="pcref-0002")),
            "reference_already_issued",
        ),
    ]
    for attempt, expected in attempts:
        assert refusal(attempt) == expected
    assert lines(ledger) == before
    fresh = pc.CredentialLedger(ledger_dir)
    assert (fresh.status("pcref-0001"), fresh.status("pcref-0002")) == ("retired", "active")


def test_t3_rotation_needs_an_active_reference(ledger_dir: Path, store: Path) -> None:
    ledger = pc.CredentialLedger(ledger_dir)
    assert refusal(lambda: ledger.rotate(reference(store / "x.secret"))) == (
        "no_active_reference_to_rotate"
    )
    assert lines(ledger) == []


def test_t4_activation_respects_the_reference_window(ledger_dir: Path, store: Path) -> None:
    clock = Clock()
    ledger = ledger_at(ledger_dir, clock)
    ledger.issue(reference(store / "early.secret", issued_at=NOW + 100, not_after=NOW + 200))
    assert refusal(lambda: ledger.activate("pcref-0001")) == "reference_outside_window"
    clock.now = NOW + 150
    ledger.issue(reference(store / "late.secret", reference_id="pcref-0002", not_after=NOW + 160))
    clock.now = NOW + 170
    assert refusal(lambda: ledger.activate("pcref-0002")) == "reference_outside_window"
    clock.now = NOW + 150
    assert ledger.activate("pcref-0001").reference_id == "pcref-0001"


# ================================================================= item 4: revocation
def test_v1_revocation_fences_in_flight_jobs_and_never_assumes_an_outcome(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    ledger, source, _, _ = authorized(ledger_dir, store, env, sentinel())
    ledger.authorize_dispatch(request(env, "job-0002"), mandates=source, actor=WORKER)
    ledger.record_outcome(
        "job-0002", pc.JobOutcome.COMPLETED, pc.ObservedBy.PROVIDER_REPORTED, "ev-0001"
    )
    receipt = ledger.revoke(["pcref-0001"], pc.RevocationCause.WITHDRAWAL)
    assert receipt == pc.RevocationReceipt(
        ("pcref-0001",), pc.RevocationCause.WITHDRAWAL, ("job-0001",), "not_attempted_out_of_scope"
    )
    restarted = pc.CredentialLedger(ledger_dir)
    job = restarted.job_status("job-0001")
    assert job.fenced is True and job.outcome is None and job.observed_by is None
    assert restarted.job_status("job-0002").fenced is False
    assert restarted.status("pcref-0001") == "revoked"
    assert refusal(
        lambda: restarted.authorize_dispatch(
            request(env, "job-0003"), mandates=source, actor=WORKER
        )
    ) == ("reference_not_active")
    assert refusal(lambda: restarted.revoke(["pcref-0001"], pc.RevocationCause.WITHDRAWAL)) == (
        "reference_already_revoked"
    )
    assert refusal(lambda: restarted.revoke(["pcref-9999"], pc.RevocationCause.COMPROMISE)) == (
        "reference_unknown"
    )


def test_v2_outcomes_are_recorded_as_observed(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    ledger, _, _, _ = authorized(ledger_dir, store, env, sentinel())
    ledger.revoke(["pcref-0001"], pc.RevocationCause.COMPROMISE)
    record = ledger.record_outcome
    assert refusal(
        lambda: record("job-0001", pc.JobOutcome.CANCELED, pc.ObservedBy.NOT_OBSERVABLE, "ev-1")
    ) == ("observed_outcome_requires_observation")
    unknown = record("job-0001", pc.JobOutcome.UNKNOWN, pc.ObservedBy.NOT_OBSERVABLE, "ev-1")
    assert (unknown.fenced, unknown.outcome) == (True, pc.JobOutcome.UNKNOWN)
    running = record(
        "job-0001", pc.JobOutcome.RUNNING_AFTER_FENCE, pc.ObservedBy.PROVIDER_REPORTED, "ev-2"
    )
    assert running.outcome is pc.JobOutcome.RUNNING_AFTER_FENCE
    final = record("job-0001", pc.JobOutcome.CANCELED, pc.ObservedBy.PROVIDER_REPORTED, "ev-3")
    count = len(lines(ledger))
    assert (
        record("job-0001", pc.JobOutcome.CANCELED, pc.ObservedBy.PROVIDER_REPORTED, "ev-3") == final
    )
    assert len(lines(ledger)) == count
    assert refusal(
        lambda: record("job-0001", pc.JobOutcome.COMPLETED, pc.ObservedBy.PROVIDER_REPORTED, "e")
    ) == ("job_outcome_already_final")
    assert refusal(
        lambda: record("job-0009", pc.JobOutcome.UNKNOWN, pc.ObservedBy.NOT_OBSERVABLE, "e")
    ) == ("job_unknown")
    for bad in (secrets.token_hex(32), "sk" + "-" + secrets.token_hex(24)):
        assert refusal(
            record, "job-0001", pc.JobOutcome.UNKNOWN, pc.ObservedBy.NOT_OBSERVABLE, bad
        ) in {"suspected_credential_entropy", "suspected_credential_prefix"}
    assert len(lines(ledger)) == count


def test_v3_withdrawal_is_atomic_and_final(ledger_dir: Path, store: Path, env: MandateEnv) -> None:
    ledger, source, _, _ = authorized(ledger_dir, store, env, sentinel())
    ledger.rotate(
        reference(put_secret(store, "rotation-0002.secret", sentinel()), reference_id="pcref-0002")
    )
    ledger.authorize_dispatch(
        request(env, "job-0002", reference_id="pcref-0002"), mandates=source, actor=WORKER
    )
    ledger.issue(reference(store / "pending.secret", reference_id="pcref-0003"))
    before = len(lines(ledger))
    receipt = ledger.withdraw(PROVIDER, ACCOUNT)
    assert len(lines(ledger)) == before + 1  # one event revokes, fences and withdraws
    assert receipt.reference_ids == ("pcref-0001", "pcref-0002", "pcref-0003")
    assert receipt.fenced_job_refs == ("job-0001", "job-0002")
    assert receipt.cause is pc.RevocationCause.WITHDRAWAL
    assert receipt.provider_side_revocation == "not_attempted_out_of_scope"
    restarted = pc.CredentialLedger(ledger_dir)
    assert restarted.withdrawn(PROVIDER, ACCOUNT)
    assert {restarted.status(f"pcref-000{i}") for i in (1, 2, 3)} == {"revoked"}
    fresh_locator = store / "after-withdrawal.secret"
    assert refusal(
        lambda: restarted.issue(reference(fresh_locator, reference_id="pcref-0004"))
    ) == ("account_withdrawn")
    assert refusal(lambda: restarted.activate("pcref-0003")) == "reference_not_activatable"
    assert refusal(lambda: restarted.withdraw(PROVIDER, ACCOUNT)) == "account_already_withdrawn"
    assert refusal(lambda: restarted.withdraw(PROVIDER, "customer-account-0002")) == (
        "no_reference_to_withdraw"
    )
    # Re-enrolment with an owner reference read back from the ledger itself is refused, and so
    # is every other form: the mandate store cannot express a post-withdrawal owner approval.
    owner_ref = restarted.reference("pcref-0001").owner_ref
    count = len(lines(restarted))
    for kwargs in (
        {"owner_ref": owner_ref},
        {"owner_ref": owner_ref, "mandates": env.mandates},
        {},
    ):
        assert refusal(restarted.reenroll, PROVIDER, ACCOUNT, **kwargs) == (
            "reenrolment_requires_signed_owner_record"
        )
    assert len(lines(restarted)) == count
    assert restarted.withdrawn(PROVIDER, ACCOUNT)
    assert refusal(restarted.issue, reference(fresh_locator, reference_id="pcref-0004")) == (
        "account_withdrawn"
    )
    dispatch = request(env, "job-0009", reference_id="pcref-0002")
    assert refusal(restarted.authorize_dispatch, dispatch, mandates=source, actor=WORKER) == (
        "account_withdrawn"
    )
    # A forged re-enrolment event (the old event kind) is refused on read.
    forge(
        restarted, "reenrolled", {"provider": PROVIDER, "account_id": ACCOUNT, "owner_ref": OWNER}
    )
    assert refusal(restarted.status, "pcref-0001") == "credential_ledger_corrupt"


def test_v4_forged_events_that_break_withdrawal_or_activity_rules_fail_closed(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    ledger, source, auth, _ = authorized(ledger_dir, store, env, sentinel())
    ledger.issue(reference(store / "pending.secret", reference_id="pcref-0002"))
    pristine = ledger.path.read_bytes()
    # A withdrawal that leaves a reference unrevoked (a torn snapshot) is refused on read.
    forge(
        ledger,
        "withdrawn",
        {
            "provider": PROVIDER,
            "account_id": ACCOUNT,
            "reference_ids": ["pcref-0001"],
            "fenced_job_refs": ["job-0001"],
            "provider_side": pc.PROVIDER_SIDE,
        },
    )
    assert refusal(lambda: ledger.status("pcref-0001")) == "credential_ledger_corrupt"
    # A revocation whose fenced list omits an in-flight job is refused on read.
    ledger.path.write_bytes(pristine)
    forge(
        ledger,
        "revoked",
        {
            "reference_ids": ["pcref-0001"],
            "cause": "withdrawal",
            "fenced_job_refs": [],
            "provider_side": pc.PROVIDER_SIDE,
        },
    )
    assert refusal(lambda: ledger.status("pcref-0001")) == "credential_ledger_corrupt"
    # A dispatch journaled against a revoked reference is refused on read.
    ledger.path.write_bytes(pristine)
    ledger.revoke(["pcref-0001"], pc.RevocationCause.COMPROMISE)
    body = dict(ledger._fold().jobs["job-0001"].body, job_ref="job-0002")
    body["authorization_id"] = pc._authorization_id(body)
    forge(ledger, "dispatch_authorized", body)
    assert refusal(lambda: ledger.status("pcref-0001")) == "credential_ledger_corrupt"
    # An authorization id that is not the module's digest of its body is refused on read.
    ledger.path.write_bytes(pristine)
    body = dict(ledger._fold().jobs["job-0001"].body, job_ref="job-0002")
    forge(ledger, "dispatch_authorized", body)
    assert refusal(lambda: ledger.status("pcref-0001")) == "credential_ledger_corrupt"
    del source, auth


def test_v5_the_journal_is_append_only_and_tamper_evident(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    ledger, _, _, _ = authorized(ledger_dir, store, env, sentinel())
    snapshot = ledger.path.read_bytes()
    ledger.revoke(["pcref-0001"], pc.RevocationCause.WITHDRAWAL)
    grown = ledger.path.read_bytes()
    assert grown.startswith(snapshot) and len(grown) > len(snapshot)
    events = [json.loads(line) for line in grown.splitlines()]
    assert [e["kind"] for e in events] == [
        "store_bound",
        "issued",
        "activated",
        "dispatch_authorized",
        "revoked",
    ]
    assert [e["seq"] for e in events] == [1, 2, 3, 4, 5]
    bound, *original = grown.splitlines(keepends=True)

    def corrupt(data: bytes) -> str:
        ledger.path.write_bytes(bound + data)
        return refusal(lambda: ledger.status("pcref-0001"))

    assert corrupt(b"".join(original[:2] + original[3:])) == "credential_ledger_corrupt"
    edited = json.loads(original[2])
    edited["body"]["max_minor_units"] = 1
    assert corrupt(b"".join(original[:2]) + pc._canonical(edited) + b"\n" + original[3]) == (
        "credential_ledger_corrupt"
    )
    assert corrupt(b"".join([original[1], original[0], *original[2:]])) == (
        "credential_ledger_corrupt"
    )
    body = b"".join(original)
    assert corrupt(body[:-5]) == "credential_ledger_corrupt"
    assert corrupt(body[:-1]) == "credential_ledger_corrupt"  # final line without a newline
    assert corrupt(body + b"not json\n") == "credential_ledger_corrupt"
    # The same final event in a non-canonical encoding (only the last line, so the chain holds).
    loose = json.dumps(json.loads(original[3])).encode() + b"\n"
    assert corrupt(b"".join(original[:3]) + loose) == "credential_ledger_corrupt"
    ledger.path.write_bytes(grown)
    assert ledger.status("pcref-0001") == "revoked"
    # Stated limitation: with no external anchor, dropping the final event is not detected.
    ledger.path.write_bytes(bound + b"".join(original[:3]))
    assert ledger.status("pcref-0001") == "active"


def test_v6_a_settled_or_uncertain_job_is_never_reauthorized(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    ledger, source, auth, _ = authorized(ledger_dir, store, env, sentinel())
    first = request(env, "job-0002")
    second = request(env, "job-0003")
    ledger.authorize_dispatch(first, mandates=source, actor=WORKER)
    ledger.authorize_dispatch(second, mandates=source, actor=WORKER)
    ledger.record_outcome(
        "job-0002", pc.JobOutcome.COMPLETED, pc.ObservedBy.PROVIDER_REPORTED, "ev-0001"
    )
    ledger.record_outcome("job-0003", pc.JobOutcome.UNKNOWN, pc.ObservedBy.NOT_OBSERVABLE, "ev-2")
    count = len(lines(ledger))
    assert refusal(lambda: ledger.authorize_dispatch(first, mandates=source, actor=WORKER)) == (
        "job_already_settled"
    )
    assert refusal(lambda: ledger.authorize_dispatch(second, mandates=source, actor=WORKER)) == (
        "job_outcome_recorded"
    )
    assert len(lines(ledger)) == count
    del auth


def test_v7_job_and_evidence_refs_cannot_carry_digest_shaped_values(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel(), env=env)
    source = seam(env)
    before = lines(ledger)
    for job in (
        "sk" + "-" + secrets.token_hex(24),
        "sk" + "-" + secrets.token_hex(30),
        secrets.token_hex(32),
        "fit-" + secrets.token_hex(20),
    ):
        dispatch = request(env, job, reservation={})
        assert refusal(ledger.authorize_dispatch, dispatch, mandates=source, actor=WORKER) in {
            "suspected_credential_prefix",
            "suspected_credential_entropy",
        }
    assert lines(ledger) == before


# ================================================================= item 5: mandate binding
def test_d1_verified_mandate_authorizes_reserves_and_is_journaled(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel(), env=env)
    source = seam(env)
    dispatch = request(env)
    auth = ledger.authorize_dispatch(dispatch, mandates=source, actor=WORKER)
    receipt = env.mandates.inspect(MANDATE, actor=OWNER)
    assert (auth.mandate_id, auth.mandate_revision) == (MANDATE, receipt["mandate_revision"])
    assert receipt["reserved_fits"] == 1 and auth.reservation_id.startswith("fit-")
    assert source.costs == {auth.reservation_id: 2_000}
    count = len(lines(ledger))
    assert ledger.authorize_dispatch(dispatch, mandates=source, actor=WORKER) == auth
    assert len(lines(ledger)) == count
    assert env.mandates.inspect(MANDATE, actor=OWNER)["reserved_fits"] == 1
    assert refusal(
        lambda: ledger.authorize_dispatch(
            replace(dispatch, max_minor_units=1_000), mandates=source, actor=WORKER
        )
    ) == ("job_already_authorized")
    job = ledger.job_status("job-0001")
    assert (job.authorization_id, job.fenced, job.outcome) == (auth.authorization_id, False, None)


def test_d2_a_real_mandate_holds_every_paid_dispatch_until_it_records_a_cap(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel(), env=env)
    before = lines(ledger)
    dispatch = request(env)
    assert refusal(
        lambda: ledger.authorize_dispatch(dispatch, mandates=env.mandates, actor=WORKER)
    ) == ("mandate_cap_required")
    invented = replace(dispatch, mandate_id="invented-0001")
    assert refusal(
        lambda: ledger.authorize_dispatch(invented, mandates=env.mandates, actor=WORKER)
    ) == ("mandate_standing_mandate_unavailable")
    assert refusal(
        lambda: ledger.authorize_dispatch(dispatch, mandates=env.mandates, actor="intruder-0001")
    ) == ("mandate_mandate_scope_mismatch")
    assert refusal(
        lambda: ledger.authorize_dispatch(dispatch, mandates=env.mandates, actor=OWNER)
    ) == ("mandate_delegate_mismatch")
    assert lines(ledger) == before
    assert env.mandates.inspect(MANDATE, actor=OWNER)["reserved_fits"] == 0


def test_d3_mandate_sources_other_than_the_verified_ledger_are_refused(
    ledger_dir: Path, store: Path, env: MandateEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel(), env=env)
    dispatch = request(env)

    class Subclass(lm.LearningMandates):
        pass

    class Lookalike:
        def inspect(self, mandate_id: str, *, actor: str | None = None) -> dict[str, Any]:
            return {}

    subclass = Subclass(env.directory, ORG, env.engagement)
    for source in (subclass, Lookalike(), None):
        assert refusal(ledger.authorize_dispatch, dispatch, mandates=source, actor=WORKER) == (
            "mandate_source_untrusted"
        )
    live = seam(env)
    monkeypatch.setattr(pc, "_SOURCE_REGISTRY", {})
    assert refusal(ledger.authorize_dispatch, dispatch, mandates=live, actor=WORKER) == (
        "mandate_source_untrusted"
    )
    resolver_args = {"expected_uid": os.getuid(), "mandates": live}
    assert refusal(pc.CredentialResolver, ledger, **resolver_args) == "mandate_source_untrusted"
    # A registered type must still wrap an exact LearningMandates.
    monkeypatch.setattr(
        pc,
        "_SOURCE_REGISTRY",
        {seams.ProviderCapTestSeam: pc._SourceHooks(lambda s: subclass, lambda *a: {})},
    )
    assert refusal(ledger.authorize_dispatch, dispatch, mandates=live, actor=WORKER) == (
        "mandate_source_untrusted"
    )


MANDATE_REFUSALS: list[tuple[str, dict[str, Any], dict[str, Any], str]] = [
    ("no-owner", {"scope_overrides": {"owner_id": None}}, {}, "mandate_view_invalid"),
    ("no-payer", {"scope_overrides": {"payer": None}}, {}, "mandate_view_invalid"),
    ("zero-cap", {"cap": 0}, {}, "mandate_cap_required"),
    ("negative-cap", {"cap": -5}, {}, "mandate_view_invalid"),
    ("float-cap", {"cap": 5_000.0}, {}, "mandate_view_invalid"),
    ("bool-cap", {"cap": True}, {}, "mandate_view_invalid"),
    ("none-cap", {"scope_overrides": {"max_cost_cents": None}}, {}, "mandate_view_invalid"),
    ("negative-consumed", {"consumed": -100_000}, {}, "mandate_view_invalid"),
    ("float-consumed", {"consumed": 0.5}, {}, "mandate_view_invalid"),
    ("text-consumed", {"consumed": "0"}, {}, "mandate_view_invalid"),
    (
        "no-consumed",
        {"receipt_overrides": {"cumulative_reserved_cost_cents": None}},
        {},
        "mandate_cost_usage_unavailable",
    ),
    ("none-expiry", {"receipt_overrides": {"expires_at": None}}, {}, "mandate_view_invalid"),
    ("text-expiry", {"receipt_overrides": {"expires_at": "soon"}}, {}, "mandate_view_invalid"),
    (
        "float-expiry",
        {
            "receipt_overrides": {"expires_at": float(NOW + 3_600)},
            "scope_overrides": {"expires_at": float(NOW + 3_600)},
        },
        {},
        "mandate_view_invalid",
    ),
    ("past-expiry", {"receipt_overrides": {"expired": True}}, {}, "mandate_not_active"),
    ("withdrawn", {"receipt_overrides": {"withdrawn": True}}, {}, "mandate_not_active"),
    ("manual-mode", {"scope_overrides": {"mode": "manual"}}, {}, "mandate_not_active"),
    (
        "prefixed-revision",
        {"receipt_overrides": {"mandate_revision": "sk" + "-" + "a" * 48}},
        {},
        "mandate_view_invalid",
    ),
    (
        "other-mandate-receipt",
        {"receipt_overrides": {"mandate_id": "standing-0002"}},
        {},
        "mandate_view_invalid",
    ),
    (
        "owner-not-in-spec",
        {"receipt_overrides": {"owner_id": "owner-0002"}},
        {},
        "mandate_view_invalid",
    ),
    (
        "no-provider",
        {"receipt_overrides": {"provider": None}},
        {},
        "mandate_provider_account_required",
    ),
    ("request-owner", {}, {"owner_id": "owner-0002"}, "mandate_owner_mismatch"),
    ("request-payer", {}, {"payer": "payer-0002"}, "mandate_payer_mismatch"),
    (
        "account",
        {"receipt_overrides": {"provider_account_id": "customer-account-0002"}},
        {},
        "mandate_account_mismatch",
    ),
    ("currency", {}, {"currency": "eur"}, "mandate_currency_mismatch"),
    ("over-remaining", {}, {"max_minor_units": 4_001}, "mandate_cap_exceeded"),
    ("consumed", {"consumed": 5_000}, {}, "mandate_cap_exceeded"),
    ("zero-bound", {}, {"max_minor_units": 0}, "mandate_cap_exceeded"),
    ("float-bound", {}, {"max_minor_units": 10.5}, "mandate_cap_exceeded"),
    ("seconds", {}, {"reservation_seconds": 21}, "mandate_per_fit_budget_exceeded"),
]


@pytest.mark.parametrize(
    ("view", "changes", "expected"),
    [case[1:] for case in MANDATE_REFUSALS],
    ids=[case[0] for case in MANDATE_REFUSALS],
)
def test_d4_dispatch_is_refused_without_a_matching_well_formed_mandate(
    ledger_dir: Path,
    store: Path,
    env: MandateEnv,
    view: dict[str, Any],
    changes: dict[str, Any],
    expected: str,
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel(), env=env)
    source = seam(env, **view)
    before = lines(ledger)
    seconds = changes.pop("reservation_seconds", 10)
    dispatch = request(env, reservation=env.reservation(seconds), **changes)
    assert refusal(lambda: ledger.authorize_dispatch(dispatch, mandates=source, actor=WORKER)) == (
        expected
    )
    assert lines(ledger) == before
    assert refusal(lambda: ledger.job_status("job-0001")) == "job_unknown"
    if expected != "mandate_per_fit_budget_exceeded":
        assert env.mandates.inspect(MANDATE, actor=OWNER)["reserved_fits"] == 0


def test_d5_the_cap_is_cumulative_and_inclusive(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel(), env=env)
    source = seam(env, cap=5_000, consumed=0)
    assert ledger.authorize_dispatch(
        request(env, "job-0001", max_minor_units=5_000), mandates=source, actor=WORKER
    )
    for idx in range(2, 6):
        dispatch = request(env, f"job-000{idx}", max_minor_units=5_000)
        assert refusal(ledger.authorize_dispatch, dispatch, mandates=source, actor=WORKER) == (
            "mandate_cap_exceeded"
        )
    # A second credential ledger sharing the mandate still sees the mandate's own usage.
    other = ledger_at(ledger_dir.parent / "other", env=env)
    other.issue(reference(store / "other.secret"))
    other.activate("pcref-0001")
    dispatch = request(env, "job-0009", max_minor_units=1)
    assert refusal(lambda: other.authorize_dispatch(dispatch, mandates=source, actor=WORKER)) == (
        "mandate_cap_exceeded"
    )


def test_d5b_the_local_journal_bounds_spend_when_the_mandate_usage_lags(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel(), env=env)
    # The mandate keeps reporting zero usage; only this ledger's journal knows about job-0001.
    lagging = seam(
        env, cap=5_000, consumed=0, receipt_overrides={"cumulative_reserved_cost_cents": 0}
    )
    ledger.authorize_dispatch(
        request(env, "job-0001", max_minor_units=5_000), mandates=lagging, actor=WORKER
    )
    dispatch = request(env, "job-0002", max_minor_units=5_000)
    assert refusal(ledger.authorize_dispatch, dispatch, mandates=lagging, actor=WORKER) == (
        "mandate_cap_exceeded"
    )


def test_d6_the_mandate_ledger_enforces_its_own_fit_budget(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel(), env=env)
    source = seam(env, cap=50_000, consumed=0)
    for idx in range(1, 4):
        ledger.authorize_dispatch(
            request(env, f"job-000{idx}", max_minor_units=100), mandates=source, actor=WORKER
        )
    dispatch = request(env, "job-0004", max_minor_units=100, reservation=env.reservation(5))
    assert refusal(lambda: ledger.authorize_dispatch(dispatch, mandates=source, actor=WORKER)) == (
        "mandate_standing_fit_budget_exhausted"
    )
    assert refusal(lambda: ledger.job_status("job-0004")) == "job_unknown"


def test_d7_reference_without_fine_tune_or_outside_window_cannot_dispatch(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    clock = Clock()
    ledger = ledger_at(ledger_dir, clock, env)
    source = seam(env)
    ledger.issue(
        reference(
            put_secret(store, "acct-0001.secret", sentinel()),
            scope=frozenset({pc.Scope.MODEL_READ}),
        )
    )
    ledger.activate("pcref-0001")
    dispatch = request(env)
    assert refusal(lambda: ledger.authorize_dispatch(dispatch, mandates=source, actor=WORKER)) == (
        "reference_scope_lacks_fine_tune"
    )
    unknown = replace(dispatch, reference_id="pcref-0009")
    assert refusal(lambda: ledger.authorize_dispatch(unknown, mandates=source, actor=WORKER)) == (
        "reference_unknown"
    )
    other = ledger_at(ledger_dir.parent / "other", env=env)
    other.issue(reference(store / "acct-0002.secret"))
    other.activate("pcref-0001")
    clock.now = NOW + 86_400
    assert refusal(lambda: other.authorize_dispatch(dispatch, mandates=source, actor=WORKER)) == (
        "reference_outside_window"
    )


def test_d8_withdrawing_the_mandate_holds_new_dispatch(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel(), env=env)
    source = seam(env)
    dispatch = request(env)
    env.mandates.withdraw(MANDATE, actor=OWNER)
    assert refusal(lambda: ledger.authorize_dispatch(dispatch, mandates=source, actor=WORKER)) == (
        "mandate_not_active"
    )


def test_d9_the_mandate_expiry_is_checked_against_this_ledger_clock(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    clock = Clock()
    ledger = active_ledger(ledger_dir, store, sentinel(), clock, env=env)
    source = seam(env)
    dispatch = request(env)
    clock.now = NOW + 4_000  # past the mandate's expiry, inside the reference window
    assert refusal(ledger.authorize_dispatch, dispatch, mandates=source, actor=WORKER) == (
        "mandate_not_active"
    )


# ================================================================= pinned mandate store
def _copy_store(env: MandateEnv, name: str) -> Path:
    copy = env.directory.parent / name
    shutil.copytree(env.directory, copy)
    copy.chmod(0o700)
    return copy


def test_p1_a_copied_store_cannot_release_the_value_after_withdrawal(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    """PX3: authorize, copy the mandate directory, withdraw the original."""
    value = sentinel()
    ledger, source, auth, resolver = authorized(ledger_dir, store, env, value)
    copy = _copy_store(env, env.directory.name + "-copy")
    env.mandates.withdraw(MANDATE, actor=OWNER)
    copied = lm.LearningMandates(copy, ORG, env.engagement)
    assert copied.inspect(MANDATE, actor=OWNER)["withdrawn"] is False  # the copy still says active
    on_copy = pc.CredentialResolver(ledger, expected_uid=os.getuid(), mandates=copied)
    assert refusal(on_copy.resolve, auth.authorization_id, job_ref="job-0001") == (
        "mandate_store_mismatch"
    )
    copy_seam = seams.ProviderCapTestSeam(
        copied, cap_minor_units=50_000, provider=PROVIDER, account_id=ACCOUNT
    )
    on_copy_seam = pc.CredentialResolver(ledger, expected_uid=os.getuid(), mandates=copy_seam)
    assert refusal(on_copy_seam.resolve, auth.authorization_id, job_ref="job-0001") == (
        "mandate_store_mismatch"
    )
    assert refusal(resolver.resolve, auth.authorization_id, job_ref="job-0001") == (
        "mandate_not_active"
    )
    status, body, output = _probe(ledger_dir, env, auth.authorization_id, "job-0001", copy)
    assert (status, body["result"]) == (2, "mandate_store_mismatch") and value not in output
    status, body, _ = _probe(ledger_dir, env, auth.authorization_id, "job-0001")
    assert (status, body["result"]) == (2, "mandate_not_active")
    dispatch = request(env, "job-0002", reservation={})
    assert refusal(ledger.authorize_dispatch, dispatch, mandates=copy_seam, actor=WORKER) == (
        "mandate_store_mismatch"
    )
    del source


def _recreate_at_same_path(env: MandateEnv) -> None:
    moved = env.directory.parent / (env.directory.name + "-moved")
    env.directory.rename(moved)
    shutil.copytree(moved, env.directory)
    env.directory.chmod(0o700)


def _replace_database(env: MandateEnv) -> None:
    database = env.directory / "learning-mandates.sqlite3"
    staged = env.directory / "staged.sqlite3"
    shutil.copy2(database, staged)
    os.replace(staged, database)


def _new_store_id(env: MandateEnv) -> None:
    with sqlite3.connect(env.directory / "learning-mandates.sqlite3") as db:
        db.execute("UPDATE provider_credential_store SET store_id=?", (secrets.token_hex(16),))


def _drop_store_id(env: MandateEnv) -> None:
    with sqlite3.connect(env.directory / "learning-mandates.sqlite3") as db:
        db.execute("DELETE FROM provider_credential_store")


STORE_CHANGES: dict[str, Callable[[MandateEnv], None]] = {
    "recreated-same-path": _recreate_at_same_path,
    "database-replaced": _replace_database,
    "store-id-changed": _new_store_id,
    "store-id-removed": _drop_store_id,
}


@pytest.mark.parametrize("change", sorted(STORE_CHANGES))
def test_p2_a_store_recreated_or_replaced_in_place_is_refused(
    ledger_dir: Path, store: Path, env: MandateEnv, change: str
) -> None:
    ledger, _, auth, _ = authorized(ledger_dir, store, env, sentinel())
    STORE_CHANGES[change](env)
    reopened = lm.LearningMandates(env.directory, ORG, env.engagement)
    resolver = pc.CredentialResolver(ledger, expected_uid=os.getuid(), mandates=reopened)
    assert refusal(resolver.resolve, auth.authorization_id, job_ref="job-0001") == (
        "mandate_store_mismatch"
    )
    status, body, _ = _probe(ledger_dir, env, auth.authorization_id, "job-0001")
    assert (status, body["result"]) == (2, "mandate_store_mismatch")
    dispatch = request(env, "job-0002") if change != "recreated-same-path" else None
    if dispatch is not None:
        assert refusal(ledger.authorize_dispatch, dispatch, mandates=reopened, actor=WORKER) == (
            "mandate_store_mismatch"
        )


def test_p3_another_scope_in_the_same_store_is_refused(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    ledger, _, auth, _ = authorized(ledger_dir, store, env, sentinel())
    other_scope = lm.LearningMandates(env.directory, "other-org", env.engagement, owner=OWNER)
    resolver = pc.CredentialResolver(ledger, expected_uid=os.getuid(), mandates=other_scope)
    assert refusal(resolver.resolve, auth.authorization_id, job_ref="job-0001") == (
        "mandate_store_mismatch"
    )


def test_p4_the_store_is_bound_once_and_required(
    ledger_dir: Path, store: Path, env: MandateEnv
) -> None:
    unbound = active_ledger(ledger_dir, store, sentinel())
    dispatch = request(env)
    source = seam(env)
    assert refusal(unbound.authorize_dispatch, dispatch, mandates=source, actor=WORKER) == (
        "mandate_store_unbound"
    )
    digest_value = unbound.bind_mandate_store(env.mandates)
    assert refusal(unbound.bind_mandate_store, env.mandates) == "mandate_store_already_bound"
    auth = unbound.authorize_dispatch(dispatch, mandates=source, actor=WORKER)
    body = unbound._fold().jobs["job-0001"].body
    assert body["mandate_store"] == digest_value
    # The store identity is inside the authorization id: changing it breaks the record.
    forged = dict(body, job_ref="job-0002", mandate_store="e" * 64)
    forged["authorization_id"] = pc._authorization_id(forged)
    forge(unbound, "dispatch_authorized", forged)
    assert refusal(unbound.status, "pcref-0001") == "credential_ledger_corrupt"
    del auth


# ================================================================= registry, clock, scope
SEAM_SURFACE = ("enable_test_seams", "ProviderCapTestSeam", "_TEST_SEAMS", "_seam")


def registry_writes(source: str) -> list[int]:
    """Lines that write ``_SOURCE_REGISTRY`` other than its empty definition, or read it other
    than through ``.get``."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    offending = []
    for node in ast.walk(tree):
        named = (
            (isinstance(node, ast.Name) and node.id == "_SOURCE_REGISTRY")
            or (isinstance(node, ast.Attribute) and node.attr == "_SOURCE_REGISTRY")
            or (isinstance(node, ast.Constant) and node.value == "_SOURCE_REGISTRY")
        )
        if not named:
            continue
        parent = parents.get(node)
        if (
            isinstance(parent, ast.AnnAssign)
            and parent.target is node
            and isinstance(parent.value, ast.Dict)
            and not parent.value.keys
        ):
            continue
        if isinstance(parent, ast.Attribute) and parent.attr == "get":
            continue
        offending.append(node.lineno)
    return offending


def test_s1_the_source_registry_is_written_only_from_the_tests_tree() -> None:
    root = SRC.parents[2]
    skip = {".git", ".venv", "node_modules", "tests", "__pycache__", ".claude"}
    checked = 0
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in skip for part in relative.parts):
            continue
        text = path.read_text(errors="replace")
        checked += 1
        if path.resolve() == Path(pc.__file__).resolve():
            assert registry_writes(text) == [], relative
            definitions = re.findall(r"^_SOURCE_REGISTRY: .*$", text, flags=re.M)
            assert definitions == ["_SOURCE_REGISTRY: dict[type, _SourceHooks] = {}"]
            assert not any(name in text for name in SEAM_SURFACE)
        else:
            assert "_SOURCE_REGISTRY" not in text and "provider_credential_seams" not in text, (
                relative
            )
    assert checked > 50
    # Positive controls: every write form is caught.
    for write in (
        "pc._SOURCE_REGISTRY[object] = hooks",
        "pc._SOURCE_REGISTRY.update({object: hooks})",
        "pc._SOURCE_REGISTRY.setdefault(object, hooks)",
        "_SOURCE_REGISTRY = {object: hooks}",
        "setattr(pc, '_SOURCE_REGISTRY', {})",
        "registry = pc._SOURCE_REGISTRY",
    ):
        assert registry_writes(write), write
    assert registry_writes("x = pc._SOURCE_REGISTRY.get(kind)") == []
    assert registry_writes("_SOURCE_REGISTRY: dict[type, _SourceHooks] = {}") == []


def test_s2_a_fresh_process_has_no_seams_and_an_empty_registry() -> None:
    script = (
        "import sys, types\n"
        "sys.modules['pytest'] = types.ModuleType('pytest')\n"
        "from opentour_cell.pilot import provider_credential as pc\n"
        "print(len(pc._SOURCE_REGISTRY))\n"
        f"print(any(hasattr(pc, n) for n in {SEAM_SURFACE!r}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=dict(os.environ, PYTHONPATH=CHILD_PATH),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.stdout.split() == ["0", "False"], completed.stderr


def test_s3_production_ledger_has_no_clock_parameter(ledger_dir: Path) -> None:
    assert list(inspect.signature(pc.CredentialLedger).parameters) == ["directory"]
    ledger = pc.CredentialLedger(ledger_dir)
    assert abs(ledger._now() - time.time()) < 5


def test_e1_evidence_tier_gates_and_the_not_proven_document() -> None:
    assert pc.EVIDENCE_TIER is EvidenceTier.FIXTURE
    for gate in (
        "provider_side_refusal_of_rotated_credential",
        "provider_side_revocation_witnessed",
        "customer_created_least_privilege_credential_in_own_provider_account",
        "standing_mandate_records_nonzero_provider_cap",
        "standing_mandate_records_provider_account_and_cost_usage",
        "credential_withdrawal_linked_to_learning_mandate_withdrawal",
        "separate_uid_or_private_pids_between_cycle_and_managed_worker",
    ):
        assert gate in pc.EXTERNAL_GATES
    text = DOC.read_text().lower()
    for phrase in (
        "no provider call",
        "provider-side revocation",
        "provider-side refusal",
        "/proc/<pid>/environ",
        "same uid",
        "max_cost_cents",
        "not tamper-proof",
        "rotation does not fence",
        "resolved before a revocation",
        "copying an old value",
        "caller's statement",
        "providercaptestseam",
        "mandate_store_mismatch",
        "ope-566",
        "reenrolment_requires_signed_owner_record",
    ):
        assert phrase in text, phrase
