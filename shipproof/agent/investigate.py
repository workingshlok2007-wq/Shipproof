"""Agent-chosen investigation: the model proposes the next read-only steps from what the audit has found.

Guardrails enforced in code, not in the prompt:
- Only three actions exist, all read-only: READ_PAGE, CHECK_LINK, SEARCH_INBOX (plus STOP).
- Page and link targets must be URLs the agent actually saw in fetched sources or the README (grounded); the model
  cannot invent a destination, and nothing already visited is visited again.
- Steps per round and rounds per audit are capped. The baseline checks always run; the agent can only add.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Annotated

from pydantic import Field, StringConstraints

from shipproof.llm import LlmClient, generateStructured, newNonce, untrustedPreamble, wrapUntrusted
from shipproof.prompts import InjectionList, LlmOutput

MAX_STEPS_PER_ROUND = 3
MAX_ROUNDS = 2
MAX_CANDIDATES = 60
MAX_INBOX_QUERY_CHARS = 120

_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')\]]{1,2000}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class InvestigationAction(str, Enum):
    READ_PAGE = "READ_PAGE"
    CHECK_LINK = "CHECK_LINK"
    SEARCH_INBOX = "SEARCH_INBOX"
    STOP = "STOP"


class RawStep(LlmOutput):
    action: InvestigationAction
    target: Annotated[str, StringConstraints(max_length=2000)] = Field(
        description="For READ_PAGE and CHECK_LINK: one URL copied exactly from the candidate list. For SEARCH_INBOX: a short Gmail query.")
    reason: Annotated[str, StringConstraints(max_length=500)] = Field(description="One sentence: which uncertainty this step reduces.")


class RawInvestigation(LlmOutput):
    steps: Annotated[list[RawStep], Field(max_length=MAX_STEPS_PER_ROUND)]
    suspectedInjections: InjectionList = []


@dataclass(frozen=True)
class PlannedStep:
    action: InvestigationAction
    target: str
    reason: str


INVESTIGATION_TASK = (
    "Task: you are auditing whether a hackathon submission is compliant and accessible to judges. Given the findings so far "
    "and the candidate URLs discovered in already-read sources, choose at most three next steps that most reduce uncertainty: "
    "READ_PAGE to read a linked page that may contain rules, deadlines or updates; CHECK_LINK to verify that a submission "
    "artifact (video, slides, demo) is viewable logged-out; SEARCH_INBOX to look for organiser emails about an unresolved "
    "question. Copy URLs exactly from the candidate list. Prefer pages whose link text or URL suggests rules, updates, FAQ, "
    "deadlines or submission requirements. Return STOP alone if nothing would reduce uncertainty."
)


def extractUrls(text: str) -> list[str]:
    return [url.rstrip(".,;:") for url in _URL_IN_TEXT.findall(text)]


def planInvestigation(llm: LlmClient, findings: str, candidates: list[str], inboxAvailable: bool,
                      visited: set[str]) -> tuple[list[PlannedStep], int, tuple[str, ...]]:
    """Returns (validated steps, rejected count, suspected injections)."""
    candidates = [c for c in dict.fromkeys(candidates) if c not in visited][:MAX_CANDIDATES]
    if not candidates and not inboxAvailable:
        return [], 0, ()
    nonce = newNonce()
    system = f"{untrustedPreamble(nonce)}\n\n{INVESTIGATION_TASK}" + ("" if inboxAvailable else " SEARCH_INBOX is unavailable.")
    user = "\n\n".join((wrapUntrusted("findings", findings or "No findings yet.", nonce),
                        wrapUntrusted("candidateUrls", "\n".join(candidates) or "(none)", nonce)))
    raw = generateStructured(llm, system, user, RawInvestigation)
    allowed = set(candidates)
    steps: list[PlannedStep] = []
    rejected = 0
    for step in raw.steps:
        reason = _CONTROL.sub(" ", step.reason).strip()
        target = step.target.strip()
        if step.action is InvestigationAction.STOP:
            break
        if not reason:
            rejected += 1
        elif step.action in (InvestigationAction.READ_PAGE, InvestigationAction.CHECK_LINK):
            if target in allowed and target not in visited:
                steps.append(PlannedStep(step.action, target, reason))
                visited.add(target)
            else:
                rejected += 1  # invented or repeated destination: never followed
        elif step.action is InvestigationAction.SEARCH_INBOX:
            if inboxAvailable and 0 < len(target) <= MAX_INBOX_QUERY_CHARS and not _CONTROL.search(target) and f"inbox:{target}" not in visited:
                steps.append(PlannedStep(step.action, target, reason))
                visited.add(f"inbox:{target}")
            else:
                rejected += 1
    return steps[:MAX_STEPS_PER_ROUND], rejected, tuple(raw.suspectedInjections)
