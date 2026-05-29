# Handoff Context

Repo: `C:\Users\yashs\OneDrive\Desktop\token26`
Remote: `https://github.com/test-user98/dl-automation.git`
Current local checkpoint commit: latest local `HEAD` (`Improve bidirectional customer progress bridge`)
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
- `application_events(event_id PK, app_id FK, status, title, message,
  actor, created_at)` — customer-visible timeline, oldest to newest
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

## UI + API polish round (SHIPPED)

22 audited issues fixed in one batch. 32/32 pytest pass (7 new tests
added for the new endpoints).

Customer UI (`frontend/index.html`):
- OTP boxes now handle paste (`onpaste` event distributes 6 digits),
  backspace navigates to previous box, arrow keys + Enter wired,
  `autocomplete="one-time-code"` for Android SMS autofill.
- Mobile regex tightened to `^[6-9]\d{9}$` (Indian mobile).
- DOB sanity check: rejects impossible dates, future dates, < 1924.
- File upload guard client-side: 8 MB max, accepts JPG/PNG/HEIC/WEBP/PDF.
- "Try again with the same details" button on FAILED state preserves
  the entered form data instead of `location.reload()`.
- Landing screen "Track existing application" → `/lookup?phone=…`.
- Mobile field copy updated to mark it required and explain it must be
  the DL-registered number.
- Topbar tagline "Powered by OpenAI · Codex".

Operator UI (`frontend/admin.html`):
- Status filter widened to all 15 JobStatus values.
- `prettyService` recognises DL/RTO/OTP/PIN/OCR/KYC/NOC acronyms
  ("DL Renewal" not "Dl Renewal").
- Search field passes through to API (`?search=` param).
- Doc preview uses `?secret=…` query param (img tags can't send
  headers); broken images render an inline "Preview unavailable" panel.
- Empty-note submission shows a toast instead of failing silently.
- Sign-out button in topbar, Enter submits token gate, ESC closes the
  drawer.
- Apps table wrapped in `.table-scroll` for mobile.
- Status pills added for OTP_RECEIVED, OCR_PROCESSING, OCR_DONE,
  PARTNER_HANDOFF.

API:
- `/onboard/extract-dl-image` rejects >8 MB (413) and non-image MIME
  (415); filename sanitised.
- `/admin/applications/{app_id}/status` — operator manual status push.
  Validates against the JobStatus enum, auto-records an operator note.
- `/admin/applications` accepts `?search=` (LIKE on app_id, app_number,
  customer phone, customer name).
- `/lookup` now accepts `?application_number=…` in addition to phone +
  customer_id.
- `/favicon.ico` served (silences the 404).
- CORS via `CORS_ALLOW_ORIGINS` env (comma-separated origins; off
  by default → same-origin only).
- Orchestrator mirrors terminal Job status (COMPLETED / FAILED /
  CANCELLED) back into the matching Application row, so the operator
  dashboard never goes stale.

Workflow (`.github/workflows/deploy.yml`):
- `update-service` no longer swallows errors (`>/dev/null` + `|| true`
  removed). The previous version masked a real failure during the
  c102e5e → 1b38006 deploy where env vars (GIT_COMMIT_SHA,
  ADMIN_SECRET) silently stayed stale until I forced an update via
  boto3.
- Workflow now waits for `Service.Status=RUNNING` before calling
  `start-deployment`, eliminating the race between config update and
  fresh image pull.

## Inputs in the UI (for the user)

If you want to refine any of these labels/hints/copy, list them.

Customer flow:
| Field | Where | Type | Required | Current label / hint |
|---|---|---|---|---|
| DL photo | Step 1 | file (≤8 MB) | optional | "Upload a photo of your DL — we'll auto-read…" |
| DL number | Step 2 | text | required | "Driving licence number" / "Printed on the front…" |
| Date of birth | Step 2 | DD-MM-YYYY | required | "Date of birth" |
| Mobile number | Step 2 | 10-digit `[6-9]\d{9}` | required | "Mobile number * / Need the number registered with your DL…" |
| Full name | Step 2 | text | optional | "Full name (optional) / Leave blank — we'll read it from the portal." |
| PIN code | Step 3 | 6-digit | required | "Present address PIN code" |
| OTP | OTP screen | 6 digits | required when prompted | "Enter the OTP" |
| Free-text answer | Human-needed | text | required when prompted | "Your answer" |
| Phone (track) | Landing → Track | 10-digit | required if used | "10-digit mobile" |

Operator flow:
| Field | Where | Type | Required | Current label / hint |
|---|---|---|---|---|
| Admin secret | Token gate | password | required | "Admin secret" |
| Operator note | App drawer | text | required | "Add an operator note…" |
| Status change | (not yet in UI; API only) | enum | required | n/a |

## Still deferred

- Move state from container disk to RDS Postgres + S3.
- Status-change widget in operator drawer (endpoint exists, UI not yet).
- Bulk export, status timeline view, per-operator job assignment.

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

- Timeline/status slice:
  - durable `application_events` rows are created for application creation,
    status changes, and acknowledgement generation;
  - `/lookup` returns `timeline` per application for customer tracking;
  - `/admin/applications/{app_id}` returns `events`;
  - `/admin/applications/{app_id}/status` updates status, writes an operator
    note, appends a customer-visible event, and optionally sends SMTP email;
  - admin drawer renders timeline + manual status control;
  - customer lookup renders order-delivery-style timeline entries.
- Optional email config: `EMAIL_NOTIFICATIONS_ENABLED=true`,
  `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USERNAME`,
  `SMTP_PASSWORD` (Gmail app password), `SMTP_FROM`.
- Validated this slice with `python -m py_compile ...`, `python -m pytest`
  (37 passed), and a fresh local browser smoke against `127.0.0.1:8000`
  rendering customer timeline + `SMOKE-ACK-002` and admin timeline/status UI.

- `python -c "from api import server"` → imports cleanly, all routes registered.
- `python -c "import yaml; yaml.safe_load(open('.github/workflows/deploy.yml'))"`
  → workflow YAML parses.
- `GET /health` returns ok + `commit` field (unknown locally; populated in CI).
- `POST /onboard/validate-dl` normalizes Rajasthan DL.
- Fake `STUCK_HUMAN_NEEDED` OTP job maps to customer action type `otp`.
- Browser UI smoke: form fill → Review details → review screen renders DL+PIN.

## Latest Agent Automation Verification

- Live run `data/live-agent-test-22.out.log`, job
  `fe3199e6-452b-4c02-bfc7-a6b9a0892328`, validated the flow through OTP:
  DL fetch → DL confirmation → email confirmation → auth method →
  Generate OTP → user OTP `003631` → OTP CAPTCHA → `#otpCheckbox` →
  `#verifySarathi` / `verifiedBySarathi()` → landed on `eKycOTPAuth.do`.
- CAPTCHA retry limit is now 4 and was exercised in the Generate OTP path:
  attempts 1–2 failed/rejected, attempt 3 used solver pass 4 with confidence
  0.9 and generated OTP successfully.
- Service list after OTP was extracted successfully. Available services
  included `CHANGE OF DATE OF BIRTH IN DL`, address/name/photo changes,
  duplicate DL, IDP, DL extract, PSV badge, replacement DL, and surrender COV.
- Service selection for `CHANGE OF DATE OF BIRTH IN DL` clicked the visible
  checkbox and injected canonical `name="dlc"` because Sarathi used `dlc1`
  on the visible input. The portal then rejected the selected service with:
  `Unable to Process your Data. DL Holder Requested Service: CHANGE OF DATE
  OF BIRTH IN DL is not legible for Requested RTO: DTO, LONGDING`.
- This is now treated as a deterministic RTO/service eligibility rejection,
  not an automation failure or retry loop. The agent marks the job failed with
  a customer-safe non-retryable message: this service is not available at the
  resolved RTO; choose another service or visit the RTO/RLA authority.

## Bidirectional Customer-Agent Contract

The customer app is now a two-way bridge between Sarathi and the user:

- Agent asks through `HumanLoop.ask(...)`.
- `HumanLoop` stores a structured
  `job.customer_data["_pending_customer_request"]`:
  `step_name`, `question`, `context`, `options`, `action_type`.
- `api.status_messages.customer_job_view(job)` exposes that as
  `customer_view.customer_request` and sets `action_required/action_type`.
- Frontend renders:
  - `action_type="otp"` as the OTP screen;
  - `confirmation` as a review/consent style prompt with context and options;
  - `service_selection` / `choice` / `text` as the human-needed screen;
  - option buttons for Sarathi choices like available DL services;
  - free text for missing details like DOB-change reason.
- Customer answers go back to:
  - `/jobs/{job_id}/otp` for OTP and resend (`{"otp":"resend"}`);
  - `/jobs/{job_id}/human-response` for service choices and other details.

Validated edge cases:

- OTP wait maps to `action_type="otp"` with masked mobile suffix.
- OTP resend button posts `{"otp":"resend"}`.
- Service-selection request carries options and the UI posts the clicked
  service text back to `/human-response`.
- Portal-down / 5xx messages map to retrying with a calm customer message.
- RTO/service ineligibility maps to a terminal, non-retryable friendly
  message instead of a loop.
- Live customer status now uses `/jobs/{job_id}/stream?secret=...` via
  `EventSource`, with polling fallback. The UI shows `last_step_label` as a
  customer-safe "Latest update" so the customer can see progress during the
  1-2 minute Sarathi automation wait.
- Human-needed UI now renders `customer_request.context` in a compact context
  box. This is where the agent can show refined portal details for customer
  confirmation, e.g. extracted DL holder name/DOB/RTO/service before moving on.

Latest validation for this contract:

- `python -m py_compile api/server.py api/status_messages.py agent/human_loop.py`
- `python -m pytest` → 39 passed.
- Local browser smoke against `127.0.0.1:8000`: live status rendered
  `Latest update: Looking up your DL on the portal`; confirmation request
  rendered `Name/DOB/DL` context and posted `Yes, details are correct` to
  `/human-response`.
