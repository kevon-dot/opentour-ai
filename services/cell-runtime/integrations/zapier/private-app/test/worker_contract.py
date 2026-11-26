"""Run the real app actions against two clean generated worker TLS installations.

No provider account, live Zap, customer records or paid resources are used. Runtime
dependencies come from the repository's pinned Python environment. The fixture
uses the shipped starter authority, intake ledger, TLS gateway and kit builder.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
ROOT = APP.parents[4]
sys.path.insert(0, str(ROOT / "services/cell-runtime/tests/pilot"))
sys.path.insert(0, str(ROOT / "services/cell-runtime/src"))
import test_intake_mcp as vectors  # noqa: E402
from opentour_cell.pilot.learning_mandate import LearningMandates  # noqa: E402
from opentour_cell.pilot.training import LocalTrainingStore  # noqa: E402


def run(*args, **kw):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30, **kw)


def private_json(path, data):
    path.write_text(json.dumps(data))
    path.chmod(0o600)


def owner(directory, tenant):
    directory.mkdir(mode=0o700)
    training = LocalTrainingStore(directory / "training.sqlite3", tenant)
    LearningMandates(directory, tenant, training.engagement_id, owner="founder")
    selection = {
        "schema_version": vectors.STARTER_SELECTION_VERSION,
        "tenant": tenant,
        "actor": "worker",
        "owner_id": "founder",
        "engagement_epoch": training.engagement_id,
        "grant_ref": "registered-starter",
    }
    path = directory / "selection.json"
    private_json(path, selection)
    private_json(directory / "starter-intake-key.json", {"key": vectors.STARTER_FIXTURE_KEY})
    private_json(
        directory / "starter-intake-grant-registered-starter.json",
        vectors._sealed_starter(selection),
    )
    return path


@contextmanager
def listen(package, work, selection, tenant):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    origin = f"https://127.0.0.1:{port}"
    token = secrets.token_urlsafe(32)
    credential = work / f"{tenant}.credential"
    credential.write_text(token)
    credential.chmod(0o600)
    config = json.loads((package / "integrations/worker-host/manifest.example.json").read_text())
    config.update(
        source_commit=json.loads((package / "PROVENANCE.json").read_text())["source_commit"],
        origin=origin,
        intake_actor="worker",
        intake_tenant=tenant,
    )
    manifest = work / f"{tenant}.manifest.json"
    private_json(manifest, config)
    server_path = package / "integrations/worker-host/health.py"
    command = [
        sys.executable,
        "-I",
        "-B",
        str(server_path),
        "--config",
        str(manifest),
        "--cert",
        str(work / "tls.crt"),
        "--key",
        str(work / "tls.key"),
        "--token",
        str(credential),
        "--selection",
        str(selection),
        "--bind",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    # A fresh interpreter imports the immutable zipapp, never this test's source modules.
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PATH": os.environ["PATH"]},
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            ready = executor.submit(process.stdout.readline).result(timeout=10)
            if ready.strip() != str(port):
                raise RuntimeError("generated_worker_start_failed")
            yield (
                {
                    "connection_alias": tenant,
                    "origin": origin,
                    "audience": config["audience"],
                    "access_token": token,
                },
                credential,
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()


def counts(directory):
    with sqlite3.connect(directory / "intake.sqlite3") as db:
        return {
            name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in ("intake_runs", "intake_workflows", "intake_materialization")
        }


def main():
    if sys.version_info[:3] != (3, 12, 16):
        raise SystemExit("Use Python 3.12.16.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    args = parser.parse_args()
    if run(args.node, "--version").stdout.strip() != "v24.22.3":
        raise SystemExit("Use Node 24.22.3 for repository conformance.")
    with tempfile.TemporaryDirectory(prefix="ope-437-generated-") as tmp:
        work = Path(tmp).resolve()
        archive = work / "kit.zip"
        run(
            sys.executable,
            "-I",
            "-B",
            str(ROOT / "tools/build_slack_pilot_kit.py"),
            str(archive),
            cwd=ROOT,
        )
        package = work / "installed"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(package)
        (work / "extensions").write_text(
            "subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n"
        )
        for command in [
            [
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=Generated CA",
                "-keyout",
                "ca.key",
                "-out",
                "ca.crt",
            ],
            [
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                "/CN=Generated worker",
                "-keyout",
                "tls.key",
                "-out",
                "tls.csr",
            ],
            [
                "x509",
                "-req",
                "-in",
                "tls.csr",
                "-CA",
                "ca.crt",
                "-CAkey",
                "ca.key",
                "-CAcreateserial",
                "-days",
                "1",
                "-extfile",
                "extensions",
                "-out",
                "tls.crt",
            ],
        ]:
            run("openssl", *command, cwd=work)
        a, b = owner(work / "a", "org-a"), owner(work / "b", "org-b")
        invocations = 0

        def call(auth, operation, inputs, *, trust=True, insecure_env=False):
            nonlocal invocations
            env = {"PATH": os.environ["PATH"]}
            if trust:
                env["NODE_EXTRA_CA_CERTS"] = str(work / "ca.crt")
            if insecure_env:
                env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
            proc = run(
                args.node,
                str(APP / "test/worker-client.js"),
                input=json.dumps(
                    {"operation": operation, "bundle": {"authData": auth, "inputData": inputs}}
                ),
                env=env,
            )
            assert auth["access_token"] not in proc.stdout + proc.stderr
            invocations += 1
            return json.loads(proc.stdout)

        with (
            listen(package, work, a, "org-a") as (auth_a, token_a),
            listen(package, work, b, "org-b") as (auth_b, _),
        ):
            catalog = call(auth_a, "discover_sources", {"limit": 1})
            assert (
                catalog["ok"] and catalog["result"]["sources"][0]["source_ref"] == "policy-resource"
            )
            first = call(auth_a, "submit_intake", vectors.STARTER)
            assert first["ok"] and first["result"]["state"] == "queued"
            ref = first["result"]["run_ref"]
            status = call(auth_a, "get_run", {"run_ref": ref})
            assert status["ok"] and status["result"]["run_ref"] == ref
            assert (
                call(auth_a, "submit_intake", vectors.STARTER | {"idempotency_key": "new-trigger"})
                == first
            )
            assert call(auth_b, "discover_sources", {"limit": 1}) == catalog
            second = call(auth_b, "submit_intake", vectors.STARTER)
            assert second["ok"] and second["result"]["run_ref"] != ref
            ref_b = second["result"]["run_ref"]
            assert call(auth_b, "get_run", {"run_ref": ref_b})["ok"]
            assert call(auth_a, "get_run", {"run_ref": ref_b}) == {
                "ok": False,
                "code": "intake_unavailable",
            }
            assert call(auth_b, "get_run", {"run_ref": ref}) == {
                "ok": False,
                "code": "intake_unavailable",
            }
            assert call(auth_a, "get_run", {"run_ref": "unknown"}) == {
                "ok": False,
                "code": "intake_unavailable",
            }
            assert call(
                auth_a | {"access_token": auth_b["access_token"]}, "discover_sources", {}
            ) == {"ok": False, "code": "intake_unavailable"}
            assert call(auth_a | {"audience": "wrong-audience"}, "discover_sources", {}) == {
                "ok": False,
                "code": "intake_unavailable",
            }
            assert call(auth_a, "discover_sources", {}, trust=False) == {
                "ok": False,
                "code": "intake_outcome_unknown",
            }
            assert call(auth_a, "discover_sources", {}, trust=False, insecure_env=True) == {
                "ok": False,
                "code": "intake_outcome_unknown",
            }
            new_token = secrets.token_urlsafe(32)
            rotated = work / "rotated.credential"
            rotated.write_text(new_token)
            rotated.chmod(0o600)
            rotated.replace(token_a)
            assert call(auth_a, "get_run", {"run_ref": ref}) == {
                "ok": False,
                "code": "intake_unavailable",
            }
            auth_new = auth_a | {"access_token": new_token}
            assert call(auth_new, "get_run", {"run_ref": ref}) == status
            assert call(auth_new, "submit_intake", vectors.STARTER) == first
            actual = counts(a.parent)
            assert actual == {"intake_runs": 1, "intake_workflows": 1, "intake_materialization": 0}
            assert counts(b.parent) == actual
        assert call(auth_new, "get_run", {"run_ref": ref}) == {
            "ok": False,
            "code": "intake_outcome_unknown",
        }
        with listen(package, work, a, "org-a") as (restarted, _):
            assert call(restarted, "get_run", {"run_ref": ref}) == status
            assert call(restarted, "submit_intake", vectors.STARTER) == first
            assert counts(a.parent) == actual
        print(
            json.dumps(
                {
                    "fixture_kind": "generated-local-TLS",
                    "status": "PASS",
                    "invocations": invocations,
                    "worker_source": json.loads((package / "PROVENANCE.json").read_text())[
                        "source_commit"
                    ],
                    "counts": actual,
                    "provider_installation": "UNVERIFIED",
                    "customer_acceptances": 0,
                }
            )
        )


if __name__ == "__main__":
    main()
