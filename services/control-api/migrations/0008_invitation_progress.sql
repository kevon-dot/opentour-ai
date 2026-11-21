-- OPE-172 pending receipt read only. The operations router owns the public route.
BEGIN;

CREATE ROLE opentour_invitation_progress_runtime
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

-- The route has no organization parameter. Bound the recipient's exact lookup
-- without requiring a global operation-ID namespace across tenant stores.
CREATE INDEX invitation_receipts_pending_recipient_operation
    ON opentour_invitation.receipts (recipient_issuer, recipient_subject, operation_id)
    WHERE state = 'awaiting_identity';

CREATE POLICY invitation_progress_read_owner ON opentour_invitation.receipts
    FOR SELECT TO CURRENT_USER USING (
        recipient_issuer = current_setting('opentour.identity_issuer', true)
        AND recipient_subject = current_setting('opentour.identity_subject', true)
        AND operation_id = current_setting('opentour.invitation_progress_operation_id', true)
        AND state = 'awaiting_identity'
    );

CREATE FUNCTION opentour_invitation.progress(
    p_issuer text, p_subject text, p_session_id text, p_session_generation bigint,
    p_binding_sha256 text, p_operation_id text, p_bearer_expires_at timestamptz
) RETURNS TABLE (
    invitation_id text, membership_id text, operation_id text, state text, revision text, request_id text
) LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    prior_issuer text := coalesce(current_setting('opentour.identity_issuer', true), '');
    prior_subject text := coalesce(current_setting('opentour.identity_subject', true), '');
    prior_session text := coalesce(current_setting('opentour.session_id', true), '');
    prior_org text := coalesce(current_setting('opentour.organization_id', true), '');
    prior_invitation text := coalesce(current_setting('opentour.invitation_id', true), '');
    prior_operation text := coalesce(current_setting('opentour.invitation_progress_operation_id', true), '');
    candidate_count integer;
    local_org text;
    local_invitation text;
    organization record;
    current_session record;
    current_membership_id text;
    receipt opentour_invitation.receipts%ROWTYPE;
    deadline timestamptz;
    now_utc timestamptz;
BEGIN
    IF p_issuer IS NULL OR length(p_issuer) NOT BETWEEN 1 AND 256
        OR p_subject IS NULL OR length(p_subject) NOT BETWEEN 1 AND 256
        OR p_session_id IS NULL OR length(p_session_id) NOT BETWEEN 1 AND 256
        OR p_session_generation IS NULL OR p_session_generation NOT BETWEEN 0 AND 9007199254740991
        OR p_binding_sha256 IS NULL OR p_binding_sha256 COLLATE "C" !~ '^[a-f0-9]{64}$'
        OR p_bearer_expires_at IS NULL OR NOT isfinite(p_bearer_expires_at)
    THEN
        RAISE EXCEPTION 'SESSION_INVALID';
    END IF;

    PERFORM set_config('opentour.identity_issuer', p_issuer, true);
    PERFORM set_config('opentour.identity_subject', p_subject, true);
    PERFORM set_config('opentour.session_id', p_session_id, true);
    PERFORM set_config('opentour.organization_id', '', true);
    PERFORM set_config('opentour.invitation_id', '', true);
    PERFORM set_config('opentour.invitation_progress_operation_id', '', true);
    IF p_operation_id IS NOT NULL AND length(p_operation_id) BETWEEN 1 AND 256 THEN
        PERFORM set_config('opentour.invitation_progress_operation_id', p_operation_id, true);
        SELECT count(*), min(candidate.organization_id), min(candidate.invitation_id)
            INTO candidate_count, local_org, local_invitation
            FROM (
                SELECT r.organization_id, r.invitation_id FROM opentour_invitation.receipts r
                WHERE r.recipient_issuer = p_issuer AND r.recipient_subject = p_subject
                    AND r.operation_id = p_operation_id AND r.state = 'awaiting_identity'
                LIMIT 2
            ) candidate;
        IF candidate_count <> 1 THEN
            local_org := NULL;
        END IF;
    END IF;

    PERFORM set_config('opentour.organization_id', coalesce(local_org, ''), true);
    SELECT o.id, o.state, o.identity_org_id INTO organization
        FROM opentour_tenants.organizations o WHERE o.id = local_org FOR SHARE OF o;

    -- Avoid waiting for a legacy writer that holds the session fence while awaiting the organization.
    IF NOT pg_try_advisory_xact_lock(hashtextextended(
        'opentour-identity:' || p_issuer || ':' || p_session_id, 0
    )) THEN
        RAISE EXCEPTION 'IDENTITY_STATE_UNAVAILABLE';
    END IF;
    SELECT s.generation, s.ended_at, s.created_at, s.expires_at,
        b.generation AS binding_generation, b.expires_at AS binding_expires_at
        INTO current_session FROM opentour_identity.sessions s
        JOIN opentour_identity.session_bindings b USING (issuer, session_id)
        WHERE s.issuer = p_issuer AND s.session_id = p_session_id AND s.subject = p_subject
            AND b.binding_sha256 = p_binding_sha256
        FOR SHARE OF s, b;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'SESSION_INVALID';
    END IF;
    IF current_session.ended_at IS NOT NULL OR EXISTS (
        SELECT 1 FROM opentour_identity.session_denials d
        WHERE d.issuer = p_issuer AND d.session_id = p_session_id
    ) THEN
        RAISE EXCEPTION 'SESSION_ENDED';
    END IF;
    deadline := least(current_session.expires_at, current_session.binding_expires_at, p_bearer_expires_at);
    now_utc := clock_timestamp();
    IF current_session.created_at > now_utc OR now_utc >= deadline THEN
        RAISE EXCEPTION 'SESSION_INVALID';
    END IF;
    IF current_session.generation <> p_session_generation
        OR current_session.generation <> current_session.binding_generation
    THEN
        RAISE EXCEPTION 'STALE_SESSION_CONTEXT';
    END IF;

    IF organization.id IS NULL OR organization.state <> 'active' THEN
        RAISE EXCEPTION 'RESOURCE_NOT_FOUND';
    END IF;
    SELECT m.membership_id INTO current_membership_id FROM opentour_tenants.memberships m
        WHERE m.organization_id = local_org AND m.issuer = p_issuer AND m.subject = p_subject
            AND m.status = 'provisional'
        FOR SHARE OF m;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'RESOURCE_NOT_FOUND';
    END IF;

    -- Recheck ambiguity, exact membership, and provider denial in one snapshot.
    WITH matching AS MATERIALIZED (
        SELECT r.* FROM opentour_invitation.receipts r
        WHERE r.recipient_issuer = p_issuer AND r.recipient_subject = p_subject
            AND r.operation_id = p_operation_id AND r.state = 'awaiting_identity'
        LIMIT 2
    )
    SELECT r.* INTO receipt FROM matching r
        WHERE r.organization_id = local_org AND r.invitation_id = local_invitation
            AND r.membership_id = current_membership_id AND (SELECT count(*) FROM matching) = 1
            AND NOT EXISTS (
                SELECT 1 FROM opentour_identity.membership_observations o
                WHERE o.issuer = p_issuer AND o.organization_id = organization.identity_org_id
                    AND o.subject = p_subject AND (o.locally_denied OR o.status = 'removed')
            );
    IF clock_timestamp() >= deadline THEN
        RAISE EXCEPTION 'SESSION_INVALID';
    END IF;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'RESOURCE_NOT_FOUND';
    END IF;
    -- Restore the caller's scope. PostgreSQL rolls back local settings on an error.
    PERFORM set_config('opentour.identity_issuer', prior_issuer, true);
    PERFORM set_config('opentour.identity_subject', prior_subject, true);
    PERFORM set_config('opentour.session_id', prior_session, true);
    PERFORM set_config('opentour.organization_id', prior_org, true);
    PERFORM set_config('opentour.invitation_id', prior_invitation, true);
    PERFORM set_config('opentour.invitation_progress_operation_id', prior_operation, true);
    RETURN QUERY SELECT receipt.invitation_id, receipt.membership_id, receipt.operation_id,
        receipt.state, receipt.revision::text, receipt.request_id;
END
$$;

REVOKE ALL ON FUNCTION opentour_invitation.progress(text,text,text,bigint,text,text,timestamptz)
    FROM PUBLIC;
GRANT USAGE ON SCHEMA opentour_invitation TO opentour_invitation_progress_runtime;
GRANT EXECUTE ON FUNCTION opentour_invitation.progress(text,text,text,bigint,text,text,timestamptz)
    TO opentour_invitation_progress_runtime;
COMMIT;
