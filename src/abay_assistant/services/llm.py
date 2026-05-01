"""Anthropic LLM клиент — реальная реализация через anthropic SDK."""

import asyncio
from typing import Any

import anthropic
import httpx
from loguru import logger

from abay_assistant.config import get_settings

MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 1024
MAX_RETRIES = 3
RETRY_DELAYS = [2, 5, 10]  # секунды между попытками (timeout/connection)
RATE_LIMIT_RETRIES = 5
RATE_LIMIT_DELAYS = [5, 15, 30, 60, 120]  # для 429 — дольше и больше попыток


class LLMClient:
    """Клиент для Anthropic Claude API."""

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=get_settings().anthropic_api_key,
            timeout=httpx.Timeout(120.0, connect=10.0),
        )

    async def _retry(self, func, **kwargs) -> Any:
        """Вызов с retry. Rate limit получает больше попыток и длиннее паузы."""
        last_error = None
        max_attempts = max(MAX_RETRIES, RATE_LIMIT_RETRIES)

        for attempt in range(max_attempts):
            try:
                return await func(**kwargs)
            except anthropic.RateLimitError as e:
                last_error = e
                if attempt < RATE_LIMIT_RETRIES - 1:
                    delay = RATE_LIMIT_DELAYS[min(attempt, len(RATE_LIMIT_DELAYS) - 1)]
                    logger.warning(
                        "LLM rate limit (попытка {}/{}): {}. Повтор через {}с",
                        attempt + 1, RATE_LIMIT_RETRIES, type(e).__name__, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error("LLM rate limit после {} попыток: {}", RATE_LIMIT_RETRIES, e)
                    raise
            except (
                anthropic.APITimeoutError,
                anthropic.InternalServerError,
                anthropic.APIConnectionError,
            ) as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    logger.warning(
                        "LLM ошибка (попытка {}/{}): {}. Повтор через {}с",
                        attempt + 1, MAX_RETRIES, type(e).__name__, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error("LLM ошибка после {} попыток: {}", MAX_RETRIES, e)
                    raise
        raise last_error

    async def chat(
        self,
        messages: list[dict],
        system: str = "",
        max_tokens: int | None = None,
    ) -> str:
        """Простой вызов Claude. Возвращает текстовый ответ."""
        kwargs: dict[str, Any] = {
            "model": MODEL,
            "max_tokens": max_tokens or MAX_TOKENS,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        resp = await self._retry(self._client.messages.create, **kwargs)
        text = resp.content[0].text if resp.content else ""
        logger.debug("LLM chat: {} input / {} output tokens",
                      resp.usage.input_tokens, resp.usage.output_tokens)
        return text

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str = "",
        max_tokens: int | None = None,
    ) -> anthropic.types.Message:
        """Вызов Claude с tool use. Возвращает полный Message объект."""
        kwargs: dict[str, Any] = {
            "model": MODEL,
            "max_tokens": max_tokens or 4096,
            "messages": messages,
            "tools": tools,
        }
        if system:
            kwargs["system"] = system

        resp = await self._retry(self._client.messages.create, **kwargs)
        logger.debug(
            "LLM chat_with_tools: {} input / {} output tokens, stop={}",
            resp.usage.input_tokens,
            resp.usage.output_tokens,
            resp.stop_reason,
        )
        return resp
