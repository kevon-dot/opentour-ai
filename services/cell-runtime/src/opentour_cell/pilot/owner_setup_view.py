"""Customer-local HTML projection of authenticated worker setup results."""

from __future__ import annotations

import json
from collections.abc import Mapping
from html import escape
from typing import Any

Record = Mapping[str, Any]

_STYLE = """<style>
:root{font:100%/1.55 system-ui,sans-serif;color:#18292a;background:#f5f7f5}
*{box-sizing:border-box}body{margin:0}main{max-width:52rem;margin:0 auto;padding:2rem 1.25rem 4rem}
h1,h2{line-height:1.2}h1{font-family:Georgia,serif;font-size:2.5rem}
h2{margin:2rem 0 1rem;padding-top:1.25rem;border-top:1px solid #bdccca}
dl{display:grid;grid-template-columns:auto 1fr;gap:.5rem 1.5rem}
dd{margin:0;overflow-wrap:anywhere}dt,th,label{font-weight:600}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:.45rem;border-bottom:1px solid #bdccca}
caption{text-align:left;font-weight:600}form,label,textarea,select{display:block}
label{margin:1rem 0 .4rem}textarea,select{margin-top:.3rem;width:100%;padding:.6rem;
border:1px solid #677d79;border-radius:.25rem;background:white;color:inherit;font:inherit}
textarea,pre{font: .9rem/1.5 ui-monospace,monospace}textarea{resize:vertical}
pre{white-space:pre-wrap;overflow-wrap:anywhere}button{background:#16574b;color:white;
border:0;border-radius:.25rem;padding:.65rem 1rem;font:inherit;cursor:pointer}
a{color:#16574b}:focus-visible{outline:3px solid #1c64ab;outline-offset:2px}
@media(max-width:32rem){main{padding:1rem}dl{display:block}dd{margin-bottom:.6rem}}
</style>"""


def _text(value: object) -> str:
    return escape(str(value), quote=True)


def _json(value: object) -> str:
    return _text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def _hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{name}" value="{_text(value)}">'


def _current(status: Record) -> str:
    fields = [
        ("Workflow", status["workflow"]),
        ("State", status["state"].replace("_", " ")),
        ("Settings revision", status["revision"] or "Initial setup"),
        ("Mandate revision", status["mandate_revision"] or "None"),
    ]
    result = (
        "<h2>Current settings</h2><dl>"
        + "".join(f"<dt>{label}</dt><dd>{_text(value)}</dd>" for label, value in fields)
        + "</dl>"
    )
    budget = status.get("budget")
    if budget:
        rows = []
        for label in ("runs", "rows", "fits", "seconds"):
            entry = budget[label]
            rows.append(
                f'<tr><th scope="row">{label.title()}</th>'
                + "".join(f"<td>{_text(entry[k])}</td>" for k in ("limit", "used", "remaining"))
                + "</tr>"
            )
        result += (
            "<table><caption>Training budget</caption><thead><tr><th>Resource</th>"
            "<th>Limit</th><th>Used</th><th>Remaining</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
            + f"<p>Monetary limit · {_text(budget['currency'])} "
            + f"{_text(budget['monetary_limit_cents'])} cents. "
            + f"Monetary usage · {_text(budget['monetary_usage'])}.</p>"
        )
    return result


def _preview(preview: Record) -> str:
    payload = preview["payload"]
    claim = payload["claim"]
    prior = preview.get("prior") or {}
    fields = preview["changed_fields"]
    old = prior.get("policy", {}).get("mandate", {})
    new = claim["policy"]["mandate"]
    changes = "".join(
        f"<li><strong>{_text(k)}</strong><p>Previous</p><pre>{_json(old.get(k))}</pre>"
        f"<p>Proposed</p><pre>{_json(new[k])}</pre></li>"
        for k in fields
    )
    previous_policy, proposed_policy = prior.get("policy", {}), claim["policy"]
    for key in ("agreement", "connection_file", "currency"):
        if previous_policy.get(key) != proposed_policy.get(key):
            changes += (
                f"<li><strong>{_text(key)}</strong><p>Previous</p><pre>{_json(previous_policy.get(key))}</pre>"
                f"<p>Proposed</p><pre>{_json(proposed_policy.get(key))}</pre></li>"
            )
    return (
        "<h2>Review changes</h2>"
        f"<p>Action · {_text(claim['action'])}. Prior settings revision · "
        f"{_text(claim['prior_revision'] or 'Initial setup')}.</p>"
        + ("<ul>" + changes + "</ul>" if changes else "<p>The policy fields are unchanged.</p>")
        + "<h2>Sign the preview</h2><p>Give this exact payload to the workflow owner and "
        "custodian for signing outside the worker. Both signatures are required.</p>"
        + f"<p>Preview expires · {_text(payload['not_after'])}</p>"
        + f'<label>Payload to sign<textarea readonly rows="10">{_json(payload)}</textarea></label>'
    )


def _change_forms(status: Record, csrf: str) -> str:
    base = (
        csrf
        + _hidden("action", "preview")
        + _hidden("expected_revision", status["revision"] or "initial")
    )
    result = "<h2>Historical source pair</h2>"
    pairs = status.get("source_pairs", ())
    if pairs:
        options = ['<option value="" disabled selected>Choose a source pair</option>']
        for pair in pairs:
            label = f"{pair['workflow_label']} / {pair['correction_label']} · {pair['id'][:12]}"
            options.append(f'<option value="{_text(pair["id"])}">{_text(label)}</option>')
        operations = '<option value="save">Save settings</option>'
        if status["revision"]:
            operations += '<option value="renew">Renew mandate</option>'
        result += (
            '<form method="post">'
            + base
            + "<label>Workflow trace and independent correction resource"
            + '<select name="source_pair" required>'
            + "".join(options)
            + "</select></label>"
            + '<label>Operation<select name="operation">'
            + operations
            + "</select></label>"
            + '<label>Policy JSON<textarea name="policy" rows="8" required></textarea></label>'
            + "<p>Supply the mandate, agreement and currency for this source pair.</p>"
            + '<button type="submit">Preview changes</button></form>'
        )
    else:
        result += (
            "<p>No eligible workflow trace and correction pair is available. "
            "The data custodian can check the source connection and grant.</p>"
        )
    if status["revision"]:
        result += (
            '<h2>Pause or revoke this mandate</h2><form method="post">'
            + base
            + _hidden("policy", "")
            + _hidden("source_pair", "")
            + '<label>Operation<select name="operation">'
            + '<option value="pause">Pause mandate</option>'
            + '<option value="revoke">Revoke mandate</option>'
            + '</select></label><button type="submit">Preview mandate change</button></form>'
        )
    return result


def render_owner_setup(
    status: Record | None,
    *,
    csrf_token: str = "",
    preview: Record | None = None,
    receipt: Record | None = None,
    failure: str = "",
) -> str:
    content = "<h1>Workflow settings</h1>"
    if failure or status is None:
        message = {
            "stale": "The revision changed. Review current settings and prepare a new preview.",
            "expired": "The preview expired. Reopen current settings and prepare a fresh preview.",
        }.get(failure, "This setup is unavailable. Contact the local administrator for access.")
        content += f'<p role="alert">{message}</p>'
        if failure in {"stale", "expired"}:
            content += '<a href="/owner/setup">Review current settings</a>'
    else:
        content += _current(status)
        if receipt:
            content += (
                f"<h2>Saved settings</h2><p>State · {_text(receipt['state'])}. "
                f"Saved revision · {_text(receipt['revision'])}.</p>"
            )
        if csrf_token and status.get("may_preview") is True:
            hidden = _hidden("csrf_token", csrf_token)
            if preview:
                content += _preview(preview)
                content += (
                    '<form method="post">'
                    + hidden
                    + _hidden("action", "submit")
                    + "<label>Signed packet"
                    + '<textarea name="signed_packet" rows="8" required></textarea></label>'
                    + '<button type="submit">Submit signed settings</button></form>'
                )
            else:
                content += _change_forms(status, hidden)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Workflow settings</title>"
        + _STYLE
        + "</head><body><main>"
        + content
        + "</main></body></html>"
    )
