"""Tests for abay_assistant.services.web — URL safety and web operations."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from abay_assistant.services.web import _is_safe_url, web_fetch


def test_is_safe_url_valid():
    assert _is_safe_url("https://example.com") is None
    assert _is_safe_url("http://example.com/path") is None


def test_is_safe_url_localhost():
    error = _is_safe_url("http://localhost:8080")
    assert error is not None
    assert "локальн" in error.lower() or "запрещён" in error.lower()


def test_is_safe_url_private_ip():
    error = _is_safe_url("http://192.168.1.1")
    assert error is not None
    assert "запрещён" in error.lower() or "внутренн" in error.lower()


def test_is_safe_url_loopback():
    error = _is_safe_url("http://127.0.0.1:3000")
    assert error is not None


def test_is_safe_url_bad_scheme():
    error = _is_safe_url("ftp://files.example.com")
    assert error is not None
    assert "схем" in error.lower()

    error = _is_safe_url("file:///etc/passwd")
    assert error is not None


async def test_web_fetch_blocked():
    result = await web_fetch("http://localhost:9090/secret")
    assert "Отклонено" in result


async def test_web_fetch_success():
    mock_response = MagicMock()
    mock_response.text = "<html><body><p>Hello World</p></body></html>"
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("abay_assistant.services.web.httpx.AsyncClient", return_value=mock_client):
        result = await web_fetch("https://example.com")
        assert "Hello World" in result
