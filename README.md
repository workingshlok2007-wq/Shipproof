# ShipProof

An agentic submission reliability system. ShipProof audits a hackathon or client deliverable before it is submitted: it reads the rules, checks every artifact the way a logged-out judge would see it, compares the shipped code with the original pitch, and stops at a human approval boundary. The agent prepares the evidence. A human decides.

## Why

Teams lose for administrative reasons (private demo video, private repo, missed rule change in an email) and for scope drift (shipping something that works but is not what was proposed). ShipProof targets both, and is designed around one rule: a false "all clear" is worse than no tool, so the agent reports what it could not confirm instead of guessing.

## How it works

The engine is an API-first LangGraph agent (`shipproof/agent/graph.py`):

| Stage | What happens |
|---|---|
| Ingest | Renders rules pages in a logged-out headless browser, reads Gmail (read-only), records every source in a Coverage Ledger as checked, unreachable or not checked. |
| Extract | Gemini extracts rules and deadlines. Every rule must quote its source verbatim or it is dropped. |
| Reconcile | Conflicting deadlines are resolved by recency only when every competing rule is dated; otherwise the conflict is flagged. |
| Gate | A confusion index is computed from structural signals only. Above the threshold the agent halts. |
| Plan / Audit | Baseline checks that always run: GitHub visibility as an anonymous user, README, license, commits; headless link checks; alignment from code evidence; interpretation checks. |
| Investigate | The model chooses up to 3 extra read-only steps per round (2 rounds): read a linked page, check a linked artifact, search the inbox. Targets must be URLs it actually saw; invented or repeated targets are refused and logged. |
| Reflect | A second pass on its own findings: re-checks failed submission links (clearing transient failures), looks for a public alternative on the same site, retries unreachable rules pages, searches the inbox for the newest deadline. |
| Ask | If sources still disagree on the deadline, the audit pauses (`AWAITING_INPUT`) and asks you, with every candidate quoted. Your answer resumes the same audit and is saved to project memory, so the same question is never asked twice. |
| Remediate | Drafts fixes for minor problems: LICENSE (you pick the type), a README that states only what the code proves, marked broken README links, advice for promised features with no code. One click opens a pull request on a new branch; the agent never merges. |
| Finalize | Re-applies risks you accepted earlier, computes the diff with the previous audit, ends at `PENDING_HUMAN_APPROVAL`. |

Every tool call is recorded in an agent trace with its origin (baseline, agent-chosen, follow-up, memory) and reason. Each run has a time budget (default 90 s, `SHIPPROOF_AUDIT_BUDGET_SECONDS`); model calls time out after 20 s with 2 attempts, and anything over budget is flagged as skipped rather than left hanging. At startup the engine lists the Gemini models your key can use, picks the newest stable flash model and probes it; the dashboard header shows the result.

Approval requires an authenticated human, a human-confirmed Coverage Ledger, no STALE checks, no failing promoted checks, and no CRITICAL flag that you have not explicitly accepted with a written reason. Accepted risks are remembered for later audits.

### Trust tiers

- **Charter** (tier 1): the client or organiser requirements. Append-only; changed only by dated amendments with a source reference. An amendment marks every dependent Proof check STALE.
- **Proof** (tier 2): your interpretation of a clause, checked against it. Reports "possible misreading", never "verified". Promotion shows the interpretation beside the exact clause text.
- **Workbench** (tier 3+): scratch checks. Never gates approval.

## Layout

```
shipproof/models.py        Domain models and invariants (Pydantic v2)
shipproof/llm.py           Gemini client, untrusted-input isolation
shipproof/prompts.py       Grounded rule extraction, interpretation check, alignment
shipproof/tools/           GitHub audit, headless link audit, Gmail, code index, SSRF guard, LangChain tools
shipproof/agent/           LangGraph pipeline, reconciliation, store, event bus
shipproof/api.py           FastAPI service and WebSocket event stream
dashboard/                 Next.js + Tailwind visualisation and approval dashboard
tests/                     pytest suite
```

## Run

Requirements: Python 3.11+, Node.js 20+.

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium   # Windows: python -m playwright install chromium
python -m pytest
```

Engine (terminal 1):

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))" > .shipproof-token
export SHIPPROOF_API_TOKEN=$(cat .shipproof-token)   # PowerShell: $env:SHIPPROOF_API_TOKEN = Get-Content .shipproof-token
export GEMINI_API_KEY=...                             # optional; without it, model stages are flagged as skipped
uvicorn shipproof.api:buildApp --factory --host 127.0.0.1 --port 8000
```

Dashboard (terminal 2):

```bash
cd dashboard
npm install
npm run build && npm start        # or: npm run dev
```

Open http://localhost:3000. There is no login screen: the dashboard server reads the engine token from `SHIPPROOF_API_TOKEN` or `../.shipproof-token` and attaches it to every request itself, so the token never reaches the browser. That makes the dashboard the access boundary, so it listens on 127.0.0.1 only (in Codespaces keep the forwarded port private). Cross-site POSTs are refused. The engine address is `SHIPPROOF_API_URL` (default `http://127.0.0.1:8000`).

Optional environment: `GITHUB_TOKEN` (5000 instead of 60 GitHub requests per hour), `GITHUB_WRITE_TOKEN` (needed only to apply suggested fixes: a fine-grained token for the one repository with Contents and Pull requests set to read/write; it can only write `README.md` and `LICENSE` on a new branch), `GMAIL_CLIENT_SECRETS` and `GMAIL_TOKEN_FILE` (authorise once with `python -m shipproof.tools.gmailIngest`), `GEMINI_MODEL`, `GEMINI_FALLBACK_MODELS`, `SHIPPROOF_CONFUSION_THRESHOLD` (default 15), `SHIPPROOF_DATA_FILE`.

## Demo mode (offline, repeatable)

```bash
SHIPPROOF_DEMO=1 uvicorn shipproof.api:buildApp --factory --host 127.0.0.1 --port 8000
```

Serves a fixture event site, repository and inbox on 127.0.0.1 and seeds a `demo` project with planted problems: a private demo video (a public one is linked from the README), an undated rules update that contradicts the dated deadline, a prompt injection on the rules page, a dead slides link, no LICENSE, and one promised feature with no code. Only the fixture origin is exempt from the private-network guard.

To make model answers repeatable and offline, record them once with a working key, then replay:

```bash
SHIPPROOF_DEMO=1 SHIPPROOF_LLM_MODE=record SHIPPROOF_LLM_CACHE=shipproof/demo/llm-cache.json uvicorn shipproof.api:buildApp --factory --port 8000
SHIPPROOF_DEMO=1 SHIPPROOF_LLM_MODE=replay SHIPPROOF_LLM_CACHE=shipproof/demo/llm-cache.json uvicorn shipproof.api:buildApp --factory --port 8000
```

Replay never invents an answer: a prompt that was not recorded fails with a clear message.

## Known limits

- Login-wall detection uses URL and text heuristics (English); it can miss unfamiliar sites.
- Alignment counts promised features with matching file or symbol names. That shows code exists, not that it works.
- Sources the agent cannot see (Discord, mentor comments, login-gated pages) are listed as not covered; the human confirms completeness.
- Single user. Roles are modelled but not enforced; multi-user access tiers are roadmap.
- A paused audit lives in engine memory; restarting the engine while a question is open ends that audit with a CRITICAL flag.
- The confusion index counts login walls as a structural signal (as specified), so a private submission link can also push the index over the threshold.
