"""Перечисления, используемые по всему проекту."""

from enum import StrEnum


class EntityType(StrEnum):
    PERSON = "person"
    PROJECT = "project"


class EveningPhase(StrEnum):
    TASKS = "tasks"
    FOLLOWUP_ASK = "followup_ask"
    FOLLOWUP_NAME = "followup_name"
    POSTPONE_WHEN = "postpone_when"
    FAILED_REASON = "failed_reason"
    FAILED_ACTION = "failed_action"
    ENERGY = "energy"
    CHARGED = "charged"
    DRAINED = "drained"
    DONE = "done"


class TaskStatus(StrEnum):
    DONE = "done"
    POSTPONED = "postponed"
    FAILED = "failed"


class FailedAction(StrEnum):
    KEEP = "keep"
    BACKLOG = "backlog"
    ARCHIVE = "archive"


class TrelloList(StrEnum):
    TODAY = "сегодня"
    WEEK = "неделя"  # подстрока — найдёт "неделя 27– 3 май." и т.п.
    WAITING = "Мяч на стороне"
    STUDY = "Изучить"
    BACKLOG = "Backlog"
    DONE = "Готово"
    NEXT_YEAR = "В следующем году"


class UserRole(StrEnum):
    OWNER = "owner"
    ASSISTANT = "assistant"
    UNKNOWN = "unknown"
