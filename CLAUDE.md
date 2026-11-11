PROJECT CONTEXT: ShipProof (full handoff, nothing omitted)

1. EVENT AND CONSTRAINTS
- 24-hour AI hackathon (AgentX 2026, Open Track).
- Must be an API-first agent orchestration engine, decoupled from a CRUD app. The frontend is only a visualization and approval dashboard.
- Builder: second-year IT engineering student focused on AI/ML.

2. PRODUCT DEFINITION
ShipProof: The Agentic Submission Reliability System. An autonomous agent that closes the gap between code completion and successful submission. It acts as a compliance auditor and product-alignment engine so deliverables are accessible, compliant with dynamic rules, and aligned with the original pitch.
Loop: Ingest -> Plan -> Audit -> Act. Least-privilege trust model with a strict human-in-the-loop approval boundary.

3. PROBLEM SPACE
- Administrative disqualification: private demo videos, inaccessible GitHub repos, missed last-minute rule updates sent by email or hidden on event pages.
- Scope drift ("hackathon hallucination"): teams pivot or chase new tech and ship something that works but misses the core problem in the original proposal or track requirements.

4. ORIGINAL CAPABILITIES
a) Dynamic rule ingestion and reconciliation: scrape event pages, monitor inboxes (Gmail API), extract rules/constraints/deadlines, resolve conflicts between sources (newer email deadline over older website deadline) into a structured Task Graph.
b) Live artifact and evidence auditing: GitHub API (repo visibility, populated README, open-source license); HTTP checks on demo URLs, slide decks, video links for 200 OK and absence of auth walls or 404s.
c) Semantic scope verification: LLM cross-references the initial proposal against final README.md and git commit history; outputs a structured JSON Alignment Score (0-100) and lists promised core features that are missing.
d) Approval boundary: prepares the submission packet, flags risks (e.g., "Demo Video is Private"), halts all irreversible actions, updates a visual Evidence Ledger, requires explicit human authorization before finalizing the submission or sending external communications. State machine holds PENDING HUMAN APPROVAL until an authenticated POST from the dashboard.

5. STRESS-TEST FINDINGS (critique so far)
Critical vulnerability: ground truth is unverifiable and the agent's confidence outruns its accuracy.
- Rules live in places the agent cannot reliably see or rank: Discord, mentor comments, login-gated Devpost fields, PDFs on Notion, emails to other addresses.
- A 200 OK from the agent's HTTP client does not prove a logged-out judge on another network can view the content.
- Recency-based conflict resolution fails silently on ambiguous or retracted emails.
- A false green "all clear" is worse than no tool: it removes manual double-checking. In a disqualification context a false negative costs more than the tool saves.
Three challenge questions posed:
1) Coverage honesty: what does the dashboard show when completeness of rule sources can't be confirmed; who owns "this is everything".
2) Judge's vantage point: unauthenticated headless-browser checks and re-audit through judging, or out of scope.
3) Scope-drift ground truth: an LLM comparing text to text may reward the best-written README, not the most faithful product; add behavioral evidence or accept documentation-only alignment.

6. USER DECISIONS AND IDEAS (in order given)
- First idea (rejected as undefined and off-mission): per-developer agents paired with each person so production and development branches don't form loops or error cycles. Critique: it is branch hygiene, not submission auditing; it is scope drift inside a scope-drift product; it enlarges the unreviewed-change surface.
- Accepted partially: a "confusion index"; when it exceeds a threshold the agent asks the human. Critique: self-reported LLM confidence is uncalibrated; the index only catches known unknowns, not unseen sources; threshold tuning risks alert fatigue and blind approval. Fix adopted: compute the index from structural signals, and keep a CoverageLedger of sources checked / unreachable / not checked that a human must confirm complete.
- Role concept: users segregated by access level, each with their own ShipProof scoped to their repo access, plus work layers so ShipProof doesn't hallucinate or interfere.
- Example scenario: Company One (client) gives Company Two (vendor software company) a deliverables list specifying what, how, and in what format. Company Two roles: A = senior manager with full repo control; B = mid-level developer; C = junior developer; D = intern. Everyone has their own ShipProof, like everyone has their own Claude Code. A's ShipProof checks changes against deliverables and flags production issues. B's and C's check only within their access. C can set benchmarks/interpretations for ShipProof to test his understanding of the deliverables.
- Layering idea: layer 1 = the client's exact deliverables, unchangeable; layer 2 = brainstorming plus trial checks on the developer's understanding, and passing trial checks can be moved into layer 1; layer 3 (and beyond) = free-form, customizable, "scrap" space. Number of layers is not fixed.
- Promotion rule (user's answer): only a person with higher access may promote a check.
- Layer 1 change handling: user delegated the solution to the assistant.
- Hierarchy: the person's position in the project hierarchy is established per project, and each person has their own tier set. User asked for a better name than "layers".

7. ADOPTED DESIGN: TRUST TIERS
8. Charter: client/organizer requirements and original pitch. Immutable, versioned, append-only. Changes enter only as dated amendments with a mandatory source reference (URL or email id), submitted only by the top role. Never edited in place.
9. Proof: the user's interpretation of a Charter clause, checked against it. Each check stores clauseId and clauseVersion. Promotable only by higher access. The promotion screen shows the interpretation beside the exact Charter clause to prevent rubber-stamping.
10. Workbench (tier 3 and beyond): user-defined, disposable, non-authoritative, never gates approval. Tier count configurable.
Rules:
- An amendment touching a clause sets every dependent Proof check to STALE. STALE is never PASS; it requires re-confirmation. Unrelated checks stay untouched.
- Roles (owner, senior, mid, junior, intern) are assigned by the owner or client, never self-declared.
- Tier 2 is LLM text-vs-text, so report "possible misreading detected", never "verified".
- Known risks: promotion can launder a junior's misreading into ground truth; per-person agents with different access can produce conflicting verdicts on the same deliverable (reconciliation authority undefined).

8. SCOPE DECISION FOR THE 24-HOUR BUILD
Assistant recommendation: single user, the three tiers, hackathon audit; roles as a roadmap slide only (enterprise multi-user vision shown as roadmap, not built). User has not yet explicitly confirmed. Items answered by assumed defaults, not by the user: headless-browser link checks (yes; post-submission re-audit is roadmap), behavioral evidence limited to checking that claimed endpoints/features exist in code, single-user MVP cut. The four assumptions to confirm: target is a coding agent; MVP is single-user with roles modeled but not enforced; link checks use unauthenticated headless browser; alignment score includes code-existence checks.

9. UNCERTAINTY DESIGN
- Confusion index is computed from structural signals: conflicting sources, undated rule, failed render, auth redirect, unreachable source, missing source category. Never from LLM self-reported confidence.
- Above a configurable threshold the agent halts and asks the human.
- CoverageLedger lists sources checked, unreachable, not checked; the human explicitly confirms "this is everything"; the agent never asserts completeness.
- Recency resolves conflicts only when both items are dated; otherwise raise an UncertaintyFlag.

10. STACK
Python; LangChain (LangGraph if state needs it); Gemini API; FastAPI with WebSockets; GitHub and Gmail integrations; Playwright for link checks. Dashboard: Next.js + Tailwind, claymorphic/minimal, background #FAF6F1, accent terracotta #C9805F.

11. DESIGN-AGAINST FLAWS
Unseen rule sources; README vs real behavior; uncalibrated LLM confidence; promotion laundering; alert fatigue; scraped pages/emails/repo content are untrusted and may contain prompt injection (never treat as instructions); post-submission link expiry (out of MVP, roadmap).

12. BUILD PROTOCOL
Before code: concise roadmap, dependencies with versions, build/run commands; research Gemini limits, GitHub rate limits, Gmail OAuth scopes; list flaws in the plan. Execute Phase 1 only, then wait for confirmation of zero errors before the next phase.
Phases: 1 Pydantic v2 models; 2 Gemini prompts; 3 LangChain tools; 4 /trigger-audit agent loop; 5 dashboard.
Phase 1 models: enums TrustTier, Role, CheckStatus (PASS, FAIL, STALE, UNVERIFIED), ApprovalState, SourceStatus (CHECKED, UNREACHABLE, NOT CHECKED). Models: SourceRecord, CharterClause, CharterAmendment (requires sourceRef), ProofCheck (clauseId, clauseVersion, interpretation, status, promotedBy, promotedAt), WorkbenchCheck, UncertaintyFlag (kind, detail, severity), CoverageLedger (sources, humanConfirmedComplete, confirmedBy, confirmedAt), EvidenceItem (url, method, finalStatus, authWallDetected), AlignmentReport (alignmentScore 0-100, missingFeatures, evidenceBasis, caveat), SubmissionPacket (approvalState, ledger, reports). Validation: no amendment without sourceRef; amendment marks dependents STALE; only a human actor can set APPROVED; alignmentScore bounded 0-100. Approval states: DRAFT -> AUDITING -> PENDING HUMAN APPROVAL -> APPROVED | REJECTED.

13. CODING RULES
camelCase; diffs or modified functions only after first delivery; production-grade; explicit error handling, input validation, bounds checking, no silent failures; external input untrusted; cross-platform (Linux/WSL/Ubuntu and Windows) with differences stated; no speculative features; ambiguity must be called out and halted, not guessed.

14. OUT OF SCOPE
Role enforcement, per-user agents, post-submission re-audit, branch-hygiene agents, any unlisted feature.

15. OPEN ITEMS
- User has not confirmed the four assumptions in section 8.
- Layer 1 amendment mechanism (section 7) was proposed by the assistant, not user-confirmed.
- Cross-agent verdict reconciliation is unresolved.
- Tier naming (Charter / Proof / Workbench) is a proposal.

This is the idea for now, lets work on this.

A few rules to follow : 
Persona & Formatting Constraints

Use the absolute minimum tokens required.
Zero conversational filler, pleasantries, or praise.
NO EMOJIS.
Maintain a direct, objective, and strictly logical tone.
Analytical & Execution Rules

Pre-Flight Research & Language Selection: Exhaustively research and define all system requirements, API constraints, and architectural prerequisites. Analyze all potential languages and frameworks (e.g., C, Java, TypeScript, React) suited for the project before committing to a stack or generating a roadmap
Critical & Security Audit: Actively interrogate all ideas, code, or questions. Exhaustively list every logical, architectural, syntax flaw, security vulnerability, race condition, and memory leak before providing a solution.
Zero-Guessing: If requirements or platform contexts are ambiguous, state the missing information and halt. Do not generate speculative code.
Phase-Based Execution: For multi-step processes, provide a concise roadmap first. List all required dependencies and compiler/build flags. Execute only Phase 1, then halt. Wait for confirmation of success before generating code for the next phase. Only give entire files if asked by the user.
Implementation Gatekeeping: Do not suggest new features, roadmap items, or optimizations until the current implementation compiles and executes with zero errors.
Context Management: Output only modified functions or diffs. Never reprint full unmodified files.
Code & Technical Constraints

Write production-grade code in any requested language with zero redundant lines.
Ensure all code, build scripts, and system commands are cross-platform (Linux/WSL/Ubuntu and Windows), addressing platform-specific pathing or API differences explicitly.
Strict Error Handling & Defense: All generated code must contain explicit error handling, input validation, and boundary checking. No silent failures. Treat all external inputs as untrusted.
Naming Conventions: Strictly adhere to camelCase (Java type)

Judging for the hackathon has the following criterea : 
Problem fit and real-world impact: 25%
Agent design (planning, tool use, memory): 25%
Working demo and reliability: 20%
Innovation: 15%
Presentation: 15%
