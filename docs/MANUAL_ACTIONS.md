# Manual actions remaining

- Ввести пароль sudo и установить `python3.14-venv`, PostgreSQL 18 client/server; локальный rootless PostgreSQL использовался только для integration tests.
- Утвердить юридический consent text и retention policy.
- Экспортировать секреты в защищённый production EnvironmentFile; текущий process их не видит.
- Создать/проверить Telegram bot и выполнить `getMe/getWebhookInfo` smoke.
- Предоставить короткие законные RU/KZ audio samples и разрешить AssemblyAI credit spend для external smoke.
- Создать repository-scoped GitHub deploy key и тестовую branch permission.
- Исправить рассинхронизацию `floorball.kz/app/package-lock.json` и dependency advisories отдельным frontend change.
- После подтверждённого production preview явно разрешить main push.
- После готовности `plesk-static` вручную нажать «Развернуть сейчас» в Plesk.
- Настроить и проверить off-device encrypted backup.

