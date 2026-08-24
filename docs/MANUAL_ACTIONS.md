# Manual actions remaining

- Ввести пароль sudo и установить `python3.14-venv`, PostgreSQL 18 client/server; локальный rootless PostgreSQL использовался только для integration tests.
- Утвердить юридический consent text и retention policy.
- Перенести секреты из локального `.env` в защищённый production EnvironmentFile.
- Telegram `getMe/getWebhookInfo` read-only smoke выполнен: бот доступен, webhook не настроен, очередь updates пуста. Перед запуском production polling повторить проверку.
- Предоставить короткие законные RU/KZ audio samples и разрешить AssemblyAI credit spend для external smoke.
- Создать repository-scoped GitHub deploy key и тестовую branch permission.
- Исправить рассинхронизацию `floorball.kz/app/package-lock.json` и dependency advisories отдельным frontend change.
- После подтверждённого production preview явно разрешить main push.
- После готовности `plesk-static` вручную нажать «Развернуть сейчас» в Plesk.
- Настроить и проверить off-device encrypted backup.
