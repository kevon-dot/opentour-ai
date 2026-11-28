"""Generated issuer, native reservation and control-worker witnesses for one bounded EVE fit."""

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from opentour_cell.pilot import autonomous_cycle, cycle_worker
from opentour_cell.pilot import eve_authority as ea
from opentour_cell.pilot import fit_dispatch as fd
from opentour_cell.pilot import learning_runs as lr
from opentour_cell.pilot import signed_records as sr
from opentour_cell.pilot.eve_exchange import Exchange, digest, run_ref
from opentour_cell.pilot.mandate_fit_ledger import MandateFitLedger
from opentour_cell.pilot.slack_adapter import Journal, _json
from test_learning_runs import OWNER, Site
from test_signed_records import Kit, kit, sign, stamp  # noqa: F401


def private(path: Path, value: bytes):
    path.write_bytes(value)
    path.chmod(0o600)


@pytest.fixture
def approved(tmp_path, kit: Kit):  # noqa: F811
    site = Site(tmp_path)
    site.authorize(ea.SELECTION_FILE.removesuffix(".json"))
    plan = replace(site.plan(), model_route_ref="eve-24b-qlora")
    site.publish(plan)
    now = int(time.time())
    spec = {
        "tenant": site.tenant,
        "engagement_id": site.engagement,
        "workspace": "T-FIXTURE",
        "channel": "C-FIXTURE",
        "reviewer": OWNER,
        "workflow": plan.workflow_ref,
        "dataset": "generated-development",
        "dataset_revision": "1" * 64,
        "mandate_id": plan.mandate_id,
        "mandate_revision": plan.mandate_revision,
        "plan_ref": plan.plan_ref,
        "benchmark_ref": plan.benchmark_ref,
        "reservation_ref": "fitexec-" + "2" * 40,
        "payer_ref": "fixture-payer",
        "host_ref": "fixture-gpu",
        "fit_intent_digest": "3" * 64,
        "max_rows": 12,
        "model_revision": ea.EVE,
        "image_id": "sha256:" + "4" * 64,
        "steps": 8,
        "development_sha256": "5" * 64,
        "protected_sha256": "6" * 64,
        "starts_at": now - 2,
        "deadline": now + 298,
        "max_cents": 25,
        "hourly_cents": 200,
    }
    ledger = MandateFitLedger(site.registry)
    # Fixture for an upstream reservation. The production consumer cannot mint this row.
    record = {
        "execution_id": spec["reservation_ref"],
        "tenant": site.tenant,
        "engagement": site.engagement,
        "actor": "worker",
        "owner_id": OWNER,
        "run_id": "generated-run",
        "payer": spec["payer_ref"],
        "mandate_id": plan.mandate_id,
        "mandate_revision": plan.mandate_revision,
        "budget_scope": "budget-" + digest([site.registry.scope, plan.mandate_id])[:40],
        "intent_digest": spec["fit_intent_digest"],
        "development_digest": spec["development_sha256"],
        "plan_revision": lr.digest(plan.document()),
        "benchmark_revision": plan.benchmark_ref,
        "model": {
            "route": "local_open_weight",
            "immutable_revision": ea.EVE,
            "account_or_host": spec["host_ref"],
        },
        "bound": {
            "currency": "USD",
            "maximum_minor_units": 25,
            "maximum_steps": 8,
            "maximum_seconds": 300,
            "maximum_rows": 12,
        },
    }
    ledger.reserve(record, client="fixture", operation_key="fixture", recheck=lambda _: None)
    payload = {
        "profile_version": "1.0",
        "tenant": site.tenant,
        "engagement_id": site.engagement,
        "issued_at": stamp(-60),
        "not_after": stamp(600),
        "reservation": spec,
    }
    registry = kit.registry()
    private(site.directory / ea.REGISTRY_FILE, registry)
    sr.PilotVerifier(registry).pin_customer_anchor(kit.enrolment(engagement_id=site.engagement))
    private(
        site.directory / ea.CAP_FILE,
        sign(payload, sr.SPEND_MANDATE_PURPOSE, kit.customer, kit.operator),
    )
    return (
        site,
        kit,
        ledger,
        ea.EveAuthority(site.directory, site.tenant, spec["workspace"], spec["channel"]),
        payload,
    )


def test_signed_native_claim_and_installed_worker_renewal(approved, monkeypatch):
    site, _, ledger, authority, payload = approved
    spec = payload["reservation"]
    scope = ea.scope_from(spec)
    with authority.current(scope, write=True) as grant:
        authority.claim(grant)
        authority.claim(grant)
        entry = ledger.get(grant.reservation_ref)
        assert entry.execution.submissions == 1 and entry.execution.charged_minor_units == 25
        assert entry.execution.job_ref == run_ref(grant.reservation_ref, spec)
    exchange = Exchange(site.directory / "eve-exchange")
    journal = Journal(
        site.directory / "slack-pilot.sqlite3",
        _json([scope.organization, scope.workspace, "AAPP", spec["channel"], str(site.directory)]),
    )
    click = journal.opaque(
        "learning_start", [scope.__dict__, grant.plan_ref, "evaluate_and_improve", digest(spec)]
    )
    row = exchange.admit(grant.reservation_ref, spec, actor=scope.actor, decision_key=click)
    assert exchange.get(grant.reservation_ref)["run_ref"] == entry.execution.job_ref
    config = site.directory / "cycle.json"
    private(config, b"{}")
    monkeypatch.setattr(
        autonomous_cycle, "cycle_once", lambda *args: pytest.fail("legacy fit route")
    )
    monkeypatch.setattr(cycle_worker, "learning_tick", lambda *args: pytest.fail("legacy schedule"))
    private(site.directory / lr.SCHEDULE_SELECTION, b"{}")
    assert (
        cycle_worker.run(
            site.directory, site.tenant, config, 300, once=True, stopped=threading.Event()
        )
        == 0
    )
    assert exchange.get(grant.reservation_ref)["permit_until"] > row["permit_until"]
    site.registry.withdraw(spec["mandate_id"], actor=OWNER)
    prior = exchange.get(grant.reservation_ref)["permit_until"]
    ea.renew_host_permit(site.directory, site.tenant)
    assert exchange.get(grant.reservation_ref)["permit_until"] == prior
    (site.directory / ea.SELECTION_FILE).unlink()
    for scheduled in (True, False):
        if not scheduled:
            (site.directory / lr.SCHEDULE_SELECTION).unlink()
        assert (
            cycle_worker.run(
                site.directory, site.tenant, config, 300, once=True, stopped=threading.Event()
            )
            == 0
        )
        assert exchange.get(grant.reservation_ref)["permit_until"] == prior


@pytest.mark.parametrize(
    "change",
    [
        "price",
        "steps",
        "model",
        "evaluator",
        "native_cap",
        "native_input",
        "other_job",
        "workspace",
        "no_reservation",
    ],
)
def test_unapproved_or_replaced_terms_cannot_claim(approved, change):
    site, issuer, ledger, authority, payload = approved
    scope = ea.scope_from(payload["reservation"])
    spec = payload["reservation"]
    signers = (issuer.customer, issuer.operator)
    if change == "evaluator":
        signers = (issuer.evaluator, issuer.operator)
    elif change == "other_job":
        entry = ledger.get(spec["reservation_ref"])
        ledger.transition(
            entry.execution_id,
            version=entry.execution.version,
            execution=replace(entry.execution, stage=fd.Stage.SUBMITTING, job_ref="other"),
            event="generated-test",
        )
    elif change == "workspace":
        scope = replace(scope, workspace="another")
    else:
        key, value = {
            "price": ("hourly_cents", 10000),
            "steps": ("steps", 1),
            "model": ("model_revision", "unknown"),
            "native_cap": ("max_cents", 26),
            "native_input": ("development_sha256", "c" * 64),
            "no_reservation": ("reservation_ref", "not-issued"),
        }[change]
        spec[key] = value
    private(site.directory / ea.CAP_FILE, sign(payload, sr.SPEND_MANDATE_PURPOSE, *signers))
    with pytest.raises((ValueError, sr.PilotRecordRefused)):
        with authority.current(scope, write=True) as grant:
            authority.claim(grant)
    assert not (site.directory / "eve-exchange").exists()


def test_held_or_ambiguous_fit_blocks_another_reservation(tmp_path):
    ex = Exchange(tmp_path.resolve() / "private")
    now = int(time.time())
    spec = {
        "reviewer": "owner",
        "reservation_ref": "one",
        "starts_at": now - 1,
        "deadline": now + 30,
        "steps": 8,
    }
    row = ex.admit("one", spec, actor="owner", decision_key="start-one")
    ex.observe(row, "held")
    with pytest.raises(ValueError, match="eve_host_busy"):
        ex.admit("two", spec | {"reservation_ref": "two"}, actor="owner", decision_key="start-two")
    ex.observe(ex.get("one"), "stopped")
    assert (
        ex.admit("two", spec | {"reservation_ref": "two"}, actor="owner", decision_key="start-two")[
            "state"
        ]
        == "queued"
    )


def test_expired_window_allows_enrolled_read_and_never_allows_a_fit(approved, monkeypatch):
    site, _, _, authority, payload = approved
    spec = payload["reservation"]
    monkeypatch.setattr(ea.time, "time", lambda: spec["deadline"] + 1)
    scope = ea.scope_from(spec)
    with authority.current(scope, write=False) as grant:
        assert not grant.can_control
    with pytest.raises(ValueError, match="eve_window_outside_cap"):
        with authority.current(scope, write=True):
            pytest.fail("expired window admitted an effect")


@pytest.mark.parametrize("withdrawn", [False, True])
def test_running_and_terminal_observations_keep_actual_provider_cost_open(approved, withdrawn):
    site, _, ledger, authority, payload = approved
    spec = payload["reservation"]
    scope = ea.scope_from(spec)
    with authority.current(scope, write=True) as grant:
        authority.claim(grant)
    ex = Exchange(site.directory / "eve-exchange")
    journal = Journal(
        site.directory / "slack-pilot.sqlite3",
        _json([scope.organization, scope.workspace, "AAPP", spec["channel"], str(site.directory)]),
    )
    click = journal.opaque(
        "learning_start", [scope.__dict__, grant.plan_ref, "evaluate_and_improve", digest(spec)]
    )
    job = ex.admit(grant.reservation_ref, spec, actor=scope.actor, decision_key=click)
    ex.observe(job, "running", trainer_id="generated-trainer", step=2, seconds=3)
    ea.renew_host_permit(site.directory, site.tenant)
    assert ledger.get(grant.reservation_ref).execution.stage == fd.Stage.RUNNING
    if withdrawn:
        site.registry.withdraw(spec["mandate_id"], actor=OWNER)
        (site.directory / ea.SELECTION_FILE).unlink()
    ex.observe(ex.get(grant.reservation_ref), "stopped", step=3, seconds=5)
    ea.renew_host_permit(site.directory, site.tenant)
    terminal = ledger.get(grant.reservation_ref).execution
    assert terminal.stage == fd.Stage.HELD and terminal.hold_reason == "usage_unverifiable"
    assert terminal.charged_minor_units == 25 and terminal.settlement is None
    assert ex.unreported() == []


def test_expiry_does_not_free_the_gpu_while_the_real_worker_lease_is_held(tmp_path, monkeypatch):
    # This cross-package fixture loads the same CPU-safe supervisor source baked into the image.
    # The installed control environment intentionally has no trainer distribution or CUDA.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "training" / "src"))
    from opentour_training.training.eve_host import TrainerHost
    from opentour_training.training.eve_run import Run

    root = tmp_path.resolve()
    ex = Exchange(root / "exchange")
    for name in ("model", "inputs", "work"):
        (root / name).mkdir(mode=0o700)
    now = int(time.time())
    image = "sha256:" + "9" * 64
    spec = {
        "reviewer": "owner",
        "reservation_ref": "one",
        "starts_at": now - 1,
        "deadline": now + 30,
        "steps": 8,
        "image_id": image,
    }
    row = ex.admit("one", spec, actor="owner", decision_key="initial")
    assert ex.begin_launch(row)
    path = root / "work" / row["run_ref"]
    path.mkdir(mode=0o700)
    run = Run(path / "trainer")
    run.directory.mkdir(mode=0o700)
    host = TrainerHost(ex, root / "model", root / "inputs", root / "work", image)
    with run.worker() as acquired:
        assert acquired and run.start(
            {"output": str(path / "candidate"), "steps": 8, "deadline": now + 30}
        )
        monkeypatch.setattr(time, "time", lambda: now + 31)
        host.tick()
        assert ex.get("one")["state"] == "stopping"
        next_spec = spec | {"reservation_ref": "two", "deadline": now + 80}
        with pytest.raises(ValueError, match="eve_host_busy"):
            ex.admit("two", next_spec, actor="owner", decision_key="second")
    host.tick()
    assert ex.get("one")["state"] == "expired"
    assert ex.admit("two", next_spec, actor="owner", decision_key="second")["state"] == "queued"
