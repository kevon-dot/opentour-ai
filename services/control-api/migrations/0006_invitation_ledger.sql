-- OPE-172 pending invitation storage. Runtime authority and transitions are separate.
BEGIN;

CREATE FUNCTION opentour_tenants.invitation_scope_sha256(workspace_ids text[])
RETURNS bytea
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
    SELECT CASE
        WHEN cardinality(workspace_ids) = 0 THEN sha256(convert_to('[]', 'UTF8'))
        WHEN cardinality(workspace_ids) <= 100
            AND array_ndims(workspace_ids) = 1 AND array_lower(workspace_ids, 1) = 1
        THEN (
            SELECT CASE
                WHEN count(*) = count(DISTINCT workspace_id COLLATE "C")
                    AND bool_and(length(workspace_id) BETWEEN 1 AND 256)
                THEN sha256(convert_to(json_agg(workspace_id ORDER BY workspace_id COLLATE "C")::text, 'UTF8'))
            END
            FROM unnest(workspace_ids) AS scope(workspace_id)
        )
    END
$$;
REVOKE ALL ON FUNCTION opentour_tenants.invitation_scope_sha256(text[]) FROM PUBLIC;

CREATE TABLE opentour_tenants.invitations (
    id text NOT NULL CHECK (length(id) BETWEEN 1 AND 256),
    organization_id text NOT NULL CHECK (length(organization_id) BETWEEN 1 AND 256)
        REFERENCES opentour_tenants.organizations(id),
    -- The issuer trims and lowercases using the same rules as WorkOS before writing.
    -- Reject ASCII uppercase independently of database locale.
    normalized_recipient text COLLATE "C" NOT NULL CHECK (
        length(normalized_recipient) BETWEEN 1 AND 254
        AND normalized_recipient = btrim(normalized_recipient)
        AND normalized_recipient !~ '[[:cntrl:]]'
        AND normalized_recipient !~ '[A-Z]'
    ),
    token_hash text COLLATE "C" NOT NULL UNIQUE CHECK (token_hash ~ '^[a-f0-9]{64}$'),
    status text COLLATE "C" NOT NULL DEFAULT 'pending' CHECK (status = 'pending'),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (
        CASE WHEN isfinite(created_at) AND isfinite(expires_at)
            THEN expires_at - created_at BETWEEN interval '900 seconds' AND interval '604800 seconds'
            ELSE false
        END
    ),
    role text COLLATE "C" NOT NULL CHECK (role IN ('member', 'admin', 'viewer')),
    workspace_ids text[] NOT NULL CHECK (opentour_tenants.invitation_scope_sha256(workspace_ids) IS NOT NULL),
    creator_issuer text NOT NULL,
    creator_subject text NOT NULL,
    accepted_issuer text,
    accepted_subject text,
    PRIMARY KEY (organization_id, id),
    FOREIGN KEY (organization_id, creator_issuer, creator_subject)
        REFERENCES opentour_tenants.memberships(organization_id, issuer, subject),
    FOREIGN KEY (organization_id, accepted_issuer, accepted_subject)
        REFERENCES opentour_tenants.memberships(organization_id, issuer, subject),
    CHECK ((accepted_issuer IS NULL) = (accepted_subject IS NULL)),
    CHECK (status <> 'pending' OR accepted_issuer IS NULL)
);

-- A pending row keeps its reservation until a later migration admits a transition.
CREATE UNIQUE INDEX invitations_pending_scope_unique ON opentour_tenants.invitations
    (organization_id COLLATE "C", normalized_recipient, opentour_tenants.invitation_scope_sha256(workspace_ids))
    WHERE status = 'pending';

REVOKE ALL ON opentour_tenants.invitations FROM PUBLIC;
ALTER TABLE opentour_tenants.invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE opentour_tenants.invitations FORCE ROW LEVEL SECURITY;

COMMIT;
