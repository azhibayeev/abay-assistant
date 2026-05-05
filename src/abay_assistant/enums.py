"""Перечисления, используемые по всему проекту."""

from enum import StrEnum


class EntityType(StrEnum):
    PERSON = "person"
    PROJECT = "project"


class TrelloList(StrEnum):
    TODAY = "сегодня"
    WEEK = "неделя"  # подстрока — найдёт "неделя 27– 3 май." и т.п.
    WAITING = "Мяч на стороне"
    STUDY = "Изучить"
    BACKLOG = "Backlog"
    DONE = "Готово"
    ARCHIVE = "архив"
    NEXT_YEAR = "В следующем году"


class UserRole(StrEnum):
    OWNER = "owner"
    ASSISTANT = "assistant"
    UNKNOWN = "unknown"
