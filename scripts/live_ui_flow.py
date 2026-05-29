"""Run a real customer-UI -> agent -> Sarathi flow locally.

This is intentionally a headed smoke harness for the live portal path. It:

1. Opens the local customer UI.
2. Fills the customer's details through the UI.
3. Captures the created job_id from /onboard/confirm-and-start.
4. Waits until the UI itself renders the OTP screen.
5. Waits for data/live_ui_otp.txt, types that OTP into the visible UI boxes,
   clicks Submit OTP, and reports the next backend/UI state.

Usage:
    python scripts/live_ui_flow.py --base http://127.0.0.1:8001

When it prints "OTP screen visible", write a 6-digit OTP into:
    data/live_ui_otp.txt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright


DATA_DIR = Path("data")
JOB_FILE = DATA_DIR / "live_ui_job.txt"
OTP_FILE = DATA_DIR / "live_ui_otp.txt"
RESULT_FILE = DATA_DIR / "live_ui_result.json"
SNAPSHOT_FILE = DATA_DIR / "live_ui_failure.png"


async def backend_job(base: str, secret: str, job_id: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{base}/jobs/{job_id}", headers={"X-Secret": secret})
        r.raise_for_status()
        return r.json()


async def write_result(base: str, secret: str, job_id: str, *, harness_error: str = "") -> dict:
    state = await backend_job(base, secret, job_id) if job_id else {}
    view = state.get("customer_view", {})
    result = {
        "job_id": job_id,
        "status": state.get("status"),
        "phase": view.get("phase"),
        "action_type": view.get("action_type"),
        "action_required": view.get("action_required"),
        "headline": view.get("headline"),
        "subline": view.get("subline"),
        "last_step_label": view.get("last_step_label"),
        "last_url": state.get("last_url"),
        "steps_completed": state.get("steps_completed", []),
        "error_message": state.get("error_message", ""),
        "harness_error": harness_error,
    }
    RESULT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8001")
    parser.add_argument("--secret", default="dev-secret-change-in-prod")
    parser.add_argument("--headed", action="store_true", default=True)
    parser.add_argument("--otp-timeout", type=int, default=600)
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    OTP_FILE.unlink(missing_ok=True)
    RESULT_FILE.unlink(missing_ok=True)

    created_job_id = ""

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        async def capture_response(response):
            nonlocal created_job_id
            if response.request.method == "POST" and response.url.endswith("/onboard/confirm-and-start"):
                try:
                    payload = await response.json()
                    created_job_id = payload.get("job_id", "")
                    if created_job_id:
                        JOB_FILE.write_text(created_job_id, encoding="utf-8")
                        print(f"JOB_ID={created_job_id}", flush=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"confirm response parse failed: {exc}", flush=True)

        page.on("response", capture_response)

        await page.goto(args.base)
        await page.locator("#screen-1:not(.hidden)").wait_for(state="visible", timeout=15000)
        await page.locator("#step1-next").click()
        await page.locator("#screen-2:not(.hidden)").wait_for(state="visible", timeout=15000)
        await page.locator("#dl_raw").fill("RJ0720170010191")
        await page.locator("#dob").fill("04-09-1998")
        await page.locator("#mobile").fill("7240734163")
        await page.locator("#name").fill("")
        await page.locator("#screen-2 button.primary").click()
        await page.locator("#screen-3:not(.hidden)").wait_for(state="visible", timeout=15000)
        await page.locator("#pin_code").fill("334401")
        await page.locator("#start-btn").click()
        await page.locator("#screen-4:not(.hidden)").wait_for(state="visible", timeout=15000)

        print("Customer UI job started. Waiting for OTP screen...", flush=True)
        try:
            await page.locator("#screen-otp:not(.hidden)").wait_for(state="visible", timeout=240000)
        except Exception as exc:  # noqa: BLE001
            if not created_job_id and JOB_FILE.exists():
                created_job_id = JOB_FILE.read_text(encoding="utf-8").strip()
            try:
                await page.screenshot(path=str(SNAPSHOT_FILE), full_page=True)
            except Exception:
                pass
            result = await write_result(
                args.base,
                args.secret,
                created_job_id,
                harness_error=f"otp_screen_timeout_or_browser_closed: {exc}",
            )
            print(json.dumps(result, indent=2), flush=True)
            await browser.close()
            return 3

        if not created_job_id and JOB_FILE.exists():
            created_job_id = JOB_FILE.read_text(encoding="utf-8").strip()

        subline = await page.locator("#otp-sub").inner_text()
        print(f"OTP_SCREEN_VISIBLE job_id={created_job_id} subline={subline}", flush=True)

        deadline = time.monotonic() + args.otp_timeout
        otp = ""
        while time.monotonic() < deadline:
            if OTP_FILE.exists():
                raw = OTP_FILE.read_text(encoding="utf-8", errors="ignore")
                digits = re.sub(r"\D", "", raw)
                if len(digits) >= 6:
                    otp = digits[:6]
                    break
            await asyncio.sleep(0.5)

        if not otp:
            print(f"OTP_TIMEOUT waiting for {OTP_FILE}", flush=True)
            return 2

        print("Submitting OTP through visible customer UI...", flush=True)
        boxes = page.locator("#otp-row input")
        count = await boxes.count()
        if count == 1:
            await boxes.nth(0).fill(otp)
        else:
            for i, ch in enumerate(otp):
                await boxes.nth(i).fill(ch)
        await page.locator("#otp-btn").click()

        await page.wait_for_timeout(2500)
        result = await write_result(args.base, args.secret, created_job_id)
        print(json.dumps(result, indent=2), flush=True)

        # Keep the browser open briefly so the visible state can be inspected.
        await page.wait_for_timeout(10000)
        await browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
