"""Google Calendar клиент — OAuth2 + Google Calendar API."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from loguru import logger

from abay_assistant.config import get_settings

# read + write — create_event требует запись
SCOPES = ["https://www.googleapis.com/auth/calendar"]
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"


class CalendarClient:
    """Клиент для Google Calendar API."""

    def __init__(self) -> None:
        self._service = None
        self._enabled = CREDENTIALS_FILE.exists()
        if not self._enabled:
            logger.warning(
                "credentials.json не найден — Google Calendar отключён"
            )

    def _get_service(self):
        """Получить или создать сервис Calendar API."""
        if self._service:
            return self._service

        creds = None
        if TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(CREDENTIALS_FILE), SCOPES
                )
                creds = flow.run_local_server(port=0)
            TOKEN_FILE.write_text(creds.to_json())

        self._service = build("calendar", "v3", credentials=creds)
        return self._service

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def get_events(self, date: datetime | None = None) -> list[dict]:
        """Получить события на указанный день (по умолчанию — сегодня)."""
        if not self._enabled:
            return []

        tz = ZoneInfo(get_settings().timezone)

        if date is None:
            date = datetime.now(tz)

        start_of_day = date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
        end_of_day = start_of_day + timedelta(days=1)

        def _fetch():
            service = self._get_service()
            result = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=start_of_day.isoformat(),
                    timeMax=end_of_day.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            return result.get("items", [])

        events = await asyncio.to_thread(_fetch)
        logger.debug("Calendar: {} событий на {}", len(events), date.strftime("%Y-%m-%d"))

        # Упрощённый формат
        return [
            {
                "summary": e.get("summary", "(без названия)"),
                "start": e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "")),
                "end": e.get("end", {}).get("dateTime", e.get("end", {}).get("date", "")),
            }
            for e in events
        ]

    async def create_event(
        self, summary: str, start: datetime, end: datetime
    ) -> dict:
        """Создать событие в календаре."""
        if not self._enabled:
            return {"error": "Calendar не настроен"}

        def _create():
            service = self._get_service()
            tz_name = get_settings().timezone
            event = {
                "summary": summary,
                "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
                "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
            }
            return service.events().insert(calendarId="primary", body=event).execute()

        result = await asyncio.to_thread(_create)
        logger.info("Calendar: создано событие '{}'", summary)
        return result
