"""Native Slack messages and modal views for one authorized pilot workspace."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from opentour_cell.pilot.readout import MAX_CASES, OUTPUT_VERSION
from opentour_cell.pilot.text_classifier import (
    CPU_LIMIT_SECONDS,
    EPOCH_CHOICES,
    MAX_HELDOUT_ROWS,
    MAX_TRAIN_ROWS,
    PROFILE,
)
from opentour_cell.pilot.training_data import EVALUATION_FIELDS, FIELD_NAMES

Block = dict[str, Any]
_REF = re.compile(r"[A-Za-z0-9_.:~-]{1,160}\Z")
_MODEL = "CPU text classifier · multiclass perceptron (cpu_text_v1)"
_SAMPLE_FIELDS = ("case_id", "text", "label")
_MODES = {"training": "Training and evaluation", "evaluation_only": "Evaluation only"}
_STAGES = {
    "pending": "Awaiting a completed evaluation",
    "ready": "Ready for evidence review",
    "stale": "Handoff held · superseded revision",
    "uncertain": "Pending verification · hold handoff",
    "held": "Handoff held",
    "failed": "Evaluation failed",
    "running": "Training in progress",
    "expired": "Authorization expired",
    "withdrawn": "Authorization withdrawn",
    "approved": "Cleared for readout",
    "changes": "Changes requested",
}
_FINDINGS = {
    "improved": "Fewer observed errors on the same automatic cases.",
    "regressed": "More observed errors on the same automatic cases.",
    "equivalent": "Equal observed error counts on the same automatic cases.",
    "different_automatic_cases": "Different cases were automated. Compare only eligible pairs.",
    "insufficient_evidence": "Insufficient evidence for an overall comparison.",
}


def _ref(value: object) -> str:
    if not isinstance(value, str) or not _REF.fullmatch(value):
        raise ValueError("Invalid opaque Slack reference.")
    return value


def _display(value: object, limit: int = 120) -> str:
    if not isinstance(value, str):
        return ""
    # Plain-text objects prevent Slack formatting. Escape reserved mention/link delimiters too.
    cleaned = " ".join("".join(c if c.isprintable() else " " for c in value).split())
    escaped = html.escape(cleaned, quote=False)
    return escaped if len(escaped) <= limit else escaped[: limit - 1] + "…"


def _text(value: str) -> Block:
    return {"type": "plain_text", "text": value, "emoji": False}


def _section(value: str) -> Block:
    return {"type": "section", "text": _text(value)}


def _button(label: str, action: str, value: str) -> Block:
    return {"type": "button", "action_id": action, "text": _text(label), "value": _ref(value)}


def _actions(*buttons: Block) -> Block:
    return {"type": "actions", "elements": list(buttons)}


def _home() -> Block:
    return _button("Back to workspace", "ot_home", "home")


def _modal(
    title: str, callback: str, context: str, blocks: list[Block], submit: str | None = None
) -> Block:
    view = {
        "type": "modal",
        "title": _text(title),
        "callback_id": callback,
        "close": _text("Close"),
        "private_metadata": _ref(context),
        "blocks": blocks,
    }
    if submit:
        view["submit"] = _text(submit)
    return view


def notice_modal(kind: str) -> Block:
    """Non-interactive feedback, including before a grant or interaction context exists."""
    notices = {
        "checking": ("Checking…", "Checking your current workspace, channel and dataset access."),
        "working": (
            "Working…",
            "Checking the local job record. Use /opentour status JOB to reopen it.",
        ),
        "denied": (
            "Access unavailable",
            "Ask the pilot operator to verify your workspace, private "
            "channel, dataset grant and expiry. Reopen /opentour after access is confirmed.",
        ),
        "uncertain": (
            "Pending verification",
            "The last response could not be verified. "
            "Reopen the workspace to check the owner record before repeating an action.",
        ),
    }
    title, message = notices.get(kind, notices["denied"])
    return {
        "type": "modal",
        "title": _text(title),
        "close": _text("Close"),
        "blocks": [_section(message)],
    }


def _trainable(row: Mapping[str, Any]) -> bool:
    return row.get("mode") == "training" and row.get("can_train") is True


def _model(audit: Mapping[str, Any] | None) -> str:
    if audit and audit.get("mode") == "training":
        return _MODEL
    if audit and audit.get("mode") == "evaluation_only":
        return "supplied incumbent and repair outputs"
    return "incumbent and repair comparison"


def _audit_blocks(audit: Mapping[str, Any]) -> list[Block]:
    labels = (
        ("job_id", "Job or evaluation"),
        ("dataset_id", "Dataset"),
        ("model_revision", "Model or method revision"),
        ("cohort_revision", "Tested cohort"),
        ("evidence_digest", "Evidence digest"),
        ("reviewer", "Named reviewer"),
        ("viewer", "Opened by"),
        ("team", "Workspace"),
        ("channel", "Private channel"),
    )
    sources = [f"{label} · {_ref(audit[key])}" for key, label in labels if audit.get(key)]
    if audit.get("mode") == "training":
        if audit.get("epochs") is not None and audit.get("cpu_limit_seconds") is not None:
            sources.append(
                f"Fit limits · {_count(audit['epochs'])} epochs, "
                f"{_count(audit['cpu_limit_seconds'])} CPU seconds."
            )
    when = audit.get("presented_at")
    if isinstance(when, int | float) and not isinstance(when, bool) and 0 < when < 253402300800:
        sources.append(f"Presentation recorded · {datetime.fromtimestamp(when, UTC).isoformat()}.")
    return [_section("\n".join(sources))] if sources else []


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Invalid aggregate count.")
    return value


def _ratio(n: int, d: int) -> str:
    if d == 0:
        return f"{n}/0 (undefined)"
    pct = f"{100 * n / d:.2f}".rstrip("0").rstrip(".")
    return f"{n}/{d} ({pct}%)"


@dataclass(frozen=True)
class _Arm:
    auto: int
    review: int
    missing: int
    unknown: int
    wrong: int

    @classmethod
    def read(cls, raw: Mapping[str, Any], size: int) -> _Arm:
        arm = cls(
            *(
                _count(raw[k])
                for k in (
                    "automatic",
                    "review",
                    "missing",
                    "unknown_reference_automatic",
                    "incorrect_automatic",
                )
            )
        )
        if arm.auto + arm.review + arm.missing != size or arm.wrong + arm.unknown > arm.auto:
            raise ValueError("Inconsistent arm counts.")
        for key, n, d in (
            ("coverage", arm.auto, size),
            ("observed_error_rate", arm.wrong, arm.auto - arm.unknown),
        ):
            if _count(raw[key]["numerator"]) != n or _count(raw[key]["denominator"]) != d:
                raise ValueError("Inconsistent rate denominator.")
        return arm

    def error(self) -> str:
        return _ratio(self.wrong, self.auto - self.unknown)

    def ceiling(self, bps: int) -> str:
        if not self.auto or self.unknown:
            return "undetermined"
        return "met" if self.wrong * 10000 <= bps * self.auto else "exceeded"


@dataclass(frozen=True)
class _Evidence:
    summary: str
    details: list[Block]

    @classmethod
    def read(cls, report: Mapping[str, Any] | None) -> _Evidence | None:
        if not isinstance(report, Mapping) or report.get("schema_version") != OUTPUT_VERSION:
            return None
        try:
            size, known, unknown, bps = (
                _count(report[k])
                for k in (
                    "cohort_size",
                    "known_references",
                    "unknown_references",
                    "error_ceiling_bps",
                )
            )
            current = _Arm.read(report["current"], size)
            repair = _Arm.read(report["repair"], size)
            pair = report["paired"]
            common, excluded, fixes, regressions, correct, wrong = (
                _count(pair[k])
                for k in (
                    "comparable",
                    "excluded",
                    "fixes",
                    "regressions",
                    "ties_correct",
                    "ties_incorrect",
                )
            )
            changes = report["changes"]
            old_only = _count(changes["current_only_automatic"])
            new_only = _count(changes["repair_only_automatic"])
            if (
                size > MAX_CASES
                or known + unknown != size
                or bps > 10000
                or common + excluded != size
                or common != fixes + regressions + correct + wrong
                or old_only > current.auto
                or new_only > repair.auto
                or current.auto - old_only != repair.auto - new_only
                or max(current.unknown, repair.unknown) > unknown
                or common
                > min(
                    known,
                    current.auto - old_only,
                    current.auto - current.unknown,
                    repair.auto - repair.unknown,
                )
                or fixes + wrong > current.wrong
                or regressions + wrong > repair.wrong
            ):
                return None
            comparison = "insufficient_evidence"
            if common and not (
                current.missing or repair.missing or current.unknown or repair.unknown
            ):
                if old_only or new_only:
                    comparison = "different_automatic_cases"
                elif fixes == regressions:
                    comparison = "equivalent"
                else:
                    comparison = "improved" if fixes > regressions else "regressed"
            if comparison != report["observed_comparison"]:
                return None
            finding = _FINDINGS[comparison]
        except (KeyError, TypeError, ValueError):
            return None
        same = "Different automatic cases." if old_only or new_only else "Same automatic cases."
        summary = (
            f"Incorrect automatic decisions · incumbent {current.error()} "
            f"→ repair {repair.error()}.\n"
            f"Coverage {_ratio(current.auto, size)} → {_ratio(repair.auto, size)}. {same}\n"
            f"Human review {current.review} → {repair.review}. "
            f"Paired fixes {fixes}, regressions {regressions}. Unknown references {unknown}.\n"
            f"Error ceiling ≤{bps / 100:g}% · incumbent {current.ceiling(bps)}, "
            f"repair {repair.ceiling(bps)}. {finding}"
        )
        details = [
            _section(summary),
            _section(
                f"Supplied evaluation cases {size}. Known references {known}, unknown {unknown}. "
                "Routed to human review / supplied cases · "
                f"incumbent {_ratio(current.review, size)}, "
                f"repair {_ratio(repair.review, size)}. "
                f"Missing outputs · incumbent {current.missing}, repair {repair.missing}. "
                f"Automatic decisions with unknown reference · incumbent {current.unknown}, "
                f"repair {repair.unknown}."
            ),
            _section(
                f"Eligible pairs {common}/{size}. Outside the paired comparison {excluded}. "
                f"Both correct {correct}, both incorrect {wrong}. "
                f"Automated by incumbent alone {old_only}, by repair alone {new_only}."
            ),
            _section(
                "Observed counts describe this supplied cohort. Upstream exclusions and "
                "unmatched records belong to the approved intake record. Review counts measure "
                "routing. Time saved and future performance were not measured."
            ),
        ]
        return cls(summary, details)


def evidence_available(report: Mapping[str, Any] | None) -> bool:
    """True when the supplied readout has a renderable, internally consistent aggregate."""
    return _Evidence.read(report) is not None


def _decision(label: str, action: str, revision: str, recipient: str, *, approve: bool) -> Block:
    button = _button(label, action, revision)
    button["style"] = "primary" if approve else "danger"
    effect = "Present" if approve else "Hold"
    button["confirm"] = {
        "title": _text(label),
        "text": _text(f"{effect} {revision} for {recipient}?"),
        "confirm": _text(label),
        "deny": _text("Cancel"),
    }
    return button


def workbench_message(
    *,
    revision: str | None,
    reviewer: str,
    recipient: str,
    state: str = "ready",
    report: Mapping[str, Any] | None = None,
    channel_aggregates: bool = False,
    private_evidence: bool = False,
    witnessed: bool = False,
    job_id: str | None = None,
    evidence_reviewed: bool = False,
    dataset_revision: str | None = None,
    receipt: str | None = None,
    tune_allowed: bool = True,
    decision_allowed: bool = True,
    audit: Mapping[str, Any] | None = None,
) -> Block:
    """A chat.postMessage/chat.update body. The caller verifies grants and the durable state."""
    ref = _ref(revision) if revision else None
    audience = _display(recipient)
    person = _display(reviewer)
    state_text = _STAGES.get(state, "Status unverified · hold handoff")
    if state in ("approved", "changes") and not (receipt and ref and audience and person):
        state_text = _STAGES["uncertain"]
    elif state in ("approved", "changes"):
        state_text += f" · owner receipt {_ref(receipt)}"
    elif state == "ready" and not ref:
        state_text = _STAGES["pending"]
    navigation = [_button("Browse data", "ot_browse", ref or "home")]
    if tune_allowed:
        navigation.append(_button("Tune model", "ot_tune", ref or "home"))
    if job_id:
        navigation.append(_button("Run status", "ot_status", _ref(job_id)))
    blocks = [{"type": "header", "text": _text("OpenTour pilot workspace")}, _actions(*navigation)]
    checked = _Evidence.read(report) if channel_aggregates or private_evidence else None
    malformed = report is not None and (channel_aggregates or private_evidence) and checked is None
    evidence = checked if channel_aggregates else None
    fallback = "OpenTour pilot workspace. " + state_text + "."
    if ref:
        scope = f"Readout revision {ref} · {_model(audit)}. "
        if dataset_revision:
            scope += f"Dataset revision {_ref(dataset_revision)}. "
        scope += f"Reviewer {person or 'unassigned'}. "
        scope += f"Requested action · present this revision to {audience or 'unassigned'} only."
        blocks.extend([_section(scope), _section(state_text)])
        fallback += " " + scope
        if evidence:
            blocks.append(_section(evidence.summary))
            fallback += " " + evidence.summary
        elif malformed:
            blocks.append(_section("Handoff held. The supplied aggregate could not be verified."))
        elif witnessed:
            blocks.append(
                _section("An owner-witnessed evidence review is recorded for this revision.")
            )
        elif private_evidence:
            blocks.append(
                _section("Authorized reviewers can inspect evidence in a private Slack view.")
            )
        else:
            blocks.append(_section("Handoff held until an authorized evidence route is available."))
        options = []
        if not malformed and (evidence or private_evidence):
            options.append(_button("View evidence", "ot_evidence", ref))
        if (
            state == "ready"
            and decision_allowed
            and audience
            and person
            and not malformed
            and (witnessed or (evidence_reviewed and (private_evidence or evidence)))
        ):
            options.extend(
                [
                    _decision("Approve for readout", "ot_approve", ref, audience, approve=True),
                    _decision("Request changes", "ot_changes", ref, audience, approve=False),
                ]
            )
        elif (
            state == "ready"
            and decision_allowed
            and not malformed
            and (evidence or private_evidence)
        ):
            blocks.append(
                _section("Open the evidence for this revision before recording a decision.")
            )
        if options:
            blocks.append(_actions(*options))
    else:
        blocks.append(_section(state_text))
        blocks.append(
            _section(
                "Browse an approved dataset. "
                "A finished evaluation is needed for a readout decision."
            )
        )
    return {
        "text": fallback,
        "blocks": blocks,
        "mrkdwn": False,
        "unfurl_links": False,
        "unfurl_media": False,
    }


def home_modal(
    *,
    context: str,
    revision: str | None,
    reviewer: str,
    recipient: str,
    state: str = "pending",
    report: Mapping[str, Any] | None = None,
    channel_aggregates: bool = False,
    private_evidence: bool = False,
    witnessed: bool = False,
    job_id: str | None = None,
    evidence_reviewed: bool = False,
    dataset_revision: str | None = None,
    receipt: str | None = None,
    tune_allowed: bool = True,
    decision_allowed: bool = True,
    audit: Mapping[str, Any] | None = None,
) -> Block:
    message = workbench_message(
        revision=revision,
        reviewer=reviewer,
        recipient=recipient,
        state=state,
        report=report,
        channel_aggregates=channel_aggregates,
        private_evidence=private_evidence,
        witnessed=witnessed,
        job_id=job_id,
        evidence_reviewed=evidence_reviewed,
        dataset_revision=dataset_revision,
        receipt=receipt,
        tune_allowed=tune_allowed,
        decision_allowed=decision_allowed,
        audit=audit,
    )
    blocks = message["blocks"][1:]
    if audit and evidence_reviewed and revision:
        blocks.extend(_audit_blocks(audit))
    return _modal("Pilot workspace", "ot_home_view", context, blocks)


def dataset_modal(
    *,
    context: str,
    datasets: Sequence[Mapping[str, Any]],
    query: str = "",
    detail: Mapping[str, Any] | None = None,
    tune_allowed: bool = True,
    evidence_revision: str | None = None,
) -> Block:
    if detail is not None:
        dataset = _ref(detail["dataset_id"])
        revision = _ref(detail["dataset_revision"])
        training = _trainable(detail)
        allowance = min(_count(detail.get("example_limit", 0)), 3) if training else 0
        fields = [name for name in _SAMPLE_FIELDS if training and name in detail.get("fields", ())]
        mode = _MODES.get(str(detail.get("mode")), "Unverified enrollment")
        blocks = [
            _section(f"{dataset} · revision {revision}"),
            _section(
                f"{mode}. "
                f"Approved purpose · {_display(detail.get('purpose')) or 'Ask the operator'}. "
                f"Training cases {_count(detail['train_count'])}. "
                f"Held-out cases {_count(detail['heldout_count'])}."
            ),
        ]
        allowed_names = (
            EVALUATION_FIELDS if detail.get("mode") == "evaluation_only" else FIELD_NAMES
        )
        visible = sorted(name for name in allowed_names if name in detail.get("visible_fields", ()))
        if visible:
            blocks.append(_section(f"Authorized field names · {', '.join(visible)}."))
        if detail.get("cohort_digest"):
            blocks.append(_section(f"Frozen cohort · {_ref(detail['cohort_digest'])}."))
        if training:
            blocks.append(
                _section(
                    f"Authorized training preview · up to {allowance} examples. "
                    f"Allowed fields · {', '.join(fields) or 'none'}."
                )
            )
        elif detail.get("mode") == "evaluation_only":
            blocks.append(
                _section(
                    "The owner authorized scoring supplied decisions. "
                    "Only the frozen evaluation summary is available here."
                )
            )
        if detail.get("scope") == "training_only" and allowance and fields:
            for row in detail.get("rows", [])[:allowance]:
                values = [
                    f"{field} · {_display(row[field], 180)}"
                    for field in fields
                    if isinstance(row.get(field), str)
                ]
                if values:
                    blocks.append(_section("\n".join(values)))
        nav = [_button("Back to datasets", "ot_dataset_back", dataset), _home()]
        if tune_allowed and training:
            nav.insert(0, _button("Tune this dataset", "ot_tune_dataset", dataset))
        if detail.get("mode") == "evaluation_only" and evidence_revision:
            nav.insert(0, _button("View evidence", "ot_evidence", evidence_revision))
        blocks.append(_actions(*nav))
        return _modal("Dataset details", "ot_dataset_detail", context, blocks)
    search: Block = {
        "type": "plain_text_input",
        "action_id": "query",
        "max_length": 100,
        "placeholder": _text("Search an approved name or field"),
    }
    if query:
        search["initial_value"] = _display(query, 100)
    blocks = [
        {
            "type": "input",
            "block_id": "query",
            "optional": True,
            "label": _text("Find a dataset"),
            "element": search,
        }
    ]
    for row in datasets[:20]:
        dataset = _ref(row["dataset_id"])
        section = _section(
            f"{dataset} · {_MODES.get(str(row.get('mode')), 'Unverified enrollment')}\n"
            f"Training {_count(row['train_count'])}, "
            f"held out {_count(row['heldout_count'])}"
        )
        section["accessory"] = _button("Inspect", "ot_dataset_open", dataset)
        blocks.append(section)
    if not datasets:
        blocks.append(_section("No authorized datasets match this search."))
    elif len(datasets) > 20:
        blocks.append(_section("Showing 20 matches. Narrow the search to see another dataset."))
    blocks.append(_actions(_home()))
    return _modal("Approved datasets", "ot_dataset_search", context, blocks, "Search")


def _select(key: str, label: str, options: Sequence[tuple[str, str]], initial: int = 0) -> Block:
    if any(len(value) > 150 for _, value in options):
        raise ValueError("Invalid Slack option reference.")
    entries = [{"text": _text(text[:75]), "value": _ref(value)} for text, value in options]
    return {
        "type": "input",
        "block_id": key,
        "label": _text(label),
        "element": {
            "type": "static_select",
            "action_id": key,
            "options": entries,
            "initial_option": entries[initial],
        },
    }


def tune_modal(*, context: str, datasets: Sequence[Mapping[str, Any]]) -> Block:
    datasets = [row for row in datasets if _trainable(row)]
    if not datasets:
        return _modal(
            "Tune a model",
            "ot_no_dataset",
            context,
            [
                _section(
                    "No dataset in this scope permits training. Open a registered dataset "
                    "to inspect its permitted uses."
                ),
                _actions(_home()),
            ],
        )
    choices = [(_ref(row["dataset_id"]), _ref(row["dataset_id"])) for row in datasets[:100]]
    blocks = [
        _select("dataset", "Approved dataset", choices),
        _select(
            "profile",
            "Model profile",
            [("CPU text classifier · multiclass perceptron", PROFILE)],
        ),
        _select("epochs", "Training epochs", [(str(n), str(n)) for n in EPOCH_CHOICES], 1),
        _section(
            "Preview the frozen revision and budget before starting. GPU and LLM profiles "
            "require a separately qualified trainer."
        ),
        _actions(_home()),
    ]
    if len(datasets) > 100:
        blocks.insert(
            -1,
            _section(
                "Showing the first 100 authorized datasets. Search the catalog and choose "
                "Tune this dataset to propose a run on another match."
            ),
        )
    return _modal("Tune a model", "ot_tune_preview", context, blocks, "Preview training")


def proposal_modal(*, context: str, job: Mapping[str, Any], recipient: str | None = None) -> Block:
    job_id = _ref(job["job_id"])
    audience = _display(recipient)
    try:
        epochs = _count(job["epochs"])
        train = _count(job["train_count"])
        heldout = _count(job["heldout_count"])
        updates = _count(job["max_train_updates"])
        cpu = _count(job["cpu_limit_seconds"])
    except (ValueError, KeyError):
        epochs = train = heldout = updates = cpu = 0
    if not (
        job.get("status") == "awaiting_approval"
        and audience
        and job.get("profile") == PROFILE
        and epochs in EPOCH_CHOICES
        and 2 <= train <= MAX_TRAIN_ROWS
        and 1 <= heldout <= MAX_HELDOUT_ROWS
        and updates == epochs * train
        and cpu == CPU_LIMIT_SECONDS
    ):
        return _modal(
            "Training proposal",
            "ot_held_proposal",
            context,
            [
                _section("Proposal held. An approved supported pending run is required."),
                _actions(_home()),
            ],
        )
    blocks = [
        _section(f"Start one supervised model fit for job {job_id}?"),
        _section(
            f"Dataset {_ref(job['dataset_id'])} · "
            f"frozen revision {_ref(job['dataset_revision'])}.\n"
            f"Model {_MODEL}. Epochs {epochs}."
        ),
        _section(
            f"Training cases {train}. Held-out cases {heldout}. "
            f"Up to {updates} training updates and {cpu} CPU seconds."
        ),
        _section(
            f"Intended readout audience · {audience}. This confirmation authorizes one local fit "
            "on the training split followed by scoring on the frozen holdout. "
            "Presentation and deployment each require separate authorization."
        ),
        _actions(_home()),
    ]
    return _modal("Confirm training", "ot_tune_start", context, blocks, "Start training")


def status_modal(
    *,
    context: str,
    job: Mapping[str, Any],
    evidence_revision: str | None = None,
    result_visible: bool = True,
    mode: str | None = None,
) -> Block:
    job_id = _ref(job["job_id"])
    stages = {
        "awaiting_approval": "Awaiting trainer confirmation",
        "running": "Running",
        "completed": "Completed",
        "failed": "Failed",
        "rejected": "Rejected",
        "uncertain": "Pending verification · hold",
    }
    state = str(job.get("status"))
    result = job.get("result")
    evaluation_only = (mode or job.get("mode")) == "evaluation_only"
    lease_expired = state == "running" and job.get("recoverable") is True
    if lease_expired:
        state = "uncertain"
    if state == "completed" and result_visible:
        try:
            if not isinstance(result, Mapping):
                raise ValueError("Missing result.")
            if evaluation_only:
                if not evidence_available(result.get("readout")):
                    raise ValueError("Missing verified evaluation.")
            elif not result.get("artifact_digest"):
                raise ValueError("Missing fitted artifact.")
            else:
                updates = _count(result["update_count"])
                weights = _count(result["changed_weights"])
        except (KeyError, ValueError):
            state = "uncertain"
    blocks = [
        _section(f"Job {job_id} · {stages.get(state, 'Status unverified · hold')}"),
        _section(f"Dataset {_ref(job['dataset_id'])} · revision {_ref(job['dataset_revision'])}."),
    ]
    buttons = [_button("Refresh status", "ot_status_refresh", job_id)]
    if lease_expired:
        blocks.append(
            _section(
                "The worker lease expired. Ask the owner-local operator to reconcile this job "
                "against its original approval, then use the documented recover command for "
                "this same job, reviewer and proposal revision."
            )
        )
    if state == "completed" and not result_visible:
        blocks.append(_section("Aggregate details are restricted for this account."))
    if state == "completed" and result_visible:
        if evaluation_only:
            blocks.append(_section("The frozen supplied incumbent and repair outputs were scored."))
        else:
            blocks.append(
                _section(f"Completed training updates {updates}. Changed weights {weights}.")
            )
        if evidence_revision:
            buttons.append(_button("View evidence", "ot_evidence", _ref(evidence_revision)))
    buttons.append(_home())
    blocks.append(_actions(*buttons))
    return _modal(
        "Evaluation status" if evaluation_only else "Training status",
        "ot_status_view",
        context,
        blocks,
    )


def evidence_modal(
    *,
    context: str,
    revision: str,
    report: Mapping[str, Any] | None,
    recipient: str | None = None,
    dataset_revision: str | None = None,
    audit: Mapping[str, Any] | None = None,
) -> Block:
    ref = _ref(revision)
    evidence = _Evidence.read(report)
    blocks = [_section(f"Readout revision {ref} · {_model(audit)}")]
    if dataset_revision:
        blocks.append(_section(f"Frozen dataset revision {_ref(dataset_revision)}."))
    if recipient:
        blocks.append(
            _section(f"Requested action · present this revision to {_display(recipient)}.")
        )
    if evidence:
        blocks.extend(evidence.details)
        if audit:
            blocks.extend(_audit_blocks(audit))
    else:
        blocks.append(
            _section(
                "This view has no verified, authorized aggregate. Use the owner's "
                "approved private evidence or witnessed review. Handoff is held."
            )
        )
    blocks.append(_actions(_home()))
    return _modal("Readout evidence", "ot_evidence_view", context, blocks)
