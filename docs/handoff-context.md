# Handoff Context

Repo: `C:\Users\yashs\OneDrive\Desktop\token26`
Remote: `https://github.com/test-user98/dl-automation.git`
Current local checkpoint commit: latest local `HEAD` (`Build customer onboarding and status layer`)
Previous agent-hardening checkpoint: `52c2100`

Do not push without explicit user approval. The user wants local proof before
remote updates.

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

- FastAPI backend: `api/server.py`, `api/onboard.py`
- Customer UI: `frontend/index.html`
- Browser agent: `agent/brain.py`
- Playwright wrapper: `browser/controller.py`
- OCR: `tools/ocr_service.py`
- CAPTCHA challenge handling: `tools/captcha_solver.py`
- Portal deterministic rules: `config/portal_rules.py`
- Repo notes: `AGENTS.md`, `docs/agent-memory.md`

Latest checkpoint hardened the agent:

- rule-book selectors/functions for Sarathi known pages;
- self-evolving `data/discovered_rules.json` overlay support;
- manual CAPTCHA fallback via `data/manual_captcha.txt`;
- OTP via `data/manual_otp.txt` in console mode;
- CAPTCHA image fetch/readback proof before submit;
- page-closed observe guard;
- no-progress loop detection;
- bad navigation guard for Dashboard/Login/Change State/Home.

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
- CAPTCHA can refresh after a failed attempt. The agent now verifies input fill,
  but the flow still needs live testing through Generate OTP and OTP submit.
- Customer-facing status mapping now exists in `api/status_messages.py`.
- Shared backend singletons now exist in `api/deps.py`, so server/onboard use
  the same state manager, OCR service, orchestrator, learning store, and human
  loop.
- OCR now retries according to `OCR_MAX_ATTEMPTS`, returns confidence/missing
  fields/manual-review flags, and uses the unified LLM client.
- Customer UI is functional for details entry, DL OCR upload, selfie upload,
  review, agent status polling, OTP entry, and success/error/action-needed
  states. Full live Sarathi end-to-end is still not verified after this UI
  checkpoint.

## User Preferences

- Keep the user in the loop.
- Ask when stuck or when user data is needed.
- No assumptions about user data.
- Do not push before local verification.
- Use `334401` as test PIN code.
- Email is optional; if required, use `sipanijai@gmail.com`.

## Latest Local Verification

- `python -m py_compile api\deps.py api\status_messages.py api\server.py
  api\onboard.py tools\ocr_service.py config\settings.py`
- `GET /health` returned ok.
- `POST /onboard/validate-dl` normalized `RJ07 2017 0010191`.
- Fake `STUCK_HUMAN_NEEDED` OTP job mapped to customer action type `otp`.
- `POST /jobs/{job_id}/otp` accepted OTP and bridged to state/human loop.
- Browser UI smoke test loaded `http://127.0.0.1:8000/`, filled DL/DOB/mobile
  PIN, clicked Review details, and confirmed the review screen rendered the DL
  and PIN.

## Immediate Next Slice

Resume live Sarathi testing from Generate OTP / Validate OTP:

- confirm OTP value is visibly entered in `#otpNumber`;
- solve the latest visible CAPTCHA only, retry 3-4 times with refresh if needed;
- ensure checkbox is checked;
- submit with `#verifySarathi` / `verifiedBySarathi()`;
- if the portal rejects CAPTCHA or OTP, surface a friendly action-needed state
  and avoid asking for a fresh OTP unless the page actually requires one.
