"""Tests for abay_assistant.bot.evening — date parsing and session logic."""

from datetime import datetime, timedelta

import pytest

from abay_assistant.bot.evening import _parse_due


def test_parse_due_tomorrow():
    result = _parse_due("завтра")
    assert result is not None
    expected = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    assert result.startswith(expected)
    assert result.endswith("T09:00:00")


def test_parse_due_day_after():
    result = _parse_due("послезавтра")
    assert result is not None
    expected = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    assert result.startswith(expected)


def test_parse_due_in_n_days():
    result = _parse_due("через 3 дня")
    assert result is not None
    expected = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    assert result.startswith(expected)


def test_parse_due_in_1_day():
    result = _parse_due("через 1 день")
    assert result is not None
    expected = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    assert result.startswith(expected)


def test_parse_due_weekday():
    result = _parse_due("в пятницу")
    assert result is not None
    parsed = datetime.fromisoformat(result)
    assert parsed.weekday() == 4  # Friday


def test_parse_due_next_week():
    result = _parse_due("на следующей неделе")
    assert result is not None
    parsed = datetime.fromisoformat(result)
    assert parsed.weekday() == 0  # Monday
    assert parsed > datetime.now()


def test_parse_due_date_month():
    result = _parse_due("15 мая")
    assert result is not None
    parsed = datetime.fromisoformat(result)
    assert parsed.month == 5
    assert parsed.day == 15


def test_parse_due_dot_format():
    result = _parse_due("15.05")
    assert result is not None
    parsed = datetime.fromisoformat(result)
    assert parsed.month == 5
    assert parsed.day == 15


def test_parse_due_dot_format_with_year():
    result = _parse_due("01.01.2027")
    assert result is not None
    parsed = datetime.fromisoformat(result)
    assert parsed.year == 2027
    assert parsed.month == 1
    assert parsed.day == 1


def test_parse_due_unknown():
    assert _parse_due("не знаю когда") is None
    assert _parse_due("") is None
    assert _parse_due("asdf") is None
