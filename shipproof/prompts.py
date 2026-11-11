"""Phase 2: Gemini prompt tasks.

Design rules enforced here, not in the prompt:
- Every quote the model returns must appear verbatim in the input it came from; ungrounded items are dropped and counted.
- Scores are computed deterministically from grounded evidence. The model never produces a number or a confidence.
- Tier 2 output is "possible misreading" or "no misreading detected", never "verified".
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from shipproof.llm import LlmClient, generateStructured, newNonce, untrustedPreamble, wrapUntrusted
from shipproof.models import (
    MAX_COLLECTION,
    AlignmentReport,
    CheckStatus,
    EntityId,
    EvidenceBasis,
    FrozenModel,
    LongText,
    ShortText,
    utcNow,
)

MIN_QUOTE_CHARS = 8
MAX_FEATURES = 50
MAX_RULES = 200
MAX_COMMITS = 500
MAX_INDEX_ENTRIES = 5000
MAX_SYMBOLS_PER_FILE = 500

_WHITESPACE = re.compile(r"\s+")


def normaliseForMatch(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().casefold()


def isGrounded(quote: Optional[str], source: str) -> bool:
    if quote is None:
        return False
    needle = normaliseForMatch(quote)
    return len(needle) >= MIN_QUOTE_CHARS and needle in normaliseForMatch(source)


def parseAwareDate(raw: Optional[str]) -> tuple[Optional[datetime], bool]:
    """Returns (parsed, ambiguous). A date without a timezone, or unparseable, is ambiguous and never guessed."""
    if raw is None or not raw.strip():
        return None, False
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None, True
    if parsed.tzinfo is None:
        return None, True
    return parsed, False


class LlmOutput(BaseModel):
    """Lenient container for raw model output; strict validation happens in post-processing."""

    model_config = ConfigDict(extra="ignore")


InjectionList = Annotated[list[Annotated[str, StringConstraints(max_length=500)]], Field(max_length=20)]


# ---------------------------------------------------------------- task 1: rule extraction


class RuleCategory(str, Enum):
    DEADLINE = "DEADLINE"
    REQUIRED_ARTIFACT = "REQUIRED_ARTIFACT"
    FORMAT_CONSTRAINT = "FORMAT_CONSTRAINT"
    ELIGIBILITY = "ELIGIBILITY"
    JUDGING = "JUDGING"
    OTHER = "OTHER"


class RawRule(LlmOutput):
    ruleText: str = Field(description="The rule restated in one plain sentence.")
    category: RuleCategory
    verbatimQuote: str = Field(description="Exact text copied from the source that states this rule.")
    issuedAt: Optional[str] = Field(None, description="ISO 8601 date-time with timezone at which the source says this rule was issued or last updated, only if explicitly stated; else null.")
    deadlineAt: Optional[str] = Field(None, description="ISO 8601 date-time with timezone for the deadline, only if the source states one; else null. Never infer a timezone.")
    supersedesEarlierRule: bool = Field(description="True only if the source explicitly says it changes, corrects or retracts an earlier rule.")


class RawRuleExtraction(LlmOutput):
    rules: Annotated[list[RawRule], Field(max_length=MAX_RULES)]
    suspectedInjections: InjectionList = []


class GroundedRule(FrozenModel):
    sourceId: EntityId
    ruleText: LongText
    category: RuleCategory
    verbatimQuote: LongText
    issuedAt: Optional[datetime] = None
    deadlineAt: Optional[datetime] = None
    dateAmbiguous: bool
    supersedesEarlierRule: bool


class RuleExtractionResult(FrozenModel):
    sourceId: EntityId
    rules: tuple[GroundedRule, ...]
    droppedUngroundedCount: Annotated[int, Field(ge=0)]
    suspectedInjections: tuple[str, ...]


RULE_EXTRACTION_TASK = (
    "Task: extract every submission rule, requirement, constraint and deadline stated in the source document. "
    "Copy verbatimQuote exactly from the source; do not paraphrase it. "
    "Only fill issuedAt and deadlineAt when the source states them explicitly, including a timezone; otherwise null. "
    "Do not invent rules, merge sources, or resolve conflicts. If the document states no rules, return an empty list."
)


def extractRules(client: LlmClient, sourceId: str, sourceText: str) -> RuleExtractionResult:
    nonce = newNonce()
    systemInstruction = f"{untrustedPreamble(nonce)}\n\n{RULE_EXTRACTION_TASK}"
    userContent = wrapUntrusted("sourceDocument", sourceText, nonce)
    raw = generateStructured(client, systemInstruction, userContent, RawRuleExtraction)

    grounded: list[GroundedRule] = []
    dropped = 0
    for rule in raw.rules:
        if not isGrounded(rule.verbatimQuote, sourceText) or not rule.ruleText.strip():
            dropped += 1
            continue
        issuedAt, issuedAmbiguous = parseAwareDate(rule.issuedAt)
        deadlineAt, deadlineAmbiguous = parseAwareDate(rule.deadlineAt)
        try:
            groundedRule = GroundedRule(
                sourceId=sourceId,
                ruleText=rule.ruleText,
                category=rule.category,
                verbatimQuote=rule.verbatimQuote,
                issuedAt=issuedAt,
                deadlineAt=deadlineAt,
                dateAmbiguous=issuedAmbiguous or deadlineAmbiguous,
                supersedesEarlierRule=rule.supersedesEarlierRule,
            )
        except ValidationError:
            dropped += 1
            continue
        grounded.append(groundedRule)
    return RuleExtractionResult(
        sourceId=sourceId, rules=tuple(grounded), droppedUngroundedCount=dropped,
        suspectedInjections=tuple(raw.suspectedInjections),
    )


# ---------------------------------------------------------------- task 2: proof interpretation check


class InterpretationVerdict(str, Enum):
    NO_MISREADING_FOUND = "NO_MISREADING_FOUND"
    POSSIBLE_MISREADING = "POSSIBLE_MISREADING"


class RawConflict(LlmOutput):
    clauseQuote: str = Field(description="Exact text from the clause that the interpretation contradicts, omits or overstates.")
    interpretationQuote: str = Field(description="Exact text from the interpretation that conflicts.")
    explanation: str = Field(description="One sentence on why these differ.")


class RawInterpretationCheck(LlmOutput):
    verdict: InterpretationVerdict
    conflicts: Annotated[list[RawConflict], Field(max_length=20)]
    suspectedInjections: InjectionList = []


class GroundedConflict(FrozenModel):
    clauseQuote: LongText
    interpretationQuote: LongText
    explanation: LongText


class InterpretationResult(FrozenModel):
    status: CheckStatus
    label: ShortText
    conflicts: tuple[GroundedConflict, ...]
    droppedUngroundedCount: Annotated[int, Field(ge=0)]
    suspectedInjections: tuple[str, ...]


LABEL_NO_MISREADING = "No misreading detected (text comparison only, not verified)"
LABEL_POSSIBLE_MISREADING = "Possible misreading detected"
LABEL_INCONCLUSIVE = "Inconclusive: model output could not be grounded"

INTERPRETATION_TASK = (
    "Task: compare a developer's interpretation against the exact requirement clause. "
    "Report POSSIBLE_MISREADING if the interpretation contradicts the clause, drops a condition, adds a permission "
    "the clause does not grant, or changes a quantity, format, date or audience. List each conflict with exact quotes "
    "from both texts. Otherwise report NO_MISREADING_FOUND with an empty conflicts list. Be strict: when unsure, "
    "report POSSIBLE_MISREADING."
)


def checkInterpretation(client: LlmClient, clauseText: str, interpretation: str) -> InterpretationResult:
    nonce = newNonce()
    systemInstruction = f"{untrustedPreamble(nonce)}\n\n{INTERPRETATION_TASK}"
    userContent = f"{wrapUntrusted('clause', clauseText, nonce)}\n\n{wrapUntrusted('interpretation', interpretation, nonce)}"
    raw = generateStructured(client, systemInstruction, userContent, RawInterpretationCheck)

    conflicts: list[GroundedConflict] = []
    dropped = 0
    for conflict in raw.conflicts:
        if not (isGrounded(conflict.clauseQuote, clauseText) and isGrounded(conflict.interpretationQuote, interpretation)):
            dropped += 1
            continue
        try:
            conflicts.append(GroundedConflict(**conflict.model_dump()))
        except ValidationError:
            dropped += 1

    if raw.verdict is InterpretationVerdict.POSSIBLE_MISREADING and conflicts:
        status, label = CheckStatus.FAIL, LABEL_POSSIBLE_MISREADING
    elif raw.verdict is InterpretationVerdict.NO_MISREADING_FOUND and not raw.conflicts:
        status, label = CheckStatus.PASS, LABEL_NO_MISREADING
    else:
        status, label = CheckStatus.UNVERIFIED, LABEL_INCONCLUSIVE
    return InterpretationResult(
        status=status, label=label, conflicts=tuple(conflicts), droppedUngroundedCount=dropped,
        suspectedInjections=tuple(raw.suspectedInjections),
    )


# ---------------------------------------------------------------- task 3: alignment


class CodeIndexEntry(FrozenModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=512, pattern=r"^[^\x00-\x1f\\]+$")]
    symbols: Annotated[tuple[Annotated[str, StringConstraints(min_length=1, max_length=256)], ...], Field(max_length=MAX_SYMBOLS_PER_FILE)] = ()


class CodeIndex(FrozenModel):
    """File paths (forward slashes, repo-relative) and declared symbol names. Built by a Phase 3 tool."""

    entries: Annotated[tuple[CodeIndexEntry, ...], Field(max_length=MAX_INDEX_ENTRIES)]

    def render(self) -> str:
        return "\n".join(f"{e.path}: {', '.join(e.symbols)}" if e.symbols else e.path for e in self.entries)

    def hasPath(self, path: str) -> bool:
        return any(e.path == path for e in self.entries)

    def hasSymbol(self, symbol: str) -> bool:
        return any(symbol in e.symbols for e in self.entries)


class RawFeature(LlmOutput):
    feature: str = Field(description="A core feature promised in the proposal, in a short phrase.")
    proposalQuote: str = Field(description="Exact text from the proposal that promises this feature.")
    readmeQuote: Optional[str] = Field(None, description="Exact text from the README claiming this feature, or null.")
    codeEvidencePaths: Annotated[list[str], Field(max_length=10)] = Field(default_factory=list, description="File paths from the code index that implement this feature. Only paths listed in the index.")
    codeEvidenceSymbols: Annotated[list[str], Field(max_length=10)] = Field(default_factory=list, description="Symbol names from the code index that implement this feature. Only names listed in the index.")


class RawAlignment(LlmOutput):
    features: Annotated[list[RawFeature], Field(max_length=MAX_FEATURES)]
    suspectedInjections: InjectionList = []


class FeatureAssessment(FrozenModel):
    feature: ShortText
    proposalQuote: LongText
    readmeQuote: Optional[LongText] = None
    codeEvidencePaths: tuple[str, ...]
    codeEvidenceSymbols: tuple[str, ...]

    @property
    def hasCodeEvidence(self) -> bool:
        return bool(self.codeEvidencePaths or self.codeEvidenceSymbols)


class AlignmentResult(FrozenModel):
    report: Optional[AlignmentReport]
    features: tuple[FeatureAssessment, ...]
    documentedOnlyFeatures: tuple[str, ...]
    droppedUngroundedCount: Annotated[int, Field(ge=0)]
    discardedEvidenceCount: Annotated[int, Field(ge=0)]
    suspectedInjections: tuple[str, ...]


ALIGNMENT_CAVEAT = (
    "Score = share of promised features with matching file or symbol names in the code index. "
    "Name matches show code exists, not that the feature works."
)

ALIGNMENT_TASK = (
    "Task: list every core feature the proposal promises. For each, copy the promising text exactly from the proposal, "
    "copy the README text claiming it if any, and cite file paths and symbol names from the code index that implement it. "
    "Cite only entries that appear in the code index. If no code implements a feature, leave both evidence lists empty. "
    "Judge implementation by code, not by how well the README describes it. Commit messages are context only."
)


def assessAlignment(
    client: LlmClient, proposal: str, readme: str, commitMessages: tuple[str, ...], codeIndex: CodeIndex
) -> AlignmentResult:
    if len(commitMessages) > MAX_COMMITS:
        raise ValueError(f"at most {MAX_COMMITS} commit messages")
    nonce = newNonce()
    systemInstruction = f"{untrustedPreamble(nonce)}\n\n{ALIGNMENT_TASK}"
    userContent = "\n\n".join((
        wrapUntrusted("proposal", proposal, nonce),
        wrapUntrusted("readme", readme, nonce),
        wrapUntrusted("commitMessages", "\n".join(commitMessages), nonce),
        wrapUntrusted("codeIndex", codeIndex.render(), nonce),
    ))
    raw = generateStructured(client, systemInstruction, userContent, RawAlignment)

    features: list[FeatureAssessment] = []
    dropped = 0
    discarded = 0
    seen: set[str] = set()
    for item in raw.features:
        key = normaliseForMatch(item.feature)
        if not key or key in seen or not isGrounded(item.proposalQuote, proposal):
            dropped += 1
            continue
        paths = tuple(dict.fromkeys(p for p in item.codeEvidencePaths if codeIndex.hasPath(p)))
        symbols = tuple(dict.fromkeys(s for s in item.codeEvidenceSymbols if codeIndex.hasSymbol(s)))
        try:
            assessment = FeatureAssessment(
                feature=item.feature,
                proposalQuote=item.proposalQuote,
                readmeQuote=item.readmeQuote if isGrounded(item.readmeQuote, readme) else None,
                codeEvidencePaths=paths,
                codeEvidenceSymbols=symbols,
            )
        except ValidationError:
            dropped += 1
            continue
        seen.add(key)
        discarded += len(item.codeEvidencePaths) - len(paths) + len(item.codeEvidenceSymbols) - len(symbols)
        features.append(assessment)

    documentedOnly = tuple(f.feature for f in features if not f.hasCodeEvidence and f.readmeQuote is not None)
    report: Optional[AlignmentReport] = None
    if features:
        implemented = sum(1 for f in features if f.hasCodeEvidence)
        basis = {EvidenceBasis.DOCUMENTATION, EvidenceBasis.CODE_EXISTENCE}
        if commitMessages:
            basis.add(EvidenceBasis.COMMIT_HISTORY)
        report = AlignmentReport(
            alignmentScore=round(100 * implemented / len(features)),
            missingFeatures=tuple(f.feature for f in features if not f.hasCodeEvidence)[:MAX_COLLECTION],
            evidenceBasis=frozenset(basis),
            caveat=ALIGNMENT_CAVEAT,
            generatedAt=utcNow(),
        )
    return AlignmentResult(
        report=report, features=tuple(features), documentedOnlyFeatures=documentedOnly,
        droppedUngroundedCount=dropped, discardedEvidenceCount=discarded,
        suspectedInjections=tuple(raw.suspectedInjections),
    )
