# Discovery report

Дата аудита: 2026-08-24. Аудит выполнен локально, без чтения секретов и без production-изменений.

## Окружение

- Ubuntu 26.04 LTS, x86_64, ядро 7.0.0.
- 7.2 GiB RAM, 4 GiB swap; доступно около 3 GiB RAM.
- SSD 219 GiB, занято 24% (пороги: warning 80%, critical 90%).
- Python 3.14.4, Node 22.23.1, npm 10.9.8, Git 2.53.0.
- Agy CLI 1.2.7 и ffmpeg установлены.
- `psql`/PostgreSQL и `cloudflared` не установлены. Tunnel не нужен для MVP long polling.
- В текущем процессе `ASSEMBLI_AI` и `TG_API_KEY` не экспортированы. Файл `.env` существует; его содержимое не читалось и не выводилось.

## Репозиторий сайта

- Источник: `/home/moses/floorball.kz`.
- Ветка `main` чистая и совпадает с `origin/main` на `ca0b684`.
- Remote: `git@github-account1:Sherzattv/floorball.kz.git`.
- Сайт — React/Vite SPA; production публикуется готовой статической веткой `plesk-static`.
- Baseline после локальной установки зависимостей без изменения lock-файла:
  - Vitest: 6 файлов, 24 теста — pass;
  - ESLint — pass;
  - Vite build — pass, `app/dist/index.html` и `.htaccess` созданы.

## Найденные риски сайта

- `npm ci` не воспроизводим: `package-lock.json` не синхронизирован с manifest и не содержит ожидаемые записи esbuild 0.27.7. Для baseline применён `npm install --package-lock=false`; source/lock сайта не менялись.
- `npm audit` после разрешения актуального dependency graph сообщает 14 уязвимостей (9 high, 4 moderate, 1 low), включая старые Vite/React Router transitive dependencies. Обновление сайта — отдельная задача и не должно смешиваться с backend scaffold.
- Публичный sanitizer удаляет переносы строк через `\s+`; длинные тексты ограничиваются, но абзацы фактически схлопываются. Экспортер бота не должен молча обрезать данные: превышение лимита блокирует публикацию.
- Контакты клуба входят в текущий public JSON. Экспортер включает их только при отдельном разрешении на публикацию.

## Подтверждённый public contract

- Оба payload имеют `ok: true`, `version: 1`, `generatedAt`.
- Городской payload содержит `cities`; текущий bundle содержит 7 городов.
- Новый город появляется в списке и получает `/clubs/:slug`, если есть `nameRu`, `nameKz`, `nameEn`. Без координат он остаётся в списке и не отображается маркером карты.
- Frontend ограничивает `players_list` до 15, `gallery` до 30, `schedule` до 20, `clubs_list` до 10.
- Federation payload содержит `mission`, `history`, `achievements`, `roadmap`, `leadership`.
- Sync scripts уже поддерживают `--source`, что позволяет publisher передавать локальные детерминированные payloads.

## Reference projects

`/home/moses/shermos_bot` изучен только read-only. Полезные паттерны: asyncpg codecs, numbered SQL migrations, subprocess process-group termination и fake transcription. Redis-архитектура reference-проекта не переносится: очередь этого проекта хранится только в PostgreSQL.

## Решения аудита

- Backend создаётся в текущем отдельном корне `/home/moses/tg_bot_floorball_site`, не внутри React-приложения.
- Runtime — два процесса: long-polling ingress/outbox и worker; publisher запускается отдельной командой/service.
- PostgreSQL является единственным state/queue store.
- Никакой production push, установки systemd units, Plesk deploy или изменения DNS без отдельного подтверждения.
