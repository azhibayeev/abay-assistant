"""Веб-поиск и загрузка страниц через DuckDuckGo HTML."""

import ipaddress
import re
from urllib.parse import urlparse

import httpx
from loguru import logger

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AbayBot/1.0)",
}

_TIMEOUT = 10.0

# Блокируемые хосты
_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]"}


def _is_safe_url(url: str) -> str | None:
    """Проверить URL на безопасность. Возвращает ошибку или None если ОК."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "Невалидный URL"

    if parsed.scheme not in ("http", "https"):
        return f"Недопустимая схема: {parsed.scheme}. Разрешены только http/https."

    host = parsed.hostname or ""
    if host in _BLOCKED_HOSTS:
        return "Доступ к локальным адресам запрещён."

    # Проверить IP-адреса
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_reserved:
            return "Доступ к внутренним IP запрещён."
    except ValueError:
        pass  # Это hostname, не IP — ОК

    return None


async def web_search(query: str) -> list[dict]:
    """Поиск через DuckDuckGo HTML. Возвращает список {title, url, snippet}."""
    url = "https://html.duckduckgo.com/html/"
    data = {"q": query}

    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.post(url, data=data)
            resp.raise_for_status()
    except Exception as e:
        logger.error("web_search ошибка: {}", e)
        return [{"error": str(e)}]

    html = resp.text
    results = []

    # Парсим результаты из HTML
    # DuckDuckGo HTML формат: <a class="result__a" href="...">title</a>
    # <a class="result__snippet">snippet</a>
    blocks = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        html,
        re.DOTALL,
    )

    for link, title, snippet in blocks[:5]:
        # Очистить от HTML тегов
        title_clean = re.sub(r"<[^>]+>", "", title).strip()
        snippet_clean = re.sub(r"<[^>]+>", "", snippet).strip()

        # DuckDuckGo редиректы: //duckduckgo.com/l/?uddg=ENCODED_URL
        if "uddg=" in link:
            match = re.search(r"uddg=([^&]+)", link)
            if match:
                from urllib.parse import unquote
                link = unquote(match.group(1))

        results.append({
            "title": title_clean,
            "url": link,
            "snippet": snippet_clean,
        })

    logger.info("web_search '{}': {} результатов", query, len(results))
    return results if results else [{"message": "Ничего не найдено"}]


async def web_fetch(url: str) -> str:
    """Загрузить страницу и вернуть очищенный текст (max 4000 символов)."""
    error = _is_safe_url(url)
    if error:
        return f"Отклонено: {error}"

    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as e:
        logger.error("web_fetch ошибка для {}: {}", url, e)
        return f"Ошибка загрузки: {e}"

    html = resp.text

    # Убрать script и style блоки
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # Убрать HTML теги
    text = re.sub(r"<[^>]+>", " ", text)

    # Убрать лишние пробелы
    text = re.sub(r"\s+", " ", text).strip()

    # HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&nbsp;", " ")

    if len(text) > 4000:
        text = text[:4000] + "…"

    logger.info("web_fetch '{}': {} символов", url, len(text))
    return text
