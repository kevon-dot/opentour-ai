"""Customer-owned fine-tuning provider credential: references and lifecycle records (OPE-578).

This module holds only credential **references** and lifecycle records. It never stores, logs or
returns a credential value except through ``CredentialResolver.resolve``, which reads the value
from the customer's secret-store locator at the moment of use, for one journaled dispatch
authorization this module issued, and hands it to the caller wrapped in a redacting
``SecretValue``.

* ``CredentialReference`` names the provider, the customer's provider account, the account owner
  and payer references, a least-privilege scope (fine-tune and model-read only), the secret-store
  locator (a path or key name, never the value), the provider-side budget id where one exists and
  an ``issued_at``/``not_after`` window. A field that looks like a credential value is refused:
  a known key prefix, a high-entropy run, or anything over the field length bound.
* ``CredentialLedger`` is a durable, append-only, hash-chained JSONL journal of issue, activation
  (rotation), revocation, withdrawal, re-enrolment, dispatch authorization and job outcome events.
  Any malformed, non-canonical or chain-broken line fails closed.
* ``authorize_dispatch`` reads the standing mandate itself from a verified ``LearningMandates``
  ledger and reserves through ``LearningMandates.reserve``. The caller never supplies the
  mandate's revision, owner, payer or cap. The current mandate schema forces
  ``max_cost_cents == 0``, so every real dispatch holds with ``mandate_cap_required`` until
  OPE-426 records a provider cap.
* Rotation retires the previous reference; revocation and withdrawal fence in-flight jobs; a
  withdrawn account stays withdrawn until an explicit, journaled re-enrolment. A fenced job's
  outcome stays unrecorded until a caller records what was observed. It is never assumed.

Test seams (``test_clock``, ``ProviderCapTestSeam``) are refused unless ``enable_test_seams`` was
called in a process that has loaded pytest. Production uses only ``time.time`` and an exact
``LearningMandates``.

Not proven (see ``services/cell-runtime/docs/pilot-provider-credential.md``): no provider call is
made, no provider-side revocation or refusal is witnessed, no provider-side budget is set, and the
same-uid ``/proc`` exposure between the worker units remains. Evidence tier is FIXTURE.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import stat
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, NoReturn

from opentour_cell.pilot.connector_ext import EvidenceTier
from opentour_cell.pilot.learning_mandate import LearningMandates, MandateError

VERSION = "opentour.pilot.provider-credential.v1"
EVIDENCE_TIER = EvidenceTier.FIXTURE
PROVIDER_SIDE = "not_attempted_out_of_scope"
EXTERNAL_GATES = (
    "customer_created_least_privilege_credential_in_own_provider_account",
    "provider_side_budget_or_usage_limit_witnessed",
    "customer_secret_store_readable_only_by_dispatch_identity",
    "provider_side_refusal_of_rotated_credential",
    "provider_side_revocation_witnessed",
    "standing_mandate_records_nonzero_provider_cap",
    "standing_mandate_records_provider_account_and_cost_usage",
    "credential_withdrawal_linked_to_learning_mandate_withdrawal",
    "separate_uid_or_private_pids_between_cycle_and_managed_worker",
)
LEDGER_NAME = "provider-credential-ledger.jsonl"
MAX_ID = 64
MAX_LOCATOR = 200
MAX_SECRET_BYTES = 4096
CAP_CURRENCY = "usd"  # The mandate's caps are cents; owner settings accept USD only.
_GENESIS = "0" * 64
_LOG = logging.getLogger(__name__)

_ID = re.compile(r"[a-z0-9][a-z0-9._:-]*")
_SEGMENT = re.compile(r"[a-z0-9][a-z0-9._-]*")
_STORE_KEY = re.compile(r"[a-z0-9][a-z0-9._/-]*")
_SPLIT = re.compile(r"[-_.:/@\s]+")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_RESERVATION = re.compile(r"fit-[0-9a-f]{40}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Known credential prefixes, matched case-insensitively at a token boundary.
_PREFIXES = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:sk-|sk_|rk_|pk_live|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|"
    r"xox[abprs]-|xapp-|hf_|r8_|sess-|-----begin|(?:akia|asia)[a-z0-9]{16}|aiza[a-z0-9_-]{20}|"
    r"eyj[a-z0-9_-]{10}|bearer[\s:=]|basic\s)"
)


class CredentialError(ValueError):
    """A public-safe refusal. ``code`` is a fixed lowercase reason; nothing else is carried."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise CredentialError(code)


def _need(check: object, code: str) -> None:
    if not check:
        _fail(code)


# --------------------------------------------------------------------------- test seams
_TEST_SEAMS = False


def enable_test_seams() -> None:
    """Allow the clock and provider-cap seams. Tests only: refused unless pytest is loaded.

    This is a guard against accidental production use, not a security boundary: code running as
    the same uid can do anything this module can.
    """
    global _TEST_SEAMS
    _need("pytest" in sys.modules, "test_seams_refused")
    _TEST_SEAMS = True


def _seam() -> None:
    _need(_TEST_SEAMS, "test_seam_refused_outside_tests")


class Scope(StrEnum):
    FINE_TUNE = "fine_tune"
    MODEL_READ = "model_read"


class LocatorKind(StrEnum):
    FILE = "file"
    STORE_KEY = "store_key"


class RevocationCause(StrEnum):
    WITHDRAWAL = "withdrawal"
    COMPROMISE = "suspected_compromise"
    ROTATION_CLEANUP = "rotation_cleanup"
    OWNER_REQUEST = "owner_request"


class JobOutcome(StrEnum):
    CANCELED = "canceled"
    COMPLETED = "completed"
    FAILED = "failed"
    RUNNING_AFTER_FENCE = "running_after_fence"
    UNKNOWN = "unknown"


class ObservedBy(StrEnum):
    PROVIDER_REPORTED = "provider_reported"
    CONNECTOR_OBSERVED = "connector_observed"
    NOT_OBSERVABLE = "not_observable"


# --------------------------------------------------------------------------- value refusal
def _entropy(text: str) -> float:
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def suspected_value(text: str, *, bound: int = MAX_ID, allow_uuid: bool = False) -> str | None:
    """Return a refusal code when ``text`` looks like a credential value, else ``None``.

    This is a heuristic screen for accidental pastes, not a guarantee: a short or low-entropy
    secret can pass. Readable references use lowercase words joined by separators. With
    ``allow_uuid`` (locator segments only) a canonical lowercase UUID is not counted toward the
    entropy screen; the length bound and key-prefix screen still apply to the whole text.
    """
    if len(text) > bound:
        return "reference_field_too_long"
    if _PREFIXES.search(text):
        return "suspected_credential_prefix"
    if not allow_uuid and _UUID.search(text):
        return "suspected_credential_entropy"
    screened = _UUID.sub("uuid", text) if allow_uuid else text
    for word in _SPLIT.split(screened):
        mixed = any(c.isdigit() for c in word) and any(c.isalpha() for c in word)
        cased = any(c.isupper() for c in word) and any(c.islower() for c in word)
        if (
            len(word) >= 20
            or (len(word) >= 10 and (mixed or cased))
            or (len(word) >= 16 and _entropy(word) >= 3.5)
        ):
            return "suspected_credential_entropy"
    joined = _SPLIT.sub("", screened)
    upper = any(c.isupper() for c in joined)
    if (len(joined) >= 16 and upper and _entropy(joined) >= 3.5) or (
        len(joined) >= 24 and _entropy(joined) >= 4.0
    ):
        return "suspected_credential_entropy"
    return None


def _text(value: object, *, bound: int = MAX_ID, allow_uuid: bool = False) -> str:
    _need(isinstance(value, str) and value, "invalid_reference_field")
    assert isinstance(value, str)
    code = suspected_value(value, bound=bound, allow_uuid=allow_uuid)
    if code is not None:
        _fail(code)
    return value


def _ident(value: object) -> str:
    """A caller-supplied reference. Always screened; digest-shaped values are refused."""
    text = _text(value)
    _need(_ID.fullmatch(text), "invalid_reference_field")
    return text


def _digest_from(value: object, pattern: re.Pattern[str], code: str) -> str:
    """A digest whose provenance is this module or the verified mandate ledger, never a caller."""
    _need(isinstance(value, str) and pattern.fullmatch(value), code)
    assert isinstance(value, str)
    return value


def _count(value: object, code: str, *, positive: bool = False) -> int:
    _need(type(value) is int and (value > 0 if positive else value >= 0), code)
    assert isinstance(value, int)
    return value


def _time(value: object) -> int:
    _need(type(value) is int and 0 < value < 2**53, "invalid_reference_time")
    assert isinstance(value, int)
    return value


def _locator(kind: LocatorKind, value: object) -> str:
    """A path or key name. Each segment is screened on its own; the whole has a length bound."""
    _need(isinstance(value, str) and value, "invalid_secret_locator")
    assert isinstance(value, str)
    if len(value) > MAX_LOCATOR:
        _fail("reference_field_too_long")
    for segment in value.split("/"):
        if segment:
            _text(segment, allow_uuid=True)
    if kind is LocatorKind.FILE:
        parts = value.split("/")
        _need(
            value.startswith("/")
            and len(parts) >= 3
            and all(
                _SEGMENT.fullmatch(p) and p not in {".", ".."} and len(p) <= MAX_ID
                for p in parts[1:]
            ),
            "invalid_secret_locator",
        )
    else:
        _need(
            _STORE_KEY.fullmatch(value) and "//" not in value and ".." not in value,
            "invalid_secret_locator",
        )
    return value


# --------------------------------------------------------------------------- records
REFERENCE_KEYS = frozenset(
    {
        "reference_id",
        "provider",
        "account_id",
        "owner_ref",
        "payer_ref",
        "scope",
        "locator_kind",
        "locator",
        "budget_id",
        "issued_at",
        "not_after",
    }
)


@dataclass(frozen=True)
class CredentialReference:
    """A reference to a credential held in the customer's secret store. Never the value."""

    reference_id: str
    provider: str
    account_id: str
    owner_ref: str
    payer_ref: str
    scope: frozenset[Scope]
    locator_kind: LocatorKind
    locator: str
    budget_id: str | None
    issued_at: int
    not_after: int

    def __post_init__(self) -> None:
        for name in ("reference_id", "provider", "account_id", "owner_ref", "payer_ref"):
            _ident(getattr(self, name))
        if self.budget_id is not None:
            _ident(self.budget_id)
        _need(
            isinstance(self.scope, frozenset)
            and self.scope
            and all(isinstance(s, Scope) for s in self.scope),
            "scope_outside_least_privilege",
        )
        _need(isinstance(self.locator_kind, LocatorKind), "invalid_secret_locator")
        _locator(self.locator_kind, self.locator)
        _need(_time(self.issued_at) < _time(self.not_after), "invalid_reference_window")

    @property
    def account(self) -> tuple[str, str]:
        return (self.provider, self.account_id)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "provider": self.provider,
            "account_id": self.account_id,
            "owner_ref": self.owner_ref,
            "payer_ref": self.payer_ref,
            "scope": sorted(s.value for s in self.scope),
            "locator_kind": self.locator_kind.value,
            "locator": self.locator,
            "budget_id": self.budget_id,
            "issued_at": self.issued_at,
            "not_after": self.not_after,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CredentialReference:
        _need(isinstance(value, Mapping), "invalid_credential_reference")
        _need(set(value) == REFERENCE_KEYS, "unknown_or_missing_reference_field")
        scope = value["scope"]
        _need(
            isinstance(scope, list) and scope and len(set(scope)) == len(scope),
            "scope_outside_least_privilege",
        )
        _need(
            all(isinstance(s, str) and s in Scope._value2member_map_ for s in scope),
            "scope_outside_least_privilege",
        )
        kind = value["locator_kind"]
        _need(
            isinstance(kind, str) and kind in LocatorKind._value2member_map_,
            "invalid_secret_locator",
        )
        return cls(
            reference_id=value["reference_id"],
            provider=value["provider"],
            account_id=value["account_id"],
            owner_ref=value["owner_ref"],
            payer_ref=value["payer_ref"],
            scope=frozenset(Scope(s) for s in scope),
            locator_kind=LocatorKind(kind),
            locator=value["locator"],
            budget_id=value["budget_id"],
            issued_at=value["issued_at"],
            not_after=value["not_after"],
        )


@dataclass(frozen=True)
class MandateView:
    """What the verified mandate ledger says, built inside this module. Never caller-supplied."""

    mandate_id: str
    revision: str
    active: bool
    delegate: str
    owner_id: str
    payer: str
    cap_minor_units: int
    consumed_minor_units: int | None
    provider: str | None
    account_id: str | None
    expires_at: int


@dataclass(frozen=True)
class DispatchRequest:
    """What the worker asks for. ``reservation`` is a ``LearningMandates.reserve`` request."""

    job_ref: str
    reference_id: str
    mandate_id: str
    owner_id: str
    payer: str
    currency: str
    max_minor_units: int
    reservation: Mapping[str, Any]


@dataclass(frozen=True)
class DispatchAuthorization:
    authorization_id: str
    job_ref: str
    reference_id: str
    mandate_id: str
    mandate_revision: str
    reservation_id: str
    currency: str
    max_minor_units: int


@dataclass(frozen=True)
class RotationReceipt:
    previous_reference_id: str | None
    reference_id: str
    local_refusal: str
    provider_side_refusal: str = PROVIDER_SIDE


@dataclass(frozen=True)
class RevocationReceipt:
    reference_ids: tuple[str, ...]
    cause: RevocationCause
    fenced_job_refs: tuple[str, ...]
    provider_side_revocation: str = PROVIDER_SIDE


@dataclass(frozen=True)
class JobStatus:
    job_ref: str
    authorization_id: str
    reference_id: str
    fenced: bool
    outcome: JobOutcome | None
    observed_by: ObservedBy | None
    evidence_ref: str | None


@dataclass
class _Job:
    body: dict[str, Any]
    fenced: bool = False
    outcome: JobOutcome | None = None
    observed_by: ObservedBy | None = None
    evidence_ref: str | None = None

    @property
    def reference_id(self) -> str:
        return str(self.body["reference_id"])

    @property
    def in_flight(self) -> bool:
        return self.outcome in {None, JobOutcome.UNKNOWN, JobOutcome.RUNNING_AFTER_FENCE}

    def authorization(self) -> DispatchAuthorization:
        b = self.body
        return DispatchAuthorization(
            b["authorization_id"],
            b["job_ref"],
            b["reference_id"],
            b["mandate_id"],
            b["mandate_revision"],
            b["reservation_id"],
            b["currency"],
            b["max_minor_units"],
        )


@dataclass
class _State:
    references: dict[str, CredentialReference] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)
    locators: set[tuple[str, str]] = field(default_factory=set)
    active: dict[tuple[str, str], str] = field(default_factory=dict)
    withdrawn: set[tuple[str, str]] = field(default_factory=set)
    jobs: dict[str, _Job] = field(default_factory=dict)
    tail: str = _GENESIS
    seq: int = 0


# --------------------------------------------------------------------------- mandate source
class ProviderCapTestSeam:
    """TESTS ONLY. Wraps a real ``LearningMandates`` and supplies what its schema cannot hold.

    ``learning_mandate`` forces ``max_cost_cents == 0`` and records no provider account or cost
    usage, so a real mandate can never authorize a paid provider dispatch. This seam adds a cap,
    a provider account and an in-memory cost tally on top of the real ledger's verified identity,
    revision, expiry, withdrawal state and fit/seconds reservation. ``receipt_overrides`` and
    ``scope_overrides`` exist to test malformed ledger values. Refused outside tests.
    """

    def __init__(
        self,
        mandates: LearningMandates,
        *,
        cap_minor_units: int,
        provider: str,
        account_id: str,
        consumed_minor_units: int = 0,
        receipt_overrides: Mapping[str, Any] | None = None,
        scope_overrides: Mapping[str, Any] | None = None,
    ) -> None:
        _seam()
        _need(type(mandates) is LearningMandates, "mandate_source_untrusted")
        self.mandates = mandates
        self.cap_minor_units = cap_minor_units
        self.provider, self.account_id = provider, account_id
        self.consumed_minor_units = consumed_minor_units
        self.receipt_overrides = dict(receipt_overrides or {})
        self.scope_overrides = dict(scope_overrides or {})
        self.costs: dict[str, int] = {}

    def inspect(self, mandate_id: str, *, actor: str | None = None) -> dict[str, Any]:
        _seam()
        receipt = dict(self.mandates.inspect(mandate_id, actor=actor))
        scope = dict(receipt["scope"], max_cost_cents=self.cap_minor_units)
        scope.update(self.scope_overrides)
        consumed: Any = self.consumed_minor_units
        if self.costs:
            consumed = consumed + sum(self.costs.values())
        receipt.update(
            scope=scope,
            provider=self.provider,
            provider_account_id=self.account_id,
            cumulative_reserved_cost_cents=consumed,
        )
        receipt.update(self.receipt_overrides)
        return receipt

    def reserve(
        self, mandate_id: str, request: Mapping[str, Any], *, actor: str, cost_minor_units: int
    ) -> dict[str, Any]:
        _seam()
        used = self.consumed_minor_units + sum(self.costs.values())
        if used + cost_minor_units > self.cap_minor_units:
            raise MandateError("standing_cost_budget_exhausted")
        result = dict(self.mandates.reserve(mandate_id, request, actor=actor))
        self.costs.setdefault(str(result.get("reservation_id")), cost_minor_units)
        return result


MandateSource = LearningMandates | ProviderCapTestSeam


def _trusted_source(source: object) -> MandateSource:
    if type(source) is LearningMandates:
        return source
    if type(source) is ProviderCapTestSeam:
        _seam()
        return source
    _fail("mandate_source_untrusted")


def _mandate_code(exc: BaseException) -> str:
    text = str(exc)
    return (
        "mandate_" + text
        if isinstance(exc, MandateError) and _CODE.fullmatch(text)
        else ("mandate_unavailable")
    )


def _inspect(source: MandateSource, mandate_id: str, actor: str) -> dict[str, Any]:
    code: str | None = None
    try:
        receipt = source.inspect(mandate_id, actor=actor)
    except CredentialError:
        raise
    except (MandateError, OSError, sqlite3.Error, TypeError, ValueError, KeyError) as exc:
        code = _mandate_code(exc)
    if code is not None:
        _fail(code)
    _need(isinstance(receipt, dict), "mandate_view_invalid")
    return receipt


def _mandate_view(receipt: Mapping[str, Any], now: int) -> MandateView:
    """Type-check every value the ledger returned; a malformed value refuses, never raises."""
    try:
        spec = receipt["scope"]
        _need(isinstance(spec, Mapping), "mandate_view_invalid")
        expires = _count(receipt["expires_at"], "mandate_view_invalid", positive=True)
        _need(spec["expires_at"] == expires, "mandate_view_invalid")
        withdrawn, expired = receipt["withdrawn"], receipt["expired"]
        _need(type(withdrawn) is bool and type(expired) is bool, "mandate_view_invalid")
        consumed = receipt.get("cumulative_reserved_cost_cents")
        provider = receipt.get("provider")
        account = receipt.get("provider_account_id")
        view = MandateView(
            mandate_id=_ident(receipt["mandate_id"]),
            revision=_digest_from(receipt["mandate_revision"], _HEX64, "mandate_view_invalid"),
            active=not withdrawn and not expired and now < expires and spec["mode"] == "autonomous",
            delegate=_ident(spec["delegate"]),
            owner_id=_ident(receipt["owner_id"]),
            payer=_ident(spec["payer"]),
            cap_minor_units=_count(spec["max_cost_cents"], "mandate_view_invalid"),
            consumed_minor_units=None
            if consumed is None
            else _count(consumed, "mandate_view_invalid"),
            provider=None if provider is None else _ident(provider),
            account_id=None if account is None else _ident(account),
            expires_at=expires,
        )
        _need(spec["owner_id"] == view.owner_id, "mandate_view_invalid")
    except (KeyError, TypeError, AttributeError, CredentialError):
        view = None
    _need(view is not None, "mandate_view_invalid")
    assert view is not None
    return view


# --------------------------------------------------------------------------- ledger
_BODY_KEYS: dict[str, set[str]] = {
    "issued": {"reference"},
    "activated": {"reference_id", "supersedes"},
    "revoked": {"reference_ids", "cause", "fenced_job_refs", "provider_side"},
    "withdrawn": {"provider", "account_id", "reference_ids", "fenced_job_refs", "provider_side"},
    "reenrolled": {"provider", "account_id", "owner_ref"},
    "dispatch_authorized": {
        "authorization_id",
        "job_ref",
        "reference_id",
        "mandate_id",
        "mandate_revision",
        "reservation_id",
        "actor",
        "currency",
        "max_minor_units",
    },
    "job_outcome": {"job_ref", "outcome", "observed_by", "evidence_ref"},
}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _authorization_id(body: Mapping[str, Any]) -> str:
    unsigned = {k: v for k, v in body.items() if k != "authorization_id"}
    return hashlib.sha256(_canonical([VERSION, "dispatch", unsigned])).hexdigest()


class CredentialLedger:
    """Durable, append-only, hash-chained lifecycle journal. Holds references, never values.

    The chain detects edits, reordering and removal of interior lines. It does not detect removal
    or rewriting of the final lines, and anyone who can write the directory as this uid can
    rewrite it: it is tamper-evident storage that only this module should write.
    """

    def __init__(
        self, directory: str | Path, *, test_clock: Callable[[], float] | None = None
    ) -> None:
        if test_clock is not None:
            _seam()
        self.directory = Path(directory)
        self._clock: Callable[[], float] = test_clock or time.time
        if not os.path.lexists(self.directory):
            os.mkdir(self.directory, 0o700)
        info = os.lstat(self.directory)
        _need(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700,
            "ledger_directory_unsafe",
        )
        self.path = self.directory / LEDGER_NAME

    def __repr__(self) -> str:
        return f"CredentialLedger({str(self.directory)!r})"

    def _now(self) -> int:
        return int(self._clock())

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd = os.open(self.directory / ".ledger.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _lines(self) -> list[bytes]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return []
        except OSError:
            _fail("credential_ledger_unreadable")
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            _need(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink == 1,
                "credential_ledger_unsafe",
            )
            data = stream.read()
        _need(not data or data.endswith(b"\n"), "credential_ledger_corrupt")
        return data.splitlines()

    def _fold(self) -> _State:
        state = _State()
        for line in self._lines():
            try:
                event = json.loads(line)
            except ValueError:
                _fail("credential_ledger_corrupt")
            _need(
                isinstance(event, dict)
                and set(event) == {"v", "seq", "prev", "kind", "at", "body"}
                and event["v"] == VERSION
                and event["seq"] == state.seq + 1
                and event["prev"] == state.tail
                and isinstance(event["body"], dict)
                and type(event["at"]) is int
                and _canonical(event) == line,
                "credential_ledger_corrupt",
            )
            try:
                self._apply(state, event["kind"], event["body"])
            except (CredentialError, KeyError, TypeError, ValueError):
                _fail("credential_ledger_corrupt")
            state.seq, state.tail = event["seq"], hashlib.sha256(line).hexdigest()
        return state

    @staticmethod
    def _fence(state: _State, ids: Sequence[str]) -> list[str]:
        return sorted(
            job_ref
            for job_ref, job in state.jobs.items()
            if job.reference_id in ids and job.in_flight and not job.fenced
        )

    @staticmethod
    def _apply(state: _State, kind: str, body: dict[str, Any]) -> None:
        _need(set(body) == _BODY_KEYS.get(kind, set()) and body, "credential_ledger_corrupt")
        if kind == "issued":
            ref = CredentialReference.from_mapping(body["reference"])
            locator = (ref.locator_kind.value, ref.locator)
            _need(ref.reference_id not in state.references, "reference_already_issued")
            _need(locator not in state.locators, "secret_locator_reused")
            _need(ref.account not in state.withdrawn, "account_withdrawn")
            state.references[ref.reference_id] = ref
            state.status[ref.reference_id] = "issued"
            state.locators.add(locator)
        elif kind == "activated":
            ref = state.references[body["reference_id"]]
            _need(state.status[ref.reference_id] == "issued", "reference_not_activatable")
            _need(ref.account not in state.withdrawn, "account_withdrawn")
            _need(state.active.get(ref.account) == body["supersedes"], "rotation_conflict")
            if body["supersedes"] is not None:
                state.status[body["supersedes"]] = "retired"
            state.status[ref.reference_id] = "active"
            state.active[ref.account] = ref.reference_id
        elif kind in {"revoked", "withdrawn"}:
            _need(body["provider_side"] == PROVIDER_SIDE, "credential_ledger_corrupt")
            ids = body["reference_ids"]
            _need(isinstance(ids, list), "credential_ledger_corrupt")
            if kind == "revoked":
                RevocationCause(body["cause"])
                _need(ids, "reference_unknown")
            else:
                account = (body["provider"], body["account_id"])
                _need(account not in state.withdrawn, "account_already_withdrawn")
                _need(
                    any(r.account == account for r in state.references.values()),
                    "no_reference_to_withdraw",
                )
                _need(
                    sorted(ids)
                    == sorted(
                        r.reference_id
                        for r in state.references.values()
                        if r.account == account and state.status[r.reference_id] != "revoked"
                    ),
                    "credential_ledger_corrupt",
                )
            _need(
                body["fenced_job_refs"] == CredentialLedger._fence(state, ids),
                "credential_ledger_corrupt",
            )
            for reference_id in ids:
                ref = state.references[reference_id]
                _need(state.status[reference_id] != "revoked", "reference_already_revoked")
                state.status[reference_id] = "revoked"
                if state.active.get(ref.account) == reference_id:
                    del state.active[ref.account]
            for job_ref in body["fenced_job_refs"]:
                state.jobs[job_ref].fenced = True
            if kind == "withdrawn":
                state.withdrawn.add((body["provider"], body["account_id"]))
        elif kind == "reenrolled":
            account = (body["provider"], body["account_id"])
            _need(account in state.withdrawn, "account_not_withdrawn")
            _need(
                body["owner_ref"]
                in {r.owner_ref for r in state.references.values() if r.account == account},
                "reenrolment_owner_mismatch",
            )
            state.withdrawn.discard(account)
        elif kind == "dispatch_authorized":
            for name in ("job_ref", "reference_id", "mandate_id", "actor", "currency"):
                _ident(body[name])
            _digest_from(body["mandate_revision"], _HEX64, "credential_ledger_corrupt")
            _digest_from(body["reservation_id"], _RESERVATION, "credential_ledger_corrupt")
            _count(body["max_minor_units"], "credential_ledger_corrupt", positive=True)
            _need(body["authorization_id"] == _authorization_id(body), "credential_ledger_corrupt")
            _need(body["job_ref"] not in state.jobs, "job_already_authorized")
            ref = state.references[body["reference_id"]]
            _need(state.status[ref.reference_id] == "active", "reference_not_active")
            _need(ref.account not in state.withdrawn, "account_withdrawn")
            state.jobs[body["job_ref"]] = _Job(dict(body))
        elif kind == "job_outcome":
            job = state.jobs[body["job_ref"]]
            _need(job.in_flight, "job_outcome_already_final")
            job.outcome = JobOutcome(body["outcome"])
            job.observed_by = ObservedBy(body["observed_by"])
            job.evidence_ref = _ident(body["evidence_ref"])
        else:
            _fail("credential_ledger_corrupt")

    def _append(self, state: _State, kind: str, body: dict[str, Any]) -> None:
        # Validate against the current fold first: a refused event is never written.
        self._apply(state, kind, body)
        seq = state.seq + 1
        event = {
            "v": VERSION,
            "seq": seq,
            "prev": state.tail,
            "kind": kind,
            "at": self._now(),
            "body": body,
        }
        line = _canonical(event)
        created = not self.path.exists()
        fd = os.open(
            self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        try:
            os.write(fd, line + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            directory = os.open(self.directory, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        state.seq, state.tail = seq, hashlib.sha256(line).hexdigest()
        _LOG.info("provider_credential.%s seq=%d", kind, state.seq)

    # ---- lifecycle -------------------------------------------------------------------------
    def _issue(self, state: _State, reference: CredentialReference) -> None:
        _need(isinstance(reference, CredentialReference), "invalid_credential_reference")
        _need(self._now() < reference.not_after, "reference_expired")
        self._append(state, "issued", {"reference": reference.to_mapping()})

    def _activate(self, state: _State, reference_id: str) -> RotationReceipt:
        ref = state.references.get(reference_id)
        _need(ref is not None, "reference_unknown")
        assert ref is not None
        _need(ref.issued_at <= self._now() < ref.not_after, "reference_outside_window")
        previous = state.active.get(ref.account)
        self._append(state, "activated", {"reference_id": reference_id, "supersedes": previous})
        return RotationReceipt(previous, reference_id, local_refusal="previous_reference_retired")

    def issue(self, reference: CredentialReference) -> None:
        with self._locked():
            self._issue(self._fold(), reference)

    def activate(self, reference_id: str) -> RotationReceipt:
        """Switch the account to ``reference_id``; the previously active reference is retired."""
        with self._locked():
            return self._activate(self._fold(), reference_id)

    def rotate(self, reference: CredentialReference) -> RotationReceipt:
        """Rotation drill: record a new reference and switch to it, under one lock.

        The old reference is refused locally from then on. Jobs authorized under it are not
        fenced (both credentials stay valid at the provider). A provider-side refusal of the old
        credential is out of scope and reported as ``provider_side_refusal``.
        """
        with self._locked():
            state = self._fold()
            _need(isinstance(reference, CredentialReference), "invalid_credential_reference")
            _need(state.active.get(reference.account) is not None, "no_active_reference_to_rotate")
            self._issue(state, reference)
            return self._activate(state, reference.reference_id)

    def revoke(self, reference_ids: Sequence[str], cause: RevocationCause) -> RevocationReceipt:
        _need(isinstance(cause, RevocationCause), "invalid_revocation_cause")
        ids = list(dict.fromkeys(reference_ids))
        _need(ids, "reference_unknown")
        with self._locked():
            state = self._fold()
            for reference_id in ids:
                _need(reference_id in state.references, "reference_unknown")
            fenced = self._fence(state, ids)
            self._append(
                state,
                "revoked",
                {
                    "reference_ids": ids,
                    "cause": cause.value,
                    "fenced_job_refs": fenced,
                    "provider_side": PROVIDER_SIDE,
                },
            )
        return RevocationReceipt(tuple(ids), cause, tuple(fenced))

    def withdraw(self, provider: str, account_id: str) -> RevocationReceipt:
        """Withdraw the account: one event, under one lock, revokes every unrevoked reference,
        fences their in-flight jobs and blocks new references until ``reenroll``."""
        account = (_ident(provider), _ident(account_id))
        with self._locked():
            state = self._fold()
            _need(account not in state.withdrawn, "account_already_withdrawn")
            _need(
                any(r.account == account for r in state.references.values()),
                "no_reference_to_withdraw",
            )
            ids = sorted(
                r.reference_id
                for r in state.references.values()
                if r.account == account and state.status[r.reference_id] != "revoked"
            )
            fenced = self._fence(state, ids)
            self._append(
                state,
                "withdrawn",
                {
                    "provider": provider,
                    "account_id": account_id,
                    "reference_ids": ids,
                    "fenced_job_refs": fenced,
                    "provider_side": PROVIDER_SIDE,
                },
            )
        return RevocationReceipt(tuple(ids), RevocationCause.WITHDRAWAL, tuple(fenced))

    def reenroll(self, provider: str, account_id: str, *, owner_ref: str) -> None:
        """Journal an explicit re-enrolment of a withdrawn account by its account owner."""
        body = {
            "provider": _ident(provider),
            "account_id": _ident(account_id),
            "owner_ref": _ident(owner_ref),
        }
        with self._locked():
            self._append(self._fold(), "reenrolled", body)

    def authorize_dispatch(
        self, request: DispatchRequest, *, mandates: MandateSource, actor: str
    ) -> DispatchAuthorization:
        """Bind a dispatch to an active reference and the verified standing mandate.

        The mandate is read here from ``mandates`` (an exact ``LearningMandates``) and the budget
        is reserved through it; nothing about the mandate is taken from the caller. The
        authorization is journaled so a revocation can fence the job and ``resolve`` can check it.
        """
        _need(isinstance(request, DispatchRequest), "invalid_dispatch_request")
        for name in ("job_ref", "reference_id", "mandate_id", "owner_id", "payer", "currency"):
            _ident(getattr(request, name))
        _ident(actor)
        bound = _count(request.max_minor_units, "mandate_cap_exceeded", positive=True)
        source = _trusted_source(mandates)
        with self._locked():
            state = self._fold()
            ref = state.references.get(request.reference_id)
            _need(ref is not None, "reference_unknown")
            assert ref is not None
            _need(ref.account not in state.withdrawn, "account_withdrawn")
            _need(state.status[ref.reference_id] == "active", "reference_not_active")
            _need(ref.issued_at <= self._now() < ref.not_after, "reference_outside_window")
            _need(Scope.FINE_TUNE in ref.scope, "reference_scope_lacks_fine_tune")
            view = _mandate_view(_inspect(source, request.mandate_id, actor), self._now())
            _need(view.mandate_id == request.mandate_id, "mandate_view_invalid")
            _need(view.active, "mandate_not_active")
            _need(actor == view.delegate, "mandate_delegate_mismatch")
            _need(view.cap_minor_units > 0, "mandate_cap_required")
            _need(
                view.provider is not None and view.account_id is not None,
                "mandate_provider_account_required",
            )
            _need(view.consumed_minor_units is not None, "mandate_cost_usage_unavailable")
            assert view.consumed_minor_units is not None
            _need(request.owner_id == view.owner_id == ref.owner_ref, "mandate_owner_mismatch")
            _need(request.payer == view.payer == ref.payer_ref, "mandate_payer_mismatch")
            _need((view.provider, view.account_id) == ref.account, "mandate_account_mismatch")
            _need(request.currency == CAP_CURRENCY, "mandate_currency_mismatch")
            existing = state.jobs.get(request.job_ref)
            if existing is not None:
                prior = existing.body
                same = {
                    "reference_id": request.reference_id,
                    "mandate_id": request.mandate_id,
                    "mandate_revision": view.revision,
                    "actor": actor,
                    "currency": request.currency,
                    "max_minor_units": bound,
                }
                _need(all(prior[k] == v for k, v in same.items()), "job_already_authorized")
                # A fenced job's reference is revoked, so it never reaches this branch.
                _need(existing.in_flight, "job_already_settled")
                _need(existing.outcome is None, "job_outcome_recorded")
                return existing.authorization()
            # Conservative local bound in addition to the mandate ledger's own reservation: every
            # authorization journaled here counts at its full maximum, whatever it later used.
            local = sum(
                job.body["max_minor_units"]
                for job in state.jobs.values()
                if job.body["mandate_id"] == request.mandate_id
            )
            _need(
                max(local, view.consumed_minor_units) + bound <= view.cap_minor_units,
                "mandate_cap_exceeded",
            )
            reservation_id = self._reserve(source, request, actor, bound)
            body: dict[str, Any] = {
                "job_ref": request.job_ref,
                "reference_id": request.reference_id,
                "mandate_id": request.mandate_id,
                "mandate_revision": view.revision,
                "reservation_id": reservation_id,
                "actor": actor,
                "currency": request.currency,
                "max_minor_units": bound,
            }
            body["authorization_id"] = _authorization_id(body)
            self._append(state, "dispatch_authorized", body)
        return state.jobs[request.job_ref].authorization()

    @staticmethod
    def _reserve(source: MandateSource, request: DispatchRequest, actor: str, cost: int) -> str:
        code: str | None = None
        result: Any = None
        try:
            if isinstance(source, ProviderCapTestSeam):
                result = source.reserve(
                    request.mandate_id, request.reservation, actor=actor, cost_minor_units=cost
                )
            else:  # pragma: no cover - unreachable until the mandate schema holds a cost cap
                result = source.reserve(request.mandate_id, request.reservation, actor=actor)
        except CredentialError:
            raise
        except (MandateError, OSError, sqlite3.Error, TypeError, ValueError, KeyError) as exc:
            code = _mandate_code(exc)
        if code is not None:
            _fail(code)
        _need(isinstance(result, Mapping), "mandate_reservation_invalid")
        return _digest_from(
            result.get("reservation_id"), _RESERVATION, "mandate_reservation_invalid"
        )

    def record_outcome(
        self, job_ref: str, outcome: JobOutcome, observed_by: ObservedBy, evidence_ref: str
    ) -> JobStatus:
        """Record what was observed for a job. ``unknown`` is a valid record; nothing is assumed.

        ``observed_by`` and ``evidence_ref`` are the caller's statement; nothing here verifies
        them against the provider.
        """
        _need(isinstance(outcome, JobOutcome), "invalid_job_outcome")
        _need(isinstance(observed_by, ObservedBy), "invalid_job_outcome")
        _ident(job_ref)
        _ident(evidence_ref)
        _need(
            outcome is JobOutcome.UNKNOWN or observed_by is not ObservedBy.NOT_OBSERVABLE,
            "observed_outcome_requires_observation",
        )
        with self._locked():
            state = self._fold()
            job = state.jobs.get(job_ref)
            _need(job is not None, "job_unknown")
            assert job is not None
            if job.outcome is not None and not job.in_flight:
                _need(
                    (job.outcome, job.observed_by, job.evidence_ref)
                    == (outcome, observed_by, evidence_ref),
                    "job_outcome_already_final",
                )
            else:
                self._append(
                    state,
                    "job_outcome",
                    {
                        "job_ref": job_ref,
                        "outcome": outcome.value,
                        "observed_by": observed_by.value,
                        "evidence_ref": evidence_ref,
                    },
                )
        return self.job_status(job_ref)

    # ---- readback --------------------------------------------------------------------------
    def status(self, reference_id: str) -> str:
        state = self._fold()
        _need(reference_id in state.status, "reference_unknown")
        return state.status[reference_id]

    def reference(self, reference_id: str) -> CredentialReference:
        state = self._fold()
        _need(reference_id in state.references, "reference_unknown")
        return state.references[reference_id]

    def withdrawn(self, provider: str, account_id: str) -> bool:
        return (provider, account_id) in self._fold().withdrawn

    def job_status(self, job_ref: str) -> JobStatus:
        job = self._fold().jobs.get(job_ref)
        _need(job is not None, "job_unknown")
        assert job is not None
        return JobStatus(
            job_ref,
            job.body["authorization_id"],
            job.reference_id,
            job.fenced,
            job.outcome,
            job.observed_by,
            job.evidence_ref,
        )


# --------------------------------------------------------------------------- resolver
class SecretValue:
    """A credential value handed to the caller. Redacted in repr/str; cannot be pickled.

    A value resolved before a revocation stays usable by whoever holds it; revocation only
    stops further resolution.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        raise TypeError("secret_not_serializable")

    def __getstate__(self) -> NoReturn:
        raise TypeError("secret_not_serializable")


def _read_secret(fd: int, expected_uid: int) -> tuple[str | None, str | None]:
    """Consume ``fd``. Never raises, so the value never reaches an exception or traceback."""
    try:
        return _read_open(fd, expected_uid)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _read_open(fd: int, expected_uid: int) -> tuple[str | None, str | None]:
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "secret_not_regular_file"
        if info.st_nlink != 1:
            return None, "secret_link_refused"
        if info.st_uid != expected_uid:
            return None, "secret_owner_mismatch"
        if stat.S_IMODE(info.st_mode) != 0o600:
            return None, "secret_mode_not_0600"
        if info.st_size > MAX_SECRET_BYTES:
            return None, "secret_too_large"
        raw = os.read(fd, MAX_SECRET_BYTES + 1)
    except OSError:
        return None, "secret_unreadable"
    if len(raw) > MAX_SECRET_BYTES:
        return None, "secret_too_large"
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if not raw:
        return None, "secret_empty"
    if not all(0x21 <= b <= 0x7E for b in raw):
        return None, "secret_format_invalid"
    return raw.decode("ascii"), None


class CredentialResolver:
    """Reads the value at the moment of use, for one journaled, unfenced authorization only.

    Never logs, keeps or persists the value. Re-reads the mandate ledger and refuses when the
    mandate was withdrawn, expired or changed revision since the authorization.
    """

    def __init__(
        self, ledger: CredentialLedger, *, expected_uid: int, mandates: MandateSource
    ) -> None:
        self.ledger = ledger
        self.expected_uid = expected_uid
        self.mandates = _trusted_source(mandates)

    def __repr__(self) -> str:
        return f"CredentialResolver(expected_uid={self.expected_uid})"

    def resolve(self, authorization_id: str, *, job_ref: str) -> SecretValue:
        _ident(job_ref)
        _digest_from(authorization_id, _HEX64, "dispatch_authorization_required")
        state = self.ledger._fold()
        job = state.jobs.get(job_ref)
        _need(
            job is not None and job.body["authorization_id"] == authorization_id,
            "dispatch_authorization_required",
        )
        assert job is not None
        _need(not job.fenced, "dispatch_fenced")
        _need(job.outcome is None, "dispatch_settled")
        reference_id = job.reference_id
        ref = state.references[reference_id]
        status = state.status[reference_id]
        _need(status != "revoked", "reference_revoked")
        _need(status != "retired", "reference_retired")
        _need(status == "active", "reference_not_active")
        now = self.ledger._now()
        _need(ref.issued_at <= now < ref.not_after, "reference_outside_window")
        view = _mandate_view(
            _inspect(_trusted_source(self.mandates), job.body["mandate_id"], job.body["actor"]),
            now,
        )
        _need(view.active, "mandate_not_active")
        _need(view.revision == job.body["mandate_revision"], "mandate_changed")
        _need(ref.locator_kind is LocatorKind.FILE, "locator_kind_unsupported")
        value, code = _read_secret(self._open(ref.locator), self.expected_uid)
        if code is not None:
            _LOG.warning("provider_credential.refused reference=%s code=%s", reference_id, code)
            _fail(code)
        assert value is not None
        _LOG.info("provider_credential.resolved reference=%s", reference_id)
        return SecretValue(value)

    def _open(self, locator: str) -> int:
        parts = locator.split("/")[1:]
        code: str | None = None
        fd = os.open("/", os.O_DIRECTORY | os.O_RDONLY | os.O_CLOEXEC)
        try:
            for index, part in enumerate(parts):
                last = index == len(parts) - 1
                try:
                    info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    code = "secret_unavailable"
                    break
                if stat.S_ISLNK(info.st_mode):
                    code = "secret_link_refused"
                    break
                if last:
                    parent = os.fstat(fd)
                    if parent.st_uid not in {0, self.expected_uid} or parent.st_mode & 0o022:
                        code = "secret_parent_unsafe"
                        break
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                    flags |= os.O_NOCTTY
                else:
                    flags = os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                try:
                    child = os.open(part, flags, dir_fd=fd)
                except OSError as exc:
                    code = (
                        "secret_link_refused"
                        if exc.errno in {errno.ELOOP, errno.EMLINK}
                        else "secret_unavailable"
                    )
                    break
                os.close(fd)
                fd = child
        except OSError:
            code = "secret_unavailable"
        if code is not None:
            os.close(fd)
            _fail(code)
        return fd


# --------------------------------------------------------------------------- probe CLI
def main(argv: list[str] | None = None) -> int:
    """``probe``: resolve one authorization in this fresh process; print only a result code."""
    parser = argparse.ArgumentParser(prog="provider-credential")
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe")
    probe.add_argument("--ledger", required=True)
    probe.add_argument("--authorization", required=True)
    probe.add_argument("--job", required=True)
    probe.add_argument("--expected-uid", type=int, required=True)
    probe.add_argument("--mandates", required=True)
    probe.add_argument("--organization", required=True)
    probe.add_argument("--engagement", required=True)
    args = parser.parse_args(argv)
    try:
        mandates = LearningMandates(args.mandates, args.organization, args.engagement)
        resolver = CredentialResolver(
            CredentialLedger(args.ledger), expected_uid=args.expected_uid, mandates=mandates
        )
        resolver.resolve(args.authorization, job_ref=args.job)
        result = "resolved_locally"
    except CredentialError as exc:
        result = exc.code
    except MandateError as exc:
        result = _mandate_code(exc)
    except (OSError, sqlite3.Error):
        result = "probe_unavailable"
    print(json.dumps({"result": result, "provider_side_refusal": PROVIDER_SIDE}, sort_keys=True))
    return 0 if result == "resolved_locally" else 2


if __name__ == "__main__":
    sys.exit(main())
