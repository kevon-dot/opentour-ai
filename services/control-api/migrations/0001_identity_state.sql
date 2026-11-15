-- OPE-142 identity state v1. Apply with a separate migration role.
-- No organization/member grants, pool settings, RLS bypass, or authority journal.
BEGIN;
CREATE SCHEMA opentour_identity;
CREATE TABLE opentour_identity.schema_version (
    version integer PRIMARY KEY CHECK (version = 1)
);
INSERT INTO opentour_identity.schema_version VALUES (1);
CREATE TABLE opentour_identity.session_denials (
    issuer text NOT NULL, session_id text NOT NULL, subject text NOT NULL,
    denied_at timestamptz NOT NULL, deny_until timestamptz NOT NULL,
    PRIMARY KEY (issuer, session_id), CHECK (deny_until >= denied_at)
);
CREATE TABLE opentour_identity.auth_transactions (
    state_sha256 text PRIMARY KEY CHECK (state_sha256 ~ '^[0-9a-f]{64}$'),
    browser_sha256 text NOT NULL CHECK (browser_sha256 ~ '^[0-9a-f]{64}$'),
    sealed_payload text NOT NULL CHECK (length(sealed_payload) BETWEEN 1 AND 16384),
    created_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    CHECK (expires_at > created_at AND expires_at <= created_at + interval '600 seconds')
);
CREATE TABLE opentour_identity.sessions (
    issuer text NOT NULL, session_id text NOT NULL, subject text NOT NULL,
    generation bigint NOT NULL DEFAULT 0 CHECK (generation BETWEEN 0 AND 9007199254740991),
    selected_organization_id text, selected_membership_id text,
    reconciliation_required boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
    ended_at timestamptz, deny_until timestamptz NOT NULL,
    PRIMARY KEY (issuer, session_id),
    CHECK ((selected_organization_id IS NULL) = (selected_membership_id IS NULL)),
    CHECK (expires_at > created_at AND deny_until >= expires_at)
);
CREATE TABLE opentour_identity.session_bindings (
    binding_sha256 text PRIMARY KEY CHECK (binding_sha256 ~ '^[0-9a-f]{64}$'),
    issuer text NOT NULL, session_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation BETWEEN 0 AND 9007199254740991),
    expires_at timestamptz NOT NULL,
    FOREIGN KEY (issuer, session_id) REFERENCES opentour_identity.sessions(issuer, session_id)
);
-- This is the provider observation/denial projection, NOT a membership grant table.
CREATE TABLE opentour_identity.membership_observations (
    issuer text NOT NULL, membership_id text NOT NULL, organization_id text NOT NULL,
    subject text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending_identity','active','removed','reconciliation_unknown')),
    locally_denied boolean NOT NULL DEFAULT false,
    checked_at timestamptz NOT NULL, observed_at timestamptz NOT NULL,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision BETWEEN 1 AND 9223372036854775807),
    PRIMARY KEY (issuer, membership_id),
    CHECK (NOT locally_denied OR status = 'removed')
);
CREATE TABLE opentour_identity.sync_cursors (
    issuer text PRIMARY KEY, cursor text
);
CREATE TABLE opentour_identity.event_inbox (
    issuer text NOT NULL, source text NOT NULL CHECK (source IN ('events_api','reconciliation')),
    event_id text NOT NULL, body_sha256 text NOT NULL,
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (issuer, source, event_id),
    CHECK (body_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE TABLE opentour_identity.outbox (
    issuer text NOT NULL, source text NOT NULL, event_id text NOT NULL, kind text NOT NULL,
    membership_id text, organization_id text,
    subject text NOT NULL, created_at timestamptz NOT NULL,
    PRIMARY KEY (issuer, source, event_id, kind),
    CHECK (kind IN ('identity.observed','identity.revocation_requested','identity.session_ended')),
    CHECK ((membership_id IS NULL) = (organization_id IS NULL)),
    FOREIGN KEY (issuer, source, event_id)
        REFERENCES opentour_identity.event_inbox(issuer, source, event_id)
);
-- No DELETE permission is needed by normal requests. Retention/recovery is a
-- separate privileged job; never remove denial markers while credentials live.
REVOKE ALL ON SCHEMA opentour_identity FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA opentour_identity FROM PUBLIC;
COMMIT;
