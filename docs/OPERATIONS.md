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
`daemon-reload` и включает bot/worker/miniapp/backup timer. Эти действия не выполнены
автоматически.

Проверки:

```bash
systemctl status floorball-bot floorball-worker floorball-miniapp
systemctl list-timers floorball-backup.timer
journalctl -u floorball-bot -u floorball-worker --since today
sudo -u floorballbot /opt/floorball-content-bot/.venv/bin/floorball-bot health
curl --fail --silent http://127.0.0.1:8088/healthz
curl --fail --silent http://127.0.0.1:8092/healthz
```

## Текущий локальный запуск

На машине разработки бот и worker установлены как user units и включены в `default.target`:

```bash
systemctl --user status floorball-content-bot.service floorball-content-worker.service \
  floorball-content-miniapp.service floorball-content-backup.timer floorball-content-health.timer
systemctl --user restart floorball-content-bot.service floorball-content-worker.service \
  floorball-content-miniapp.service
journalctl --user -u floorball-content-bot.service -u floorball-content-worker.service \
  -u floorball-content-miniapp.service --since today
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

Mini App публикуется по стабильному HTTPS URL с завершающим `/`. Для текущего домашнего сервера:

```bash
tailscale funnel --bg --set-path /floorball-miniapp http://127.0.0.1:8092
tailscale funnel status
```

В `/home/moses/tg_bot_floorball_site/.env` сейчас используются:

```dotenv
MINI_APP_TUNNEL_PROVIDER=tailscale
MINI_APP_PUBLIC_URL=https://moses-cv.tail55e85c.ts.net/floorball-miniapp/
AGY_MODEL=gemini-3.8-flash-high
```

`floorball-content-tunnel.service` восстанавливает только `/floorball-miniapp` и
проверяет публичный `/healthz` каждые 30 секунд. Существующие `/` и
`/tenders-miniapp` сохранены. Background Funnel принадлежит `tailscaled` и
возобновляется после перезапуска; требуются работающий tailscaled и разрешение
Funnel в tailnet. Cloudflare Quick Tunnel доступен как временный режим
`MINI_APP_TUNNEL_PROVIDER=cloudflare`.

Проверить фактический restart и состояния:

```bash
systemctl --user restart floorball-content-bot floorball-content-worker \
  floorball-content-miniapp floorball-content-tunnel
systemctl --user is-active floorball-content-bot floorball-content-worker \
  floorball-content-miniapp floorball-content-tunnel
loginctl show-user moses -p Linger
curl --fail https://moses-cv.tail55e85c.ts.net/floorball-miniapp/healthz
```

`health` теперь также проверяет `failed_updates`. При ненулевом значении изучите
`processed_updates` и журналы polling, устраните причину и разберите каждый
ошибочный update. Запись о сбое нельзя просто удалять ради зелёного health.
Успешный health показывает состояние процессов, очередей и backup, но не
подтверждает доставку SMTP, качество распознавания речи или обновление Plesk.

После изменения URL перезапустите `floorball-content-bot.service`: он синхронизирует Telegram
menu button. API принимает только подписанный Telegram `initData`; прямой запрос к
`/api/miniapp/v1/bootstrap` без заголовка `Authorization: tma ...` должен вернуть `401`.

## Backup/restore

`scripts/backup.sh` использует `pg_dump` custom format, архивирует media, хранит 7 дневных и 4 недельных набора. DB credentials передаются libpq через environment/`PGPASSFILE`, не в argv. `scripts/restore-test.sh` отказывается работать с БД, имя которой не заканчивается `_restore_test`.

В локальном запуске безопасные wrappers получают libpq environment из `POSTGRES_DSN`, не передавая пароль в argv:

```bash
.venv/bin/python scripts/backup.py
.venv/bin/python scripts/restore_test.py var/backups/daily/database-YYYYMMDDTHHMMSSZ.dump
```

Копия на том же SSD не является полноценным backup. Настройте шифрованную копию на внешний диск/хранилище и периодически физически отключайте её. Restore drill выполняется минимум ежемесячно.

## Incident/recovery

- Telegram outage: незабранные updates остаются на стороне Telegram; fail-fast supervision завершает процесс, а systemd перезапускает polling. AssemblyAI/Agy jobs переходят в bounded retry/dead. После устранения причины повторите только dead jobs после анализа error class.
- Power loss: systemd рестартует процессы; leased `running` job снова доступен после истечения lease. Уникальные update/idempotency keys предотвращают повторные business mutations.
- Disk 80%: warning, остановить новые media uploads и выгрузить backup. 90%: critical, остановить worker/publisher до освобождения места.
- DB corruption: остановить bot/worker, сохранить повреждённый data dir read-only, восстановить последний проверенный dump в новую БД, сверить audit/publication IDs.
- Compromised token: остановить units, rotate только затронутый token/key, обновить EnvironmentFile, `daemon-reload` не нужен для value change, запустить units и проверить logs без вывода секрета.
- Wrong publication: создать новую approved revision и обычный revert commit. Не force-push и не переписывать Git history.

## Plesk handoff

После успешного push и проверки обеих удалённых веток бот сообщает commit IDs. Публичный сайт берёт готовую сборку из Plesk repository `floorball-build.git`: ветка `plesk-static`, каталог `/httpdocs`. Оператор на этой карточке нажимает «Получить сейчас», сверяет полученный commit с сообщением бота и, если автоматическое развёртывание не произошло, нажимает «Развернуть сейчас». Карточка `floorball.kz.git` с веткой `main` обновляет только исходники в `/app-src` и не меняет публичный сайт. Автоматический click не выполняется.

Проверить `https://floorball.kz`, прямой reload `/clubs/almaty`, RU/KZ/EN, hero, gallery и игрока без фото.

Для формы связи на текущем домашнем сервере нужен отдельный постоянный публичный
Публикация не требует contact API или SMTP. PLESK_STATIC_WEBHOOK_URL и
PLESK_SOURCE_WEBHOOK_URL хранятся в приватной .env; никогда не публикуйте их.
После атомарного Git push publisher проверяет развёрнутые bytes. Сбой остаётся
видимым в remote_verified и повторяется reconciliation. До завершения проверки
бот не сообщает об успешном развёртывании.
