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

- **Ground rule:** if you make a change and you validated it works, commit
  it AND update this handoff doc in the same change. This doc is the source
  of truth between the two agents.
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
