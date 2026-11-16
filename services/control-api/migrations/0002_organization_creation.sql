-- OPE-171. Apply once with the migration role; never the request/worker roles.
-- Initial owner records do not mint OPE-143 capability grants or authority leases.
BEGIN;
CREATE ROLE opentour_tenant_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE opentour_provisioner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE SCHEMA opentour_tenants;
CREATE TABLE opentour_tenants.terms_versions (
    revision text PRIMARY KEY CHECK (length(revision) BETWEEN 1 AND 256),
    document_url text NOT NULL CHECK (length(document_url) BETWEEN 1 AND 2048),
    effective_at timestamptz NOT NULL
);
CREATE TABLE opentour_tenants.terms_publication (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision text NOT NULL REFERENCES opentour_tenants.terms_versions(revision)
);
-- Lock only the current publication/version without granting request handlers
-- UPDATE privileges required by PostgreSQL's FOR SHARE clause. No parameters,
-- dynamic SQL, caller paths or caller-selected tables enter this function.
CREATE FUNCTION opentour_tenants.locked_current_terms()
RETURNS TABLE(revision text, document_url text, effective_at timestamptz)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog
AS $$
    SELECT v.revision, v.document_url, v.effective_at
    FROM opentour_tenants.terms_publication p
    JOIN opentour_tenants.terms_versions v ON p.revision=v.revision
    WHERE p.singleton FOR SHARE OF p,v
$$;
REVOKE ALL ON FUNCTION opentour_tenants.locked_current_terms() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION opentour_tenants.locked_current_terms() TO opentour_tenant_runtime;
CREATE TABLE opentour_tenants.admission_locks (
    issuer text NOT NULL, subject text NOT NULL, last_checked_at timestamptz NOT NULL,
    PRIMARY KEY (issuer, subject)
);
CREATE TABLE opentour_tenants.organizations (
    id text PRIMARY KEY, identity_org_id text UNIQUE,
    display_name varchar(120) NOT NULL CHECK (length(display_name) > 0),
    creator_issuer text NOT NULL, creator_subject text NOT NULL,
    terms_revision text NOT NULL REFERENCES opentour_tenants.terms_versions(revision),
    terms_accepted_at timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN (
        'requested','identity_provisioning','identity_bound','active',
        'provider_outcome_unknown','action_required','cancelled'
    )),
    revision bigint NOT NULL CHECK (revision >= 1),
    created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
    UNIQUE (id, creator_issuer, creator_subject),
    CHECK (state <> 'active' OR identity_org_id IS NOT NULL)
);
CREATE TABLE opentour_tenants.memberships (
    organization_id text NOT NULL REFERENCES opentour_tenants.organizations(id),
    issuer text NOT NULL, subject text NOT NULL,
    membership_id text NOT NULL UNIQUE,
    provider_membership_id text UNIQUE,
    -- Initial-owner constraints only.172 expands these for invitation lifecycle
    -- and member/admin/viewer roles; this is not the final membership interface.
    status text NOT NULL CONSTRAINT initial_owner_status CHECK (status IN ('provisional','active','removed')),
    role text NOT NULL CONSTRAINT initial_owner_role CHECK (role = 'owner'),
    grant_revision bigint NOT NULL CHECK (grant_revision >= 1),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (organization_id, issuer, subject),
    CHECK (status <> 'active' OR provider_membership_id IS NOT NULL)
);
CREATE TABLE opentour_tenants.provisioning_operations (
    id text PRIMARY KEY, organization_id text NOT NULL UNIQUE,
    creator_issuer text NOT NULL, creator_subject text NOT NULL,
    provider_correlation text NOT NULL UNIQUE CHECK (length(provider_correlation) <= 64),
    requested_display_name varchar(120) NOT NULL CHECK (length(requested_display_name) > 0),
    provider_environment text NOT NULL,
    stage text NOT NULL CHECK (stage IN ('organization','membership','activation')),
    stage_outcome text NOT NULL CHECK (stage_outcome IN (
        'not_dispatched','dispatched','unknown','observed','rejected'
    )),
    provider_org_id text, provider_membership_id text,
    last_error text CHECK (last_error IN (
        'PROVIDER_OUTCOME_UNKNOWN','PROVIDER_REJECTED','PROVIDER_IDENTITY_CONFLICT',
        'PROVIDER_MEMBERSHIP_NOT_ACTIVE','SETUP_CANCELLED'
    )),
    dispatch_started_at timestamptz, next_attempt_at timestamptz,
    created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
    FOREIGN KEY (organization_id, creator_issuer, creator_subject)
        REFERENCES opentour_tenants.organizations(id, creator_issuer, creator_subject),
    UNIQUE (id, organization_id),
    UNIQUE (id, creator_issuer, creator_subject),
    CHECK (stage = 'organization' OR provider_org_id IS NOT NULL)
);
CREATE TABLE opentour_tenants.request_dedup (
    issuer text NOT NULL, subject text NOT NULL,
    operation text NOT NULL CHECK (operation IN ('organization.create','setup.retry','setup.cancel')),
    key_sha256 text NOT NULL CHECK (key_sha256 ~ '^[0-9a-f]{64}$'),
    body_sha256 text NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    operation_id text NOT NULL,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (issuer, subject, operation, key_sha256),
    FOREIGN KEY (operation_id, issuer, subject)
        REFERENCES opentour_tenants.provisioning_operations(id, creator_issuer, creator_subject)
);
CREATE TABLE opentour_tenants.outbox (
    operation_id text NOT NULL,
    organization_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 1),
    kind text NOT NULL CHECK (kind IN ('organization.requested','organization.progressed','organization.active','organization.cancelled','organization.reconcile_requested')),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (operation_id, revision, kind),
    FOREIGN KEY (operation_id, organization_id)
        REFERENCES opentour_tenants.provisioning_operations(id, organization_id)
);
CREATE INDEX organizations_creator_created ON opentour_tenants.organizations
    (creator_issuer, creator_subject, created_at);
CREATE INDEX provisioning_reconcile ON opentour_tenants.provisioning_operations
    (next_attempt_at) WHERE stage_outcome IN ('dispatched','unknown');

CREATE FUNCTION opentour_tenants.guard_request_transition()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF pg_has_role(current_user, 'opentour_tenant_runtime', 'MEMBER')
       AND NOT pg_has_role(current_user, 'opentour_provisioner', 'MEMBER')
       AND (OLD.state IN ('active','cancelled') OR NEW.state NOT IN (OLD.state,'cancelled')) THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Request role cannot change setup authority';
    END IF;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION opentour_tenants.guard_request_transition() FROM PUBLIC;
CREATE TRIGGER request_transition_guard BEFORE UPDATE ON opentour_tenants.organizations
    FOR EACH ROW EXECUTE FUNCTION opentour_tenants.guard_request_transition();

-- Only trusted server construction assigns these roles; no request claims choose a role.
REVOKE ALL ON SCHEMA opentour_tenants FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA opentour_tenants FROM PUBLIC;
GRANT USAGE ON SCHEMA opentour_tenants TO opentour_tenant_runtime, opentour_provisioner;
--142's shared advisory lock protects these read-only admission observations.
--No session UPDATE or DELETE permission is granted to the tenant setup role.
GRANT USAGE ON SCHEMA opentour_identity TO opentour_tenant_runtime, opentour_provisioner;
GRANT SELECT ON opentour_identity.sessions, opentour_identity.session_denials TO opentour_tenant_runtime;
GRANT SELECT ON opentour_identity.membership_observations TO opentour_provisioner;
GRANT SELECT ON opentour_tenants.terms_versions, opentour_tenants.terms_publication
    TO opentour_tenant_runtime, opentour_provisioner;
GRANT SELECT, INSERT, UPDATE ON opentour_tenants.admission_locks TO opentour_tenant_runtime;
GRANT SELECT, INSERT ON opentour_tenants.organizations, opentour_tenants.memberships,
    opentour_tenants.provisioning_operations TO opentour_tenant_runtime;
-- The request role can cancel/reconcile its own intent, never bind a provider
-- identity, alter an admitted request or activate the initial owner.
GRANT UPDATE (state, revision, updated_at) ON opentour_tenants.organizations
    TO opentour_tenant_runtime;
GRANT UPDATE (status, updated_at) ON opentour_tenants.memberships TO opentour_tenant_runtime;
GRANT UPDATE (updated_at, next_attempt_at) ON opentour_tenants.provisioning_operations
    TO opentour_tenant_runtime;
GRANT SELECT, INSERT ON opentour_tenants.request_dedup,
    opentour_tenants.outbox TO opentour_tenant_runtime;
GRANT SELECT, UPDATE ON opentour_tenants.organizations,
    opentour_tenants.memberships, opentour_tenants.provisioning_operations TO opentour_provisioner;
GRANT SELECT, INSERT ON opentour_tenants.outbox TO opentour_provisioner;

ALTER TABLE opentour_tenants.admission_locks ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_tenants.admission_locks FORCE ROW LEVEL SECURITY;
CREATE POLICY own_admission ON opentour_tenants.admission_locks TO opentour_tenant_runtime
    USING (issuer = current_setting('opentour.identity_issuer', true)
       AND subject = current_setting('opentour.identity_subject', true));
ALTER TABLE opentour_tenants.organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_tenants.organizations FORCE ROW LEVEL SECURITY;
CREATE POLICY own_organization_read ON opentour_tenants.organizations FOR SELECT TO opentour_tenant_runtime
    USING (creator_issuer = current_setting('opentour.identity_issuer', true)
       AND creator_subject = current_setting('opentour.identity_subject', true));
CREATE POLICY own_organization_insert ON opentour_tenants.organizations FOR INSERT TO opentour_tenant_runtime
    WITH CHECK (creator_issuer = current_setting('opentour.identity_issuer', true)
       AND creator_subject = current_setting('opentour.identity_subject', true)
       AND state = 'identity_provisioning' AND revision = 1 AND identity_org_id IS NULL);
CREATE POLICY own_organization_change ON opentour_tenants.organizations FOR UPDATE TO opentour_tenant_runtime
    USING (creator_issuer = current_setting('opentour.identity_issuer', true)
       AND creator_subject = current_setting('opentour.identity_subject', true)
       AND state NOT IN ('active','cancelled'))
    WITH CHECK (creator_issuer = current_setting('opentour.identity_issuer', true)
       AND creator_subject = current_setting('opentour.identity_subject', true) AND state <> 'active');
CREATE POLICY provision_organization ON opentour_tenants.organizations TO opentour_provisioner
    USING (true);
ALTER TABLE opentour_tenants.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_tenants.memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY own_initial_membership_read ON opentour_tenants.memberships FOR SELECT TO opentour_tenant_runtime
    USING (issuer = current_setting('opentour.identity_issuer', true)
       AND subject = current_setting('opentour.identity_subject', true));
CREATE POLICY own_initial_membership_insert ON opentour_tenants.memberships FOR INSERT TO opentour_tenant_runtime
    WITH CHECK (issuer = current_setting('opentour.identity_issuer', true)
       AND subject = current_setting('opentour.identity_subject', true)
       AND status = 'provisional' AND role = 'owner' AND provider_membership_id IS NULL
       AND EXISTS (SELECT 1 FROM opentour_tenants.organizations org
           WHERE org.id = memberships.organization_id
           AND org.creator_issuer = memberships.issuer AND org.creator_subject = memberships.subject));
CREATE POLICY own_initial_membership_remove ON opentour_tenants.memberships FOR UPDATE TO opentour_tenant_runtime
    USING (issuer = current_setting('opentour.identity_issuer', true)
       AND subject = current_setting('opentour.identity_subject', true))
    WITH CHECK (issuer = current_setting('opentour.identity_issuer', true)
       AND subject = current_setting('opentour.identity_subject', true) AND status = 'removed');
CREATE POLICY provision_membership ON opentour_tenants.memberships TO opentour_provisioner
    USING (true);
ALTER TABLE opentour_tenants.provisioning_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_tenants.provisioning_operations FORCE ROW LEVEL SECURITY;
CREATE POLICY own_operation_read ON opentour_tenants.provisioning_operations FOR SELECT TO opentour_tenant_runtime
    USING (creator_issuer = current_setting('opentour.identity_issuer', true)
       AND creator_subject = current_setting('opentour.identity_subject', true));
CREATE POLICY own_operation_insert ON opentour_tenants.provisioning_operations FOR INSERT TO opentour_tenant_runtime
    WITH CHECK (creator_issuer = current_setting('opentour.identity_issuer', true)
       AND creator_subject = current_setting('opentour.identity_subject', true)
       AND stage = 'organization' AND stage_outcome = 'not_dispatched'
       AND provider_org_id IS NULL AND provider_membership_id IS NULL
       AND last_error IS NULL AND dispatch_started_at IS NULL);
CREATE POLICY own_operation_schedule ON opentour_tenants.provisioning_operations FOR UPDATE TO opentour_tenant_runtime
    USING (creator_issuer = current_setting('opentour.identity_issuer', true)
       AND creator_subject = current_setting('opentour.identity_subject', true));
CREATE POLICY provision_operation ON opentour_tenants.provisioning_operations TO opentour_provisioner
    USING (true);
ALTER TABLE opentour_tenants.request_dedup ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_tenants.request_dedup FORCE ROW LEVEL SECURITY;
CREATE POLICY own_dedup ON opentour_tenants.request_dedup TO opentour_tenant_runtime
    USING (issuer = current_setting('opentour.identity_issuer', true)
       AND subject = current_setting('opentour.identity_subject', true));
ALTER TABLE opentour_tenants.outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_tenants.outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY own_outbox_read ON opentour_tenants.outbox FOR SELECT TO opentour_tenant_runtime
    USING (EXISTS (SELECT 1 FROM opentour_tenants.organizations org WHERE org.id = outbox.organization_id));
CREATE POLICY own_outbox_insert ON opentour_tenants.outbox FOR INSERT TO opentour_tenant_runtime
    WITH CHECK (kind IN ('organization.requested','organization.cancelled','organization.reconcile_requested')
       AND EXISTS (SELECT 1 FROM opentour_tenants.organizations org WHERE org.id = outbox.organization_id));
CREATE POLICY provision_outbox ON opentour_tenants.outbox TO opentour_provisioner USING (true);
-- No runtime DELETE, terms publication, role membership or schema-changing privilege.
COMMIT;
