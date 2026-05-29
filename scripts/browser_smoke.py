"""Browser smoke test for the customer + operator UIs.

This test drives the real local HTML in Chromium and mocks only the dangerous
job-start path so it does not launch a live Sarathi automation. It still uses
the real local backend for DL validation, customer lookup, and admin dashboard
data.

Usage:
    python scripts/browser_smoke.py --base http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from playwright.async_api import Page, async_playwright


SECRET_RE = re.compile(r"const\s+SECRET\s*=\s*['\"]([^'\"]+)['\"]")


def _short_url(url: str) -> str:
    return url.replace("http://127.0.0.1:8000", "")


def _mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 4:
        return "***"
    return f"{secret[:2]}***{secret[-2:]}"


def _extract_ui_secret(text: str) -> str:
    match = SECRET_RE.search(text)
    return match.group(1) if match else ""


def _detect_secret(base: str) -> tuple[str, str]:
    """Use the same UI secret that the customer frontend sends to the API."""
    frontend_path = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
    if frontend_path.exists():
        secret = _extract_ui_secret(frontend_path.read_text(encoding="utf-8", errors="ignore"))
        if secret:
            return secret, str(frontend_path)

    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}/", timeout=5) as response:
            secret = _extract_ui_secret(response.read().decode("utf-8", errors="ignore"))
        if secret:
            return secret, f"{base.rstrip('/')}/"
    except Exception:
        pass

    secret = os.environ.get("API_SECRET_KEY", "")
    if secret:
        return secret, "API_SECRET_KEY"

    raise RuntimeError(
        "Could not detect API secret. Pass --secret, set API_SECRET_KEY, "
        "or ensure frontend/index.html defines const SECRET."
    )


async def _layout_issues(page: Page, label: str) -> list[dict[str, Any]]:
    """Catch browser-visible layout regressions without being too noisy."""
    return await page.evaluate(
        """(label) => {
          const issues = [];
          const doc = document.documentElement;
          if (doc.scrollWidth > window.innerWidth + 2) {
            issues.push({
              label,
              type: 'horizontal_overflow',
              scrollWidth: doc.scrollWidth,
              viewport: window.innerWidth
            });
          }
          const visible = (el) => {
            const drawer = el.closest?.('.drawer');
            if (drawer && !drawer.classList.contains('on')) return false;
            const s = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return s.display !== 'none' && s.visibility !== 'hidden'
              && r.width > 0 && r.height > 0;
          };
          const watch = [
            ...document.querySelectorAll('button,input,select,textarea,.card,.panel,.drawer')
          ].filter(visible);
          for (const el of watch) {
            const r = el.getBoundingClientRect();
            const name = el.id || (el.textContent || '').trim().slice(0, 40) || el.tagName;
            if (r.left < -2 || r.right > window.innerWidth + 2) {
              issues.push({
                label,
                type: 'outside_viewport_x',
                name,
                left: Math.round(r.left),
                right: Math.round(r.right),
                viewport: window.innerWidth
              });
            }
            if (el.tagName === 'BUTTON' && el.scrollWidth > el.clientWidth + 2) {
              issues.push({
                label,
                type: 'button_text_overflow',
                name,
                scrollWidth: el.scrollWidth,
                clientWidth: el.clientWidth
              });
            }
          }
          return issues;
        }""",
        label,
    )


def _mock_job_payload(status: str, customer_view: dict[str, Any], app_no: str = "") -> dict[str, Any]:
    return {
        "job_id": "smoke-job",
        "status": status,
        "steps_completed": ["fetch_dl_details"],
        "application_number": app_no,
        "otp_pending_type": "mobile" if customer_view.get("action_type") == "otp" else "",
        "error_message": "",
        "last_url": "envaction.do",
        "step_logs": [],
        "updated_at": "2026-05-29T13:00:00",
        "customer_view": customer_view,
    }


async def _install_job_mocks(page: Page, posts: list[dict[str, Any]]) -> None:
    state = {"phase": "otp"}

    async def route_jobs(route, request):
        url = request.url
        method = request.method

        if url.endswith("/onboard/confirm-and-start") and method == "POST":
            posts.append({"url": _short_url(url), "body": request.post_data})
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "job_id": "smoke-job",
                    "status": "AGENT_RUNNING",
                    "application_id": "APP-SMOKE",
                }),
            )
            return

        if "/jobs/smoke-job/otp" in url and method == "POST":
            posts.append({"url": _short_url(url), "body": request.post_data})
            state["phase"] = "human"
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"message": "OTP received, agent resuming"}),
            )
            return

        if "/jobs/smoke-job/human-response" in url and method == "POST":
            posts.append({"url": _short_url(url), "body": request.post_data})
            state["phase"] = "submitted"
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"message": "Response received, agent resuming"}),
            )
            return

        if "/jobs/smoke-job" in url and method == "GET":
            if state["phase"] == "otp":
                body = _mock_job_payload(
                    "WAITING_OTP",
                    {
                        "phase": "waiting",
                        "headline": "Enter the OTP",
                        "subline": (
                            "The government portal just sent an OTP to +91 98******63. "
                            "Enter it here so we can submit your application."
                        ),
                        "severity": "action",
                        "action_required": True,
                        "action_type": "otp",
                        "mobile_suffix": "+91 98******63",
                        "last_step_label": "Waiting for the OTP",
                        "customer_request": {},
                    },
                )
            elif state["phase"] == "human":
                body = _mock_job_payload(
                    "STUCK_HUMAN_NEEDED",
                    {
                        "phase": "waiting",
                        "headline": "Choose a DL service",
                        "subline": "Which DL service would you like to apply for?",
                        "severity": "action",
                        "action_required": True,
                        "action_type": "service_selection",
                        "last_step_label": "Filling your application",
                        "customer_request": {
                            "step_name": "service_selection",
                            "question": "Which DL service would you like to apply for?",
                            "context": "Sarathi says these services are available for this licence.",
                            "options": ["CHANGE OF DATE OF BIRTH IN DL", "DL EXTRACT"],
                            "action_type": "service_selection",
                        },
                    },
                )
            else:
                body = _mock_job_payload(
                    "SUBMITTED",
                    {
                        "phase": "done",
                        "headline": "Application submitted",
                        "subline": "Your application was submitted on the government portal.",
                        "severity": "success",
                        "action_required": False,
                        "action_type": "",
                        "retryable": False,
                        "last_step_label": "Collecting your acknowledgement",
                        "customer_request": {},
                    },
                    app_no="SMOKE-ACK-123",
                )
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
            return

        await route.continue_()

    await page.route("**/*", route_jobs)


async def run(base: str, secret: str, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_bust = int(time.time() * 1000)
    console_issues: list[dict[str, Any]] = []
    failed_requests: list[dict[str, Any]] = []
    api_calls: list[dict[str, Any]] = []
    request_started: dict[Any, float] = {}
    posted_bodies: list[dict[str, Any]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()

        page.on(
            "console",
            lambda msg: console_issues.append({
                "type": msg.type,
                "text": msg.text,
                "url": msg.location.get("url"),
            }) if msg.type in {"error", "warning", "warn"} else None,
        )
        page.on("request", lambda req: request_started.__setitem__(req, time.perf_counter()))
        page.on(
            "requestfailed",
            lambda req: failed_requests.append({
                "method": req.method,
                "url": _short_url(req.url),
                "failure": (req.failure or {}).get("errorText", ""),
            }),
        )

        async def response_hook(resp):
            req = resp.request
            dur = None
            if req in request_started:
                dur = round((time.perf_counter() - request_started[req]) * 1000)
            if any(token in resp.url for token in ["/onboard/", "/jobs/", "/lookup", "/admin/"]):
                api_calls.append({
                    "method": req.method,
                    "url": _short_url(resp.url),
                    "status": resp.status,
                    "duration_ms": dur,
                })

        page.on("response", response_hook)

        # Force polling path so the smoke script controls job state cleanly.
        await page.add_init_script(
            'Object.defineProperty(window, "EventSource", { value: undefined, configurable: true });'
        )
        await _install_job_mocks(page, posted_bodies)

        layout: list[dict[str, Any]] = []

        # Customer new application flow.
        await page.goto(f"{base}/?smoke={cache_bust}", wait_until="domcontentloaded")
        await page.screenshot(path=output_dir / "customer_01_landing.png")
        layout += await _layout_issues(page, "customer_landing_desktop")

        await page.click("#step1-next")
        await page.fill("#dl_raw", "RJ0720170010191")
        await page.wait_for_timeout(900)
        await page.fill("#dob", "04-09-1998")
        await page.fill("#mobile", "9876544163")
        await page.fill("#name", "Test User")
        await page.get_by_text("Continue", exact=True).click()
        await page.fill("#pin_code", "334401")
        review_text = await page.locator("#review-rows").inner_text()
        await page.screenshot(path=output_dir / "customer_02_review.png")
        layout += await _layout_issues(page, "customer_review_desktop")

        await page.click("#start-btn")
        await page.wait_for_selector("#screen-otp:not(.hidden)", timeout=7_000)
        otp_subline = await page.locator("#otp-sub").inner_text()
        await page.locator("#otp-row input").first.fill("123456")
        await page.click("#otp-btn")
        await page.wait_for_selector("#screen-human:not(.hidden)", timeout=7_000)
        human_context = await page.locator("#human-context").inner_text()
        human_option = await page.locator("#human-options button").first.inner_text()
        await page.screenshot(path=output_dir / "customer_03_human_request.png")
        layout += await _layout_issues(page, "customer_human_desktop")

        await page.locator("#human-options button").first.click()
        await page.wait_for_selector("#screen-done:not(.hidden)", timeout=7_000)
        done_text = await page.locator("#app-number").inner_text()
        await page.screenshot(path=output_dir / "customer_04_done.png")
        layout += await _layout_issues(page, "customer_done_desktop")

        # Customer lookup against real local API.
        await page.goto(f"{base}/?smoke={cache_bust + 1}", wait_until="domcontentloaded")
        await page.get_by_text("Track existing application", exact=True).click()
        await page.fill("#track-phone", "9876512345")
        await page.get_by_text("Look up", exact=True).click()
        await page.wait_for_selector("#track-result .banner", timeout=7_000)
        track_text = await page.locator("#track-result").inner_text()
        await page.screenshot(path=output_dir / "customer_05_track_lookup.png")
        layout += await _layout_issues(page, "customer_track_desktop")

        # Mobile layout quick pass.
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.goto(f"{base}/?smoke={cache_bust + 2}", wait_until="domcontentloaded")
        await page.click("#step1-next")
        await page.screenshot(path=output_dir / "customer_06_mobile_details.png", full_page=True)
        layout += await _layout_issues(page, "customer_details_mobile")

        # Admin/operator dashboard against real local API.
        admin = await context.new_page()
        await admin.set_viewport_size({"width": 1280, "height": 720})
        admin.on(
            "console",
            lambda msg: console_issues.append({
                "type": msg.type,
                "text": msg.text,
                "url": msg.location.get("url"),
            }) if msg.type in {"error", "warning", "warn"} else None,
        )
        admin.on("request", lambda req: request_started.__setitem__(req, time.perf_counter()))
        admin.on(
            "requestfailed",
            lambda req: failed_requests.append({
                "method": req.method,
                "url": _short_url(req.url),
                "failure": (req.failure or {}).get("errorText", ""),
            }),
        )
        admin.on("response", response_hook)

        await admin.goto(f"{base}/admin?smoke={cache_bust + 3}", wait_until="domcontentloaded")
        await admin.fill("#admin-secret", secret)
        await admin.get_by_text("Open dashboard", exact=True).click()
        await admin.wait_for_selector("#dash:not(.hidden)", timeout=7_000)
        summary = {
            "customers": await admin.locator("#cnt-customers").inner_text(),
            "applications": await admin.locator("#cnt-apps").inner_text(),
            "documents": await admin.locator("#cnt-docs").inner_text(),
        }
        app_rows = await admin.locator("tr.row").count()
        await admin.screenshot(path=output_dir / "admin_01_dashboard.png")
        layout += await _layout_issues(admin, "admin_dashboard_desktop")
        if app_rows:
            await admin.locator("tr.row").first.click()
            await admin.wait_for_selector("#drawer.on", timeout=7_000)
            await admin.wait_for_timeout(250)
            drawer_title = await admin.locator("#drawer-title").inner_text()
            await admin.screenshot(path=output_dir / "admin_02_drawer.png")
            layout += await _layout_issues(admin, "admin_drawer_desktop")
        else:
            drawer_title = ""

        await browser.close()

    bad_responses = [r for r in api_calls if r["status"] >= 400]
    return {
        "ok": not console_issues and not failed_requests and not bad_responses and not layout,
        "review_text": review_text,
        "otp_subline": otp_subline,
        "human_context": human_context,
        "human_option": human_option,
        "done_text": done_text,
        "track_excerpt": track_text[:300],
        "admin_summary": summary,
        "admin_application_rows": app_rows,
        "admin_drawer_title": drawer_title,
        "console_issues": console_issues,
        "failed_requests": failed_requests,
        "bad_responses": bad_responses,
        "layout_issues": layout,
        "api_calls": api_calls,
        "posted_bodies": posted_bodies,
        "screenshots": [str(p) for p in sorted(output_dir.glob("*.png"))],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--secret",
        default="",
        help="Optional override. Defaults to detecting const SECRET from frontend/index.html.",
    )
    parser.add_argument("--out", default="data/browser_smoke")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    secret, secret_source = (args.secret, "cli") if args.secret else _detect_secret(base)
    result = asyncio.run(run(base, secret, Path(args.out)))
    result["secret_source"] = secret_source
    result["secret_masked"] = _mask_secret(secret)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
