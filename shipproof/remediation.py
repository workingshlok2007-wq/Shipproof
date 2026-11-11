"""Remediation: the agent drafts fixes for minor problems from verified audit facts.

Boundary: drafting is autonomous; applying is not. A fix becomes a pull request on a new branch only after a human
clicks Apply, and only README.md and LICENSE may ever be written. Nothing is merged by the agent.
README drafts contain only facts verified in the repository (files, routes, manifests) and features whose code
evidence was confirmed by the grounded alignment step. Unverified promises are listed as "not yet implemented".
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import PurePosixPath
from typing import Annotated, Optional

from pydantic import Field, model_validator

from shipproof.models import EntityId, EvidenceItem, FrozenModel, CheckStatus, LongText, ShortText
from shipproof.prompts import CodeIndex, FeatureAssessment

WRITABLE_PATHS = frozenset({"README.md", "LICENSE"})
MAX_README_LINKS = 15
MAX_ADVICE_FEATURES = 20
LICENSE_KEYS = ("mit", "apache-2.0", "bsd-3-clause", "bsd-2-clause", "mpl-2.0", "gpl-3.0", "agpl-3.0", "lgpl-3.0", "unlicense")

_MARKDOWN_LINK = re.compile(r"(?<!!)\[([^\]\n]{1,200})\]\((https?://[^)\s]{1,2000})\)")
_BARE_LINK = re.compile(r"(?<![(\[<])\bhttps?://[^\s)<>\]]{1,2000}")


class RemediationKind(str, Enum):
    ADD_LICENSE = "ADD_LICENSE"
    IMPROVE_README = "IMPROVE_README"
    FIX_README_LINKS = "FIX_README_LINKS"
    MISSING_FEATURE_GUIDANCE = "MISSING_FEATURE_GUIDANCE"


class RemediationStatus(str, Enum):
    PROPOSED = "PROPOSED"
    APPLIED = "APPLIED"
    DISMISSED = "DISMISSED"


class FileChange(FrozenModel):
    path: Annotated[str, Field(min_length=1, max_length=64)]
    newContent: Annotated[str, Field(max_length=200_000)]

    @model_validator(mode="after")
    def _pathAllowed(self) -> FileChange:
        if self.path not in WRITABLE_PATHS:
            raise ValueError(f"remediation may only write {sorted(WRITABLE_PATHS)}, not {self.path!r}")
        return self


class Remediation(FrozenModel):
    remediationId: EntityId
    kind: RemediationKind
    title: ShortText
    rationale: LongText
    changes: tuple[FileChange, ...] = ()
    advice: tuple[LongText, ...] = ()
    requiredInputs: tuple[str, ...] = ()
    applicable: bool
    status: RemediationStatus = RemediationStatus.PROPOSED
    pullRequestUrl: Optional[str] = None
    draftedFrom: ShortText


# ---------------------------------------------------------------- license


def licenseRemediation() -> Remediation:
    return Remediation(
        remediationId="addLicense", kind=RemediationKind.ADD_LICENSE, title="Add an open-source LICENSE file",
        rationale="No recognised license was detected. Many hackathons and clients require one, and without it the code is not legally reusable. "
                  "Choosing a license is a legal decision, so you pick the type and the copyright holder.",
        requiredInputs=("licenseKey", "copyrightHolder"), applicable=True,
        draftedFrom="GitHub license template text, filled at apply time",
    )


def fillLicenseTemplate(body: str, year: int, copyrightHolder: str) -> str:
    holder = copyrightHolder.strip()
    if not 1 <= len(holder) <= 100 or any(ord(ch) < 0x20 for ch in holder):
        raise ValueError("copyright holder must be 1-100 printable characters")
    if not 1970 <= year <= 2100:
        raise ValueError("year out of range")
    filled = body
    for placeholder in ("[year]", "[yyyy]"):
        filled = filled.replace(placeholder, str(year))
    for placeholder in ("[fullname]", "[name of copyright owner]"):
        filled = filled.replace(placeholder, holder)
    return filled


# ---------------------------------------------------------------- README draft


def _detectStack(index: CodeIndex) -> tuple[list[str], list[str]]:
    """Returns (stack labels, setup commands) from manifests that actually exist in the repository."""
    paths = {entry.path for entry in index.entries}
    stack: list[str] = []
    setup: list[str] = []
    if "requirements.txt" in paths:
        stack.append("Python")
        setup.append("pip install -r requirements.txt")
    elif "pyproject.toml" in paths:
        stack.append("Python")
        setup.append("pip install .")
    if "package.json" in paths:
        stack.append("Node.js")
        setup.append("npm install")
    for manifest, label, command in (("go.mod", "Go", "go build ./..."), ("Cargo.toml", "Rust", "cargo build"),
                                     ("pom.xml", "Java (Maven)", "mvn package"), ("build.gradle", "Java (Gradle)", "./gradlew build"),
                                     ("Dockerfile", "Docker", "docker build -t app .")):
        if manifest in paths:
            stack.append(label)
            setup.append(command)
    return stack, setup


def _routes(index: CodeIndex) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for entry in index.entries:
        for symbol in entry.symbols:
            if re.match(r"^(GET|POST|PUT|PATCH|DELETE|WEBSOCKET|ROUTE) /", symbol):
                found.append((symbol, entry.path))
    return found[:40]


def draftReadme(repoName: str, index: CodeIndex, features: tuple[FeatureAssessment, ...]) -> str:
    stack, setup = _detectStack(index)
    topLevel = sorted({PurePosixPath(e.path).parts[0] for e in index.entries if len(PurePosixPath(e.path).parts) > 1})[:15]
    implemented = [f for f in features if f.hasCodeEvidence]
    missing = [f for f in features if not f.hasCodeEvidence]
    lines = [f"# {repoName}", "", "<!-- Drafted by ShipProof from verified repository facts. Review and edit before merging. -->", ""]
    if implemented:
        lines += ["## Features", ""]
        for feature in implemented:
            evidence = ", ".join(f"`{p}`" for p in (*feature.codeEvidencePaths, *feature.codeEvidenceSymbols)[:4])
            lines.append(f"- {feature.feature} ({evidence})")
        lines.append("")
    if stack:
        lines += ["## Tech stack", "", ", ".join(stack), ""]
    if setup:
        lines += ["## Setup", "", "```bash", *setup, "```", ""]
    routes = _routes(index)
    if routes:
        lines += ["## API endpoints", "", "| Endpoint | Defined in |", "|---|---|"]
        lines += [f"| `{route}` | `{path}` |" for route, path in routes]
        lines.append("")
    if topLevel:
        lines += ["## Project structure", "", *[f"- `{folder}/`" for folder in topLevel], ""]
    if missing:
        lines += ["## Not yet implemented", "", "Promised in the proposal, no matching code found:", ""]
        lines += [f"- {feature.feature}" for feature in missing]
        lines.append("")
    lines += ["## License", "", "See `LICENSE`.", ""]
    return "\n".join(lines)


def readmeRemediation(repoName: str, index: CodeIndex, features: tuple[FeatureAssessment, ...], alignmentAvailable: bool) -> Remediation:
    return Remediation(
        remediationId="improveReadme", kind=RemediationKind.IMPROVE_README, title="Replace the thin README with a verified draft",
        rationale="The README is missing or under 200 characters. Judges read it first. This draft states only what the audit verified "
                  "in the code; promises without code evidence are listed as not yet implemented instead of being claimed."
                  + ("" if alignmentAvailable else " The language model was unavailable, so the Features section is omitted."),
        changes=(FileChange(path="README.md", newContent=draftReadme(repoName, index, features)),), applicable=True,
        draftedFrom="templates + grounded alignment features" if alignmentAvailable else "templates only",
    )


# ---------------------------------------------------------------- README links


def extractReadmeLinks(readme: str) -> list[str]:
    links = [match.group(2) for match in _MARKDOWN_LINK.finditer(readme)]
    links += [match.group(0).rstrip(".,;:") for match in _BARE_LINK.finditer(readme)]
    return list(dict.fromkeys(links))[:MAX_README_LINKS]


def removeLinks(readme: str, brokenUrls: set[str]) -> str:
    """Keeps the link text, drops the broken target, so no content silently disappears."""
    def replaceMarkdown(match: re.Match[str]) -> str:
        return f"{match.group(1)} (link removed: unreachable)" if match.group(2) in brokenUrls else match.group(0)

    def replaceBare(match: re.Match[str]) -> str:
        url = match.group(0).rstrip(".,;:")
        return match.group(0).replace(url, f"{url} (unreachable)") if url in brokenUrls else match.group(0)

    return _BARE_LINK.sub(replaceBare, _MARKDOWN_LINK.sub(replaceMarkdown, readme))


def readmeLinksRemediation(readme: str, linkEvidence: list[EvidenceItem]) -> Optional[Remediation]:
    broken = [item for item in linkEvidence if item.status is not CheckStatus.PASS]
    if not broken:
        return None
    brokenUrls = {item.url for item in broken}
    return Remediation(
        remediationId="fixReadmeLinks", kind=RemediationKind.FIX_README_LINKS,
        title=f"Mark {len(broken)} unreachable README link(s)",
        rationale="These links failed for a logged-out visitor: " + "; ".join(f"{item.url} ({item.detail})" for item in broken)[:1500]
                  + ". Replace them with public links if possible; this draft marks them so judges are not sent to dead ends.",
        changes=(FileChange(path="README.md", newContent=removeLinks(readme, brokenUrls)),), applicable=True,
        draftedFrom="headless link audit",
    )


# ---------------------------------------------------------------- missing features


def featureGuidance(features: tuple[FeatureAssessment, ...], index: CodeIndex) -> Optional[Remediation]:
    missing = [f for f in features if not f.hasCodeEvidence][:MAX_ADVICE_FEATURES]
    if not missing:
        return None
    folders = sorted({PurePosixPath(e.path).parts[0] for e in index.entries if len(PurePosixPath(e.path).parts) > 1})[:8]
    where = f" Existing source folders: {', '.join(folders)}." if folders else ""
    advice = tuple(
        f"\"{f.feature}\": promised as \"{f.proposalQuote[:300]}\". No matching file or symbol was found. "
        f"Either implement it before submission, or state it as out of scope in the README so judges are not misled.{where}"
        for f in missing
    )
    return Remediation(
        remediationId="missingFeatures", kind=RemediationKind.MISSING_FEATURE_GUIDANCE,
        title=f"{len(missing)} promised feature(s) have no code evidence",
        rationale="Scope drift: these were promised in the proposal. Code changes need human judgement, so this is advice only.",
        advice=advice, applicable=False, draftedFrom="grounded alignment features",
    )
