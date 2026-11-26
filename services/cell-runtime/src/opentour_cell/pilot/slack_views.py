"""Native Slack messages and modal views for one authorized pilot workspace."""

from __future__ import annotations

import html
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from opentour_cell.pilot.readout import INPUT_VERSION, MAX_CASES, OUTPUT_VERSION, evaluate_manifest
from opentour_cell.pilot.text_classifier import (
    CPU_LIMIT_SECONDS,
    EPOCH_CHOICES,
    MAX_HELDOUT_ROWS,
    MAX_TRAIN_ROWS,
    PROFILE,
)
from opentour_cell.pilot.training_data import EVALUATION_FIELDS, FIELD_NAMES

Block = dict[str, Any]
SEARCH_LIMIT = 64
MANAGED_OUTCOMES = {
    "simple_repair": "The simple repair qualified for handoff.",
    "rule": "The simple repair qualified for handoff.",
    "model_candidate": "The open-weight model qualified for handoff.",
    "model": "The open-weight model qualified for handoff.",
    "no_change": "Keep the incumbent.",
    "incumbent": "Keep the incumbent.",
    "hold": "Hold the handoff. The existing workflow remains in place.",
    "inconclusive": "Evidence is inconclusive. Hold the handoff.",
    "stop": "Stop this pilot change.",
}
# Only these locally defined descriptions can leave the operator store.
MANAGED_REASONS = {
    "insufficient_protected_support": "Too few verified protected cases met the agreed minimum.",
    "incomplete_protected_evidence": "Some protected outcomes or candidate decisions were missing.",
    "operating_point_missed_or_undetermined": (
        "The coverage or error limit was missed or unverified."
    ),
    "critical_slice_failed_or_unverified": "A required group failed its check or lacked evidence.",
    "cost_limit_failed_or_unverified": "The agreed cost limit was exceeded or unverified.",
    "latency_limit_failed_or_unverified": "The agreed latency limit was exceeded or unverified.",
    "quality_cost_tradeoff_requires_owner_choice": (
        "Higher observed cost requires an owner decision."
    ),
    "no_confirmed_improvement": "The protected comparison found no verified error reduction.",
    "protected_improvement_at_agreed_limits": "Verified errors fell at the agreed limits.",
    "adequate_simple_repair_at_lower_observed_cost": (
        "The simple repair met the limits at lower observed cost."
    ),
    "different_automatic_cases": "The compared methods automated different cases.",
    "regressed": "The candidate made more verified errors on the same automatic cases.",
    "insufficient_evidence": "There was too little matched evidence to recommend a change.",
    "recurring_residual_requires_model_and_compute_rights": (
        "The recurring error persisted. Separate model and compute authorization is required."
    ),
    "insufficient_independent_development_evidence": (
        "Too few independent development groups supported a candidate."
    ),
    "simple_repair_unproven_and_residual_support_insufficient": (
        "The simple repair and recurring residual had insufficient development support."
    ),
    "incumbent_tied_on_same_automatic_cases": (
        "Both methods had equal errors on the same automatic cases."
    ),
}

QUESTION_LABELS = {
    "errors": "Verified errors",
    "coverage": "Coverage and limits",
    "changes": "What got worse?",
    "audience": "Who receives it?",
}
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
    if audit and audit.get("mode") == "managed_rule":
        if "model" in audit.get("comparisons", {}):
            return "managed workflow · simple repair and open-weight model"
        return "managed workflow · simple repair"
    if audit and audit.get("mode") == "managed_model":
        return "managed workflow · open-weight model and simple repair"
    return "incumbent and repair comparison"


def _audit_blocks(audit: Mapping[str, Any]) -> list[Block]:
    labels = (
        ("job_id", "Job or evaluation"),
        ("dataset_id", "Dataset"),
        ("model_revision", "Model or method revision"),
        ("cohort_revision", "Tested cohort"),
        ("evidence_digest", "Evidence digest"),
        ("decision_quality_digest", "Decision quality digest"),
        ("run_revision", "Frozen workflow comparison"),
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
    answers: dict[str, str]

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
        errors = (
            f"Incorrect automatic decisions · incumbent {current.error()} "
            f"→ repair {repair.error()}."
        )
        coverage = f"Coverage {_ratio(current.auto, size)} → {_ratio(repair.auto, size)}. {same}"
        changes_text = f"Paired fixes {fixes}, regressions {regressions}."
        regressions_text = (
            f"What got worse? {regressions} of {common} comparable cases became wrong. "
            f"{fixes} became correct. {excluded} fell outside the paired comparison."
            if common
            else "What got worse? Undetermined. There were no comparable cases with verified data."
        )
        ceiling = (
            f"Error ceiling ≤{bps / 100:g}% · incumbent {current.ceiling(bps)}, "
            f"repair {repair.ceiling(bps)}."
        )
        summary = (
            f"{errors}\n{coverage}\nHuman review {current.review} → {repair.review}. "
            f"{changes_text} Unknown references {unknown}.\n{ceiling} {finding}"
        )
        details = [
            _section(regressions_text),
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
                "unmatched records belong to the approved intake record. "
                "Review counts measure routing."
            ),
        ]
        return cls(
            summary,
            details,
            {
                "errors": f"{errors} {ceiling} Automatic decisions with unknown references · "
                f"incumbent {current.unknown}, repair {repair.unknown}.",
                "coverage": f"{coverage} {ceiling} Eligible pairs {common}/{size}.",
                "changes": (
                    f"{regressions_text} {changes_text} {coverage} "
                    f"Eligible pairs {common}/{size}. {finding}"
                ),
            },
        )


def evidence_available(report: Mapping[str, Any] | None) -> bool:
    """True when the supplied readout has a renderable, internally consistent aggregate."""
    return _Evidence.read(report) is not None


def managed_report(value: Mapping[str, Any]) -> Block:
    """Project the existing v1 numeric schema. Drop row-level and unknown source fields."""
    template = evaluate_manifest(
        {"schema_version": INPUT_VERSION, "error_ceiling_bps": 0, "cases": []}
    )

    def project(original: Any, schema: Any) -> Any:
        if isinstance(schema, dict):
            if not isinstance(original, Mapping):
                raise ValueError("Unverified comparison.")
            return {key: project(original[key], child) for key, child in schema.items()}
        if isinstance(schema, str):
            if original != OUTPUT_VERSION and original not in _FINDINGS:
                raise ValueError("Unknown comparison.")
        elif original is not None and (
            type(original) not in (int, float, bool)
            or type(original) is float
            and not math.isfinite(original)
        ):
            raise ValueError("Expected aggregate number.")
        return original

    projected: Block = project(value, template)
    if not evidence_available(projected):
        raise ValueError("Unverified comparison.")
    return projected


def managed_operating_point(point: Mapping[str, Any], ceiling: int) -> Block:
    selected = {
        key: point[key]
        for key in (
            "schema_version",
            "agreement_revision",
            "minimum_coverage_bps",
            "error_ceiling_bps",
        )
    }
    for arm in ("current", "repair"):
        selected[arm] = {key: point[arm][key] for key in ("coverage", "error", "combined")}
    _operating_terms({"operating_point": selected, "error_ceiling_bps": ceiling})
    return selected


def _managed_outcome(audit: Mapping[str, Any] | None) -> str:
    if not audit or audit.get("mode") not in ("managed_rule", "managed_model"):
        return ""
    outcome = MANAGED_OUTCOMES[audit["handoff_disposition"]]
    reason = MANAGED_REASONS.get(audit.get("decision_reason", ""), "")
    return "Workflow comparison · " + " ".join(filter(None, (outcome, reason)))


def _operating_terms(audit: Mapping[str, Any]) -> str:
    terms = audit.get("operating_point")
    if terms is None:
        return "Descriptive comparison. A frozen minimum-coverage agreement is unavailable."
    if (
        not isinstance(terms, Mapping)
        or terms.get("schema_version") != "opentour.pilot.operating-point.v1"
        or not re.fullmatch(r"[0-9a-f]{64}", str(terms.get("agreement_revision", "")))
    ):
        raise ValueError("Unverified operating agreement.")
    minimum = _count(terms.get("minimum_coverage_bps"))
    ceiling = _count(terms.get("error_ceiling_bps"))
    if minimum > 10000 or ceiling > 10000 or ceiling != audit.get("error_ceiling_bps"):
        raise ValueError("Unverified operating limits.")
    lines = [
        f"Agreed minimum automated coverage ≥{minimum / 100:g}%. "
        f"Maximum wrong-automatic rate ≤{ceiling / 100:g}%.",
    ]
    for key, label in (("current", "Incumbent"), ("repair", "Repair")):
        arm = terms.get(key)
        if not isinstance(arm, Mapping) or any(
            arm.get(k) not in ("met", "missed", "undetermined")
            for k in ("coverage", "error", "combined")
        ):
            raise ValueError("Unverified operating outcome.")
        lines.append(
            f"{label} · coverage {arm['coverage']}, error {arm['error']}, "
            f"combined {arm['combined']}."
        )
    return "\n".join(lines)


def reviewer_help(context: str, message: str = "") -> Block:
    blocks = [_section(message)] if message else []
    blocks.extend(
        [
            _section(
                "Open /opentour review to choose a readout. Open its evidence to ask about "
                "errors, coverage, changes or audience. Only the named reviewer's buttons "
                "record a decision."
            ),
            _section(
                "Use /opentour data to browse datasets, /opentour search FIELD to find one, "
                "or /opentour status JOB to inspect an authorized job. Named trainers can "
                "preview a preapproved workflow with /opentour repair SOURCE. "
                "Only the confirmation button queues the run."
            ),
            _actions(
                _button("Review results", "ot_readouts", "home"),
                _button("Browse data", "ot_browse", "home"),
            ),
        ]
    )
    return _modal("Pilot help", "ot_help_view", context, blocks)


def managed_launch_preview(context: str, summary: Mapping[str, Any]) -> Block:
    dataset = _ref(summary["dataset_id"])
    revision = _ref(summary["plan_revision"])
    conditional = summary["scope"] == "conditional_open_weight"
    method = (
        "Start with the incumbent and simple repair. An open-weight adapter may run only "
        "if the confirmed error persists and separate source, model and compute grants are current."
        if conditional
        else "Compare the incumbent and one frozen simple repair on the protected cases. "
        "This scope permits the simple-repair comparison only."
    )
    button = _button("Queue workflow", "ot_repair_confirm", revision)
    button["style"] = "primary"
    button["confirm"] = {
        "title": _text("Queue this workflow?"),
        "text": _text("The local worker will use this exact plan under its existing permissions."),
        "confirm": _text("Queue workflow"),
        "deny": _text("Go back"),
    }
    return _modal(
        "Preview repair",
        "ot_repair_preview",
        context,
        [
            _section(f"Source {dataset} · trainer {_ref(summary['trainer'])}\nPlan {revision}"),
            _section(method),
            _section(
                "The operator's local worker executes the queue. "
                "Review the protected comparison before approving an audience or handoff."
            ),
            _actions(button, _home()),
        ],
    )


def managed_launch_status(context: str, run_id: str, state: str, *, review_visible: bool) -> Block:
    states = {
        "queued": "Queued for the local worker",
        "running": "Running on the local worker",
        "ready": "Completed",
        "held": "Held for verification",
        "failed": "Execution failed",
    }
    label = states.get(state, "Status requires operator verification")
    buttons = [_button("Refresh status", "ot_repair_refresh", _ref(run_id))]
    if review_visible:
        buttons.append(_button("Review results", "ot_readouts", "home"))
    return _modal(
        "Repair status",
        "ot_repair_progress",
        context,
        [_section(f"{label}. Run {_ref(run_id)}."), _actions(*buttons)],
    )


def result_chooser(
    context: str, rows: Sequence[Mapping[str, Any]], *, previous: bool, more: bool
) -> Block:
    blocks = [_section("Choose the result to inspect. Each selection opens its own revision.")]
    for item in rows:
        section = _section(
            f"{_ref(item['dataset'])} · {_display(item['method'])}\n"
            f"{_display(item['state'])} · {_ref(item['reference'])}"
        )
        section["accessory"] = _button("Open result", "ot_result_open", item["token"])
        blocks.append(section)
    navigation = []
    if previous:
        navigation.append(_button("Previous", "ot_result_prev", "previous"))
    if more:
        navigation.append(_button("Next", "ot_result_next", "next"))
    navigation.extend(
        [_button("Browse data", "ot_browse", "home"), _button("Help", "ot_help", "home")]
    )
    blocks.append(_actions(*navigation))
    return _modal("Review results", "ot_review_list", context, blocks)


def regression_picker(
    context: str, rows: Sequence[tuple[str, str, str]], *, previous: bool, more: bool
) -> Block:
    blocks = [
        _section(
            "Choose an inspected frozen comparison to see which decisions became wrong."
            if rows
            else "No currently inspected comparison is available. Open a result and inspect "
            "its evidence before asking which decisions became wrong."
        )
    ]
    for dataset, source, revision in rows:
        blocks.append(_section(f"Workflow {_ref(dataset)} · {_display(source)} · {_ref(revision)}"))
        blocks.append(_actions(_button("Use this comparison", "ot_result_open", revision)))
    navigation = []
    if previous:
        navigation.append(_button("Previous", "ot_result_prev", "previous"))
    if more:
        navigation.append(_button("Next", "ot_result_next", "next"))
    navigation.append(_button("Browse results", "ot_readouts", "home"))
    blocks.append(_actions(*navigation))
    return _modal("Choose comparison", "ot_regression_picker", context, blocks)


def question_modal(
    context: str,
    revision: str,
    report: Mapping[str, Any],
    audit: Mapping[str, Any],
    topic: str = "",
) -> Block:
    facts = _Evidence.read(report)
    if facts is None:
        raise ValueError("Unverified evidence.")
    answers = {
        **facts.answers,
        "coverage": facts.answers["coverage"] + "\n" + _operating_terms(audit),
        "audience": f"Only {_display(audit.get('recipient'))} is the intended audience. "
        "Approve for readout permits presenting this revision. The workflow owner separately "
        "decides whether to use or deploy it.",
    }
    if audit.get("release_reviewer"):
        answers["audience"] = (
            f"The named reviewer is {_display(audit['release_reviewer'])}. "
            f"Approval of this candidate permits handoff to {_display(audit['recipient'])}. "
            "The customer separately decides whether to deploy it."
        )
    managed_outcome = _managed_outcome(audit)
    if managed_outcome:
        answers["changes"] = managed_outcome + "\n" + answers["changes"]
    blocks = [_section(f"Readout revision {_ref(revision)}")]
    if topic:
        blocks.append(_section(answers[topic]))
    blocks.append(
        _actions(
            *[
                _button(label, "ot_question_" + key, revision)
                for key, label in QUESTION_LABELS.items()
            ]
        )
    )
    blocks.append(_actions(_button("View evidence", "ot_evidence", revision), _home()))
    return _modal("Evidence questions", "ot_questions_view", context, blocks)


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
        result = _managed_outcome(audit)
        if result:
            blocks.append(_section(result))
            fallback += " " + result
        if evidence:
            summary = evidence.summary
            if audit and "operating_point" in audit:
                summary += "\n" + _operating_terms(audit)
            blocks.append(_section(summary))
            fallback += " " + summary
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
    reviewer_navigation: bool = False,
    workspace_title: str = "OpenTour pilot workspace",
    workflow_label: str = "",
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
    if workspace_title != "OpenTour pilot workspace" or workflow_label:
        blocks.insert(
            0,
            _section(
                _display(workspace_title)
                + (" · " + _display(workflow_label) if workflow_label else "")
            ),
        )
    if audit and evidence_reviewed and revision:
        blocks.extend(_audit_blocks(audit))
    if reviewer_navigation:
        blocks.append(
            _actions(
                _button("Review results", "ot_readouts", "home"), _button("Help", "ot_help", "home")
            )
        )
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
        "max_length": SEARCH_LIMIT,
        "placeholder": _text("Search an approved name or field"),
    }
    if query:
        search["initial_value"] = query
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
    recipe: bool = False,
    request_status: Mapping[str, Any] | None = None,
    context: str,
    revision: str,
    report: Mapping[str, Any] | None,
    recipient: str | None = None,
    dataset_revision: str | None = None,
    audit: Mapping[str, Any] | None = None,
    questions: bool = False,
    release_allowed: bool = False,
    charts_allowed: bool = False,
    benchmark_allowed: bool = False,
    workflow: str = "",
) -> Block:
    ref = _ref(revision)
    evidence = _Evidence.read(report)
    heading = f"Readout revision {ref} · {_model(audit)}"
    blocks = [_section(f"{_display(workflow)}\n{heading}" if workflow else heading)]
    if dataset_revision:
        blocks.append(_section(f"Frozen dataset revision {_ref(dataset_revision)}."))
    if release_allowed and audit and recipient:
        blocks.append(
            _section(
                f"Named reviewer {_ref(audit['release_reviewer'])} · handoff to "
                f"{_display(recipient)}. Inspect this exact candidate before deciding."
            )
        )
    elif recipient:
        blocks.append(
            _section(f"Requested action · present this revision to {_display(recipient)}.")
        )
    if evidence:
        blocks.extend(evidence.details)
        if audit and audit.get("mode") in {"managed_rule", "managed_model"}:
            if audit.get("population") == 0:
                blocks.append(
                    _section("No protected cases were evaluated. No comparable paired cases.")
                )
            if benchmark_allowed and "workflow_benchmark" in audit:
                from opentour_cell.pilot.slack_benchmark import benchmark_summary

                blocks.append(_section(benchmark_summary(audit["workflow_benchmark"])))
            elif benchmark_allowed:
                blocks.append(
                    _section(
                        "Workflow benchmark unavailable. This revision has no verified figures "
                        "for reviewer time, latency or cost."
                    )
                )
        else:
            blocks.append(_section("Time saved and future performance were not measured."))
        if audit:
            blocks.extend(_audit_blocks(audit))
            blocks.append(_section(_operating_terms(audit)))
            result = _managed_outcome(audit)
            if result:
                blocks.append(_section(result))
                for key, label in (("rule", "Simple repair"), ("model", "Open-weight model")):
                    pair = audit.get("comparisons", {}).get(key)
                    if pair:
                        comparison = _Evidence.read(pair)
                        if comparison is None:
                            raise ValueError("Unverified workflow arm.")
                        blocks.append(
                            _section(f"{label} against the incumbent\n{comparison.summary}")
                        )
        if (
            release_allowed
            and audit
            and recipient
            and audit.get("release_provider") == "zapier_slack"
        ):
            if recipe and request_status:
                blocks.extend(_plugin_status_blocks(request_status["request_id"], request_status))
            elif recipe:
                blocks.append(
                    _section(
                        f"Candidate {_ref(audit['model_revision'])}. Evaluation handoff only to "
                        f"{_display(recipient)}. The incumbent remains the fallback. "
                        "The request expires within 15 minutes or sooner if authority expires."
                    )
                )
                proceed = _button("Continue to approval", "ot_plugin_continue", ref)
                proceed["style"] = "primary"
                blocks.append(_actions(proceed))
            else:
                blocks.append(
                    _section(
                        "This workspace uses Slack Request Approval in Zapier. After inspection, "
                        "use your assigned approval to approve this candidate or request changes. "
                        "The worker stages the request for the configured Zap."
                    )
                )
        elif release_allowed and audit and recipient:
            artifact = _ref(audit["model_revision"])
            blocks.append(
                _section(
                    f"Candidate {artifact}. Approval permits preparing this frozen "
                    "repair for the named audience. The incumbent stays active "
                    "until the customer makes a separate deployment decision."
                )
            )
            approve = _button("Approve repair for handoff", "ot_release_approve", ref)
            approve["style"] = "primary"
            approve["confirm"] = {
                "title": _text("Approve this repair?"),
                "text": _text(f"Permit handoff of {artifact} to {_display(recipient)}?"),
                "confirm": _text("Approve handoff"),
                "deny": _text("Go back"),
            }
            change = _button("Request changes", "ot_release_changes", ref)
            change["style"] = "danger"
            change["confirm"] = {
                "title": _text("Request changes?"),
                "text": _text("Keep this candidate on hold for its named audience."),
                "confirm": _text("Request changes"),
                "deny": _text("Go back"),
            }
            blocks.append(_actions(approve, change))
        explore = []
        if recipe and not release_allowed:
            explore.append(_button("View current status", "ot_managed_review_status", ref))
        if recipe and report:
            explore.append(
                _button(
                    f"Inspect regression ({report['paired']['regressions']})",
                    "ot_question_changes",
                    ref,
                )
            )
        if questions:
            explore.append(_button("Ask about evidence", "ot_question", ref))
        if charts_allowed:
            explore.append(
                _button("Compare arms" if recipe else "View comparison charts", "ot_charts", ref)
            )
        if explore:
            blocks.append(_actions(*explore))
    else:
        blocks.append(
            _section(
                "This view has no verified, authorized aggregate. Use the owner's "
                "approved private evidence or witnessed review. Handoff is held."
            )
        )
    blocks.append(_actions(_home()))
    return _modal("Readout evidence", "ot_evidence_view", context, blocks)


def chart_status(context: str, revision: str, figures: str, status: str) -> Block:
    messages = {
        "accepted": "Slack accepted a chart message for you in the private pilot channel.",
        "uncertain": "Slack could not confirm the chart message. The same figures are below.",
        "numeric": "The available evidence is shown as figures below.",
    }
    return _modal(
        "Comparison charts",
        "ot_chart_status",
        context,
        [
            _section(messages[status]),
            _section(figures),
            _actions(_button("Back to evidence", "ot_evidence", revision), _home()),
        ],
    )


def release_recorded(context: str, decision: str, artifact: str, audience: str) -> Block:
    state = "Approved for handoff" if decision == "approved" else "Changes requested. Handoff held"
    return _modal(
        "Handoff decision",
        "ot_release_recorded",
        context,
        [
            _section(f"{state}. Candidate {_ref(artifact)} · audience {_display(audience)}."),
            _actions(_home()),
        ],
    )


def app_home_view(
    context: str = "",
    title: str = "",
    workflow: str = "",
    *,
    can_review: bool = False,
    can_tune: bool = False,
    named_reviewer: bool = False,
    pending: str = "",
    can_add_workflow: bool = False,
) -> Block:
    if not context:
        return {
            "type": "home",
            "blocks": [
                _section(
                    "OpenTour access is unavailable. Ask your pilot "
                    "operator to check your current access."
                )
            ],
        }
    blocks = [{"type": "header", "text": _text(_display(title))}]
    if workflow:
        blocks.append(_section("Workflow · " + _display(workflow)))
    nav = [
        _button("Browse data", "ot_app_data", "ot_app_data"),
        _button("Ask OpenTour", "ot_app_prompt", "ot_app_prompt"),
    ]
    if pending:
        blocks.append(_section("A repair is ready for your review."))
        first = _button("Review pending repair", "ot_app_pending", pending)
        first["style"] = "primary"
        nav.insert(0, first)
    elif can_review:
        if named_reviewer:
            blocks.append(_section("No repair needs your review."))
        nav.insert(0, _button("Review results", "ot_app_review", "ot_app_review"))
    if can_add_workflow:
        blocks.append(
            {
                **_actions(_button("Add workflow", "ot_app_add_workflow", "ot_app_add_workflow")),
                "block_id": "ot_workflow_entry",
            }
        )
    if can_tune:
        nav.append(_button("Preview a repair", "ot_app_repair", "ot_app_repair"))
    blocks.extend(
        [
            _section("Choose a task to open a private view. Access is checked each time."),
            _actions(*nav),
        ]
    )
    return {
        "type": "home",
        "callback_id": "ot_app_home",
        "private_metadata": _ref(context),
        "blocks": blocks,
    }


def workflow_chooser(context: str) -> Block:
    choices = _actions(
        _button("We’re starting from scratch", "ot_workflow_starter", "starter"),
        _button("Connect existing decisions", "ot_workflow_historical", "historical"),
    )
    choices["block_id"] = "ot_workflow_choices"
    return _modal(
        "Add workflow",
        "ot_workflow_chooser",
        context,
        [_section("Choose how to set up this workflow privately."), choices],
    )


def workflow_instructions(context: str, route: str) -> Block:
    if route == "starter":
        title = "Start from a policy"
        message = (
            "Choose one permitted policy and its revision. Identify the decision it governs "
            "with your workflow owner. Use private starter setup when available to save a draft."
        )
    elif route == "historical":
        title = "Connect decisions"
        message = (
            "Identify the original decision feed and an independent correction source. "
            "Ask the authorized data custodian to confirm permission for both sources, "
            "then submit the approved pair through the customer’s intake connection "
            "using submit_intake."
        )
    else:
        raise ValueError("Unknown workflow route")
    return _modal(title, "ot_workflow_" + route + "_info", context, [_section(message)])


def prompt_modal(context: str, workflow: str, *, repair: bool = False) -> Block:
    entry: Block = {"type": "plain_text_input", "action_id": "command", "max_length": 160}
    if repair:
        entry["initial_value"] = "repair "
    examples = (
        "Enter repair followed by your approved source ID. The next screen previews the exact "
        "plan. Only a separate confirmation can queue it."
        if repair
        else "Try review, search FIELD, status RUN, or ask coverage after inspecting a "
        "result. Named trainers can preview an authorized plan with repair SOURCE."
    )
    return _modal(
        "Ask OpenTour",
        "ot_prompt_enter",
        context,
        [
            _section(examples),
            {
                "type": "input",
                "block_id": "command",
                "label": _text("Pilot command" + (" · " + _display(workflow) if workflow else "")),
                "element": entry,
            },
        ],
        "Continue",
    )


def plugin_preview_modal(context: str, preview: Mapping[str, Any]) -> Block:
    expires = datetime.fromtimestamp(preview["expires_at"], UTC).isoformat()
    button = _button("Request provider vote", "ot_plugin_request", preview["request_id"])
    button["style"] = "primary"
    return _modal(
        "Confirm approval request",
        "ot_evidence_view",
        context,
        [
            _section(f"Version {preview['run_revision']}. Candidate {preview['artifact_sha']}."),
            _section(
                f"Recipient {_display(preview['audience'])}. Operation: {preview['operation']}. "
                f"Fallback: {preview['fallback']}. Expires {expires}."
            ),
            _actions(button),
        ],
    )


def managed_review_status(context: str, audit: Mapping[str, Any]) -> Block:
    return _modal(
        "Current review status",
        "ot_evidence_view",
        context,
        [
            _section(_managed_outcome(audit)),
            _actions(_home()),
        ],
    )


def _plugin_status_blocks(
    request_id: str, status: Mapping[str, Any] | None, show_receipt: bool = False
) -> list[Block]:
    if status and "receipt_sha256" in status:
        blocks = [_section(f"Provider decision: {status['state']}.")]
        if show_receipt:
            blocks.append(
                _section(
                    f"Receipt {status['receipt_sha256']}. Candidate {status['artifact_sha']}. "
                    f"Evaluation handoff audience: {_display(status['audience'])}."
                )
            )
        else:
            blocks.append(_actions(_button("View receipt", "ot_plugin_receipt", request_id)))
    else:
        blocks = [
            _section(
                {
                    "staged": "The request is staged. The worker has not confirmed dispatch.",
                    "accepted": "The provider accepted dispatch. Waiting for a signed decision.",
                    "uncertain": (
                        "Dispatch is uncertain. Reconcile the original request in Zap history."
                    ),
                    "failed": "Dispatch failed. Reconcile the original request in Zap history.",
                    "expired": "The request expired. Reopen the current evidence.",
                }[status["state"] if status else "staged"]
            ),
            _actions(_button("View current status", "ot_plugin_status", request_id)),
        ]
    return blocks


def plugin_waiting_modal(
    context: str,
    request_id: str,
    status: Mapping[str, Any] | None = None,
    *,
    show_receipt: bool = False,
) -> Block:
    return _modal(
        "Approval request",
        "ot_evidence_view",
        context,
        _plugin_status_blocks(request_id, status, show_receipt),
    )


def plugin_held_modal(modal: Block) -> Block:
    return {
        **modal,
        "blocks": [
            *modal["blocks"][:-1],
            _section(
                "Evidence inspected. The Zapier approval request is on hold. Ask the operator "
                "to check the approval policy and connection, then reopen this evidence."
            ),
            modal["blocks"][-1],
        ],
    }
