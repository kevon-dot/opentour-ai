"""Customer-owned fine-tuning provider credential: references and lifecycle records (OPE-578).

This module holds only credential **references** and lifecycle records. It never stores, logs or
returns a credential value except through ``CredentialResolver.resolve``, which reads the value
from the customer's secret-store locator at the moment of use and hands it to the caller wrapped
in a redacting ``SecretValue``.

* ``CredentialReference`` names the provider, the customer's provider account, the account owner
  and payer references, a least-privilege scope (fine-tune and model-read only), the secret-store
  locator (a path or key name, never the value), the provider-side budget id where one exists and
  an ``issued_at``/``not_after`` window. A field that looks like a credential value is refused:
  a known key prefix, a high-entropy run, or anything over the field length bound.
* ``CredentialLedger`` is a durable, append-only, hash-chained JSONL journal of issue, activation
  (rotation), revocation, withdrawal, dispatch authorization and job outcome events. Any malformed
  or chain-broken line fails closed.
* Rotation activates a new reference and retires the previous one in one event. A retired or
  revoked reference, and any reuse of its locator, is refused locally from any process.
* Revocation and withdrawal fence every in-flight job bound to the revoked references. A fenced
  job's outcome stays unrecorded until a caller records what was observed; it is never assumed.
* ``authorize_dispatch`` requires a fresh standing-mandate view (injected) naming the same
  account owner, payer, provider account and a positive cap, and an exact mandate revision match.

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
    "separate_uid_or_private_pids_between_cycle_and_managed_worker",
)
LEDGER_NAME = "provider-credential-ledger.jsonl"
MAX_ID = 64
MAX_LOCATOR = 200
MAX_SECRET_BYTES = 4096
_GENESIS = "0" * 64
_LOG = logging.getLogger(__name__)

_ID = re.compile(r"[a-z0-9][a-z0-9._:-]*")
_SEGMENT = re.compile(r"[a-z0-9][a-z0-9._-]*")
_STORE_KEY = re.compile(r"[a-z0-9][a-z0-9._/-]*")
_SPLIT = re.compile(r"[-_.:/@\s]+")
# Known credential prefixes, matched case-insensitively at a token boundary.
_PREFIXES = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:sk-|sk_|rk_|pk_live|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|"
    r"xox[abprs]-|xapp-|hf_|r8_|sess-|-----begin|(?:akia|asia)[a-z0-9]{16}|aiza[a-z0-9_-]{20}|"
    r"eyj[a-z0-9_-]{10}|bearer[\s:=]|basic\s)"
)
# A digest-shaped reference produced elsewhere (a mandate revision, a fit or evidence id).
_DIGEST_REF = re.compile(r"(?:[a-z][a-z0-9]{0,15}-)?[0-9a-f]{32,64}")


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


def suspected_value(text: str, *, bound: int = MAX_ID) -> str | None:
    """Return a refusal code when ``text`` looks like a credential value, else ``None``.

    This is a heuristic screen for accidental pastes, not a guarantee: a short or low-entropy
    secret can pass. Readable references use lowercase words joined by separators.
    """
    if len(text) > bound:
        return "reference_field_too_long"
    if _PREFIXES.search(text):
        return "suspected_credential_prefix"
    for word in _SPLIT.split(text):
        mixed = any(c.isdigit() for c in word) and any(c.isalpha() for c in word)
        cased = any(c.isupper() for c in word) and any(c.islower() for c in word)
        if (
            len(word) >= 20
            or (len(word) >= 10 and (mixed or cased))
            or (len(word) >= 16 and _entropy(word) >= 3.5)
        ):
            return "suspected_credential_entropy"
    joined = _SPLIT.sub("", text)
    upper = any(c.isupper() for c in joined)
    if (len(joined) >= 16 and upper and _entropy(joined) >= 3.5) or (
        len(joined) >= 24 and _entropy(joined) >= 4.0
    ):
        return "suspected_credential_entropy"
    return None


def _text(value: object, *, bound: int = MAX_ID) -> str:
    _need(isinstance(value, str) and value, "invalid_reference_field")
    assert isinstance(value, str)
    code = suspected_value(value, bound=bound)
    if code is not None:
        _fail(code)
    return value


def _ident(value: object) -> str:
    text = _text(value)
    _need(_ID.fullmatch(text), "invalid_reference_field")
    return text


def _ref(value: object) -> str:
    """An identifier, or a digest-shaped reference that is matched against another record."""
    if isinstance(value, str) and _DIGEST_REF.fullmatch(value):
        return value
    return _ident(value)


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
    if _PREFIXES.search(value):
        _fail("suspected_credential_prefix")
    for segment in value.split("/"):
        if segment:
            _text(segment)
    text = value
    if kind is LocatorKind.FILE:
        parts = text.split("/")
        _need(
            text.startswith("/")
            and len(parts) >= 3
            and all(
                _SEGMENT.fullmatch(p) and p not in {".", ".."} and len(p) <= MAX_ID
                for p in parts[1:]
            ),
            "invalid_secret_locator",
        )
    else:
        _need(
            _STORE_KEY.fullmatch(text) and "//" not in text and ".." not in text,
            "invalid_secret_locator",
        )
    return text


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
    """What the standing mandate currently says. Supplied by the caller, read fresh per dispatch."""

    mandate_id: str
    revision: str
    state: str
    owner_id: str | None
    payer: str | None
    provider: str
    account_id: str
    cap_currency: str | None
    cap_minor_units: int | None
    consumed_minor_units: int
    expires_at: int


@dataclass(frozen=True)
class DispatchRequest:
    job_ref: str
    reference_id: str
    mandate_id: str
    mandate_revision: str
    owner_id: str
    payer: str
    currency: str
    max_minor_units: int


@dataclass(frozen=True)
class DispatchAuthorization:
    job_ref: str
    reference_id: str
    mandate_id: str
    mandate_revision: str
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
    reference_id: str
    fenced: bool
    outcome: JobOutcome | None
    observed_by: ObservedBy | None
    evidence_ref: str | None


@dataclass
class _Job:
    reference_id: str
    request: dict[str, Any]
    fenced: bool = False
    outcome: JobOutcome | None = None
    observed_by: ObservedBy | None = None
    evidence_ref: str | None = None

    @property
    def in_flight(self) -> bool:
        return self.outcome in {None, JobOutcome.UNKNOWN, JobOutcome.RUNNING_AFTER_FENCE}


@dataclass
class _State:
    references: dict[str, CredentialReference] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)
    locators: set[tuple[str, str]] = field(default_factory=set)
    active: dict[tuple[str, str], str] = field(default_factory=dict)
    jobs: dict[str, _Job] = field(default_factory=dict)
    tail: str = _GENESIS
    seq: int = 0


# --------------------------------------------------------------------------- ledger
_BODY_KEYS: dict[str, set[str]] = {
    "issued": {"reference"},
    "activated": {"reference_id", "supersedes"},
    "revoked": {"reference_ids", "cause", "fenced_job_refs", "provider_side"},
    "dispatch_authorized": {
        "job_ref",
        "reference_id",
        "mandate_id",
        "mandate_revision",
        "currency",
        "max_minor_units",
    },
    "job_outcome": {"job_ref", "outcome", "observed_by", "evidence_ref"},
}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


class CredentialLedger:
    """Durable, append-only, hash-chained lifecycle journal. Holds references, never values.

    The chain detects edits, reordering and removal of interior lines. It does not detect removal
    of trailing lines, and anyone who can write the directory as this uid can rewrite it: it is
    tamper-evident storage that only this module should write, not an integrity proof.
    """

    def __init__(self, directory: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.directory = Path(directory)
        self.clock = clock
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
        return int(self.clock())

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
    def _apply(state: _State, kind: str, body: dict[str, Any]) -> None:
        _need(set(body) == _BODY_KEYS.get(kind, set()) and body, "credential_ledger_corrupt")
        if kind == "issued":
            ref = CredentialReference.from_mapping(body["reference"])
            locator = (ref.locator_kind.value, ref.locator)
            _need(ref.reference_id not in state.references, "reference_already_issued")
            _need(locator not in state.locators, "secret_locator_reused")
            state.references[ref.reference_id] = ref
            state.status[ref.reference_id] = "issued"
            state.locators.add(locator)
        elif kind == "activated":
            ref = state.references[body["reference_id"]]
            account = (ref.provider, ref.account_id)
            _need(state.status[ref.reference_id] == "issued", "reference_not_activatable")
            _need(state.active.get(account) == body["supersedes"], "rotation_conflict")
            if body["supersedes"] is not None:
                state.status[body["supersedes"]] = "retired"
            state.status[ref.reference_id] = "active"
            state.active[account] = ref.reference_id
        elif kind == "revoked":
            RevocationCause(body["cause"])
            _need(body["provider_side"] == PROVIDER_SIDE, "credential_ledger_corrupt")
            for reference_id in body["reference_ids"]:
                ref = state.references[reference_id]
                _need(state.status[reference_id] != "revoked", "reference_already_revoked")
                state.status[reference_id] = "revoked"
                if state.active.get((ref.provider, ref.account_id)) == reference_id:
                    del state.active[(ref.provider, ref.account_id)]
            for job_ref in body["fenced_job_refs"]:
                state.jobs[job_ref].fenced = True
        elif kind == "dispatch_authorized":
            for name in ("job_ref", "mandate_revision"):
                _ref(body[name])
            _ident(body["mandate_id"])
            _need(body["job_ref"] not in state.jobs, "job_already_authorized")
            _need(state.status[body["reference_id"]] == "active", "reference_not_active")
            state.jobs[body["job_ref"]] = _Job(body["reference_id"], dict(body))
        elif kind == "job_outcome":
            job = state.jobs[body["job_ref"]]
            _need(job.in_flight, "job_outcome_already_final")
            job.outcome = JobOutcome(body["outcome"])
            job.observed_by = ObservedBy(body["observed_by"])
            job.evidence_ref = _ref(body["evidence_ref"])
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
        previous = state.active.get((ref.provider, ref.account_id))
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

        The old reference is refused locally from then on. A provider-side refusal of the old
        credential is out of scope and reported as ``provider_side_refusal``.
        """
        with self._locked():
            state = self._fold()
            _need(isinstance(reference, CredentialReference), "invalid_credential_reference")
            previous = state.active.get((reference.provider, reference.account_id))
            _need(previous is not None, "no_active_reference_to_rotate")
            self._issue(state, reference)
            return self._activate(state, reference.reference_id)

    def revoke(self, reference_ids: Sequence[str], cause: RevocationCause) -> RevocationReceipt:
        _need(isinstance(cause, RevocationCause), "invalid_revocation_cause")
        ids = tuple(dict.fromkeys(reference_ids))
        _need(ids, "reference_unknown")
        with self._locked():
            state = self._fold()
            for reference_id in ids:
                _need(reference_id in state.references, "reference_unknown")
            fenced = tuple(
                sorted(
                    job_ref
                    for job_ref, job in state.jobs.items()
                    if job.reference_id in ids and job.in_flight and not job.fenced
                )
            )
            self._append(
                state,
                "revoked",
                {
                    "reference_ids": list(ids),
                    "cause": cause.value,
                    "fenced_job_refs": list(fenced),
                    "provider_side": PROVIDER_SIDE,
                },
            )
        return RevocationReceipt(ids, cause, fenced)

    def withdraw(self, provider: str, account_id: str) -> RevocationReceipt:
        """Withdrawal revokes every unrevoked reference of the account and fences its jobs."""
        with self._locked():
            state = self._fold()
            ids = [
                r.reference_id
                for r in state.references.values()
                if (r.provider, r.account_id) == (provider, account_id)
                and state.status[r.reference_id] != "revoked"
            ]
        _need(ids, "no_reference_to_withdraw")
        return self.revoke(ids, RevocationCause.WITHDRAWAL)

    def authorize_dispatch(
        self, request: DispatchRequest, mandate_view: Callable[[], MandateView | None]
    ) -> DispatchAuthorization:
        """Bind a dispatch to an active reference and the current standing mandate.

        Does not reserve or settle budget; that stays with the mandate ledger. The authorization
        is journaled so a later revocation can fence the job.
        """
        _need(isinstance(request, DispatchRequest), "invalid_dispatch_request")
        _ref(request.job_ref)
        _ref(request.mandate_revision)
        for name in ("reference_id", "mandate_id", "owner_id", "payer", "currency"):
            _ident(getattr(request, name))
        with self._locked():
            state = self._fold()
            ref = state.references.get(request.reference_id)
            _need(ref is not None, "reference_unknown")
            assert ref is not None
            _need(state.status[ref.reference_id] == "active", "reference_not_active")
            _need(ref.issued_at <= self._now() < ref.not_after, "reference_outside_window")
            _need(Scope.FINE_TUNE in ref.scope, "reference_scope_lacks_fine_tune")
            view = mandate_view()
            _need(isinstance(view, MandateView), "standing_mandate_required")
            assert view is not None
            _need(view.state == "active" and self._now() < view.expires_at, "mandate_not_active")
            _need(view.owner_id, "mandate_owner_required")
            _need(view.payer, "mandate_payer_required")
            _need(
                view.cap_currency
                and type(view.cap_minor_units) is int
                and view.cap_minor_units > 0,
                "mandate_cap_required",
            )
            assert view.cap_minor_units is not None
            _need(
                (request.mandate_id, request.mandate_revision) == (view.mandate_id, view.revision),
                "stale_mandate_reference",
            )
            _need(request.owner_id == view.owner_id == ref.owner_ref, "mandate_owner_mismatch")
            _need(request.payer == view.payer == ref.payer_ref, "mandate_payer_mismatch")
            _need(
                (view.provider, view.account_id) == (ref.provider, ref.account_id),
                "mandate_account_mismatch",
            )
            _need(request.currency == view.cap_currency, "mandate_currency_mismatch")
            _need(
                type(request.max_minor_units) is int
                and 0 < request.max_minor_units <= view.cap_minor_units - view.consumed_minor_units,
                "mandate_cap_exceeded",
            )
            existing = state.jobs.get(request.job_ref)
            body = {
                "job_ref": request.job_ref,
                "reference_id": request.reference_id,
                "mandate_id": request.mandate_id,
                "mandate_revision": request.mandate_revision,
                "currency": request.currency,
                "max_minor_units": request.max_minor_units,
            }
            if existing is not None:
                _need(existing.request == body and not existing.fenced, "job_already_authorized")
            else:
                self._append(state, "dispatch_authorized", body)
        return DispatchAuthorization(
            request.job_ref,
            request.reference_id,
            request.mandate_id,
            request.mandate_revision,
            request.currency,
            request.max_minor_units,
        )

    def record_outcome(
        self, job_ref: str, outcome: JobOutcome, observed_by: ObservedBy, evidence_ref: str
    ) -> JobStatus:
        """Record what was observed for a job. ``unknown`` is a valid record; nothing is assumed."""
        _need(isinstance(outcome, JobOutcome), "invalid_job_outcome")
        _need(isinstance(observed_by, ObservedBy), "invalid_job_outcome")
        _ref(evidence_ref)
        _need(
            outcome is JobOutcome.UNKNOWN or observed_by is not ObservedBy.NOT_OBSERVABLE,
            "observed_outcome_requires_observation",
        )
        with self._locked():
            state = self._fold()
            job = state.jobs.get(job_ref)
            _need(job is not None, "job_unknown")
            assert job is not None
            final = job.outcome is not None and not job.in_flight
            if final:
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

    def job_status(self, job_ref: str) -> JobStatus:
        job = self._fold().jobs.get(job_ref)
        _need(job is not None, "job_unknown")
        assert job is not None
        return JobStatus(
            job_ref, job.reference_id, job.fenced, job.outcome, job.observed_by, job.evidence_ref
        )


# --------------------------------------------------------------------------- resolver
class SecretValue:
    """A credential value handed to the caller. Redacted in repr/str; cannot be pickled."""

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
    """Reads the value from the locator at the moment of use. Never logs, keeps or persists it."""

    def __init__(self, ledger: CredentialLedger, *, expected_uid: int) -> None:
        self.ledger = ledger
        self.expected_uid = expected_uid

    def __repr__(self) -> str:
        return f"CredentialResolver(expected_uid={self.expected_uid})"

    def resolve(self, reference_id: str) -> SecretValue:
        _ident(reference_id)
        state = self.ledger._fold()
        ref = state.references.get(reference_id)
        _need(ref is not None, "reference_unknown")
        assert ref is not None
        status = state.status[reference_id]
        _need(status != "revoked", "reference_revoked")
        _need(status != "retired", "reference_retired")
        _need(status == "active", "reference_not_active")
        now = self.ledger._now()
        _need(ref.issued_at <= now < ref.not_after, "reference_outside_window")
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
    """``probe``: resolve one reference in this fresh process and report only the outcome."""
    parser = argparse.ArgumentParser(prog="provider-credential")
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe")
    probe.add_argument("--ledger", required=True)
    probe.add_argument("--reference", required=True)
    probe.add_argument("--expected-uid", type=int, required=True)
    args = parser.parse_args(argv)
    shown: str | None = args.reference
    try:
        resolver = CredentialResolver(CredentialLedger(args.ledger), expected_uid=args.expected_uid)
        resolver.resolve(args.reference)
        result = "resolved_locally"
    except CredentialError as exc:
        result = exc.code
        if suspected_value(args.reference) is not None:
            shown = None
    except OSError:
        result = "probe_unavailable"
    print(
        json.dumps(
            {
                "reference": shown,
                "result": result,
                "provider_side_refusal": PROVIDER_SIDE,
            },
            sort_keys=True,
        )
    )
    return 0 if result == "resolved_locally" else 2


if __name__ == "__main__":
    sys.exit(main())
