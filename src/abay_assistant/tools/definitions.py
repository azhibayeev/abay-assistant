"""Tool definitions для Claude — Контур Inbox."""

TOOLS: list[dict] = [
    {
        "name": "create_trello_card",
        "description": (
            "Создать новую карточку (цикл) в Trello. "
            "Каждая карточка = цикл, не разовая задача. "
            "Метка зоны обязательна. Описание (desc) обязательно — "
            "карточка без контекста бесполезна."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Название цикла/задачи",
                },
                "list_name": {
                    "type": "string",
                    "description": "Список (колонка): Сегодня, Неделя, Май, Backlog и т.д.",
                    "default": "Сегодня",
                },
                "desc": {
                    "type": "string",
                    "description": (
                        "Описание: контекст (откуда задача), суть, участники, ожидаемый результат. "
                        "ВСЕГДА заполняй — пустой desc недопустим."
                    ),
                },
                "label": {
                    "type": "string",
                    "description": "Название метки зоны: Стратегия, B2B / B2G, Команда, Финансы, Продукт, Личное",
                },
                "due": {
                    "type": "string",
                    "description": "Дедлайн в формате ISO 8601 (YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS)",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "update_trello_card",
        "description": (
            "Обновить существующую карточку в Trello (desc, due, labels). "
            "Для переименования используй rename_trello_card."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {
                    "type": "string",
                    "description": "ID карточки в Trello",
                },
                "desc": {
                    "type": "string",
                    "description": "Новое описание (полностью заменяет старое)",
                },
                "due": {
                    "type": "string",
                    "description": "Новый дедлайн (ISO 8601)",
                },
                "label": {
                    "type": "string",
                    "description": "Добавить метку по имени (не убирает существующие): Стратегия, B2B / B2G, Команда, Финансы, Продукт, Личное, Срочно, Денежная",
                },
            },
            "required": ["card_id"],
        },
    },
    {
        "name": "add_trello_comment",
        "description": (
            "Добавить комментарий к карточке Trello. "
            "Используй для статус-апдейтов: итоги встреч, решения, заметки по прогрессу. "
            "Комментарии сохраняют историю (в отличие от desc)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {
                    "type": "string",
                    "description": "ID карточки",
                },
                "text": {
                    "type": "string",
                    "description": "Текст комментария (статус, итог, решение)",
                },
            },
            "required": ["card_id", "text"],
        },
    },
    {
        "name": "rename_trello_card",
        "description": (
            "Переименовать карточку в Trello. "
            "Автоматически сохраняет старое название в комментарии (история). "
            "Используй при перемещении в 'Мяч на стороне' — добавь ответственного через тире: "
            "'Встреча с Апайкой — Биби'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {
                    "type": "string",
                    "description": "ID карточки",
                },
                "new_name": {
                    "type": "string",
                    "description": "Новое название карточки",
                },
            },
            "required": ["card_id", "new_name"],
        },
    },
    {
        "name": "move_trello_card",
        "description": "Переместить карточку в другой список (колонку) Trello.",
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {
                    "type": "string",
                    "description": "ID карточки",
                },
                "target_list": {
                    "type": "string",
                    "description": "Название целевого списка",
                },
            },
            "required": ["card_id", "target_list"],
        },
    },
    {
        "name": "set_trello_card_cover",
        "description": (
            "Установить цвет обложки карточки Trello (приоритет/тип). "
            "Цвета: red (🔴 критично), orange (🟠 важно, не срочно), "
            "green (🟢 цикличная), blue (🔵 важно, но можно потом). "
            "Спрашивай цвет при создании карточки и когда видишь карточку без обложки."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {
                    "type": "string",
                    "description": "ID карточки",
                },
                "color": {
                    "type": "string",
                    "enum": ["red", "orange", "green", "blue"],
                    "description": "Цвет обложки: red (критично), orange (важно не срочно), green (цикличная), blue (важно можно потом)",
                },
            },
            "required": ["card_id", "color"],
        },
    },
    {
        "name": "add_checklist_item",
        "description": (
            "Добавить пункт в чек-лист карточки. "
            "Шаги цикла записываются как пункты чек-листа."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {
                    "type": "string",
                    "description": "ID карточки",
                },
                "text": {
                    "type": "string",
                    "description": "Текст пункта чек-листа",
                },
            },
            "required": ["card_id", "text"],
        },
    },
    {
        "name": "get_trello_cards",
        "description": "Получить карточки из указанного списка Trello.",
        "input_schema": {
            "type": "object",
            "properties": {
                "list_name": {
                    "type": "string",
                    "description": "Название списка: Сегодня, Неделя, Май, Мяч на стороне, Backlog и т.д.",
                },
            },
            "required": ["list_name"],
        },
    },
    {
        "name": "append_obsidian_daily",
        "description": "Добавить текст в дневную заметку Obsidian (Daily/YYYY-MM-DD.md).",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Текст для добавления в дневную заметку",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "save_entity_note",
        "description": (
            "Сохранить информацию о человеке или проекте в CRM (Obsidian). "
            "Используй после каждого значимого взаимодействия: встреча, договорённость, "
            "предпочтение, контакт, решение. Используй [[wiki-ссылки]] для связей — "
            "система автоматически обновит обратные ссылки."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_type": {
                    "type": "string",
                    "enum": ["person", "project"],
                    "description": "Тип сущности: person или project",
                },
                "entity_name": {
                    "type": "string",
                    "description": "Имя человека или название проекта (например: Расул, DMS)",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Информация для записи. Используй [[ссылки]] на связанные сущности. "
                        "Пример: 'Обсудили [[DMS]] roadmap с [[Марат]]. Решили запустить пилот к маю.'"
                    ),
                },
                "meta": {
                    "type": "object",
                    "description": (
                        "Метаданные для обновления. "
                        "Для person: role, company, contacts (dict с phone/email/telegram). "
                        "Для project: status (active/paused/done), zone."
                    ),
                },
            },
            "required": ["entity_type", "entity_name", "content"],
        },
    },
    {
        "name": "update_entity_meta",
        "description": (
            "Обновить метаданные человека или проекта без добавления записи. "
            "Используй для обновления роли, контактов, статуса проекта."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_type": {
                    "type": "string",
                    "enum": ["person", "project"],
                    "description": "Тип: person или project",
                },
                "entity_name": {
                    "type": "string",
                    "description": "Имя человека или название проекта",
                },
                "role": {"type": "string", "description": "Роль (для person): партнёр, клиент, сотрудник, контакт"},
                "company": {"type": "string", "description": "Компания (для person)"},
                "contacts": {
                    "type": "object",
                    "description": "Контакты (для person): {phone, email, telegram}",
                },
                "status": {"type": "string", "description": "Статус (для project): active, paused, done"},
                "zone": {"type": "string", "description": "Зона (для project): Стратегия, B2B / B2G и т.д."},
            },
            "required": ["entity_type", "entity_name"],
        },
    },
    {
        "name": "get_entity",
        "description": (
            "Загрузить всю информацию о человеке или проекте из CRM. "
            "Используй когда пользователь спрашивает о ком-то/чём-то или перед принятием решений."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_type": {
                    "type": "string",
                    "enum": ["person", "project"],
                    "description": "Тип: person или project",
                },
                "entity_name": {
                    "type": "string",
                    "description": "Имя человека или название проекта",
                },
            },
            "required": ["entity_type", "entity_name"],
        },
    },
    {
        "name": "list_entities",
        "description": (
            "Показать список всех людей или проектов в CRM с метаданными. "
            "Используй когда пользователь спрашивает 'кто в базе?' или 'какие проекты?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_type": {
                    "type": "string",
                    "enum": ["person", "project"],
                    "description": "Тип: person или project",
                },
            },
            "required": ["entity_type"],
        },
    },
    {
        "name": "search_knowledge",
        "description": (
            "Поиск по всей базе знаний (People, Projects, Daily). "
            "Используй когда нужно найти информацию, но неясно в какой заметке она лежит."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос (имя, ключевое слово, тема)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "set_reminder",
        "description": (
            "Поставить напоминание. Парси время из контекста сообщения: "
            "'через 2 часа', 'завтра в 10', 'в пятницу в 15:00' → ISO datetime."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Текст напоминания (что напомнить)",
                },
                "remind_at": {
                    "type": "string",
                    "description": "Дата и время в формате ISO 8601 (YYYY-MM-DDTHH:MM:SS), локальное время Алматы",
                },
            },
            "required": ["text", "remind_at"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Поиск в интернете через DuckDuckGo. "
            "Используй когда пользователь просит найти информацию в сети, "
            "контакты компании, актуальные данные и т.д."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Загрузить и прочитать содержимое веб-страницы по URL. "
            "Используй после web_search, чтобы получить подробную информацию со страницы."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL страницы для загрузки",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": (
            "Создать событие в Google Calendar. "
            "Используй когда пользователь просит запланировать встречу, звонок, дедлайн. "
            "Время указывай в часовом поясе Алматы (UTC+5)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Название события (встреча с Расулом, звонок с Али и т.д.)",
                },
                "start": {
                    "type": "string",
                    "description": "Начало в ISO 8601 (YYYY-MM-DDTHH:MM:SS), локальное время Алматы",
                },
                "end": {
                    "type": "string",
                    "description": "Конец в ISO 8601 (YYYY-MM-DDTHH:MM:SS). Если не указано — +1 час от начала",
                },
            },
            "required": ["summary", "start"],
        },
    },
    {
        "name": "request_clarification",
        "description": (
            "Задать уточняющий вопрос пользователю. "
            "Используй когда сообщение неоднозначно и нельзя определить действие."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Уточняющий вопрос",
                },
            },
            "required": ["question"],
        },
    },
]
