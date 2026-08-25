# Operations runbook

## Production layout

- code: `/opt/floorball-content-bot`;
- site clone: `/srv/floorball/repos/floorball.kz`;
- worktrees: `/srv/floorball/worktrees`;
- state/media: `/var/lib/floorball-bot`;
- backups: `/var/backups/floorball-bot`;
- secrets: `/etc/floorball-bot.env`, owner root, group floorballbot, mode 640 (or mode 600 when no group read is needed).

## Installation handoff

После всех тестов оператор создаёт непривилегированного user/group, каталоги с минимальными правами, копирует units из `deploy/systemd`, выполняет `systemd-analyze verify`, затем `daemon-reload` и включает bot/worker/backup timer. Эти действия не выполнены автоматически.

Проверки:

```bash
systemctl status floorball-bot floorball-worker
systemctl list-timers floorball-backup.timer
journalctl -u floorball-bot -u floorball-worker --since today
sudo -u floorballbot /opt/floorball-content-bot/.venv/bin/floorball-bot health
```

## Текущий локальный запуск

На машине разработки бот и worker установлены как user units и включены в `default.target`:

```bash
systemctl --user status floorball-content-bot.service floorball-content-worker.service
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

## Backup/restore

`scripts/backup.sh` использует `pg_dump` custom format, архивирует media, хранит 7 дневных и 4 недельных набора. DB credentials передаются libpq через environment/`PGPASSFILE`, не в argv. `scripts/restore-test.sh` отказывается работать с БД, имя которой не заканчивается `_restore_test`.

Копия на том же SSD не является полноценным backup. Настройте шифрованную копию на внешний диск/хранилище и периодически физически отключайте её. Restore drill выполняется минимум ежемесячно.

## Incident/recovery

- Telegram/AssemblyAI/Codex outage: bot продолжает принимать updates; jobs переходят в bounded retry/dead. После устранения причины повторите только dead jobs после анализа error class.
- Power loss: systemd рестартует процессы; leased `running` job снова доступен после истечения lease. Уникальные update/idempotency keys предотвращают повторные business mutations.
- Disk 80%: warning, остановить новые media uploads и выгрузить backup. 90%: critical, остановить worker/publisher до освобождения места.
- DB corruption: остановить bot/worker, сохранить повреждённый data dir read-only, восстановить последний проверенный dump в новую БД, сверить audit/publication IDs.
- Compromised token: остановить units, rotate только затронутый token/key, обновить EnvironmentFile, `daemon-reload` не нужен для value change, запустить units и проверить logs без вывода секрета.
- Wrong publication: создать новую approved revision и обычный revert commit. Не force-push и не переписывать Git history.

## Plesk handoff

После успешного main push и проверки remote `plesk-static` бот сообщает commit IDs. Оператор открывает Plesk repository `floorball-build.git`, проверяет ветку `plesk-static` и нажимает «Получить сейчас»/«Развернуть сейчас». Автоматический click не выполняется.

Проверить `https://floorball.kz`, прямой reload `/clubs/almaty`, RU/KZ/EN, hero, gallery и игрока без фото.
