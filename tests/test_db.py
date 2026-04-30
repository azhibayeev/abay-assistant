"""Tests for abay_assistant.db — CRUD operations."""

from datetime import datetime, timedelta

import pytest

from abay_assistant.db import (
    save_message,
    get_recent_messages,
    create_reminder,
    claim_due_reminders,
    get_active_reminders,
    delete_reminder,
    save_daily_stat,
    get_daily_stats,
)


def test_save_message(in_memory_db):
    msg = save_message(111, "user", "Привет")
    assert msg.id is not None
    assert msg.telegram_id == 111
    assert msg.role == "user"
    assert msg.text == "Привет"


def test_get_recent_messages_limit(in_memory_db):
    for i in range(10):
        save_message(111, "user", f"msg {i}")
    recent = get_recent_messages(111, limit=3)
    assert len(recent) == 3
    # Should be in chronological order (oldest first)
    assert recent[0].text == "msg 7"
    assert recent[2].text == "msg 9"


def test_get_recent_messages_per_user(in_memory_db):
    save_message(111, "user", "from user 111")
    save_message(222, "user", "from user 222")
    msgs_111 = get_recent_messages(111, limit=10)
    msgs_222 = get_recent_messages(222, limit=10)
    assert len(msgs_111) == 1
    assert len(msgs_222) == 1
    assert msgs_111[0].text == "from user 111"


def test_create_reminder(in_memory_db):
    remind_at = datetime.now() + timedelta(hours=1)
    r = create_reminder(111, "Test reminder", remind_at)
    assert r.id is not None
    assert r.telegram_id == 111
    assert r.text == "Test reminder"
    assert r.sent is False


def test_claim_due_reminders(in_memory_db):
    past = datetime.now() - timedelta(minutes=5)
    future = datetime.now() + timedelta(hours=1)
    create_reminder(111, "due reminder", past)
    create_reminder(111, "future reminder", future)

    claimed = claim_due_reminders()
    assert len(claimed) == 1
    assert claimed[0].text == "due reminder"
    assert claimed[0].sent is True


def test_claim_due_reminders_idempotent(in_memory_db):
    past = datetime.now() - timedelta(minutes=5)
    create_reminder(111, "due reminder", past)

    first = claim_due_reminders()
    assert len(first) == 1
    second = claim_due_reminders()
    assert len(second) == 0


def test_get_active_reminders(in_memory_db):
    future = datetime.now() + timedelta(hours=1)
    create_reminder(111, "active 1", future)
    create_reminder(111, "active 2", future + timedelta(hours=1))
    create_reminder(222, "other user", future)

    active = get_active_reminders(111)
    assert len(active) == 2
    assert active[0].text == "active 1"


def test_delete_reminder(in_memory_db):
    future = datetime.now() + timedelta(hours=1)
    r = create_reminder(111, "to delete", future)

    # Can delete own reminder
    assert delete_reminder(r.id, 111) is True
    # Can't delete again
    assert delete_reminder(r.id, 111) is False


def test_delete_reminder_wrong_user(in_memory_db):
    future = datetime.now() + timedelta(hours=1)
    r = create_reminder(111, "private", future)
    # User 222 can't delete user 111's reminder
    assert delete_reminder(r.id, 222) is False


def test_save_daily_stat(in_memory_db):
    stat = save_daily_stat(
        date="2026-04-30",
        done_count=3,
        postponed_count=1,
        failed_count=0,
        energy=4,
        charged_by="встреча",
        drained_by="отчёт",
    )
    assert stat.id is not None
    assert stat.done_count == 3
    assert stat.energy == 4


def test_save_daily_stat_overwrites(in_memory_db):
    save_daily_stat("2026-04-30", 1, 0, 0, 3)
    stat = save_daily_stat("2026-04-30", 5, 2, 1, 4, "спорт", "звонки")
    assert stat.done_count == 5
    assert stat.energy == 4
    # Only one record for that date
    stats = get_daily_stats("2026-04-30", "2026-04-30")
    assert len(stats) == 1


def test_get_daily_stats_range(in_memory_db):
    save_daily_stat("2026-04-28", 2, 0, 0, 3)
    save_daily_stat("2026-04-29", 4, 1, 0, 4)
    save_daily_stat("2026-04-30", 3, 0, 1, 2)
    save_daily_stat("2026-05-01", 1, 0, 0, 5)  # outside range

    stats = get_daily_stats("2026-04-28", "2026-04-30")
    assert len(stats) == 3
    assert stats[0].date == "2026-04-28"
    assert stats[2].date == "2026-04-30"
    total_done = sum(s.done_count for s in stats)
    assert total_done == 9
