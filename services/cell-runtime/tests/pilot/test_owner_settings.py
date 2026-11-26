"""Generated private setup, real signatures/catalog reader and normal worker entry."""

from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest
from opentour_cell.pilot import autonomous_cycle, repair_bundle
from opentour_cell.pilot import owner_settings as settings
from opentour_cell.pilot.learning_mandate import LearningMandates, MandateError
from opentour_cell.pilot.operating_point import canonical, digest
from test_autonomous_cycle import Customer, read, save


class Setup:
    def __init__(self, path: Path, *, model: bool = False):
        self.customer = c = Customer(path, real_reader=True, model=model)
        self.owner, self.operator, self.reviewer = "generated-owner", c.delegate, "UREVIEWER"
        self.private = path / "custodian-private.pem"
        self.public = c.owner / "settings-public.pem"
        repair_bundle._crypto(
            [
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(self.private),
            ],
            b"",
            "generated_key_failed",
        )
        self.private.chmod(0o600)
        repair_bundle._crypto(
            ["pkey", "-in", str(self.private), "-pubout", "-out", str(self.public)],
            b"",
            "generated_key_failed",
        )
        self.public.chmod(0o600)
        self.authority = c.owner / "settings-authority.json"
        save(
            self.authority,
            {
                "schema_version": settings.AUTHORITY,
                "organization": c.org,
                "engagement": c.eng,
                "custodian": self.owner,
                "operator": self.operator,
                "reviewers": [self.reviewer],
                "public_key_file": self.public.name,
                "public_key_sha256": repair_bundle._sha(self.public.read_bytes()),
                "expires_at": int(time.time()) + 3600,
                "revoked": False,
                "initial_mandate": c.mandate,
                "workflow_owner": self.owner,
            },
        )
        self.service = settings.OwnerSettings(c.owner, c.org, c.eng, authority_file=self.authority)
        self.policy = {
            "mandate": dict(c.scope, mandate_id="settings-one"),
            "agreement": c.agreement,
            "connection_file": c.config["connection_file"],
            "currency": "USD",
        }

    def preview(self, policy=None, *, action="save"):
        return self.service.preview(
            self.policy if policy is None and action in {"save", "renew"} else policy,
            actor=self.owner,
            action=action,
        )

    def sign(self, claim):
        signature = repair_bundle._crypto(
            ["dgst", "-sha256", "-sign", str(self.private)],
            canonical(claim).encode(),
            "generated_sign_failed",
        )
        return {"claim": claim, "signature": base64.b64encode(signature).decode()}

    def apply(self, policy=None, *, action="save"):
        return self.service.save(
            self.sign(self.preview(policy, action=action)["claim"]), actor=self.owner
        )

    def protocol(self):
        path = self.customer.owner / "generated-protocol-log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_signed_preview_retry_roles_and_real_worker(tmp_path):
    s = Setup(tmp_path)
    preview = s.preview()
    assert preview["prior"] is None
    assert preview["claim"]["prior_mandate"] == s.customer.mandate
    assert preview["claim"]["binding"]["data_policy"]["exclusions"] == settings.EXCLUSIONS
    assert not any(p["method"] == "resources/read" for p in s.protocol())
    packet = s.sign(preview["claim"])
    first = s.service.save(packet, actor=s.owner)
    assert first == s.service.save(packet, actor=s.owner)
    assert s.customer.ledger.inspect(s.customer.mandate)["withdrawn"]
    assert s.service.inspect(actor=s.reviewer)["current"] == first
    with pytest.raises(settings.SettingsError, match="operator_required"):
        s.service.worker_config(first, actor=s.reviewer)
    handoff = s.service.worker_config(first, actor=s.operator)
    assert handoff == s.service.worker_config(first, actor=s.operator)
    # Actual cycle_worker entry, real separate-process generated MCP reader, no seam replacement.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.cycle_worker",
            "--directory",
            str(s.customer.owner),
            "--organization",
            s.customer.org,
            "--config",
            handoff["config_file"],
            "--once",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    outcome = json.loads(proc.stdout)
    assert outcome["state"] == "pending_slack_review", outcome
    assert any(p["method"] == "resources/read" for p in s.protocol())
    assert s.service.reconcile(first, actor=s.owner)["withdrawn"] is True


@pytest.mark.parametrize("actor", ["generated-worker", "UREVIEWER", "forged-owner"])
def test_wrong_roles_cannot_preview_or_save_before_provider(tmp_path, actor):
    s = Setup(tmp_path)
    packet = s.sign(s.preview()["claim"])
    before = s.protocol()
    with pytest.raises(settings.SettingsError):
        s.service.preview(s.policy, actor=actor)
    with pytest.raises(settings.SettingsError):
        s.service.save(packet, actor=actor)
    assert s.protocol() == before
    assert s.service.inspect(actor=s.owner)["current"] is None


@pytest.mark.parametrize("kind", ["forged", "absent", "foreign", "expired", "unknown", "key"])
def test_invalid_signed_packets_fail_without_catalog_reads(tmp_path, kind):
    s = Setup(tmp_path)
    claim = s.preview()["claim"]
    packet = s.sign(claim)
    if kind == "forged":
        packet["claim"]["policy"]["mandate"]["max_runs"] += 1
    elif kind == "absent":
        del packet["signature"]
    elif kind == "foreign":
        claim["organization"] = "neighbor-tenant"
        packet = s.sign(claim)
    elif kind == "expired":
        claim["expires_at"] = int(time.time()) - 1
        packet = s.sign(claim)
    elif kind == "unknown":
        claim["policy"]["extra"] = "unapproved"
        packet = s.sign(claim)
    else:
        s.public.write_bytes(b"changed-key")
    before = s.protocol()
    with pytest.raises((settings.SettingsError, MandateError)):
        s.service.save(packet, actor=s.owner)
    assert s.protocol() == before
    assert s.service.inspect(actor=s.owner)["current"] is None


@pytest.mark.parametrize(
    "kind", ["grant_revoke", "scope_change", "pair_change", "verifier", "authority"]
)
def test_live_grant_pair_and_authority_changes_fail_closed(tmp_path, kind):
    s = Setup(tmp_path)
    packet = s.sign(s.preview()["claim"])
    c = s.customer
    f = c.real_reader
    if kind == "grant_revoke":
        save(
            c.owner / ("source-withdrawn-" + f.cfg["scope"]["owner_attestation"] + ".json"),
            {"withdrawn": True},
        )
    elif kind == "scope_change":
        f.cfg["scope"]["permitted_features"] = ["private-email"]
        f.rebuild()
    elif kind == "pair_change":
        f.versions["trace"] = "new-version"
        f.rebuild()
    elif kind == "verifier":
        f.cfg["roles"]["correction"]["authority"] = f.cfg["roles"]["trace"]["authority"]
        f.rebuild()
    else:
        save(s.authority, dict(read(s.authority), revoked=True))
    with pytest.raises(ValueError):
        s.service.save(packet, actor=s.owner)
    assert not any(p["method"] == "resources/read" for p in s.protocol())
    assert c.ledger.inspect(c.mandate)["withdrawn"] is False


def test_competing_saves_and_explicit_prior_preview_atomicity(tmp_path):
    s = Setup(tmp_path)
    first = s.apply()
    candidates = [deepcopy(s.policy), deepcopy(s.policy)]
    for index, policy in enumerate(candidates):
        policy["mandate"].update(mandate_id=f"settings-next-{index}", max_runs=3 + index)
    previews = [s.preview(p) for p in candidates]
    assert all(
        p["prior"] == first and p["claim"]["prior_revision"] == first["revision"] for p in previews
    )
    packets = [s.sign(p["claim"]) for p in previews]
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(s.service.save, p, actor=s.owner) for p in packets]
    good, bad = [], []
    for f in futures:
        try:
            good.append(f.result())
        except settings.SettingsError as error:
            bad.append(str(error))
    assert len(good) == 1 and bad == ["settings_revision_conflict"]
    assert s.service.inspect(actor=s.owner)["current"] == good[0]
    with pytest.raises(settings.SettingsError, match="stale_settings_receipt"):
        s.service.worker_config(first, actor=s.operator)
    assert s.customer.ledger.inspect(first["mandate_id"])["withdrawn"]
    # Failed candidate adds neither a successor nor a settings row.
    with sqlite3.connect(s.customer.ledger.path) as db:
        assert db.execute("SELECT count(*) FROM renewals").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM owner_settings_revisions").fetchone()[0] == 2


@pytest.mark.parametrize("action", ["pause", "revoke"])
def test_pause_revoke_old_worker_and_stale_receipt_zero_reads(tmp_path, action):
    s = Setup(tmp_path)
    first = s.apply()
    config = s.service.worker_config(first, actor=s.operator)
    prior = s.preview(action=action)
    receipt = s.service.save(s.sign(prior["claim"]), actor=s.owner)
    assert receipt["prior_revision"] == first["revision"]
    assert receipt["prior_treatment"]["release"] == "denied"
    before = s.protocol()
    held = autonomous_cycle.cycle_once(s.customer.owner, s.customer.org, config["config"])
    assert held["state"] == "held"
    assert s.protocol() == before
    with pytest.raises(settings.SettingsError, match="settings_not_active"):
        s.service.worker_config(receipt, actor=s.operator)
    next_policy = deepcopy(s.policy)
    next_policy["mandate"]["mandate_id"] = "settings-renewed"
    renewed = s.apply(next_policy, action="renew")
    assert renewed["state"] == "active"
    assert s.customer.ledger.inspect(first["mandate_id"])["withdrawn"]
    assert s.service.reconcile(receipt, actor=s.owner)["pending_fit_destinations"] == 0


def test_read_only_scope_never_starts_worker_and_unknown_fields_refused(tmp_path):
    s = Setup(tmp_path)
    s.policy["mandate"]["mode"] = "manual"
    receipt = s.apply()
    before = s.protocol()
    with pytest.raises(MandateError, match="manual_learning_only"):
        s.service.worker_config(receipt, actor=s.operator)
    assert s.protocol() == before
    s.policy["mandate"]["unknown"] = "unchecked"
    with pytest.raises(MandateError, match="invalid_mandate_specification"):
        s.preview()


def test_workflow_owner_binding_is_distinct_from_custodian_save(tmp_path):
    s = Setup(tmp_path)
    save(s.authority, dict(read(s.authority), workflow_owner="workflow-owner"))
    assert s.service.inspect(actor="workflow-owner")["current"] is None
    packet = s.sign(s.preview()["claim"])
    with pytest.raises(settings.SettingsError, match="settings_custodian_required"):
        s.service.save(packet, actor="workflow-owner")
    receipt = s.service.save(packet, actor=s.owner)
    assert receipt["workflow_owner"] == "workflow-owner" and receipt["actor"] == s.owner
    assert receipt["reviewer"] == s.reviewer
    assert receipt["reviewer_grant_revision"] != receipt["binding"]["grant_revision"]
    assert receipt["workflow_owner_role_revision"] != receipt["reviewer_grant_revision"]
    p = deepcopy(s.policy)
    p["mandate"].update(mandate_id="reviewer-change", reviewer="UOTHER")
    p["agreement"]["release_review"]["reviewer"] = "UOTHER"
    from opentour_cell.pilot.operating_point import digest

    p["mandate"]["agreement_revision"] = digest(p["agreement"])
    save(s.authority, dict(read(s.authority), reviewers=[s.reviewer, "UOTHER"]))
    with pytest.raises(settings.SettingsError, match="reviewer_replacement_settings_required"):
        s.preview(p)


def test_failed_successor_rolls_back_prior_withdrawal_and_receipt(tmp_path):
    s = Setup(tmp_path)
    packet = s.sign(s.preview()["claim"])
    with patch.object(
        s.service.ledger, "_reauthorize", side_effect=MandateError("injected_failure")
    ):
        with pytest.raises(MandateError, match="injected_failure"):
            s.service.save(packet, actor=s.owner)
    assert not s.customer.ledger.inspect(s.customer.mandate)["withdrawn"]
    assert s.service.inspect(actor=s.owner)["current"] is None
    assert s.service.save(packet, actor=s.owner)["state"] == "active"


@pytest.mark.parametrize("expired", ["mandate", "source_grant"])
def test_expired_grant_still_allows_signed_pause_and_denies_worker(tmp_path, expired):
    s = Setup(tmp_path)
    first = s.apply()
    cfg = s.service.worker_config(first, actor=s.operator)["config"]
    now = s.policy["mandate"]["expires_at"] + 1 if expired == "mandate" else time.time()
    reason = "standing_mandate_expired"
    if expired == "source_grant":
        s.customer.real_reader.cfg["scope"]["expires_at"] = int(now) - 1
        s.customer.real_reader.rebuild()
        reason = "standing_engagement_unavailable"
    before = s.protocol()
    with patch("time.time", return_value=now):
        with pytest.raises(ValueError, match=reason) as held:
            s.service.worker_config(first, actor=s.operator)
        if expired == "source_grant":
            assert str(held.value.__cause__) == "source_grant_outside_validity"
        packet = s.sign(s.preview(action="pause")["claim"])
        receipt = s.service.save(packet, actor=s.owner)
    assert receipt["state"] == "pause" and s.protocol() == before
    assert autonomous_cycle.cycle_once(s.customer.owner, s.customer.org, cfg)["state"] == "held"


def test_authentication_is_rechecked_inside_writer_lock(tmp_path):
    s = Setup(tmp_path)
    packet = s.sign(s.preview()["claim"])
    entered, release = Event(), Event()
    original = s.service._live

    def pause(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs["discover"]:
            entered.set()
            assert release.wait(4)
        return result

    with patch.object(s.service, "_live", pause), ThreadPoolExecutor(1) as pool:
        future = pool.submit(s.service.save, packet, actor=s.owner)
        assert entered.wait(4)
        save(s.authority, dict(read(s.authority), revoked=True))
        release.set()
        with pytest.raises(settings.SettingsError, match="authority_unavailable"):
            future.result(timeout=5)
    assert not s.customer.ledger.inspect(s.customer.mandate)["withdrawn"]


def test_first_settings_cannot_replace_existing_mandate_reviewer(tmp_path):
    s = Setup(tmp_path)
    claim = s.preview()["claim"]
    save(s.authority, dict(read(s.authority), reviewers=[s.reviewer, "UOTHER"]))
    claim["authority_revision"] = digest(read(s.authority))
    p = deepcopy(s.policy)
    p["mandate"]["reviewer"] = "UOTHER"
    p["agreement"]["release_review"]["reviewer"] = "UOTHER"
    p["mandate"]["agreement_revision"] = digest(p["agreement"])
    with pytest.raises(settings.SettingsError, match="reviewer_replacement_settings_required"):
        s.preview(p)
    claim["policy"] = p
    with pytest.raises(settings.SettingsError, match="reviewer_replacement_settings_required"):
        s.service.save(s.sign(claim), actor=s.owner)
    assert not s.customer.ledger.inspect(s.customer.mandate)["withdrawn"]
    assert s.service.inspect(actor=s.owner)["current"] is None


def test_worker_rechecks_waiting_operator_and_current_authority(tmp_path):
    s = Setup(tmp_path)
    receipt = s.apply()
    entered, resume = Event(), Event()
    original = s.service.ledger._authorized_db

    @contextmanager
    def delayed():
        entered.set()
        assert resume.wait(4)
        with original() as db:
            yield db

    with patch.object(s.service.ledger, "_authorized_db", delayed), ThreadPoolExecutor(1) as pool:
        future = pool.submit(s.service.worker_config, receipt, actor=s.operator)
        assert entered.wait(4)
        save(s.authority, dict(read(s.authority), operator="replacement-operator"))
        resume.set()
        with pytest.raises(settings.SettingsError, match="settings_actor_not_authorized"):
            future.result(timeout=5)
    assert not list(s.customer.owner.glob("settings-cycle-*.json"))
    with pytest.raises(settings.SettingsError, match="settings_authority_changed"):
        s.service.worker_config(receipt, actor="replacement-operator")


def test_cohort_guard_remains_held_until_successor_commit(tmp_path):
    s = Setup(tmp_path)
    packet = s.sign(s.preview()["claim"])
    under_lock = False
    observed = []
    original_live = s.service._live

    def live(*args, **kwargs):
        nonlocal under_lock
        result = original_live(*args, **kwargs)
        if not kwargs["discover"]:
            under_lock = True
        return result

    @contextmanager
    def guard():
        yield
        if under_lock:
            # A different connection cannot see the new head until the writer commits.
            with sqlite3.connect(s.customer.ledger.path) as db:
                observed.append(db.execute("SELECT revision FROM owner_settings_heads").fetchone())

    with (
        patch.object(s.service, "_live", live),
        patch.object(settings.workplace, "_claim_lease", return_value=guard),
    ):
        receipt = s.service.save(packet, actor=s.owner)
    assert observed == [(receipt["revision"],)]


@pytest.mark.parametrize("crash", ["before_rename", "after_rename"])
def test_interrupted_worker_publication_has_no_partial_final_and_retries(tmp_path, crash):
    s = Setup(tmp_path)
    receipt = s.apply()
    rename = settings.os.replace

    def interrupted(source, target):
        if crash == "after_rename":
            rename(source, target)
        raise OSError("simulated_crash")

    with patch.object(settings.os, "replace", side_effect=interrupted):
        with pytest.raises(MandateError, match="standing_engagement_unavailable"):
            s.service.worker_config(receipt, actor=s.operator)
    agreements = list(s.customer.owner.glob("settings-agreement-*.json"))
    assert len(agreements) == (1 if crash == "after_rename" else 0)
    if agreements:
        assert read(agreements[0]) == s.policy["agreement"]
        assert agreements[0].stat().st_nlink == 1
    assert not list(s.customer.owner.glob("settings-cycle-*.json"))
    assert (
        s.service.worker_config(receipt, actor=s.operator)["config"]["mandate_id"]
        == receipt["mandate_id"]
    )


def test_lowered_renewal_row_cap_stops_before_content_acquisition(tmp_path):
    s = Setup(tmp_path)
    first = s.apply()
    cfg = s.service.worker_config(first, actor=s.operator)["config"]
    assert (
        autonomous_cycle.cycle_once(s.customer.owner, s.customer.org, cfg)["state"]
        == "pending_slack_review"
    )
    usage = s.customer.ledger.inspect(first["mandate_id"])
    assert usage["cumulative_runs"] == 1 and usage["cumulative_rows"] > 0
    p = deepcopy(s.policy)
    p["mandate"].update(mandate_id="lowered", max_runs=1)
    receipt = s.apply(p, action="renew")
    cfg = s.service.worker_config(receipt, actor=s.operator)["config"]
    before_reads = sum(p["method"] == "resources/read" for p in s.protocol())
    held = autonomous_cycle.cycle_once(s.customer.owner, s.customer.org, cfg)
    assert held["state"] == "held" and held["reason"] == "standing_source_budget_exhausted"
    assert sum(p["method"] == "resources/read" for p in s.protocol()) == before_reads
    now = s.customer.ledger.inspect(receipt["mandate_id"])
    assert now["runs"] == 0 and now["cumulative_runs"] == 1
    assert now["cumulative_rows"] == usage["cumulative_rows"]


def test_renewal_fits_seconds_accounting_and_scope_isolation(tmp_path):
    # Use the existing generated mandate fixture with genuine reservation rows, no fitting needed.
    from test_learning_mandate import TestLearningMandate

    f = TestLearningMandate()
    f.setUp()
    try:
        run = f.admit()
        f.confirm(run["run_id"])
        f.ledger.reserve(
            "standing-one", f.reservation(run["run_id"], seconds=25), actor="local-worker"
        )
        f.ledger.withdraw("standing-one", actor="customer-owner")
        spec = dict(f.spec, mandate_id="renew-one")
        f.ledger.reauthorize("standing-one", spec, actor="customer-owner")
        source = f.ledger.authorize_source("renew-one", f.request(2), actor="local-worker")
        f.confirm(source["run_id"])
        with pytest.raises(MandateError, match="standing_fit_budget_exhausted"):
            f.ledger.reserve(
                "renew-one", f.reservation(source["run_id"], seconds=10), actor="local-worker"
            )
        f.ledger.reserve(
            "renew-one", f.reservation(source["run_id"], seconds=5), actor="local-worker"
        )
        f.ledger.withdraw("renew-one", actor="customer-owner")
        f.ledger.reauthorize(
            "renew-one", dict(spec, mandate_id="renew-two", max_fits=1), actor="customer-owner"
        )
        restarted = LearningMandates(f.directory, "example-org", f.engagement)
        usage = restarted.inspect("renew-two")
        assert usage["reserved_fits"] == 0 and usage["reserved_seconds"] == 0
        assert usage["cumulative_reserved_fits"] == 2 and usage["cumulative_reserved_seconds"] == 30
        assert restarted.inspect("standing-one")["cumulative_reserved_fits"] == 2
        run = restarted.authorize_source("renew-two", f.request(3), actor="local-worker")
        f.confirm(run["run_id"])
        with pytest.raises(MandateError, match="standing_fit_budget_exhausted"):
            restarted.reserve(
                "renew-two", f.reservation(run["run_id"], seconds=1), actor="local-worker"
            )
        with sqlite3.connect(f.ledger.path) as db:
            assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 2
    finally:
        f.doCleanups()
