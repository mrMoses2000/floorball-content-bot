# Manual actions remaining

- Ввести пароль sudo и создать постоянную staging-БД:
  `sudo -u postgres createdb --owner=moses floorball_bot_staging`. PostgreSQL 18 уже работает;
  текущий staging gate успешно прошёл на disposable `floorball_bot_test`.
- Создать отдельного Telegram-бота через BotFather, записать его token только в `.env.staging` и
  никогда не использовать production token вместе со staging-БД.
- Утвердить юридический consent text и retention policy.
- Перенести секреты из локального `.env` в защищённый production EnvironmentFile.
- Telegram `getMe/getWebhookInfo` read-only smoke выполнен: бот доступен, webhook не настроен, очередь updates пуста. Перед запуском production polling повторить проверку.
- Для рассылки тренерам использовать `https://t.me/floorball_site_agent_bot?start=coach`;
  self-contact onboarding выдаёт только анкетную роль `coach_form`.
- AssemblyAI RU batch и KZ streaming connectivity smoke выполнен на синтетическом аудио.
  Для проверки качества распознавания всё ещё нужны короткие законные RU/KZ речевые сэмплы.
- Backend remote `github-account2:mrMoses2000/floorball-content-bot.git` настроен и проверен push.
  Для автоматического production publisher всё ещё нужна отдельная минимальная deploy key/branch
  policy вместо общего интерактивного SSH-доступа.
- Для почтового этапа добавить Gmail app password в `SMTP_PASSWORD`, задать
  `SMTP_USERNAME=Knff@gmail.com` и случайный `CONTACT_API_SECRET`, не сохраняя значения в Git.
- После появления SMTP credential включить `floorball-contact-api.service`, создать на домашнем
  сервере постоянный HTTPS route `/floorball-contact` через Tailscale Funnel (или named
  Cloudflare Tunnel), задать его полный URL в `VITE_CONTACT_API_URL`, пересобрать и опубликовать
  `plesk-static`. Затем проверить уникальную заявку и письмо в `Knff@gmail.com`.
- После подтверждённого production preview явно разрешить main push.
- После готовности `plesk-static` вручную нажать «Развернуть сейчас» в Plesk.
- Настроить и проверить off-device encrypted backup.
