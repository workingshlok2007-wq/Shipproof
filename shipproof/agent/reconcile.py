"""Deadline reconciliation across sources. Recency decides only when every competing rule is dated."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from shipproof.models import Severity, UncertaintyKind
from shipproof.prompts import GroundedRule, RuleCategory


@dataclass(frozen=True)
class FlagSpec:
    kind: UncertaintyKind
    severity: Severity
    detail: str
    sourceIds: tuple[str, ...]


def effectiveDate(rule: GroundedRule, sourceDates: dict[str, Optional[datetime]]) -> Optional[datetime]:
    """The rule's own stated issue date, else its source's date (e.g. email sent time), else None."""
    return rule.issuedAt or sourceDates.get(rule.sourceId)


def reconcileDeadlines(
    rules: tuple[GroundedRule, ...], sourceDates: dict[str, Optional[datetime]],
    formatDate: Callable[[datetime], str] = lambda d: d.isoformat(),
) -> tuple[Optional[datetime], list[FlagSpec]]:
    flags: list[FlagSpec] = []
    deadlineRules = [r for r in rules if r.category is RuleCategory.DEADLINE]
    for rule in deadlineRules:
        if rule.dateAmbiguous:
            flags.append(FlagSpec(UncertaintyKind.UNDATED_RULE, Severity.MEDIUM,
                                  f"Deadline without explicit timezone or unparseable date: \"{rule.verbatimQuote[:200]}\"", (rule.sourceId,)))
    for rule in rules:
        if rule.supersedesEarlierRule:
            flags.append(FlagSpec(UncertaintyKind.CONFLICTING_SOURCES, Severity.MEDIUM,
                                  f"Source states a correction or retraction; confirm which rule it replaces: \"{rule.verbatimQuote[:200]}\"",
                                  (rule.sourceId,)))

    dated = [r for r in deadlineRules if r.deadlineAt is not None]
    distinct = {r.deadlineAt for r in dated}
    if not distinct:
        flags.append(FlagSpec(UncertaintyKind.MISSING_SOURCE_CATEGORY, Severity.MEDIUM,
                              "No deadline with an explicit date and timezone was found in any source.", ()))
        return None, flags
    if len(distinct) == 1:
        return dated[0].deadlineAt, flags

    sourceIds = tuple(dict.fromkeys(r.sourceId for r in dated))
    dates = [(effectiveDate(r, sourceDates), r) for r in dated]
    if any(d is None for d, _ in dates):
        flags.append(FlagSpec(UncertaintyKind.CONFLICTING_SOURCES, Severity.HIGH,
                              f"{len(distinct)} different deadlines found and at least one source is undated; cannot resolve by recency.",
                              sourceIds))
        return None, flags
    newestDate = max(d for d, _ in dates)
    newest = {r.deadlineAt for d, r in dates if d == newestDate}
    if len(newest) > 1:
        flags.append(FlagSpec(UncertaintyKind.CONFLICTING_SOURCES, Severity.HIGH,
                              "Different deadlines were issued at the same time; cannot resolve by recency.", sourceIds))
        return None, flags
    chosen = newest.pop()
    others = ", ".join(sorted(formatDate(d) for d in distinct if d != chosen))
    flags.append(FlagSpec(UncertaintyKind.CONFLICTING_SOURCES, Severity.MEDIUM,
                          f"Deadline resolved by recency to {formatDate(chosen)}; superseded: {others}. Confirm the newer source is authoritative.",
                          sourceIds))
    return chosen, flags
