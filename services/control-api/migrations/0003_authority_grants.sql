-- OPE-143. Apply once under the separate migration owner after0001/0002.
-- No initial-owner policy or effective grant is seeded by this migration.
BEGIN;
CREATE ROLE opentour_grant_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE opentour_authority_coordinator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE SCHEMA opentour_authority;
CREATE TABLE opentour_authority.schema_version (version integer PRIMARY KEY CHECK(version=1));
INSERT INTO opentour_authority.schema_version VALUES(1);

CREATE TABLE opentour_authority.organizations (
    organization_id text PRIMARY KEY REFERENCES opentour_tenants.organizations(id),
    authority_revision text NOT NULL CHECK(length(authority_revision) BETWEEN 1 AND 256),
    revocation_epoch bigint NOT NULL CHECK(revocation_epoch BETWEEN 0 AND 9007199254740991),
    current_operation_id text,
    state text NOT NULL CHECK(state IN ('current','reconciliation_required','unavailable')),
    journal_head jsonb NOT NULL,
    frontier_proof jsonb NOT NULL,
    enrollment_ref jsonb NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE TABLE opentour_authority.requests (
    organization_id text NOT NULL REFERENCES opentour_tenants.organizations(id),
    issuer text NOT NULL, subject text NOT NULL, workspace_id text NOT NULL,
    key_sha256 text NOT NULL CHECK(key_sha256 ~ '^[0-9a-f]{64}$'),
    request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
    body bytea NOT NULL CHECK(octet_length(body) BETWEEN 1 AND 65536),
    request_id text NOT NULL, created_at timestamptz NOT NULL,
    PRIMARY KEY(organization_id,issuer,subject,workspace_id,key_sha256)
);
CREATE TABLE opentour_authority.operations (
    organization_id text NOT NULL REFERENCES opentour_authority.organizations(organization_id),
    operation_id text NOT NULL, grant_id text,
    operation_kind text NOT NULL DEFAULT 'access_grant_create'
        CHECK(operation_kind IN ('access_grant_create','creator_installation')),
    issuer text NOT NULL, subject text NOT NULL, workspace_id text,
    key_sha256 text NOT NULL,
    context jsonb NOT NULL, request_body jsonb NOT NULL,
    plan jsonb NOT NULL, proposed_grant jsonb NOT NULL,
    proposed_authority_revision text NOT NULL,
    proposed_epoch bigint NOT NULL CHECK(proposed_epoch BETWEEN 0 AND 9007199254740991),
    intent_body bytea NOT NULL CHECK(octet_length(intent_body) BETWEEN 1 AND 65536),
    state text NOT NULL CHECK(state IN ('prepared','reconciliation_required','succeeded','aborted')),
    revision bigint NOT NULL CHECK(revision>=1),
    intent_response jsonb, outcome_body bytea, outcome_response jsonb,
    creation_receipt jsonb,
    created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
    PRIMARY KEY(organization_id,operation_id),
    UNIQUE(organization_id,grant_id),
    UNIQUE(organization_id,issuer,subject,workspace_id,key_sha256),
    FOREIGN KEY(organization_id,issuer,subject,workspace_id,key_sha256)
        REFERENCES opentour_authority.requests(organization_id,issuer,subject,workspace_id,key_sha256),
    CHECK((operation_kind='access_grant_create')=(grant_id IS NOT NULL)),
    CHECK((operation_kind='access_grant_create')=(workspace_id IS NOT NULL)),
    CHECK((state='succeeded' AND operation_kind='access_grant_create')
        =(creation_receipt IS NOT NULL)),
    CHECK(outcome_body IS NULL OR octet_length(outcome_body) BETWEEN 1 AND 65536)
);
-- One original birth installation survives retries, changed policies and tombstones.
CREATE UNIQUE INDEX one_creator_installation ON opentour_authority.operations(organization_id)
    WHERE operation_kind='creator_installation';
ALTER TABLE opentour_authority.organizations ADD FOREIGN KEY(organization_id,current_operation_id)
    REFERENCES opentour_authority.operations(organization_id,operation_id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE opentour_authority.grants (
    organization_id text NOT NULL REFERENCES opentour_authority.organizations(organization_id),
    grant_id text NOT NULL, creation_operation_id text NOT NULL,
    body jsonb NOT NULL,
    state text NOT NULL CHECK(state IN ('prepared','active','revoked')),
    revision bigint NOT NULL CHECK(revision>=1),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY(organization_id,grant_id),
    FOREIGN KEY(organization_id,creation_operation_id)
        REFERENCES opentour_authority.operations(organization_id,operation_id)
);
CREATE TABLE opentour_authority.ancestry (
    organization_id text NOT NULL, grant_id text NOT NULL, parent_grant_id text NOT NULL,
    PRIMARY KEY(organization_id,grant_id,parent_grant_id),
    FOREIGN KEY(organization_id,grant_id) REFERENCES opentour_authority.grants(organization_id,grant_id),
    FOREIGN KEY(organization_id,parent_grant_id) REFERENCES opentour_authority.grants(organization_id,grant_id),
    CHECK(grant_id<>parent_grant_id)
);
CREATE TABLE opentour_authority.template_bindings (
    organization_id text NOT NULL, grant_id text NOT NULL,
    template_id text NOT NULL, template_revision bigint NOT NULL CHECK(template_revision>=1),
    template_sha256 text NOT NULL CHECK(template_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY(organization_id,grant_id,template_id,template_revision),
    FOREIGN KEY(organization_id,grant_id) REFERENCES opentour_authority.grants(organization_id,grant_id)
);
CREATE TABLE opentour_authority.sources (
    organization_id text NOT NULL, operation_id text NOT NULL, record_id text NOT NULL,
    kind text NOT NULL CHECK(kind IN ('intent','actor','transaction')),
    revision text NOT NULL CHECK(length(revision) BETWEEN 1 AND 256),
    sha256 text NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'),
    body bytea NOT NULL CHECK(octet_length(body) BETWEEN 1 AND 65536),
    created_at timestamptz NOT NULL,
    PRIMARY KEY(organization_id,record_id),
    UNIQUE(organization_id,operation_id,kind),
    FOREIGN KEY(organization_id,operation_id)
        REFERENCES opentour_authority.operations(organization_id,operation_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE opentour_authority.retries (
    organization_id text NOT NULL, operation_id text NOT NULL,
    phase text NOT NULL CHECK(phase IN ('intent','outcome')),
    body_sha256 text NOT NULL CHECK(body_sha256 ~ '^[0-9a-f]{64}$'),
    original_body bytea NOT NULL CHECK(octet_length(original_body) BETWEEN 1 AND 65536),
    token text NOT NULL, revision bigint NOT NULL CHECK(revision>=1),
    attempt bigint NOT NULL CHECK(attempt BETWEEN 1 AND 9007199254740991),
    state text NOT NULL CHECK(state IN ('unknown','scheduled','completed','held')),
    first_attempt_at timestamptz NOT NULL, not_before timestamptz NOT NULL,
    reason_code text, response jsonb,
    PRIMARY KEY(organization_id,operation_id,phase),
    FOREIGN KEY(organization_id,operation_id)
        REFERENCES opentour_authority.operations(organization_id,operation_id)
);
CREATE TABLE opentour_authority.outbox (
    organization_id text NOT NULL, operation_id text NOT NULL, revision bigint NOT NULL CHECK(revision>=1),
    kind text NOT NULL CHECK(kind IN ('grant.prepared','grant.sql_committed','grant.succeeded',
        'grant.aborted','grant.reconciliation_required','grant.restrictive_followup_required')),
    created_at timestamptz NOT NULL,
    PRIMARY KEY(organization_id,operation_id,revision,kind),
    FOREIGN KEY(organization_id,operation_id)
        REFERENCES opentour_authority.operations(organization_id,operation_id)
);

-- Every request/source record is immutable. No normal role receives DELETE.
CREATE FUNCTION opentour_authority.immutable_record() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Immutable authority record';
END
$$;
CREATE TRIGGER immutable_request BEFORE UPDATE OR DELETE ON opentour_authority.requests
    FOR EACH ROW EXECUTE FUNCTION opentour_authority.immutable_record();
CREATE TRIGGER immutable_source BEFORE UPDATE OR DELETE ON opentour_authority.sources
    FOR EACH ROW EXECUTE FUNCTION opentour_authority.immutable_record();
CREATE TRIGGER immutable_ancestry BEFORE UPDATE OR DELETE ON opentour_authority.ancestry
    FOR EACH ROW EXECUTE FUNCTION opentour_authority.immutable_record();
CREATE TRIGGER immutable_template BEFORE UPDATE OR DELETE ON opentour_authority.template_bindings
    FOR EACH ROW EXECUTE FUNCTION opentour_authority.immutable_record();
CREATE TRIGGER immutable_outbox BEFORE UPDATE OR DELETE ON opentour_authority.outbox
    FOR EACH ROW EXECUTE FUNCTION opentour_authority.immutable_record();

CREATE FUNCTION opentour_authority.guard_operation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF (to_jsonb(NEW)-ARRAY['state','revision','intent_response','outcome_body','outcome_response','creation_receipt','updated_at'])
       IS DISTINCT FROM
       (to_jsonb(OLD)-ARRAY['state','revision','intent_response','outcome_body','outcome_response','creation_receipt','updated_at'])
       OR (OLD.creation_receipt IS NOT NULL AND NEW.creation_receipt IS DISTINCT FROM OLD.creation_receipt)
       OR (OLD.intent_response IS NOT NULL AND NEW.intent_response IS DISTINCT FROM OLD.intent_response)
       OR (OLD.outcome_body IS NOT NULL AND NEW.outcome_body IS DISTINCT FROM OLD.outcome_body)
       OR (OLD.outcome_response IS NOT NULL AND NEW.outcome_response IS DISTINCT FROM OLD.outcome_response)
       OR (OLD.state IN ('succeeded','aborted') AND NEW.state<>OLD.state)
       OR NEW.revision<>OLD.revision+1 THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Invalid authority transition';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER guard_operation BEFORE UPDATE ON opentour_authority.operations
    FOR EACH ROW EXECUTE FUNCTION opentour_authority.guard_operation();
CREATE FUNCTION opentour_authority.guard_grant() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF (NEW.organization_id,NEW.grant_id,NEW.creation_operation_id,NEW.body)
       IS DISTINCT FROM (OLD.organization_id,OLD.grant_id,OLD.creation_operation_id,OLD.body)
       OR OLD.state='revoked'
       OR (NEW.state<>OLD.state AND (OLD.state,NEW.state) NOT IN
           (('prepared','active'),('prepared','revoked'),('active','revoked')))
       OR (NEW.state='revoked' AND NEW.revision<>OLD.revision+1)
       OR (NEW.state<>'revoked' AND NEW.revision<>OLD.revision) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Invalid grant transition';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER guard_grant BEFORE UPDATE ON opentour_authority.grants
    FOR EACH ROW EXECUTE FUNCTION opentour_authority.guard_grant();
CREATE FUNCTION opentour_authority.guard_retry() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF (NEW.organization_id,NEW.operation_id,NEW.phase,NEW.body_sha256,NEW.original_body,NEW.first_attempt_at)
       IS DISTINCT FROM (OLD.organization_id,OLD.operation_id,OLD.phase,OLD.body_sha256,OLD.original_body,OLD.first_attempt_at)
       OR NEW.revision<>OLD.revision+1 OR OLD.state='completed' THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Invalid retry transition';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER guard_retry BEFORE UPDATE ON opentour_authority.retries
    FOR EACH ROW EXECUTE FUNCTION opentour_authority.guard_retry();

-- Fixed minimum-column source helper. Context is set only by trusted server code.
-- The migration owner already owns source tables; no source UPDATE is delegated.
CREATE POLICY grant_lock_organization_owner ON opentour_tenants.organizations TO CURRENT_USER
    USING(id=current_setting('opentour.organization_id',true)
      AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL
      AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL)
    WITH CHECK(false);
CREATE POLICY grant_lock_membership_owner ON opentour_tenants.memberships TO CURRENT_USER
    USING(organization_id=current_setting('opentour.organization_id',true)
      AND issuer=current_setting('opentour.identity_issuer',true)
      AND subject=current_setting('opentour.identity_subject',true)) WITH CHECK(false);
CREATE FUNCTION opentour_authority.locked_membership()
RETURNS TABLE(organization_id text,identity_organization_id text,organization_state text,
    organization_revision bigint,creator_issuer text,creator_subject text,terms_revision text,
    membership_id text,provider_membership_id text,membership_status text,membership_revision bigint,
    identity_issuer text,identity_subject text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE o record; m record; selected_org text; selected_issuer text; selected_subject text;
BEGIN
    selected_org:=nullif(current_setting('opentour.organization_id',true),'');
    selected_issuer:=nullif(current_setting('opentour.identity_issuer',true),'');
    selected_subject:=nullif(current_setting('opentour.identity_subject',true),'');
    IF selected_org IS NULL OR selected_issuer IS NULL OR selected_subject IS NULL THEN
        RETURN;
    END IF;
    SELECT org.id,org.identity_org_id,org.state,org.revision,org.creator_issuer,
           org.creator_subject,org.terms_revision INTO o
      FROM opentour_tenants.organizations org WHERE org.id=selected_org FOR SHARE;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT member.membership_id,member.provider_membership_id,member.status,member.grant_revision
      INTO m FROM opentour_tenants.memberships member
      WHERE member.organization_id=selected_org AND member.issuer=selected_issuer
        AND member.subject=selected_subject FOR SHARE;
    IF NOT FOUND THEN RETURN; END IF;
    RETURN QUERY SELECT o.id,o.identity_org_id,o.state,o.revision,o.creator_issuer,
        o.creator_subject,o.terms_revision,m.membership_id,m.provider_membership_id,m.status,
        m.grant_revision,selected_issuer,selected_subject;
END
$$;
REVOKE ALL ON SCHEMA opentour_authority FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA opentour_authority FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA opentour_authority FROM PUBLIC;
GRANT USAGE ON SCHEMA opentour_authority TO opentour_grant_runtime,opentour_authority_coordinator;
GRANT EXECUTE ON FUNCTION opentour_authority.locked_membership() TO opentour_authority_coordinator;
GRANT SELECT ON ALL TABLES IN SCHEMA opentour_authority TO opentour_authority_coordinator;
GRANT SELECT ON opentour_authority.organizations,opentour_authority.requests,
    opentour_authority.operations,opentour_authority.grants,opentour_authority.ancestry,
    opentour_authority.template_bindings,opentour_authority.schema_version TO opentour_grant_runtime;
GRANT INSERT ON opentour_authority.requests TO opentour_grant_runtime,opentour_authority_coordinator;
GRANT INSERT,UPDATE ON opentour_authority.operations,opentour_authority.grants,
    opentour_authority.retries TO opentour_authority_coordinator;
-- Enrollment is a separate approved policy installation, never ordinary runtime INSERT.
GRANT UPDATE ON opentour_authority.organizations TO opentour_authority_coordinator;
GRANT INSERT ON opentour_authority.organizations TO opentour_authority_coordinator;
GRANT INSERT ON opentour_authority.ancestry,opentour_authority.template_bindings,
    opentour_authority.sources,opentour_authority.outbox TO opentour_authority_coordinator;
-- Owner-read security-barrier projections avoid broad source-table privileges.
-- Existing142 writer policies/lifecycle are unchanged; no session payload is read.
CREATE VIEW opentour_authority.identity_membership WITH(security_barrier=true) AS
SELECT obs.issuer,obs.membership_id,obs.organization_id,obs.subject,obs.status,
       obs.locally_denied,obs.checked_at,obs.observed_at,obs.revision
FROM opentour_identity.membership_observations obs
JOIN opentour_tenants.memberships member ON member.issuer=obs.issuer
    AND member.provider_membership_id=obs.membership_id AND member.subject=obs.subject
JOIN opentour_tenants.organizations org ON org.id=member.organization_id
    AND org.identity_org_id=obs.organization_id
WHERE member.organization_id=current_setting('opentour.organization_id',true)
  AND obs.issuer=current_setting('opentour.identity_issuer',true)
  AND obs.subject=current_setting('opentour.identity_subject',true);
CREATE VIEW opentour_authority.identity_session WITH(security_barrier=true) AS
SELECT s.issuer,s.session_id,s.subject,s.generation,s.selected_organization_id,
       s.selected_membership_id,s.ended_at,s.created_at,s.expires_at,s.reconciliation_required,
       d.deny_until
FROM opentour_identity.sessions s LEFT JOIN opentour_identity.session_denials d
    ON d.issuer=s.issuer AND d.session_id=s.session_id
WHERE (s.selected_organization_id IS NULL
       OR s.selected_organization_id=current_setting('opentour.organization_id',true))
  AND s.issuer=current_setting('opentour.identity_issuer',true)
  AND s.subject=current_setting('opentour.identity_subject',true)
  AND s.session_id=current_setting('opentour.session_id',true);
-- 171 mutates its provisioning result under the canonical organization lock
-- already held by locked_membership(). Only the completed exact tuple is visible.
CREATE POLICY grant_read_provision_owner ON opentour_tenants.provisioning_operations FOR SELECT
    TO CURRENT_USER USING (organization_id=current_setting('opentour.organization_id',true)
      AND creator_issuer=current_setting('opentour.identity_issuer',true)
      AND creator_subject=current_setting('opentour.identity_subject',true));
CREATE VIEW opentour_authority.completed_provisioning WITH(security_barrier=true) AS
SELECT p.id AS provisioning_operation_id,p.organization_id,p.creator_issuer,p.creator_subject,
       p.provider_org_id,p.provider_membership_id
FROM opentour_tenants.provisioning_operations p
WHERE p.organization_id=current_setting('opentour.organization_id',true)
  AND p.creator_issuer=current_setting('opentour.identity_issuer',true)
  AND p.creator_subject=current_setting('opentour.identity_subject',true)
  AND p.stage='activation' AND p.stage_outcome='observed' AND p.last_error IS NULL;
REVOKE ALL ON opentour_authority.identity_membership,opentour_authority.identity_session,
    opentour_authority.completed_provisioning FROM PUBLIC;
GRANT SELECT ON opentour_authority.identity_membership,opentour_authority.identity_session,
    opentour_authority.completed_provisioning
    TO opentour_authority_coordinator;
ALTER TABLE opentour_authority.organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_authority.organizations FORCE ROW LEVEL SECURITY;
CREATE POLICY coordinator_tenant ON opentour_authority.organizations TO opentour_authority_coordinator USING (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL) WITH CHECK (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL);
CREATE POLICY request_tenant ON opentour_authority.organizations FOR SELECT TO opentour_grant_runtime USING (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL);
ALTER TABLE opentour_authority.requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_authority.requests FORCE ROW LEVEL SECURITY;
CREATE POLICY coordinator_tenant ON opentour_authority.requests TO opentour_authority_coordinator USING (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL) WITH CHECK (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL);
CREATE POLICY request_tenant ON opentour_authority.requests FOR SELECT TO opentour_grant_runtime USING (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL AND issuer=current_setting('opentour.identity_issuer',true) AND subject=current_setting('opentour.identity_subject',true));
CREATE POLICY request_submission ON opentour_authority.requests FOR INSERT TO opentour_grant_runtime WITH CHECK (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL AND issuer=current_setting('opentour.identity_issuer',true) AND subject=current_setting('opentour.identity_subject',true));
ALTER TABLE opentour_authority.operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_authority.operations FORCE ROW LEVEL SECURITY;
CREATE POLICY coordinator_tenant ON opentour_authority.operations TO opentour_authority_coordinator USING (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL) WITH CHECK (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL);
CREATE POLICY request_tenant ON opentour_authority.operations FOR SELECT TO opentour_grant_runtime USING (operation_kind='access_grant_create' AND organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL AND issuer=current_setting('opentour.identity_issuer',true) AND subject=current_setting('opentour.identity_subject',true));
ALTER TABLE opentour_authority.grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_authority.grants FORCE ROW LEVEL SECURITY;
CREATE POLICY coordinator_tenant ON opentour_authority.grants TO opentour_authority_coordinator USING (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL) WITH CHECK (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL);
CREATE POLICY request_tenant ON opentour_authority.grants FOR SELECT TO opentour_grant_runtime USING (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL);
ALTER TABLE opentour_authority.ancestry ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_authority.ancestry FORCE ROW LEVEL SECURITY;
CREATE POLICY coordinator_tenant ON opentour_authority.ancestry TO opentour_authority_coordinator USING (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL) WITH CHECK (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL);
CREATE POLICY request_tenant ON opentour_authority.ancestry FOR SELECT TO opentour_grant_runtime USING (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL);
ALTER TABLE opentour_authority.template_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_authority.template_bindings FORCE ROW LEVEL SECURITY;
CREATE POLICY coordinator_tenant ON opentour_authority.template_bindings TO opentour_authority_coordinator USING (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL) WITH CHECK (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL);
CREATE POLICY request_tenant ON opentour_authority.template_bindings FOR SELECT TO opentour_grant_runtime USING (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL);
ALTER TABLE opentour_authority.sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_authority.sources FORCE ROW LEVEL SECURITY;
CREATE POLICY coordinator_tenant ON opentour_authority.sources TO opentour_authority_coordinator USING (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL) WITH CHECK (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL);
ALTER TABLE opentour_authority.retries ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_authority.retries FORCE ROW LEVEL SECURITY;
CREATE POLICY coordinator_tenant ON opentour_authority.retries TO opentour_authority_coordinator USING (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL) WITH CHECK (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL);
ALTER TABLE opentour_authority.outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_authority.outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY coordinator_tenant ON opentour_authority.outbox TO opentour_authority_coordinator USING (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL) WITH CHECK (organization_id=current_setting('opentour.organization_id',true) AND nullif(current_setting('opentour.identity_issuer',true),'') IS NOT NULL AND nullif(current_setting('opentour.identity_subject',true),'') IS NOT NULL);
COMMIT;
