"""
CAPTCHA solving tool — multi-provider with automatic fallback chain.

Solve order (all tried before giving up):
  1. Primary LLM vision  (claude or gpt4o, whichever is configured as primary)
  2. Fallback LLM vision (the other one)
  3. 2captcha / capsolver (if CAPTCHA_API_KEY is set)
  4. Manual              (ask human — last resort)

No single provider failure breaks the flow.
Each LLM provider is retried once before moving to the next.
"""

import asyncio
import base64
import httpx
import structlog
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config.settings import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()

# Words that mean the LLM refused instead of reading the CAPTCHA
_REFUSAL_WORDS = (
    "sorry", "can't", "cannot", "assist", "unable",
    "help", "i'm", "describe", "appear", "understand",
    "apolog", "not able",
)


def _normalize_captcha(text: str) -> str:
    """Keep only alphanumeric CAPTCHA characters from model/manual output."""
    return "".join(ch for ch in (text or "").strip() if ch.isalnum())


def _is_valid_captcha(text: str) -> bool:
    """Return True only for short alphanumeric strings (≤12 chars, no refusal words)."""
    normalized = _normalize_captcha(text)
    if len(normalized) < 4 or len(normalized) > 8:
        return False
    lower = text.lower()
    if any(w in lower for w in _REFUSAL_WORDS):
        return False
    return True


@dataclass
class CaptchaResult:
    text: str = ""
    confidence: float = 0.0
    provider: str = ""
    attempts: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.text)


class CaptchaSolver:

    async def solve(
        self,
        image_bytes: bytes,
        *,
        force_manual: bool = False,
        allow_manual: bool = True,
        prompt_context: str = "",
        job=None,
        human_loop=None,
    ) -> Optional[str]:
        """
        Try every available provider in order until we get a valid answer.
        Returns the CAPTCHA text or None if all providers fail.

        When `job` and `human_loop` are provided AND the solver falls back to
        the manual path, the CAPTCHA image is surfaced to the customer UI via
        `human_loop.ask(action_type="captcha")` instead of stdin/file. The
        stdin/file path is kept as a backup so `run_agent.py` terminal mode
        still works.
        """
        provider = settings.captcha_provider

        if force_manual or provider == "manual":
            return await self._solve_manual(
                image_bytes, prompt_context, job=job, human_loop=human_loop,
            )

        # Paid services — reliable, use directly with retries
        if provider in ("2captcha", "capsolver"):
            for attempt in range(settings.captcha_max_retries):
                result = await self._solve_once(image_bytes, provider)
                if result and _is_valid_captcha(result):
                    result = _normalize_captcha(result)
                    log.info("captcha.solved", provider=provider, attempt=attempt + 1)
                    return result
                log.warning("captcha.attempt_failed", provider=provider, attempt=attempt + 1)
                await asyncio.sleep(2)
            return await self._solve_manual(
                image_bytes, prompt_context, job=job, human_loop=human_loop,
            ) if allow_manual else None

        # LLM vision — build the fallback chain based on configured primary/fallback
        llm_chain = self._build_llm_chain(provider)

        for llm_provider in llm_chain:
            for attempt in range(2):  # 2 tries per LLM provider
                result = await self._solve_llm(image_bytes, llm_provider)
                if result and _is_valid_captcha(result):
                    result = _normalize_captcha(result)
                    log.info("captcha.solved", provider=llm_provider, attempt=attempt + 1)
                    return result
                log.warning(
                    "captcha.llm_attempt_failed",
                    provider=llm_provider,
                    attempt=attempt + 1,
                    preview=(result or "")[:40],
                )
                await asyncio.sleep(0.5)

        # All LLM providers exhausted — try paid service if key available
        if settings.captcha_api_key:
            fallback_paid = "2captcha"
            log.warning("captcha.falling_back_to_paid", provider=fallback_paid)
            result = await self._solve_once(image_bytes, fallback_paid)
            if result and _is_valid_captcha(result):
                result = _normalize_captcha(result)
                log.info("captcha.solved_via_paid_fallback", provider=fallback_paid)
                return result

        if allow_manual:
            return await self._solve_manual(
                image_bytes, prompt_context, job=job, human_loop=human_loop,
            )

        log.error("captcha.all_providers_failed")
        return None

    async def solve_with_confidence(
        self,
        image_bytes: bytes,
        *,
        force_manual: bool = False,
        allow_manual: bool = True,
        prompt_context: str = "",
        job=None,
        human_loop=None,
    ) -> CaptchaResult:
        """
        Return a CAPTCHA solution with a confidence estimate.

        LLM OCR needs agreement from repeated reads before it is considered
        high confidence. Low-confidence guesses are returned to the caller so
        the page can refresh the CAPTCHA instead of submitting weak guesses.

        When `job` and `human_loop` are provided AND the manual fallback fires,
        the CAPTCHA image is surfaced to the customer UI via
        `human_loop.ask(action_type="captcha")` instead of stdin/file.
        """
        provider = settings.captcha_provider

        if force_manual or provider == "manual":
            text = await self._solve_manual(
                image_bytes, prompt_context, job=job, human_loop=human_loop,
            )
            text = _normalize_captcha(text or "")
            if _is_valid_captcha(text):
                return CaptchaResult(text=text, confidence=1.0, provider="manual")
            return CaptchaResult(provider="manual")

        if provider in ("2captcha", "capsolver"):
            for attempt in range(settings.captcha_max_retries):
                result = await self._solve_once(image_bytes, provider)
                if result and _is_valid_captcha(result):
                    result = _normalize_captcha(result)
                    log.info("captcha.solved", provider=provider, attempt=attempt + 1)
                    return CaptchaResult(
                        text=result,
                        confidence=0.95,
                        provider=provider,
                        attempts=[{"provider": provider, "attempt": attempt + 1, "text": result}],
                    )
                log.warning("captcha.attempt_failed", provider=provider, attempt=attempt + 1)
                await asyncio.sleep(2)
            if allow_manual:
                return await self.solve_with_confidence(
                    image_bytes,
                    force_manual=True,
                    prompt_context=prompt_context,
                    job=job,
                    human_loop=human_loop,
                )
            return CaptchaResult(provider=provider)

        llm_chain = self._build_llm_chain(provider)
        attempts: list[dict] = []

        for llm_provider in llm_chain:
            for attempt in range(2):
                result = await self._solve_llm(image_bytes, llm_provider)
                if result and _is_valid_captcha(result):
                    result = _normalize_captcha(result)
                    attempts.append({
                        "provider": llm_provider,
                        "attempt": attempt + 1,
                        "text": result,
                    })
                    best = self._best_llm_result(attempts)
                    log.info(
                        "captcha.llm_candidate",
                        provider=llm_provider,
                        attempt=attempt + 1,
                        text=result,
                        best=best.text,
                        confidence=best.confidence,
                    )
                    if best.confidence >= settings.captcha_confidence_threshold:
                        log.info(
                            "captcha.solved",
                            provider=best.provider,
                            confidence=best.confidence,
                            attempts=len(attempts),
                        )
                        return best
                else:
                    log.warning(
                        "captcha.llm_attempt_failed",
                        provider=llm_provider,
                        attempt=attempt + 1,
                        preview=(result or "")[:40],
                    )
                await asyncio.sleep(0.5)

        best = self._best_llm_result(attempts)
        if best.ok:
            log.warning(
                "captcha.low_confidence",
                text=best.text,
                confidence=best.confidence,
                threshold=settings.captcha_confidence_threshold,
                attempts=attempts,
            )

        if settings.captcha_api_key:
            fallback_paid = "2captcha"
            log.warning("captcha.falling_back_to_paid", provider=fallback_paid)
            result = await self._solve_once(image_bytes, fallback_paid)
            if result and _is_valid_captcha(result):
                result = _normalize_captcha(result)
                log.info("captcha.solved_via_paid_fallback", provider=fallback_paid)
                return CaptchaResult(
                    text=result,
                    confidence=0.95,
                    provider=fallback_paid,
                    attempts=attempts + [{"provider": fallback_paid, "attempt": 1, "text": result}],
                )

        if allow_manual:
            manual = await self.solve_with_confidence(
                image_bytes,
                force_manual=True,
                prompt_context=prompt_context,
                job=job,
                human_loop=human_loop,
            )
            manual.attempts = attempts + manual.attempts
            return manual

        log.error("captcha.all_providers_failed")
        return best if best.ok else CaptchaResult(provider="none", attempts=attempts)

    def _best_llm_result(self, attempts: list[dict]) -> CaptchaResult:
        valid = [
            {**a, "text": _normalize_captcha(a.get("text", ""))}
            for a in attempts
            if _is_valid_captcha(a.get("text", ""))
        ]
        if not valid:
            return CaptchaResult(provider="llm", attempts=attempts)

        counts = Counter(a["text"] for a in valid)
        best_text, best_count = counts.most_common(1)[0]
        providers = sorted({a.get("provider", "llm") for a in valid if a["text"] == best_text})

        if best_count >= 3:
            confidence = 0.97
        elif best_count >= 2 or len(providers) >= 2:
            confidence = 0.90
        else:
            confidence = 0.65

        return CaptchaResult(
            text=best_text,
            confidence=confidence,
            provider="+".join(providers) if providers else "llm",
            attempts=attempts,
        )

    def _build_llm_chain(self, configured: str) -> list[str]:
        """
        Build ordered list of LLM providers to try.
        claude/gpt4v config sets the primary; the other is tried as automatic fallback.
        """
        has_anthropic = bool(settings.anthropic_api_key)
        has_openai    = bool(settings.openai_api_key)

        if configured == "claude" or settings.llm_primary == "anthropic":
            chain = (["claude"] if has_anthropic else []) + (["gpt4v"] if has_openai else [])
        elif configured == "gpt4v" or settings.llm_primary == "openai":
            chain = (["gpt4v"] if has_openai else []) + (["claude"] if has_anthropic else [])
        else:
            # default: whatever is available
            chain = (["claude"] if has_anthropic else []) + (["gpt4v"] if has_openai else [])

        if not chain:
            log.warning("captcha.no_llm_api_keys_configured")
        return chain

    async def _solve_once(self, image_bytes: bytes, provider: str) -> Optional[str]:
        if provider == "2captcha":
            return await self._solve_2captcha(image_bytes)
        elif provider == "capsolver":
            return await self._solve_capsolver(image_bytes)
        elif provider == "claude":
            return await self._solve_llm(image_bytes, "claude")
        elif provider == "gpt4v":
            return await self._solve_llm(image_bytes, "gpt4v")
        else:
            return None

    # ── LLM dispatcher ────────────────────────────────────────────────────────

    async def _solve_llm(self, image_bytes: bytes, provider: str) -> Optional[str]:
        if provider == "claude":
            return await self._solve_claude(image_bytes)
        elif provider == "gpt4v":
            return await self._solve_gpt4v(image_bytes)
        return None

    # ── Claude Vision ─────────────────────────────────────────────────────────

    async def _solve_claude(self, image_bytes: bytes) -> Optional[str]:
        if not settings.anthropic_api_key:
            return None
        try:
            from agent.llm_client import get_llm_client
            llm = get_llm_client()
            system = (
                "You are a CAPTCHA transcription tool. "
                "Output ONLY the exact characters visible in the image. "
                "No explanation, no apology, no punctuation — characters only."
            )
            user = (
                "Transcribe the CAPTCHA text in this image. "
                "It is 4-8 alphanumeric characters. "
                "Reply with ONLY those characters, nothing else."
            )
            result = (await llm.vision(image_bytes, system, user)).strip()
            log.info("captcha.claude_response", preview=result[:20], chars=len(result))
            return result if result else None
        except Exception as e:
            log.error("captcha.claude_failed", error=str(e))
            return None

    # ── GPT-4o Vision (OpenAI) ────────────────────────────────────────────────

    async def _solve_gpt4v(self, image_bytes: bytes) -> Optional[str]:
        if not settings.openai_api_key:
            return None
        try:
            import openai
            b64 = base64.b64encode(image_bytes).decode()
            client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
            response = await client.chat.completions.create(
                model="gpt-4o",
                max_tokens=20,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "This is a CAPTCHA image. "
                                    "Output ONLY the characters shown (4-8 alphanumeric chars). "
                                    "Nothing else."
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{b64}"},
                            },
                        ],
                    }
                ],
            )
            result = response.choices[0].message.content.strip()
            log.info("captcha.gpt4v_response", preview=result[:20], chars=len(result))
            return result if result else None
        except Exception as e:
            log.error("captcha.gpt4v_failed", error=str(e))
            return None

    # ── 2captcha ──────────────────────────────────────────────────────────────

    async def _solve_2captcha(self, image_bytes: bytes) -> Optional[str]:
        b64 = base64.b64encode(image_bytes).decode()
        api_key = settings.captcha_api_key
        timeout = settings.captcha_timeout_seconds

        async with httpx.AsyncClient(timeout=timeout + 10) as client:
            resp = await client.post(
                "http://2captcha.com/in.php",
                data={"key": api_key, "method": "base64", "body": b64, "json": 1},
            )
            data = resp.json()
            if data.get("status") != 1:
                log.error("captcha.2captcha_submit_failed", response=data)
                return None

            captcha_id = data["request"]
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(5)
                poll = await client.get(
                    f"http://2captcha.com/res.php?key={api_key}&action=get&id={captcha_id}&json=1"
                )
                pd = poll.json()
                if pd.get("status") == 1:
                    return pd["request"]
                if pd.get("request") == "ERROR_CAPTCHA_UNSOLVABLE":
                    return None
        return None

    # ── CapSolver ─────────────────────────────────────────────────────────────

    async def _solve_capsolver(self, image_bytes: bytes) -> Optional[str]:
        b64 = base64.b64encode(image_bytes).decode()
        api_key = settings.captcha_api_key
        timeout = settings.captcha_timeout_seconds

        async with httpx.AsyncClient(timeout=timeout + 10) as client:
            resp = await client.post(
                "https://api.capsolver.com/createTask",
                json={
                    "clientKey": api_key,
                    "task": {"type": "ImageToTextTask", "body": b64},
                },
            )
            task_id = resp.json().get("taskId")
            if not task_id:
                return None

            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(3)
                poll = await client.post(
                    "https://api.capsolver.com/getTaskResult",
                    json={"clientKey": api_key, "taskId": task_id},
                )
                pd = poll.json()
                if pd.get("status") == "ready":
                    return pd.get("solution", {}).get("text")
        return None

    # ── Manual human challenge fallback ──────────────────────────────────────

    async def _solve_manual(
        self,
        image_bytes: bytes,
        prompt_context: str = "",
        *,
        job=None,
        human_loop=None,
    ) -> Optional[str]:
        """
        Last-resort human-in-the-loop CAPTCHA handling.

        The image is saved to data/latest_captcha.png in all cases (debug
        artifact, also the fallback if the customer-UI ask fails).

        Order of preference:
          1. `human_loop.ask` with the CAPTCHA image embedded — customer types
             the value in the web UI. Used whenever both `job` and `human_loop`
             are provided (production path).
          2. stdin / data/manual_captcha.txt — legacy terminal-mode path,
             still used by run_agent.py and during tests without a job.
        """
        data_dir = Path("data")
        data_dir.mkdir(parents=True, exist_ok=True)
        image_path = (data_dir / "latest_captcha.png").resolve()
        answer_path = (data_dir / "manual_captcha.txt").resolve()
        image_path.write_bytes(image_bytes)
        try:
            answer_path.unlink()
        except FileNotFoundError:
            pass

        log.warning(
            "captcha.manual_needed",
            image_path=str(image_path),
            answer_file=str(answer_path),
            context=prompt_context,
            via="human_loop" if (job is not None and human_loop is not None) else "stdin",
        )

        # Production path — ask the customer through the web UI.
        if job is not None and human_loop is not None:
            try:
                response = await human_loop.ask(
                    job=job,
                    step_name="captcha_manual",
                    question=(
                        "Help us read the security code shown on the government "
                        "portal. Type the characters you see in the image."
                    ),
                    context=prompt_context or (
                        "The portal showed a CAPTCHA we couldn't read automatically. "
                        "Type the exact characters you see — letters and numbers, "
                        "case-sensitive."
                    ),
                    screenshot=image_bytes,
                    options=[],
                    timeout_seconds=settings.captcha_manual_timeout_seconds,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("captcha.manual_human_loop_failed", error=str(e))
                response = None

            if response is not None:
                raw = (response.answer or "").strip()
                if raw and raw != "__timeout__":
                    value = _normalize_captcha(raw)
                    if _is_valid_captcha(value):
                        log.info(
                            "captcha.manual_received",
                            source="human_loop",
                            chars=len(value),
                        )
                        return value
                    log.warning(
                        "captcha.manual_invalid",
                        source="human_loop",
                        preview=raw[:20],
                    )
            # Fall through to stdin so an operator can still rescue the run.

        print("\n" + "=" * 60, flush=True)
        print("  CAPTCHA NEEDS HUMAN", flush=True)
        print("=" * 60, flush=True)
        if prompt_context:
            print(prompt_context, flush=True)
        print(f"Image saved at: {image_path}", flush=True)
        print(f"You can also write the answer to: {answer_path}", flush=True)

        terminal_closed = False
        input_task = asyncio.create_task(asyncio.to_thread(input, "Enter CAPTCHA: "))
        deadline = asyncio.get_event_loop().time() + max(
            60,
            settings.captcha_manual_timeout_seconds,
        )

        try:
            while asyncio.get_event_loop().time() < deadline:
                if input_task.done():
                    try:
                        raw = input_task.result()
                    except EOFError:
                        terminal_closed = True
                        raw = ""
                    except Exception:
                        raw = ""
                    value = _normalize_captcha(raw)
                    if _is_valid_captcha(value):
                        log.info("captcha.manual_received", source="terminal", chars=len(value))
                        return value
                    if raw.strip():
                        log.warning(
                            "captcha.manual_invalid",
                            source="terminal",
                            preview=raw[:20],
                        )
                    if not terminal_closed:
                        input_task = asyncio.create_task(
                            asyncio.to_thread(input, "Enter CAPTCHA: ")
                        )

                if answer_path.exists():
                    raw = answer_path.read_text(encoding="utf-8").strip()
                    try:
                        answer_path.unlink()
                    except FileNotFoundError:
                        pass
                    value = _normalize_captcha(raw)
                    if _is_valid_captcha(value):
                        log.info("captcha.manual_received", source="file", chars=len(value))
                        return value
                    log.warning("captcha.manual_invalid", source="file", preview=raw[:20])

                await asyncio.sleep(0.5)
        finally:
            if not input_task.done():
                input_task.cancel()

        log.error("captcha.manual_timeout", seconds=settings.captcha_manual_timeout_seconds)
        return None
