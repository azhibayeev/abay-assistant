"""Tests for abay_assistant.services.obsidian — vault operations."""

from datetime import datetime

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
