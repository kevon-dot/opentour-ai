-- OPE-172, after 0001–0003. Only the scoped selector can execute these operations.
BEGIN;
CREATE ROLE opentour_selection_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE SCHEMA opentour_selection;

CREATE TABLE opentour_selection.receipts (
    issuer text NOT NULL,
    session_id text NOT NULL,
    subject text NOT NULL,
    binding_sha256 text NOT NULL CHECK (binding_sha256 ~ '^[a-f0-9]{64}$'),
    key_sha256 text NOT NULL CHECK (key_sha256 ~ '^[a-f0-9]{64}$'),
    request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    organization_id text NOT NULL,
    membership_id text NOT NULL,
    session_generation bigint NOT NULL CHECK (session_generation BETWEEN 0 AND 9007199254740991),
    request_id text NOT NULL CHECK (length(request_id) BETWEEN 1 AND 256),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (issuer, session_id, binding_sha256, key_sha256)
);

CREATE TABLE opentour_selection.outbox (
    issuer text NOT NULL,
    session_id text NOT NULL,
    subject text NOT NULL,
    session_generation bigint NOT NULL CHECK (session_generation BETWEEN 1 AND 9007199254740991),
    organization_id text NOT NULL,
    membership_id text NOT NULL,
    request_id text NOT NULL,
    kind text NOT NULL CHECK (kind = 'session.organization_selected'),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (issuer, session_id, session_generation)
);

ALTER TABLE opentour_selection.receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_selection.receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY selection_receipt_owner ON opentour_selection.receipts TO CURRENT_USER
    USING (issuer = current_setting('opentour.identity_issuer', true)
        AND subject = current_setting('opentour.identity_subject', true)
        AND session_id = current_setting('opentour.session_id', true));
ALTER TABLE opentour_selection.outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_selection.outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY selection_outbox_insert_owner ON opentour_selection.outbox FOR INSERT TO CURRENT_USER
    WITH CHECK (issuer = current_setting('opentour.identity_issuer', true)
        AND subject = current_setting('opentour.identity_subject', true)
        AND session_id = current_setting('opentour.session_id', true)
        AND organization_id = current_setting('opentour.organization_id', true));

-- A scoped source lock for the definer only. The service cannot access source rows directly.
CREATE POLICY selection_authority_owner ON opentour_authority.organizations TO CURRENT_USER
    USING (organization_id = current_setting('opentour.organization_id', true)
        AND nullif(current_setting('opentour.identity_issuer', true), '') IS NOT NULL
        AND nullif(current_setting('opentour.identity_subject', true), '') IS NOT NULL)
    WITH CHECK (false);

CREATE FUNCTION opentour_selection.lock_target_membership() RETURNS text
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
    SELECT source.provider_membership_id FROM opentour_authority.locked_membership() source
$$;

CREATE FUNCTION opentour_selection.current_membership(p_issuer text, p_subject text, p_org text)
RETURNS TABLE (
    identity_organization_id text, membership_id text, provider_membership_id text,
    organization_state text, status text, authority_state text, authority_revision text,
    revocation_epoch bigint
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    PERFORM set_config('opentour.identity_issuer', p_issuer, true);
    PERFORM set_config('opentour.identity_subject', p_subject, true);
    PERFORM set_config('opentour.organization_id', p_org, true);
    RETURN QUERY
      SELECT m.identity_organization_id, m.membership_id, m.provider_membership_id,
          m.organization_state, m.membership_status, a.state, a.authority_revision, a.revocation_epoch
      FROM opentour_authority.locked_membership() m
      LEFT JOIN opentour_authority.organizations a ON a.organization_id = m.organization_id;
END
$$;

CREATE FUNCTION opentour_selection.resolve_selection(
    p_identity_generation bigint, p_binding_sha256 text, p_expected_generation bigint,
    p_key_sha256 text, p_request_sha256 text, p_identity_org text, p_membership text,
    p_authority_revision text, p_claim_expiry timestamptz, p_sealed_sha256 text, p_request_id text
)
RETURNS TABLE (
    outcome text, organization_id text, identity_organization_id text, membership_id text,
    authority_revision text, session_generation bigint, request_id text, already_current boolean
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE
    local_org text := nullif(current_setting('opentour.organization_id', true), '');
    actor_issuer text := nullif(current_setting('opentour.identity_issuer', true), '');
    actor_subject text := nullif(current_setting('opentour.identity_subject', true), '');
    actor_session text := nullif(current_setting('opentour.session_id', true), '');
    selected record;
    current_session record;
    previous_receipt record;
    observation record;
    authority record;
    credential record;
    now_utc timestamptz;
    next_generation bigint;
BEGIN
    IF local_org IS NULL OR actor_issuer IS NULL OR actor_subject IS NULL OR actor_session IS NULL
       OR p_identity_generation IS NULL OR p_expected_generation IS NULL
       OR p_binding_sha256 IS NULL OR p_binding_sha256 !~ '^[a-f0-9]{64}$'
       OR p_key_sha256 IS NULL OR p_key_sha256 !~ '^[a-f0-9]{64}$'
       OR p_request_sha256 IS NULL OR p_request_sha256 !~ '^[a-f0-9]{64}$' THEN
        RAISE EXCEPTION 'SESSION_INVALID';
    END IF;

    -- Match OPE-143 order: organization/membership, session, observation, authority.
    -- The trusted Python caller also takes OPE-142's exact observation advisory lock.
    SELECT * INTO selected FROM opentour_authority.locked_membership();
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'opentour-identity:' || actor_issuer || ':' || actor_session, 0));
    SELECT s.*, b.generation AS binding_generation, b.expires_at AS binding_expires_at
      INTO current_session
      FROM opentour_identity.sessions s
      JOIN opentour_identity.session_bindings b USING (issuer, session_id)
      WHERE s.issuer = actor_issuer AND s.session_id = actor_session AND s.subject = actor_subject
        AND b.binding_sha256 = p_binding_sha256
      FOR UPDATE OF s, b;
    now_utc := clock_timestamp();
    IF NOT FOUND OR current_session.ended_at IS NOT NULL OR current_session.created_at > now_utc
       OR current_session.expires_at <= now_utc OR current_session.binding_expires_at <= now_utc
       OR EXISTS (SELECT 1 FROM opentour_identity.session_denials d
           WHERE d.issuer = actor_issuer AND d.session_id = actor_session) THEN
        RAISE EXCEPTION 'SESSION_INVALID';
    END IF;
    IF current_session.generation <> current_session.binding_generation
       OR current_session.generation <> p_identity_generation THEN
        RAISE EXCEPTION 'STALE_SESSION_CONTEXT';
    END IF;

    SELECT r.* INTO previous_receipt FROM opentour_selection.receipts r
      WHERE r.issuer = actor_issuer AND r.session_id = actor_session AND r.subject = actor_subject
        AND r.binding_sha256 = p_binding_sha256 AND r.key_sha256 = p_key_sha256;
    IF FOUND THEN
        IF previous_receipt.request_sha256 <> p_request_sha256 THEN
            RAISE EXCEPTION 'IDEMPOTENCY_CONFLICT';
        END IF;
        RETURN QUERY SELECT 'receipt'::text, previous_receipt.organization_id, NULL::text,
            previous_receipt.membership_id, NULL::text, previous_receipt.session_generation,
            previous_receipt.request_id, false;
        RETURN;
    END IF;

    IF current_session.generation <> p_expected_generation THEN
        RAISE EXCEPTION 'STALE_SESSION_CONTEXT';
    END IF;
    IF selected.organization_id IS NULL THEN
        RAISE EXCEPTION 'RESOURCE_NOT_FOUND';
    END IF;
    IF selected.organization_state <> 'active' OR selected.membership_status <> 'active' THEN
        RAISE EXCEPTION 'MEMBERSHIP_INACTIVE';
    END IF;
    IF selected.identity_organization_id IS NULL OR selected.provider_membership_id IS NULL THEN
        RAISE EXCEPTION 'CONTEXT_RECONCILIATION_REQUIRED';
    END IF;
    IF current_session.selected_organization_id IS NOT DISTINCT FROM local_org THEN
        IF current_session.selected_membership_id IS DISTINCT FROM selected.membership_id
           OR current_session.reconciliation_required THEN
            RAISE EXCEPTION 'CONTEXT_RECONCILIATION_REQUIRED';
        END IF;
        next_generation := current_session.generation;
    ELSE
        IF current_session.generation = 9007199254740991 THEN
            RAISE EXCEPTION 'SESSION_GENERATION_EXHAUSTED';
        END IF;
        next_generation := current_session.generation + 1;
    END IF;

    SELECT o.* INTO observation FROM opentour_identity.membership_observations o
      WHERE o.issuer = actor_issuer AND o.membership_id = selected.provider_membership_id FOR SHARE;
    IF FOUND THEN
        IF observation.subject <> actor_subject
           OR observation.organization_id <> selected.identity_organization_id THEN
            RAISE EXCEPTION 'CONTEXT_RECONCILIATION_REQUIRED';
        END IF;
        IF observation.locally_denied OR observation.status = 'removed' THEN
            RAISE EXCEPTION 'MEMBERSHIP_INACTIVE';
        END IF;
    END IF;
    SELECT a.authority_revision, a.state INTO authority FROM opentour_authority.organizations a
      WHERE a.organization_id = local_org FOR SHARE;
    IF NOT FOUND OR authority.state <> 'current' THEN
        RAISE EXCEPTION 'AUTHORITY_UNAVAILABLE';
    END IF;

    now_utc := clock_timestamp();
    IF current_session.expires_at <= now_utc OR current_session.binding_expires_at <= now_utc THEN
        RAISE EXCEPTION 'SESSION_INVALID';
    END IF;
    IF observation.issuer IS NULL OR observation.status <> 'active'
       OR observation.checked_at > now_utc OR observation.observed_at > now_utc
       OR observation.checked_at < now_utc - interval '30 seconds' THEN
        RAISE EXCEPTION 'IDENTITY_FRESHNESS_UNAVAILABLE';
    END IF;
    IF p_request_id IS NULL THEN
        RETURN QUERY SELECT 'target'::text, local_org, selected.identity_organization_id,
            selected.membership_id, authority.authority_revision, NULL::bigint, NULL::text,
            next_generation = current_session.generation;
        RETURN;
    END IF;

    IF selected.identity_organization_id IS DISTINCT FROM p_identity_org
       OR selected.membership_id IS DISTINCT FROM p_membership
       OR authority.authority_revision IS DISTINCT FROM p_authority_revision THEN
        RAISE EXCEPTION 'STALE_SESSION_CONTEXT';
    END IF;
    SELECT c.generation, c.expires_at, encode(sha256(convert_to(c.sealed_payload, 'UTF8')), 'hex') AS digest
      INTO credential FROM opentour_identity.session_credentials c
      WHERE c.issuer = actor_issuer AND c.session_id = actor_session FOR UPDATE;
    IF NOT FOUND OR credential.expires_at <= now_utc THEN
        RAISE EXCEPTION 'SESSION_CREDENTIALS_UNAVAILABLE';
    END IF;
    IF credential.generation <> current_session.generation
       OR credential.digest IS DISTINCT FROM p_sealed_sha256 THEN
        RAISE EXCEPTION 'STALE_SESSION_CONTEXT';
    END IF;
    now_utc := clock_timestamp();
    IF p_claim_expiry IS NULL OR p_claim_expiry <= now_utc OR current_session.expires_at <= now_utc
       OR current_session.binding_expires_at <= now_utc OR credential.expires_at <= now_utc THEN
        RAISE EXCEPTION 'SESSION_INVALID';
    END IF;
    IF observation.checked_at > now_utc OR observation.observed_at > now_utc
       OR observation.checked_at < now_utc - interval '30 seconds' THEN
        RAISE EXCEPTION 'IDENTITY_FRESHNESS_UNAVAILABLE';
    END IF;

    IF next_generation <> current_session.generation THEN
        UPDATE opentour_identity.sessions s
          SET selected_organization_id = local_org, selected_membership_id = selected.membership_id,
              generation = next_generation, reconciliation_required = false
          WHERE s.issuer = actor_issuer AND s.session_id = actor_session;
        UPDATE opentour_identity.session_bindings b SET generation = next_generation
          WHERE b.binding_sha256 = p_binding_sha256;
        UPDATE opentour_identity.session_credentials c SET generation = next_generation
          WHERE c.issuer = actor_issuer AND c.session_id = actor_session;
        INSERT INTO opentour_selection.outbox VALUES (
            actor_issuer, actor_session, actor_subject, next_generation, local_org,
            selected.membership_id, p_request_id, 'session.organization_selected', now_utc
        );
    END IF;
    INSERT INTO opentour_selection.receipts VALUES (
        actor_issuer, actor_session, actor_subject, p_binding_sha256, p_key_sha256,
        p_request_sha256, local_org, selected.membership_id, next_generation, p_request_id, now_utc
    );
    RETURN QUERY SELECT 'receipt'::text, local_org, NULL::text, selected.membership_id,
        NULL::text, next_generation, p_request_id, false;
END
$$;

REVOKE ALL ON SCHEMA opentour_selection FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA opentour_selection FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA opentour_selection FROM PUBLIC;
GRANT USAGE ON SCHEMA opentour_selection TO opentour_selection_runtime;
GRANT EXECUTE ON FUNCTION opentour_selection.lock_target_membership(),
    opentour_selection.current_membership(text,text,text),
    opentour_selection.resolve_selection(bigint,text,bigint,text,text,text,text,text,timestamptz,text,text)
    TO opentour_selection_runtime;
COMMIT;
