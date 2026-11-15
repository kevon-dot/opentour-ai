"""Generated actual-SQL checks; production tenant session and migrations belong to OPE-144."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

from tools.fixtures.postgres import DisposablePostgres
from tools.fixtures.registry import MODES, digest

SCHEMA = """
CREATE ROLE c03_runtime LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA c03 AUTHORIZATION c03_owner;
GRANT USAGE ON SCHEMA c03 TO c03_runtime;
CREATE TABLE c03.workspaces (
 organization_id text NOT NULL, id text NOT NULL,
 PRIMARY KEY(organization_id,id)
);
CREATE TABLE c03.resources (
 organization_id text NOT NULL, id text NOT NULL, workspace_id text NOT NULL,
 revision bigint NOT NULL CHECK(revision>0), value text NOT NULL,
 PRIMARY KEY(organization_id,id),
 FOREIGN KEY(organization_id,workspace_id) REFERENCES c03.workspaces(organization_id,id)
);
CREATE TABLE c03.outbox (
 organization_id text NOT NULL, event_id text NOT NULL, resource_id text NOT NULL,
 revision bigint NOT NULL,
 PRIMARY KEY(organization_id,event_id),
 FOREIGN KEY(organization_id,resource_id) REFERENCES c03.resources(organization_id,id)
);
ALTER TABLE c03.workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE c03.workspaces FORCE ROW LEVEL SECURITY;
ALTER TABLE c03.resources ENABLE ROW LEVEL SECURITY;
ALTER TABLE c03.resources FORCE ROW LEVEL SECURITY;
ALTER TABLE c03.outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE c03.outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_workspaces ON c03.workspaces
 USING(organization_id = nullif(current_setting('opentour.tenant',true),''))
 WITH CHECK(organization_id = nullif(current_setting('opentour.tenant',true),''));
CREATE POLICY tenant_resources ON c03.resources
 USING(organization_id = nullif(current_setting('opentour.tenant',true),''))
 WITH CHECK(organization_id = nullif(current_setting('opentour.tenant',true),''));
CREATE POLICY tenant_outbox ON c03.outbox
 USING(organization_id = nullif(current_setting('opentour.tenant',true),''))
 WITH CHECK(organization_id = nullif(current_setting('opentour.tenant',true),''));
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA c03 TO c03_runtime;
"""


def setup_schema(pg: DisposablePostgres) -> None:
    pg.sql(SCHEMA, role="c03_owner")
    rows = pg.sql("""
        SELECT current_user, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
        FROM pg_roles WHERE rolname=current_user;
        SELECT pg_has_role(current_user,'c03_owner','MEMBER');
        SELECT bool_and(c.relrowsecurity AND c.relforcerowsecurity
                        AND c.relowner<>current_user::regrole)
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='c03' AND c.relkind='r';
    """)
    assert rows.splitlines() == ["c03_runtime|f|f|f|f", "f", "t"], rows


def _tenant(mode: str, suffix: str) -> str:
    if mode not in MODES or suffix not in {"a", "b"}:
        raise ValueError("Only registered generated SQL identities are accepted")
    return f"simulated:{mode}:{suffix}"


def tenant_checks(pg: DisposablePostgres, mode: str) -> dict[str, Any]:
    a, b = _tenant(mode, "a"), _tenant(mode, "b")
    # Identical resource IDs across tenants, deliberately different generated content.
    pg.sql(
        f"""
        INSERT INTO c03.workspaces VALUES('{a}','workspace-a'),('{b}','workspace-b');
        INSERT INTO c03.resources VALUES
            ('{a}','same-visible-id','workspace-a',9,'sentinel-a'),
            ('{b}','same-visible-id','workspace-b',9,'sentinel-b');
    """,
        role="c03_owner",
    )
    observed = pg.sql(f"""
        SELECT count(*) FROM c03.resources;
        BEGIN; SET LOCAL opentour.tenant='{a}';
        SELECT value FROM c03.resources; COMMIT;
        SELECT count(*) FROM c03.resources;
        BEGIN; SET LOCAL opentour.tenant='{b}';
        SELECT value FROM c03.resources; ROLLBACK;
        SELECT count(*) FROM c03.resources;
        BEGIN; SET LOCAL opentour.tenant='{a}';
        DO $$BEGIN
          BEGIN PERFORM 1/0; EXCEPTION WHEN division_by_zero THEN NULL; END;
        END$$;
        SELECT value FROM c03.resources; COMMIT;
        SELECT count(*) FROM c03.resources;
    """)
    assert observed.splitlines() == ["0", "sentinel-a", "0", "sentinel-b", "0", "sentinel-a", "0"]
    # Explicit error/timeout recovery in one physical connection. ON_ERROR_STOP is disabled
    # only for these two expected errors, then immediately restored. Output verifies reset.
    recovered = pg.sql(f"""
        BEGIN; SET LOCAL opentour.tenant='{a}';
        \\set ON_ERROR_STOP off
        SELECT 1/0;
        \\echo :SQLSTATE
        \\set ON_ERROR_STOP on
        ROLLBACK;
        SELECT count(*) FROM c03.resources;
        BEGIN; SET LOCAL opentour.tenant='{b}'; SET LOCAL statement_timeout=10;
        \\set ON_ERROR_STOP off
        SELECT pg_sleep(0.05);
        \\echo :SQLSTATE
        \\set ON_ERROR_STOP on
        ROLLBACK;
        SELECT count(*) FROM c03.resources;
        BEGIN; SET LOCAL opentour.tenant='{a}'; SELECT value FROM c03.resources; COMMIT;
    """)
    assert recovered.splitlines() == ["22012", "0", "57014", "0", "sentinel-a"]
    refused = pg.sql(f"""
        BEGIN; SET LOCAL opentour.tenant='{a}';
        DO $$ BEGIN
          BEGIN
            INSERT INTO c03.resources VALUES('{a}','foreign-fk','workspace-b',1,'generated');
            RAISE EXCEPTION 'Cross-tenant composite reference accepted';
          EXCEPTION WHEN foreign_key_violation THEN NULL;
          END;
        END $$;
        SELECT 'foreign_fk_refused'; COMMIT;
    """)
    assert refused == "foreign_fk_refused"
    return {
        "fixture_id": "postgres-tenant-isolation",
        "fixture_version": "1",
        "mode": mode,
        "kind": "local_integration",
        "runtime_role": "c03_runtime",
        "unfiltered_query_rows_sha256": digest(observed.splitlines()),
        "connection_recovery_sha256": digest(recovered.splitlines()),
        "foreign_composite_reference_refused": True,
        "product_acceptance": "NOT_RUN",
    }


def transaction_checks(pg: DisposablePostgres, mode: str) -> dict[str, Any]:
    a = _tenant(mode, "a")
    # Before commit, update and outbox are rolled back as one transaction.
    pg.sql(f"""
        BEGIN; SET LOCAL opentour.tenant='{a}';
        UPDATE c03.resources SET revision=10 WHERE id='same-visible-id' AND revision=9;
        INSERT INTO c03.outbox VALUES('{a}','before-commit','same-visible-id',10);
        ROLLBACK;
    """)
    before = pg.sql(f"""
        BEGIN; SET LOCAL opentour.tenant='{a}';
        SELECT revision FROM c03.resources WHERE id='same-visible-id';
        SELECT count(*) FROM c03.outbox; COMMIT;
    """)
    assert before.splitlines() == ["9", "0"]
    barrier = Barrier(2, timeout=5)

    def contender(event: str) -> str:
        barrier.wait()
        return pg.sql(f"""
            BEGIN; SET LOCAL opentour.tenant='{a}';
            WITH changed AS (
              UPDATE c03.resources SET revision=revision+1
              WHERE id='same-visible-id' AND revision=9 RETURNING id,revision
            ) INSERT INTO c03.outbox
              SELECT '{a}','{event}',id,revision FROM changed RETURNING revision;
            COMMIT;
        """)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(contender, event) for event in ("attempt-1", "attempt-2")]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(results) == ["", "10"]
    # A fresh connection after acknowledged local commit recovers its durable result.
    after = pg.sql(f"""
        BEGIN; SET LOCAL opentour.tenant='{a}';
        SELECT revision FROM c03.resources WHERE id='same-visible-id';
        SELECT count(*) FROM c03.outbox; COMMIT;
    """)
    assert after.splitlines() == ["10", "1"]
    return {
        "fixture_id": "postgres-transaction-recovery",
        "fixture_version": "1",
        "mode": mode,
        "kind": "local_integration",
        "before_commit_rollback": True,
        "concurrent_mutations_accepted": 1,
        "after_commit_recovered_revision": "10",
        "outbox_records": 1,
        "product_acceptance": "NOT_RUN",
    }
