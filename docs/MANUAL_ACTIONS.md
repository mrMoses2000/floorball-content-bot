# Manual actions remaining

- Ввести пароль sudo и установить `python3.14-venv`, PostgreSQL 18 client/server; локальный rootless PostgreSQL использовался только для integration tests.
- Утвердить юридический consent text и retention policy.
- Перенести секреты из локального `.env` в защищённый production EnvironmentFile.
- Telegram `getMe/getWebhookInfo` read-only smoke выполнен: бот доступен, webhook не настроен, очередь updates пуста. Перед запуском production polling повторить проверку.
- AssemblyAI RU batch и KZ streaming connectivity smoke выполнен на синтетическом аудио.
  Для проверки качества распознавания всё ещё нужны короткие законные RU/KZ речевые сэмплы.
- Создать repository-scoped GitHub deploy key и тестовую branch permission.
- Разобрать npm dependency advisories отдельным frontend change; lockfile уже синхронизирован.
- Добавить Gmail app password в `SMTP_PASSWORD`, задать `SMTP_USERNAME` и случайный
  `CONTACT_API_SECRET`, не сохраняя значения в Git.
- Установить и включить `floorball-contact-api.service`, проксировать `/api/contact/` в Plesk на
  `127.0.0.1:8088`, затем проверить уникальную заявку и письмо в `Knff@gmail.com`.
- После подтверждённого production preview явно разрешить main push.
- После готовности `plesk-static` вручную нажать «Развернуть сейчас» в Plesk.
- Настроить и проверить off-device encrypted backup.
