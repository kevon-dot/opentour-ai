"""Generated source intake, installed watcher and native priced approval fence."""

import json
import threading

import pytest
from opentour_cell.pilot import autonomous_cycle, cycle_worker, eve_authority, eve_scan
from opentour_cell.pilot import fit_dispatch as fd
from opentour_cell.pilot import signed_records as sr
from opentour_cell.pilot.eve_exchange import digest
from opentour_cell.pilot.slack_adapter import Journal, _json
from test_autonomous_cycle import Customer
from test_eve_authority import approved, private  # noqa: F401
from test_signed_records import kit, sign, stamp  # noqa: F401


@pytest.fixture
def watcher(approved):  # noqa: F811
    site, issuer, ledger, authority, payload = approved
    spec = payload["reservation"]
    scope = {
        k: spec[k]
        for k in (
            "tenant",
            "engagement_id",
            "workspace",
            "channel",
            "reviewer",
            "workflow",
            "mandate_id",
        )
    }
    scope.update(
        profile_version="1.0",
        kind="eve_source_discovery",
        connection_revision="1" * 64,
        issued_at=stamp(-60),
        not_after=stamp(600),
    )
    private(
        site.directory / eve_scan.DISCOVERY_FILE,
        sign(scope, sr.GRANT_PURPOSE, issuer.customer, issuer.operator),
    )
    service = eve_scan.EveScanService(
        site.directory, site.tenant, spec["workspace"], spec["channel"]
    )
    journal = Journal(
        site.directory / "slack-pilot.sqlite3",
        _json([site.tenant, spec["workspace"], "AFIX", spec["channel"], str(site.directory)]),
    )

    def click(action):
        revision = service.status(spec["reviewer"])["revision"]
        key = journal.opaque(
            "eve_scan",
            [site.tenant, spec["workspace"], spec["channel"], spec["reviewer"], action, revision],
        )
        return service.act(spec["reviewer"], action, revision, key)

    return service, click, site, issuer, ledger, authority, payload


def test_normal_entry_waits_coalesces_and_fences_native_paid_start(watcher, monkeypatch, capsys):
    scanner, click, site, issuer, ledger, authority, payload = watcher
    spec = payload["reservation"]
    scope = eve_authority.scope_from(spec)
    config = site.directory / "cycle.json"
    private(config, b"{}")
    monkeypatch.setattr(autonomous_cycle, "cycle_once", lambda *_: pytest.fail("legacy fit"))
    monkeypatch.setattr(cycle_worker, "learning_tick", lambda *_: pytest.fail("legacy scheduler"))
    snapshots = []

    def intake(*args):
        snapshots.append("a" * 64 if len(snapshots) < 2 else "b" * 64)
        return snapshots[-1]

    monkeypatch.setattr(eve_scan, "discover_sources", intake)

    def tick():
        result = cycle_worker.run(
            site.directory, site.tenant, config, 300, once=True, stopped=threading.Event()
        )
        return result, json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    def stage(source, generation):
        spec.update(source_snapshot=source, scan_generation=generation)
        private(
            site.directory / eve_authority.CAP_FILE,
            sign(payload, sr.SPEND_MANDATE_PURPOSE, issuer.customer, issuer.operator),
        )

    def claim():
        with authority.current(scope, write=True) as grant:
            authority.claim(grant)

    assert tick()[1]["state"] == "ready" and snapshots == []
    g = click("start_scan")["generation"]
    assert tick()[1]["source_revision"] == "a" * 64
    assert tick()[1]["state"] == "watching" and len(snapshots) == 1
    stage("a" * 64, g)
    with authority.current(scope, write=False) as current:
        assert current.can_control
    assert click("done_for_now")["state"] == "parked"
    assert tick()[1]["state"] == "parked" and len(snapshots) == 1
    with pytest.raises(ValueError):
        claim()
    assert ledger.get(spec["reservation_ref"]).execution.stage == fd.Stage.RESERVED
    next_g = click("start_scan")["generation"]
    assert tick()[1]["source_revision"] == "a" * 64
    with pytest.raises(ValueError):
        claim()
    stage("a" * 64, next_g)
    click("another_scan")
    assert tick()[1]["source_revision"] == "b" * 64
    with pytest.raises(ValueError):
        claim()
    stage("b" * 64, next_g)
    claim()
    claim()
    entry = ledger.get(spec["reservation_ref"]).execution
    assert entry.submissions == 1 and entry.stage == fd.Stage.SUBMITTING
    with pytest.raises(ValueError):
        scanner.check_proposal(spec | {"reservation_ref": "another-job"}, claim=True)
    with scanner.db() as db:
        assert db.execute("SELECT count(*) FROM seen").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM proposals").fetchone()[0] == 1
    (site.directory / eve_scan.DISCOVERY_FILE).unlink()
    assert tick()[0] == 2 and len(snapshots) == 3


def test_done_during_read_fences_result_across_worker_restart(watcher):
    scanner, click, site, _, ledger, _, payload = watcher
    click("start_scan")

    def in_flight(*args):
        assert click("done_for_now")["state"] == "parked"
        return "e" * 64

    assert scanner.tick(site.directory / "cycle.json", 300, in_flight)["state"] == "parked"
    spec = payload["reservation"]
    restarted = eve_scan.open_eve_scan_service(
        site.directory, site.tenant, spec["workspace"], spec["channel"]
    )
    assert restarted.status(spec["reviewer"])["source_revision"] is None
    assert (
        restarted.tick(site.directory / "cycle.json", 300, lambda *_: pytest.fail("parked"))[
            "state"
        ]
        == "parked"
    )
    assert ledger.get(spec["reservation_ref"]).execution.submissions == 0


def test_private_native_stdio_reader_binds_trace_and_correction_without_dispatch(
    tmp_path, monkeypatch
):
    site = Customer(tmp_path, real_reader=True)
    scope = {
        "workflow": site.scope["workflow"],
        "engagement_id": site.eng,
        "mandate_id": site.mandate,
        "connection_revision": site.agreement["connection_revision"],
    }
    monkeypatch.setattr(
        autonomous_cycle, "_execute", lambda *_: pytest.fail("source scan did a fit")
    )
    revision = eve_scan.discover_sources(site.owner, site.org, site.owner / "cycle.json", scope)
    assert isinstance(revision, str) and len(revision) == 64
    assert (
        eve_scan.discover_sources(site.owner, site.org, site.owner / "cycle.json", scope)
        == revision
    )
    assert site.ledger.inspect(site.mandate, actor=site.delegate)["cumulative_runs"] == 1
    with pytest.raises(ValueError):
        eve_scan.discover_sources(
            site.owner,
            site.org,
            site.owner / "cycle.json",
            scope | {"connection_revision": digest("other")},
        )
