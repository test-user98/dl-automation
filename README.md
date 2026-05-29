# Sarathi RTO Automation (DL Flow)

AI-assisted automation for Sarathi DL workflows with customer-in-the-loop inputs (OTP, captcha, choices) and real-time status updates.

## 1) Architecture (short)

```text
Customer UI (frontend/index.html)
  <-> FastAPI API (api/server.py)
      -> Orchestrator (orchestrator.py)
          -> Agent brain (agent/brain.py)
              -> Playwright browser (browser/controller.py)
                  -> Sarathi portal
```

- **UI -> API**: customer submits details, OTP, captcha, or other answers.
- **API -> Agent**: starts/resumes the job.
- **Agent -> UI**: emits action-needed state (`otp`, `captcha`, `service_selection`, etc.) via `/jobs/{id}` + SSE.

## 2) Core flow

1. Customer submits details from UI.
2. API creates job and starts orchestrator.
3. Agent runs Sarathi steps.
4. If customer input is needed:
   - API marks actionable state (`WAITING_OTP` or `STUCK_HUMAN_NEEDED`).
   - UI switches to the correct input screen.
5. Customer submits input:
   - `POST /jobs/{id}/otp` for OTP
   - `POST /jobs/{id}/human-response` for captcha/service/confirmation/text
6. API immediately transitions job back to `AGENT_RUNNING` so UI returns to live progress.
7. Agent continues on Sarathi; on success returns acknowledgement number.

## 3) State-sync rules (important)

- UI should render OTP only when:
  - `status == WAITING_OTP`, or
  - `customer_view.action_required == true` and `action_type == "otp"`.
- UI should render captcha/human screens only when `action_required == true`.
- If no action is pending, UI must show live progress (`screen-4`).
- After input submission, backend transitions to `AGENT_RUNNING` immediately.

## 4) Run locally

Backend:

```powershell
cd C:\Users\yashs\OneDrive\Desktop\token26
uvicorn api.server:app --host 127.0.0.1 --port 8001 --reload
```

Customer UI:

- Open [http://127.0.0.1:8001](http://127.0.0.1:8001)

## 5) Key files

- `api/server.py` - API endpoints + state transitions
- `api/status_messages.py` - customer-safe status mapping
- `frontend/index.html` - customer screens + live state rendering
- `agent/brain.py` - Sarathi automation logic
- `browser/controller.py` - Playwright interaction layer
- `agent/human_loop.py` - customer-input pause/resume bridge

## 6) Current scope

Primary focus is reliable two-way sync:
`customer input -> agent resumes -> Sarathi progresses -> UI updates correctly`.

