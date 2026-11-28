"""OPE-578 provider credential references, resolver, rotation, revocation and mandate binding.

Fixture kind for every test: generated. Every secret is a sentinel generated at test time and
every value-like string is assembled at runtime; no real key, provider or account is used. These
tests prove local software behavior only (EvidenceTier.FIXTURE). Provider-side refusal,
provider-side revocation and the customer's real secret store remain open external gates.

Item 1  least-privilege reference record and value refusal ........ C1-C6
Item 2  value only at the locator; resolver; leak scan ............ R1-R5, L1
Item 3  rotation drill with fresh-subprocess refusal of the old .... T1-T3
Item 4  durable append-only revocation; fenced jobs; outcome ...... V1-V5
Item 5  mandate owner, payer and cap bound before dispatch ........ D1-D5
Scope   evidence tier, gates and the not-proven document .......... E1
"""

from __future__ import annotations

import base64
import gc
import json
import logging
import os
import pickle
import secrets
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import provider_credential as pc
from opentour_cell.pilot.connector_ext import EvidenceTier

SRC = Path(pc.__file__).resolve().parents[2]
TEST_FILE = __file__
DOC = SRC.parent / "docs" / "pilot-provider-credential.md"
NOW = 1_800_000_000


class Clock:
    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)


def sentinel() -> str:
    """A fresh generated secret. It never exists outside this test process and its files."""
    return "otsentinel" + secrets.token_hex(24)


@pytest.fixture
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("store")
    directory.chmod(0o700)
    return directory


@pytest.fixture
def ledger_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("ledger") / "state"
    return directory


def put_secret(store: Path, name: str, value: str, mode: int = 0o600) -> Path:
    path = store / name
    path.write_text(value + "\n")
    path.chmod(mode)
    return path


def reference(locator: Path | str, **changes: Any) -> pc.CredentialReference:
    fields: dict[str, Any] = {
        "reference_id": "pcref-0001",
        "provider": "sentinel-provider",
        "account_id": "customer-account-0001",
        "owner_ref": "owner-0001",
        "payer_ref": "payer-0001",
        "scope": frozenset({pc.Scope.FINE_TUNE, pc.Scope.MODEL_READ}),
        "locator_kind": pc.LocatorKind.FILE,
        "locator": str(locator),
        "budget_id": "budget-0001",
        "issued_at": NOW - 60,
        "not_after": NOW + 86_400,
    }
    return pc.CredentialReference(**(fields | changes))


def mandate(**changes: Any) -> pc.MandateView:
    base = pc.MandateView(
        mandate_id="mandate-0001",
        revision="a" * 64,
        state="active",
        owner_id="owner-0001",
        payer="payer-0001",
        provider="sentinel-provider",
        account_id="customer-account-0001",
        cap_currency="usd",
        cap_minor_units=5_000,
        consumed_minor_units=1_000,
        expires_at=NOW + 3_600,
    )
    return replace(base, **changes)


def request(**changes: Any) -> pc.DispatchRequest:
    base = pc.DispatchRequest(
        job_ref="fit-" + "b" * 40,
        reference_id="pcref-0001",
        mandate_id="mandate-0001",
        mandate_revision="a" * 64,
        owner_id="owner-0001",
        payer="payer-0001",
        currency="usd",
        max_minor_units=2_000,
    )
    return replace(base, **changes)


def active_ledger(
    ledger_dir: Path, store: Path, value: str, clock: Clock | None = None
) -> pc.CredentialLedger:
    ledger = pc.CredentialLedger(ledger_dir, clock=clock or Clock())
    ledger.issue(reference(put_secret(store, "acct-0001.secret", value)))
    ledger.activate("pcref-0001")
    return ledger


def lines(ledger: pc.CredentialLedger) -> list[bytes]:
    return ledger.path.read_bytes().splitlines() if ledger.path.exists() else []


def code(excinfo: pytest.ExceptionInfo[pc.CredentialError]) -> str:
    return excinfo.value.code


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
    ]


def _high_entropy() -> list[str]:
    return [
        secrets.token_urlsafe(32)[:32],
        secrets.token_hex(20),
        base64.b64encode(secrets.token_bytes(30)).decode(),
        "acct-" + secrets.token_hex(12),
        "q" * 4 + "".join(secrets.choice("bcdfghjklmnpqrstvwxz") for _ in range(20)),
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
    with pytest.raises(pc.CredentialError) as excinfo:
        pc.CredentialReference.from_mapping(mapping)
    assert code(excinfo) == "scope_outside_least_privilege"
    with pytest.raises(pc.CredentialError) as excinfo:
        reference(store / "x.secret", scope=frozenset())
    assert code(excinfo) == "scope_outside_least_privilege"
    with pytest.raises(pc.CredentialError) as excinfo:
        pc.CredentialReference.from_mapping(
            reference(store / "x.secret").to_mapping() | {"scope": ["fine_tune", "fine_tune"]}
        )
    assert code(excinfo) == "scope_outside_least_privilege"


@pytest.mark.parametrize("field", ID_FIELDS)
def test_c3_known_key_prefixes_are_refused_in_every_field(store: Path, field: str) -> None:
    for value in _prefixed():
        with pytest.raises(pc.CredentialError) as excinfo:
            reference(store / "x.secret", **{field: value})
        assert code(excinfo) == "suspected_credential_prefix", field


@pytest.mark.parametrize("field", ID_FIELDS)
def test_c4_high_entropy_values_are_refused_in_every_field(store: Path, field: str) -> None:
    for value in _high_entropy():
        with pytest.raises(pc.CredentialError) as excinfo:
            reference(store / "x.secret", **{field: value})
        assert code(excinfo) == "suspected_credential_entropy", field


@pytest.mark.parametrize("field", ID_FIELDS)
def test_c5_values_over_the_length_bound_are_refused(store: Path, field: str) -> None:
    with pytest.raises(pc.CredentialError) as excinfo:
        reference(store / "x.secret", **{field: "ab-" * 22})
    assert code(excinfo) == "reference_field_too_long"
    assert reference(store / "x.secret", **{field: "ab-" * 21 + "a"})  # 64 is the bound


def test_c6_locator_and_unknown_fields_cannot_carry_a_value(store: Path) -> None:
    for value in _prefixed():
        with pytest.raises(pc.CredentialError) as excinfo:
            reference(str(store / value.lower().replace(" ", "-")))
        assert code(excinfo) == "suspected_credential_prefix"
    for value in _high_entropy():
        with pytest.raises(pc.CredentialError) as excinfo:
            reference(str(store) + "/" + value)
        assert code(excinfo) == "suspected_credential_entropy"
    with pytest.raises(pc.CredentialError) as excinfo:
        reference("/" + "/".join(["segment"] * 26))
    assert code(excinfo) == "reference_field_too_long"
    for bad in ("relative/path.secret", "/etc/../x.secret", "/onlyone", str(store) + "/UPPER"):
        with pytest.raises(pc.CredentialError):
            reference(bad)
    mapping = reference(store / "x.secret").to_mapping()
    for extra in ("value", "api_key", "secret", "token"):
        with pytest.raises(pc.CredentialError) as excinfo:
            pc.CredentialReference.from_mapping(mapping | {extra: "generated-placeholder"})
        assert code(excinfo) == "unknown_or_missing_reference_field"
    with pytest.raises(pc.CredentialError) as excinfo:
        pc.CredentialReference.from_mapping({k: v for k, v in mapping.items() if k != "budget_id"})
    assert code(excinfo) == "unknown_or_missing_reference_field"
    with pytest.raises(pc.CredentialError):
        reference(store / "x.secret", issued_at=NOW, not_after=NOW)
    benign = ["customer-account-0001", "asia-pacific-0001", "fine-tuning-budget-2026-q4"]
    assert all(pc.suspected_value(v) is None for v in benign)


def test_c7_ledger_written_by_anyone_else_cannot_smuggle_a_value(
    ledger_dir: Path, store: Path
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel())
    first = json.loads(lines(ledger)[0])
    first["body"]["reference"]["account_id"] = secrets.token_hex(20)
    ledger.path.write_bytes(pc._canonical(first) + b"\n")
    with pytest.raises(pc.CredentialError) as excinfo:
        ledger.status("pcref-0001")
    assert code(excinfo) == "credential_ledger_corrupt"


# ================================================================= item 2: resolver
def test_r1_resolver_reads_the_value_only_at_use(ledger_dir: Path, store: Path) -> None:
    value = sentinel()
    ledger = active_ledger(ledger_dir, store, value)
    assert value.encode() not in ledger.path.read_bytes()
    secret = pc.CredentialResolver(ledger, expected_uid=os.getuid()).resolve("pcref-0001")
    assert secret.reveal() == value
    assert value not in repr(secret) and value not in str(secret) and value not in f"{secret}"
    with pytest.raises(TypeError) as excinfo:
        pickle.dumps(secret)
    assert value not in str(excinfo.value)
    with pytest.raises(TypeError):
        json.dumps(secret)
    with pytest.raises(AttributeError):
        secret.__dict__  # noqa: B018 - slots only, nothing else to leak
    # A changed value at the locator is read fresh at the next use.
    replacement = sentinel()
    put_secret(store, "acct-0001.secret", replacement)
    assert pc.CredentialResolver(ledger, expected_uid=os.getuid()).resolve("pcref-0001").reveal()
    assert (
        pc.CredentialResolver(ledger, expected_uid=os.getuid()).resolve("pcref-0001").reveal()
        == replacement
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
        # The locator's parent becomes a link to a safe directory holding the same file.
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
def test_r2_unsafe_secret_files_are_refused(ledger_dir: Path, store: Path, case: str) -> None:
    value = sentinel()
    ledger = active_ledger(ledger_dir, store, value)
    mutate, expected = _unsafe_cases(store, value)[case]
    mutate()
    with pytest.raises(pc.CredentialError) as excinfo:
        pc.CredentialResolver(ledger, expected_uid=os.getuid()).resolve("pcref-0001")
    assert code(excinfo) == expected
    assert value not in repr(excinfo.value) and value not in str(excinfo.value)


def test_r3_owner_other_than_the_dispatch_identity_is_refused(
    ledger_dir: Path, store: Path
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel())
    with pytest.raises(pc.CredentialError) as excinfo:
        pc.CredentialResolver(ledger, expected_uid=os.getuid() + 1).resolve("pcref-0001")
    # The parent directory is owned by the test uid, so the parent check refuses first unless
    # the test runs as root, where a root-owned parent is permitted and the file owner is checked.
    expected = "secret_owner_mismatch" if os.getuid() == 0 else "secret_parent_unsafe"
    assert code(excinfo) == expected
    if os.getuid() == 0:
        os.chown(store / "acct-0001.secret", 65534, 65534)
        with pytest.raises(pc.CredentialError) as excinfo:
            pc.CredentialResolver(ledger, expected_uid=0).resolve("pcref-0001")
        assert code(excinfo) == "secret_owner_mismatch"


def test_r4_only_an_active_reference_inside_its_window_resolves(
    ledger_dir: Path, store: Path
) -> None:
    clock = Clock()
    ledger = pc.CredentialLedger(ledger_dir, clock=clock)
    resolver = pc.CredentialResolver(ledger, expected_uid=os.getuid())
    with pytest.raises(pc.CredentialError) as excinfo:
        resolver.resolve("pcref-0001")
    assert code(excinfo) == "reference_unknown"
    ledger.issue(reference(put_secret(store, "acct-0001.secret", sentinel())))
    with pytest.raises(pc.CredentialError) as excinfo:
        resolver.resolve("pcref-0001")
    assert code(excinfo) == "reference_not_active"
    ledger.activate("pcref-0001")
    assert resolver.resolve("pcref-0001")
    clock.now = NOW + 86_400
    with pytest.raises(pc.CredentialError) as excinfo:
        resolver.resolve("pcref-0001")
    assert code(excinfo) == "reference_outside_window"
    with pytest.raises(pc.CredentialError) as excinfo:
        resolver.resolve(secrets.token_hex(20))
    assert code(excinfo) == "suspected_credential_entropy"


def test_r5_store_key_locators_need_a_secret_store_resolver(ledger_dir: Path) -> None:
    ledger = pc.CredentialLedger(ledger_dir, clock=Clock())
    ledger.issue(
        reference("customer-store/fine-tuning/acct-0001", locator_kind=pc.LocatorKind.STORE_KEY)
    )
    ledger.activate("pcref-0001")
    with pytest.raises(pc.CredentialError) as excinfo:
        pc.CredentialResolver(ledger, expected_uid=os.getuid()).resolve("pcref-0001")
    assert code(excinfo) == "locator_kind_unsupported"


def test_r6_ledger_storage_is_private_and_fails_closed(
    ledger_dir: Path, store: Path, tmp_path: Path
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel())
    assert (ledger_dir.stat().st_mode & 0o777, ledger.path.stat().st_mode & 0o777) == (
        0o700,
        0o600,
    )
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    open_dir.chmod(0o755)
    with pytest.raises(pc.CredentialError) as excinfo:
        pc.CredentialLedger(open_dir)
    assert code(excinfo) == "ledger_directory_unsafe"
    ledger.path.chmod(0o644)
    with pytest.raises(pc.CredentialError) as excinfo:
        pc.CredentialResolver(ledger, expected_uid=os.getuid()).resolve("pcref-0001")
    assert code(excinfo) == "credential_ledger_unsafe"


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


def _leaks(needles: list[str], texts: list[str], blobs: list[bytes]) -> list[str]:
    found = []
    for needle in needles:
        if any(needle in text for text in texts):
            found.append("text")
        if any(needle.encode() in blob for blob in blobs):
            found.append("file")
    return found


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

    ``input_refusals`` are refusals of a value the caller itself passed in as a field. Their
    message, args, repr, attributes and chained exceptions are scanned; their traceback frames
    are not, because those frames necessarily hold the caller's own refused argument.
    """
    formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
    texts: list[str] = list(streams)
    for record in records:
        texts += [record.getMessage(), repr(record.args), str(record.msg)]
        texts.append(formatter.format(record))
        if record.exc_info and record.exc_info[1] is not None:
            texts.extend(_exception_texts(record.exc_info[1]))
    for exc in exceptions:
        texts.extend(_exception_texts(exc))
    for exc in input_refusals:
        texts.extend(_exception_texts(exc, frames=False))
    for obj in objects:
        texts.append(repr(obj))
        texts.append(repr(getattr(obj, "__dict__", {})))
    blobs = [
        path.read_bytes()
        for root in roots
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]
    return _leaks(needles, texts, blobs)


def test_l1_no_value_reaches_logs_exceptions_reprs_files_or_the_journal(
    ledger_dir: Path,
    store: Path,
    tmp_path: Path,
    captured: _Capture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first, second, bad = sentinel(), sentinel(), sentinel()
    clock = Clock()
    ledger = pc.CredentialLedger(ledger_dir, clock=clock)
    resolver = pc.CredentialResolver(ledger, expected_uid=os.getuid())
    old = reference(put_secret(store, "acct-0001.secret", first))
    ledger.issue(old)
    rotation0 = ledger.activate("pcref-0001")
    exceptions: list[BaseException] = []
    refused_inputs: list[BaseException] = []
    objects: list[object] = [ledger, resolver, old, rotation0]

    def expect_refusal(action: Callable[[], object], *, input_refusal: bool = False) -> None:
        try:
            action()
        except pc.CredentialError as exc:
            assert exc.args == (exc.code,)
            (refused_inputs if input_refusal else exceptions).append(exc)
        else:  # pragma: no cover - a refusal is required
            raise AssertionError("refusal expected")

    secret = resolver.resolve("pcref-0001")
    assert secret.reveal() == first
    objects.append(secret)
    authorization = ledger.authorize_dispatch(request(), lambda: mandate())
    objects.append(authorization)
    new = reference(
        put_secret(store, "acct-0001-rotation-0002.secret", second), reference_id="pcref-0002"
    )
    rotation = ledger.rotate(new)
    objects += [new, rotation]
    assert resolver.resolve("pcref-0002").reveal() == second
    expect_refusal(lambda: resolver.resolve("pcref-0001"))
    # Refusals that happen after the value was read must not carry it.
    put_secret(store, "acct-0001-rotation-0002.secret", second + " " + bad)
    expect_refusal(lambda: resolver.resolve("pcref-0002"))
    put_secret(store, "acct-0001-rotation-0002.secret", second)
    (store / "acct-0001-rotation-0002.secret").chmod(0o644)
    expect_refusal(lambda: resolver.resolve("pcref-0002"))
    (store / "acct-0001-rotation-0002.secret").chmod(0o600)
    expect_refusal(lambda: reference(store / "x.secret", account_id=first), input_refusal=True)
    expect_refusal(lambda: reference(store / "x.secret", owner_ref=second), input_refusal=True)
    expect_refusal(lambda: resolver.resolve(first), input_refusal=True)
    expect_refusal(
        lambda: ledger.authorize_dispatch(request(job_ref=bad), lambda: mandate()),
        input_refusal=True,
    )
    ledger.authorize_dispatch(
        request(job_ref="fit-" + "c" * 40, reference_id="pcref-0002"), lambda: mandate()
    )
    revocation = ledger.withdraw("sentinel-provider", "customer-account-0001")
    objects += [revocation, ledger.job_status("fit-" + "c" * 40)]
    expect_refusal(lambda: resolver.resolve("pcref-0002"))
    status = ledger.record_outcome(
        "fit-" + "c" * 40, pc.JobOutcome.UNKNOWN, pc.ObservedBy.NOT_OBSERVABLE, "evidence-0001"
    )
    objects.append(status)
    probe = subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.provider_credential",
            "probe",
            "--ledger",
            str(ledger_dir),
            "--reference",
            "pcref-0002",
            "--expected-uid",
            str(os.getuid()),
        ],
        env=dict(os.environ, PYTHONPATH=str(SRC)),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    probe_with_value = subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.provider_credential",
            "probe",
            "--ledger",
            str(ledger_dir),
            "--reference",
            first,
            "--expected-uid",
            str(os.getuid()),
        ],
        env=dict(os.environ, PYTHONPATH=str(SRC)),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert json.loads(probe_with_value.stdout)["reference"] is None
    out = capsys.readouterr()
    gc.collect()
    leaks = scan_for_leaks(
        [first, second, bad],
        captured.records,
        exceptions,
        objects,
        [ledger_dir, tmp_path],
        [
            out.out,
            out.err,
            probe.stdout,
            probe.stderr,
            probe_with_value.stdout,
            probe_with_value.stderr,
        ],
        tuple(refused_inputs),
    )
    assert leaks == []
    # The scan is live: it saw module log records, refusals and the journal.
    assert any(r.name == pc.__name__ for r in captured.records)
    assert len(exceptions) >= 4 and len(refused_inputs) == 4 and len(lines(ledger)) >= 5


def test_l2_the_leak_scanner_detects_each_channel(tmp_path: Path, captured: _Capture) -> None:
    needle = sentinel()
    logging.getLogger(pc.__name__).info("leaked %s", needle)
    assert scan_for_leaks([needle], captured.records, [], [], [], []) == ["text"]
    captured.records.clear()
    try:
        raise RuntimeError("wrapped") from ValueError(needle)
    except RuntimeError as exc:
        assert scan_for_leaks([needle], [], [exc], [], [], []) == ["text"]

    class Holder:
        def __init__(self) -> None:
            self.kept = needle

    assert scan_for_leaks([needle], [], [], [Holder()], [], []) == ["text"]
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "journal.jsonl").write_text(json.dumps({"v": needle}))
    assert scan_for_leaks([needle], [], [], [], [tmp_path], []) == ["file"]
    assert scan_for_leaks([needle], [], [], [], [], ["stdout " + needle]) == ["text"]
    assert scan_for_leaks([needle], [], [], [], [], []) == []


# ================================================================= item 3: rotation drill
def _probe(ledger_dir: Path, reference_id: str) -> tuple[int, dict[str, Any], str]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.provider_credential",
            "probe",
            "--ledger",
            str(ledger_dir),
            "--reference",
            reference_id,
            "--expected-uid",
            str(os.getuid()),
        ],
        env=dict(os.environ, PYTHONPATH=str(SRC)),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return completed.returncode, json.loads(completed.stdout), completed.stdout + completed.stderr


def _real_time_reference(store: Path, name: str, value: str, **changes: Any) -> Any:
    import time

    now = int(time.time())
    return reference(
        put_secret(store, name, value), issued_at=now - 60, not_after=now + 3_600, **changes
    )


def test_t1_rotation_drill_old_reference_refused_from_a_fresh_process(
    ledger_dir: Path, store: Path
) -> None:
    old_value, new_value = sentinel(), sentinel()
    ledger = pc.CredentialLedger(ledger_dir)
    ledger.issue(_real_time_reference(store, "acct-0001.secret", old_value))
    ledger.activate("pcref-0001")
    assert _probe(ledger_dir, "pcref-0001")[:2] == (
        0,
        {
            "reference": "pcref-0001",
            "result": "resolved_locally",
            "provider_side_refusal": "not_attempted_out_of_scope",
        },
    )
    receipt = ledger.rotate(
        _real_time_reference(
            store, "acct-0001-rotation-0002.secret", new_value, reference_id="pcref-0002"
        )
    )
    assert receipt == pc.RotationReceipt(
        "pcref-0001", "pcref-0002", "previous_reference_retired", "not_attempted_out_of_scope"
    )
    # The old value is still present at its locator with safe permissions: refusal is the
    # ledger's, not an accident of file state.
    assert (store / "acct-0001.secret").read_text().strip() == old_value
    status, body, output = _probe(ledger_dir, "pcref-0001")
    assert (status, body["result"]) == (2, "reference_retired")
    assert body["provider_side_refusal"] == "not_attempted_out_of_scope"
    assert old_value not in output and new_value not in output
    status, body, output = _probe(ledger_dir, "pcref-0002")
    assert (status, body["result"]) == (0, "resolved_locally")
    assert old_value not in output and new_value not in output


def test_t2_old_locator_cannot_be_reissued_or_reactivated(ledger_dir: Path, store: Path) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel())
    old_locator = ledger.reference("pcref-0001").locator
    ledger.rotate(
        reference(put_secret(store, "rotation-0002.secret", sentinel()), reference_id="pcref-0002")
    )
    before = lines(ledger)
    for attempt in (
        lambda: ledger.issue(reference(old_locator, reference_id="pcref-0003")),
        lambda: ledger.rotate(reference(old_locator, reference_id="pcref-0003")),
        lambda: ledger.activate("pcref-0001"),
        lambda: ledger.issue(reference(store / "fresh.secret", reference_id="pcref-0002")),
    ):
        with pytest.raises(pc.CredentialError) as excinfo:
            attempt()
        assert code(excinfo) in {
            "secret_locator_reused",
            "reference_not_activatable",
            "reference_already_issued",
        }
    assert lines(ledger) == before
    fresh = pc.CredentialLedger(ledger_dir, clock=Clock())
    assert (fresh.status("pcref-0001"), fresh.status("pcref-0002")) == ("retired", "active")


def test_t3_rotation_needs_an_active_reference(ledger_dir: Path, store: Path) -> None:
    ledger = pc.CredentialLedger(ledger_dir, clock=Clock())
    with pytest.raises(pc.CredentialError) as excinfo:
        ledger.rotate(reference(store / "x.secret"))
    assert code(excinfo) == "no_active_reference_to_rotate"
    assert lines(ledger) == []


# ================================================================= item 4: revocation
def test_v1_revocation_fences_in_flight_jobs_and_never_assumes_an_outcome(
    ledger_dir: Path, store: Path
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel())
    ledger.authorize_dispatch(request(), lambda: mandate())
    done = "fit-" + "d" * 40
    ledger.authorize_dispatch(request(job_ref=done), lambda: mandate())
    ledger.record_outcome(done, pc.JobOutcome.COMPLETED, pc.ObservedBy.PROVIDER_REPORTED, "ev-1")
    receipt = ledger.revoke(["pcref-0001"], pc.RevocationCause.WITHDRAWAL)
    assert receipt == pc.RevocationReceipt(
        ("pcref-0001",),
        pc.RevocationCause.WITHDRAWAL,
        ("fit-" + "b" * 40,),
        "not_attempted_out_of_scope",
    )
    restarted = pc.CredentialLedger(ledger_dir, clock=Clock())
    job = restarted.job_status("fit-" + "b" * 40)
    assert job.fenced is True and job.outcome is None and job.observed_by is None
    assert restarted.job_status(done).fenced is False
    assert restarted.status("pcref-0001") == "revoked"
    with pytest.raises(pc.CredentialError) as excinfo:
        pc.CredentialResolver(restarted, expected_uid=os.getuid()).resolve("pcref-0001")
    assert code(excinfo) == "reference_revoked"
    with pytest.raises(pc.CredentialError) as excinfo:
        restarted.authorize_dispatch(request(job_ref="fit-" + "e" * 40), lambda: mandate())
    assert code(excinfo) == "reference_not_active"
    with pytest.raises(pc.CredentialError) as excinfo:
        restarted.authorize_dispatch(request(), lambda: mandate())
    assert code(excinfo) == "reference_not_active"
    with pytest.raises(pc.CredentialError) as excinfo:
        restarted.revoke(["pcref-0001"], pc.RevocationCause.WITHDRAWAL)
    assert code(excinfo) == "reference_already_revoked"


def test_v2_outcomes_are_recorded_as_observed(ledger_dir: Path, store: Path) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel())
    job = "fit-" + "b" * 40
    ledger.authorize_dispatch(request(), lambda: mandate())
    ledger.revoke(["pcref-0001"], pc.RevocationCause.COMPROMISE)
    with pytest.raises(pc.CredentialError) as excinfo:
        ledger.record_outcome(job, pc.JobOutcome.CANCELED, pc.ObservedBy.NOT_OBSERVABLE, "ev-1")
    assert code(excinfo) == "observed_outcome_requires_observation"
    unknown = ledger.record_outcome(
        job, pc.JobOutcome.UNKNOWN, pc.ObservedBy.NOT_OBSERVABLE, "ev-1"
    )
    assert (unknown.fenced, unknown.outcome) == (True, pc.JobOutcome.UNKNOWN)
    running = ledger.record_outcome(
        job, pc.JobOutcome.RUNNING_AFTER_FENCE, pc.ObservedBy.PROVIDER_REPORTED, "ev-2"
    )
    assert running.outcome is pc.JobOutcome.RUNNING_AFTER_FENCE
    final = ledger.record_outcome(
        job, pc.JobOutcome.CANCELED, pc.ObservedBy.PROVIDER_REPORTED, "ev-" + "f" * 40
    )
    assert final.outcome is pc.JobOutcome.CANCELED
    count = len(lines(ledger))
    assert (
        ledger.record_outcome(
            job, pc.JobOutcome.CANCELED, pc.ObservedBy.PROVIDER_REPORTED, "ev-" + "f" * 40
        )
        == final
    )
    assert len(lines(ledger)) == count
    with pytest.raises(pc.CredentialError) as excinfo:
        ledger.record_outcome(job, pc.JobOutcome.COMPLETED, pc.ObservedBy.PROVIDER_REPORTED, "e")
    assert code(excinfo) == "job_outcome_already_final"
    with pytest.raises(pc.CredentialError) as excinfo:
        ledger.record_outcome("fit-0", pc.JobOutcome.UNKNOWN, pc.ObservedBy.NOT_OBSERVABLE, "e")
    assert code(excinfo) == "job_unknown"


def test_v3_withdrawal_revokes_every_reference_of_the_account(
    ledger_dir: Path, store: Path
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel())
    ledger.authorize_dispatch(request(), lambda: mandate())
    ledger.rotate(
        reference(put_secret(store, "rotation-0002.secret", sentinel()), reference_id="pcref-0002")
    )
    ledger.authorize_dispatch(
        request(job_ref="fit-" + "c" * 40, reference_id="pcref-0002"), lambda: mandate()
    )
    receipt = ledger.withdraw("sentinel-provider", "customer-account-0001")
    assert receipt.reference_ids == ("pcref-0001", "pcref-0002")
    assert receipt.fenced_job_refs == ("fit-" + "b" * 40, "fit-" + "c" * 40)
    assert receipt.cause is pc.RevocationCause.WITHDRAWAL
    assert receipt.provider_side_revocation == "not_attempted_out_of_scope"
    assert {ledger.status("pcref-0001"), ledger.status("pcref-0002")} == {"revoked"}
    with pytest.raises(pc.CredentialError) as excinfo:
        ledger.withdraw("sentinel-provider", "customer-account-0001")
    assert code(excinfo) == "no_reference_to_withdraw"
    with pytest.raises(pc.CredentialError) as excinfo:
        ledger.revoke(["pcref-9999"], pc.RevocationCause.OWNER_REQUEST)
    assert code(excinfo) == "reference_unknown"


def test_v4_the_journal_is_append_only_and_tamper_evident(ledger_dir: Path, store: Path) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel())
    snapshot = ledger.path.read_bytes()
    ledger.authorize_dispatch(request(), lambda: mandate())
    ledger.revoke(["pcref-0001"], pc.RevocationCause.WITHDRAWAL)
    grown = ledger.path.read_bytes()
    assert grown.startswith(snapshot) and len(grown) > len(snapshot)
    events = [json.loads(line) for line in grown.splitlines()]
    assert [e["kind"] for e in events] == ["issued", "activated", "dispatch_authorized", "revoked"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]

    def corrupt(data: bytes) -> str:
        ledger.path.write_bytes(data)
        with pytest.raises(pc.CredentialError) as excinfo:
            ledger.status("pcref-0001")
        return code(excinfo)

    original = grown.splitlines(keepends=True)
    # Un-revoke by deleting the revocation line's interior neighbour, by editing, by reordering.
    assert corrupt(b"".join(original[:2] + original[3:])) == "credential_ledger_corrupt"
    edited = json.loads(original[2])
    edited["body"]["max_minor_units"] = 1
    assert corrupt(b"".join(original[:2]) + pc._canonical(edited) + b"\n" + original[3]) == (
        "credential_ledger_corrupt"
    )
    assert corrupt(b"".join([original[1], original[0], *original[2:]])) == (
        "credential_ledger_corrupt"
    )
    assert corrupt(grown[:-5]) == "credential_ledger_corrupt"
    assert corrupt(grown + b"not json\n") == "credential_ledger_corrupt"
    ledger.path.write_bytes(grown)
    assert ledger.status("pcref-0001") == "revoked"
    # Stated limitation: with no external anchor, dropping the final event is not detected.
    ledger.path.write_bytes(b"".join(original[:3]))
    assert ledger.status("pcref-0001") == "active"


# ================================================================= item 5: mandate binding
def test_d1_matching_standing_mandate_authorizes_and_is_journaled(
    ledger_dir: Path, store: Path
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel())
    reads: list[int] = []

    def view() -> pc.MandateView:
        reads.append(1)
        return mandate()

    authorization = ledger.authorize_dispatch(request(), view)
    assert authorization == pc.DispatchAuthorization(
        "fit-" + "b" * 40, "pcref-0001", "mandate-0001", "a" * 64, "usd", 2_000
    )
    assert reads == [1]
    count = len(lines(ledger))
    assert ledger.authorize_dispatch(request(), view) == authorization
    assert len(lines(ledger)) == count and reads == [1, 1]
    with pytest.raises(pc.CredentialError) as excinfo:
        ledger.authorize_dispatch(request(max_minor_units=1_000), view)
    assert code(excinfo) == "job_already_authorized"
    job = ledger.job_status("fit-" + "b" * 40)
    assert (job.fenced, job.outcome) == (False, None)


MANDATE_REFUSALS: list[tuple[str, Callable[[], pc.MandateView | None], Any, str]] = [
    ("missing-view", lambda: None, {}, "standing_mandate_required"),
    ("paused", lambda: mandate(state="paused"), {}, "mandate_not_active"),
    ("withdrawn", lambda: mandate(state="withdrawn"), {}, "mandate_not_active"),
    ("expired", lambda: mandate(expires_at=NOW), {}, "mandate_not_active"),
    ("no-owner", lambda: mandate(owner_id=None), {}, "mandate_owner_required"),
    ("empty-owner", lambda: mandate(owner_id=""), {}, "mandate_owner_required"),
    ("no-payer", lambda: mandate(payer=None), {}, "mandate_payer_required"),
    ("no-cap", lambda: mandate(cap_minor_units=None), {}, "mandate_cap_required"),
    ("zero-cap", lambda: mandate(cap_minor_units=0), {}, "mandate_cap_required"),
    ("no-currency", lambda: mandate(cap_currency=None), {}, "mandate_cap_required"),
    ("stale-revision", lambda: mandate(revision="c" * 64), {}, "stale_mandate_reference"),
    ("other-mandate", lambda: mandate(mandate_id="mandate-0002"), {}, "stale_mandate_reference"),
    (
        "view-owner",
        lambda: mandate(owner_id="owner-0002"),
        {"owner_id": "owner-0002"},
        "mandate_owner_mismatch",
    ),
    ("request-owner", mandate, {"owner_id": "owner-0002"}, "mandate_owner_mismatch"),
    (
        "view-payer",
        lambda: mandate(payer="payer-0002"),
        {"payer": "payer-0002"},
        "mandate_payer_mismatch",
    ),
    ("request-payer", mandate, {"payer": "payer-0002"}, "mandate_payer_mismatch"),
    (
        "account",
        lambda: mandate(account_id="customer-account-0002"),
        {},
        "mandate_account_mismatch",
    ),
    ("provider", lambda: mandate(provider="other-provider"), {}, "mandate_account_mismatch"),
    ("currency", mandate, {"currency": "eur"}, "mandate_currency_mismatch"),
    ("over-remaining", mandate, {"max_minor_units": 4_001}, "mandate_cap_exceeded"),
    ("consumed", lambda: mandate(consumed_minor_units=5_000), {}, "mandate_cap_exceeded"),
    ("zero-bound", mandate, {"max_minor_units": 0}, "mandate_cap_exceeded"),
]


@pytest.mark.parametrize(
    ("view", "changes", "expected"),
    [case[1:] for case in MANDATE_REFUSALS],
    ids=[case[0] for case in MANDATE_REFUSALS],
)
def test_d2_dispatch_is_refused_without_a_matching_mandate(
    ledger_dir: Path,
    store: Path,
    view: Callable[[], pc.MandateView | None],
    changes: dict[str, Any],
    expected: str,
) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel())
    before = lines(ledger)
    with pytest.raises(pc.CredentialError) as excinfo:
        ledger.authorize_dispatch(request(**changes), view)
    assert code(excinfo) == expected
    assert lines(ledger) == before
    with pytest.raises(pc.CredentialError):
        ledger.job_status("fit-" + "b" * 40)


def test_d3_the_cap_boundary_is_inclusive(ledger_dir: Path, store: Path) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel())
    assert ledger.authorize_dispatch(request(max_minor_units=4_000), lambda: mandate())


def test_d4_reference_without_fine_tune_or_outside_window_cannot_dispatch(
    ledger_dir: Path, store: Path
) -> None:
    clock = Clock()
    ledger = pc.CredentialLedger(ledger_dir, clock=clock)
    ledger.issue(
        reference(
            put_secret(store, "acct-0001.secret", sentinel()),
            scope=frozenset({pc.Scope.MODEL_READ}),
        )
    )
    ledger.activate("pcref-0001")
    with pytest.raises(pc.CredentialError) as excinfo:
        ledger.authorize_dispatch(request(), lambda: mandate())
    assert code(excinfo) == "reference_scope_lacks_fine_tune"
    with pytest.raises(pc.CredentialError) as excinfo:
        ledger.authorize_dispatch(request(reference_id="pcref-0009"), lambda: mandate())
    assert code(excinfo) == "reference_unknown"
    other = pc.CredentialLedger(ledger_dir.parent / "other", clock=clock)
    other.issue(reference(store / "acct-0002.secret"))
    other.activate("pcref-0001")
    clock.now = NOW + 86_400
    with pytest.raises(pc.CredentialError) as excinfo:
        other.authorize_dispatch(request(), lambda: mandate(expires_at=NOW + 10**6))
    assert code(excinfo) == "reference_outside_window"


def test_d5_mandate_view_is_read_fresh_for_each_dispatch(ledger_dir: Path, store: Path) -> None:
    ledger = active_ledger(ledger_dir, store, sentinel())
    current = {"view": mandate()}
    ledger.authorize_dispatch(request(), lambda: current["view"])
    current["view"] = mandate(revision="e" * 64)
    with pytest.raises(pc.CredentialError) as excinfo:
        ledger.authorize_dispatch(request(job_ref="fit-" + "c" * 40), lambda: current["view"])
    assert code(excinfo) == "stale_mandate_reference"
    assert ledger.authorize_dispatch(
        request(job_ref="fit-" + "c" * 40, mandate_revision="e" * 64), lambda: current["view"]
    )
    with pytest.raises(pc.CredentialError) as excinfo:
        ledger.authorize_dispatch(request(job_ref=secrets.token_urlsafe(30)), lambda: mandate())
    assert code(excinfo) in {"suspected_credential_entropy", "suspected_credential_prefix"}


# ================================================================= scope of the evidence
def test_e1_evidence_tier_gates_and_the_not_proven_document() -> None:
    assert pc.EVIDENCE_TIER is EvidenceTier.FIXTURE
    for gate in (
        "provider_side_refusal_of_rotated_credential",
        "provider_side_revocation_witnessed",
        "customer_created_least_privilege_credential_in_own_provider_account",
        "standing_mandate_records_nonzero_provider_cap",
        "separate_uid_or_private_pids_between_cycle_and_managed_worker",
    ):
        assert gate in pc.EXTERNAL_GATES
    text = DOC.read_text().lower()
    for phrase in (
        "no provider call",
        "provider-side revocation",
        "/proc/<pid>/environ",
        "provider-side refusal",
        "not tamper-proof",
        "same uid",
        "max_cost_cents",
    ):
        assert phrase in text, phrase
