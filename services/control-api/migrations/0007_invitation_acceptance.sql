-- OPE-172 local acceptance only. The owner applies this after 0006.
BEGIN;

CREATE ROLE opentour_invitation_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE SCHEMA opentour_invitation;

-- Keep the legacy setup roles confined to the owner rows they already manage.
ALTER POLICY own_initial_membership_read ON opentour_tenants.memberships USING (
    role = 'owner' AND issuer = current_setting('opentour.identity_issuer', true)
    AND subject = current_setting('opentour.identity_subject', true)
);
ALTER POLICY own_initial_membership_remove ON opentour_tenants.memberships USING (
    role = 'owner' AND issuer = current_setting('opentour.identity_issuer', true)
    AND subject = current_setting('opentour.identity_subject', true)
) WITH CHECK (
    role = 'owner' AND issuer = current_setting('opentour.identity_issuer', true)
    AND subject = current_setting('opentour.identity_subject', true) AND status = 'removed'
);
ALTER POLICY provision_membership ON opentour_tenants.memberships
    USING (role = 'owner') WITH CHECK (role = 'owner');
ALTER TABLE opentour_tenants.memberships
    DROP CONSTRAINT initial_owner_role,
    ADD CONSTRAINT membership_role CHECK (role IN ('owner', 'member', 'admin', 'viewer'));
CREATE POLICY invitation_membership_insert_owner ON opentour_tenants.memberships
    FOR INSERT TO CURRENT_USER WITH CHECK (
        organization_id = current_setting('opentour.organization_id', true)
        AND issuer = current_setting('opentour.identity_issuer', true)
        AND subject = current_setting('opentour.identity_subject', true)
        AND role = current_setting('opentour.invitation_role', true)
        AND role IN ('member', 'admin', 'viewer')
        AND status = 'provisional' AND provider_membership_id IS NULL AND grant_revision = 1
    );

ALTER TABLE opentour_tenants.invitations
    DROP CONSTRAINT invitations_status_check,
    ADD CONSTRAINT invitation_acceptance_status CHECK (status IN ('pending', 'consumed')),
    ADD CONSTRAINT invitation_consumed_actor CHECK ((status = 'consumed') = (accepted_issuer IS NOT NULL));
-- Exact digest discovery is limited to the definer. Only its accepted actor can be recorded.
CREATE POLICY invitation_acceptance_lookup_owner ON opentour_tenants.invitations
    FOR SELECT TO CURRENT_USER USING (
        token_hash = current_setting('opentour.invitation_token_sha256', true)
    );
CREATE POLICY invitation_acceptance_consume_owner ON opentour_tenants.invitations
    FOR UPDATE TO CURRENT_USER USING (
        organization_id = current_setting('opentour.organization_id', true)
        AND token_hash = current_setting('opentour.invitation_token_sha256', true)
    ) WITH CHECK (
        organization_id = current_setting('opentour.organization_id', true)
        AND token_hash = current_setting('opentour.invitation_token_sha256', true)
        AND status = 'consumed'
        AND accepted_issuer = current_setting('opentour.identity_issuer', true)
        AND accepted_subject = current_setting('opentour.identity_subject', true)
    );

CREATE TABLE opentour_invitation.receipts (
    organization_id text NOT NULL,
    invitation_id text NOT NULL,
    recipient_issuer text NOT NULL,
    recipient_subject text NOT NULL,
    membership_id text NOT NULL CHECK (length(membership_id) BETWEEN 1 AND 256),
    operation_id text CHECK (length(operation_id) BETWEEN 1 AND 256),
    state text NOT NULL CHECK (state IN ('awaiting_identity', 'already_member')),
    revision bigint NOT NULL CHECK (revision >= 1),
    request_id text NOT NULL CHECK (length(request_id) BETWEEN 1 AND 256),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (organization_id, invitation_id),
    UNIQUE (organization_id, operation_id),
    FOREIGN KEY (organization_id, invitation_id)
        REFERENCES opentour_tenants.invitations(organization_id, id),
    FOREIGN KEY (organization_id, recipient_issuer, recipient_subject)
        REFERENCES opentour_tenants.memberships(organization_id, issuer, subject),
    CHECK ((state = 'awaiting_identity') = (operation_id IS NOT NULL))
);
-- One durable request for identity reconciliation per provisional acceptance operation.
CREATE TABLE opentour_invitation.outbox (
    organization_id text NOT NULL,
    operation_id text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (organization_id, operation_id),
    FOREIGN KEY (organization_id, operation_id)
        REFERENCES opentour_invitation.receipts(organization_id, operation_id)
);

ALTER TABLE opentour_invitation.receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_invitation.receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY invitation_receipt_read_owner ON opentour_invitation.receipts
    FOR SELECT TO CURRENT_USER USING (
        organization_id = current_setting('opentour.organization_id', true)
        AND invitation_id = current_setting('opentour.invitation_id', true)
        AND recipient_issuer = current_setting('opentour.identity_issuer', true)
        AND recipient_subject = current_setting('opentour.identity_subject', true)
    );
CREATE POLICY invitation_receipt_insert_owner ON opentour_invitation.receipts
    FOR INSERT TO CURRENT_USER WITH CHECK (
        organization_id = current_setting('opentour.organization_id', true)
        AND invitation_id = current_setting('opentour.invitation_id', true)
        AND recipient_issuer = current_setting('opentour.identity_issuer', true)
        AND recipient_subject = current_setting('opentour.identity_subject', true)
    );
ALTER TABLE opentour_invitation.outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_invitation.outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY invitation_outbox_insert_owner ON opentour_invitation.outbox
    FOR INSERT TO CURRENT_USER WITH CHECK (
        organization_id = current_setting('opentour.organization_id', true)
        AND EXISTS (
            SELECT 1 FROM opentour_invitation.receipts r
            WHERE r.organization_id = outbox.organization_id AND r.operation_id = outbox.operation_id
        )
    );

CREATE FUNCTION opentour_invitation.accept(
    p_issuer text, p_subject text, p_session_id text, p_session_generation bigint,
    p_binding_sha256 text, p_recipient text, p_token_sha256 text, p_membership_id text,
    p_operation_id text, p_request_id text, p_bearer_expires_at timestamptz
) RETURNS TABLE (
    invitation_id text, membership_id text, operation_id text, state text, revision text, request_id text
) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    local_org text;
    organization record;
    current_session record;
    invite opentour_tenants.invitations%ROWTYPE;
    member opentour_tenants.memberships%ROWTYPE;
    receipt opentour_invitation.receipts%ROWTYPE;
    member_exists boolean;
    membership_checked_at timestamptz;
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
    PERFORM set_config('opentour.invitation_token_sha256', coalesce(p_token_sha256, ''), true);
    -- Discover the internal organization first, then acquire its canonical write lock.
    SELECT i.organization_id INTO local_org FROM opentour_tenants.invitations i
        WHERE p_token_sha256 COLLATE "C" ~ '^[a-f0-9]{64}$' AND i.token_hash = p_token_sha256;
    PERFORM set_config('opentour.organization_id', coalesce(local_org, ''), true);
    SELECT o.id, o.state, o.identity_org_id INTO organization
        FROM opentour_tenants.organizations o WHERE o.id = local_org FOR UPDATE;

    -- OPE-171 may already hold this session lock while waiting on the organization.
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
        FOR UPDATE OF s, b;
    now_utc := clock_timestamp();
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
    SELECT i.* INTO invite FROM opentour_tenants.invitations i
        WHERE i.organization_id = local_org AND i.token_hash = p_token_sha256 FOR UPDATE;
    IF clock_timestamp() >= deadline THEN
        RAISE EXCEPTION 'SESSION_INVALID';
    END IF;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'RESOURCE_NOT_FOUND';
    END IF;
    IF invite.normalized_recipient IS DISTINCT FROM p_recipient COLLATE "C"
        OR (invite.status = 'consumed' AND (
            invite.accepted_issuer IS DISTINCT FROM p_issuer
            OR invite.accepted_subject IS DISTINCT FROM p_subject
        ))
    THEN
        RAISE EXCEPTION 'CAPABILITY_DENIED';
    END IF;
    IF invite.status = 'pending' AND clock_timestamp() >= invite.expires_at THEN
        RAISE EXCEPTION 'INVITATION_EXPIRED';
    END IF;
    PERFORM set_config('opentour.invitation_id', invite.id, true);

    SELECT m.* INTO member FROM opentour_tenants.memberships m
        WHERE m.organization_id = local_org AND m.issuer = p_issuer AND m.subject = p_subject FOR UPDATE;
    member_exists := FOUND;
    IF (member_exists AND member.status = 'removed') OR EXISTS (
        SELECT 1 FROM opentour_identity.membership_observations o
        WHERE o.issuer = p_issuer AND o.organization_id = organization.identity_org_id
            AND o.subject = p_subject AND (o.locally_denied OR o.status = 'removed')
    ) THEN
        RAISE EXCEPTION 'CAPABILITY_DENIED';
    END IF;
    IF member_exists AND member.status = 'active' THEN
        SELECT o.checked_at INTO membership_checked_at FROM opentour_identity.membership_observations o
            WHERE o.issuer = p_issuer AND o.membership_id = member.provider_membership_id
                AND o.organization_id = organization.identity_org_id AND o.subject = p_subject
                AND o.status = 'active' AND NOT o.locally_denied
            FOR SHARE OF o;
        now_utc := clock_timestamp();
        IF NOT FOUND OR membership_checked_at > now_utc
            OR membership_checked_at < now_utc - interval '30 seconds'
        THEN
            RAISE EXCEPTION 'IDENTITY_STATE_UNAVAILABLE';
        END IF;
    END IF;

    IF invite.status = 'consumed' THEN
        SELECT r.* INTO receipt FROM opentour_invitation.receipts r
            WHERE r.organization_id = local_org AND r.invitation_id = invite.id;
        IF NOT FOUND OR NOT member_exists OR receipt.membership_id <> member.membership_id
            OR (receipt.state = 'already_member' AND member.status <> 'active')
            OR (receipt.state = 'awaiting_identity' AND member.status <> 'provisional')
        THEN
            RAISE EXCEPTION 'CAPABILITY_DENIED';
        END IF;
        IF receipt.state = 'already_member' THEN
            receipt.revision := member.grant_revision;
        END IF;
    ELSE
        IF member_exists AND member.status <> 'active' THEN
            RAISE EXCEPTION 'CAPABILITY_DENIED';
        END IF;
        now_utc := clock_timestamp();
        IF now_utc >= deadline THEN
            RAISE EXCEPTION 'SESSION_INVALID';
        END IF;
        IF now_utc >= invite.expires_at THEN
            RAISE EXCEPTION 'INVITATION_EXPIRED';
        END IF;
        IF p_request_id IS NULL OR length(p_request_id) NOT BETWEEN 1 AND 256 THEN
            RAISE EXCEPTION 'IDENTITY_STATE_UNAVAILABLE';
        END IF;
        IF NOT member_exists THEN
            IF p_membership_id IS NULL OR length(p_membership_id) NOT BETWEEN 1 AND 256
                OR p_operation_id IS NULL OR length(p_operation_id) NOT BETWEEN 1 AND 256
            THEN
                RAISE EXCEPTION 'IDENTITY_STATE_UNAVAILABLE';
            END IF;
            PERFORM set_config('opentour.invitation_role', invite.role, true);
            INSERT INTO opentour_tenants.memberships
                (organization_id, issuer, subject, membership_id, status, role, grant_revision, updated_at)
                VALUES (local_org, p_issuer, p_subject, p_membership_id, 'provisional', invite.role, 1, now_utc)
                RETURNING * INTO member;
        END IF;
        UPDATE opentour_tenants.invitations i SET
            status = 'consumed', accepted_issuer = p_issuer, accepted_subject = p_subject
            WHERE i.organization_id = local_org AND i.id = invite.id AND i.status = 'pending';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'IDENTITY_STATE_UNAVAILABLE';
        END IF;
        INSERT INTO opentour_invitation.receipts
            (organization_id, invitation_id, recipient_issuer, recipient_subject,
                membership_id, operation_id, state, revision, request_id, created_at)
            VALUES (local_org, invite.id, p_issuer, p_subject, member.membership_id,
                CASE WHEN NOT member_exists THEN p_operation_id END,
                CASE WHEN member_exists THEN 'already_member' ELSE 'awaiting_identity' END,
                member.grant_revision, p_request_id, now_utc)
            RETURNING * INTO receipt;
        IF NOT member_exists THEN
            INSERT INTO opentour_invitation.outbox (organization_id, operation_id, created_at)
                VALUES (local_org, receipt.operation_id, now_utc);
        END IF;
        IF clock_timestamp() >= invite.expires_at THEN
            RAISE EXCEPTION 'INVITATION_EXPIRED';
        END IF;
    END IF;
    -- Recheck after the final writes and any waits they incurred.
    now_utc := clock_timestamp();
    IF now_utc >= deadline THEN
        RAISE EXCEPTION 'SESSION_INVALID';
    END IF;
    IF membership_checked_at IS NOT NULL AND (
        membership_checked_at > now_utc OR membership_checked_at < now_utc - interval '30 seconds'
    ) THEN
        RAISE EXCEPTION 'IDENTITY_STATE_UNAVAILABLE';
    END IF;
    RETURN QUERY SELECT receipt.invitation_id, receipt.membership_id, receipt.operation_id,
        receipt.state, receipt.revision::text, receipt.request_id;
END
$$;

REVOKE ALL ON SCHEMA opentour_invitation FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA opentour_invitation FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA opentour_invitation FROM PUBLIC;
GRANT USAGE ON SCHEMA opentour_invitation TO opentour_invitation_runtime;
GRANT EXECUTE ON FUNCTION opentour_invitation.accept(text,text,text,bigint,text,text,text,text,text,text,timestamptz)
    TO opentour_invitation_runtime;
COMMIT;
