# Handoff Context

Repo: `C:\Users\yashs\OneDrive\Desktop\token26`
Remote: `https://github.com/test-user98/dl-automation.git`
Live App Runner URL: `https://thxz3gzmhf.ap-south-1.awsapprunner.com`
Current local checkpoint commit: latest local `HEAD` (`Add dynamic customer agent progress UI`)
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

## Latest Verification Hardening

Current local checkpoint: `Add resilience regression tests and verification matrix`.

This slice did not change production agent behavior. It added regression tests
around the failure modes seen during live Sarathi testing so future fixes do
not silently reintroduce loops or bad customer prompts.

Validated locally:

- `python -m py_compile agent/brain.py api/status_messages.py agent/human_loop.py config/portal_rules.py`
- Targeted resilience run:
  `python -m pytest tests/test_agent_resilience_helpers.py tests/test_customer_interactions.py -q`
  -> 18 passed.
- Full suite: `python -m pytest -q` -> 51 passed.

Coverage added:

- OTP submit failure classification:
  - invalid CAPTCHA -> refresh/retry CAPTCHA, keep the same OTP;
  - expired OTP -> resend and ask for fresh OTP;
  - invalid OTP -> ask for a new OTP;
  - unknown unchanged page -> do not mislabel as success.
- OTP input detection:
  - visible OTP fields are detected;
  - hidden OTP fields do not count;
  - OTP selector candidates do not pick CAPTCHA or DOB fields.
- Bad navigation guard:
  - Dashboard/Login/Change State/Home are blocked mid-application so the
    agent does not restart the portal flow by accident.
- Portal alert classification:
  - invalid CAPTCHA / invalid OTP are treated as failures;
  - "OTP sent successfully" is not treated as a failure.
- No-progress loop fingerprint:
  - query-string churn does not hide loops;
  - real form value changes do alter the signature.
- Self-learning rule overlay:
  - `record_discovery()` persists a discovered selector to
    `data/discovered_rules.json` and applies it live for the current run.
- Customer-safe status mapping:
  - answered human prompts stop asking the customer again;
  - OTP expired/invalid states show the correct customer action;
  - 403/Forbidden becomes a friendly retrying portal message;
  - browser/session interruption becomes a retryable customer-safe failure.

Current verification boundary:

- Unit/API/UI-contract smoke coverage is green.
- A fresh live Sarathi E2E was not run in this slice. Live E2E still requires
  real OTP/CAPTCHA timing and may be blocked by portal availability, 403s, or
  RTO/service eligibility. The last known live boundary remains: OTP path can
  reach service selection, and `CHANGE OF DATE OF BIRTH IN DL` was rejected by
  Sarathi for `DTO, LONGDING`; that rejection is now treated as a terminal,
  customer-safe service/RTO eligibility message.

## Latest Browser/UI Smoke Verification

Current local checkpoint: `Add browser smoke test and fix UI regressions`.

Added `scripts/browser_smoke.py`, a repeatable Chromium smoke test that:

- loads the real customer UI from local FastAPI;
- mocks only `/onboard/confirm-and-start` and `/jobs/smoke-job*` so it does
  not start live Sarathi automation;
- uses the real backend for `/onboard/validate-dl`, `/lookup`, `/admin/summary`,
  `/admin/applications`, and `/admin/applications/{id}`;
- captures console warnings/errors, failed requests, 4xx/5xx API responses,
  API call durations, screenshots, and simple layout overflow issues;
- exercises customer flow:
  landing -> details -> review -> mocked start -> OTP -> service-selection
  human request -> acknowledgement done;
- exercises customer tracking lookup with seeded phone `9876512345`;
- exercises operator dashboard sign-in, summary, applications table, and app
  drawer.

UI fixes from this browser pass:

- Customer review now updates the PIN row when the customer enters the PIN
  instead of showing `PIN code: -`.
- Timeline dot color now has `--accent` defined in both customer and admin UI.
- Admin drawer inputs/selects/textareas are constrained to drawer width.
- Admin drawer positioning is stabilized against horizontally scrollable
  tables, and the smoke test waits for the drawer slide animation before
  measuring layout.

Validated locally:

- `python scripts/browser_smoke.py --base http://127.0.0.1:8000`
  -> `ok: true`
- Browser smoke observed:
  - console issues: `[]`
  - failed requests: `[]`
  - bad responses: `[]`
  - layout issues: `[]`
  - secret source: `frontend/index.html` (masked in output; full secret is not printed)
  - acknowledgement screen: `SMOKE-ACK-123`
  - admin rows: `8`
- `python -m py_compile scripts/browser_smoke.py`
- `python -m pytest -q` -> 51 passed.

Latest tweak:

- Browser smoke no longer hardcodes the UI/API secret. It detects `const SECRET`
  from `frontend/index.html`, falls back to the served customer page, then
  `API_SECRET_KEY`, and only requires `--secret` as an explicit override.
- Do not write cloud credentials into this doc or the repo. Use local env /
  GitHub secrets / AWS secrets only.

Screenshots are written to `data/browser_smoke/*.png` during the run and are
not committed.

## Dynamic Customer Progress UI

The customer live screen is no longer only four static phase buttons. It now
has:

- richer phase cards with sublabels:
  `Connecting / Opening portal`,
  `Filling form / Auto-completing fields`,
  `Your input / OTP or choice`,
  `Submitting / Collecting ACK`;
- green completed states, warning retry states, and red stopped states;
- a live agent activity panel with current focus, chip (`Live`, `Needs you`,
  `Retrying`, `Done`, `Stopped`), and a deduped customer-safe activity feed;
- activity entries generated from `customer_view.last_step_label`,
  `headline/subline`, and recent `step_logs`;
- mobile-safe responsive layout for the phase cards and activity rows.

Backend streaming was also improved: `/jobs/{job_id}/stream` now emits when
`updated_at` or step-log count changes, not only when status/completed-step
count changes. This means retries within the same Sarathi step can still move
the customer UI instead of feeling frozen.

Validated locally:

- `python -m py_compile api/server.py scripts/browser_smoke.py`
- `python -m pytest -q` -> 51 passed
- `python scripts/browser_smoke.py --base http://127.0.0.1:8000` -> `ok: true`
- Browser smoke now verifies the live-progress screen before OTP and captures
  `data/browser_smoke/customer_03_live_progress.png`.
- In-app browser check confirmed the local UI serves `#agent-panel` and the
  updated phase labels.

## Latest Deploy Verification

GitHub Actions run for commit `6917a19`
(`Auto-detect UI secret in browser smoke`) started correctly and passed:

- checkout
- AWS credential configuration
- ECR repository lookup/create
- ECR login
- Docker build and push
- App Runner ECR-access role

It failed in `Deploy to App Runner (create or update)` with exit code `254`.
Public GitHub logs do not expose the underlying AWS exception without signing
in, but runs #5, #6, and #7 failed in the same deploy step while run #4 was the
last successful deploy. The likely failure class is App Runner rejecting a
service update/deployment operation while the service is not yet `RUNNING` or
while another operation is in progress.

Workflow hardening added after this failure:

- serialized deploys with a `concurrency` group per branch;
- wait for App Runner service `RUNNING` before `update-service`;
- retry `update-service` only for operation-in-progress/conflict errors;
- wait after `update-service` before `start-deployment`;
- treat `start-deployment` rejection as acceptable only if the service returns
  to `RUNNING`;
- omit optional blank `ANTHROPIC_API_KEY` from App Runner env vars;
- explicitly fail with a clear message if required GitHub secrets
  `OPENAI_API_KEY` or `API_SECRET_KEY` are empty.

Validated locally before pushing this workflow fix:

- `python -c "import yaml; yaml.safe_load(open('.github/workflows/deploy.yml', encoding='utf-8')); print('workflow yaml ok')"`

Final deploy result:

- Fix commit: `4aad131` (`Harden App Runner deploy workflow`)
- Live URL: `https://thxz3gzmhf.ap-south-1.awsapprunner.com`
- GitHub Actions run: `#8`
- Status: `Success`
- Duration: about 9m14s
- Every step passed, including `Wait for App Runner to serve this commit`.
  That step polls the live App Runner `/health` endpoint and only succeeds
  when `.commit == 4aad131...`, so the live app was verified as updated.
- Manual check after the run:
  `GET https://thxz3gzmhf.ap-south-1.awsapprunner.com/health`
  returned `status=ok`, `commit=4aad131cf8fca54572d06d1713c1e0b38a7f18cd`.
- Only remaining warning: GitHub's Node.js 20 deprecation warning for upstream
  actions (`actions/checkout`, AWS credential action, Docker actions). This is
  not blocking today, but should be upgraded later.

## CRITICAL: live container browser-launch bug (NOT YET FIXED)

Discovered 2026-05-29 while the user was running a real DL submission against
the live App Runner deploy. The customer hit "Portal session interrupted" within
~17 seconds of clicking Start.

**Real failure** (from `GET /jobs/{job_id}.error_message` on the live API):

```
BrowserType.launch: Executable doesn't exist at
/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell

Looks like Playwright was just updated to 1.60.0.
Please update docker image as well.
-  current: mcr.microsoft.com/playwright/python:v1.48.0-jammy
- required: mcr.microsoft.com/playwright/python:v1.60.0-jammy
```

**Root cause:** `requirements.txt` pins `playwright>=1.48.0` with no upper
bound. The Docker base image is `mcr.microsoft.com/playwright/python:v1.48.0-jammy`.
Inside the container `pip install` resolves to playwright **1.60.0**, which
expects chromium at a new path that doesn't exist in the 1.48 base image.
The agent's `BrowserType.launch()` fails immediately; it retries 3 times; the
job dies. The "browser" keyword in the error text routes the customer-facing
message to `"Portal session interrupted"` (`api/status_messages.py:338-343`),
which is misleading — the portal was never contacted.

**Verification gap that allowed this:**

- The workflow's "Wait for App Runner to serve this commit" step polls
  `/health.commit` until it matches the pushed SHA. `/health` only proves the
  FastAPI process started — it does NOT spin up Playwright. So a workflow can
  go green while every actual agent run dies on browser launch.
- No automated post-deploy smoke runs `/onboard/confirm-and-start` against the
  live container and waits for the resulting job to reach at least
  `AGENT_RUNNING` for ~30 s. That is the minimum check that proves the agent
  can actually launch a browser in the container.

**Fix options (NOT YET APPLIED — user wants local proof first):**

- **A. Pin** `playwright==1.48.0` in `requirements.txt` so the SDK matches the
  current Docker base image. Cheapest; no Docker rebuild logic changes.
- **B. Bump** the Dockerfile base to
  `mcr.microsoft.com/playwright/python:v1.60.0-jammy` so the chromium binary
  matches whatever pip resolved. Stays current but risks selector/behavior
  drift in newer Playwright versions.

Plan: implement A, run the full local flow (uvicorn + visible browser) to
prove the agent reaches at least the portal, only then push.

**Verification policy update (apply to both agents):**

- `/health.commit == SHA` is necessary but NOT sufficient for "deploy is good."
- A real live submission via `/onboard/confirm-and-start` (or the equivalent
  customer UI flow) MUST be run before any deploy is called verified.
- Until that smoke is automated in the workflow, the agent that ships a deploy
  also runs the live customer smoke and records the result here, including the
  job's last observed `customer_view.phase` and any `error_message`.

## Current local-testing session (in progress)

To debug the live failure without burning more deploy cycles, a local uvicorn
is running on **port 8001** (port 8000 is held by the other agent's session).
`BROWSER_HEADLESS=false` so the agent's Chrome is visible. Live log tail:
`data/local_run_<timestamp>.log` and the corresponding task output file.
The local Playwright install is SDK 1.60 + chromium 1.60 (matched), so the
container-only mismatch does not reproduce locally — confirming the agent code
itself is sound and the bug is purely the packaging.

## Customer-UI bug round (this slice)

The user ran the live flow and reported four distinct customer-UI bugs that
were independent of the Playwright deploy bug. Root cause for each and the
fix landed in this commit:

- **Bug A — PIN-empty bounce.** On screen-3, leaving PIN empty and clicking
  "Start my application" silently took the customer back to screen-2 with no
  visible error. Cause: `inlineAlert` wrote the error to `#ocr-banner` (which
  lives on screen-1) and called `goStep(2)` for every validation failure.
  Fix in `frontend/index.html`: added per-field error nodes
  (`#dl_raw-error`, `#dob-error`, `#mobile-error`, `#pin_code-error`) and a
  screen-local banner (`#step2-banner`, `#step3-banner`). New helpers
  `setFieldError / clearFieldError / setScreenBanner / focusFirstError`.
  `startJob` now collects all invalid fields, decides whether to stay on
  step 3 (PIN-only failure) or return to step 2 (DL/DOB/mobile failure),
  shows errors inline, and never bounces silently. CSS added for
  `.field-error` and `input.input-error`.

- **Bug B — "Latest update: Filling your application" stuck during OTP wait.**
  When the agent reached the OTP page and called `human_loop.ask`, the job
  flipped to `STUCK_HUMAN_NEEDED` but `last_step_label` continued to read
  from `steps_completed[-1]` (= `accept_alert_popup` → "Filling your
  application"). Fix in `api/status_messages.py`: when status is
  `WAITING_OTP` or `STUCK_HUMAN_NEEDED` with `action_type == "otp"`, the
  function now overrides `step_label = "Waiting for the OTP"`. Other
  human-needed action types use the request title as label.

- **Bug C — "Enter the FRESH OTP / previous OTP expired" on the FIRST OTP.**
  The OTP question text contains "or choose **'Resend** OTP'…" as a built-in
  resend option. The old check `if "expired" in lower or "fresh otp" in
  lower or "resend" in lower` matched on the literal word "resend" and routed
  every OTP through the expired-OTP message. Fix: extracted `_otp_message()`
  helper with explicit pattern sets `_OTP_EXPIRED_PATTERNS` (`otp expired`,
  `otp has expired`, `fresh otp`, `expired otp`, `request a fresh`, …) and
  `_OTP_INVALID_PATTERNS`, and a `_otp_in_question()` helper that checks the
  pending request's own text before falling back to broader job context.
  Also confirmed via regression test that genuine "OTP expired" still trips
  the fresh-OTP branch.

- **Bug D — "Connecting…" stuck even after the portal is open.**
  Previously `close_homepage_popup`, `select_state`, `close_state_popup` all
  mapped to `PHASE_CONNECTING` with title "Connecting to the portal", so the
  customer's headline read "Connecting" while the agent was already deep
  inside Sarathi. Fix in `STEP_TO_PHASE`: those three steps now map to
  `PHASE_FILLING` with new labels "On the portal — preparing your request",
  "Selecting your state", "Opening the DL renewal section". `open_homepage`
  alone keeps `PHASE_CONNECTING`.

- **Bug E — OTP screen not appearing in customer UI even though backend was
  correctly emitting `action_required:true, action_type:"otp"`.** Backend
  payload verified against live job by querying `/jobs/{job_id}` directly
  while the agent was waiting; payload was correct. Frontend code path
  (`applyJob` → `showOTP` → `show('screen-otp')`) also looks correct on
  read. Could not reproduce in the mocked browser smoke. Added diagnostic
  `console.log('[applyJob]', {...})` so the next user run produces clean
  dev-tools evidence (status / action_type / action_required / phase /
  headline / last_step_label) when applyJob fires, and a follow-up log
  `console.log('[applyJob] -> showOTP() called')` confirms the OTP branch
  was entered. Logs will be removed once Bug E is reproduced and fixed.

**What was NOT touched in this slice:**

- The agent code (`agent/brain.py`, `agent/human_loop.py`, …). The live
  agent log proved the agent reached OTP cleanly when run locally; the bugs
  were all in the customer-UI / status-mapping layer.
- The Playwright `requirements.txt` pin. Still required to fix the live
  container, but only landing after this UI round is verified.

**Local verification for this slice:**

- `python -m pytest -q` → 51 passed (no regressions).
- `python scripts/validate_fixes.py` → 18/18 assertions pass:
  - phase advances past `connecting` after popup-closed (Bug D);
  - first OTP with "Resend OTP" option keeps headline "Enter the OTP" (Bug C);
  - `last_step_label = "Waiting for the OTP"` during OTP wait (Bug B);
  - genuine `OTP expired` / `Invalid OTP` still trip their correct branches;
  - leaving PIN empty on step 3 keeps the customer on step 3 with the inline
    error visible, `#pin_code` gets `.input-error` class, the error clears
    on typing (Bug A).
- `python scripts/browser_smoke.py --base http://127.0.0.1:8001` →
  `ok: true`, no console issues, no failed requests, no bad responses, no
  layout issues. OTP + human-needed mock flows still post correctly.

**Loose ends I noticed but did NOT change:**

- `api/status_messages.py:_service_rejection_message(lower)` has an unused
  `lower` parameter (IDE hint). Pre-existing; not from this slice.
- `tests/test_customer_interactions.py` only asserted the customer-safe
  failure-mode messages, not the new step_label override during OTP. Worth
  adding a unit test there in a future slice — for now the new
  `scripts/validate_fixes.py` covers it.
- `frontend/index.html` still has the diagnostic `console.log` calls in
  `applyJob`. Remove once Bug E is reproduced and root-caused.
