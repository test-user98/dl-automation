"""
Unified LLM client with primary/fallback support.

Default: OpenAI (GPT-4o) as primary, Anthropic (Claude) as fallback.
Toggle via .env:
    LLM_PRIMARY=openai        # primary provider
    LLM_FALLBACK=anthropic    # fallback when primary fails or is paused
    LLM_PRIMARY_PAUSED=false  # set true to skip primary and always use fallback

If the primary call fails (any exception), the client automatically retries
with the fallback provider and logs which one was used.
"""

import structlog
from abc import ABC, abstractmethod
from typing import Optional
from config.settings import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()


class LLMClient(ABC):
    """Base interface — all providers implement vision() and text()."""

    @abstractmethod
    async def vision(self, image_bytes: bytes, system_prompt: str, user_text: str, detail: str = "high") -> str: ...

    @abstractmethod
    async def text(self, system_prompt: str, user_text: str) -> str: ...


# ── OpenAI (GPT-4o) ────────────────────────────────────────────────────────────

class OpenAIClient(LLMClient):
    def __init__(self):
        import base64 as _b64
        from openai import OpenAI
        self._b64    = _b64
        self._client = OpenAI(api_key=settings.openai_api_key)
        self._model  = settings.resolved_model_for("openai")
        log.info("llm.provider_ready", provider="openai", model=self._model)

    async def vision(self, image_bytes: bytes, system_prompt: str, user_text: str, detail: str = "high") -> str:
        b64 = self._b64.b64encode(image_bytes).decode()
        response = self._client.chat.completions.create(
            model      = self._model,
            max_tokens = settings.llm_max_tokens,
            temperature= settings.llm_temperature,
            messages   = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url":    f"data:image/png;base64,{b64}",
                                "detail": detail,
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                },
            ],
        )
        return response.choices[0].message.content.strip()

    async def text(self, system_prompt: str, user_text: str) -> str:
        response = self._client.chat.completions.create(
            model      = self._model,
            max_tokens = settings.llm_max_tokens,
            temperature= settings.llm_temperature,
            messages   = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
        )
        return response.choices[0].message.content.strip()


# ── Anthropic (Claude) ─────────────────────────────────────────────────────────

class AnthropicClient(LLMClient):
    def __init__(self):
        import base64 as _b64
        import anthropic
        self._b64    = _b64
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._model  = settings.resolved_model_for("anthropic")
        log.info("llm.provider_ready", provider="anthropic", model=self._model)

    async def vision(self, image_bytes: bytes, system_prompt: str, user_text: str, detail: str = "high") -> str:
        # Anthropic auto-manages image resolution; `detail` is accepted for a
        # uniform interface but only affects the OpenAI provider.
        b64 = self._b64.b64encode(image_bytes).decode()
        response = self._client.messages.create(
            model      = self._model,
            max_tokens = settings.llm_max_tokens,
            temperature= settings.llm_temperature,
            system     = system_prompt,
            messages   = [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type":       "base64",
                            "media_type": "image/png",
                            "data":       b64,
                        },
                    },
                    {"type": "text", "text": user_text},
                ],
            }],
        )
        return response.content[0].text.strip()

    async def text(self, system_prompt: str, user_text: str) -> str:
        response = self._client.messages.create(
            model      = self._model,
            max_tokens = settings.llm_max_tokens,
            temperature= settings.llm_temperature,
            system     = system_prompt,
            messages   = [{"role": "user", "content": user_text}],
        )
        return response.content[0].text.strip()


# ── Primary + Fallback wrapper ─────────────────────────────────────────────────

class LLMWithFallback(LLMClient):
    """
    Tries the primary provider first.
    If it raises any exception OR LLM_PRIMARY_PAUSED=true, uses the fallback.
    Logs which provider was actually used on every call.
    """

    def __init__(self, primary: LLMClient, fallback: Optional[LLMClient], primary_name: str, fallback_name: str):
        self._primary      = primary
        self._fallback     = fallback
        self._primary_name = primary_name
        self._fallback_name= fallback_name

    async def vision(self, image_bytes: bytes, system_prompt: str, user_text: str, detail: str = "high") -> str:
        if settings.llm_primary_paused:
            return await self._use_fallback("vision", image_bytes, system_prompt, user_text, detail)
        try:
            result = await self._primary.vision(image_bytes, system_prompt, user_text, detail)
            log.debug("llm.used", provider=self._primary_name)
            return result
        except Exception as e:
            log.warning("llm.primary_failed", provider=self._primary_name, error=str(e))
            return await self._use_fallback("vision", image_bytes, system_prompt, user_text, detail)

    async def text(self, system_prompt: str, user_text: str) -> str:
        if settings.llm_primary_paused:
            return await self._use_fallback("text", None, system_prompt, user_text)
        try:
            result = await self._primary.text(system_prompt, user_text)
            log.debug("llm.used", provider=self._primary_name)
            return result
        except Exception as e:
            log.warning("llm.primary_failed", provider=self._primary_name, error=str(e))
            return await self._use_fallback("text", None, system_prompt, user_text)

    async def _use_fallback(self, method: str, image_bytes, system_prompt: str, user_text: str, detail: str = "high") -> str:
        if not self._fallback:
            raise RuntimeError(f"Primary LLM ({self._primary_name}) failed and no fallback configured")
        log.info("llm.using_fallback", provider=self._fallback_name)
        if method == "vision":
            return await self._fallback.vision(image_bytes, system_prompt, user_text, detail)
        return await self._fallback.text(system_prompt, user_text)


# ── Factory ────────────────────────────────────────────────────────────────────

_client: Optional[LLMClient] = None


def _make_provider(name: str) -> Optional[LLMClient]:
    if name == "openai":
        if not settings.openai_api_key:
            log.warning("llm.no_key", provider="openai")
            return None
        return OpenAIClient()
    elif name == "anthropic":
        if not settings.anthropic_api_key:
            log.warning("llm.no_key", provider="anthropic")
            return None
        return AnthropicClient()
    return None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        primary  = _make_provider(settings.llm_primary)
        fallback = _make_provider(settings.llm_fallback) if settings.llm_fallback else None

        if not primary and not fallback:
            raise RuntimeError("No LLM provider configured — set OPENAI_API_KEY or ANTHROPIC_API_KEY in .env")

        if not primary:
            # If primary has no key, use fallback as sole provider
            log.info("llm.primary_unavailable_using_fallback", fallback=settings.llm_fallback)
            _client = fallback
        else:
            _client = LLMWithFallback(
                primary       = primary,
                fallback      = fallback,
                primary_name  = settings.llm_primary,
                fallback_name = settings.llm_fallback or "none",
            )

    return _client
