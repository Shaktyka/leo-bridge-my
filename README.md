# Leo

Корпоративный AI-ассистент для Respect.Chat. Работает в Matrix через E2EE, помнит пользователя между комнатами, управляет календарём, создаёт документы, ведёт личную базу знаний.

> Актуальную версию смотри в [Tags](https://github.com/slv1970/leo-bridge/tags) или через `curl http://127.0.0.1:9090/health`

## Стек

```
Matrix (Element / Synapse)
   ↓
matrix-letta-bridge (Python, matrix-nio)
   ↓
Letta (memory + tool runtime)
   ↓
Claude Sonnet 4.6 (Anthropic API)
```

Дополнительно:

- **Postgres** — личная KB (pgvector), `ai.user_memory`, `ai.user_rooms`, feedback, audit
- **Radicale** (CalDAV) — per-user календари по схеме `/username/leo/`
- **Caddy** — реверс-прокси для `cal.respectrb.ru` (TLS Let's Encrypt)
- **pandoc + WeasyPrint** — генерация документов (DOCX, PDF, XLSX, PPTX, MD, HTML)

## Возможности

**Память.** У каждого пользователя один shared `human` блок с фактами (имя, должность, таймзона, предпочтения). Один блок на все комнаты юзера — узнаёт везде. При накоплении 60k токенов агент пересоздаётся автоматически с тихим уведомлением, факты сохраняются.

**Календарь.** Создание, поиск, отмена событий через CalDAV. Apple Calendar, Outlook, iPhone подключаются по `https://cal.respectrb.ru/username/leo/`. Напоминания за N минут до начала.

**Файлы.** Leo создаёт документы прямо в чате — отчёты (DOCX/PDF), таблицы (XLSX), презентации (PPTX), заметки (MD/HTML). Достаточно попросить: «сделай отчёт», «создай презентацию», «сформируй PDF».

**Личная KB.** Загрузка документов в чат → автоматический парсинг (PDF, DOCX, MD, TXT, XLSX, CSV, JSON), эмбеддинги OpenAI, гибридный поиск (ключевые слова + семантика).

**Корпоративная KB.** Общие документы компании, поиск через `kb_search_corporate`.

**Интернет-поиск.** Tool `internet_search` для актуальных данных (Tavily API).

**Feedback.** Реакции 👍 / 👎 на ответы Leo пишутся в Postgres, дашборд показывает тренд.

**Smart threading.** В групповых комнатах ответы Leo идут в треде, в DM — в основной ленте.

**Empty rooms cleanup.** Если пользователь покинул комнату и остался только Leo — Leo автоматически выходит, агент удаляется.

**Orphan auto-heal.** Legacy-агенты с per-agent блоками автоматически приводятся к актуальной архитектуре shared при первой активности.

## Slash-команды

```
/leo_help     справка
/leo_reset    сброс агента (память пользователя сохраняется)
/kb_help      справка по личной KB
/kb_list      список загруженных документов
/kb_search    поиск по KB
/kb_info      инфо о документе
/kb_delete    удалить документ
```

## Структура репозитория

```
app/
  bridge.py             Matrix-клиент, callbacks, _on_message, auto-compact
  letta_client.py       HTTP-клиент к Letta API
  internal_api.py       FastAPI на :8284 для tools (calendar/kb/files)
  calendar_client.py    CalDAV клиент для Radicale
  kb_handler.py         ingest файлов в личную KB
  reminders.py          ReminderWatcher (FS-scan ICS)
  feedback_handler.py   реакции и slash для feedback
  feedback_logger.py    запись в Postgres
  audit.py              audit log
  leo_handler.py        /leo_* slash-команды
  room_mapping.py       bridge.sqlite room→agent
  timezone_resolver.py  user_tz из Matrix profile
  file_generators/      генераторы документов (md/html/docx/pdf/xlsx/pptx)

scripts/
  register_tools.py            регистрация custom tools в Letta
  attach_tools_to_agents.py    массовый attach к агентам
  update_system_prompt.py      массовое обновление SYSTEM_PROMPT

requirements.txt
.gitignore
```

## Развёртывание

Сервер: OVH SYS-1, Ubuntu 24.

Systemd-сервисы:

- `matrix-letta-bridge` — основной bridge (port 9090 health)
- `bridge-internal-api` — FastAPI для tools (port 8284)
- `ai-radicale` — CalDAV (Docker, port 5232)
- `caddy` — reverse proxy (TLS)

Конфиг: `/opt/ai/bridge/.env` (не в git).

## Разработка

Все команды через `sudo -u ai`:

```bash
cd /opt/ai/bridge

# Проверить статус
git status

# Закоммитить изменения
git add -A
git commit -m "vX.Y.Z: описание"
git push

# Релизный тег
git tag -a vX.Y.Z -m "..."
git push origin vX.Y.Z

# Перезапуск сервисов
sudo systemctl restart matrix-letta-bridge
sudo systemctl restart bridge-internal-api
```

## История изменений

См. [Tags](https://github.com/slv1970/leo-bridge/tags) или `git log --oneline`.

## Лицензия

Proprietary — ProjectDB.
