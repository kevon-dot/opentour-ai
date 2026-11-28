"""Browser form controls and disclosure states for the customer-local owner setup."""

from html.parser import HTMLParser

import pytest
from opentour_cell.pilot.owner_setup_view import render_owner_setup


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.words = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.words.append(data)

    def attrs(self, tag):
        return [attrs for kind, attrs in self.elements if kind == tag]


def status(**changes):
    return {
        "workflow": "generated-order",
        "state": "unconfigured",
        "revision": None,
        "mandate_revision": None,
        "may_preview": True,
        "budget": None,
        **changes,
    }


def test_private_page_initial_edit_and_read_only_view():
    pair = {
        "id": "b" * 64,
        "workflow_label": "Approved workflow traces",
        "correction_label": "Independent corrections",
        "uri": "private://never",
    }
    page = Page(render_owner_setup(status(source_pairs=[pair]), csrf_token="generated-csrf"))
    assert page.attrs("form") == [{"method": "post"}]
    assert {f["name"]: f["value"] for f in page.attrs("input")} == {
        "csrf_token": "generated-csrf",
        "action": "preview",
        "expected_revision": "initial",
    }
    assert page.attrs("select") == [
        {"name": "source_pair", "required": None},
        {"name": "operation"},
    ]
    assert [o["value"] for o in page.attrs("option")] == ["", pair["id"], "save"]
    assert "Approved workflow traces / Independent corrections" in "".join(page.words)
    assert "private://never" not in "".join(page.words)
    assert [t["name"] for t in page.attrs("textarea")] == ["policy"]
    saved = Page(render_owner_setup(status(source_pairs=[pair], revision="a" * 64), csrf_token="t"))
    assert [o["value"] for o in saved.attrs("option")] == [
        "",
        pair["id"],
        "save",
        "renew",
        "pause",
        "revoke",
    ]
    assert any(
        f == {"type": "hidden", "name": "source_pair", "value": ""} for f in saved.attrs("input")
    )
    held = Page(render_owner_setup(status(), csrf_token="t"))
    assert not held.attrs("form") and "No eligible workflow trace" in "".join(held.words)
    held_saved = Page(render_owner_setup(status(revision="a" * 64), csrf_token="t"))
    assert [opt["value"] for opt in held_saved.attrs("option")] == ["pause", "revoke"]
    page = Page(render_owner_setup(status(may_preview=False), csrf_token="generated-csrf"))
    assert not page.attrs("form") and "generated-csrf" not in "".join(page.words)


def test_signing_payload_round_trips_and_result_renders_stable_revision():
    import json

    prior = {"policy": {"mandate": {"max_runs": 1}, "currency": "USD", "connection_file": "local"}}
    claim = {
        "action": "renew",
        "prior_revision": "a" * 64,
        "policy": {
            "mandate": {"max_runs": 2},
            "currency": "USD",
            "connection_file": "local",
            "agreement": {"name": "</textarea><script>never()</script>"},
        },
    }
    payload = {"claim": claim, "not_after": "2026-11-28T01:00:00.000Z"}
    page = Page(
        render_owner_setup(
            status(),
            csrf_token="t",
            preview={"payload": payload, "prior": prior, "changed_fields": ["max_runs"]},
        )
    )
    assert not page.attrs("script") and not page.attrs("a")
    assert page.attrs("textarea")[0]["readonly"] is None
    assert any(
        json.loads(word) == payload
        for word in page.words
        if word.startswith("{") and '"claim"' in word
    )
    assert "agreement" in page.words and "max_runs" in page.words
    assert {f["name"]: f["value"] for f in page.attrs("input")} == {
        "action": "submit",
        "csrf_token": "t",
    }
    assert page.attrs("textarea")[1]["name"] == "signed_packet"
    receipt = {"revision": "r-saved", "state": "active", "secret": "unprojected"}
    saved = render_owner_setup(status(revision="r-saved"), csrf_token="generated", receipt=receipt)
    assert Page(saved).attrs("a") == [{"href": "/owner/setup"}]
    assert not Page(saved).attrs("form") and "No eligible workflow" not in saved
    assert "Saved settings" in saved and "r-saved" in saved and "unprojected" not in saved


@pytest.mark.parametrize(
    "failure,notice",
    [
        ("stale", "revision changed"),
        ("expired", "preview expired"),
        ("internal sensitive exception", "unavailable"),
    ],
)
def test_denied_page_has_no_controls_or_prior_private_values(failure, notice):
    html = render_owner_setup(
        status(),
        csrf_token="private-token",
        receipt={"revision": "private"},
        preview={"payload": "private"},
        failure=failure,
    )
    p = Page(html)
    assert notice in html and not p.attrs("form") and not p.attrs("textarea")
    assert not any(
        s in html for s in ("generated-order", "private-token", "internal sensitive exception")
    )
