# Sarathi DL Renewal Agent

An AI-powered browser agent that autonomously completes Driving Licence Renewal applications on India's [Sarathi portal](https://sarathi.parivahan.gov.in). Built for the Cars24/CarInfo RTO Services automation platform.

## What it does

- Customer submits DL details and documents through a web UI
- An AI agent opens a real browser (Playwright) and fills the government form
- When an OTP is needed, the agent pauses and the customer enters it in the app
- When the agent gets stuck, it **self-diagnoses**, tries a different approach, and learns from what worked — no human intervention needed for common obstacles
- Completed application number is returned to the customer

## Architecture

```
customer app (browser)
        │  POST /onboard/confirm-and-start
        ▼
FastAPI server (api/server.py)
        │  creates Job, starts background task
        ▼
Orchestrator (orchestrator.py)
        │  manages job lifecycle, up to 3 restarts
        ▼
AgentBrain (agent/brain.py)          ◄──── LearningStore (agent/learning_store.py)
  ReAct loop:                               SQLite memory of past scenarios
  1. OBSERVE  screenshot + real DOM         Seeded with known Sarathi quirks
  2. THINK    LLM (GPT-4o / Claude)         Grows with every run
  3. ACT      BrowserController             Human solutions auto-recorded
  4. VERIFY   did URL change? success?
  5. DIAGNOSE if failed: WHY? try different
        │
        ▼
BrowserController (browser/controller.py)
  Playwright (Chromium)
  - DOM inspector: real selectors, not guesses
  - Click fallback chain: selector → JS click → link text
  - Popup auto-handling, payment window tracking
        │
        ▼
Sarathi Portal (sarathi.parivahan.gov.in)
```

## Self-healing behaviour

The agent never repeats the same failed approach twice in a row:

1. Action fails (e.g. selector not found)
2. `_diagnose_failure()` inspects the live DOM — "selector `#stateSelection` doesn't exist; real selector is `#stfNameId`"
3. Diagnosis is fed back into the next LLM call
4. LLM picks a different approach using the actual DOM elements
5. Successful approaches are recorded in the LearningStore for future runs
6. After `MAX_CONSECUTIVE_STEP_FAILURES` (default 4), escalates to human-in-the-loop

## Setup

### Requirements

- Python 3.11+
- OpenAI API key (primary LLM)
- Optionally: Anthropic API key (fallback LLM)

### Install

```bash
pip install -r requirements.txt
playwright install chromium
```

### Configure

Copy `.env.example` to `.env` and fill in your keys:

```env
LLM_PRIMARY=openai
OPENAI_API_KEY=sk-...

# Optional: Anthropic as fallback
LLM_FALLBACK=anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Set to true to force Anthropic (e.g. if OpenAI is down)
LLM_PRIMARY_PAUSED=false

BROWSER_HEADLESS=false      # false = watch the browser, true = server mode
API_SECRET_KEY=change-this-in-production
```

All settings are in `config/settings.py` and overridable via `.env`.

## Running

### Test run (visible browser, Rajasthan DL)

```bash
python run_agent.py
```

Watch the Chromium window. Logs show every step the agent takes, what it sees, and why it made each decision.

### API server

```bash
uvicorn api.server:app --reload
```

Then open `http://localhost:8000` for the customer web UI.

### API endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/onboard/validate-dl` | Validate and normalise a DL number |
| POST | `/onboard/confirm-and-start` | Start a job (creates job + launches agent) |
| GET | `/jobs/{id}` | Get job status |
| POST | `/jobs/{id}/otp` | Submit OTP (agent resumes) |
| GET | `/jobs/{id}/stream` | SSE real-time status stream |
| POST | `/ocr/extract` | Extract data from DL/Aadhaar image |
| GET | `/health` | LLM/browser config status |

All job endpoints require `X-Secret` header matching `API_SECRET_KEY`.

## Project structure

```
.
├── agent/
│   ├── brain.py           # Core ReAct loop — observe, think, act, diagnose, learn
│   ├── llm_client.py      # OpenAI + Anthropic with primary/fallback switching
│   ├── learning_store.py  # SQLite memory of past scenarios and solutions
│   ├── state_manager.py   # Job state machine (SQLite)
│   └── human_loop.py      # Human-in-the-loop escalation
├── browser/
│   └── controller.py      # Playwright wrapper with DOM inspector + click fallbacks
├── flows/
│   └── dl_renewal.py      # DL Renewal step definitions (not a script — a map)
├── tools/
│   ├── captcha_solver.py  # CAPTCHA solving (Claude vision / 2captcha / CapSolver)
│   ├── ocr_service.py     # Document OCR (Claude vision)
│   ├── otp_relay.py       # OTP pause/resume relay
│   ├── image_processor.py # Photo/signature compression
│   └── dl_normalizer.py   # DL number format normalisation
├── api/
│   ├── server.py          # FastAPI app
│   └── onboard.py         # Customer onboarding endpoints
├── config/
│   └── settings.py        # All config via Pydantic-settings + .env
├── frontend/
│   └── index.html         # Customer web UI (5-screen flow)
├── orchestrator.py        # Job lifecycle manager
├── run_agent.py           # Test runner (visible browser)
└── data/                  # SQLite DBs (auto-created)
```

## Key design decisions

**Why Playwright, not Claude computer use?**
Playwright is faster (no API round-trip per click), cheaper, and more reliable for web forms. The LLM acts as the reasoning layer — it decides what to do next — but execution uses Playwright's direct DOM access. Claude's computer use API would add ~2–5s latency per action.

**Why no hardcoded selectors?**
The Sarathi portal's HTML changes. Instead, the agent extracts real DOM elements at each step using JavaScript (`get_interactive_elements()`), sends them to the LLM, and the LLM picks actual selectors from what's on the page — not guesses.

**Why a LearningStore?**
Government portals have consistent quirks: the same popup always appears, the same CAPTCHA refresh pattern, the same modal after state selection. Rather than re-discovering these every run, the agent records what worked and retrieves it next time via keyword similarity — no embedding model needed.

## Configuration reference

| Env var | Default | Description |
|---------|---------|-------------|
| `LLM_PRIMARY` | `openai` | Primary LLM provider |
| `LLM_FALLBACK` | `anthropic` | Fallback when primary fails or is paused |
| `LLM_PRIMARY_PAUSED` | `false` | Force fallback provider |
| `LLM_MODEL` | (provider default) | Override model (e.g. `gpt-4o`, `claude-sonnet-4-6`) |
| `CAPTCHA_PROVIDER` | `claude` | `claude` / `2captcha` / `capsolver` / `manual` |
| `BROWSER_HEADLESS` | `false` | Run browser invisibly |
| `BROWSER_SLOW_MO_MS` | `80` | Slow down browser actions (ms) |
| `MAX_STEPS_PER_JOB` | `100` | Hard limit on agent steps |
| `MAX_CONSECUTIVE_STEP_FAILURES` | `4` | Failures before human escalation |
| `SCENARIO_SIMILARITY_THRESHOLD` | `0.85` | LearningStore match threshold (0–1) |
| `HUMAN_LOOP_BACKEND` | `polling` | `polling` / `webhook` / `firebase` |
| `API_SECRET_KEY` | `change-this` | `X-Secret` header value |
