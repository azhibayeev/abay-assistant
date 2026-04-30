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
