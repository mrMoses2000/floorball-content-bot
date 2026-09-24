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
`daemon-reload` и включает bot/worker/contact-api/miniapp/backup timer. Эти действия не выполнены
автоматически.

Проверки:

```bash
systemctl status floorball-bot floorball-worker floorball-contact-api floorball-miniapp
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

Для формы связи Plesk/Nginx проксирует `/api/contact/` на `http://127.0.0.1:8088/api/contact/`.
Перед публикацией frontend убедитесь, что contact API и worker запущены, миграция
`012_contact_requests.sql` применена, а SMTP app password проверен уникальной тестовой заявкой.

## Уведомления о готовности и публикация

Получатель должен быть заранее создан, иметь активную роль `superadmin`, выполнить `/start` и отправить боту собственный контакт. Для него включаются подписки `content_ready` и `publication_status`.

## Официальные документы

Доступ имеют только активные `federation_editor` и `superadmin`. `/documents` показывает
обязательный комплект, `/document <код>` запускает пошаговую загрузку, `/document_cancel`
отменяет её. Оригиналы находятся в `MEDIA_ROOT/official_documents/<user-id>/` с mode `0600` и
входят в обычный media backup. В БД хранятся SHA-256, имя, размер, номер, даты, статус,
разрешение на будущую публикацию и журнал действий.

Worker проверяет комплектность раз в час, но idempotency разрешает не более одного напоминания
одному получателю за ISO-неделю для одного набора проблем. Напоминание получают только
привязанные к Telegram активные пользователи с указанными ролями. Отсутствие документов видно в
health как бизнес-состояние, но не останавливает runtime. Загруженный файл остаётся закрытым до
отдельного approval-gated действия публикации.

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

Версия `@playwright/test` зафиксирована lock-файлом сайта. После каждой смены lock-файла
установите Chromium, соответствующий именно этой версии:

```bash
cd /home/moses/floorball.kz/app
npm exec -- playwright install chromium
```

Текущая ожидаемая связка — `@playwright/test 1.62.1`, Chromium `151.0.7922.34`, revision `1234`.
Не подменяйте browser executable системным Chrome: иначе воспроизводимость preview теряется.

## Publisher reconciliation

Нормальный путь проходит состояния `preview_ready → confirming → pushing → remote_verified →
published`. `confirming`, `pushing` или `remote_verified` старше lease автоматически подхватывает
worker; отдельный durable `publish_reconcile` job сохраняется до сетевого push.

Read-only диагностика строки и refs выполняется до любых ручных действий:

```bash
cd /home/moses/tg_bot_floorball_site
.venv/bin/floorball-bot health
.venv/bin/floorball-bot publish-reconcile --publication PUBLICATION_UUID
```

Если обе ветки ещё равны сохранённым base refs, reconciler повторит тот же atomic push. Если обе
уже равны expected refs, он только завершит локальное состояние и уведомление. При смешанных или
посторонних refs публикация остаётся `failed`, health показывает `ref_mismatch`, а бот запрещает
deploy. В этом случае не удаляйте worktree и не собирайте новый preview до ручной сверки:

```bash
git -C /home/moses/floorball.kz ls-remote origin refs/heads/main refs/heads/plesk-static
```

Не применяйте force-push. Сопоставьте `base_*`, `expected_*`, `main_commit` и `static_commit` из
`publication_jobs`, затем оформите обычный forward/revert commit или отмените публикацию после
зафиксированного операторского решения.

## Staging gate

Production worker должен явно иметь `PUBLISH_ENABLED=true` в защищённом EnvironmentFile. В
`.env.example` и `.env.staging.example` значение намеренно `false`; без явного production opt-in
confirm завершается до DB/Git mutation.

Один раз создайте отдельную БД от имени локального PostgreSQL-администратора:

```bash
sudo -u postgres createdb --owner=moses floorball_bot_staging
cp .env.staging.example .env.staging
chmod 600 .env.staging
```

Затем вставьте в `.env.staging` отдельный токен бота, созданного через BotFather, и реальный
loopback DSN staging-БД. Не копируйте production `TG_API_KEY`. Первый gate не запускает polling,
внешних провайдеров или почту:

```bash
set -a
. ./.env.staging
set +a
export STAGING_POSTGRES_DSN="$POSTGRES_DSN"
.venv/bin/python scripts/staging_gate.py
```

Gate разрешает только БД с suffix `_staging`/`_test`, принудительно требует
`PUBLISH_ENABLED=false` и использует local bare Git внутри тестовых каталогов. После зелёного gate
staging polling можно запускать отдельным unit только с отдельными token, DB, media и worktree.

## Отзыв согласия на медиа

Каждый readiness scan перед публичной проекцией обрабатывает медиа, у которого последнее
действующее согласие `media_publication` имеет статус `withdrawn`. Worker удаляет только файл под
`MEDIA_ROOT/derived`, очищает DB-ссылку и снимает media links с публикации. Оригинал под
`MEDIA_ROOT/originals` сохраняется; история Git не переписывается.

Для ручного восстановления используйте идемпотентную команду:

```bash
.venv/bin/floorball-bot media-consent-reconcile
```

Команда выводит только количество и UUID обработанных media assets. Если сохранённый путь выходит
за `MEDIA_ROOT/derived` или указывает не на обычный файл, reconciliation завершается ошибкой до
удаления и требует операторской проверки пути.
