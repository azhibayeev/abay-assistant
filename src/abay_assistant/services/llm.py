"""Anthropic LLM клиент — реальная реализация через anthropic SDK."""

import asyncio
from typing import Any

import anthropic
from loguru import logger

from abay_assistant.config import get_settings

MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 1024
MAX_RETRIES = 3
RETRY_DELAYS = [2, 5, 10]  # секунды между попытками


class LLMClient:
    """Клиент для Anthropic Claude API."""

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=get_settings().anthropic_api_key
        )

    async def _retry(self, func, **kwargs) -> Any:
        """Вызов с retry при timeout/overloaded/rate-limit ошибках."""
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                return await func(**kwargs)
            except (
                anthropic.APITimeoutError,
                anthropic.RateLimitError,
                anthropic.InternalServerError,
                anthropic.APIConnectionError,
            ) as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[attempt]
                    logger.warning(
                        "LLM ошибка (попытка {}/{}): {}. Повтор через {}с",
                        attempt + 1, MAX_RETRIES, type(e).__name__, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error("LLM ошибка после {} попыток: {}", MAX_RETRIES, e)
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
