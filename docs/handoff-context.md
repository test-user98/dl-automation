# Handoff Context

Repo: `C:\Users\yashs\OneDrive\Desktop\token26`
Remote: `https://github.com/test-user98/dl-automation.git`
Current local checkpoint commit: latest local `HEAD` (`Add live-deploy verification + handoff sync`)
Previous checkpoint: `674096e` (deploy pipeline), `fc15a82` (customer onboarding + status layer), `52c2100` (agent hardening)

Do not push without explicit user approval. The user wants local proof before
remote updates.

## Two-agent collaboration protocol

Two LLMs are working on this repo in parallel (one focused on the customer
layer + Sarathi flow, one focused on infra/deploy). To stay coherent:

- **Ground rule #1 (test before push):** every change MUST be tested locally
  before pushing to `origin/main`. That means:
  1. Run the existing test suite (`python -m pytest`) — no regressions.
  2. If you touched code reachable from `/api`, boot uvicorn locally and
     curl the affected endpoints. New endpoints get new test cases.
  3. Think through edge cases (empty/missing input, unauthorised, unknown
     IDs, malformed payload, rate limits, oversized data) and verify each.
  4. Only then `git push origin main` — the workflow auto-deploys to
     App Runner. A bad push wastes a 6–8 minute build cycle, and worse,
     poisons `/health.commit` for anyone watching live.
- **Ground rule #2 (commit + handoff together):** if you make a change and
  you validated it works, commit it AND update this handoff doc in the
  same change. This doc is the source of truth between the two agents.
- Whoever commits last updates this doc to point at the latest meaningful
  checkpoint and adds a one-line summary of what changed.
- Both follow the same "no push without approval" rule.
- Both prefer narrow, well-scoped commits over big sweeping ones.
- Both run a local sanity check before committing (FastAPI imports cleanly,
  YAML/Python syntax valid).
- Live verification of *what's actually deployed* uses `/health.commit` —
  it returns the git SHA the running container was built from. This is the
  source of truth for "is my commit live yet?".

## Logging + redaction

`config/logging_setup.py` is the single structlog config. Both `api/server.py`
and `run_agent.py` import `configure_logging()` at startup. A redaction
processor runs BEFORE the timestamp processor and masks:

- API keys: `sk-…`, `sk-ant-…`, `AKIA…`, `Bearer …`
- 10-digit numbers (mobile), 12-digit (Aadhaar), 6-digit (OTP/PIN) inside
  free-text strings;
- any value whose key matches `_SENSITIVE_KEYS` (otp, mobile, dl_number,
  dob, api_key, secret, password, token, aws_*, …) — masked to first2+***+last2
  so support can still correlate.

If you log a new sensitive field, add its key to `_SENSITIVE_KEYS`.

## Product Goal

Build a Cars24/CarInfo-style RTO services prototype where the customer uploads
or enters DL details, the app validates/extracts data, then an AI/browser agent
fills Sarathi without the customer visiting the government portal. Success means:

- customer does not fill Sarathi forms manually;
- agent completes automatically when possible;
- agent asks clear human questions for OTP/CAPTCHA/missing info;
- customer sees friendly status, not raw portal errors;
- final flow reaches application submission/acknowledgement where possible.

## Current State

The codebase has:

- FastAPI backend: `api/server.py`, `api/onboard.py`, shared singletons in `api/deps.py`
- Customer-safe status mapper: `api/status_messages.py`
- Customer UI: `frontend/index.html` (single-page, 5-screen flow)
- Browser agent: `agent/brain.py`
- Playwright wrapper: `browser/controller.py`
- OCR (Claude vision): `tools/ocr_service.py`
- CAPTCHA challenge handling: `tools/captcha_solver.py`
- Portal deterministic rules: `config/portal_rules.py`
- Deploy pipeline: `Dockerfile`, `.dockerignore`, `apprunner.yaml`,
  `.github/workflows/deploy.yml`
- Repo notes: `AGENTS.md`, `docs/agent-memory.md`, `docs/deploy.md`

Hardened agent already shipped (52c2100):

- rule-book selectors/functions for Sarathi known pages;
- self-evolving `data/discovered_rules.json` overlay support;
- manual CAPTCHA fallback via `data/manual_captcha.txt`;
- OTP via `data/manual_otp.txt` in console mode;
- CAPTCHA image fetch/readback proof before submit;
- page-closed observe guard, no-progress loop detection;
- bad navigation guard for Dashboard/Login/Change State/Home.

Customer layer shipped (fc15a82):

- Unified backend singletons (`api/deps.py`) so server + onboard router share
  the same StateManager / HumanLoop / OCR / Orchestrator. Fixes prior bug where
  OTP submitted from UI never reached the running agent.
- `api/status_messages.py` maps raw JobStatus + observations to customer-safe
  `customer_view` payload (title, message, severity, action_required,
  action_type, retryable, last_step_label). Returned by `/jobs/{id}` and SSE.
- `/onboard/extract-dl-image` now returns `confidence`, `missing_fields`,
  `needs_manual_review`, with OCR retried up to `OCR_MAX_ATTEMPTS`.
- `frontend/index.html` is a fully functional single-page flow: DL OCR upload
  → manual edit → review card → live status polling → OTP screen → human-
  response screen → done. Uses `customer_view` from `/jobs/{id}`.

Deploy pipeline shipped (674096e):

- `Dockerfile` based on `mcr.microsoft.com/playwright/python:v1.48.0-jammy`
  (Chromium + libs pre-installed, headless).
- `.github/workflows/deploy.yml` on push to `main`:
  ECR repo create-if-missing → docker build/push (`:latest` + `:<sha>`) →
  IAM ECR-access role create-if-missing → App Runner create-or-update →
  trigger deployment → poll `/health.commit` until it matches expected SHA.
- Region: `ap-south-1` (Mumbai). Service name: `sarathi-agent`. 1 vCPU / 2 GB.
- All AWS resources tagged `Owner=sipanijai@gmail.com`.
- `/health` returns `commit` field sourced from `GIT_COMMIT_SHA` env var
  injected by the workflow — use this to confirm a given commit is live.

## Known Observed Portal Flow

- State dropdown: `#stfNameId`
- DL details CAPTCHA input: often `#entCaptha`
- Generate OTP CAPTCHA input: `#entcaptxt`
- OTP input: `#otpNumber`
- OTP CAPTCHA image: `#capimg`
- OTP submit button: `#verifySarathi`
- OTP submit function: `verifiedBySarathi()`
- Generate OTP button/function: `#generateSarathiotp`, `gensarathiOTP()`

## Current Blockers

- End-to-end DL renewal has not yet reached final acknowledgement reliably.
  Generate OTP / Verify OTP leg still needs live testing.
- SQLite + uploads live on container disk — wiped on each App Runner redeploy.
  Fine for MVP demo; not for real customers. Move to RDS Postgres + S3.
- `frontend/index.html` hardcodes `X-Secret = 'dev-secret-change-in-prod'`.
  GitHub secret `API_SECRET_KEY` must equal that string until we switch to a
  runtime config endpoint.

## Customer-facing UX (current)

`frontend/index.html` is a 3-step intake + live status + OTP + done:

1. **Step 1**: optional DL photo upload. OCR returns confidence + missing
   fields. Low confidence shows a calm "retake or type instead" banner.
2. **Step 2**: confirm DL number (live normalised + validated) + DOB + mobile
   + name (optional).
3. **Step 3**: present-address PIN + review card + inline consent disclosure
   (no checkbox — soft opt-in by clicking "Start my application").
4. **Live screen**: 4-phase track (Connecting → Filling → OTP → Submitting),
   one headline + one subline rotation, portal-down banner on `phase=retrying`.
5. **OTP overlay**: 6-digit boxes, masked mobile suffix "+91 72******63"
   pulled from `customer_view.mobile_suffix`.

`api/status_messages.py` returns:

- new fields: `phase` (connecting/filling/waiting/submitting/retrying/done/failed),
  `headline`, `subline`, `mobile_suffix`;
- legacy fields kept for any older callers.

Portal-down detection: status_messages scans recent step logs for `5xx`,
"service unavailable", "bad gateway", DNS failures — and surfaces a calm
"government portal is slow, we will retry" message instead of an error.

## Operator + Customer-lookup layer (SHIPPED)

This batch landed alongside the live deploy. NOT mocked — actual aiosqlite
tables, async CRUD, 25 pytest tests passing, smoke-verified locally.

**Schema** — `data/customers.db` (separate from `data/jobs.db`):

- `customers(customer_id PK CUST-XXXXXXXX, phone UNIQUE, name, email,
  kyc_status, created_at, updated_at)` — phone normalised on insert
- `applications(app_id PK APP-XXXXXXXXXX, customer_id FK, service_type,
  status, application_number, current_job_id, state_code, fee_inr,
  metadata JSON, created_at, updated_at)`
- `documents(doc_id PK DOC-XXXXXXXXXX, customer_id FK, app_id FK NULL,
  doc_type, file_path, mime_type, size_bytes, ocr_data JSON, confidence,
  uploaded_at)`
- `notes(note_id, app_id FK, operator_id, text, created_at)`

**Hook**: `/onboard/confirm-and-start` now upserts the Customer + creates
an Application linked to the Job, and persists the OCR'd DL image as a
Document with `ocr_data` + `confidence`. `/onboard/extract-dl-image` now
returns `dl_image_path` so the frontend can pass it through.

**Endpoints** (auth via `X-Admin-Secret` header → env `ADMIN_SECRET`,
falls back to `API_SECRET_KEY` if unset):

- `GET /admin/summary` → counts + by-status breakdown
- `GET /admin/customers?limit&offset&search` — search matches phone, name, CUST-id
- `GET /admin/customers/{phone_or_cust_id}` — customer + apps + docs
- `GET /admin/applications?status&service&customer_id&limit&offset`
- `GET /admin/applications/{app_id}` — app + customer + docs + notes + live job view
- `POST /admin/applications/{app_id}/notes` body `{text, operator_id?}`
- `GET /admin/documents/{doc_id}` — metadata
- `GET /admin/documents/{doc_id}/preview` — serves the file (mime-typed)

**Customer self-service** (no admin auth, rate-limited 10/min/phone):

- `GET /lookup?phone=…` OR `?customer_id=…` — returns redacted view
  (`phone_mask`, no DL/DOB, no internal IDs unless looked up by ID)
- Unknown phone returns `{found: false}` to avoid leaking registration.

**Operator dashboard UI** at `/admin` — single self-contained HTML/JS.
Token gate stores secret in sessionStorage only. Tabs: Applications /
Customers. Side drawer for detail with doc previews, metadata, live job
view, and operator notes thread.

**Seed**: on startup, if `customers` table is empty, inserts 5 mock
customers across 5 states with mixed statuses (SUBMITTED, WAITING_OTP,
AGENT_RUNNING, STUCK_HUMAN_NEEDED, COMPLETED, FAILED) so the dashboard
is never empty in demo.

**Tests**: `tests/test_customer_store.py` (10) + `tests/test_admin_api.py`
(15) — covers auth, phone normalisation, dedupe, filters, edge cases,
404s, note validation, rate limit, lookup redaction. Run with
`python -m pytest`.

## Still deferred

- Move state from container disk to RDS Postgres + S3.
- Operator: bulk export, status timeline view, assigning jobs to specific
  operators. None blocking demo.
- Frontend customer "track my application" page that hits `/lookup`.

## User Preferences

- Keep the user in the loop.
- Ask when stuck or when user data is needed.
- No assumptions about user data.
- Do not push before local verification.
- Use `334401` as test PIN code.
- Email is optional; if required, use `sipanijai@gmail.com`.

## How to verify the live deploy

Once the GitHub Actions workflow finishes its first run, the Summary shows the
public URL (e.g. `https://abc123.ap-south-1.awsapprunner.com`). To check that
a specific commit is actually serving traffic:

```
curl https://<service>.ap-south-1.awsapprunner.com/health
# → { "commit": "<git sha>", "status": "ok", ... }
```

The workflow itself blocks on this poll (60 attempts × 15s) before marking the
deploy successful, so a green workflow run = the right commit is live.

To list the AWS resources from the deploy:

```
aws apprunner list-services --region ap-south-1
aws ecr list-images --repository-name sarathi-agent --region ap-south-1
```

## Immediate Next Slice (post-deploy)

Once first live URL is up:

1. Hit the UI in a real browser, walk the form → review → confirm flow,
   confirm the job appears with `customer_view` payload in `/jobs/{id}`.
2. Resume live Sarathi testing from Generate OTP / Validate OTP:
   - confirm OTP value is visibly entered in `#otpNumber`;
   - solve the latest visible CAPTCHA only, retry 3–4 times with refresh
     if needed;
   - ensure checkbox is checked;
   - submit with `#verifySarathi` / `verifiedBySarathi()`;
   - if the portal rejects CAPTCHA or OTP, surface a friendly action-needed
     state and avoid asking for a fresh OTP unless the page actually requires
     one.
3. Then move state out of the container: RDS Postgres for jobs, S3 for uploads.

## Latest Local Verification

- `python -c "from api import server"` → imports cleanly, all routes registered.
- `python -c "import yaml; yaml.safe_load(open('.github/workflows/deploy.yml'))"`
  → workflow YAML parses.
- `GET /health` returns ok + `commit` field (unknown locally; populated in CI).
- `POST /onboard/validate-dl` normalizes Rajasthan DL.
- Fake `STUCK_HUMAN_NEEDED` OTP job maps to customer action type `otp`.
- Browser UI smoke: form fill → Review details → review screen renders DL+PIN.
