"""Obsidian vault клиент — CRM/knowledge graph с YAML-метаданными и автосвязями."""

import re
from datetime import datetime
from pathlib import Path

import yaml
from loguru import logger

ENTITY_FOLDERS = {
    "person": "People",
    "project": "Projects",
}


def _sanitize_name(name: str) -> str:
    """Санитизировать имя сущности для безопасного использования в путях."""
    # Убрать path traversal
    name = name.replace("..", "").replace("/", "").replace("\\", "")
    # Убрать управляющие символы и спец. символы файловой системы
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "", name)
    return name.strip(". ")


class ObsidianClient:
    """Клиент для работы с Obsidian vault как CRM."""

    def __init__(self, vault_path: Path | str = "./abay-vault"):
        self.vault = Path(vault_path).resolve()

    # ─────────────────────────────────────────
    # Базовые операции
    # ─────────────────────────────────────────

    def _safe_path(self, relative_path: str) -> Path:
        """Проверить что путь не выходит за пределы vault."""
        fp = (self.vault / relative_path).resolve()
        if not str(fp).startswith(str(self.vault.resolve())):
            raise ValueError(f"Попытка выхода за пределы vault: '{relative_path}'")
        return fp

    async def read_note(self, relative_path: str) -> str:
        fp = self._safe_path(relative_path)
        if fp.exists():
            return fp.read_text(encoding="utf-8")
        return ""

    async def write_note(self, relative_path: str, content: str) -> None:
        fp = self._safe_path(relative_path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")

    async def list_notes(self, folder: str = "") -> list[str]:
        target = self.vault / folder
        if not target.exists():
            return []
        return [str(p.relative_to(self.vault)) for p in target.rglob("*.md")]

    # ─────────────────────────────────────────
    # Daily notes
    # ─────────────────────────────────────────

    async def append_daily(self, content: str) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        path = f"Daily/{today}.md"
        fp = self.vault / path
        fp.parent.mkdir(parents=True, exist_ok=True)

        if fp.exists():
            existing = fp.read_text(encoding="utf-8")
            fp.write_text(existing.rstrip() + "\n\n" + content + "\n", encoding="utf-8")
        else:
            fp.write_text(f"# {today}\n\n{content}\n", encoding="utf-8")

        logger.info("Obsidian: добавлено в {}", path)
        return path

    # ─────────────────────────────────────────
    # Personal patterns — привычки/предпочтения Алана и Абая
    # ─────────────────────────────────────────

    def _patterns_path(self, person: str) -> Path:
        safe = _sanitize_name(person) or "Unknown"
        fp = (self.vault / "Patterns" / f"{safe}.md").resolve()
        if not str(fp).startswith(str(self.vault.resolve())):
            raise ValueError(f"Невалидный путь паттернов: '{person}'")
        return fp

    async def append_personal_pattern(self, person: str, observation: str) -> str:
        """Дописать наблюдение о привычке/предпочтении. Формат: дата + одна строка."""
        fp = self._patterns_path(person)
        fp.parent.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        line = f"- {today}: {observation.strip()}\n"
        if fp.exists():
            existing = fp.read_text(encoding="utf-8")
            fp.write_text(existing.rstrip() + "\n" + line, encoding="utf-8")
        else:
            fp.write_text(f"# Паттерны: {person}\n\n{line}", encoding="utf-8")
        rel = fp.relative_to(self.vault)
        logger.info("Obsidian: pattern → {}", rel)
        return str(rel)

    async def read_personal_patterns(self, person: str) -> str:
        """Прочитать накопленные паттерны. Пусто если файла нет."""
        fp = self._patterns_path(person)
        if fp.exists():
            return fp.read_text(encoding="utf-8")
        return ""

    # ─────────────────────────────────────────
    # YAML frontmatter helpers
    # ─────────────────────────────────────────

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict, str]:
        """Разобрать YAML frontmatter и тело файла."""
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                except yaml.YAMLError:
                    meta = {}
                body = parts[2].lstrip("\n")
                return meta, body
        return {}, text

    @staticmethod
    def _build_frontmatter(meta: dict, body: str) -> str:
        """Собрать файл из YAML frontmatter + тело."""
        yaml_str = yaml.dump(meta, allow_unicode=True, default_flow_style=False).strip()
        return f"---\n{yaml_str}\n---\n\n{body}"

    @staticmethod
    def _extract_wiki_links(text: str) -> list[str]:
        """Извлечь [[wiki-ссылки]] из текста."""
        return re.findall(r"\[\[([^\]]+)\]\]", text)

    # ─────────────────────────────────────────
    # Knowledge graph — сущности (People, Projects)
    # ─────────────────────────────────────────

    def _entity_path(self, entity_type: str, entity_name: str) -> Path:
        folder = ENTITY_FOLDERS.get(entity_type, "Inbox")
        safe_name = _sanitize_name(entity_name)
        if not safe_name:
            raise ValueError(f"Невалидное имя сущности: '{entity_name}'")
        path = (self.vault / folder / f"{safe_name}.md").resolve()
        # Проверить что не вышли за пределы vault
        if not str(path).startswith(str(self.vault.resolve())):
            raise ValueError(f"Попытка выхода за пределы vault: '{entity_name}'")
        return path

    async def get_entity(self, entity_type: str, entity_name: str) -> str:
        """Прочитать заметку о человеке или проекте."""
        fp = self._entity_path(entity_type, entity_name)
        if fp.exists():
            return fp.read_text(encoding="utf-8")
        return ""

    async def get_entity_meta(self, entity_type: str, entity_name: str) -> dict:
        """Получить только метаданные сущности."""
        text = await self.get_entity(entity_type, entity_name)
        if not text:
            return {}
        meta, _ = self._parse_frontmatter(text)
        return meta

    async def save_entity_note(
        self,
        entity_type: str,
        entity_name: str,
        content: str,
        meta_update: dict | None = None,
    ) -> str:
        """Добавить запись в заметку сущности. Автоматически обновляет связи."""
        fp = self._entity_path(entity_type, entity_name)
        fp.parent.mkdir(parents=True, exist_ok=True)

        today = datetime.now().strftime("%Y-%m-%d")
        entry = f"### {today}\n{content}\n"

        if fp.exists():
            existing = fp.read_text(encoding="utf-8")
            meta, body = self._parse_frontmatter(existing)
            # Дедуп: если такой content уже есть в теле дословно — пропустить запись.
            # meta всё равно обновим (last_updated, links), но новый ### блок не плодим.
            if content.strip() and content.strip() in body:
                logger.info("Obsidian: дубль content для {} — обновлю только meta", entity_name)
                if meta_update:
                    for key, val in meta_update.items():
                        if val is not None:
                            meta[key] = val
                meta["last_updated"] = today
                fp.write_text(self._build_frontmatter(meta, body), encoding="utf-8")
                rel_path = str(fp.relative_to(self.vault))
                # обратные связи всё равно прогоним — могли появиться новые
                links = self._extract_wiki_links(content)
                await self._cross_link(entity_type, entity_name, links)
                return rel_path
        else:
            # Новый файл — создаём базовую структуру
            meta = {"type": entity_type}
            if entity_type == "person":
                meta.update({"role": "", "projects": [], "contacts": {}})
            elif entity_type == "project":
                meta.update({"status": "active", "zone": "", "people": []})
            body = f"# {entity_name}\n\n"

        # Обновить метаданные если переданы
        if meta_update:
            for key, val in meta_update.items():
                if val is not None:
                    meta[key] = val

        # Обновить last_contact
        meta["last_updated"] = today

        # Извлечь wiki-ссылки и обновить связи
        links = self._extract_wiki_links(content)
        if entity_type == "person" and links:
            projects = set(meta.get("projects") or [])
            projects.update(links)
            meta["projects"] = sorted(projects)
        elif entity_type == "project" and links:
            people = set(meta.get("people") or [])
            people.update(links)
            meta["people"] = sorted(people)

        # Добавить запись в тело
        body = body.rstrip() + "\n\n" + entry

        # Сохранить
        fp.write_text(self._build_frontmatter(meta, body), encoding="utf-8")

        # Автоматические обратные связи
        await self._cross_link(entity_type, entity_name, links)

        rel_path = str(fp.relative_to(self.vault))
        logger.info("Obsidian: записано в {}", rel_path)
        return rel_path

    async def _cross_link(
        self, source_type: str, source_name: str, linked_names: list[str]
    ) -> None:
        """Обновить обратные ссылки: если person → project, то project.people += person."""
        target_type = "project" if source_type == "person" else "person"
        link_field = "people" if target_type == "project" else "projects"

        for name in linked_names:
            fp = self._entity_path(target_type, name)
            if not fp.exists():
                continue  # не создаём файлы для несуществующих сущностей

            text = fp.read_text(encoding="utf-8")
            meta, body = self._parse_frontmatter(text)

            items = set(meta.get(link_field) or [])
            if source_name not in items:
                items.add(source_name)
                meta[link_field] = sorted(items)
                fp.write_text(self._build_frontmatter(meta, body), encoding="utf-8")
                logger.debug("Obsidian: обратная связь {} → {}", name, source_name)

    async def update_entity_meta(
        self, entity_type: str, entity_name: str, **fields
    ) -> str:
        """Обновить метаданные сущности (role, status, contacts, zone и т.д.)."""
        fp = self._entity_path(entity_type, entity_name)
        if not fp.exists():
            return f"Заметка '{entity_name}' не найдена."

        text = fp.read_text(encoding="utf-8")
        meta, body = self._parse_frontmatter(text)

        for key, val in fields.items():
            if val is not None:
                meta[key] = val

        fp.write_text(self._build_frontmatter(meta, body), encoding="utf-8")
        rel_path = str(fp.relative_to(self.vault))
        logger.info("Obsidian: обновлены метаданные {}", rel_path)
        return rel_path

    async def list_entities(self, entity_type: str) -> list[dict]:
        """Список всех сущностей типа с метаданными."""
        folder = ENTITY_FOLDERS.get(entity_type)
        if not folder:
            return []

        target = self.vault / folder
        if not target.exists():
            return []

        results = []
        for fp in sorted(target.glob("*.md")):
            text = fp.read_text(encoding="utf-8")
            meta, _ = self._parse_frontmatter(text)
            name = fp.stem
            results.append({"name": name, **meta})

        return results

    async def get_entity_summary(self, entity_type: str, entity_name: str) -> str:
        """Краткая сводка по сущности для отображения в Telegram."""
        fp = self._entity_path(entity_type, entity_name)
        if not fp.exists():
            return f"'{entity_name}' не найден в базе знаний."

        text = fp.read_text(encoding="utf-8")
        meta, body = self._parse_frontmatter(text)

        lines = [f"<b>{entity_name}</b>"]

        if entity_type == "person":
            if meta.get("role"):
                lines.append(f"Роль: {meta['role']}")
            if meta.get("company"):
                lines.append(f"Компания: {meta['company']}")
            if meta.get("projects"):
                lines.append(f"Проекты: {', '.join(meta['projects'])}")
            if meta.get("contacts"):
                contacts = meta["contacts"]
                if isinstance(contacts, dict):
                    for k, v in contacts.items():
                        if v:
                            lines.append(f"{k}: {v}")
        elif entity_type == "project":
            if meta.get("status"):
                lines.append(f"Статус: {meta['status']}")
            if meta.get("zone"):
                lines.append(f"Зона: {meta['zone']}")
            if meta.get("people"):
                lines.append(f"Люди: {', '.join(meta['people'])}")

        if meta.get("last_updated"):
            lines.append(f"Обновлено: {meta['last_updated']}")

        # Последние 3 записи из тела
        entries = re.findall(r"### (\d{4}-\d{2}-\d{2})\n(.*?)(?=\n###|\Z)", body, re.DOTALL)
        if entries:
            lines.append("\n<b>Последние записи:</b>")
            for date, entry_text in entries[-3:]:
                short = entry_text.strip()[:150]
                # Убрать wiki-ссылки для отображения
                short = re.sub(r"\[\[([^\]]+)\]\]", r"\1", short)
                lines.append(f"  {date}: {short}")

        return "\n".join(lines)

    async def search(self, query: str) -> list[dict]:
        """Поиск по всем заметкам vault."""
        results = []
        query_lower = query.lower()

        for md_file in self.vault.rglob("*.md"):
            if ".obsidian" in str(md_file):
                continue

            try:
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                continue

            if query_lower in text.lower():
                lines = text.split("\n")
                matches = [
                    line.strip()
                    for line in lines
                    if query_lower in line.lower()
                ]

                rel_path = str(md_file.relative_to(self.vault))
                results.append({
                    "path": rel_path,
                    "matches": matches[:5],
                })

        logger.debug("Obsidian search '{}': {} файлов", query, len(results))
        return results
