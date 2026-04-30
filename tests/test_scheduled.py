"""Tests for abay_assistant.bot.scheduled — cron job helpers."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from abay_assistant.services.obsidian import ObsidianClient
from abay_assistant.bot.scheduled import (
    _match_event_to_crm,
    _filter_forgotten_contacts,
    _find_stuck_cards,
    _compute_energy_insight,
    _classify_due_date,
    _format_week_name,
    _find_month_list_name,
)


async def test_get_weekly_daily_notes(tmp_vault):
    """Test reading daily notes for a date range."""
    client = ObsidianClient(vault_path=tmp_vault)
    today = datetime.now()

    # Write a note for today
    await client.append_daily("Today's notes")

    # Read it back via the read_note method (same as scheduled uses)
    today_str = today.strftime("%Y-%m-%d")
    content = await client.read_note(f"Daily/{today_str}.md")
    assert "Today's notes" in content


async def test_get_weekly_daily_notes_missing_days(tmp_vault):
    """Missing days return empty string."""
    client = ObsidianClient(vault_path=tmp_vault)
    content = await client.read_note("Daily/2020-01-01.md")
    assert content == ""


async def test_check_reminders_sends(in_memory_db):
    """Claimed reminders are processed (integration with db)."""
    from abay_assistant.db import create_reminder, claim_due_reminders

    past = datetime.now() - timedelta(minutes=1)
    create_reminder(111, "Test reminder", past)

    claimed = claim_due_reminders()
    assert len(claimed) == 1
    assert claimed[0].text == "Test reminder"
    assert claimed[0].sent is True

    # Second claim returns nothing
    claimed2 = claim_due_reminders()
    assert len(claimed2) == 0


# ─────────────────────────────────────────────
# _match_event_to_crm
# ─────────────────────────────────────────────

def test_match_event_to_crm_found():
    people = [{"name": "Расул"}, {"name": "Алмас"}]
    result = _match_event_to_crm("Встреча с Расул по DMS", people)
    assert result == "Расул"


def test_match_event_to_crm_not_found():
    people = [{"name": "Расул"}]
    result = _match_event_to_crm("Обед", people)
    assert result is None


def test_match_event_to_crm_case_insensitive():
    people = [{"name": "Расул"}]
    result = _match_event_to_crm("встреча с расул", people)
    assert result == "Расул"


def test_match_event_to_crm_short_name_skipped():
    """Names shorter than 3 chars should be skipped."""
    people = [{"name": "Ан"}]
    result = _match_event_to_crm("Встреча с Ан", people)
    assert result is None


# ─────────────────────────────────────────────
# _filter_forgotten_contacts
# ─────────────────────────────────────────────

def test_filter_forgotten_contacts_basic():
    old_date = (date.today() - timedelta(days=30)).isoformat()
    people = [
        {"name": "Расул", "role": "партнёр", "last_updated": old_date},
        {"name": "Алмас", "role": "сотрудник", "last_updated": old_date},  # wrong role
    ]
    result = _filter_forgotten_contacts(people, cutoff_days=21)
    assert len(result) == 1
    assert result[0]["name"] == "Расул"
    assert result[0]["days_ago"] >= 30


def test_filter_forgotten_contacts_no_last_updated():
    """People without last_updated should be included with days_ago=9999."""
    people = [{"name": "Ержан", "role": "клиент"}]
    result = _filter_forgotten_contacts(people, cutoff_days=21)
    assert len(result) == 1
    assert result[0]["days_ago"] == 9999
    assert result[0]["last_updated"] == "(никогда)"


def test_filter_forgotten_contacts_sorted():
    """Results should be sorted by staleness, most stale first."""
    old = (date.today() - timedelta(days=60)).isoformat()
    medium = (date.today() - timedelta(days=30)).isoformat()
    people = [
        {"name": "Средний", "role": "партнёр", "last_updated": medium},
        {"name": "Старый", "role": "клиент", "last_updated": old},
    ]
    result = _filter_forgotten_contacts(people, cutoff_days=21)
    assert len(result) == 2
    assert result[0]["name"] == "Старый"
    assert result[1]["name"] == "Средний"


def test_filter_forgotten_contacts_recent_excluded():
    """Recently updated contacts should not be returned."""
    recent = (date.today() - timedelta(days=5)).isoformat()
    people = [{"name": "Активный", "role": "партнёр", "last_updated": recent}]
    result = _filter_forgotten_contacts(people, cutoff_days=21)
    assert len(result) == 0


# ─────────────────────────────────────────────
# _find_stuck_cards
# ─────────────────────────────────────────────

def test_find_stuck_cards_basic():
    old_date = (datetime.now() - timedelta(days=5)).isoformat() + "Z"
    cards = [
        {"name": "Старая задача", "dateLastActivity": old_date},
        {"name": "Свежая", "dateLastActivity": datetime.now().isoformat() + "Z"},
    ]
    result = _find_stuck_cards(cards, stale_days=3)
    assert len(result) == 1
    assert result[0]["name"] == "Старая задача"
    assert result[0]["days_inactive"] >= 4


def test_find_stuck_cards_missing_activity():
    """Cards without dateLastActivity should be skipped."""
    cards = [
        {"name": "Без даты"},
        {"name": "Пустая дата", "dateLastActivity": ""},
    ]
    result = _find_stuck_cards(cards, stale_days=3)
    assert len(result) == 0


# ─────────────────────────────────────────────
# _compute_energy_insight
# ─────────────────────────────────────────────

def _make_stat(date_str: str, energy: int):
    return SimpleNamespace(date=date_str, energy=energy)


def test_compute_energy_insight_low():
    """Low energy day should produce a warning."""
    # Create stats for Mondays (weekday=0) with low energy
    stats = [
        _make_stat("2026-04-13", 2),  # Monday
        _make_stat("2026-04-20", 2),  # Monday
        _make_stat("2026-04-27", 3),  # Monday
    ]
    result = _compute_energy_insight(stats, today_weekday=0, low_threshold=3.0)
    assert result is not None
    assert "понедельник" in result
    assert "/5" in result


def test_compute_energy_insight_insufficient_data():
    """Less than 2 data points should return None."""
    stats = [_make_stat("2026-04-13", 2)]  # Only 1 Monday
    result = _compute_energy_insight(stats, today_weekday=0)
    assert result is None


def test_compute_energy_insight_high():
    """High energy day should return None (no warning needed)."""
    stats = [
        _make_stat("2026-04-13", 4),  # Monday
        _make_stat("2026-04-20", 5),  # Monday
    ]
    result = _compute_energy_insight(stats, today_weekday=0, low_threshold=3.0)
    assert result is None


# ─────────────────────────────────────────────
# _classify_due_date
# ─────────────────────────────────────────────

def test_classify_due_date_today():
    """Today's date → 'today'."""
    today_str = date.today().isoformat()
    assert _classify_due_date(today_str) == "today"


def test_classify_due_date_overdue():
    """Past date → 'today' (overdue goes to today)."""
    past = (date.today() - timedelta(days=3)).isoformat()
    assert _classify_due_date(past) == "today"


def test_classify_due_date_this_week():
    """Date within current week → 'week'."""
    today = date.today()
    # Find next day that's still this week (not today)
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    if today < sunday:
        target = today + timedelta(days=1)
        if target <= sunday:
            assert _classify_due_date(target.isoformat()) == "week"


def test_classify_due_date_invalid():
    """Invalid date → None."""
    assert _classify_due_date("not-a-date") is None
    assert _classify_due_date("") is None


# ─────────────────────────────────────────────
# _format_week_name
# ─────────────────────────────────────────────

def test_classify_due_date_future_month():
    """Date in a future month this year → 'month:N'."""
    future = date.today().replace(month=12, day=15)
    if future <= date.today():
        return  # skip if December has passed
    assert _classify_due_date(future.isoformat()).startswith("month:")


def test_classify_due_date_next_year():
    """Date in next year → 'next_year'."""
    next_year = date(date.today().year + 1, 3, 15)
    assert _classify_due_date(next_year.isoformat()) == "next_year"


def test_format_week_name_same_month():
    monday = date(2026, 5, 4)
    sunday = date(2026, 5, 10)
    result = _format_week_name(monday, sunday)
    assert "4" in result
    assert "10" in result
    assert "мая" in result


def test_format_week_name_cross_month():
    monday = date(2026, 4, 27)
    sunday = date(2026, 5, 3)
    result = _format_week_name(monday, sunday)
    assert "27" in result
    assert "3" in result


# ─────────────────────────────────────────────
# _find_month_list_name
# ─────────────────────────────────────────────

def test_find_month_list_name_found():
    lists = ["Сегодня", "Неделя 5-11 мая", "Май", "Backlog"]
    assert _find_month_list_name(lists, 5) == "Май"


def test_find_month_list_name_not_found():
    lists = ["Сегодня", "Неделя", "Backlog"]
    assert _find_month_list_name(lists, 6) is None
