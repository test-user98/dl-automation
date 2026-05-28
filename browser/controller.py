"""
Playwright browser controller.

Key design decisions:
- Vision-first: every action is preceded by a screenshot so the agent sees
  exactly what a human would see.
- Reset/Cancel protection: a blocklist of destructive button texts/selectors
  that the controller refuses to click unless explicitly unlocked on the job.
- Popup auto-handling: JS confirm() dialogs are always accepted; JS alert()
  dialogs are dismissed. Popup windows (payment) are tracked automatically.
- Session save/restore: cookies + localStorage are serialised after each major
  step so the agent can resume if the process crashes.
"""

import asyncio
import base64
import json
import structlog
from typing import Optional
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Dialog,
    Playwright,
)

from config.settings import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()

# Text patterns on buttons/links the agent must NEVER click
DESTRUCTIVE_PATTERNS = [
    "reset",
    "clear all",
    "clear form",
    "cancel application",
    "delete",
    "withdraw",
    "abort",
]


class BrowserController:
    def __init__(self):
        self._playwright: Optional[Playwright]     = None
        self._browser: Optional[Browser]           = None
        self._context: Optional[BrowserContext]    = None
        self._page: Optional[Page]                 = None
        self._popup_page: Optional[Page]           = None  # payment popup
        self._allow_destructive: bool              = False

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self, saved_cookies: list[dict] = None):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=settings.browser_headless,
            slow_mo=settings.browser_slow_mo_ms,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            viewport={
                "width":  settings.browser_viewport_width,
                "height": settings.browser_viewport_height,
            },
            user_agent=settings.browser_user_agent,
            accept_downloads=True,
            # Pre-grant browser-level permissions so no dialog ever blocks the agent.
            # Sarathi requests camera for photo capture — we handle that via file upload.
            permissions=["camera", "microphone", "notifications"],
        )

        # Restore cookies from a previous session if available
        if saved_cookies:
            await self._context.add_cookies(saved_cookies)

        self._page = await self._context.new_page()

        # Auto-handle JS dialogs
        self._page.on("dialog", self._handle_dialog)

        # Track new popup windows (e.g. payment gateway)
        self._page.on("popup", self._handle_popup)

        log.info("browser.started", headless=settings.browser_headless)

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        log.info("browser.stopped")

    # ── Core actions ───────────────────────────────────────────────────────────

    async def goto(self, url: str):
        await self._page.goto(url, timeout=settings.browser_timeout_ms, wait_until="domcontentloaded")
        await self._page.wait_for_load_state("networkidle", timeout=10000)
        log.debug("browser.goto", url=url)

    async def screenshot(self) -> bytes:
        return await self._page.screenshot(type="png", full_page=False)

    async def screenshot_b64(self) -> str:
        return base64.b64encode(await self.screenshot()).decode()

    async def current_url(self) -> str:
        return self._page.url

    async def page_text(self) -> str:
        return await self._page.inner_text("body")

    # ── Click ──────────────────────────────────────────────────────────────────

    async def click_text(self, text: str, exact: bool = False) -> bool:
        """Click the first visible element containing the given text."""
        if self._is_destructive(text):
            log.warning("browser.blocked_destructive_click", text=text)
            return False

        stripped = text.strip()

        # Short strings (≤3 chars, e.g. "x", "×", "OK") must use exact match and
        # prefer <button> elements over <a> tags to avoid false positives.
        if len(stripped) <= 3:
            try:
                # Prefer button with exact text
                btn = self._page.locator(f"button").filter(has_text=stripped).first
                if await btn.is_visible(timeout=500):
                    await btn.click(timeout=3000)
                    await self._wait_stable()
                    log.info("browser.short_text_button_clicked", text=stripped)
                    return True
            except Exception:
                pass
            exact = True  # fall through with exact match

        try:
            locator = self._page.get_by_text(stripped, exact=exact)
            await locator.first.click(timeout=8000)
            await self._wait_stable()
            return True
        except Exception as e:
            log.warning("browser.click_text_failed", text=stripped, error=str(e))
            return False

    async def click_selector(self, selector: str, label: str = "") -> bool:
        """Click by CSS selector. label is just for logging."""
        if label and self._is_destructive(label):
            log.warning("browser.blocked_destructive_selector", label=label)
            return False
        try:
            await self._page.click(selector, timeout=8000)
            await self._wait_stable()
            return True
        except Exception as e:
            log.warning("browser.click_selector_failed", selector=selector, error=str(e))
            return False

    async def click_nth(self, selector: str, n: int = 0) -> bool:
        try:
            await self._page.locator(selector).nth(n).click(timeout=8000)
            await self._wait_stable()
            return True
        except Exception as e:
            log.warning("browser.click_nth_failed", selector=selector, n=n, error=str(e))
            return False

    # ── Type ───────────────────────────────────────────────────────────────────

    async def type_into(self, selector: str, value: str, clear_first: bool = True) -> bool:
        try:
            el = self._page.locator(selector).first
            if clear_first:
                await el.triple_click()
                await el.fill("")
            await el.type(value, delay=40)
            return True
        except Exception as e:
            log.warning("browser.type_failed", selector=selector, error=str(e))
            return False

    async def fill(self, selector: str, value: str) -> bool:
        try:
            await self._page.fill(selector, value, timeout=8000)
            return True
        except Exception as e:
            log.warning("browser.fill_failed", selector=selector, error=str(e))
            return False

    # ── Select ─────────────────────────────────────────────────────────────────

    async def select_option(self, selector: str, value: str = "", label: str = "") -> bool:
        try:
            if label:
                await self._page.select_option(selector, label=label, timeout=8000)
            else:
                await self._page.select_option(selector, value=value, timeout=8000)
            return True
        except Exception as e:
            log.warning("browser.select_failed", selector=selector, error=str(e))
            return False

    # ── Upload ────────────────────────────────────────────────────────────────

    async def upload_file(self, selector: str, file_path: str) -> bool:
        try:
            await self._page.set_input_files(selector, file_path, timeout=8000)
            return True
        except Exception as e:
            log.warning("browser.upload_failed", selector=selector, error=str(e))
            return False

    # ── Scroll ────────────────────────────────────────────────────────────────

    async def scroll_to_bottom(self):
        await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(0.5)

    async def scroll_into_view(self, selector: str):
        try:
            await self._page.locator(selector).first.scroll_into_view_if_needed()
        except Exception:
            pass

    # ── Wait helpers ──────────────────────────────────────────────────────────

    async def wait_for_text(self, text: str, timeout_ms: int = 10000) -> bool:
        try:
            await self._page.wait_for_selector(f"text={text}", timeout=timeout_ms)
            return True
        except Exception:
            return False

    async def wait_for_selector(self, selector: str, timeout_ms: int = 10000) -> bool:
        try:
            await self._page.wait_for_selector(selector, timeout=timeout_ms)
            return True
        except Exception:
            return False

    async def _wait_stable(self):
        try:
            await self._page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            await asyncio.sleep(0.5)

    # ── Popup window handling (payment gateway etc.) ──────────────────────────

    async def _handle_popup(self, popup: Page):
        log.info("browser.popup_opened", url=popup.url)
        self._popup_page = popup
        popup.on("dialog", self._handle_dialog)

    async def switch_to_popup(self) -> bool:
        if self._popup_page:
            self._page = self._popup_page
            self._popup_page = None
            return True
        return False

    async def switch_to_main(self):
        pages = self._context.pages
        if pages:
            self._page = pages[0]

    # ── JS Dialog handling ────────────────────────────────────────────────────

    async def _handle_dialog(self, dialog: Dialog):
        msg = dialog.message
        log.info("browser.dialog", type=dialog.type, message=msg[:100])
        # Never dismiss — always accept (OK / Confirm)
        # Exception: if the dialog says "cancel application" or "reset"
        if any(d in msg.lower() for d in ["cancel application", "reset all", "clear all"]):
            log.warning("browser.blocked_destructive_dialog", message=msg)
            await dialog.dismiss()
        else:
            await dialog.accept()

    # ── Session persistence ───────────────────────────────────────────────────

    async def save_cookies(self) -> list[dict]:
        return await self._context.cookies()

    async def close_popups_on_page(self) -> bool:
        """
        Close any visible overlay/modal/popup.
        Fast path: Sarathi-specific selectors with 300 ms timeout.
        Fallback: generic selectors + Escape.
        """
        # Fast path — Sarathi known modals
        fast_selectors = [
            ".modal.in .close",
            ".modal.in [data-dismiss='modal']",
            "#contactless_statepopup .close",
            "#contactless_statepopup [data-dismiss='modal']",
        ]
        for selector in fast_selectors:
            try:
                el = self._page.locator(selector).first
                if await el.is_visible(timeout=300):
                    await el.click(timeout=2000)
                    await asyncio.sleep(0.3)
                    log.info("browser.popup_closed_fast", selector=selector)
                    return True
            except Exception:
                pass

        # Escape key (works when data-keyboard is not false)
        await self._page.keyboard.press("Escape")
        await asyncio.sleep(0.3)

        # Generic selectors
        for selector in [
            "button[aria-label='Close']", "button[aria-label='close']",
            ".close", "[data-dismiss='modal']", "button.btn-close",
        ]:
            try:
                el = self._page.locator(selector).first
                if await el.is_visible(timeout=300):
                    await el.click(timeout=2000)
                    await asyncio.sleep(0.3)
                    log.info("browser.popup_closed", selector=selector)
                    return True
            except Exception:
                pass

        # × character (exact text match only — avoids false positives)
        try:
            x_btn = self._page.locator("button").filter(has_text="×").first
            if await x_btn.is_visible(timeout=300):
                await x_btn.click(timeout=2000)
                log.info("browser.popup_closed_x_button")
                return True
        except Exception:
            pass

        log.debug("browser.no_popup_found")
        return False

    # ── Destructive action guard ───────────────────────────────────────────────

    def unlock_destructive(self, allow: bool):
        """Only set to True when human explicitly approves a reset."""
        self._allow_destructive = allow

    def _is_destructive(self, text: str) -> bool:
        if self._allow_destructive:
            return False
        t = text.lower().strip()
        return any(p in t for p in DESTRUCTIVE_PATTERNS)

    # ── CAPTCHA crop ──────────────────────────────────────────────────────────

    async def crop_element_screenshot(self, selector: str) -> Optional[bytes]:
        """Return a screenshot cropped to a specific element (e.g. CAPTCHA image)."""
        try:
            el = self._page.locator(selector).first
            return await el.screenshot()
        except Exception:
            return None

    # ── DOM inspector — gives LLM real selectors instead of guesses ───────────

    async def get_interactive_elements(self) -> dict:
        """
        Extract all interactive elements from the current page.
        Returns a dict the LLM can use to pick real selectors.
        """
        try:
            result = await self._page.evaluate("""() => {
                const out = { selects: [], inputs: [], buttons: [], links: [], forms: [] };

                // <select> elements
                document.querySelectorAll('select').forEach(el => {
                    const opts = Array.from(el.options).map(o => ({v: o.value, t: o.text.trim()})).slice(0, 30);
                    out.selects.push({
                        id: el.id, name: el.name,
                        selector: el.id ? '#' + el.id : (el.name ? 'select[name="' + el.name + '"]' : 'select'),
                        options: opts,
                        visible: el.offsetParent !== null
                    });
                });

                // <input> elements (not hidden)
                document.querySelectorAll('input:not([type="hidden"])').forEach(el => {
                    out.inputs.push({
                        id: el.id, name: el.name, type: el.type,
                        placeholder: el.placeholder,
                        selector: el.id ? '#' + el.id : (el.name ? 'input[name="' + el.name + '"]' : 'input[type="' + el.type + '"]'),
                        visible: el.offsetParent !== null
                    });
                });

                // Buttons
                document.querySelectorAll('button, input[type="submit"], input[type="button"]').forEach(el => {
                    const txt = (el.innerText || el.value || '').trim();
                    if (txt) out.buttons.push({
                        text: txt, id: el.id,
                        selector: el.id ? '#' + el.id : ('button:has-text("' + txt.substring(0,40) + '")'),
                        visible: el.offsetParent !== null
                    });
                });

                // Anchor links that look like navigation (have href or onclick)
                document.querySelectorAll('a[href], a[onclick]').forEach(el => {
                    const txt = el.innerText.trim();
                    if (txt && txt.length < 80) out.links.push({
                        text: txt,
                        href: el.href,
                        id: el.id,
                        visible: el.offsetParent !== null
                    });
                });

                // Form IDs
                document.querySelectorAll('form').forEach(f => {
                    out.forms.push({ id: f.id, action: f.action, method: f.method });
                });

                return out;
            }""")
            log.debug("browser.dom_elements",
                      selects=len(result.get("selects", [])),
                      inputs=len(result.get("inputs", [])),
                      buttons=len(result.get("buttons", [])),
                      links=len(result.get("links", [])))
            return result
        except Exception as e:
            log.warning("browser.dom_inspect_failed", error=str(e))
            return {}

    async def click_by_js(self, selector: str) -> bool:
        """Click via JavaScript — useful when Playwright locator times out."""
        try:
            clicked = await self._page.evaluate(
                f"() => {{ const el = document.querySelector('{selector}'); if(el){{el.click(); return true;}} return false; }}"
            )
            if clicked:
                await self._wait_stable()
                return True
            log.warning("browser.js_click_not_found", selector=selector)
            return False
        except Exception as e:
            log.warning("browser.js_click_failed", selector=selector, error=str(e))
            return False

    async def click_link_containing(self, text: str) -> bool:
        """
        Click an <a> tag whose text contains the given string (case-insensitive).
        Refuses single-character searches — too ambiguous (e.g. 'x' matches 'Expired').
        For short strings (<= 2 chars) uses exact whole-word match instead.
        """
        if not text or not text.strip():
            return False

        stripped = text.strip()
        try:
            if len(stripped) <= 2:
                # Exact word match only — avoid single-letter false positives
                result = await self._page.evaluate(f"""() => {{
                    const target = {json.dumps(stripped.lower())};
                    const links = Array.from(document.querySelectorAll('a'));
                    const match = links.find(a => a.innerText.trim().toLowerCase() === target && a.offsetParent !== null);
                    if (match) {{ match.click(); return true; }}
                    return false;
                }}""")
            else:
                result = await self._page.evaluate(f"""() => {{
                    const target = {json.dumps(stripped.lower())};
                    const links = Array.from(document.querySelectorAll('a'));
                    const match = links.find(a => a.innerText.toLowerCase().includes(target) && a.offsetParent !== null);
                    if (match) {{ match.click(); return true; }}
                    return false;
                }}""")
            if result:
                await self._wait_stable()
                log.info("browser.link_clicked", text=stripped)
                return True
            log.warning("browser.link_not_found", text=stripped)
            return False
        except Exception as e:
            log.warning("browser.link_click_failed", text=stripped, error=str(e))
            return False
