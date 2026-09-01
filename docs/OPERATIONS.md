# Operations runbook

## Production layout

- code: `/opt/floorball-content-bot`;
- site clone: `/srv/floorball/repos/floorball.kz`;
- worktrees: `/srv/floorball/worktrees`;
- state/media: `/var/lib/floorball-bot`;
- backups: `/var/backups/floorball-bot`;
- secrets: `/etc/floorball-bot.env`, owner root, group floorballbot, mode 640 (or mode 600 when no group read is needed).

## Installation handoff

После всех тестов оператор создаёт непривилегированного user/group, каталоги с минимальными
правами, копирует units из `deploy/systemd`, выполняет `systemd-analyze verify`, затем
`daemon-reload` и включает bot/worker/contact-api/backup timer. Эти действия не выполнены
автоматически.

Проверки:

```bash
systemctl status floorball-bot floorball-worker floorball-contact-api
systemctl list-timers floorball-backup.timer
journalctl -u floorball-bot -u floorball-worker --since today
sudo -u floorballbot /opt/floorball-content-bot/.venv/bin/floorball-bot health
curl --fail --silent http://127.0.0.1:8088/healthz
```

## Текущий локальный запуск

На машине разработки бот и worker установлены как user units и включены в `default.target`:

```bash
systemctl --user status floorball-content-bot.service floorball-content-worker.service \
  floorball-content-backup.timer floorball-content-health.timer
systemctl --user restart floorball-content-bot.service floorball-content-worker.service
journalctl --user -u floorball-content-bot.service -u floorball-content-worker.service --since today
```

Для пользователя `moses` включён linger, поэтому units запускаются после перезагрузки без
интерактивного входа. Они используют `/home/moses/tg_bot_floorball_site/.env` с mode `0600`.
После изменения Python-кода выполните restart обоих units; после изменения только значений
`.env` также достаточно restart. Миграции перед запуском применяются явно:

```bash
cd /home/moses/tg_bot_floorball_site
.venv/bin/floorball-bot migrate
.venv/bin/floorball-bot health
```

Polling и worker записывают heartbeat в PostgreSQL. Health считается успешным только при
свежих heartbeat, свежем backup, отсутствии dead jobs/outbox и заполненном диске менее 90%.
Health timer запускает эту проверку каждые пять минут.

## Backup/restore

`scripts/backup.sh` использует `pg_dump` custom format, архивирует media, хранит 7 дневных и 4 недельных набора. DB credentials передаются libpq через environment/`PGPASSFILE`, не в argv. `scripts/restore-test.sh` отказывается работать с БД, имя которой не заканчивается `_restore_test`.

В локальном запуске безопасные wrappers получают libpq environment из `POSTGRES_DSN`, не передавая пароль в argv:

```bash
.venv/bin/python scripts/backup.py
.venv/bin/python scripts/restore_test.py var/backups/daily/database-YYYYMMDDTHHMMSSZ.dump
```

Копия на том же SSD не является полноценным backup. Настройте шифрованную копию на внешний диск/хранилище и периодически физически отключайте её. Restore drill выполняется минимум ежемесячно.

## Incident/recovery

- Telegram outage: незабранные updates остаются на стороне Telegram; fail-fast supervision завершает процесс, а systemd перезапускает polling. AssemblyAI/Codex jobs переходят в bounded retry/dead. После устранения причины повторите только dead jobs после анализа error class.
- Power loss: systemd рестартует процессы; leased `running` job снова доступен после истечения lease. Уникальные update/idempotency keys предотвращают повторные business mutations.
- Disk 80%: warning, остановить новые media uploads и выгрузить backup. 90%: critical, остановить worker/publisher до освобождения места.
- DB corruption: остановить bot/worker, сохранить повреждённый data dir read-only, восстановить последний проверенный dump в новую БД, сверить audit/publication IDs.
- Compromised token: остановить units, rotate только затронутый token/key, обновить EnvironmentFile, `daemon-reload` не нужен для value change, запустить units и проверить logs без вывода секрета.
- Wrong publication: создать новую approved revision и обычный revert commit. Не force-push и не переписывать Git history.

## Plesk handoff

После успешного main push и проверки remote `plesk-static` бот сообщает commit IDs. Оператор открывает Plesk repository `floorball-build.git`, проверяет ветку `plesk-static` и нажимает «Получить сейчас»/«Развернуть сейчас». Автоматический click не выполняется.

Проверить `https://floorball.kz`, прямой reload `/clubs/almaty`, RU/KZ/EN, hero, gallery и игрока без фото.

Для формы связи Plesk/Nginx проксирует `/api/contact/` на `http://127.0.0.1:8088/api/contact/`.
Перед публикацией frontend убедитесь, что contact API и worker запущены, миграция
`012_contact_requests.sql` применена, а SMTP app password проверен уникальной тестовой заявкой.

## Уведомления о готовности и публикация

Получатель должен быть заранее создан, иметь активную роль `superadmin`, выполнить `/start` и отправить боту собственный контакт. Для него включаются подписки `content_ready` и `publication_status`.

## Заявка на новый город

Публичная ссылка — `https://t.me/floorball_site_agent_bot?start=new_city`. Заявитель не становится
обычным пользователем и не получает контекст редактора. После отправки superadmin открывает
`/city-applications`, проверяет полноту и совпадения и нажимает «Проверено».

Инициализация всегда начинается с read-only отчёта:

```bash
.venv/bin/floorball-bot city-initialize --application APPLICATION_UUID --actor ADMIN_UUID
.venv/bin/floorball-bot city-initialize --application APPLICATION_UUID --actor ADMIN_UUID --apply
```

`--apply` требует зафиксированной проверки superadmin, берёт advisory lock по slug и создаёт
неактивный город. Перед активацией города отдельно проверьте локализации, источник, данные карты
и обычный approval-gated preview сайта.

Worker раз в минуту проверяет публичный контракт каждого города и федерации. `/readiness` запускает ту же проверку вручную. При новом полном content hash бот отправляет кнопку «Одобрить и собрать preview». После успешных тестов приходит отдельная одноразовая кнопка «Даю добро: commit и push». Слова «добро» в обычном сообщении не являются разрешением.

Если preview или push не удался, бот прямо сообщает, что развёртывание запускать нельзя. Успехом считается только подтверждённое совпадение удалённых `main` и `plesk-static` с созданными commit IDs.

## Visual preview runtime

Publisher запускает только собранный Vite preview на случайном `127.0.0.1` порту. Playwright
блокирует запросы к любому внешнему origin и создаёт RU/KZ/EN кадры `1280x720` и `390x844`.
Файлы хранятся в `<worktree-root>/_artifacts/<publication-id>` семь дней; очистка не удаляет пути
вне этого корня.

Версия `@playwright/test` зафиксирована lock-файлом сайта. На Ubuntu 26.04 Playwright 1.58.2 ещё
не распознаёт платформу официально, поэтому один раз установите его frozen Ubuntu 24.04 build:

```bash
cd /home/moses/floorball.kz/app
PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64 npm exec -- playwright install chromium
```

Текущая ожидаемая связка — Chromium `145.0.7632.6`, Playwright revision `1208`. После обновления
lock-файла повторите установку и реальный smoke. Не подменяйте browser executable системным Chrome:
иначе воспроизводимость preview теряется.
