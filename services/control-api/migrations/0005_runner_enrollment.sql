-- OPE-184 local candidate. Apply only after the serialized 0004 session migration.
-- The migration does not assign any application role or deployment authority.
BEGIN;
CREATE SCHEMA opentour_enrollment;

CREATE TABLE opentour_enrollment.challenges (
    organization_id text NOT NULL,
    environment_id text NOT NULL,
    challenge_id text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('initial', 'rotation', 'recovery')),
    environment_revision bigint NOT NULL CHECK (environment_revision > 0),
    identity_epoch bigint NOT NULL CHECK (identity_epoch BETWEEN 0 AND 9007199254740991),
    installation_sha256 text NOT NULL CHECK (installation_sha256 ~ '^[0-9a-f]{64}$'),
    installed_scope_sha256 text NOT NULL CHECK (installed_scope_sha256 ~ '^[0-9a-f]{64}$'),
    nonce_sha256 text NOT NULL CHECK (nonce_sha256 ~ '^[0-9a-f]{64}$'),
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > issued_at),
    closed_at timestamptz CHECK (closed_at >= issued_at),
    prepared_operation_id text,
    prepared_actor_id text,
    prepared_proof_sha256 text CHECK (prepared_proof_sha256 ~ '^[0-9a-f]{64}$'),
    prepared_at timestamptz CHECK (prepared_at >= issued_at AND prepared_at < expires_at),
    CHECK (num_nonnulls(prepared_operation_id, prepared_actor_id, prepared_proof_sha256, prepared_at) IN (0, 4)),
    UNIQUE (organization_id, environment_id, prepared_operation_id),
    PRIMARY KEY (organization_id, environment_id, challenge_id)
);
CREATE UNIQUE INDEX one_open_enrollment_challenge
    ON opentour_enrollment.challenges (organization_id, environment_id) WHERE closed_at IS NULL;

CREATE TABLE opentour_enrollment.runners (
    organization_id text NOT NULL,
    environment_id text NOT NULL,
    runner_id text NOT NULL,
    cell_id text NOT NULL,
    identity_epoch bigint NOT NULL CHECK (identity_epoch BETWEEN 1 AND 9007199254740991),
    bound_role_id text NOT NULL,
    key_x text NOT NULL CHECK (key_x ~ '^[A-Za-z0-9_-]{43}$'),
    key_y text NOT NULL CHECK (key_y ~ '^[A-Za-z0-9_-]{43}$'),
    key_thumbprint text NOT NULL CHECK (key_thumbprint ~ '^[A-Za-z0-9_-]{43}$'),
    PRIMARY KEY (organization_id, environment_id),
    UNIQUE (runner_id)
);

CREATE TABLE opentour_enrollment.receipts (
    organization_id text NOT NULL,
    environment_id text NOT NULL,
    challenge_id text NOT NULL,
    operation_id text NOT NULL,
    actor_id text NOT NULL,
    proof_sha256 text NOT NULL CHECK (proof_sha256 ~ '^[0-9a-f]{64}$'),
    runner_id text NOT NULL,
    cell_id text NOT NULL,
    identity_epoch bigint NOT NULL CHECK (identity_epoch BETWEEN 1 AND 9007199254740991),
    bound_role_id text NOT NULL,
    key_thumbprint text NOT NULL CHECK (key_thumbprint ~ '^[A-Za-z0-9_-]{43}$'),
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (organization_id, environment_id, challenge_id),
    UNIQUE (organization_id, environment_id, operation_id),
    FOREIGN KEY (organization_id, environment_id, challenge_id)
        REFERENCES opentour_enrollment.challenges (organization_id, environment_id, challenge_id)
);

-- The exact receipt is the local event source. The publication contract is composed later.
CREATE TABLE opentour_enrollment.outbox (
    organization_id text NOT NULL,
    environment_id text NOT NULL,
    challenge_id text NOT NULL,
    environment_revision bigint NOT NULL CHECK (environment_revision > 0),
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (organization_id, environment_id, challenge_id),
    FOREIGN KEY (organization_id, environment_id, challenge_id)
        REFERENCES opentour_enrollment.receipts (organization_id, environment_id, challenge_id)
);

CREATE FUNCTION opentour_enrollment.prevent_enrollment_history_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Immutable enrollment history';
END
$$;
CREATE TRIGGER immutable_receipt BEFORE UPDATE OR DELETE ON opentour_enrollment.receipts
    FOR EACH ROW EXECUTE FUNCTION opentour_enrollment.prevent_enrollment_history_change();
CREATE TRIGGER immutable_enrollment_event BEFORE UPDATE OR DELETE ON opentour_enrollment.outbox
    FOR EACH ROW EXECUTE FUNCTION opentour_enrollment.prevent_enrollment_history_change();

DO $$
DECLARE scoped_table text;
BEGIN
    FOREACH scoped_table IN ARRAY ARRAY['challenges','runners','receipts','outbox'] LOOP
        EXECUTE format('ALTER TABLE opentour_enrollment.%I ENABLE ROW LEVEL SECURITY', scoped_table);
        EXECUTE format('ALTER TABLE opentour_enrollment.%I FORCE ROW LEVEL SECURITY', scoped_table);
        EXECUTE format(
            'CREATE POLICY same_organization ON opentour_enrollment.%I USING '
            || '(organization_id = current_setting(''opentour.organization_id'', true))', scoped_table
        );
    END LOOP;
END
$$;
REVOKE ALL ON SCHEMA opentour_enrollment FROM PUBLIC;
COMMIT;
