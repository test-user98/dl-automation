"""
Quick test runner — opens a real browser and runs the DL renewal agent.
Watch the Chromium window to see exactly what the agent does.
"""

import asyncio
import sys

# Windows console needs UTF-8 to handle Unicode in Playwright error messages
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, ".")

from config.logging_setup import configure_logging
configure_logging(level="INFO", json_output=False)

import os
os.environ.setdefault("HUMAN_LOOP_BACKEND", "console")  # show stuck questions in terminal

from config.settings import get_settings
from agent.state_manager import StateManager
from agent.learning_store import LearningStore
from agent.human_loop import HumanLoop
from agent.brain import AgentBrain
from browser.controller import BrowserController

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
            "mobile_number": "7240734163",
            "email":         "",
            "state_code":    "RJ",
            "state":         "Rajasthan",
            "state_name":    "Rajasthan",
            "pin_code":      "334401",     # present address pin code (test value)
            "pincode":       "334401",     # alias used by some form fields
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
        try:
            if sys.stdin.isatty():
                input("\nPress Enter to close browser...")
        except EOFError:
            pass
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
