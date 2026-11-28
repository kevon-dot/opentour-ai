"""Classify bounded read-only requests before a worker resolves authorized scope."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class WorkflowMetric(StrEnum):
    RECURRING_INVOICE_CODE_ERRORS = "recurring_invoice_code_errors"


class CalendarWindow(StrEnum):
    PREVIOUS_MONTH = "previous_calendar_month"


class ComparisonArm(StrEnum):
    INCUMBENT = "incumbent"
    SIMPLE_RULE = "simple"


class ReadoutQuestion(StrEnum):
    REGRESSIONS = "regressions"
    WORKPLACE_SUPPORT = "independent_workplace_cases"
    SYNTHETIC_BENEFIT = "synthetic_benefit"
    LABEL_SAVINGS = "label_savings"


class QueryHold(StrEnum):
    VERSION_REQUIRED = "select_exact_benchmark_version"
    UNSUPPORTED = "choose_supported_query"


@dataclass(frozen=True, slots=True)
class WorkflowDiscovery:
    metric: WorkflowMetric
    window: CalendarWindow


@dataclass(frozen=True, slots=True)
class ArmChart:
    version: int
    arms: tuple[ComparisonArm, ComparisonArm]


QueryPlan = WorkflowDiscovery | ArmChart | ReadoutQuestion | QueryHold

_WORKFLOW_DISCOVERY = re.compile(
    r"(?:which|what) workflows (?:had|showed) (?:recurring|repeated) "
    r"invoice[ -]cod(?:e|ing) (?:errors|mistakes) (?:in )?(?:the )?"
    r"(?:last|previous) (?:calendar )?month"
)
_ARM_CHART = re.compile(
    r"(?:graph|chart|plot) (?:the )?(?:baseline|incumbent|current workflow) "
    r"(?:versus|vs\.?|against) (?:the )?(?:rule|simple rule|simple repair)"
    r"(?: (?:on|for) (?:latest|(?:v|version )(?P<version>[1-9][0-9]{0,18})"
    r"(?P<alternative> or (?:v|version )[1-9][0-9]{0,18})?))?"
)
_READOUT_QUESTIONS = (
    (
        re.compile(
            r"what (?:got|became) worse|(?:which|what) (?:cases|decisions|answers) "
            r"(?:(?:got|became) (?:worse|wrong|incorrect)|regressed)"
        ),
        ReadoutQuestion.REGRESSIONS,
    ),
    (
        re.compile(
            r"how many independent workplace cases (?:support|back) "
            r"(?:this(?: result| benchmark)?|the selected (?:result|benchmark))"
        ),
        ReadoutQuestion.WORKPLACE_SUPPORT,
    ),
    (
        re.compile(
            r"did (?:synthetic (?:data|examples|training)|augmentation) "
            r"(?:help|improve the (?:result|comparison))"
        ),
        ReadoutQuestion.SYNTHETIC_BENEFIT,
    ),
    (
        re.compile(r"how many (?:real |verified )(?:training )?labels did (?:this|we) save"),
        ReadoutQuestion.LABEL_SAVINGS,
    ),
)


def plan_query(prompt: str) -> QueryPlan:
    """Return the requested operation. Identity, clock, timezone and facts belong to the reader."""
    if len(prompt) > 160 or not prompt.isascii() or not prompt.isprintable():
        return QueryHold.UNSUPPORTED
    text = " ".join(prompt.lower().split()).removesuffix("?").rstrip()
    if _WORKFLOW_DISCOVERY.fullmatch(text):
        return WorkflowDiscovery(
            WorkflowMetric.RECURRING_INVOICE_CODE_ERRORS, CalendarWindow.PREVIOUS_MONTH
        )

    chart = _ARM_CHART.fullmatch(text)
    if chart:
        if chart["version"] is None or chart["alternative"] is not None:
            return QueryHold.VERSION_REQUIRED
        version = int(chart["version"])
        if version >= 2**63:  # The benchmark ledger stores the version as a signed INTEGER.
            return QueryHold.UNSUPPORTED
        return ArmChart(version, (ComparisonArm.INCUMBENT, ComparisonArm.SIMPLE_RULE))

    for pattern, topic in _READOUT_QUESTIONS:
        if pattern.fullmatch(text):
            return topic
    return QueryHold.UNSUPPORTED
