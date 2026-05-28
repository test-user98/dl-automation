"""
Quick test runner — opens a real browser and runs the DL renewal agent.
Watch the Chromium window to see exactly what the agent does.
"""

import asyncio
import sys
import structlog
import logging

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    processors=[
        structlog.dev.ConsoleRenderer(colors=True),
    ],
)

sys.path.insert(0, ".")

from config.settings import get_settings
from agent.state_manager import StateManager, JobStatus
from agent.learning_store import LearningStore
from agent.human_loop import HumanLoop
from agent.brain import AgentBrain
from browser.controller import BrowserController
from orchestrator import Orchestrator

settings = get_settings()
SARATHI_HOME = f"{settings.sarathi_base_url}/sarathiHomePublic.do"


async def main():
    print("\n" + "="*60)
    print("  SARATHI AGENT — DL RENEWAL TEST RUN")
    print("="*60)
    print(f"  LLM Primary : {settings.llm_primary} ({settings.resolved_model_for(settings.llm_primary)})")
    print(f"  CAPTCHA     : {settings.captcha_provider}")
    print(f"  Browser     : {'headless' if settings.browser_headless else 'VISIBLE (you can watch)'}")
    print("="*60 + "\n")

    sm = StateManager()
    ls = LearningStore()
    hl = HumanLoop(sm)

    # Seed known portal quirks
    await ls.seed_known_scenarios()

    job = sm.new_job(
        customer_id   = "test_rj_run",
        service       = "DL_RENEWAL",
        state_code    = "RJ",
        customer_data = {
            "dl_number":     "RJ0720170010191",
            "dob":           "04-09-1998",
            "name":          "",           # will be read from portal
            "mobile_number": "9999999999", # replace with real number for OTP
            "email":         "",
            "state_code":    "RJ",
            "state":         "Rajasthan",
            "state_name":    "Rajasthan",
        },
        documents     = {},
    )
    await sm.save(job)

    print(f"Job created: {job.job_id}")
    print(f"Opening browser -> navigating to Sarathi...\n")

    browser = BrowserController()
    try:
        await browser.start()
        await browser.goto(SARATHI_HOME)

        brain = AgentBrain(
            browser        = browser,
            state_manager  = sm,
            learning_store = ls,
            human_loop     = hl,
        )

        job = await brain.run(job)

    except KeyboardInterrupt:
        print("\n\nStopped by user.")
    except Exception as e:
        print(f"\nAgent error: {e}")
        import traceback; traceback.print_exc()
    finally:
        input("\nPress Enter to close browser...")
        await browser.stop()

    # Final report
    final = await sm.load(job.job_id)
    print("\n" + "="*60)
    print(f"  FINAL STATUS      : {final.status.value}")
    print(f"  APPLICATION NUMBER: {final.application_number or 'N/A'}")
    print(f"  STEPS COMPLETED   : {final.steps_completed}")
    if final.step_logs:
        print(f"\n  STEP LOG (last 5):")
        for s in final.step_logs[-5:]:
            print(f"    [{s['status']}] {s['step_name']} — {s.get('observation','')[:60]}")
    print("="*60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
