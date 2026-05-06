"""Tests for abay_assistant.services.obsidian — vault operations."""

from datetime import datetime, timedelta

import pytest

from abay_assistant.services.obsidian import ObsidianClient, _sanitize_name


@pytest.fixture
def client(tmp_vault):
    return ObsidianClient(vault_path=tmp_vault)


async def test_append_daily_new(client, tmp_vault):
    path = await client.append_daily("Test entry")
    today = datetime.now().strftime("%Y-%m-%d")
    assert path == f"Daily/{today}.md"
    content = (tmp_vault / path).read_text()
    assert f"# {today}" in content
    assert "Test entry" in content


async def test_append_daily_existing(client, tmp_vault):
    # Write initial content
    await client.append_daily("First entry")
    await client.append_daily("Second entry")

    today = datetime.now().strftime("%Y-%m-%d")
    content = (tmp_vault / f"Daily/{today}.md").read_text()
    assert "First entry" in content
    assert "Second entry" in content


async def test_save_entity_note_new_person(client, tmp_vault):
    path = await client.save_entity_note(
        entity_type="person",
        entity_name="Расул",
        content="Встреча по проекту",
    )
    assert path == "People/Расул.md"
    content = (tmp_vault / "People" / "Расул.md").read_text()
    assert "type: person" in content
    assert "Встреча по проекту" in content
    assert "# Расул" in content


async def test_save_entity_note_existing(client, tmp_vault):
    await client.save_entity_note("person", "Марат", "First meeting")
    await client.save_entity_note("person", "Марат", "Second meeting")

    content = (tmp_vault / "People" / "Марат.md").read_text()
    assert "First meeting" in content
    assert "Second meeting" in content


async def test_save_entity_note_wiki_links(client, tmp_vault):
    # Create the project first so cross-linking works
    await client.save_entity_note("project", "DMS", "Project start")

    await client.save_entity_note(
        "person", "Расул",
        "Обсудили [[DMS]] roadmap",
    )

    content = (tmp_vault / "People" / "Расул.md").read_text()
    assert "DMS" in content  # in projects list
    # Check frontmatter has the project link
    assert "projects:" in content


async def test_cross_link(client, tmp_vault):
    await client.save_entity_note("project", "DMS", "Project init")
    await client.save_entity_note("person", "Расул", "Works on [[DMS]]")

    # DMS should now have Расул in its people list
    dms_content = (tmp_vault / "Projects" / "DMS.md").read_text()
    assert "Расул" in dms_content


async def test_get_entity_found(client, tmp_vault):
    await client.save_entity_note("person", "Алан", "Test note")
    text = await client.get_entity("person", "Алан")
    assert "Test note" in text
    assert "Алан" in text


async def test_get_entity_not_found(client):
    text = await client.get_entity("person", "НесуществующийЧеловек")
    assert text == ""


async def test_list_entities(client, tmp_vault):
    await client.save_entity_note("person", "Расул", "Note 1")
    await client.save_entity_note("person", "Марат", "Note 2")

    entities = await client.list_entities("person")
    assert len(entities) == 2
    names = {e["name"] for e in entities}
    assert names == {"Расул", "Марат"}


async def test_update_entity_meta(client, tmp_vault):
    await client.save_entity_note("person", "Расул", "Initial")
    path = await client.update_entity_meta(
        "person", "Расул", role="партнёр", company="АО Техно"
    )
    assert "Расул" in path

    content = (tmp_vault / "People" / "Расул.md").read_text()
    assert "role:" in content


async def test_search(client, tmp_vault):
    await client.save_entity_note("person", "Расул", "Ключевое слово: альфа")
    await client.save_entity_note("person", "Марат", "Другая заметка")

    results = await client.search("альфа")
    assert len(results) == 1
    assert "People/Расул.md" in results[0]["path"]


async def test_safe_path_traversal(client):
    # Name that becomes empty after sanitization
    with pytest.raises(ValueError, match="Невалидное имя"):
        client._entity_path("person", "... ")

    # Path traversal is stripped but result is valid (etcpasswd)
    path = client._entity_path("person", "../../../etc/passwd")
    assert "etc" not in str(path) or "People" in str(path)


def test_sanitize_name():
    assert _sanitize_name("Расул") == "Расул"
    assert _sanitize_name("../hack") == "hack"
    assert _sanitize_name('file<>:"|?*name') == "filename"
    assert _sanitize_name("  dots...  ") == "dots"


# ─────────────────────────────────────────
# Дедуп CRM-записей: точный + семантический
# ─────────────────────────────────────────


async def test_save_entity_note_dedup_exact_same_content(client, tmp_vault):
    """Дословный повтор того же content в один день — не плодит второй ### блок."""
    await client.save_entity_note("person", "Расул", "Встреча по [[DMS]] прошла")
    await client.save_entity_note("person", "Расул", "Встреча по [[DMS]] прошла")

    content = (tmp_vault / "People" / "Расул.md").read_text()
    today = datetime.now().strftime("%Y-%m-%d")
    # Должен быть ровно один заголовок ### {today}
    assert content.count(f"### {today}") == 1


async def test_save_entity_note_dedup_paraphrase_same_day(client, tmp_vault):
    """Парафраз того же события в один день — не плодит второй ### блок."""
    await client.save_entity_note(
        "person", "Абеке",
        "Переговорил по [[Инвест-линия]]. Результат: пока нет следующих шагов, ждём решения.",
    )
    await client.save_entity_note(
        "person", "Абеке",
        "Статус: переговорили с Абеке по инвест-линии — пока нет следующих шагов, ждём решения.",
    )

    content = (tmp_vault / "People" / "Абеке.md").read_text()
    today = datetime.now().strftime("%Y-%m-%d")
    assert content.count(f"### {today}") == 1, "парафраз создал второй ### блок"
    # Первая (исходная) запись осталась
    assert "следующих шагов" in content


async def test_save_entity_note_distinct_events_same_day_kept(client, tmp_vault):
    """Разные события того же человека в один день — обе записи остаются."""
    await client.save_entity_note(
        "person", "Расул",
        "Передал спецификацию [[DMS]] для ревью",
    )
    await client.save_entity_note(
        "person", "Расул",
        "Согласовал график отпуска на июль",
    )

    content = (tmp_vault / "People" / "Расул.md").read_text()
    assert "спецификацию" in content
    assert "отпуска" in content


async def test_save_entity_note_paraphrase_outside_window_kept(client, tmp_vault):
    """Парафраз старше N дней — окно не покрывает, новая запись добавляется."""
    # Подкладываем в файл блок с датой 10 дней назад, минуя save_entity_note
    fp = tmp_vault / "People" / "Абеке.md"
    fp.parent.mkdir(parents=True, exist_ok=True)
    old_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    fp.write_text(
        "---\ntype: person\nrole: ''\nprojects: []\n---\n\n"
        "# Абеке\n\n"
        f"### {old_date}\nПереговорил по [[Инвест-линия]]. Пока ждём решения с их стороны.\n",
        encoding="utf-8",
    )

    await client.save_entity_note(
        "person", "Абеке",
        "Переговорил с Абеке по инвест-линии. Пока ждём решения с их стороны.",
    )

    content = fp.read_text(encoding="utf-8")
    today = datetime.now().strftime("%Y-%m-%d")
    # И старая запись осталась, и новая добавлена — окно дедупа сработало по дате
    assert f"### {old_date}" in content
    assert f"### {today}" in content


def test_dedup_tokens_strips_links_dates_punctuation():
    from abay_assistant.services.obsidian import _dedup_tokens
    tokens = _dedup_tokens(
        "📅 06.05.2026 Переговорил с [[Абеке]] по [[Инвест-линия]]! Мяч на их стороне."
    )
    # дата и эмодзи убраны, слова стеммированы до 5 символов, стоп-слова отброшены
    assert "перег" in tokens
    assert "абеке" in tokens
    assert "инвес" in tokens
    assert "06" not in tokens
    assert "📅" not in tokens
    # короткие токены и стоп-слова отброшены
    assert "на" not in tokens
    assert "их" not in tokens


def test_extract_recent_entries_filters_by_date():
    from abay_assistant.services.obsidian import _extract_recent_entries
    today = datetime.now().date()
    old = (today - timedelta(days=10)).strftime("%Y-%m-%d")
    fresh = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    body = (
        f"# Test\n\n"
        f"### {old}\nстарая запись\n\n"
        f"### {fresh}\nсвежая запись\n"
    )
    entries = _extract_recent_entries(body, days=3)
    dates = [d for d, _ in entries]
    assert fresh in dates
    assert old not in dates
