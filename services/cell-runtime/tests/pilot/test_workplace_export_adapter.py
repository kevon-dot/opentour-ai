"""Generated conformance of the shipped structured-export adapter."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

from opentour_cell.pilot import workplace_context as workplace
from test_workplace_context import WorkplaceFixture, direct, put

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/workplace_export_adapter.py"
MANIFEST = "workplace-export-adapter.json"
PROVIDER = "opentour-structured-export"
SCHEMA = "opentour.example.workplace-export-adapter.v1"
SENTINEL = "GENERATED_PRIVATE_OUTSIDE_FIELD"


def configure(f: WorkplaceFixture, *, horizons=(300, 300)):
    f.rebuild()
    launcher = f.directory / "structured-export"
    launcher.write_bytes(f"#!{sys.executable}\n".encode() + EXAMPLE.read_bytes())
    launcher.chmod(0o700)
    f.cfg["reader"] = {
        "executable": str(launcher),
        "sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
        "provider": PROVIDER,
        "env_names": [],
    }
    f.cfg["watermark_mode"] = "provider_verified"
    put(f.connection, f.cfg)
    manifest = {"schema_version": SCHEMA, "expires_at": f.grant["expires_at"], "resources": {}}
    for role, horizon in zip(workplace.ROLES, horizons, strict=True):
        uri = f.cfg["roles"][role]["namespace"] + "chosen"
        body = deepcopy(f.contents[uri])
        body["projection"].update(provider=PROVIDER, covered_through=horizon)
        path = f.directory / f"workplace-export-{role}-{f.versions[role]}.json"
        put(path, body)
        manifest["resources"][role] = {
            "uri": uri,
            "file": path.name,
            "projection": body["projection"],
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    put(f.directory / MANIFEST, manifest)
    return manifest


def wire(directory, *requests):
    messages = [
        {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        {"method": "notifications/initialized"},
        *[
            {"id": index, "method": method, "params": args}
            for index, (method, args) in enumerate(requests, 2)
        ],
    ]
    output = subprocess.run(
        [sys.executable, "-B", str(EXAMPLE)],
        cwd=directory,
        input="".join(json.dumps({"jsonrpc": "2.0", **frame}) + "\n" for frame in messages),
        capture_output=True,
        check=False,
        timeout=10,
        text=True,
    )
    assert SENTINEL not in output.stdout + output.stderr
    return output, [json.loads(line) for line in output.stdout.splitlines()]


def test_catalog_metadata_survives_unavailable_bodies_and_rejects_unknown_uris(tmp_path):
    f = WorkplaceFixture.small(tmp_path)
    manifest = configure(f)
    trace, correction = (manifest["resources"][r] for r in workplace.ROLES)
    for pin in (trace, correction):
        (f.directory / pin["file"]).unlink()
    response, rows = wire(
        f.directory,
        ("resources/list", {}),
        ("resources/list", {"cursor": "correction"}),
        ("resources/read", {"uri": "workplace://generated/foreign/" + SENTINEL}),
        ("resources/read", {"uri": trace["uri"]}),
        ("tools/list", {}),
    )
    assert response.returncode == 0 and not response.stderr
    assert [row["result"]["resources"][0]["uri"] for row in rows[1:3]] == [
        trace["uri"],
        correction["uri"],
    ]
    assert rows[1]["result"]["nextCursor"] == "correction"
    assert (
        rows[3]["error"]
        == rows[4]["error"]
        == {
            "code": -32000,
            "message": "workplace_export_unavailable",
        }
    )
    assert rows[5]["error"]["code"] == -32601


def test_body_pin_and_exact_fields_are_required(tmp_path):
    f = WorkplaceFixture.small(tmp_path)
    manifest = configure(f)
    trace = manifest["resources"]["trace"]
    selection = ("resources/read", {"uri": trace["uri"]})
    ok, rows = wire(f.directory, selection)
    assert ok.returncode == 0 and rows[1]["result"]["contents"][0]["uri"] == trace["uri"]
    path = f.directory / trace["file"]
    original = path.read_bytes()
    body = json.loads(original)
    body["rows"][0]["outside_projection"] = SENTINEL
    put(path, body)
    for update_pin in (False, True):
        if update_pin:
            trace["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            put(f.directory / MANIFEST, manifest)
        output, rows = wire(f.directory, selection)
        assert output.returncode == 0 and "result" not in rows[1]
    path.unlink()
    other = f.directory / "outside.json"
    other.write_bytes(original)
    other.chmod(0o600)
    path.symlink_to(other)
    trace["sha256"] = hashlib.sha256(original).hexdigest()
    put(f.directory / MANIFEST, manifest)
    output, rows = wire(f.directory, selection)
    assert output.returncode == 0 and "result" not in rows[1]


def test_pair_rejects_foreign_and_nondistinct_correction_sources(tmp_path):
    f = WorkplaceFixture.small(tmp_path)
    baseline = configure(f)
    for key, changed in (
        ("organization", "generated-foreign"),
        ("source_id", baseline["resources"]["trace"]["projection"]["source_id"]),
        ("authority", baseline["resources"]["trace"]["projection"]["authority"]),
    ):
        manifest = deepcopy(baseline)
        manifest["resources"]["correction"]["projection"][key] = changed
        put(f.directory / MANIFEST, manifest)
        denied, rows = wire(f.directory, ("resources/list", {}))
        assert denied.returncode == 2 and rows == []
        assert denied.stderr == "workplace_export_unavailable\n"


def test_active_session_rechecks_manifest_before_read(tmp_path):
    f = WorkplaceFixture.small(tmp_path)
    manifest = configure(f)
    trace = manifest["resources"]["trace"]
    with subprocess.Popen(
        [sys.executable, "-B", str(EXAMPLE)],
        cwd=f.directory,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:

        def send(method, parameters, *, identifier=None):
            data = {"jsonrpc": "2.0", "method": method, "params": parameters}
            if identifier is not None:
                data["id"] = identifier
            child.stdin.write(json.dumps(data) + "\n")
            child.stdin.flush()
            if identifier is not None:
                return json.loads(child.stdout.readline())
            return None

        assert "result" in send("initialize", {"protocolVersion": "2025-11-25"}, identifier=1)
        send("notifications/initialized", {})
        assert "result" in send("resources/list", {}, identifier=2)
        changed = deepcopy(manifest)
        changed["expires_at"] -= 1
        put(f.directory / MANIFEST, changed)
        blocked = send("resources/read", {"uri": trace["uri"]}, identifier=3)
        assert blocked["error"]["message"] == "workplace_export_unavailable"
        assert "result" not in blocked
        child.stdin.close()
        assert child.wait(timeout=5) == 0
        assert child.stderr.read() == ""


def exact_descriptor_probe(directory: Path):
    """Run the existing worker CLI and a separate descriptor-backed process on Linux."""
    f = WorkplaceFixture.small(directory)
    f.rows["trace"].append(deepcopy(f.rows["trace"][0]))
    f.rows["correction"][5]["verified_at"] = 320
    f.rows["correction"].append(deepcopy(f.rows["correction"][0]))
    manifest = configure(f, horizons=(320, 310))
    body_paths = tuple(f.directory / manifest["resources"][r]["file"] for r in workplace.ROLES)
    parked = tuple(p.with_suffix(".pending") for p in body_paths)
    for source, target in zip(body_paths, parked, strict=True):
        source.rename(target)
    preflight = direct(f, "discover")
    assert preflight.returncode == 0, preflight.stderr
    admission = json.loads(preflight.stdout)
    assert admission["counts"] == {"trace_rows": 7, "correction_rows": 7}
    for source, target in zip(parked, body_paths, strict=True):
        source.rename(target)
    expected = ("--expected-pair-revision", admission["pair_revision"])
    first = direct(f, "acquire", *expected)
    assert first.returncode == 0, first.stderr
    snapshot = json.loads(first.stdout)
    assert snapshot["counts"]["duplicates"] == {"trace": 1, "correction": 1}
    assert snapshot["counts"]["excluded_counts"] == {"verification_after_cutoff": 1}
    assert snapshot["counts"]["development_rows"] == 3
    assert snapshot["counts"]["protected_rows"] == 2
    assert snapshot["source_proposal"]["suspected_error_classes"][0]["count"] == 2
    receipt_path = Path(snapshot["receipt_path"])
    before = receipt_path.read_bytes()
    receipt = json.loads(before)
    assert {role: row["covered_through"] for role, row in receipt["resources"].items()} == {
        "trace": 320,
        "correction": 310,
    }
    assert receipt["wire_sha256"] == {
        role: pin["sha256"] for role, pin in manifest["resources"].items()
    }
    assert json.loads(Path(snapshot["grant_path"]).read_bytes())["as_of"] == 310
    assert json.loads(direct(f, "acquire", *expected).stdout) == snapshot

    # The second stream vanishes after discovery. The complete earlier snapshot stays usable.
    body_paths[1].rename(parked[1])
    partial = direct(f, "acquire", *expected)
    assert partial.returncode == 1 and not partial.stdout
    assert partial.stderr.strip() == "workplace_request_denied"
    assert receipt_path.read_bytes() == before
    parked[1].rename(body_paths[1])
    restarted = direct(f, "acquire", *expected)
    assert restarted.returncode == 0 and json.loads(restarted.stdout) == snapshot

    f.versions["correction"] = "source-v2"
    configure(f, horizons=(320, 320))
    stale = direct(f, "acquire", *expected)
    assert stale.returncode == 1 and not stale.stdout and receipt_path.read_bytes() == before
    assert stale.stderr.strip() == "workplace_selected_pair_changed"
    newer = direct(f, "acquire")
    assert newer.returncode == 0, newer.stderr
    updated = json.loads(newer.stdout)
    assert updated["snapshot_revision"] != snapshot["snapshot_revision"]
    assert updated["counts"]["protected_rows"] == 3
    return {
        "kind": "local_integration",
        "first_snapshot_revision": snapshot["snapshot_revision"],
        "successor_snapshot_revision": updated["snapshot_revision"],
        "last_complete_receipt_sha256": hashlib.sha256(before).hexdigest(),
        "coverage_cutoffs": [310, 320],
        "protected_counts": [2, 3],
        "total_catalog_rows": admission["counts"],
        "interrupted_exit": partial.returncode,
        "stale_exit": stale.returncode,
    }
