# Floorball Content Bot

Локальный approval-gated backend для управления контентом `floorball.kz` через Telegram. Приложение использует long polling, PostgreSQL queue/outbox, AssemblyAI, Codex CLI, строгие Pydantic-схемы и изолированный Git publisher.

## Состояние

Реализован production-oriented MVP scaffold: схема БД, migrations, RBAC/city scopes,
отдельная узкая роль `coach_form`, self-contact binding, durable update/job/outbox primitives,
draft state machine, RU/KZ extraction contracts, fake и реальные provider adapters, image
sanitization, deterministic city exporter/importer, автоматическая проверка готовности,
Telegram-уведомления, двухшаговый publication preview/confirm, atomic Git push, durable contact
requests с асинхронной SMTP-доставкой, approval-gated национальные и городские новости,
systemd units и backup scripts.

Commit/push выполняется только после двух явных Telegram-кнопок для конкретной ревизии;
Plesk deployment остаётся ручным.

Проверяется unit/contract и PostgreSQL integration suite, Ruff, `compileall`, `pip check`,
Telegram Bot API, Codex CLI и AssemblyAI RU/KZ маршруты. Проверка качества распознавания
всё ещё требует разрешённых речевых RU/KZ сэмплов.

## Быстрый старт

```bash
sudo apt install python3.14-venv postgresql postgresql-client ffmpeg
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]' --constraint requirements.lock
cp .env.example .env
.venv/bin/floorball-bot migrate
.venv/bin/pytest -q
.venv/bin/ruff check src tests
```

Запуск основных процессов в отдельных терминалах:

```bash
.venv/bin/floorball-bot bot
.venv/bin/floorball-bot worker
```

После настройки `CONTACT_API_SECRET` (не менее 32 случайных байт), SMTP и reverse proxy
`/api/contact` на `127.0.0.1:8088` запустите API формы связи:

```bash
.venv/bin/floorball-bot contact-api
```

Сначала создайте пользователя и назначьте права:

```bash
.venv/bin/floorball-bot create-user --phone '+7 700 000 00 00' --name 'Администратор'
.venv/bin/floorball-bot grant-role --user UUID --role superadmin
.venv/bin/floorball-bot scope-city --user UUID --city CITY_UUID
```

Текущие bundles импортируются сначала dry-run, затем явным `--apply`:

```bash
.venv/bin/floorball-bot import-city /home/moses/floorball.kz/app/src/data/generated/city-content.json
.venv/bin/floorball-bot import-city /home/moses/floorball.kz/app/src/data/generated/city-content.json --apply
.venv/bin/floorball-bot import-federation /home/moses/floorball.kz/app/src/data/generated/federation-content.json --apply
```

Подробности: [архитектура](docs/ARCHITECTURE.md), [операции](docs/OPERATIONS.md), [настройка интеграций](docs/INTEGRATIONS.md), [privacy/consent](docs/PRIVACY_CONSENT.md).
