"""Generated, separate-process workplace MCP resources and source-to-repair admission."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from opentour_cell.pilot import repair_workflow, source_intake
from opentour_cell.pilot import workplace_context as workplace
from opentour_cell.pilot.custody import Closeout
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.training import LocalTrainingStore

Json = dict[str, Any]
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
PROVIDER = """
import json, os, signal, subprocess, sys, time
from pathlib import Path
state = json.loads(Path("generated-provider.json").read_bytes())
reads = 0
Path("generated-reader-pid").write_text(str(os.getpid()))
if state.get("mode") == "ignore_term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
for line in sys.stdin:
    request = json.loads(line)
    method, params = request["method"], request.get("params", {})
    mode = state.get("mode", "")
    with Path("generated-protocol-log.jsonl").open("a") as log:
        log.write(json.dumps({"method": method, "params": params}) + "\\n")
    if "id" not in request:
        continue
    if method == "initialize":
        reply = {"protocolVersion": params["protocolVersion"], "capabilities": {"resources": {}},
                 "serverInfo": {"name": state.get("server", "generated-reader"), "version": "1"}}
    elif method == "resources/list":
        items = state["catalog"]
        if reads == 2 and mode == "changing":
            items[0]["_meta"]["ai.opentour/workplace"]["version"] = "changed"
        if mode == "loop":
            reply = {"resources": [], "nextCursor": "opaque-cursor"}
        elif mode == "page_limit":
            reply = {"resources": [], "nextCursor": str(int(params.get("cursor", "0")) + 1)}
        elif mode == "flood":
            reply = {"resources": [{"uri": "workplace://unused/owner/item/" + str(i)}
                                   for i in range(257)]}
        elif "cursor" not in params:
            reply = {"resources": items[:2], "nextCursor": "opaque-cursor"}
        elif params["cursor"] == "opaque-cursor":
            reply = {"resources": items[2:]}
        else:
            sys.exit(15)
    elif method == "resources/read":
        uri = params["uri"]
        reads += 1
        if uri not in state["contents"]:
            sys.exit(13)
        if reads == 2:
            if mode == "disconnect":
                sys.exit(7)
            if mode == "supervises_helper":
                # This helper inherits the MCP stdout, like an owner-installed reader's child.
                helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
                def stop_own_helper(*_):
                    helper.terminate()
                    helper.wait(timeout=1)
                    sys.exit(0)
                signal.signal(signal.SIGTERM, stop_own_helper)
                Path("generated-helper-pid").write_text(str(helper.pid))
                try:
                    time.sleep(5)
                finally:
                    if helper.poll() is None:
                        helper.kill()
                        helper.wait(timeout=1)
            if mode in {"timeout", "ignore_term"}:
                time.sleep(5)
            if mode == "deny":
                error = {"code":-32000, "message":"generated-secret-never-print"}
                print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "error":error}), flush=True)
                continue
            if mode == "withdraw":
                marker = Path("source-withdrawn-" + state["attestation"] + ".json")
                marker.write_text('{"withdrawn":true}'); marker.chmod(0o600)
        chunk = state["contents"][uri]
        content = json.dumps(chunk)
        if reads == 2 and mode == "oversize":
            content += "x" * 5000000
        if reads == 2 and mode == "invalid_json":
            content = '{"generated-secret-never-print"'
        returned_uri = uri + "-redirect" if mode == "redirect" else uri
        part = {"uri": returned_uri, "mimeType": "application/json", "text":content}
        reply = {"contents": [part]}
    else:
        sys.exit(14)
    print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "result":reply}), flush=True)
"""


def put(path: Path, value: Any) -> None:
    path.write_text(canonical(value) + "\n", encoding="utf-8")
    path.chmod(0o600)


class WorkplaceFixture:
    """A scoped generated provider over either the small or managed-repair example."""

    def __init__(self, directory: Path, scope: Json, traces: list[Json], corrections: list[Json]):
        directory.mkdir(mode=0o700, exist_ok=True)
        self.directory = directory
        self.store = LocalTrainingStore(directory / "training.sqlite3", scope["organization"])
        self.connection = directory / "workplace-connection.json"
        self.reader = directory / "generated-mcp-reader"
        startup = ""
        if os.environ.get("OPENTOUR_UNIT_GUARD") == "1":
            # The real reader deliberately receives no ambient Python path. Equip this
            # generated child with the same repository network/process limits as its parent.
            guard = (
                Path(__file__).resolve().parents[4]
                / "packages/contracts/common/python-unit/sitecustomize.py"
            )
            startup = (
                "import os, runpy\nos.environ['OPENTOUR_UNIT_GUARD'] = '1'\n"
                + f"runpy.run_path({str(guard)!r})\n"
            )
        self.reader.write_text("#!" + sys.executable + "\n" + startup + PROVIDER)
        self.reader.chmod(0o700)
        self.grant = {**scope, "engagement": self.store.engagement_id}
        roles = {}
        for role in workplace.ROLES:
            dsts = (
                source_intake.TRACE - {"features"} | {"slices"}
                if role == "trace"
                else source_intake.OUTCOME
            )
            roles[role] = {
                "namespace": f"workplace://generated/approved/{role}/",
                "authority": role + "-issuer",
                "columns": {k: "src_" + k for k in sorted(dsts)},
            }
            if role == "trace":
                roles[role]["feature_columns"] = {
                    k: "at_decision_" + k for k in scope["permitted_features"]
                }
        self.cfg = {
            "schema_version": workplace.CONNECTION,
            "reader": {
                "provider": "generated-reader",
                "executable": str(self.reader),
                "sha256": hashlib.sha256(self.reader.read_bytes()).hexdigest(),
                "env_names": [],
            },
            "scope": {
                k: v for k, v in self.grant.items() if k in source_intake.SCOPE - workplace._DERIVED
            },
            "roles": roles,
            "purposes": ["decision_context", "independent_verification"],
            "destination": "owner_local",
            "data_location": "owner_local",
        }
        self.rows = {"trace": traces, "correction": corrections}
        self.versions = dict.fromkeys(workplace.ROLES, "source-v1")
        self.mode = ""
        self.catalog: list[Json] = []
        self.contents: Json = {}
        self.rebuild()

    @classmethod
    def small(cls, path: Path) -> WorkplaceFixture:
        grant = json.loads((EXAMPLES / "pilot-source-grant.template.json").read_bytes())
        return cls(
            path,
            grant,
            *[
                json.loads((EXAMPLES / f"pilot-source-{role}.json").read_bytes())
                for role in workplace.ROLES
            ],
        )

    @classmethod
    def from_repair(cls, directory: Path) -> WorkplaceFixture:
        return cls(
            directory,
            json.loads((directory / "source-grant.json").read_bytes()),
            *[
                [json.loads(x) for x in (directory / name).read_text().splitlines()]
                for name in ["trace.jsonl", "corrections.jsonl"]
            ],
        )

    def rebuild(self) -> None:
        self.catalog, self.contents = [], {}
        for role in workplace.ROLES:
            spec = self.cfg["roles"][role]
            uri = spec["namespace"] + "chosen"
            fields = workplace._projection(role, self.cfg)
            meta = {k: self.cfg["scope"][k] for k in workplace._BINDINGS}
            meta.update(
                provider="generated-reader",
                source_id=self.grant[role + "_source"],
                authority=spec["authority"],
                schema_version=workplace.RESOURCE,
                policy_status="published",
                version=self.versions[role],
                role=role,
                columns=fields,
                row_count=len(self.rows[role]),
            )
            chosen = {
                "uri": uri,
                "mimeType": "application/json",
                "name": "business-view",
                "_meta": {workplace.META: meta},
            }
            self.catalog.append(chosen)
            irrelevant = deepcopy(chosen)
            irrelevant["uri"] = spec["namespace"] + "unpublished-policy"
            irrelevant["_meta"][workplace.META]["policy_version"] = "draft-policy"
            self.catalog.append(irrelevant)
            wire_rows = []
            for row in self.rows[role]:
                projected = {name: row[dst] for dst, name in spec["columns"].items()}
                for feature, name in spec.get("feature_columns", {}).items():
                    projected[name] = row["features"][feature]
                if policy := spec.get("policy_context"):
                    projected[policy["effective_at_column"]] = row["sop_effective_at"]
                wire_rows.append(projected)
            self.contents[uri] = {"projection": deepcopy(meta), "rows": wire_rows}
        self.catalog.append(
            {
                "uri": "workplace://generated/other/chat/private-history",
                "name": "generated-secret-never-print",
                "mimeType": "text/plain",
            }
        )
        self.save()

    def save(self) -> None:
        put(self.connection, self.cfg)
        put(
            self.directory / "generated-provider.json",
            {
                "catalog": self.catalog,
                "contents": self.contents,
                "mode": self.mode,
                "attestation": self.cfg["scope"]["owner_attestation"],
            },
        )

    def received(self) -> list[Json]:
        return [
            json.loads(x)
            for x in (self.directory / "generated-protocol-log.jsonl").read_text().splitlines()
        ]

    def reads(self) -> list[str]:
        return [r["params"]["uri"] for r in self.received() if r["method"] == "resources/read"]


def direct(f: WorkplaceFixture, action: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.workplace_context",
            action,
            "--directory",
            str(f.directory),
            "--org",
            f.store.organization,
            "--grant",
            str(f.connection),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_real_mcp_catalog_then_only_two_reads_cli_fresh_idempotent(tmp_path: Path) -> None:
    f = WorkplaceFixture.small(tmp_path)
    preview = direct(f, "discover")
    assert preview.returncode == 0, preview.stderr
    proposal = json.loads(preview.stdout)
    assert proposal["counts"] == {"trace_rows": 6, "correction_rows": 6}
    assert f.reads() == []
    assert [r["method"] for r in f.received()] == [
        "initialize",
        "notifications/initialized",
        "resources/list",
        "resources/list",
    ]
    assert "invoice-" not in preview.stdout and "private-history" not in preview.stdout
    assert proposal["connection_revision"] == digest(f.cfg)
    one = direct(f, "acquire", "--expected-pair-revision", proposal["pair_revision"])
    assert one.returncode == 0, one.stderr
    first = json.loads(one.stdout)
    assert f.reads() == [f.cfg["roles"][r]["namespace"] + "chosen" for r in workplace.ROLES]
    assert "generated-secret-never-print" not in one.stdout + one.stderr
    assert "equipment lease rental" not in one.stdout
    assert first["counts"]["development_rows"] == first["counts"]["protected_rows"] == 3
    assert first["source_proposal"]["suspected_error_classes"][0]["count"] == 2
    g = json.loads(Path(first["grant_path"]).read_bytes())
    assert "training_attestation" not in g
    receipt = json.loads(Path(first["receipt_path"]).read_bytes())
    assert receipt["counts"] == first["counts"]
    for role in workplace.ROLES:
        path = Path(first[role + "_path"])
        assert g[role + "_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert receipt["resources"][role]["version"] == "source-v1"
        assert len(receipt["wire_sha256"][role]) == 64
        assert receipt["fetch"][role]["elapsed_ns"] > 0
        assert path.stat().st_mode & 0o077 == 0
    second = direct(f, "acquire")
    assert second.returncode == 0 and json.loads(second.stdout) == first
    assert json.loads(Path(first["receipt_path"]).read_bytes()) == receipt


def test_withdrawal_between_provider_responses_prevents_next_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = WorkplaceFixture.small(tmp_path)
    wire = f.directory / "generated-protocol-log.jsonl"

    @contextmanager
    def session(
        _cfg: Json, _root: Path, dispatch: Callable[[], AbstractContextManager[None]]
    ) -> Iterator[workplace._Stdio]:
        with wire.open("w+b", buffering=0) as stdin:
            child = cast("subprocess.Popen[bytes]", SimpleNamespace(stdin=stdin))
            yield workplace._Stdio(child, time.monotonic() + 5, dispatch)

    def receive(_client: workplace._Stdio, _limit: int) -> Json:
        request = f.received()[-1]
        if request["method"] == "resources/list":
            result = {"resources": f.catalog}
        else:
            assert request["method"] == "resources/read"
            uri = request["params"]["uri"]
            Closeout(f.directory, f.store.organization).withdraw(
                agreement="generated-terms",
                custodian="generated-custodian",
                support="generated-support",
                due="2040-04-01",
                trigger="withdrawal",
            )
            result = {
                "contents": [
                    {"uri": uri, "mimeType": "application/json", "text": canonical(f.contents[uri])}
                ]
            }
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}

    monkeypatch.setattr(workplace, "_session", session)
    monkeypatch.setattr(workplace._Stdio, "receive", receive)
    with pytest.raises(workplace.WorkplaceError, match="engagement_withdrawn"):
        workplace.acquire_workplace(f.store, f.connection)
    assert f.reads() == [f.cfg["roles"]["trace"]["namespace"] + "chosen"]
    assert not (f.directory / "workplace-snapshots").exists()


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("ambiguous", "ambiguous_resource_pair"),
        ("no_correction", "workplace_resource_pair_missing"),
        ("foreign", "workplace_resource_pair_missing"),
        ("unpublished", "workplace_resource_pair_missing"),
        ("self", "independent_workplace_authority_required"),
        ("no_time", "workplace_resource_pair_missing"),
        ("no_case", "workplace_resource_pair_missing"),
        ("bad_uri", "invalid_workplace_namespace"),
        ("flood", "workplace_catalog_limit"),
        ("page_limit", "workplace_catalog_limit"),
        ("loop", "workplace_catalog_cursor_invalid"),
        ("duplicate", "workplace_catalog_duplicate"),
        ("budget", "workplace_row_limit"),
    ],
)
def test_unqualified_catalog_never_reads(tmp_path: Path, fault: str, reason: str) -> None:
    f = WorkplaceFixture.small(tmp_path)
    a, b = f.catalog[0], f.catalog[2]
    if fault == "ambiguous":
        dupe = deepcopy(a)
        dupe["uri"] += "-second"
        f.catalog.append(dupe)
    elif fault == "no_correction":
        f.catalog.remove(b)
    elif fault == "unpublished":
        b["_meta"][workplace.META]["policy_status"] = "draft"
    elif fault == "foreign":
        b["_meta"][workplace.META]["organization"] = "foreign"
    elif fault == "self":
        b["_meta"][workplace.META]["source_id"] = a["_meta"][workplace.META]["source_id"]
    elif fault in {"no_time", "no_case"}:
        a["_meta"][workplace.META]["columns"].remove(
            "src_features_available_at" if fault == "no_time" else "src_event_id"
        )
    elif fault == "bad_uri":
        a["uri"] = f.cfg["roles"]["trace"]["namespace"] + "../forbidden"
    elif fault == "duplicate":
        f.catalog.append(deepcopy(a))
    else:
        f.mode = fault
    f.save()
    with pytest.raises(workplace.WorkplaceError, match=reason):
        workplace.acquire_workplace(
            f.store, f.connection, max_total_rows=11 if fault == "budget" else 100
        )
    assert not f.reads()
    assert not (tmp_path / "workplace-snapshots").exists()


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("redirect", "workplace_resource_identity_mismatch"),
        ("wrong_org", "workplace_row_scope_mismatch"),
        ("oversize", "workplace_response_too_large"),
        ("disconnect", "workplace_reader_disconnected"),
        ("deny", "workplace_request_denied"),
        ("changing", "workplace_selected_pair_changed"),
        ("more_fields", "workplace_unapproved_fields"),
        ("missing_row", "workplace_resource_count_mismatch"),
        ("wrong_envelope", "workplace_resource_version_mismatch"),
        ("no_key", "workplace_unapproved_fields"),
        ("invalid_json", "workplace_source_unavailable"),
        ("withdraw", "source_grant_withdrawn"),
    ],
)
def test_interrupted_or_overbroad_refresh_keeps_last_pair(
    tmp_path: Path, fault: str, reason: str
) -> None:
    f = WorkplaceFixture.small(tmp_path)
    first = workplace.acquire_workplace(f.store, f.connection)
    previous = {
        key: Path(first[key]).read_bytes()
        for key in ("trace_path", "correction_path", "grant_path", "receipt_path")
    }
    f.versions["correction"] = "source-v2"
    f.rebuild()
    rows = f.contents[f.cfg["roles"]["correction"]["namespace"] + "chosen"]
    if fault == "wrong_org":
        rows["rows"][0]["src_organization"] = "foreign"
    elif fault == "more_fields":
        rows["rows"][0]["salary"] = "generated-secret-never-print"
    elif fault == "missing_row":
        rows["rows"].pop()
    elif fault == "wrong_envelope":
        rows["projection"]["version"] = "not-selected"
    elif fault == "no_key":
        del rows["rows"][0]["src_event_id"]
    else:
        f.mode = fault
    f.save()
    with pytest.raises(workplace.WorkplaceError, match=reason):
        workplace.acquire_workplace(f.store, f.connection)
    for key, raw in previous.items():
        assert Path(first[key]).read_bytes() == raw
    assert len([p for p in (tmp_path / "workplace-snapshots").iterdir() if p.is_dir()]) == 1
    if fault != "withdraw":
        f.mode = ""
        f.rebuild()
        assert (
            workplace.acquire_workplace(f.store, f.connection)["snapshot_revision"]
            != first["snapshot_revision"]
        )


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("expired", "source_grant_outside_validity"),
        ("withdrawn", "source_grant_withdrawn"),
        ("wrong_org", "source_grant_scope_mismatch"),
        ("foreign_engagement", "source_grant_scope_mismatch"),
        ("event_uri", "workplace_connection_scope_mismatch"),
        ("untrusted_binary", "workplace_reader_identity_mismatch"),
        ("missing_auth", "workplace_connection_credentials_unavailable"),
        ("private_field", "sensitive_workplace_feature"),
        ("alias", "workplace_projection_alias"),
        ("arbitrary_env", "invalid_workplace_environment"),
        ("wildcard", "invalid_workplace_namespace"),
        ("same_issuer", "independent_workplace_authority_required"),
        ("wider_projection", "invalid_workplace_feature_projection"),
        ("no_review", "source_grant_scope_mismatch"),
        ("scope_revision", "workplace_connection_changed"),
        ("symlink", "workplace_source_unavailable"),
    ],
)
def test_no_provider_call_without_exact_live_permission(
    tmp_path: Path, fault: str, reason: str
) -> None:
    f = WorkplaceFixture.small(tmp_path)
    scope, trace = f.cfg["scope"], f.cfg["roles"]["trace"]
    rev = None
    if fault == "expired":
        scope["expires_at"] = 301
    elif fault == "withdrawn":
        source_intake.withdraw_intake(f.store, scope["owner_attestation"])
    elif fault == "wrong_org":
        scope["organization"] = "foreign-org"
    elif fault == "foreign_engagement":
        scope["engagement"] = "foreign-engagement"
    elif fault == "event_uri":
        f.cfg["uri"] = "workplace://generated/unscoped/history"
    elif fault == "untrusted_binary":
        f.cfg["reader"]["sha256"] = "0" * 64
    elif fault == "missing_auth":
        f.cfg["reader"]["env_names"] = ["OPENTOUR_WORKPLACE_GENERATED_NO_SUCH_KEY"]
    elif fault == "arbitrary_env":
        f.cfg["reader"]["env_names"] = ["PATH"]
    elif fault == "private_field":
        trace["feature_columns"]["description"] = "reviewerEmail"
    elif fault == "alias":
        trace["columns"]["event_id"] = trace["columns"]["customer_id"]
    elif fault == "wildcard":
        trace["namespace"] += "*/"
    elif fault == "same_issuer":
        trace["authority"] = f.cfg["roles"]["correction"]["authority"]
    elif fault == "wider_projection":
        trace["feature_columns"]["cost"] = "amount"
    elif fault == "no_review":
        scope["evaluation_allowed"] = False
    elif fault == "scope_revision":
        rev = "f" * 64
    f.save()
    if fault == "symlink":
        alias = tmp_path / "indirect.json"
        alias.symlink_to(f.connection)
        f.connection = alias
    with pytest.raises(workplace.WorkplaceError, match=reason):
        workplace.acquire_workplace(f.store, f.connection, expected_connection_revision=rev)
    assert not (tmp_path / "generated-protocol-log.jsonl").exists()


def test_stale_choice_or_timeout_and_integrity_recheck(tmp_path: Path, monkeypatch) -> None:
    f = WorkplaceFixture.small(tmp_path)
    first = workplace.discover_workplace(f.store, f.connection)
    f.versions["trace"] = "v-2"
    f.rebuild()
    with pytest.raises(workplace.WorkplaceError, match="selected_pair_changed"):
        workplace.acquire_workplace(
            f.store, f.connection, expected_pair_revision=first["pair_revision"]
        )
    assert not f.reads()
    second = workplace.acquire_workplace(f.store, f.connection)
    f.rows["trace"][0]["features"]["description"] = "changed without increasing version"
    f.rebuild()
    with pytest.raises(workplace.WorkplaceError, match="version_content_changed"):
        workplace.acquire_workplace(f.store, f.connection)
    f.versions["trace"] = "v-3"
    f.rebuild()
    third = workplace.acquire_workplace(f.store, f.connection)
    assert third["snapshot_revision"] != second["snapshot_revision"]
    Path(third["trace_path"]).write_bytes(b"wrong\n")
    with pytest.raises(workplace.WorkplaceError, match="snapshot_integrity_failed"):
        workplace.acquire_workplace(f.store, f.connection)
    f.mode = "timeout"
    f.save()
    monkeypatch.setattr(workplace, "SESSION_SECONDS", 0.3)
    with pytest.raises(workplace.WorkplaceError, match="workplace_reader_timeout"):
        workplace.acquire_workplace(f.store, f.connection)


def test_sop_time_duplicates_late_outcomes_family_and_training_rights(tmp_path: Path) -> None:
    f = WorkplaceFixture.small(tmp_path)
    g = f.cfg["scope"]
    g["permitted_features"].append("sop_summary")
    trace = f.cfg["roles"]["trace"]
    trace["feature_columns"]["sop_summary"] = "current_sop"
    trace["policy_context"] = {"feature": "sop_summary", "effective_at_column": "sop_published"}
    for row in f.rows["trace"]:
        row["features"]["sop_summary"] = "generated instructions"
        row["sop_effective_at"] = row["decision_at"] - 1
    # Two reasons the independent verifier cannot establish a decision-time pair.
    f.rows["trace"][2]["sop_effective_at"] = f.rows["trace"][2]["decision_at"] + 1
    f.rows["correction"][5]["verified_at"] = g["as_of"] + 10
    f.rows["trace"].append(deepcopy(f.rows["trace"][0]))
    f.rows["correction"].append(deepcopy(f.rows["correction"][0]))
    f.rebuild()
    base = workplace.acquire_workplace(f.store, f.connection)
    assert base["counts"]["duplicates"] == {"trace": 1, "correction": 1}
    assert base["counts"]["excluded_counts"] == {
        "future_decision_features": 1,
        "verification_after_cutoff": 1,
    }
    confirm = tmp_path / "workplace-training.json"
    consent = {
        "schema_version": workplace.TRAINING,
        **{k: g[k] for k in ("organization", "engagement", "owner_id", "owner_attestation")},
        "snapshot_revision": base["snapshot_revision"],
        "training_attestation": "att-" + "b" * 16,
        "training_expires_at": g["expires_at"],
    }
    put(confirm, consent)
    admitted = workplace.acquire_workplace(f.store, f.connection, training_confirmation=confirm)
    assert admitted["snapshot_revision"] == base["snapshot_revision"]
    assert admitted["grant_path"] != base["grant_path"]
    proposal = admitted["source_proposal"]
    frozen = source_intake.freeze_intake(
        f.store,
        Path(admitted["grant_path"]),
        Path(admitted["trace_path"]),
        Path(admitted["correction_path"]),
        tmp_path / "current-intake.json",
        confirmation={
            "owner_id": g["owner_id"],
            "attestation": "att-" + "3" * 16,
            "discovery_revision": proposal["discovery_revision"],
            "class_id": proposal["suspected_error_classes"][0]["class_id"],
        },
    )
    assert frozen["training_allowed"] and set(frozen["training_case_ids"]) == {
        "invoice-1",
        "invoice-2",
    }
    assert not set(frozen["training_case_ids"]) & {r["case_id"] for r in frozen["protected"]}
    assert all("reference" not in r["features"] for r in frozen["development"])
    f.versions["trace"] = "new-trace"
    f.rebuild()
    with pytest.raises(workplace.WorkplaceError, match="stale_workplace_training_confirmation"):
        workplace.acquire_workplace(f.store, f.connection, training_confirmation=confirm)
    refreshed = workplace.acquire_workplace(f.store, f.connection)
    assert refreshed["snapshot_revision"] != base["snapshot_revision"]
    assert "training_attestation" not in json.loads(Path(refreshed["grant_path"]).read_bytes())
    f.versions["trace"] = "source-v1"
    f.rebuild()
    source_intake.withdraw_intake(f.store, consent["training_attestation"])
    assert (
        source_intake.inspect_intake(f.store, frozen["intake_revision"])["training_allowed"]
        is False
    )
    with pytest.raises(ValueError, match="source_training_not_authorized"):
        source_intake.development_rows(f.store, frozen["intake_revision"], for_training=True)
    with pytest.raises(workplace.WorkplaceError, match="workplace_training_grant_withdrawn"):
        workplace.acquire_workplace(f.store, f.connection, training_confirmation=confirm)
    assert (
        workplace.acquire_workplace(f.store, f.connection)["snapshot_revision"]
        == base["snapshot_revision"]
    )


@pytest.mark.parametrize("route", ["rule", "model"])
def test_direct_provider_pair_enters_real_managed_repair_and_respects_compute_grant(
    tmp_path: Path, route: str
) -> None:
    directory = tmp_path / "owner"
    subprocess.run(
        [
            sys.executable,
            str(EXAMPLES / "prepare-repair-example.py"),
            str(directory),
            "--route",
            route,
        ],
        check=True,
        capture_output=True,
    )
    f = WorkplaceFixture.from_repair(directory)
    acquired = workplace.acquire_workplace(f.store, f.connection)
    paths = [Path(acquired[k]) for k in ("grant_path", "trace_path", "correction_path")]
    found = acquired["source_proposal"]
    class_confirmation = {
        "owner_id": f.cfg["scope"]["owner_id"],
        "attestation": "att-" + "9" * 16,
        "discovery_revision": found["discovery_revision"],
        "class_id": found["suspected_error_classes"][0]["class_id"],
    }
    intake_name = "workplace-intake.json"
    frozen = source_intake.freeze_intake(
        f.store, *paths, directory / intake_name, confirmation=class_confirmation
    )
    manifest = json.loads((directory / "managed-plan.json").read_bytes())
    manifest.update(intake_file=intake_name, intake_revision=frozen["intake_revision"])
    result = repair_workflow.execute_managed(
        directory, f.store.organization, manifest, "run-" + "a" * 32
    )
    if route == "rule":
        assert result["outcome"] == "simple_repair" and result["case_count"] == 4
    else:
        assert result["outcome"] == "hold" and result["case_count"] == 0
        assert not (directory / "managed-outputs" / ("run-" + "a" * 32) / "model").exists()


def test_recurring_verified_watermark_is_independent_and_version_bound(tmp_path: Path) -> None:
    f = WorkplaceFixture.small(tmp_path)
    f.cfg["watermark_mode"] = "provider_verified"
    f.rows["correction"][3]["verified_at"] = 400
    f.rebuild()

    def covered(role: str, value: int) -> None:
        item = next(
            r for r in f.catalog if r["uri"] == f.cfg["roles"][role]["namespace"] + "chosen"
        )
        item["_meta"][workplace.META]["covered_through"] = value
        f.contents[item["uri"]]["projection"]["covered_through"] = value
        f.save()

    covered("trace", 450)
    with pytest.raises(workplace.WorkplaceError, match="workplace_watermark_unverified"):
        workplace.acquire_workplace(f.store, f.connection)
    assert not f.reads()
    covered("correction", 380)
    one = workplace.acquire_workplace(f.store, f.connection)
    assert one["counts"]["excluded_counts"] == {"verification_after_cutoff": 1}
    assert json.loads(Path(one["grant_path"]).read_bytes())["as_of"] == 380
    covered("correction", 420)
    with pytest.raises(workplace.WorkplaceError, match="workplace_version_content_changed"):
        workplace.acquire_workplace(f.store, f.connection)
    f.versions["correction"] = "finalized-v2"
    f.rebuild()
    covered("trace", 450)
    covered("correction", 420)
    two = workplace.acquire_workplace(f.store, f.connection)
    assert two["counts"]["excluded_counts"] == {} and two["counts"]["protected_rows"] == 3
    assert two["snapshot_revision"] != one["snapshot_revision"]
    assert workplace.acquire_workplace(f.store, f.connection) == two
    covered("correction", 4_102_444_800)
    with pytest.raises(workplace.WorkplaceError, match="workplace_watermark_unverified"):
        workplace.discover_workplace(f.store, f.connection)


def test_provider_identity_and_unrecognized_cli_input_stay_private(tmp_path: Path) -> None:
    f = WorkplaceFixture.small(tmp_path)
    provider = json.loads((tmp_path / "generated-provider.json").read_bytes())
    put(tmp_path / "generated-provider.json", {**provider, "server": "unknown-reader"})
    with pytest.raises(workplace.WorkplaceError, match="workplace_provider_mismatch"):
        workplace.discover_workplace(f.store, f.connection)
    assert [r["method"] for r in f.received()] == ["initialize"]
    denied = direct(f, "acquire", "--resource-uri", "workplace://private/secret/customer")
    assert denied.returncode == 2 and "private/secret" not in denied.stderr
    assert denied.stderr.strip() == "invalid_workplace_arguments"


@pytest.mark.parametrize("mode", ["ignore_term", "supervises_helper"])
def test_hung_reader_is_reaped_without_signalling_the_supervisor(
    tmp_path: Path, monkeypatch, mode: str
) -> None:
    import time

    f = WorkplaceFixture.small(tmp_path)
    f.mode = mode
    f.save()
    monkeypatch.setattr(workplace, "SESSION_SECONDS", 0.4)
    started = time.monotonic()
    with pytest.raises(workplace.WorkplaceError, match="workplace_reader_timeout"):
        workplace.acquire_workplace(f.store, f.connection)
    assert time.monotonic() - started < 4
    child = int((tmp_path / "generated-reader-pid").read_text())
    with pytest.raises(ChildProcessError):
        os.waitpid(child, os.WNOHANG)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    if mode == "supervises_helper":
        helper = int((tmp_path / "generated-helper-pid").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(helper, 0)
    f.mode = ""
    f.save()
    monkeypatch.setattr(workplace, "SESSION_SECONDS", 10)
    assert workplace.acquire_workplace(f.store, f.connection)["counts"]["protected_rows"] == 3
