"""Tests for abay_assistant.bot.handlers — markdown conversion and helpers."""

import pytest

from abay_assistant.bot.handlers import _md_to_html, _serialize_content, _detect_unlinked_crm_mentions


def test_md_to_html_bold():
    assert _md_to_html("**bold**") == "<b>bold</b>"
    assert _md_to_html("text **bold** text") == "text <b>bold</b> text"


def test_md_to_html_italic():
    assert _md_to_html("*italic*") == "<i>italic</i>"


def test_md_to_html_code():
    assert _md_to_html("`code`") == "<code>code</code>"
    assert _md_to_html("run `pip install`") == "run <code>pip install</code>"


def test_md_to_html_escapes_html():
    result = _md_to_html("<script>alert('xss')</script>")
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_md_to_html_combined():
    result = _md_to_html("**bold** and *italic* and `code`")
    assert "<b>bold</b>" in result
    assert "<i>italic</i>" in result
    assert "<code>code</code>" in result


def test_serialize_content():
    class TextBlock:
        type = "text"
        text = "Hello"

    class ToolBlock:
        type = "tool_use"
        id = "tool_123"
        name = "web_search"
        input = {"query": "test"}

    result = _serialize_content([TextBlock(), ToolBlock()])
    assert len(result) == 2
    assert result[0] == {"type": "text", "text": "Hello"}
    assert result[1]["type"] == "tool_use"
    assert result[1]["id"] == "tool_123"
    assert result[1]["name"] == "web_search"


# ─────────────────────────────────────────────
# _detect_unlinked_crm_mentions
# ─────────────────────────────────────────────

def test_detect_unlinked_found():
    """Detect CRM names mentioned by user but not saved by LLM."""
    crm_names = ["Расул", "DMS проект"]
    user_text = "Обсудил с Расул запуск DMS проект"
    llm_response = "Создал задачу по запуску."
    result = _detect_unlinked_crm_mentions(user_text, crm_names, llm_response)
    assert "Расул" in result
    assert "DMS проект" in result


def test_detect_unlinked_already_saved():
    """If LLM response indicates saving, don't suggest."""
    crm_names = ["Расул"]
    user_text = "Встретился с Расул"
    llm_response = "Записано в CRM: встреча с Расулом."
    result = _detect_unlinked_crm_mentions(user_text, crm_names, llm_response)
    assert len(result) == 0


def test_detect_unlinked_short_name_skipped():
    """Names shorter than 4 chars should be skipped."""
    crm_names = ["Ан", "Бо"]
    user_text = "Встреча с Ан и Бо"
    llm_response = "ОК"
    result = _detect_unlinked_crm_mentions(user_text, crm_names, llm_response)
    assert len(result) == 0


def test_detect_unlinked_empty_crm():
    """Empty CRM list should return empty."""
    result = _detect_unlinked_crm_mentions("текст", [], "ответ")
    assert result == []


def test_detect_unlinked_name_not_in_text():
    """CRM names not mentioned by user should not be returned."""
    crm_names = ["Расул", "Алмас"]
    user_text = "Обсудил проект"
    llm_response = "ОК"
    result = _detect_unlinked_crm_mentions(user_text, crm_names, llm_response)
    assert len(result) == 0
