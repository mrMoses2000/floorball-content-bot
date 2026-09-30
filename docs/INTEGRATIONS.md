# Настройка интеграций

## Telegram

1. Создайте отдельного бота через BotFather и отключите privacy mode только если это действительно нужно для групп.
2. Поместите токен в защищённый `TG_API_KEY`.
3. Запустите `floorball-bot bot`. Процесс вызывает `getWebhookInfo`; webhook удаляется только у этого токена и с `drop_pending_updates=false`.
4. Привилегированный пользователь должен быть заранее создан CLI-командой. При создании по
   телефону `/start` предлагает `request_contact`; текстовый телефон не авторизует. При создании
   по Telegram ID существующий активный actor сразу получает меню назначенных ему ролей.
5. Публичная ссылка `https://t.me/floorball_site_agent_bot?start=coach` разрешает self-contact
   onboarding только в роль `coach_form`. Она не выдаёт city scope, review или publish права.

Long polling не требует домена, HTTPS, Cloudflare Tunnel, port forwarding или публичного входящего порта.

## Публикация сайта

Бот собирает анкеты и файлы, reviewer утверждает ревизии. Publisher строит
preview и ждёт отдельное подтверждение конкретной сборки. Затем атомарно
обновляет main/plesk-static, вызывает приватные Plesk webhooks и сверяет
публичный index.html и изменённые assets с manifest. При сбое развёртывания
сохраняется remote_verified; существующая reconciliation повторяет проверку.

Contact API / SMTP и веб-формы сбора удалены: они не использовались в production.
Телефонное подтверждение личности в Telegram сохранено.


## AssemblyAI

- Пользовательское имя секрета: `ASSEMBLI_AI`; также принят canonical alias `ASSEMBLYAI_API_KEY`.
- RU и другие проверенные batch-языки идут через pre-recorded API с закреплённой
  `universal-2`. RU проверен синтетической записью 30 сентября 2026; `universal-3-pro`
  не подходит этому пути и заменён в текущем API.
- KZ/KK маршрутизируется в Whisper Streaming (`whisper-rt`) после ffmpeg 16 kHz mono conversion и отправляется с wall-clock pacing. Session всегда получает `Terminate`.
- Обычные тесты используют `FakeTranscriber` и не расходуют credits.
- Для external smoke подготовьте короткие аудио с юридически допустимым содержимым и запустите отдельный тестовый сценарий; production-голоса не используйте как тестовые fixtures.

## Agy CLI

Проверьте `agy --version` и авторизацию для того же Linux user, от которого работает worker.
На проверенном сервере закреплена модель `gemini-3.8-flash-high` с effort `high`, print mode,
`--sandbox`, отключённые slash-команды и строгую JSON Schema. Процесс запускается прямым argv
без shell; Telegram/AssemblyAI secrets в дочернее окружение не наследуются. Значения можно
переопределить через `AGY_CLI`, `AGY_MODEL`, `AGY_EFFORT` и `AGY_TIMEOUT_SECONDS`.

## GitHub deploy key

1. Создайте отдельную ED25519 key pair от имени `floorballbot` без использования личного PAT.
2. Добавьте public key как write-enabled deploy key только в `Sherzattv/floorball.kz`.
3. Создайте SSH alias `github-floorball` с `IdentitiesOnly yes` и pin GitHub host key после ручной проверки fingerprint.
4. Измените repository remote на `git@github-floorball:Sherzattv/floorball.kz.git` только после тестового fetch/push в отдельную ветку.

Никогда не добавляйте private key в репозиторий или `/etc/floorball-bot.env`.
