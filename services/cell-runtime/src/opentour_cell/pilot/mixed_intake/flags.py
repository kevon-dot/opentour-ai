"""Ambiguity flags for date/time-like text. Ambiguity goes to review, never to a silent guess."""

from __future__ import annotations

import re

_AMBIGUOUS_DATE = re.compile(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\s*$")
_NAIVE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")


def value_flags(value: str) -> tuple[str, ...]:
    """Flag date/time text whose meaning cannot be fixed without a guess."""
    flags = []
    match = _AMBIGUOUS_DATE.match(value)
    if match and int(match[1]) <= 12 and int(match[2]) <= 12 and match[1] != match[2]:
        flags.append("ambiguous_date")
    if _NAIVE_TIME.match(value.strip()):
        flags.append("timezone_unspecified")
    return tuple(flags)
