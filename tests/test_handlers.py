"""Tests for abay_assistant.bot.handlers — markdown conversion and helpers."""

import pytest

from abay_assistant.bot.handlers import (
    _md_to_html,
    _serialize_content,
    _detect_unlinked_crm_mentions,
    _split_message,
    _truncate_tool_results,
    _build_actions_summary,
    _describe_tool_action,
)


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


# ─────────────────────────────────────────────
# _split_message
# ─────────────────────────────────────────────

def test_split_message_short():
    """Short messages not split."""
    assert _split_message("Привет", 100) == ["Привет"]


def test_split_message_long():
    """Long messages split by paragraphs."""
    text = "Абзац 1\n\nАбзац 2\n\nАбзац 3"
    parts = _split_message(text, 20)
    assert len(parts) >= 2
    # All original text preserved
    assert "Абзац 1" in "".join(parts)
    assert "Абзац 3" in "".join(parts)


def test_split_message_single_long_line():
    """Single line longer than limit gets truncated."""
    text = "A" * 200
    parts = _split_message(text, 50)
    assert len(parts) >= 1
    assert len(parts[0]) <= 50


# ─────────────────────────────────────────────
# _truncate_tool_results
# ─────────────────────────────────────────────

def test_truncate_tool_results_few_rounds():
    """With few rounds, messages are unchanged."""
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    ]
    result = _truncate_tool_results(messages, keep_recent=2)
    assert result == messages


def test_truncate_tool_results_many_rounds():
    """Old tool results get truncated, recent ones stay full."""
    long_result = "A" * 500
    messages = [
        {"role": "user", "content": "hello"},
        # Round 1
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": long_result}]},
        # Round 2
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t2"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": long_result}]},
        # Round 3
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t3"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t3", "content": long_result}]},
    ]
    result = _truncate_tool_results(messages, keep_recent=2)
    # Round 1 should be truncated
    r1_content = result[2]["content"][0]["content"]
    assert len(r1_content) <= 104  # 100 chars + "..."
    # Round 3 should be full
    r3_content = result[6]["content"][0]["content"]
    assert r3_content == long_result


# ─────────────────────────────────────────────
# _build_actions_summary / _describe_tool_action
# ─────────────────────────────────────────────

def test_build_actions_summary_empty():
    assert _build_actions_summary([]) == "Выполнено."


def test_build_actions_summary_with_actions():
    result = _build_actions_summary(["CRM: Расул", "Напоминание: звонок"])
    assert "CRM: Расул" in result
    assert "Напоминание: звонок" in result
    assert result.startswith("Выполнено:")


def test_describe_tool_action():
    assert "Расул" in _describe_tool_action("save_entity_note", {"entity_name": "Расул", "entity_type": "person", "content": "test"})
    assert _describe_tool_action("unknown_tool", {}) == "unknown_tool"
