"""Verify issuer-signed pilot authority records. Verify only: this module cannot sign.

Governing rules: OPE-566 "Governing contract — owner ruling" as amended by "Amendment —
owner rulings 2026-11-27, second round", the PR #209 review decisions and the decision
"customer keys are purpose-scoped" (OPE-566 comment 68da165c).

Profile: the authority-journal profile, unchanged. ES256 over P-256, RFC 8785 canonical
JSON, signed bytes ``canonical({"purpose": p, "payload": payload})`` pre-hashed with
SHA-256, signature ``base64url_unpadded_r_s_64``. The canonicalisation, transcript, digest,
DER and strict JSON primitives are imported from ``opentour_journal`` so the two verifiers
cannot drift. A record is ``{"payload": {...}, "signatures": [...]}``: one signature per
required signer role, every signature over the same transcript. Low-S is not enforced:
hardware-token issuers may emit high-S signatures, and normalisation is an OPE-217
decision. Every de-duplication below is therefore keyed on the payload digest, never on
signature bytes, and consumers of verified records must likewise de-duplicate on the
payload digest, never on signature bytes.

Refusal contract: every public entry point either returns or raises ``PilotRecordRefused``
with a reason code. It never raises anything else and never treats an unexpected error as
acceptance: any unexpected internal exception becomes ``verifier_internal_error``.

Trust chain:

1. Kit configuration pins only the operator registry-root public key(s). The file path is
   fixed by the kit layout (``<kit>/trust/pilot-registry-root.json``) and its bytes are
   pinned by ``APPROVED_ROOT_PIN_SHA256`` in this module, which ships inside the verified
   kit. Neither is a caller argument. No root is approved yet, so loading refuses with
   ``root_pin_not_approved`` until the key ceremony has run and a kit pins its result.
2. The operator publishes an issuer registry, signed by a pinned root key under
   ``opentour.pilot.issuer-registry.v1``. Each entry is one hardware-token issuer key (or its
   sealed backup): an opaque registry key id, one issuer role, the purposes it may sign and
   its public-key DER pinned by SHA-256. A key id or public key maps to exactly one role
   across the whole registry, and a root key is never a registry or customer key.
3. Customer-owner keys never come from the registry. Each engagement's customer key is
   anchored by an enrolment record signed by that customer key and co-signed by the
   operator custodian, pinned once at engagement start. Only a rotation record signed by the
   currently anchored (previous) customer key and by the new key it installs (proof of
   possession) can replace it; the operator does not co-sign. No registry revision, however
   validly signed, can change an anchor or introduce a customer-owner key.
4. A customer key is purpose-scoped: it signs only the consent purposes its customer enrolled
   it for (see Consent scope below).

Required signer roles per purpose (``REQUIRED_ROLES``): customer consent records
(engagement grant, spend mandate, routing policy, workflow-index publication) need the
customer owner and the operator custodian; a verdict needs the independent evaluator; an
audit receipt needs the benchmark auditor; a registry needs the operator root; a customer
key rotation needs the previous customer key and the new key; a consent-scope update needs
the anchored customer key and the operator custodian. A missing, duplicated or unexpected
role, one key signing under two roles, or a key signing outside its own role or purposes is
refused.

Consent scope (OPE-566 68da165c). A customer key may sit behind automation, such as an
approval signer, so a new consent purpose is an explicit customer opt-in, never inherited:

* The enrolment payload carries ``consent_purposes``: an explicit, non-empty, sorted list
  without duplicates, of exact ``str`` values, each in ``CONSENT_PURPOSES``. The enrolment,
  rotation and scope purposes are never in it. An enrolment without the field is refused
  with ``customer_consent_scope_missing`` and is never defaulted to every purpose, at pin
  time and when an already stored chain is re-verified; an invalid list is refused with
  ``customer_consent_scope_invalid``.
* Resolution: the anchored key's purposes are its anchored ``consent_purposes`` intersected
  with the current ``CONSENT_PURPOSES``, so a retired purpose drops out without making the
  anchor unverifiable. A record signed under a purpose outside that scope is refused with
  ``customer_purpose_not_enrolled``, after it has been authenticated. For an anchor that is
  never rotated, a retired purpose that a later release reinstates is restored under the
  same rule: reinstatement is an operator table change made in a reviewed release, while a
  purpose new to the table still needs the customer's opt-in.
* A rotation's successor inherits the previous anchor's effective scope, unless the
  rotation states a new ``consent_purposes``, validated like an enrolment's. Inheritance is
  re-derived from the signed chain on every read, and the table at rotation time is not
  recorded, so the verifier can carry forward only a scope that is signed: when the
  effective scope differs from the signed one (a retired purpose, or a cap from an expired
  pending update), an inheriting rotation is refused with
  ``customer_scope_restatement_required`` and must state its scope. A retired purpose thus
  never comes back for the new key, even if it is reinstated.
* ``update_customer_scope`` accepts a scope update under ``CONSENT_SCOPE_PURPOSE``: the
  full new list, which may widen or narrow the scope, at the next anchor revision,
  co-signed by the currently anchored customer key and a current ``operator_custodian``
  key permitted for that purpose. It is an anchor-chain entry,
  not a consent purpose: ``verify`` never accepts it. It follows the rotation rules: the same
  lock, the anti-rollback floor, pending-tail handling and re-verification on every read.
  Re-presenting the head update, keyed on its payload digest, is a no-op once it
  authenticates now; after its custodian leaves the current registry, or once it expires,
  the re-presentation is refused, while the accepted entry still re-verifies.
* A customer signature names the key's signing label in ``key_registry_revision``: the
  anchor revision that installed the key (its enrolment, or the rotation that installed
  it). A scope update advances the head anchor revision but never the label, so the
  customer's existing signatures stay valid; a rotation installs a new key and label. Any
  other label, the head revision included once they differ, is refused with
  ``customer_key_not_anchored``. The effective scope is read from the head on every
  verification, so narrowing applies at once to every record, however old: a record whose
  purpose was narrowed out is refused with ``customer_purpose_not_enrolled``. A rotation or
  scope update names the head revision as ``previous_anchor_revision``.

Anti-rollback state: ``TrustStateStore`` keeps named monotonic records in a strict,
versioned format (``opentour.pilot.trust-state.v1``). This module uses three namespaces:

* ``registry_revision``: the highest verified registry revision and its payload digest.
  Every verification re-reads it, so a verifier built from an older registry refuses once a
  newer one has been verified (``registry_superseded``).
* ``customer_anchor:<engagement>``: the anchor's evidence chain, the signed enrolment and
  every signed rotation and scope update, with a write generation as its sequence. Each
  entry after the enrolment is discriminated by its signed purpose; any other purpose, or a
  malformed entry, does not verify. The current customer key and scope are derived by
  re-verifying the chain on every read; no stored key or scope field is ever trusted. A
  chain holds at most ``MAX_ANCHOR_CHAIN`` entries of either kind.
* ``customer_anchor_revision:<engagement>``: the highest anchor revision this worker has
  accepted, with the payload digest at that revision. A chain shorter than it is refused.

Chain entries up to the anchor revision floor were accepted by this worker and are
re-verified for signatures and engagement binding only. Entries beyond the floor are
*pending* (for example a rotation or scope update whose floor write was interrupted): they
count only while they also pass the time check now, the ``MAX_VALIDITY`` ceiling included,
and a pending scope update needs a current custodian at its exact registry revision.
Otherwise the anchor is derived at the floor and the pending tail does not count, except
that revocation fails closed: pending scope updates by the floor key that pass every check
but expiry still restrict, so the effective scope is the floor scope ∩ the pending scope. A
pure narrowing advances the head and becomes an accepted, signed entry when the floor is
next rolled forward; the first update that widens only caps the scope at read time and is
never accepted, so its widening never takes effect (see ``_restrict``). A pending rotation,
or any other failure, ends that walk. Any locked operation on the engagement (a pin, a
rotation or a scope update) first rolls the floor forward over a pending tail that is
still inside its window, or over the pure narrowings above, after which they are accepted
and no longer expire.

The enrolment co-signature is re-verified against the operator custodian key named by key
id, accepted only if a current registry entry for that id carries the enrolment purpose (at
any registry revision) or the id is listed in the registry's verify-only
``former_custodian_keys``. A key id maps to one public key and one role across the
registry, so the key id fixes the pinned public-key digest. A former custodian key verifies
existing enrolments and accepted scope updates only (below): never a new pin, never a new
or pending scope update, never any other purpose. The one permitted overlap is a
still-current custodian key listed there with the same id and public key, so its enrolment
or scope purpose can be removed without bricking its anchors. Dropping a key from that
list (and from the enrolment purpose) deliberately invalidates every anchor it co-signed;
that is the intended response to a compromised custodian key, and those engagements need a
supervised re-enrolment (see Recovery).

An accepted scope update's co-signature (an entry at or below the anchor floor) is
re-verified the same way by key id: a current registry entry for that id carrying the scope
purpose (at any registry revision), or the id in ``former_custodian_keys``. Retiring a
custodian into that list therefore keeps the anchors whose scope updates it co-signed. A
former custodian key never authorizes a new scope update and never carries a pending one,
which needs a current custodian at its exact registry revision. A scope update also needs
the anchored customer key's signature, so a former custodian key cannot widen a scope on its
own. Dropping a key from both lists still invalidates every anchor whose enrolment or scope
update it co-signed.

Custody of retired custodian keys: anyone holding a former custodian key plus write access
to the anti-rollback location can substitute an anchor, by writing a chain whose enrolment
that key co-signed. Minting a consent record still needs a current custodian key permitted
for its purpose. Retired custodian tokens must therefore be destroyed, or kept as tightly as
current ones; the OPE-582 key ceremony covers this.

Further namespaces, such as revocation epochs or withdrawn mandate ids, use the same format
with their own forward-only rule. The location is fixed worker-host layout configuration
(``ANTI_ROLLBACK_DIRECTORY``, the ``/var/lib/opentour-antirollback`` StateDirectory reserved
by OPE-577 outside the snapshot volume), never a caller argument and never the worker data
directory.

Limits, stated plainly. This is local state. A party who can rewrite the anti-rollback
location can reset the registry floor; registry replay is then still bounded by the
kit-pinned ``minimum_registry_revision`` and by the registry's signed ``not_after``. Those
bounds apply to the registry only, not to anchors. The same local-state limit applies to
anchors: an older copy of the chain alone is refused, but restoring older copies of both
the chain and its revision floor undoes a rotation. Without the operator custodian key, no
writer can forge or substitute an anchor: the chain is re-verified on every read. But on a
host the operator controls, an operator holding the custodian key can delete both anchor
records for an engagement and enrol a customer key it holds. No worker-side code can
prevent that. The defence is the customer's own record of what they signed, plus the
out-of-band key fingerprint confirmation (OPE-583). A concurrent pin can briefly cause a
false refusal on a lock-free read; that fails closed and is accepted.

Recovery:

* A missing registry floor recovers when a verifier is next constructed from a valid
  registry. That re-admits an older registry that is still within its signed window and at
  or above the kit minimum, so treat a missing floor as an incident.
* A corrupted (unreadable) registry floor fails closed; the only recovery is for the
  operator to delete the file, which then behaves as a missing floor.
* A deleted anchor revision floor after a rotation fails closed with
  ``customer_anchor_inconsistent``, and so does an anchor whose custodian key was dropped.
  Re-presenting the original enrolment does not restore it: any stored chain longer than
  the enrolment blocks a fresh pin, whatever its pending tail derives to. Recovery is a
  supervised re-enrolment: delete both anchor records and pin a new enrolment, with the
  customer confirming the key fingerprint out of band per OPE-583.
* An interrupted rotation (chain written, floor write lost) counts while inside its window,
  and the next locked operation on that engagement rolls the floor forward. Remaining risk:
  if nothing touches the engagement's anchor under the lock before the rotation expires, the
  anchor reverts to the key the customer rotated away from. Re-run the rotation with a fresh
  record signed by both keys. An interrupted scope update counts the same way while inside
  its window. After it expires a narrowing still holds, and the next locked operation
  records it as accepted; a widening does not take effect, so re-run it with a fresh
  co-signed update. An update that both widens and narrows caps the scope at their
  intersection only while it stays stored: a rotation must then restate its scope, and a
  fresh co-signed update replaces the cap.
* An anchor at ``MAX_ANCHOR_CHAIN`` entries refuses every further entry
  (``customer_scope_limit``, ``customer_rotation_limit``), so its customer can neither
  narrow nor revoke a purpose nor rotate the key. Both need a supervised re-enrolment:
  delete both anchor records and pin a new enrolment that states the intended scope, with
  the customer confirming the key fingerprint out of band per OPE-583. Until then, records
  keep verifying under the anchored scope, so treat an anchor at the cap that needs a
  narrowing as an incident.
* Retiring a custodian key: add it to ``former_custodian_keys`` so the enrolments and
  accepted scope updates it co-signed keep verifying. A scope update it co-signed that was
  still pending (its floor write lost) is ignored once it is retired, and the anchor reverts
  to the earlier scope; re-run it with a current custodian.

Time: every signed document carries issuer-signed ``issued_at`` and ``not_after``. The local
clock only shortens validity: refuse after ``not_after`` or more than ``ISSUED_AT_SKEW``
before ``issued_at``. A window longer than ``MAX_VALIDITY`` is refused. No argument supplies
time, and the registry's own window is re-checked on every verification. Accepted chain
entries are re-verified for signatures only, because their time was checked when they were
accepted; pending entries are time-checked on every read.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import re
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from opentour_journal.crypto import canonical, raw_to_der, sha256, transcript
from opentour_journal.models import (
    Id,
    JournalError,
    Revision,
    Sha256,
    Timestamp,
    WireModel,
    strict_json,
)
from pydantic import Field, TypeAdapter, ValidationError

GRANT_PURPOSE: Final = "opentour.pilot.engagement-grant.v1"
SPEND_MANDATE_PURPOSE: Final = "opentour.pilot.spend-mandate.v1"
ROUTING_POLICY_PURPOSE: Final = "opentour.pilot.routing-policy.v1"
# Customer-owner consent to publish private workflow display metadata and optional directory
# viewers (OPE-547). Proposal actions are governed separately and never derive from it.
WORKFLOW_INDEX_PUBLICATION_PURPOSE: Final = "opentour.pilot.workflow-index-publication.v1"
VERDICT_PURPOSE: Final = "opentour.pilot.independent-verdict.v1"
BENCHMARK_AUDIT_PURPOSE: Final = "opentour.pilot.benchmark-audit.v1"
REGISTRY_PURPOSE: Final = "opentour.pilot.issuer-registry.v1"
ENROLMENT_PURPOSE: Final = "opentour.pilot.customer-enrolment.v1"
ROTATION_PURPOSE: Final = "opentour.pilot.customer-key-rotation.v1"
# Changes an anchored customer key's consent scope without re-keying (OPE-566 68da165c). An
# anchor-chain entry only: not a consent purpose and never accepted by ``verify``.
CONSENT_SCOPE_PURPOSE: Final = "opentour.pilot.customer-consent-scope.v1"

CUSTOMER_OWNER: Final = "customer_owner"
# The key a rotation installs, proving possession by signing the same rotation transcript.
CUSTOMER_OWNER_SUCCESSOR: Final = "customer_owner_successor"
OPERATOR_CUSTODIAN: Final = "operator_custodian"
INDEPENDENT_EVALUATOR: Final = "independent_evaluator"
BENCHMARK_AUDITOR: Final = "benchmark_auditor"
OPERATOR_ROOT: Final = "operator_root"

CONSENT_PURPOSES: Final = frozenset(
    {
        GRANT_PURPOSE,
        SPEND_MANDATE_PURPOSE,
        ROUTING_POLICY_PURPOSE,
        WORKFLOW_INDEX_PUBLICATION_PURPOSE,
    }
)
RECORD_PURPOSES: Final = CONSENT_PURPOSES | {VERDICT_PURPOSE, BENCHMARK_AUDIT_PURPOSE}
REQUIRED_ROLES: Final[Mapping[str, frozenset[str]]] = {
    GRANT_PURPOSE: frozenset({CUSTOMER_OWNER, OPERATOR_CUSTODIAN}),
    SPEND_MANDATE_PURPOSE: frozenset({CUSTOMER_OWNER, OPERATOR_CUSTODIAN}),
    ROUTING_POLICY_PURPOSE: frozenset({CUSTOMER_OWNER, OPERATOR_CUSTODIAN}),
    WORKFLOW_INDEX_PUBLICATION_PURPOSE: frozenset({CUSTOMER_OWNER, OPERATOR_CUSTODIAN}),
    VERDICT_PURPOSE: frozenset({INDEPENDENT_EVALUATOR}),
    BENCHMARK_AUDIT_PURPOSE: frozenset({BENCHMARK_AUDITOR}),
    REGISTRY_PURPOSE: frozenset({OPERATOR_ROOT}),
    ENROLMENT_PURPOSE: frozenset({CUSTOMER_OWNER, OPERATOR_CUSTODIAN}),
    # Previous key and new key both sign; the operator does not co-sign a rotation.
    ROTATION_PURPOSE: frozenset({CUSTOMER_OWNER, CUSTOMER_OWNER_SUCCESSOR}),
    CONSENT_SCOPE_PURPOSE: frozenset({CUSTOMER_OWNER, OPERATOR_CUSTODIAN}),
}
# Anchor-chain entries after the enrolment, discriminated by their signed purpose. Any other
# purpose (or a malformed entry) makes an accepted chain unverifiable.
_CHAIN_ENTRY_PURPOSES: Final = frozenset({ROTATION_PURPOSE, CONSENT_SCOPE_PURPOSE})
# Purposes each registry role may be granted. Customer-owner keys are never registry keys,
# and the root role only exists in the kit-pinned root configuration.
REGISTRY_ROLES: Final[Mapping[str, frozenset[str]]] = {
    OPERATOR_CUSTODIAN: frozenset(
        {
            GRANT_PURPOSE,
            SPEND_MANDATE_PURPOSE,
            ROUTING_POLICY_PURPOSE,
            WORKFLOW_INDEX_PUBLICATION_PURPOSE,
            ENROLMENT_PURPOSE,
            CONSENT_SCOPE_PURPOSE,
        }
    ),
    INDEPENDENT_EVALUATOR: frozenset({VERDICT_PURPOSE}),
    BENCHMARK_AUDITOR: frozenset({BENCHMARK_AUDIT_PURPOSE}),
}
ROOT_ROLES: Final[Mapping[str, frozenset[str]]] = {OPERATOR_ROOT: frozenset({REGISTRY_PURPOSE})}

PROFILE_VERSION: Final = "1.0"
ALGORITHM: Final = "ES256"
ENCODING: Final = "base64url_unpadded_r_s_64"
ISSUED_AT_SKEW: Final = timedelta(seconds=60)
# Global ceiling on any signed validity window; tighter per-purpose windows come later.
MAX_VALIDITY: Final = timedelta(days=366)
# Opaque registry identifiers (hardware-token keys), never provider resource names.
KEY_ID_PATTERN: Final = r"[a-z0-9](?:[a-z0-9._-]{1,126}[a-z0-9])"
MAX_SIGNATURES: Final = 4
MAX_CONFIG_BYTES: Final = 65536
MAX_STATE_BYTES: Final = 1048576
MAX_ANCHOR_CHAIN: Final = 64
MAX_FORMER_KEYS: Final = 64
TRUST_STATE_FORMAT: Final = "opentour.pilot.trust-state.v1"
RECORDS_NAME: Final = "records"
LOCK_NAME: Final = "pilot-trust-state.lock"
# ``namespace`` or ``namespace:subject``; subjects are opaque ids such as engagement ids.
RECORD_KEY_PATTERN: Final = r"[a-z][a-z0-9_]{0,63}(?::[^\x00-\x1f]{1,512})?"
REGISTRY_REVISION_KEY: Final = "registry_revision"

# Fixed by the kit layout: <kit>/opentour-*.pyz/opentour_cell/pilot/ in a kit and
# services/cell-runtime/src/opentour_cell/pilot/ in the repository (kit root == CELL root).
ROOT_PIN_PATH = Path(os.path.abspath(__file__)).parents[3] / "trust" / "pilot-registry-root.json"
# SHA-256 of the approved root pin file. None until the operator root key ceremony has run.
APPROVED_ROOT_PIN_SHA256: str | None = None
# Worker-host layout (OPE-577): a dedicated StateDirectory owned by opentour-worker, mode
# 0700, outside the snapshot volume and unreachable by the health unit. Never the data
# directory and never a caller argument.
ANTI_ROLLBACK_DIRECTORY: Path | None = Path("/var/lib/opentour-antirollback")

_ENVELOPE_FIELDS: Final = frozenset({"payload", "signatures"})
_SIGNATURE_FIELDS: Final = frozenset(
    {
        "profile_version",
        "algorithm",
        "encoding",
        "purpose",
        "signer_role",
        "key_id",
        "key_registry_revision",
        "payload_sha256",
        "transcript_sha256",
        "signature",
    }
)
_TIMESTAMP: Final = TypeAdapter(Timestamp)
_ID: Final = TypeAdapter(Id)
_Der = Annotated[str, Field(strict=True, min_length=1, max_length=4096)]


class PilotRecordRefused(Exception):
    """Safe refusal: carries a reason code only, never a wire value or key material."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _refusing[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Public entry points refuse with a code; they never raise anything else."""

    @wraps(function)
    def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except PilotRecordRefused:
            raise
        except Exception:
            raise PilotRecordRefused("verifier_internal_error") from None

    return guarded


class _KeyEntry(WireModel):
    key_id: Id
    registry_revision: Id
    role: Id
    public_key_der: _Der
    public_key_sha256: Sha256
    purposes: list[Id]


class _RootPin(WireModel):
    profile_version: Literal["1.0"]
    kind: Literal["opentour.pilot.registry-root.v1"]
    registry_id: Id
    minimum_registry_revision: Revision
    root_keys: Annotated[list[_KeyEntry], Field(min_length=1)]


class _CustomerKey(WireModel):
    key_id: Id
    public_key_der: _Der
    public_key_sha256: Sha256


class _RegistryPayload(WireModel):
    profile_version: Literal["1.0"]
    registry_id: Id
    revision: Revision
    issued_at: Timestamp
    not_after: Timestamp
    keys: Annotated[list[_KeyEntry], Field(min_length=1)]
    # Verify-only: re-verify the co-signature of existing enrolments and of accepted
    # scope updates, nothing else.
    former_custodian_keys: Annotated[
        list[_CustomerKey], Field(default_factory=list, max_length=MAX_FORMER_KEYS)
    ]


class _EnrolmentPayload(WireModel):
    profile_version: Literal["1.0"]
    engagement_id: Id
    anchor_revision: Revision
    customer_key: _CustomerKey
    # Required; validated by ``_consent_scope`` from the raw payload so that an absent field
    # (customer_consent_scope_missing) is told apart from an invalid one.
    consent_purposes: Any = None
    issued_at: Timestamp
    not_after: Timestamp


class _RotationPayload(WireModel):
    profile_version: Literal["1.0"]
    engagement_id: Id
    previous_key_id: Id
    previous_anchor_revision: Revision
    anchor_revision: Revision
    customer_key: _CustomerKey
    # Optional: absent means the successor inherits the previous anchor's scope exactly.
    consent_purposes: Any = None
    issued_at: Timestamp
    not_after: Timestamp


class _ScopePayload(WireModel):
    profile_version: Literal["1.0"]
    engagement_id: Id
    customer_key_id: Id
    previous_anchor_revision: Revision
    anchor_revision: Revision
    # Required: the full new scope, validated like an enrolment's.
    consent_purposes: Any = None
    issued_at: Timestamp
    not_after: Timestamp


class _Signature(WireModel):
    profile_version: Id
    algorithm: Id
    encoding: Id
    purpose: Id
    signer_role: Id
    key_id: Id
    key_registry_revision: Id
    payload_sha256: Sha256
    transcript_sha256: Sha256
    signature: Annotated[str, Field(strict=True, max_length=256)]


@dataclass(frozen=True)
class PilotTrustedKey:
    """Mirrors ``opentour_journal.crypto.TrustedKey`` with a pilot issuer role."""

    key_id: str
    registry_revision: str
    role: str
    public_key_sha256: str
    purposes: frozenset[str]
    public_key: ec.EllipticCurvePublicKey


@dataclass(frozen=True)
class CustomerAnchor:
    engagement_id: str
    key_id: str
    anchor_revision: int
    public_key_sha256: str
    # The effective scope: the anchored ``consent_purposes`` that are still current.
    consent_purposes: frozenset[str]
    # The customer key's signing label: the anchor revision that installed it (its
    # enrolment or rotation). Every customer signature names it in key_registry_revision.
    key_revision: int


@dataclass(frozen=True)
class _AnchorHead:
    """The head of a re-verified anchor chain.

    ``key`` is the anchored customer key, labelled with the anchor revision that installed
    it (the label every customer signature must name). ``revision`` is the head anchor
    revision, which a scope update advances without changing the label. ``scope`` is the
    signed scope as anchored; a purpose retired from ``CONSENT_PURPOSES`` since then drops
    out of ``effective``. ``ceiling`` is a read-time cap from an expired pending scope update
    that is not a pure narrowing (``_restrict``); it is never stored.
    """

    key: PilotTrustedKey
    revision: int
    scope: frozenset[str]
    ceiling: frozenset[str] | None = None

    @property
    def effective(self) -> frozenset[str]:
        capped = self.scope if self.ceiling is None else self.scope & self.ceiling
        return capped & CONSENT_PURPOSES

    def anchor(self, engagement_id: str) -> CustomerAnchor:
        return CustomerAnchor(
            engagement_id,
            self.key.key_id,
            self.revision,
            self.key.public_key_sha256,
            self.effective,
            int(self.key.registry_revision),
        )


@dataclass(frozen=True)
class _Registry:
    revision: int
    payload_sha256: str
    issued_at: datetime
    not_after: datetime
    keys: Mapping[tuple[str, str], PilotTrustedKey]
    root_digests: frozenset[str]
    former: Mapping[str, PilotTrustedKey]


Resolver = Callable[[_Signature], PilotTrustedKey]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _public_key(key_id: str, der_b64: str, pinned_sha256: str) -> ec.EllipticCurvePublicKey:
    # Mirrors TrustedKey.public_key with distinct reason codes; key ids are opaque ids.
    if not re.fullmatch(KEY_ID_PATTERN, key_id):
        raise PilotRecordRefused("trust_key_id_invalid")
    try:
        der = base64.b64decode(der_b64, validate=True)
        if base64.b64encode(der).decode("ascii") != der_b64:
            raise ValueError("noncanonical base64")
    except ValueError:
        raise PilotRecordRefused("trust_set_invalid") from None
    if sha256(der) != pinned_sha256:
        raise PilotRecordRefused("trust_key_digest_mismatch")
    try:
        key = serialization.load_der_public_key(der)
    except (TypeError, ValueError, UnsupportedAlgorithm):
        # Unsupported or unknown curves (brainpool, prime192v2, SM2 ...) refuse, never raise.
        raise PilotRecordRefused("trust_key_curve_invalid") from None
    # Only P-256, checked explicitly.
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise PilotRecordRefused("trust_key_curve_invalid")
    return key


def _trust_set(
    entries: list[_KeyEntry],
    roles: Mapping[str, frozenset[str]],
    reserved: Mapping[tuple[str, str], PilotTrustedKey] | None = None,
) -> dict[tuple[str, str], PilotTrustedKey]:
    """Mirrors TrustPolicy.validate, plus one role per key id and per public key.

    ``reserved`` keys (the pinned roots) may not reappear in this set at all.
    """
    keys: dict[tuple[str, str], PilotTrustedKey] = {}
    role_by_id: dict[str, str] = {}
    digest_by_id: dict[str, str] = {}
    by_digest: dict[str, tuple[str, str]] = {}
    for pinned in (reserved or {}).values():
        role_by_id[pinned.key_id] = pinned.role
        digest_by_id[pinned.key_id] = pinned.public_key_sha256
        by_digest[pinned.public_key_sha256] = (pinned.key_id, pinned.role)
    for entry in entries:
        identity = (entry.key_id, entry.registry_revision)
        if identity in keys:
            raise PilotRecordRefused("trust_key_duplicate")
        if entry.role not in roles:
            raise PilotRecordRefused("trust_key_role_invalid")
        purposes = frozenset(entry.purposes)
        if (
            not purposes
            or len(purposes) != len(entry.purposes)
            or not purposes <= roles[entry.role]
        ):
            raise PilotRecordRefused("trust_key_purposes_invalid")
        key = _public_key(entry.key_id, entry.public_key_der, entry.public_key_sha256)
        # One key, one role, one id, across every registry revision in the set.
        if role_by_id.get(entry.key_id, entry.role) != entry.role:
            raise PilotRecordRefused("trust_key_role_conflict")
        # A key id names one public key, so key id and pinned digest identify a key together.
        if digest_by_id.get(entry.key_id, entry.public_key_sha256) != entry.public_key_sha256:
            raise PilotRecordRefused("trust_key_duplicate")
        seen = by_digest.get(entry.public_key_sha256)
        if seen is not None and seen != (entry.key_id, entry.role):
            raise PilotRecordRefused(
                "trust_key_role_conflict" if seen[1] != entry.role else "trust_key_duplicate"
            )
        role_by_id[entry.key_id] = entry.role
        digest_by_id[entry.key_id] = entry.public_key_sha256
        by_digest[entry.public_key_sha256] = (entry.key_id, entry.role)
        keys[identity] = PilotTrustedKey(
            entry.key_id,
            entry.registry_revision,
            entry.role,
            entry.public_key_sha256,
            purposes,
            key,
        )
    return keys


def _former_custodians(
    entries: list[_CustomerKey], taken: list[PilotTrustedKey]
) -> dict[str, PilotTrustedKey]:
    """Verify-only former custodian keys, distinct from every current and root key.

    The single permitted overlap: a still-current custodian key listed with the same key id
    and public key, so its enrolment or scope purpose can be removed without bricking its
    anchors. These keys re-verify accepted enrolments and scope updates only.
    """
    former: dict[str, PilotTrustedKey] = {}
    taken_ids = {key.key_id for key in taken}
    taken_digests = {key.public_key_sha256 for key in taken}
    narrowed = {
        (key.key_id, key.public_key_sha256) for key in taken if key.role == OPERATOR_CUSTODIAN
    }
    for entry in entries:
        if entry.key_id in former or any(
            key.public_key_sha256 == entry.public_key_sha256 for key in former.values()
        ):
            raise PilotRecordRefused("trust_key_duplicate")
        # No role reuse: a former custodian key is never also a current key of another role,
        # a root key, or a different public key under a current key id.
        if (entry.key_id, entry.public_key_sha256) not in narrowed and (
            entry.key_id in taken_ids or entry.public_key_sha256 in taken_digests
        ):
            raise PilotRecordRefused("trust_key_role_conflict")
        public_key = _public_key(entry.key_id, entry.public_key_der, entry.public_key_sha256)
        former[entry.key_id] = PilotTrustedKey(
            entry.key_id,
            "former",
            OPERATOR_CUSTODIAN,
            entry.public_key_sha256,
            frozenset({ENROLMENT_PURPOSE}),
            public_key,
        )
    return former


def _load_root_pin() -> tuple[_RootPin, dict[tuple[str, str], PilotTrustedKey]]:
    pinned = APPROVED_ROOT_PIN_SHA256
    if pinned is None:
        raise PilotRecordRefused("root_pin_not_approved")
    try:
        with ROOT_PIN_PATH.open("rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        raise PilotRecordRefused("root_pin_unavailable") from None
    if len(data) > MAX_CONFIG_BYTES:
        raise PilotRecordRefused("root_pin_invalid")
    if sha256(data) != pinned:
        raise PilotRecordRefused("root_pin_digest_mismatch")
    try:
        pin = _RootPin.model_validate(strict_json(data, limit=MAX_CONFIG_BYTES))
    except (JournalError, ValidationError):
        raise PilotRecordRefused("root_pin_invalid") from None
    return pin, _trust_set(pin.root_keys, ROOT_ROLES)


def _instant(value: object, subject: str) -> datetime:
    try:
        return datetime.fromisoformat(_TIMESTAMP.validate_python(value))
    except (ValidationError, ValueError):
        raise PilotRecordRefused(f"{subject}_validity_window_invalid") from None


def _check_window(issued_at: datetime, not_after: datetime, subject: str) -> None:
    # The local clock may only shorten the issuer-signed window, never extend it.
    if not_after <= issued_at:
        raise PilotRecordRefused(f"{subject}_validity_window_invalid")
    if not_after - issued_at > MAX_VALIDITY:
        raise PilotRecordRefused(f"{subject}_validity_window_too_long")
    now = _utc_now()
    if now > not_after:
        raise PilotRecordRefused(f"{subject}_expired")
    if now < issued_at - ISSUED_AT_SKEW:
        raise PilotRecordRefused(f"{subject}_not_yet_valid")


def _open(record: object, subject: str) -> tuple[dict[str, object], list[_Signature]]:
    """Strict structure only. Nothing returned here is authenticated yet."""
    if type(record) is not bytes:
        raise PilotRecordRefused(f"{subject}_schema_invalid")
    try:
        value = strict_json(record)
        canonical_record = canonical(value)
    except JournalError:
        raise PilotRecordRefused(f"{subject}_schema_invalid") from None
    if not isinstance(value, dict):
        raise PilotRecordRefused(f"{subject}_schema_invalid")
    # One accepted byte representation: no whitespace, key-order or escape malleability.
    if canonical_record != record:
        raise PilotRecordRefused(f"{subject}_not_canonical")
    if not set(value) <= _ENVELOPE_FIELDS:
        raise PilotRecordRefused(f"{subject}_field_forbidden")
    payload, raw = value.get("payload"), value.get("signatures")
    if (
        not isinstance(payload, dict)
        or not isinstance(raw, list)
        or not 1 <= len(raw) <= MAX_SIGNATURES
        or not all(isinstance(item, dict) for item in raw)
    ):
        raise PilotRecordRefused(f"{subject}_schema_invalid")
    signatures = []
    for item in raw:
        if not set(item) <= _SIGNATURE_FIELDS:
            raise PilotRecordRefused(f"{subject}_field_forbidden")
        try:
            signatures.append(_Signature.model_validate(item))
        except ValidationError:
            raise PilotRecordRefused(f"{subject}_schema_invalid") from None
    return payload, signatures


def _payload_sha256(payload: Mapping[str, object]) -> str:
    return sha256(canonical(dict(payload)))


def _consent_scope(value: object, *, fresh: bool) -> frozenset[str]:
    """A signed ``consent_purposes`` list: explicit, non-empty, sorted, no duplicates, exact
    ``str`` values.

    ``fresh`` (a record presented now) also requires every value to be a current consent
    purpose, which excludes the enrolment, rotation and scope purposes themselves. A
    re-verified chain entry was checked when it was accepted, so a purpose retired since then
    drops out at resolution (``_AnchorHead.effective``) instead of bricking the anchor.
    """
    if type(value) is not list or not value or any(type(item) is not str for item in value):
        raise PilotRecordRefused("customer_consent_scope_invalid")
    # Strictly increasing: sorted and without duplicates.
    if any(later <= earlier for earlier, later in zip(value, value[1:], strict=False)):
        raise PilotRecordRefused("customer_consent_scope_invalid")
    scope = frozenset(value)
    if fresh and not scope <= CONSENT_PURPOSES:
        raise PilotRecordRefused("customer_consent_scope_invalid")
    return scope


def _scope_of(
    payload: Mapping[str, object], *, fresh: bool, inherit: frozenset[str] | None = None
) -> frozenset[str]:
    # Absent, never defaulted to every purpose; only a rotation may inherit.
    if "consent_purposes" not in payload:
        if inherit is None:
            raise PilotRecordRefused("customer_consent_scope_missing")
        return inherit
    return _consent_scope(payload["consent_purposes"], fresh=fresh)


def _entry_purpose(item: object) -> str:
    """The signed purpose of a chain entry after the enrolment; unknown kinds refuse.

    Only a discriminator: ``_authenticate`` then requires every signature to carry it, and
    the transcript binds it. An unknown kind makes an accepted chain
    ``customer_anchor_unverifiable`` and ends a pending tail.
    """
    signatures = item.get("signatures") if isinstance(item, dict) else None
    first = signatures[0] if isinstance(signatures, list) and signatures else None
    purpose = first.get("purpose") if isinstance(first, dict) else None
    if type(purpose) is not str or purpose not in _CHAIN_ENTRY_PURPOSES:
        raise PilotRecordRefused("customer_anchor_unverifiable")
    return purpose


def _authenticate(
    payload: dict[str, object],
    signatures: list[_Signature],
    purpose: str,
    resolve: Mapping[str, Resolver],
    subject: str,
    *,
    check_time: bool = True,
) -> None:
    for signature in signatures:
        if signature.profile_version != PROFILE_VERSION or signature.algorithm != ALGORITHM:
            raise PilotRecordRefused("profile_unsupported")
        if signature.encoding != ENCODING:
            raise PilotRecordRefused("signature_encoding_invalid")
        if signature.purpose != purpose:
            raise PilotRecordRefused("purpose_mismatch")
    roles = [signature.signer_role for signature in signatures]
    required = REQUIRED_ROLES[purpose]
    if len(set(roles)) != len(roles):
        raise PilotRecordRefused("signature_role_duplicate")
    if not set(roles) <= required:
        raise PilotRecordRefused("signature_role_unexpected")
    if set(roles) != required:
        raise PilotRecordRefused("signature_role_missing")
    # One key may not stand in for two roles, by key id (claimed) or by public key (resolved).
    if len({signature.key_id for signature in signatures}) != len(signatures):
        raise PilotRecordRefused("signature_key_reused")
    keys = [resolve[signature.signer_role](signature) for signature in signatures]
    if len({key.public_key_sha256 for key in keys}) != len(keys):
        raise PilotRecordRefused("signature_key_reused")
    digest = hashlib.sha256(transcript(purpose, payload)).digest()
    payload_sha256 = _payload_sha256(payload)
    for signature, key in zip(signatures, keys, strict=True):
        if key.role != signature.signer_role:
            raise PilotRecordRefused("key_role_not_permitted")
        if purpose not in key.purposes:
            raise PilotRecordRefused("key_purpose_not_permitted")
        try:
            der = raw_to_der(signature.signature)
        except JournalError:
            raise PilotRecordRefused("signature_encoding_invalid") from None
        if signature.payload_sha256 != payload_sha256:
            raise PilotRecordRefused("payload_digest_mismatch")
        if signature.transcript_sha256 != digest.hex():
            raise PilotRecordRefused("transcript_digest_mismatch")
        try:
            key.public_key.verify(der, digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        except InvalidSignature:
            raise PilotRecordRefused("signature_invalid") from None
    if check_time:
        _check_window(
            _instant(payload.get("issued_at"), subject),
            _instant(payload.get("not_after"), subject),
            subject,
        )


def _registry_resolver(keys: Mapping[tuple[str, str], PilotTrustedKey]) -> Resolver:
    def resolve(signature: _Signature) -> PilotTrustedKey:
        key = keys.get((signature.key_id, signature.key_registry_revision))
        if key is None:
            known = any(key_id == signature.key_id for key_id, _ in keys)
            raise PilotRecordRefused("key_revision_not_active" if known else "key_unknown")
        return key

    return resolve


def _customer_key(key: _CustomerKey, revision: int, purposes: frozenset[str]) -> PilotTrustedKey:
    public_key = _public_key(key.key_id, key.public_key_der, key.public_key_sha256)
    return PilotTrustedKey(
        key.key_id, str(revision), CUSTOMER_OWNER, key.public_key_sha256, purposes, public_key
    )


def _fixed_resolver(key: PilotTrustedKey, code: str = "customer_key_not_anchored") -> Resolver:
    def resolve(signature: _Signature) -> PilotTrustedKey:
        if (signature.key_id, signature.key_registry_revision) != (
            key.key_id,
            key.registry_revision,
        ):
            raise PilotRecordRefused(code)
        return key

    return resolve


def _write_all(fd: int, body: bytes) -> None:
    view = memoryview(body)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _write_atomic(directory: Path, name: str, body: bytes) -> None:
    temporary = directory / (name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        _write_all(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, directory / name)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _private_directory(directory: Path) -> bool:
    """A real directory owned by this uid, not writable by group or others."""
    try:
        status = os.lstat(directory)
    except OSError:
        return False
    return (
        stat.S_ISDIR(status.st_mode)
        and status.st_uid == os.geteuid()
        and not status.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


@dataclass(frozen=True)
class MonotonicRecord:
    """One named anti-rollback record: a namespaced key, a sequence and a JSON value.

    ``key`` is ``namespace`` or ``namespace:subject`` (for example ``registry_revision`` or
    ``customer_anchor:<engagement>``). ``sequence`` never decreases, and an equal sequence
    is accepted only with an identical value. A namespace adds its own stricter rule.
    """

    key: str
    sequence: int
    value: Mapping[str, object]


# A namespace rule sees the current record (or None) and the proposed one, and raises
# PilotRecordRefused with its own code to refuse. The store's generic rule always follows.
TransitionRule = Callable[[MonotonicRecord | None, MonotonicRecord], None]


class _RecordState(WireModel):
    format: Literal["opentour.pilot.trust-state.v1"]
    key: Annotated[str, Field(strict=True, min_length=1, max_length=600)]
    sequence: Revision
    value: dict[str, Any]


class TrustStateStore:
    """Anti-rollback state as named monotonic records, outside every worker snapshot.

    Located only by ``ANTI_ROLLBACK_DIRECTORY``, which must be a directory owned by this uid
    and not writable by group or others. One file per key under ``records/``, named by the
    key's SHA-256 and holding canonical JSON in the ``opentour.pilot.trust-state.v1``
    format; anything else fails closed. ``advance`` refuses unless ``locked`` is held: it
    reads the current record, applies the namespace rule and the generic forward-only rule,
    then writes atomically (temporary file, fsync, rename, directory fsync). New namespaces
    need no format change. The store enforces transitions only; the verifier authenticates
    the signed documents that justify them.
    """

    @_refusing
    def __init__(self) -> None:
        location = ANTI_ROLLBACK_DIRECTORY
        if location is None:
            raise PilotRecordRefused("anti_rollback_location_not_configured")
        directory = Path(location)
        if not directory.is_absolute() or not _private_directory(directory):
            raise PilotRecordRefused("anti_rollback_state_unavailable")
        self._directory = directory
        self._held = False

    @contextmanager
    def locked(self) -> Iterator[None]:
        try:
            lock = os.open(
                self._directory / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
        except OSError:
            raise PilotRecordRefused("anti_rollback_state_unavailable") from None
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._held = True
            yield
        except OSError:
            raise PilotRecordRefused("anti_rollback_state_unavailable") from None
        finally:
            self._held = False
            os.close(lock)

    @_refusing
    def read(self, key: str) -> MonotonicRecord | None:
        _require_record_key(key)
        path = self._directory / RECORDS_NAME / _record_name(key)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            raise PilotRecordRefused("trust_state_unreadable") from None
        try:
            with os.fdopen(fd, "rb") as handle:
                data = handle.read(MAX_STATE_BYTES + 1)
        except OSError:
            raise PilotRecordRefused("trust_state_unreadable") from None
        try:
            state = _RecordState.model_validate(strict_json(data, limit=MAX_STATE_BYTES))
            if state.key != key or canonical(state.wire()) != data:
                raise ValueError("record binding")
        except (JournalError, ValidationError, ValueError):
            raise PilotRecordRefused("trust_state_unreadable") from None
        return MonotonicRecord(state.key, int(state.sequence), state.value)

    @_refusing
    def advance(self, record: MonotonicRecord, rule: TransitionRule) -> bool:
        """Requires ``locked``. Return False when an identical record is already held."""
        if not self._held:
            raise PilotRecordRefused("anti_rollback_lock_not_held")
        _require_record_key(record.key)
        current = self.read(record.key)
        rule(current, record)
        if current is not None:
            if record.sequence < current.sequence:
                raise PilotRecordRefused("trust_state_regressed")
            if record.sequence == current.sequence:
                if dict(record.value) != dict(current.value):
                    raise PilotRecordRefused("trust_state_conflict")
                return False
        state = {
            "format": TRUST_STATE_FORMAT,
            "key": record.key,
            "sequence": str(record.sequence),
            "value": dict(record.value),
        }
        try:
            _RecordState.model_validate(state)
            body = canonical(state)
        except (JournalError, ValidationError):
            raise PilotRecordRefused("trust_state_invalid") from None
        if len(body) > MAX_STATE_BYTES:
            raise PilotRecordRefused("trust_state_invalid")
        directory = self._directory / RECORDS_NAME
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if not _private_directory(directory):
            raise PilotRecordRefused("anti_rollback_state_unavailable")
        _write_atomic(directory, _record_name(record.key), body)
        return True


def _require_record_key(key: str) -> None:
    if not re.fullmatch(RECORD_KEY_PATTERN, key):
        raise PilotRecordRefused("trust_state_key_invalid")


def _record_name(key: str) -> str:
    return sha256(key.encode("utf-8")) + ".json"


def _anchor_key(engagement_id: str) -> str:
    return "customer_anchor:" + engagement_id


def _anchor_revision_key(engagement_id: str) -> str:
    return "customer_anchor_revision:" + engagement_id


def _registry_rule(current: MonotonicRecord | None, proposed: MonotonicRecord) -> None:
    if current is None:
        return
    if proposed.sequence < current.sequence:
        raise PilotRecordRefused("registry_revision_rolled_back")
    if proposed.sequence == current.sequence and dict(proposed.value) != dict(current.value):
        raise PilotRecordRefused("registry_revision_equivocated")


def _forward_rule(current: MonotonicRecord | None, proposed: MonotonicRecord) -> None:
    # Anchor revision floor: forward only (it may lag its chain after an interrupted write).
    if current is not None and proposed.sequence < current.sequence:
        raise PilotRecordRefused("customer_anchor_rolled_back")


def _chain_rule(current: MonotonicRecord | None, proposed: MonotonicRecord) -> None:
    # Anchor chain: its sequence is a write generation that starts at 1 and advances by one
    # per write; equality is the generic rule's.
    if current is not None and proposed.sequence == current.sequence:
        return
    if proposed.sequence != (1 if current is None else current.sequence + 1):
        raise PilotRecordRefused("customer_rotation_not_successor")


class _ChainValue(WireModel):
    chain: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=MAX_ANCHOR_CHAIN)]


class _AnchorRevisionValue(WireModel):
    head_payload_sha256: Sha256


class PilotVerifier:
    """Verify-only. Trust comes from the kit-pinned root and re-verified customer anchors.

    ``registry_document`` is the operator-signed issuer registry; it is authenticated
    against the pinned root, so where it was read from confers no authority. Neither the
    root nor the anti-rollback location is a caller argument.
    """

    @_refusing
    def __init__(self, registry_document: bytes) -> None:
        self._store = TrustStateStore()
        pin, roots = _load_root_pin()
        payload, signatures = _open(registry_document, "registry")
        _authenticate(
            payload,
            signatures,
            REGISTRY_PURPOSE,
            {OPERATOR_ROOT: _registry_resolver(roots)},
            "registry",
        )
        try:
            registry = _RegistryPayload.model_validate(payload)
        except ValidationError:
            raise PilotRecordRefused("registry_schema_invalid") from None
        if registry.registry_id != pin.registry_id:
            raise PilotRecordRefused("registry_binding_invalid")
        revision = int(registry.revision)
        if revision < int(pin.minimum_registry_revision):
            raise PilotRecordRefused("registry_revision_below_kit_minimum")
        keys = _trust_set(registry.keys, REGISTRY_ROLES, reserved=roots)
        former = _former_custodians(
            registry.former_custodian_keys, [*keys.values(), *roots.values()]
        )
        # Keyed on the payload digest: a re-signed or high-S copy is the same registry.
        payload_sha256 = _payload_sha256(payload)
        floor = MonotonicRecord(
            REGISTRY_REVISION_KEY, revision, {"registry_payload_sha256": payload_sha256}
        )
        with self._store.locked():
            self._store.advance(floor, _registry_rule)
        self._registry = _Registry(
            revision,
            payload_sha256,
            datetime.fromisoformat(registry.issued_at),
            datetime.fromisoformat(registry.not_after),
            keys,
            frozenset(key.public_key_sha256 for key in roots.values()),
            former,
        )
        self._root_ids = frozenset(key.key_id for key in roots.values())

    @property
    def registry_revision(self) -> int:
        return self._registry.revision

    @_refusing
    def verify(self, record: bytes, purpose: str) -> dict[str, object]:
        """Return the verified payload of ``record`` for ``purpose`` or refuse with a code.

        A purpose the customer owner signs resolves that signer from the engagement's anchor
        only, and also needs the purpose in the anchor's effective consent scope.
        """
        if type(purpose) is not str or purpose not in RECORD_PURPOSES:
            raise PilotRecordRefused("purpose_unknown")
        self._registry_current()
        payload, signatures = _open(record, "record")
        resolvers = self._issuer_resolvers()
        in_scope = True
        # Every purpose the customer owner signs resolves that signer from the anchor only.
        if CUSTOMER_OWNER in REQUIRED_ROLES[purpose]:
            engagement = payload.get("engagement_id")
            try:
                engagement_id = _ID.validate_python(engagement)
            except ValidationError:
                raise PilotRecordRefused("record_schema_invalid") from None
            current = self._current_customer(engagement_id)
            if current is None:
                raise PilotRecordRefused("customer_anchor_missing")
            head = current[0]
            resolvers[CUSTOMER_OWNER] = _fixed_resolver(
                replace(head.key, purposes=frozenset({purpose}))
            )
            in_scope = purpose in head.effective
        _authenticate(payload, signatures, purpose, resolvers, "record")
        # Refused only once authenticated, so the code says nothing about unsigned input.
        if not in_scope:
            raise PilotRecordRefused("customer_purpose_not_enrolled")
        return payload

    @_refusing
    def pin_customer_anchor(self, enrolment: bytes) -> CustomerAnchor:
        """Pin an engagement's customer key at engagement start. Never replaces an anchor.

        Re-presenting the same enrolment payload (even re-signed) is idempotent, and also
        restores a deleted chain or a missing revision floor at revision 1.
        """
        self._registry_current()
        body, head, digest = self._check_enrolment(enrolment, replay=False)
        engagement = body.engagement_id
        anchor = head.anchor(engagement)
        chain_value = {"chain": [strict_json(enrolment)]}
        floor_value = {"head_payload_sha256": digest}
        with self._store.locked():
            self._settle_pending(engagement)
            floor = self._store.read(_anchor_revision_key(engagement))
            record = self._store.read(_anchor_key(engagement))
            if floor is not None and (floor.sequence != 1 or dict(floor.value) != floor_value):
                raise PilotRecordRefused("customer_anchor_already_pinned")
            if record is not None:
                # Keyed on the payload digest, so a re-signed enrolment is the same one.
                # The raw stored length counts, not what the chain derives to: an expired
                # pending rotation must not let the original enrolment re-pin the old key.
                stored = self._chain_entries(record)
                _, digests, _ = self._replay(engagement, record, 1)
                if digests[0] != digest or len(stored) != 1:
                    raise PilotRecordRefused(
                        "customer_anchor_already_pinned"
                        if floor is not None
                        else "customer_anchor_inconsistent"
                    )
            else:
                self._store.advance(
                    MonotonicRecord(_anchor_key(engagement), 1, chain_value), _chain_rule
                )
            self._store.advance(
                MonotonicRecord(_anchor_revision_key(engagement), 1, floor_value), _forward_rule
            )
        return anchor

    @_refusing
    def rotate_customer_key(self, rotation: bytes) -> CustomerAnchor:
        """Replace an anchor only on a rotation signed by the anchored key and the new key.

        The currently anchored (previous) customer key authorizes the change; the key being
        installed proves possession by signing the same transcript, so a customer cannot be
        rotated onto a key nobody holds or onto someone else's key. The successor inherits
        the previous scope exactly unless the rotation states a new ``consent_purposes``.
        """
        self._registry_current()
        payload, _ = _open(rotation, "rotation")
        try:
            engagement = _RotationPayload.model_validate(payload).engagement_id
        except ValidationError:
            raise PilotRecordRefused("rotation_schema_invalid") from None
        with self._store.locked():
            self._settle_pending(engagement)
            record = self._store.read(_anchor_key(engagement))
            current = self._current_customer(engagement)
            if current is None or record is None:
                raise PilotRecordRefused("customer_anchor_missing")
            previous, _, chain = current
            body, successor, digest = self._check_rotation(
                rotation, previous, check_time=True, fresh=True
            )
            if len(chain) >= MAX_ANCHOR_CHAIN:
                raise PilotRecordRefused("customer_rotation_limit")
            self._extend(engagement, record, chain, rotation, int(body.anchor_revision), digest)
        return successor.anchor(engagement)

    @_refusing
    def update_customer_scope(self, record: bytes) -> CustomerAnchor:
        """Replace an anchored key's consent scope, without re-keying, at the next revision.

        Co-signed by the currently anchored customer key and a current ``operator_custodian``
        key whose registry purposes include ``CONSENT_SCOPE_PURPOSE``. Carries the full new
        list, which may widen or narrow the scope. Re-presenting the head update (keyed on
        its payload digest, so a re-signed copy counts) is an idempotent no-op, but only once
        it authenticates now: after its custodian has left the current registry, or once it
        has expired, the re-presentation is refused even though the accepted entry still
        re-verifies.
        """
        self._registry_current()
        payload, _ = _open(record, "scope")
        try:
            engagement = _ScopePayload.model_validate(payload).engagement_id
        except ValidationError:
            raise PilotRecordRefused("scope_schema_invalid") from None
        with self._store.locked():
            self._settle_pending(engagement)
            stored = self._store.read(_anchor_key(engagement))
            current = self._current_customer(engagement)
            if current is None or stored is None:
                raise PilotRecordRefused("customer_anchor_missing")
            head, digests, chain = current
            if len(digests) > 1 and _payload_sha256(payload) == digests[-1]:
                # The head itself, re-presented: authenticate it against the state it was
                # applied to (the same key, one revision earlier), then change nothing.
                prior = _AnchorHead(head.key, head.revision - 1, head.scope)
                self._check_scope_update(record, prior, check_time=True, replay=False, fresh=True)
                return head.anchor(engagement)
            body, updated, digest = self._check_scope_update(
                record, head, check_time=True, replay=False, fresh=True
            )
            if len(chain) >= MAX_ANCHOR_CHAIN:
                raise PilotRecordRefused("customer_scope_limit")
            self._extend(engagement, stored, chain, record, int(body.anchor_revision), digest)
        return updated.anchor(engagement)

    def _extend(
        self,
        engagement: str,
        stored: MonotonicRecord,
        chain: list[dict[str, Any]],
        entry: bytes,
        revision: int,
        digest: str,
    ) -> None:
        """Under the lock: append one accepted entry to the chain, then raise the floor.

        The chain's sequence is its write generation; an ignored pending tail is replaced.
        The signed chain is written first: a chain ahead of its floor still verifies while
        pending, a floor ahead of its chain reads as a rollback.
        """
        chain_value = {"chain": [*chain, strict_json(entry)]}
        self._store.advance(
            MonotonicRecord(_anchor_key(engagement), stored.sequence + 1, chain_value),
            _chain_rule,
        )
        self._store.advance(
            MonotonicRecord(
                _anchor_revision_key(engagement), revision, {"head_payload_sha256": digest}
            ),
            _forward_rule,
        )

    def _issuer_resolvers(self) -> dict[str, Resolver]:
        resolve = _registry_resolver(self._registry.keys)
        return {role: resolve for role in REGISTRY_ROLES}

    def _replay_custodian(self) -> Resolver:
        """Resolve an existing enrolment's co-signer by key id (which fixes its public key).

        Accepted only if a current registry entry for that key id carries the enrolment
        purpose (at any revision), or the key id is listed in ``former_custodian_keys``. A
        custodian key that never held the enrolment purpose validates no enrolment.
        """

        def resolve(signature: _Signature) -> PilotTrustedKey:
            current = [
                key
                for key in self._registry.keys.values()
                if key.key_id == signature.key_id and key.role == OPERATOR_CUSTODIAN
            ]
            enrolling = [key for key in current if ENROLMENT_PURPOSE in key.purposes]
            key = enrolling[0] if enrolling else self._registry.former.get(signature.key_id)
            if key is None:
                raise PilotRecordRefused("key_purpose_not_permitted" if current else "key_unknown")
            return replace(
                key,
                registry_revision=signature.key_registry_revision,
                purposes=frozenset({ENROLMENT_PURPOSE}),
            )

        return resolve

    def _replay_scope_custodian(self) -> Resolver:
        """Resolve an accepted scope update's co-signer by key id (which fixes its key).

        Accepted only if a current registry entry for that key id carries the scope purpose
        (at any revision), or the key id is listed in the verify-only
        ``former_custodian_keys``. Used only for entries at or below the anchor floor: a new
        or pending scope update needs a current custodian at its exact registry revision.
        """

        def resolve(signature: _Signature) -> PilotTrustedKey:
            current = [
                key
                for key in self._registry.keys.values()
                if key.key_id == signature.key_id and key.role == OPERATOR_CUSTODIAN
            ]
            permitted = [key for key in current if CONSENT_SCOPE_PURPOSE in key.purposes]
            key = permitted[0] if permitted else self._registry.former.get(signature.key_id)
            if key is None:
                raise PilotRecordRefused("key_purpose_not_permitted" if current else "key_unknown")
            return replace(
                key,
                registry_revision=signature.key_registry_revision,
                purposes=frozenset({CONSENT_SCOPE_PURPOSE}),
            )

        return resolve

    def _registry_current(self) -> None:
        _check_window(self._registry.issued_at, self._registry.not_after, "registry")
        # A newer verified registry (for example one that drops a key) supersedes this one.
        floor = self._store.read(REGISTRY_REVISION_KEY)
        if floor is None or (floor.sequence, floor.value) != (
            self._registry.revision,
            {"registry_payload_sha256": self._registry.payload_sha256},
        ):
            raise PilotRecordRefused("registry_superseded")

    def _require_distinct(self, customer: PilotTrustedKey) -> None:
        # The operator must not enrol one of its registry, former or root keys as a customer.
        operator_keys = [*self._registry.keys.values(), *self._registry.former.values()]
        if (
            customer.public_key_sha256 in self._registry.root_digests
            or customer.key_id in self._root_ids
            or any(
                key.key_id == customer.key_id or key.public_key_sha256 == customer.public_key_sha256
                for key in operator_keys
            )
        ):
            raise PilotRecordRefused("customer_key_not_distinct")

    def _check_enrolment(
        self, record: bytes, *, replay: bool
    ) -> tuple[_EnrolmentPayload, _AnchorHead, str]:
        payload, signatures = _open(record, "enrolment")
        try:
            body = _EnrolmentPayload.model_validate(payload)
        except ValidationError:
            raise PilotRecordRefused("enrolment_schema_invalid") from None
        if int(body.anchor_revision) != 1:
            raise PilotRecordRefused("customer_anchor_revision_invalid")
        customer = _customer_key(body.customer_key, 1, frozenset({ENROLMENT_PURPOSE}))
        self._require_distinct(customer)
        # A new pin needs a current custodian key permitted for enrolment, at its exact
        # revision; replaying an accepted enrolment also admits former custodian keys.
        resolvers = self._issuer_resolvers()
        if replay:
            resolvers[OPERATOR_CUSTODIAN] = self._replay_custodian()
        # Proof of possession: the enrolled key itself must sign its enrolment.
        resolvers[CUSTOMER_OWNER] = _fixed_resolver(customer, "enrolment_key_mismatch")
        _authenticate(
            payload, signatures, ENROLMENT_PURPOSE, resolvers, "enrolment", check_time=not replay
        )
        scope = _scope_of(payload, fresh=not replay)
        return body, _AnchorHead(customer, 1, scope), _payload_sha256(payload)

    def _check_rotation(
        self, record: bytes, head: _AnchorHead, *, check_time: bool, fresh: bool
    ) -> tuple[_RotationPayload, _AnchorHead, str]:
        previous = head.key
        payload, signatures = _open(record, "rotation")
        try:
            body = _RotationPayload.model_validate(payload)
        except ValidationError:
            raise PilotRecordRefused("rotation_schema_invalid") from None
        revision = int(body.anchor_revision)
        installing = replace(
            _customer_key(body.customer_key, revision, frozenset({ROTATION_PURPOSE})),
            role=CUSTOMER_OWNER_SUCCESSOR,
        )
        _authenticate(
            payload,
            signatures,
            ROTATION_PURPOSE,
            {
                CUSTOMER_OWNER: _fixed_resolver(
                    replace(previous, purposes=frozenset({ROTATION_PURPOSE}))
                ),
                CUSTOMER_OWNER_SUCCESSOR: _fixed_resolver(
                    installing, "rotation_successor_key_mismatch"
                ),
            },
            "rotation",
            check_time=check_time,
        )
        if (
            body.previous_key_id != previous.key_id
            or body.previous_anchor_revision != str(head.revision)
            or revision != head.revision + 1
        ):
            raise PilotRecordRefused("customer_rotation_not_successor")
        successor = _customer_key(body.customer_key, revision, frozenset())
        self._require_distinct(successor)
        if successor.public_key_sha256 == previous.public_key_sha256:
            raise PilotRecordRefused("customer_key_not_distinct")
        # Inheriting is re-derived from the signed chain on every read, so a purpose that is
        # not effective now (retired, or capped by an expired pending update) would come back
        # for the new key. A rotation then states its scope instead of inheriting it.
        if fresh and "consent_purposes" not in payload and head.effective != head.scope:
            raise PilotRecordRefused("customer_scope_restatement_required")
        scope = _scope_of(payload, fresh=fresh, inherit=head.scope)
        return body, _AnchorHead(successor, revision, scope), _payload_sha256(payload)

    def _check_scope_update(
        self, record: bytes, head: _AnchorHead, *, check_time: bool, replay: bool, fresh: bool
    ) -> tuple[_ScopePayload, _AnchorHead, str]:
        """A scope update applied to ``head``: the next anchor revision, the same key.

        The key keeps its signing label (the revision that installed it), so the customer's
        existing signatures stay valid. A new or pending update needs a current custodian at
        its exact registry revision; an accepted one (``replay``) is re-verified by key id,
        like an enrolment's co-signer, former custodian keys included.
        """
        payload, signatures = _open(record, "scope")
        try:
            body = _ScopePayload.model_validate(payload)
        except ValidationError:
            raise PilotRecordRefused("scope_schema_invalid") from None
        previous = head.key
        custodian = (
            self._replay_scope_custodian() if replay else _registry_resolver(self._registry.keys)
        )
        _authenticate(
            payload,
            signatures,
            CONSENT_SCOPE_PURPOSE,
            {
                CUSTOMER_OWNER: _fixed_resolver(
                    replace(previous, purposes=frozenset({CONSENT_SCOPE_PURPOSE}))
                ),
                OPERATOR_CUSTODIAN: custodian,
            },
            "scope",
            check_time=check_time,
        )
        revision = int(body.anchor_revision)
        if (
            body.customer_key_id != previous.key_id
            or body.previous_anchor_revision != str(head.revision)
            or revision != head.revision + 1
        ):
            raise PilotRecordRefused("customer_scope_not_successor")
        scope = _scope_of(payload, fresh=fresh)
        return body, _AnchorHead(previous, revision, scope), _payload_sha256(payload)

    def _chain_step(
        self, item: dict[str, Any], head: _AnchorHead, *, accepted: bool
    ) -> tuple[str, _AnchorHead, str]:
        """Apply one chain entry after the enrolment; returns its engagement, head, digest.

        Accepted entries are re-verified for signatures and binding only; pending ones are
        also time-checked now. Either way scope membership was checked on acceptance.
        """
        record = canonical(item)
        if _entry_purpose(item) == ROTATION_PURPOSE:
            rotated, head, digest = self._check_rotation(
                record, head, check_time=not accepted, fresh=False
            )
            return rotated.engagement_id, head, digest
        scoped, head, digest = self._check_scope_update(
            record, head, check_time=not accepted, replay=accepted, fresh=False
        )
        return scoped.engagement_id, head, digest

    @staticmethod
    def _chain_entries(record: MonotonicRecord) -> list[dict[str, Any]]:
        try:
            return _ChainValue.model_validate(dict(record.value)).chain
        except ValidationError:
            raise PilotRecordRefused("trust_state_unreadable") from None

    def _replay(
        self, engagement_id: str, record: MonotonicRecord, accepted: int
    ) -> tuple[_AnchorHead, list[str], list[dict[str, Any]]]:
        """Re-verify a stored chain from its enrolment; never trust a stored key field.

        The first ``accepted`` entries were accepted by this worker: any failure there makes
        the anchor unverifiable, except that an authenticated enrolment without a scope is
        refused as ``customer_consent_scope_missing``. Later entries are pending and count
        only while they also pass the time check now; otherwise the whole pending tail is
        ignored. Returns the current head, the payload digests and the entries it was
        derived from.
        """
        chain = self._chain_entries(record)
        if len(chain) < accepted:
            # An older copy of the chain is shorter than the revision this worker has seen.
            raise PilotRecordRefused("customer_anchor_rolled_back")
        try:
            body, head, digest = self._check_enrolment(canonical(chain[0]), replay=True)
            if body.engagement_id != engagement_id:
                raise PilotRecordRefused("customer_anchor_unverifiable")
            digests = [digest]
            for item in chain[1:accepted]:
                engagement, head, digest = self._chain_step(item, head, accepted=True)
                if engagement != engagement_id:
                    raise PilotRecordRefused("customer_anchor_unverifiable")
                digests.append(digest)
        except PilotRecordRefused as refusal:
            # A pre-scope anchor is never defaulted to every purpose.
            if refusal.code == "customer_consent_scope_missing":
                raise
            raise PilotRecordRefused("customer_anchor_unverifiable") from None
        except JournalError:
            raise PilotRecordRefused("customer_anchor_unverifiable") from None
        pending_head, pending = head, []
        try:
            for item in chain[accepted:]:
                engagement, pending_head, digest = self._chain_step(
                    item, pending_head, accepted=False
                )
                if engagement != engagement_id:
                    raise PilotRecordRefused("customer_anchor_unverifiable")
                pending.append(digest)
        except (PilotRecordRefused, JournalError):
            # Not acceptable as a whole now: derive the anchor at the floor, still restricted
            # by the expired pending scope updates (fail closed on revocation).
            head, narrowed = self._restrict(engagement_id, head, chain[accepted:])
            return head, digests + narrowed, chain[: accepted + len(narrowed)]
        return pending_head, digests + pending, chain

    def _restrict(
        self, engagement_id: str, head: _AnchorHead, tail: list[dict[str, Any]]
    ) -> tuple[_AnchorHead, list[str]]:
        """Walk a pending tail that does not count, keeping only what narrows the scope.

        Only scope updates by the floor key count, each passing every check now except
        expiry (``scope_expired``); the walk ends at anything else, including a rotation. A
        pure narrowing (its scope within the current signed scope) advances the head, so the
        next locked operation rolls the floor over it: it becomes an accepted, signed entry.
        The first update that widens is never accepted, so its widening never takes effect:
        it caps the head instead (``ceiling``: effective = current scope ∩ its scope) and ends
        the walk.
        That cap holds only while the update stays stored; a rotation must restate its scope
        while it holds, and a new co-signed update replaces it.
        """
        narrowed: list[str] = []
        for item in tail:
            try:
                if _entry_purpose(item) != CONSENT_SCOPE_PURPOSE:
                    break
                record = canonical(item)
                try:
                    body, updated, digest = self._check_scope_update(
                        record, head, check_time=True, replay=False, fresh=False
                    )
                except PilotRecordRefused as refusal:
                    if refusal.code != "scope_expired":
                        raise
                    body, updated, digest = self._check_scope_update(
                        record, head, check_time=False, replay=False, fresh=False
                    )
            except (PilotRecordRefused, JournalError):
                break
            if body.engagement_id != engagement_id:
                break
            if not updated.scope <= head.scope:
                return replace(head, ceiling=updated.scope), narrowed
            head = updated
            narrowed.append(digest)
        return head, narrowed

    def _settle_pending(self, engagement_id: str) -> None:
        """Under the lock: roll the anchor floor forward over a pending tail still in window.

        After this, the pending rotation is accepted and no longer reverts when it expires.
        Any inconsistency is left for the caller's own read to refuse.
        """
        floor = self._store.read(_anchor_revision_key(engagement_id))
        record = self._store.read(_anchor_key(engagement_id))
        if floor is None or record is None:
            return
        try:
            _, digests, _ = self._replay(engagement_id, record, floor.sequence)
        except PilotRecordRefused:
            return
        if len(digests) > floor.sequence and {
            "head_payload_sha256": digests[floor.sequence - 1]
        } == dict(floor.value):
            self._store.advance(
                MonotonicRecord(
                    _anchor_revision_key(engagement_id),
                    len(digests),
                    {"head_payload_sha256": digests[-1]},
                ),
                _forward_rule,
            )

    def _current_customer(
        self, engagement_id: str
    ) -> tuple[_AnchorHead, list[str], list[dict[str, Any]]] | None:
        floor = self._store.read(_anchor_revision_key(engagement_id))
        record = self._store.read(_anchor_key(engagement_id))
        if floor is None and record is None:
            return None
        if floor is None or record is None:
            raise PilotRecordRefused("customer_anchor_inconsistent")
        head, digests, chain = self._replay(engagement_id, record, floor.sequence)
        if {"head_payload_sha256": digests[floor.sequence - 1]} != dict(floor.value):
            raise PilotRecordRefused("customer_anchor_inconsistent")
        return head, digests, chain
