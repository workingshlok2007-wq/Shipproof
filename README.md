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
| Gate | A confusion index is computed from structural signals only (conflicts, undated rules, unreachable sources, failed renders, suspected prompt injection). Above the threshold the agent halts and asks. |
| Plan | Builds the task list: repository audit, link audits, scope alignment, interpretation checks. |
| Audit | GitHub visibility as an anonymous user, README, license, commits; headless link checks for login walls and soft 404s; alignment score computed from code evidence, not from README prose. |
| Finalize | Ends at `PENDING_HUMAN_APPROVAL`. Approval requires an authenticated human, a human-confirmed Coverage Ledger, no STALE checks and no CRITICAL flags. |

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

Open http://localhost:3000 and paste the contents of `.shipproof-token`. The dashboard proxies `/api/*` to the engine (`SHIPPROOF_API_URL`, default `http://127.0.0.1:8000`), so only port 3000 needs to be reachable from the browser.

Optional environment: `GITHUB_TOKEN` (5000 instead of 60 GitHub requests per hour), `GMAIL_CLIENT_SECRETS` and `GMAIL_TOKEN_FILE` (authorise once with `python -m shipproof.tools.gmailIngest`), `GEMINI_MODEL`, `GEMINI_FALLBACK_MODELS`, `SHIPPROOF_CONFUSION_THRESHOLD` (default 15), `SHIPPROOF_DATA_FILE`.

## Known limits

- Login-wall detection uses URL and text heuristics (English); it can miss unfamiliar sites.
- Alignment counts promised features with matching file or symbol names. That shows code exists, not that it works.
- Sources the agent cannot see (Discord, mentor comments, login-gated pages) are listed as not covered; the human confirms completeness.
- Single user. Roles are modelled but not enforced; multi-user access tiers are roadmap.
