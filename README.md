# Floorball Content Bot

Локальный approval-gated backend для управления контентом `floorball.kz` через Telegram. Приложение использует long polling, PostgreSQL queue/outbox, AssemblyAI, Agy CLI, строгие Pydantic-схемы и изолированный Git publisher.

## Состояние

Реализован production-oriented MVP scaffold: схема БД, migrations, RBAC/city scopes,
отдельная узкая роль `coach_form`, self-contact binding, durable update/job/outbox primitives,
draft state machine, RU/KZ extraction contracts, fake и реальные provider adapters, image
sanitization, deterministic city exporter/importer, автоматическая проверка готовности,
Telegram-уведомления, RU/KZ/EN desktop/mobile screenshot preview с manifest-bound confirm,
atomic Git push, проверка развёртывания Plesk, approval-gated национальные и городские новости,
Telegram-галереи новостей до десяти фотографий с manifest-bound публикацией,
приватный реестр официальных PDF с контролем комплектности и напоминаниями руководству,
Telegram Mini App с проверкой подписи `initData`, статусами доступа, прогрессом анкет и
безопасным редактированием простых полей,
автоматическое удаление публичных media derivatives после отзыва согласия,
systemd units и backup scripts.

Commit/push выполняется только после двух явных Telegram-кнопок для конкретной ревизии;
После подтверждения сборки publisher вызывает Plesk и проверяет публичные файлы.

Git push fail-closed: worker/CLI требуют явный `PUBLISH_ENABLED=true`. Первый staging gate всегда
работает с `false`, принимает только disposable `_staging`/`_test` БД, использует local bare Git и
не запускает почтовую доставку:

```bash
export STAGING_POSTGRES_DSN='postgresql:///floorball_bot_staging?host=/var/run/postgresql'
PUBLISH_ENABLED=false .venv/bin/python scripts/staging_gate.py
```

Проверяется unit/contract и PostgreSQL integration suite, Ruff, `compileall`, `pip check`,
Telegram Bot API, Agy CLI и AssemblyAI RU/KZ маршруты. Проверка качества распознавания
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
cd miniapp && npm ci && npm run lint && npm test && npm run build
```

Запуск основных процессов в отдельных терминалах:

```bash
.venv/bin/floorball-bot bot
.venv/bin/floorball-bot worker
.venv/bin/floorball-bot miniapp-api
```

Для Mini App задайте стабильный `MINI_APP_PUBLIC_URL` с HTTPS и завершающим `/`. Сервис слушает
только loopback `MINI_APP_HOST:MINI_APP_PORT`; внешний доступ публикуется reverse proxy или
туннелем. После перезапуска `bot` адрес автоматически устанавливается как Telegram menu button.
Чат и Mini App используют одну память диалогов: сложные списки, файлы и согласия бот намеренно
оставляет в чате, а отправленные анкеты в кабинете доступны только для чтения.

Публичные веб-формы и SMTP relay удалены. Сбор контента выполняется ботом и Mini App.
Стабильный вход кабинета: https://floorball.kz/bot/ (перенаправление на Funnel).
