# Floorball Content Bot

Локальный approval-gated backend для управления контентом `floorball.kz` через Telegram. Приложение использует long polling, PostgreSQL queue/outbox, AssemblyAI, Codex CLI, строгие Pydantic-схемы и изолированный Git publisher.

## Состояние

Реализован production-oriented MVP scaffold: схема БД, migrations, RBAC/city scopes, self-contact binding, durable update/job/outbox primitives, draft state machine, RU/KZ extraction contracts, fake и реальные provider adapters, image sanitization, deterministic city exporter/importer, publication preview/confirm core, systemd units и backup scripts.

Production push, systemd installation и Plesk deployment намеренно не выполняются автоматически.

Проверено локально: 26 unit/contract tests, 9 integration tests на PostgreSQL 18,
Ruff, `compileall` и `pip check`. Read-only smoke прошёл для Telegram Bot API и
реальный structured extraction smoke — для Codex CLI. AssemblyAI smoke ожидает
разрешённые тестовые RU/KZ аудиофайлы, чтобы не расходовать API-кредиты без явного
согласия.

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

Запуск в двух терминалах:

```bash
.venv/bin/floorball-bot bot
.venv/bin/floorball-bot worker
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
