"""LLM-as-judge: оценивает соответствие transcript ожиданиям сценария."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from abay_assistant.services.llm import LLMClient

from .runner import Transcript


JUDGE_SYSTEM = """Ты — строгий QA-судья для Telegram-ассистента Абая.

Тебе даётся:
- Описание сценария: что сказал пользователь, какое было состояние доски Trello
- Пронумерованный список ожиданий
- Транскрипт прогона: tool calls с аргументами и результатами + финальный текст ответа + состояние доски после

ПРАВИЛА:
1. Оценивай ТОЛЬКО ожидания из списка. Не придумывай дополнительных требований.
2. Каждое ожидание оценивается отдельно: либо удовлетворено, либо нет.
3. Если в ожидании есть «либо A, либо B» (или «любой из …») — достаточно ОДНОГО из вариантов.
4. Если ожидание про tool call — проверь по списку tool_calls (имя tool + аргументы).
5. Если про состояние доски — проверь board_after.
6. Если про текст ответа — проверь final_text по смыслу (без эмодзи-фанатизма).
7. Если про «не задавал уточнений» — проверь что нет request_clarification и финальный текст не вопрос.
8. Каждое violation должно цитировать конкретное ожидание из списка с его номером.

САМОПРОВЕРКА перед возвратом ответа (ВАЖНО):
- Если ты пишешь в violation что-то вроде «соответствует ожиданию» / «выполнено корректно» / «бот сделал правильно» — значит ожидание выполнено, и это НЕ violation. Убери его и поставь pass=true.
- Если в нотах написано «все ожидания выполнены» — pass должен быть true.
- Имена людей в русском языке могут быть в разной падежной форме (Аксу = Аксе = Аксы и т.п.) — это та же сущность, не считай за нарушение.
- Названия зон с пробелами и слэшами (например «B2B / B2G», «Стратегия», «Финансы») — это валидные одиночные значения метки, не «две зоны».

Верни СТРОГО один JSON-объект, без markdown-обёртки:
{
  "pass": true|false,
  "violations": ["#N: <номер и краткая причина почему ожидание N не выполнено>", ...],
  "notes": "одна-две строки общей оценки"
}

pass=true только если ВСЕ ожидания из списка выполнены. violations — пусто при pass=true.
violations НЕ должны содержать претензий вне списка ожиданий.
"""


@dataclass
class JudgeResult:
    passed: bool
    violations: list[str]
    notes: str
    raw: str

    def render(self) -> str:
        status = "✅ PASS" if self.passed else "❌ FAIL"
        lines = [status]
        if self.violations:
            lines.append("Violations:")
            for v in self.violations:
                lines.append(f"  • {v}")
        if self.notes:
            lines.append(f"Notes: {self.notes}")
        return "\n".join(lines)


def _strip_json_fence(text: str) -> str:
    """Убрать ```json ... ``` обёртку если LLM её добавил вопреки промпту."""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return fence.group(1).strip() if fence else text


async def judge(scenario: dict, transcript: Transcript, *, llm: LLMClient | None = None) -> JudgeResult:
    """Прогнать судью по сценарию и transcript."""
    llm = llm or LLMClient()
    expectations = scenario.get("expectations") or []
    if not expectations:
        return JudgeResult(
            passed=True, violations=[],
            notes="(нет ожиданий — сценарий считается пройденным по умолчанию)",
            raw="",
        )

    from datetime import datetime, timedelta
    today = datetime.now()
    tomorrow = today + timedelta(days=1)
    next_week_start = today + timedelta(days=7)

    user_prompt = (
        "## Контекст времени (бот работает в этом контексте)\n"
        f"Сегодня: {today.strftime('%Y-%m-%d (%A)')}\n"
        f"Завтра: {tomorrow.strftime('%Y-%m-%d')}\n"
        f"Через неделю: {next_week_start.strftime('%Y-%m-%d')}\n\n"
        "## Сценарий\n"
        f"Имя: {scenario.get('name', '?')}\n"
        f"Роль пользователя: {scenario.get('role', 'owner')}\n"
        f"Сообщение: «{scenario['message']}»\n"
        f"Начальное состояние доски:\n{json.dumps(scenario.get('board', {}), ensure_ascii=False, indent=2)}\n\n"
        "## Ожидания\n"
        + "\n".join(f"{i+1}. {e}" for i, e in enumerate(expectations))
        + "\n\n## Транскрипт прогона\n"
        + transcript.render()
    )

    raw = await llm.chat(
        messages=[{"role": "user", "content": user_prompt}],
        system=JUDGE_SYSTEM,
        max_tokens=1024,
    )

    cleaned = _strip_json_fence(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return JudgeResult(
            passed=False,
            violations=[f"Судья вернул не-JSON: {cleaned[:200]}"],
            notes="judge parse failed",
            raw=raw,
        )

    return JudgeResult(
        passed=bool(parsed.get("pass", False)),
        violations=list(parsed.get("violations") or []),
        notes=str(parsed.get("notes") or ""),
        raw=raw,
    )
